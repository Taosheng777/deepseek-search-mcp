"""HTTP client for DeepSeek Search/Fetch with retries, caching and a spend cap."""

import asyncio
import ipaddress
import json
import os
import random
import socket
import subprocess
import tempfile
import time
from collections import OrderedDict
from contextlib import contextmanager
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlsplit

import httpx

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None
    import msvcrt


@contextmanager
def _exclusive_lock(path: Path):
    """Cross-process exclusive lock held for the duration of the block."""
    with open(path, "a+") as f:
        if fcntl is not None:
            fcntl.flock(f, fcntl.LOCK_EX)  # released when the file closes
            yield
            return
        f.seek(0)
        while True:
            try:
                msvcrt.locking(f.fileno(), msvcrt.LK_LOCK, 1)
                break
            except OSError:
                time.sleep(0.05)
        try:
            yield
        finally:
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)


from .clean import as_text, fetch_route_hint, unescape_markdown

BASE_URL = "https://api.deepseek.com/v1/tools/"
KEYCHAIN_SERVICE = "deepseek-api-key"
SECURITY_BIN = "/usr/bin/security"
RETRYABLE = {408, 429, 500, 502, 503, 504}
MIN_RETRY_WINDOW = 3.0
STATUS_HINTS = {
    400: "请求参数不合法",
    401: "API key 缺失或无效，请检查钥匙串里的 deepseek-api-key",
    402: "DeepSeek 账号余额不足，请先充值",
    403: "该网址被 DeepSeek 安全策略拒绝抓取",
    404: "DeepSeek 抓不到这个网页的正文（网址失效、被站点拦截或内容类型不支持）",
    408: "DeepSeek 读取请求超时",
    413: "请求体过大",
    429: "DeepSeek 请求频率超限，稍后再试",
}
_NAT64 = ipaddress.ip_network("64:ff9b::/96")


class ApiError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status
        self.uncertain = 0  # attempts that timed out client-side and may have been billed


def load_api_key() -> str:
    key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not key:
        try:
            out = subprocess.run(
                [SECURITY_BIN, "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"],
                capture_output=True, text=True, timeout=30,
            )
        except subprocess.TimeoutExpired:
            raise ApiError("读取钥匙串超时（可能在等待授权弹窗），请在弹窗中允许后重试")
        except (OSError, subprocess.SubprocessError):
            out = None
        key = out.stdout.strip() if out and out.returncode == 0 else ""
    if not key:
        raise ApiError(f"找不到 DeepSeek API key：请设置环境变量 DEEPSEEK_API_KEY 或在钥匙串存入服务名 {KEYCHAIN_SERVICE}")
    if not key.isascii() or any(c.isspace() or not c.isprintable() for c in key):
        raise ApiError("DeepSeek API key 格式不对（包含空白或不可见字符），请重新存入钥匙串")
    return key


def validate_query(query: str) -> str:
    q = as_text(query).strip()
    if not q:
        raise ValueError("query 不能为空")
    if len(q) > 128:
        raise ValueError(f"query 最多 128 个字符，当前 {len(q)} 个，请精简关键词")
    return q


def _embedded_ipv4(ip: ipaddress.IPv6Address):
    if ip.ipv4_mapped:
        return ip.ipv4_mapped
    if ip in _NAT64 or int(ip) >> 32 == 0:  # NAT64 or deprecated IPv4-compatible ::a.b.c.d
        return ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    return None


def _is_private_host(host: str) -> bool:
    host = host.rstrip(".")
    if not host:
        return True
    if host.startswith("[") or ":" in host:
        try:
            ip = ipaddress.IPv6Address(host.strip("[]"))
        except ValueError:
            return True
        inner = _embedded_ipv4(ip)
        return not ip.is_global or (inner is not None and not inner.is_global)
    try:
        ascii_host = host.encode("idna").decode("ascii").lower()
    except UnicodeError:
        return True
    if ascii_host == "localhost" or ascii_host.endswith((".localhost", ".local", ".internal", ".lan", ".home.arpa")):
        return True
    try:  # accepts every legacy IPv4 spelling: 127.1, 0177.0.0.1, 0x7f.0.0.1, 2130706433
        packed = socket.inet_aton(ascii_host)
    except OSError:
        return False
    return not ipaddress.IPv4Address(packed).is_global


def validate_url(url: str) -> str:
    u = as_text(url).strip()
    if any(c == "\\" or not c.isprintable() or c.isspace() for c in u):
        raise ValueError("网址包含反斜杠、空白或控制字符")
    try:
        parts = urlsplit(u)
        hostname = parts.hostname
    except ValueError:
        raise ValueError("网址格式不合法")
    if parts.scheme not in {"http", "https"} or not hostname:
        raise ValueError("只能抓取带域名的 http/https 网址")
    if parts.username or parts.password:
        raise ValueError("网址不能包含用户名或密码")
    if len(u) > 8192:
        raise ValueError("网址过长（上限 8192 字符）")
    netloc_host = parts.netloc.rsplit("@", 1)[-1]
    if netloc_host.startswith("["):
        hostname = netloc_host[: netloc_host.find("]") + 1]
    if _is_private_host(hostname):
        raise ValueError("不抓取本机或内网地址")
    return u


class Budget:
    """Daily/monthly count of billable calls, shared across processes via a locked JSON file.

    Calls reserve a slot before the request and are refunded only when the call
    certainly was not billed, so concurrent callers cannot overshoot the cap."""

    def __init__(self, path: Path, daily_limit: int, monthly_limit: int):
        self.path = Path(path)
        self.daily_limit = daily_limit
        self.monthly_limit = monthly_limit

    def _locked(self, mutate):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with _exclusive_lock(self.path.with_suffix(".lock")):
                data = self._read()
                if mutate(data):
                    self._write(data)
                return dict(data)
        except OSError as e:
            raise ApiError(f"无法读写用量文件 {self.path}（{type(e).__name__}），为防误扣费暂停调用")

    def _read(self) -> dict:
        try:
            raw = self.path.read_text()
        except FileNotFoundError:
            raw = ""
        data = {}
        if raw.strip():
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                data = None
            if not isinstance(data, dict) or not all(
                type(data.get(k, 0)) is int and data.get(k, 0) >= 0 for k in ("day_count", "month_count")
            ):
                raise ApiError(f"用量文件 {self.path} 已损坏，为防误扣费暂停调用；确认后删除该文件即可恢复")
        now = datetime.now()
        day, month = now.strftime("%Y-%m-%d"), now.strftime("%Y-%m")
        if data.get("day") != day:
            data["day"], data["day_count"] = day, 0
        if data.get("month") != month:
            data["month"], data["month_count"] = month, 0
        data.setdefault("day_count", 0)
        data.setdefault("month_count", 0)
        return data

    def _write(self, data: dict) -> None:
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".usage-")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(data, f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    def usage(self) -> dict:
        return self._locked(lambda d: False)

    def reserve(self) -> None:
        def take(d):
            if d["day_count"] >= self.daily_limit:
                raise ApiError(f"今日 DeepSeek 搜索调用已达上限 {self.daily_limit} 次（防止意外扣费），明天自动恢复")
            if d["month_count"] >= self.monthly_limit:
                raise ApiError(f"本月 DeepSeek 搜索调用已达上限 {self.monthly_limit} 次（防止意外扣费）")
            d["day_count"] += 1
            d["month_count"] += 1
            return True

        self._locked(take)

    def adjust(self, delta: int) -> None:
        """Correct a reservation once the call's billing outcome is known."""
        if not delta:
            return

        def change(d):
            d["day_count"] = max(0, d["day_count"] + delta)
            d["month_count"] = max(0, d["month_count"] + delta)
            return True

        self._locked(change)

    def refund(self) -> None:
        self.adjust(-1)

    def record(self) -> None:
        """Count a call unconditionally (kept for tooling)."""
        def inc(d):
            d["day_count"] += 1
            d["month_count"] += 1
            return True

        self._locked(inc)


class TTLCache:
    def __init__(self, ttl: float = 900, maxsize: int = 256):
        self.ttl, self.maxsize = ttl, maxsize
        self._data: OrderedDict = OrderedDict()

    def get(self, key):
        hit = self._data.get(key)
        if hit and hit[0] > time.monotonic():
            self._data.move_to_end(key)
            return hit[1]
        self._data.pop(key, None)
        return None

    def put(self, key, value):
        self._data[key] = (time.monotonic() + self.ttl, value)
        self._data.move_to_end(key)
        while len(self._data) > self.maxsize:
            self._data.popitem(last=False)


def _retry_after_seconds(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())


class DeepSeekClient:
    def __init__(
        self,
        api_key: str,
        budget: Budget,
        transport: httpx.AsyncBaseTransport | None = None,
        search_timeout: float = 15.0,
        fetch_timeout: float = 35.0,
        deadline: float = 90.0,
        max_attempts: int = 4,
        sleep=asyncio.sleep,
    ):
        self._key = api_key
        self._timeouts = {"search": search_timeout, "fetch": fetch_timeout}
        self.budget = budget
        self._deadline = deadline
        self._max_attempts = max_attempts
        self._sleep = sleep
        self._cache = TTLCache()
        self._inflight: dict = {}
        self._http = httpx.AsyncClient(
            base_url=BASE_URL,
            transport=transport,
            headers={"Authorization": f"Bearer {api_key}", "User-Agent": "deepseek-search-mcp/0.3"},
            follow_redirects=False,
        )

    def _redact(self, text: str) -> str:
        return text.replace(self._key, "sk-***") if len(self._key) >= 8 else text

    def _error(self, status: int | None, resp: httpx.Response | None = None, detail: str = "") -> ApiError:
        msg = ""
        if resp is not None:
            try:
                body = resp.json()
            except ValueError:
                body = None
            err = body.get("error") if isinstance(body, dict) else None
            msg = as_text(err.get("message")) if isinstance(err, dict) else as_text(err)
            msg = msg or ("" if body is not None else resp.text[:200])
        hint = STATUS_HINTS.get(status, "DeepSeek 服务暂时不可用，稍后再试")
        parts = [f"HTTP {status}" if status else "网络错误", hint, msg[:300] or detail]
        return ApiError(self._redact("：".join(p for p in parts if p)), status)

    async def _post(self, path: str, body: dict, base_delay: float = 1.0) -> tuple[dict, int]:
        """POST with retries. Returns (data, uncertain); errors carry .uncertain too."""
        start = time.monotonic()
        last: ApiError | None = None
        uncertain = 0

        def fail(err: ApiError) -> ApiError:
            err.uncertain = uncertain
            return err

        for attempt in range(1, self._max_attempts + 1):
            remaining = self._deadline - (time.monotonic() - start)
            if remaining <= 0 or (attempt > 1 and remaining < MIN_RETRY_WINDOW):
                break
            retry_after = None
            try:
                async with asyncio.timeout(remaining):
                    resp = await self._http.post(path, json=body, timeout=min(self._timeouts[path], remaining))
            except TimeoutError:
                uncertain += 1
                last = ApiError("DeepSeek 请求超时（已达总时限）")
                break
            except httpx.TimeoutException as e:
                uncertain += 1
                last = self._error(None, detail=type(e).__name__)
            except (httpx.NetworkError, httpx.RemoteProtocolError) as e:
                last = self._error(None, detail=type(e).__name__)
            except httpx.DecodingError:
                raise fail(ApiError("DeepSeek 返回的数据无法解码", 200))
            except httpx.HTTPError as e:  # deterministic local errors: proxy, protocol, unsupported URL
                raise fail(self._error(None, detail=type(e).__name__))
            else:
                if resp.status_code == 200:
                    try:
                        data = resp.json()
                    except ValueError:
                        raise fail(ApiError("DeepSeek 返回了无法解析的内容", 200))
                    if not isinstance(data, dict):
                        raise fail(ApiError("DeepSeek 返回了无法识别的数据结构", 200))
                    return data, uncertain
                last = self._error(resp.status_code, resp)
                if resp.status_code not in RETRYABLE:
                    raise fail(last)
                retry_after = _retry_after_seconds(resp.headers.get("retry-after"))
            if attempt == self._max_attempts:
                break
            delay = min(base_delay * 2 ** (attempt - 1), 16) + random.uniform(0, 0.5)
            if retry_after is not None:
                delay = max(delay, retry_after)
            if time.monotonic() - start + delay + MIN_RETRY_WINDOW > self._deadline:
                if retry_after is not None and last is not None:
                    last = ApiError(f"{last}；服务器要求等待约 {round(retry_after)} 秒后再试", last.status)
                break
            await self._sleep(delay)
        raise fail(last or ApiError("DeepSeek 请求超时"))

    async def _billed_call(self, path: str, body: dict, base_delay: float, is_billable) -> dict:
        """Reserve one call up front, then settle to the conservative billed count:
        a billable 200 (or an undecodable 200) plus every client-side timeout."""
        self.budget.reserve()
        try:
            data, uncertain = await self._post(path, body, base_delay)
        except ApiError as e:
            self.budget.adjust(e.uncertain + (1 if e.status == 200 else 0) - 1)
            raise
        self.budget.adjust(uncertain + (1 if is_billable(data) else 0) - 1)
        return data

    async def _dedup(self, key, factory):
        if (hit := self._cache.get(key)) is not None:
            return hit
        entry = self._inflight.get(key)
        if entry is None:
            async def run_and_cache():
                value = await factory()
                self._cache.put(key, value)
                return value

            entry = {"task": asyncio.ensure_future(run_and_cache()), "waiters": 0}
            self._inflight[key] = entry

            def done(task, entry=entry):
                if self._inflight.get(key) is entry:
                    del self._inflight[key]
                if not task.cancelled():
                    task.exception()  # mark retrieved; waiters re-raise it themselves

            entry["task"].add_done_callback(done)
        entry["waiters"] += 1
        try:
            return await asyncio.shield(entry["task"])
        except asyncio.CancelledError:
            if entry["waiters"] == 1 and not entry["task"].done():
                entry["task"].cancel()  # last caller gone: stop retrying in the background
            raise
        finally:
            entry["waiters"] -= 1

    async def search(self, query: str, count: int = 10, need_content: bool = False) -> list[dict]:
        query = validate_query(query)
        count = max(1, min(int(count), 50))
        body = {"query": query, "count": count, "need_content": bool(need_content)}

        async def go():
            data = await self._billed_call("search", body, 1.0,
                                           lambda d: isinstance(d.get("results"), list) and bool(d["results"]))
            results = data.get("results")
            return [r for r in results if isinstance(r, dict)] if isinstance(results, list) else []

        return await self._dedup(("search", json.dumps(body, sort_keys=True, ensure_ascii=False)), go)

    async def fetch(self, url: str) -> dict:
        url = validate_url(url)
        if hint := fetch_route_hint(url):
            raise ApiError(hint)

        async def go():
            # fetch: server keeps crawling after a 504, so wait ~8s before retrying
            data = await self._billed_call("fetch", {"url": url}, 8.0, lambda d: bool(as_text(d.get("content"))))
            return {"title": as_text(data.get("title")), "content": unescape_markdown(as_text(data.get("content")))}

        return await self._dedup(("fetch", url), go)

    async def aclose(self):
        await self._http.aclose()
