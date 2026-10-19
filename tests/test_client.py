import asyncio
import json

import httpx
import pytest

from deepseek_search_mcp.client import ApiError, Budget, DeepSeekClient, validate_query, validate_url

KEY = "sk-test-secret-123456"


def make_client(handler, tmp_path, **kw):
    transport = httpx.MockTransport(handler)
    budget = Budget(tmp_path / "usage.json", daily_limit=kw.pop("daily_limit", 100), monthly_limit=1000)
    return DeepSeekClient(api_key=KEY, budget=budget, transport=transport, sleep=lambda s: asyncio.sleep(0), **kw)


def ok_search(n=1):
    return httpx.Response(200, json={"results": [{"url": f"https://a.com/{i}", "title": "t", "summary": "s"} for i in range(n)]})


def run(coro):
    return asyncio.run(coro)


def test_validate_query():
    assert validate_query("  hi ") == "hi"
    with pytest.raises(ValueError):
        validate_query("   ")
    with pytest.raises(ValueError):
        validate_query("x" * 129)


@pytest.mark.parametrize("url", [
    "ftp://a.com/x", "http://user:pw@a.com/", "http://localhost/", "http://127.0.0.1/", "http://10.0.0.5/",
    "http://192.168.1.1/", "http://169.254.169.254/latest", "http://[::1]/", "http://2130706433/",
    "http://0x7f000001/", "http://printer.local/", "notaurl",
])
def test_validate_url_rejects(url):
    with pytest.raises(ValueError):
        validate_url(url)


def test_validate_url_accepts_public():
    assert validate_url(" https://github.com/astral-sh/uv ") == "https://github.com/astral-sh/uv"


def test_sends_auth_and_body(tmp_path):
    seen = {}

    def handler(req):
        seen["auth"] = req.headers["authorization"]
        seen["body"] = json.loads(req.content)
        seen["url"] = str(req.url)
        return ok_search()

    c = make_client(handler, tmp_path)
    run(c.search("q", count=5))
    assert seen["auth"] == f"Bearer {KEY}"
    assert seen["url"] == "https://api.deepseek.com/v1/tools/search"
    assert seen["body"]["query"] == "q" and seen["body"]["count"] == 5


def test_retries_429_then_succeeds(tmp_path):
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(429, headers={"retry-after": "1"}, json={"error": {"message": "slow"}}) if len(calls) < 3 else ok_search()

    c = make_client(handler, tmp_path)
    res = run(c.search("q"))
    assert len(calls) == 3 and len(res) == 1


def test_retries_5xx_and_network_errors(tmp_path):
    calls = []

    def handler(req):
        calls.append(1)
        if len(calls) == 1:
            raise httpx.ConnectError("boom")
        if len(calls) == 2:
            return httpx.Response(503)
        return ok_search()

    run(make_client(handler, tmp_path).search("q"))
    assert len(calls) == 3


def test_gives_up_after_max_attempts(tmp_path):
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(500, json={"error": {"message": "x"}})

    with pytest.raises(ApiError) as e:
        run(make_client(handler, tmp_path, max_attempts=3).search("q"))
    assert len(calls) == 3 and e.value.status == 500


@pytest.mark.parametrize("status,hint", [(400, "参数"), (401, "key"), (402, "余额")])
def test_no_retry_on_client_errors(tmp_path, status, hint):
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(status, json={"error": {"message": f"bad {KEY}"}})

    with pytest.raises(ApiError) as e:
        run(make_client(handler, tmp_path).search("q"))
    assert len(calls) == 1
    assert hint in str(e.value)
    assert KEY not in str(e.value)


def test_cache_avoids_second_call(tmp_path):
    calls = []

    def handler(req):
        calls.append(1)
        return ok_search()

    c = make_client(handler, tmp_path)
    run(c.search("q"))
    run(c.search("q"))
    run(c.search("other"))
    assert len(calls) == 2


def test_budget_blocks_after_daily_limit_and_counts_only_billable(tmp_path):
    def handler(req):
        body = json.loads(req.content)
        return httpx.Response(200, json={"results": []}) if body["query"] == "empty" else ok_search()

    c = make_client(handler, tmp_path, daily_limit=2)
    run(c.search("empty"))  # not billable
    run(c.search("a"))
    run(c.search("b"))
    with pytest.raises(ApiError) as e:
        run(c.search("c"))
    assert "上限" in str(e.value)
    assert c.budget.usage()["day_count"] == 2


def test_budget_persists_across_instances(tmp_path):
    b1 = Budget(tmp_path / "u.json", daily_limit=5, monthly_limit=10)
    b1.record()
    b1.record()
    b2 = Budget(tmp_path / "u.json", daily_limit=5, monthly_limit=10)
    assert b2.usage()["day_count"] == 2 and b2.usage()["month_count"] == 2


def test_fetch_unescapes_and_validates(tmp_path):
    def handler(req):
        assert json.loads(req.content)["url"] == "https://a.com/x"
        return httpx.Response(200, json={"title": "T", "content": r"\# Hi\, there\."})

    page = run(make_client(handler, tmp_path).fetch("https://a.com/x"))
    assert page == {"title": "T", "content": "# Hi, there."}
    with pytest.raises(ValueError):
        run(make_client(handler, tmp_path).fetch("http://127.0.0.1/"))


def test_fetch_404_message(tmp_path):
    def handler(req):
        return httpx.Response(404, json={"error": {"message": "no content"}})

    with pytest.raises(ApiError) as e:
        run(make_client(handler, tmp_path).fetch("https://a.com/x"))
    assert "抓不到" in str(e.value)


def test_per_endpoint_timeouts(tmp_path):
    seen = {}

    def handler(req):
        seen[req.url.path] = req.extensions["timeout"]["read"]
        return ok_search() if req.url.path.endswith("search") else httpx.Response(200, json={"title": "", "content": "x"})

    c = make_client(handler, tmp_path)
    run(c.search("q"))
    run(c.fetch("https://a.com/"))
    assert seen["/v1/tools/search"] == 15
    assert seen["/v1/tools/fetch"] == 35


def test_fetch_known_unsupported_site_short_circuits(tmp_path):
    def handler(req):
        raise AssertionError("should not call API")

    with pytest.raises(ApiError) as e:
        run(make_client(handler, tmp_path).fetch("https://www.zhihu.com/question/1"))
    assert "专用工具" in str(e.value)
