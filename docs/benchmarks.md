# 评测：DeepSeek Search vs Claude Code 内置 WebSearch

日期：2026-10-19。DeepSeek Search API 开放后第 2 天。

## 方法与局限

- 每个查询两边各跑一次，取前 10 条结果，按 5 项各 0–2 分打分：相关性、原始/权威来源占比、时效性、垃圾内容少（营销号、内容农场、SEO 站越少分越高）、是否命中最有用的那条。满分 10。
- **打分由 AI 评审（Claude）完成，每题只跑一次、单一评审**，结论只代表倾向，不是严格的统计结论。欢迎用自己的查询复现。
- 两边都只看搜索结果本身，没有经过模型二次整理。

## 中文检索（12 题）

| # | 类别 | 查询 | DeepSeek | WebSearch |
|---|---|---|---|---|
| 1 | 找开源项目 | Claude Code 自动整理参考文献 skill | **9** | 5 |
| 2 | 找开源项目 | 公众号排版 开源工具 | **9** | 8 |
| 3 | 找开源项目 | 医院科室 门诊 量表 扫码 开源 | 3 | **4** |
| 4 | 中文经验 | 国自然 面上项目 申请书 经验 2026 | 6 | **9** |
| 5 | 中文经验 | Mac mini 外置 SSD 做开发 经验 | **9** | 3 |
| 6 | 医学 | 儿童 分泌性中耳炎 指南 2025 中华 | 6 | **7** |
| 7 | 医学 | 突发性聋 激素 鼓室内注射 专家共识 | **9** | 5 |
| 8 | 时效 | DeepSeek V4.1 Flash 1017 评测 | **10** | 3 |
| 9 | 时效 | 2026年10月 AI 新模型 发布 | **7** | 1 |
| 10 | 易被污染 | 降血压 偏方 | **7** | 5 |
| 11 | 易被污染 | AI 副业 月入过万 教程 | **5** | 2 |
| 12 | 医学经验 | 儿童 腺样体肥大 手术还是保守 家长经验 | 8 | 8 |
| | | **合计** | **88** | **60** |

观察：

- DeepSeek 在时效（10-17 发布的内容前 10 条全中）、中文社区一手经验（linux.do、B 站、个人博客）、近两年医学共识、GitHub/PyPI 项目上明显更好。
- WebSearch 在政策制度类（如国自然改革文件）更好，摘要还会提示“商业内容”“无法核实”。
- DeepSeek 的弱点：会把文库站的“指南”排进前 10（本项目默认过滤）；`rerank_score` 和 `authority` 与实际质量不一致，本项目只展示、不据此过滤；不加 `lang:zh` 时会混入外文结果。

## 英文 / 技术检索（15 题）

| # | 查询 | DeepSeek | WebSearch |
|---|---|---|---|
| 1 | MCP server web search multiple backends | **7** | 1 |
| 2 | open source tool convert docx references to zotero | 8 | 8 |
| 3 | claude code skill PDF figure extraction | **6** | 1 |
| 4 | site:github.com MCP server web search multiple backends | **10** | 1 |
| 5 | uv pip install error externally-managed-environment macOS | **7** | 6 |
| 6 | FastMCP python stdio server tutorial | **9** | 7 |
| 7 | otitis media with effusion children guideline 2025 | 3 | **5** |
| 8 | SSNHL intratympanic steroid meta-analysis | **9** | 7 |
| 9 | Claude Opus 5.5 release | **8** | 5 |
| 10 | October 2026 AI model release | **7** | 4 |
| 11 | LLM agent benchmark after:2026-09-01 | **9** | 4 |
| 12 | "intratympanic dexamethasone" "salvage therapy" | 6 | **8** |
| 13 | FastMCP tutorial -site:github.com -site:medium.com | **8** | 7 |
| 14 | site:arxiv.org RAG evaluation benchmark | 6 | **8** |
| 15 | pdf figure extraction site:github.com | **9** | 6 |
| | **平均** | **7.5** | **5.2** |

注：WebSearch 写在查询串里的 `site:` 不生效（第 4 题），改用它自带的 `allowed_domains=["github.com"]` 参数后可达 10 分。

## 搜索语法实测（DeepSeek）

| 语法 | 结果 |
|---|---|
| `site:` / `-site:` | 生效 |
| `before:` | 生效 |
| `after:` | 基本生效（按页面日期，旧文转载页可能通过） |
| `"精确短语"` | 弱约束 |
| `lang:` | 文档支持，中文检索建议加 `lang:zh` |

## Fetch 抓取实测

- 能读：微信公众号文章、PubMed、中华医学会期刊站（这三类 Claude Code 内置抓取被验证页或登录跳转挡住）、GitHub 页面、官方文档站。
- 读不了：知乎、小红书、X、Wikipedia、raw.githubusercontent.com、PDF（约 0.3 秒返回 404，整站屏蔽）；NEJM、Lancet 固定 30 秒超时。本项目对这些网址直接提示改道，不发请求。
- 正文不含超链接和图片；登录页、验证页会按“成功”返回（本项目会给出警告）。
- 原始返回把所有 ASCII 标点加了反斜杠转义，本项目已还原（在约 39 万字符样本上逐字核对一致）。

## 延迟

Search 不带正文时中位数约 0.6–1.4 秒；带正文、50 条时响应约 170KB，延迟没有明显增加。Fetch 视站点 0.4–35 秒。
