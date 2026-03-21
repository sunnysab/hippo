# 特殊类型文章内容回填设计

**日期：** 2026-03-21

**范围**

- 为历史特殊类型文章回填缺失的内容结构。
- 仅处理 `item_show_type IN (5, 7, 8, 10)` 且 `article_content.content_json = []` 的记录。
- 通过重新抓取文章页并复用现有解析链路，覆盖旧的 `clean_html`、`content_markdown`、`content_json`。

**背景**

库中存在一批历史文章，数据库里已经有 `article_content` 行，但 `content_json` 为 `[]`。当前代码库中的 `hippo db backfill-content-json` 只会根据已有 `content_markdown` 重建 `content_json`，不会重新抓取文章页，因此无法修复这批“当时已抓到页面，但特殊类型页面未被正确解析”的历史数据。

已确认当前生产数据里：

- `articles.item_show_type IS NULL` 的记录数为 `0`。
- 目标集合总数为 `3977`。
- 类型分布为：`5=963`、`7=46`、`8=2909`、`10=59`。

**目标**

- 仅修复特殊类型历史文章的空 `content_json`。
- 不复用旧的 `clean_html` 和 `content_markdown`，避免把历史错误结果继续传播。
- 复用当前主链路的抓取、解析和落库逻辑，保证回填后的数据结构与新下载文章一致。
- 提供可中断、可续跑的一次性脚本，适合真实库中的批量修复。

**非目标**

- 不将该能力接入正式 `hippo` CLI 命令树。
- 不处理普通图文 `item_show_type = 0`。
- 不处理 `content_json` 已有有效内容的记录。
- 不引入基于旧 `clean_html` 或旧 `content_markdown` 的兜底回填。
- 不顺带修复与本次空 `content_json` 无关的历史数据问题。

**方案选择**

本次采用“重新抓取文章页，再走现有解析链路重写内容”的方案。

放弃其它方案的原因：

- 基于旧 `content_markdown` 重建 `content_json` 无法解决根因，因为历史错误结果本身就不可信。
- 针对特殊类型直接从 `raw_json` 组装 `content_json` 会复制一套一次性解析逻辑，和现有 parser 分叉，风险高且不可复用。

**脚本形态**

- 新增一次性脚本：`scripts/backfill_special_article_content.py`
- 该脚本独立执行，不注册为正式 CLI 子命令。
- 默认从环境变量 `HIPPO_PG_DSN` 读取数据库连接，也允许显式传参覆盖。

**候选筛选**

脚本只处理满足以下条件的文章：

- `articles.item_show_type IN (5, 7, 8, 10)`
- `article_content.content_json = []`
- 按 `articles.id ASC` 递增扫描

候选筛选不使用旧 `clean_html`、旧 `content_markdown`、旧 `content_json` 参与重建判断。它们只作为“待覆盖的旧结果”存在。

**抓取与解析流程**

对每一条候选记录，脚本执行以下流程：

1. 读取数据库中的文章元信息，至少包括 `id`、`biz`、`article_id`、`title`、`link`、`item_show_type`、`raw_json`。
2. 使用文章 `link` 重新抓取文章页 HTML。
3. 复用当前主链路的解析器 `parse_wechat_article` 生成新的 `clean_html`、`markdown`、`item_show_type`。
4. 使用 `_parse_markdown_blocks` 从新的 markdown 构建内容块。
5. 绑定图片元信息并生成最终 `content_json`。
6. 调用现有落库逻辑覆盖 `article_content` 与相关图片记录。

这次回填明确不接受以下行为：

- 不从旧 `clean_html` 继续推导新结果。
- 不从旧 `content_markdown` 继续推导新结果。
- 不在抓取失败或解析失败时，把旧结果混入新结果作为 fallback。

**复用边界**

应复用的现有能力：

- 登录态与文章页抓取能力
- Worker 前缀与代理配置
- `parse_wechat_article`
- `_parse_markdown_blocks`
- 图片 metadata 绑定逻辑
- `save_article_content`

不应复用的内容：

- 历史 `article_content.clean_html`
- 历史 `article_content.content_markdown`
- 历史 `article_content.content_json`

**回写策略**

- 只有在“重新抓取成功且重新解析成功”时，才覆盖数据库中的旧内容。
- 覆盖字段包括：
  - `article_content.clean_html`
  - `article_content.content_markdown`
  - `article_content.content_json`
- 同时沿用现有保存逻辑，保持图片记录、封面记录和 `articles.item_show_type` 与新解析结果一致。
- 每篇文章使用单独事务提交，避免单条失败回滚整批结果。

**失败处理与续跑**

- 抓取失败：记录文章 ID、链接和错误原因，跳过该条。
- 解析失败：记录文章 ID、类型和错误原因，跳过该条。
- 落库失败：回滚当前文章事务，记录错误并继续后续文章。
- 成功写回后的记录会自动脱离候选集合，因此脚本天然幂等，可直接重跑。

续跑机制：

- 使用 `articles.id` 递增扫描。
- 支持 `--start-id`，允许从上次处理到的位置继续。
- 也允许直接重跑，由候选条件自动跳过已完成记录。

**脚本参数**

- `--pg-dsn`：可选，默认使用 `HIPPO_PG_DSN`
- `--start-id`：仅处理 `articles.id > start_id` 的记录
- `--limit`：本次最多处理的候选条数
- `--batch-size`：每批读取候选数
- `--dry-run`：只打印候选规模与样本，不写数据库
- `--types`：可选，默认 `5,7,8,10`
- `--worker-prefix`：覆盖文章 HTML worker 前缀或模板
- `--worker-proxy`：覆盖访问 worker 的代理
- `--workers`：抓取并发数

**数据一致性要求**

- 回填后的特殊类型文章必须来自当前 parser 的输出，而不是历史缓存内容。
- 成功处理后，`content_json` 不再为 `[]`。
- 回填脚本不得修改不在候选集合内的文章。

**验证**

实现完成后，至少要执行以下验证：

1. `--dry-run` 模式确认候选总数与样本类型符合预期。
2. 以小批量方式试跑，例如 `--limit 20`。
3. 抽查至少一篇 `item_show_type = 8` 和一篇 `item_show_type = 5` 的文章，确认 `content_json` 中存在符合当前 parser 结果的内容块。
4. 复查 SQL，确认目标集合中剩余 `content_json = []` 的数量下降。
5. 若存在失败项，输出可用于续跑或单独排查的文章 ID 与失败原因。

**测试范围**

- 为脚本添加最小必要的单元测试，覆盖：
  - 候选筛选条件
  - `dry-run` 行为
  - 成功回写时仅覆盖目标文章
  - 抓取或解析失败时跳过且不中断全局流程
- 特殊类型 HTML 解析正确性继续依赖现有 parser 测试，不为一次性脚本重复实现同类测试。

**实施假设**

- 当前运行环境具备可用的登录态或等效抓取能力，可以访问这些历史文章页。
- 当前 parser 已经能够正确处理目标特殊类型 `5`、`7`、`8`、`10`。
- 目标库允许以单篇事务方式执行批量回填，不要求整批原子提交。
