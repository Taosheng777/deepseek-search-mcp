from deepseek_search_mcp.clean import (
    canonical_url,
    clean_results,
    format_results,
    is_doc_farm,
    unescape_markdown,
)


def r(url, title="t", **kw):
    return {"url": url, "title": title, "summary": kw.pop("summary", "s"), **kw}


def test_canonical_url_strips_chunk_fragment():
    assert canonical_url("https://pypi.org/project/x/#files#1") == "https://pypi.org/project/x/#files"
    assert canonical_url("https://a.com/p#1") == "https://a.com/p"
    assert canonical_url("https://a.com/p#intro") == "https://a.com/p#intro"
    assert canonical_url("https://a.com/p") == "https://a.com/p"


def test_clean_results_dedupes_chunks_of_same_page_keeping_first():
    out, stats = clean_results(
        [r("https://a.com/p#1", "A"), r("https://a.com/p#2", "A2"), r("https://b.com/q", "B")],
        exclude_doc_farms=True,
    )
    assert [x["title"] for x in out] == ["A", "B"]
    assert out[0]["url"] == "https://a.com/p"
    assert stats["duplicates"] == 1


def test_doc_farm_filter():
    assert is_doc_farm("https://max.book118.com/html/2024/x.shtm")
    assert is_doc_farm("https://www.renrendoc.com/paper/1.html")
    assert is_doc_farm("https://wenku.baidu.com/view/abc")
    assert not is_doc_farm("https://www.baidu.com/s?wd=x")
    assert not is_doc_farm("https://notbook118.com.example.org/")
    out, stats = clean_results([r("https://max.book118.com/a"), r("https://c.com/")], exclude_doc_farms=True)
    assert len(out) == 1 and stats["doc_farms"] == 1
    out, _ = clean_results([r("https://max.book118.com/a"), r("https://c.com/")], exclude_doc_farms=False)
    assert len(out) == 2


def test_unescape_markdown_restores_punctuation():
    src = r"\# GitHub \- astral\-sh\/uv\: fast\, written in Rust\." "\n" r"curl \-LsSf https\:\/\/astral\.sh\/uv\/install\.sh \| sh"
    assert unescape_markdown(src) == "# GitHub - astral-sh/uv: fast, written in Rust.\ncurl -LsSf https://astral.sh/uv/install.sh | sh"


def test_unescape_markdown_keeps_real_backslashes():
    assert unescape_markdown(r"path C\\Users\\x") == r"path C\Users\x"
    assert unescape_markdown(r"\n stays") == r"\n stays"


def test_format_results_marks_untrusted_and_truncates():
    text = format_results(
        "q",
        [r("https://a.com", "Title A", summary="x" * 1000, site_name="A站", date="2026-10-01T00:00:00+00:00",
           rerank_score=2.345, authority=3)],
        stats={"duplicates": 0, "doc_farms": 2},
        max_summary_chars=100,
    )
    assert "不可信" in text
    assert "Title A" in text and "https://a.com" in text and "2026-10-01" in text
    assert "x" * 101 not in text
    assert "2 条文库站" in text


def test_format_results_unknown_date_and_empty():
    text = format_results("q", [r("https://a.com", date=None)], stats={"duplicates": 0, "doc_farms": 0})
    assert "日期未知" in text
    assert "没有结果" in format_results("q", [], stats={"duplicates": 0, "doc_farms": 0})


from deepseek_search_mcp.clean import fetch_route_hint, looks_like_blocked_page


def test_fetch_route_hint_for_unsupported_sites():
    assert "专用工具" in fetch_route_hint("https://www.zhihu.com/question/1")
    assert "专用工具" in fetch_route_hint("https://www.xiaohongshu.com/explore/abc")
    assert "专用工具" in fetch_route_hint("https://x.com/a/status/1")
    assert "自带" in fetch_route_hint("https://en.wikipedia.org/wiki/Otitis_media")
    assert "自带" in fetch_route_hint("https://raw.githubusercontent.com/a/b/main/README.md")
    assert "PDF" in fetch_route_hint("https://arxiv.org/pdf/2401.00001.pdf")
    assert "PubMed" in fetch_route_hint("https://www.nejm.org/doi/full/10.1056/x")
    assert fetch_route_hint("https://github.com/astral-sh/uv") is None


def test_looks_like_blocked_page():
    assert looks_like_blocked_page("环境异常\n完成验证后即可继续访问")
    assert looks_like_blocked_page("# Sign in\nPlease log in to continue")
    assert looks_like_blocked_page("该内容已被发布者删除")
    assert not looks_like_blocked_page("正文 " * 2000 + "登录")
