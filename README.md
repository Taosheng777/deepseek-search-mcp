# deepseek-search-mcp

[![CI](https://github.com/Taosheng777/deepseek-search-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/Taosheng777/deepseek-search-mcp/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.12%2B-blue)
![License](https://img.shields.io/badge/license-MIT-green)

把 DeepSeek 2026-10-17 开放的**自研搜索引擎**（Search / Fetch API）接进 Claude Code、Codex、Cursor 等任何支持 MCP 的 AI 工具。

[English](#english) · [评测结果](docs/benchmarks.md)

## 为什么用它

我们在 2026-10-19 拿 27 个真实查询，对比了它和 Claude Code 内置的 WebSearch（方法和局限见 [docs/benchmarks.md](docs/benchmarks.md)）：

| 场景 | DeepSeek | 内置 WebSearch |
|---|---|---|
| 中文检索（12 题，满分 10/题） | **88 分** | 60 分 |
| 英文 / 技术 / GitHub 找轮子（15 题平均） | **7.5** | 5.2 |
| 近期动态（10 月新发布） | 能命中官方原文 | 索引停在 9 月 |
| 政策制度类、英文临床指南、经典论文 | 偏弱 | **更好** |

结论：中文、开源项目、近期动态用它更好；政策和英文经典文献建议与内置搜索交叉使用。

## 和同类封装的区别

| | 本项目 |
|---|---|
| 防误扣费 | 每日 / 每月调用上限（默认 500 / 5000 次），多个会话共享计数；超时等“可能已计费”的请求保守计入 |
| 防提示注入 | 网页内容放在每次随机生成的围栏里并标为不可信；网页里伪造的围栏、结果条目会被中和 |
| 输出可控 | 单次输出硬上限 2 万字符，不会撑爆模型上下文；同一网页的分块结果合并去重 |
| 结果清洗 | 默认过滤 book118 / 人人文库 / 豆丁 / 道客巴巴 / 百度文库等转卖文档站；修复 Fetch 正文的反斜杠转义 |
| 稳定性 | 只对超时、网络错误、408/429/5xx 重试；遵守 Retry-After；总时限是硬上限；格式异常的响应不会让工具崩溃 |
| 已知限制提前告知 | 知乎 / 小红书 / X / Wikipedia / PDF 等 Fetch 抓不了的站点，直接提示改用别的工具，不白花钱 |
| 质量 | 97 个自动测试，CI 覆盖 Linux / macOS / Windows；两轮独立代码审查 |

## 安装

需要 [uv](https://docs.astral.sh/uv/) 和一个 [DeepSeek API key](https://platform.deepseek.com/)。如果你的环境只有 `uv` 没有 `uvx`，把下面的 `uvx` 换成 `uv tool run`。

**Claude Code**

```bash
claude mcp add -s user deepseek-search -e DEEPSEEK_API_KEY=sk-你的key -- uvx --from git+https://github.com/Taosheng777/deepseek-search-mcp deepseek-search-mcp
```

**Codex**（`~/.codex/config.toml`）

```toml
[mcp_servers.deepseek-search]
command = "uvx"
args = ["--from", "git+https://github.com/Taosheng777/deepseek-search-mcp", "deepseek-search-mcp"]
env = { DEEPSEEK_API_KEY = "sk-你的key" }
```

**Cursor / Claude Desktop / 其他 MCP 客户端**

```json
{
  "mcpServers": {
    "deepseek-search": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/Taosheng777/deepseek-search-mcp", "deepseek-search-mcp"],
      "env": { "DEEPSEEK_API_KEY": "sk-你的key" }
    }
  }
}
```

**macOS 更安全的做法**：不把 key 写进配置文件，而是存进钥匙串，配置里去掉 `DEEPSEEK_API_KEY` 即可：

```bash
security add-generic-password -U -a "$USER" -s deepseek-api-key -w sk-你的key
```

## 工具

| 工具 | 作用 | 费用 |
|---|---|---|
| `deepseek_search` | 搜索网页。支持 `site:`、`-site:`、`lang:zh`、`after:`、`before:`、`"短语"`、`-关键词` | 每次 0.005 元，与条数无关 |
| `deepseek_fetch` | 抓取单个网页为 Markdown，长页面分页读取（15 分钟内翻页不重复计费） | 每次 0.005 元 |
| `deepseek_usage` | 查看本机调用次数和估算花费 | 免费 |

实用写法：中文检索加 `lang:zh`；找开源项目加 `site:github.com`；`include_content=true` 一次拿到正文，比逐条抓取省钱。

## 配置

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `DEEPSEEK_API_KEY` | — | 未设置时在 macOS 上读取钥匙串服务 `deepseek-api-key` |
| `DEEPSEEK_DAILY_LIMIT` | 500 | 每日计费调用上限（约 2.5 元） |
| `DEEPSEEK_MONTHLY_LIMIT` | 5000 | 每月计费调用上限（约 25 元） |
| `DEEPSEEK_SEARCH_STATE_DIR` | `~/.local/state/deepseek-search-mcp` | 用量计数文件位置 |

## 注意

- 查询词和网址会发送给 DeepSeek；DeepSeek 文档未说明数据留存政策。
- 不建议把 `deepseek_fetch` 加入客户端的“免确认”列表：网页内容可能诱导模型去抓取带敏感参数的网址。
- 搜索结果只是线索。医学、法律等专业结论请回原始来源核对。
- 本项目与 DeepSeek 官方无关。

## 开发

```bash
uv sync
uv run pytest -q
```

---

## English

An MCP server for DeepSeek's **self-built web search engine** (Search / Fetch API, opened 2026-10-17). Works with Claude Code, Codex, Cursor and any MCP client.

**Why:** in our 27-query comparison against Claude Code's built-in WebSearch (2026-10-19, see [benchmarks](docs/benchmarks.md)), DeepSeek scored 88 vs 60 on Chinese queries and 7.5 vs 5.2 (per-query average) on English / technical / GitHub queries. It is weaker on policy documents, English clinical guidelines and classic papers — cross-check those with your client's built-in search.

**What's different:** spend caps shared across sessions (conservative accounting for timed-out calls), per-call random fences against prompt injection from web content, a hard 20k-character output cap, de-duplication and doc-farm filtering, retries only where they are safe, upfront hints for sites DeepSeek Fetch cannot read, 97 tests with CI on Linux / macOS / Windows.

**Install (Claude Code):**

```bash
claude mcp add -s user deepseek-search -e DEEPSEEK_API_KEY=sk-... -- uvx --from git+https://github.com/Taosheng777/deepseek-search-mcp deepseek-search-mcp
```

Tools: `deepseek_search`, `deepseek_fetch`, `deepseek_usage`. Pricing: CNY 0.005 per successful call. Tool messages are in Chinese; models read them fine. Not affiliated with DeepSeek.

License: MIT
