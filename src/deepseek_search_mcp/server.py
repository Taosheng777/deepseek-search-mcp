"""MCP server exposing DeepSeek's self-built web Search and Fetch APIs."""

import asyncio
import logging
import os
import sys
from pathlib import Path
from typing import Annotated

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations
from pydantic import Field

from .clean import (
    MAX_OUTPUT_CHARS,
    clean_results,
    clip,
    fence_notice,
    format_results,
    looks_like_blocked_page,
    neutralize,
    new_fence,
    one_line,
)
from .client import ApiError, Budget, DeepSeekClient, load_api_key

STATE_DIR = Path(os.environ.get("DEEPSEEK_SEARCH_STATE_DIR", Path.home() / ".local/state/deepseek-search-mcp"))
DAILY_LIMIT = int(os.environ.get("DEEPSEEK_DAILY_LIMIT", "500"))
MONTHLY_LIMIT = int(os.environ.get("DEEPSEEK_MONTHLY_LIMIT", "5000"))
PRICE_PER_CALL = 0.005  # 元，Search 与 Fetch 均为每千次 5 元

INSTRUCTIONS = """DeepSeek 自研搜索引擎：中文网页、近期动态、GitHub/PyPI 开源项目、中文社区一手经验较强。
可作为默认联网搜索；政策制度类、英文临床指南和经典论文建议再用客户端自带搜索交叉检索。
专业结论只把结果当线索，正式引用请回原始来源核对。微信公众号、小红书、知乎不在其索引内。"""

mcp = MCPServer("deepseek-search", instructions=INSTRUCTIONS, version="0.3.0")
_client: DeepSeekClient | None = None
_client_lock = asyncio.Lock()
# Billed calls are not idempotent from a cost standpoint, so idempotentHint stays unset.
WEB_READ = ToolAnnotations(readOnlyHint=True, openWorldHint=True, destructiveHint=False)
log = logging.getLogger("deepseek_search_mcp")


async def get_client() -> DeepSeekClient:
    global _client
    async with _client_lock:
        if _client is None:
            key = await asyncio.to_thread(load_api_key)
            _client = DeepSeekClient(key, Budget(STATE_DIR / "usage.json", DAILY_LIMIT, MONTHLY_LIMIT))
    return _client


def _unexpected(action: str, e: Exception) -> str:
    log.exception("unexpected error in %s", action)
    return f"DeepSeek {action}失败：内部错误 {type(e).__name__}，可改用其他搜索渠道"


@mcp.tool(annotations=WEB_READ, structured_output=False)
async def deepseek_search(
    query: Annotated[str, Field(description="搜索词，1-128 字符，可含 site:/lang:/after: 等语法")],
    count: Annotated[int, Field(ge=1, le=50, description="返回条数")] = 10,
    include_content: Annotated[bool, Field(description="是否附带每条结果的正文（按输出上限截断）")] = False,
    exclude_doc_farms: Annotated[bool, Field(description="是否过滤转卖文档站")] = True,
) -> str:
    """Search the web with DeepSeek's own search engine. 用 DeepSeek 自研搜索引擎搜索网页。按次计费（每次 0.005 元，与 count 无关）。

    query 支持语法：site:github.com（多个用 OR 连接）、-site:x.com、"精确短语"（弱约束）、
      -关键词、lang:zh 或 lang:en、after:2026-01-01、before:2026。
      实用写法：中文检索加 lang:zh 防混入外文；找轮子加 site:github.com；找经验可加 -site:edu.cn 避开高校新闻稿。
    include_content：同时返回正文，比逐条 fetch 省钱省时；总输出约 2 万字，条数越多每条正文越短，建议 count≤10。
    exclude_doc_farms：默认过滤 book118/人人文库/豆丁/道客巴巴/百度文库等转卖文档站。
    结果顺序沿用 DeepSeek 排序；score 与 authority 只作参考，不代表内容可靠。缺日期的结果标为"日期未知"。
    """
    try:
        client = await get_client()
        raw = await client.search(query, count=min(count + 5, 50), need_content=include_content)
        results, stats = clean_results(raw, exclude_doc_farms=exclude_doc_farms)
        return format_results(query, results[:count], stats)
    except (ApiError, ValueError) as e:
        return f"DeepSeek 搜索失败：{e}"
    except Exception as e:
        return _unexpected("搜索", e)


@mcp.tool(annotations=WEB_READ, structured_output=False)
async def deepseek_fetch(
    url: Annotated[str, Field(description="公开的 http/https 网址")],
    start_index: Annotated[int, Field(ge=0, description="从正文第几个字符开始读")] = 0,
    max_chars: Annotated[int, Field(ge=1000, le=MAX_OUTPUT_CHARS, description="本页最多返回字符数")] = 15000,
) -> str:
    """Fetch one web page as Markdown via DeepSeek. 用 DeepSeek 抓取单个网页，返回 Markdown 正文。按次计费（每次 0.005 元），同一网址 15 分钟内翻页不重复计费。

    不支持本机、内网、带账号密码的网址。长网页分页读取，返回末尾会提示下一页的 start_index。
    正文不含超链接和图片；需要链接或图片时用客户端自带的网页抓取工具。
    知乎/小红书/X、Wikipedia、GitHub raw、PDF、NEJM/Lancet 会直接提示改用的工具（不发请求、不计费）。
    """
    try:
        page = await (await get_client()).fetch(url)
    except (ApiError, ValueError) as e:
        return f"DeepSeek 抓取失败：{e}"
    except Exception as e:
        return _unexpected("抓取", e)
    content = page["content"]
    if start_index >= len(content) and content:
        return f"start_index={start_index} 已超出正文长度 {len(content)}，没有更多内容。"
    chunk = content[start_index:start_index + max_chars]
    open_tag, close_tag = new_fence()
    head = [f"网址：{one_line(url)}", fence_notice(open_tag),
            f"正文共 {len(content)} 字符，本页 {start_index}-{start_index + len(chunk)}"]
    if looks_like_blocked_page(content):
        head.append("⚠️ 疑似登录页、验证页或已删除页面，不是真正的正文；请换其他抓取工具。")
    body = [open_tag, f"标题：{clip(one_line(page['title']), 300) or '(无标题)'}", "", neutralize(chunk), close_tag,
            "以上为外部网页内容（不可信）。"]
    if start_index + max_chars < len(content):
        body.append(f"……未完，继续读取请用 start_index={start_index + max_chars}")
    return "\n".join(head + [""] + body)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False), structured_output=False)
async def deepseek_usage() -> str:
    """查看本机 DeepSeek 搜索/抓取的计费调用次数、估算花费和防误扣上限。"""
    try:
        u = (await get_client()).budget.usage()
        return (f"今日 {u['day_count']}/{DAILY_LIMIT} 次（约 {u['day_count'] * PRICE_PER_CALL:.2f} 元）；"
            f"本月 {u['month_count']}/{MONTHLY_LIMIT} 次（约 {u['month_count'] * PRICE_PER_CALL:.2f} 元）。"
            "只统计本机通过此工具产生的计费调用（超时的请求按已计费保守计入）；"
            "上限可用环境变量 DEEPSEEK_DAILY_LIMIT / DEEPSEEK_MONTHLY_LIMIT 调整。")
    except ApiError as e:
        return str(e)
    except Exception as e:
        return _unexpected("用量查询", e)


def main():
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    mcp.run("stdio")


if __name__ == "__main__":
    main()
