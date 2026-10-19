import asyncio

from deepseek_search_mcp import server
from deepseek_search_mcp.client import ApiError


class FakeClient:
    def __init__(self, results=None, page=None, error=None):
        self.results, self.page, self.error = results or [], page, error
        self.calls = []

    async def search(self, query, count, need_content):
        self.calls.append(count)
        if self.error:
            raise self.error
        return self.results

    async def fetch(self, url):
        if self.error:
            raise self.error
        return self.page


def use(monkeypatch, fake):
    async def get():
        return fake

    monkeypatch.setattr(server, "get_client", get)


def test_search_overfetches_then_trims(monkeypatch):
    fake = FakeClient(results=[{"url": f"https://max.book118.com/{i}"} for i in range(3)]
                      + [{"url": f"https://a.com/{i}", "title": f"T{i}"} for i in range(10)])
    use(monkeypatch, fake)
    out = asyncio.run(server.deepseek_search("q", count=5))
    assert fake.calls == [10]
    assert "[5] T4" in out and "[6]" not in out and "3 条文库站" in out


def test_search_error_is_returned_as_text(monkeypatch):
    use(monkeypatch, FakeClient(error=ApiError("HTTP 402：余额不足")))
    assert "余额不足" in asyncio.run(server.deepseek_search("q"))


def test_fetch_paginates_and_warns_on_login_wall(monkeypatch):
    use(monkeypatch, FakeClient(page={"title": "T", "content": "a" * 2500}))
    out = asyncio.run(server.deepseek_fetch("https://a.com/", max_chars=1000))
    assert "本页 0-1000" in out and "start_index=1000" in out and "疑似登录" not in out
    last = asyncio.run(server.deepseek_fetch("https://a.com/", start_index=2000, max_chars=1000))
    assert "本页 2000-2500" in last and "未完" not in last
    use(monkeypatch, FakeClient(page={"title": "", "content": "环境异常，完成验证后即可继续访问"}))
    assert "疑似登录" in asyncio.run(server.deepseek_fetch("https://mp.weixin.qq.com/s/x"))
