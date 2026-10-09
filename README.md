# Hippo

管理微信公众号文章的自托管工具：把订阅的公众号文章同步到自己的 PostgreSQL，提供阅读器、全文检索、
高亮标注、每日邮件摘要、RSS 与「对单篇文章提问」的 AI 对话。后端 Python + FastAPI，前端 React + Vite。

## 架构

```
        ┌──────────────────────────┐
        │  weixin-rs daemon        │  微信登录态与协议（列表 / 正文 / 搜索）
        │  /opt/weixin-rs          │  运行在独立进程，持有唯一的微信会话
        └───────────┬──────────────┘
                    │ RPC (msgpack)
        ┌───────────▼──────────────┐        ┌────────────────────┐
        │  hippo sync-worker       │───────▶│  PostgreSQL        │
        │  列表入队 → 正文落库      │        │  + pg_jieba 全文   │
        └──────────────────────────┘        │  + S3 兼容对象存储 │
        ┌──────────────────────────┐        └─────────▲──────────┘
        │  hippo serve (API + SPA) │──────────────────┘
        └──────────────────────────┘
```

两个进程：`serve` 只提供 API/UI，`sync-worker` 独占同步任务。微信凭据只存在 daemon 里，hippo 不保存。

## 快速开始

依赖：Python ≥ 3.14、PostgreSQL（需 `pg_jieba` 与 `pg_trgm` 扩展）、[uv](https://docs.astral.sh/uv/)、
Node.js 24（仅构建前端）、S3 兼容对象存储（可选，未配置时图片按需回源）、weixin-rs daemon。

```bash
uv sync --extra weixin            # --extra weixin 提供 daemon SDK 需要的 msgpack
cp .env.example .env              # 至少设置 HIPPO_PG_DSN；有 daemon 时设置 WEIXIN_SDK_PATH

uv run python -m hippo db init    # 建表（幂等），校验 schema 版本
uv run python -m hippo user add admin --password '…' --admin   # 第一个管理员

cd frontend && npm ci && npm run build && cd ..   # 构建 SPA（serve 默认读 frontend/dist）

uv run python -m hippo serve --host 127.0.0.1 --port 8000
uv run python -m hippo sync-worker                 # 另开一个终端
```

打开页面 → 用刚建的管理员登录 → **设置 → 微信登录** 扫码（`POST /api/login/qr` + `/api/login/wait`）
→ **公众号** 页搜索并添加要同步的号。添加后会投递一条历史回填任务，worker 自动从新到旧翻完。

验证：`GET /api/settings/status` 里 `status` 在同步时变为 `running`；`GET /api/article?page=1` 能返回文章。

## 数据源：weixin-rs daemon

hippo 只通过 RPC 使用 daemon，不接触微信协议细节：

| 能力 | daemon 接口 |
|---|---|
| 文章列表 | `get_biz_articles`（给 alias 或 `gh_…`，返回长链、游标、解析出的 `gh_`） |
| 正文 | `get_article_bodies`（短链优先，daemon 侧限流与分批） |
| 搜索公众号 | `search_biz`（H5 搜索，返回 `gh_…` 与头像） |
| 登录 | 扫码 / `auto_auth_key` 免扫重登 |

配置（默认值见 `hippo/weixin_source.py`）：

| 变量 | 默认 | 说明 |
|---|---|---|
| `WEIXIN_SDK_PATH` | `/opt/weixin-rs/sdk/python` | daemon 的 Python SDK 位置，hippo 从这里 `import weixin_bot` |
| `WEIXIN_DAEMON_HOST` | `127.0.0.1` | daemon 监听地址 |
| `WEIXIN_DAEMON_PORT` | `9099` | daemon 监听端口 |

daemon 掉线或会话失效时，同步会整体暂停（不把这种错记成文章失败），页面顶部会提示需要在设置页重新扫码。

## 功能

- **多用户**：注册 + 邮箱验证 + 找回密码（可关闭注册）、管理员后台、审计日志；分组与订阅按用户隔离。
- **公众号**：搜索添加（微信 H5 搜索）、`gh_id` ↔ fakeid 双向映射、头像缓存、批量分组与同步间隔、停用。
- **同步**：列表阶段只入队（长链会失效），正文阶段拿到永久短链才落库；原始 HTML 存 `article_document`，
  派生内容（markdown + JSON blocks）存 `article_content`；图片按独立队列回填到对象存储；
  支持历史回填、失败重试与逐条失败诊断（`content_fetch_diagnostic`）。
- **阅读器**：`pg_jieba` 中文分词全文检索、相关度排序、按版式（图文/视频/图片消息…）分面筛选、
  高亮标注、按内容哈希全局屏蔽图片。
- **日报**：按用户时区生成当天摘要并邮件发送；投递台账保证同一天同一渠道只发一次（`POST /api/report/{date}/send` 幂等）。
- **AI 对话**：对单篇文章提问或走预设（摘要 / 要点）；模型供应商在管理台配置，密钥只以末四位回显。
- **RSS**：`GET /api/feed/mixed?format=rss`（用 `token=` 代替 Cookie），或用 `hippo rss` 直接产出 XML。
- **可观测**：structlog 结构化日志、可选 OTLP 上报 SignOz、`GET /api/admin/log/tail` 兜底看日志文件。

## CLI

```bash
uv run python -m hippo --help        # 全部命令
```

| 命令 | 用途 |
|---|---|
| `serve` | 启动 API + SPA（`--host/--port`、`--static-dir`、`--unix-socket`、`--inprocess-sync`） |
| `sync-worker` | 独立同步进程（`--poll-interval`，默认 5s 轮询任务队列） |
| `db init` | 建表/升级 schema；`db migrate-multiuser --owner <用户名>` 接管历史数据；`db rebuild-counts`、`db backfill-*` 维护 |
| `user add <用户名> --password … [--admin] [--email …]` | 创建账号（CLI 建的账号直接视为已验证） |
| `account list / sync / sync-all / sync-config / enable / disable / remove` | 账号与订阅维护 |
| `account group add / list / sync / set / clear` | 分组维护 |
| `article list / sync / sync-all / download <url> / backfill-images` | 文章抓取与图片回填 |
| `rss --group 分组 --limit 50` | 输出 RSS XML 到标准输出 |
| `export-accounts` | 导出账号（含敏感字段）为 JSON |

## HTTP API

全部接口见 **[API.md](API.md)**；服务本身也暴露 `GET /docs`（Swagger UI）与 `GET /openapi.json`。

## 数据库

- schema 定义在 `schema/postgres.sql`，`hippo db init` 幂等执行；`hippo/storage.py` 里的 `SCHEMA_VERSION`
  与库中记录不一致时服务拒绝启动（迁移总是先于重启）。
- 主要表：`accounts`/`subscription`/`account_groups`（目录 + 每用户订阅）、`articles`/`article_content`/
  `article_document`/`article_images`（文章与派生内容）、`article_queue`（列表→正文的队列）、
  `sync_jobs`（同步任务）、`avatar_images`（头像缓存）、`blocked_image_hashes`（图片屏蔽）、
  `users`/`user_session`/`user_token`（账号与会话）、`annotation`/`article_read`（阅读数据）、
  `chat_session`/`chat_message`、`llm_provider`、`report_setting`/`report_delivery`、`audit_log`、`meta`（键值）。
- 文章图片存在 S3 兼容对象存储，`article_images` 只存元数据与 `s3_key`；下载失败的记录可由
  `hippo article backfill-images` 重试。

## 配置

`hippo/config.py` 启动时 `load_dotenv()`，因此**必须在仓库目录下启动**，或自行注入环境变量。

| 变量 | 默认 | 说明 |
|---|---|---|
| `HIPPO_PG_DSN` | — | **必填**，PostgreSQL 连接串 |
| `HIPPO_PG_POOL_MIN` / `HIPPO_PG_POOL_MAX` | `1` / `8` | 连接池大小 |
| `HIPPO_PG_JIEBA_WARMUP` | `1` | 预热 jieba 分词表 |
| `HIPPO_PG_DISABLE_JIT` | — | 关闭 PG 的 JIT（大查询更稳） |
| `HIPPO_S3_ENDPOINT` 等 `HIPPO_S3_*` | — | 对象存储（endpoint/bucket/access_key/secret_key/region/prefix，按 path-style 寻址） |
| `WEIXIN_SDK_PATH` / `WEIXIN_DAEMON_HOST` / `WEIXIN_DAEMON_PORT` | 见上表 | daemon 连接 |
| `HIPPO_HTTP_PROXY` | — | 抓取文章页/头像时使用的代理 |
| `HIPPO_ARTICLE_WORKER` / `HIPPO_ARTICLE_WORKER_PROXY` / `HIPPO_ARTICLE_MAX_CONNECTIONS` | — | 用 Cloudflare Worker 中转抓正文 |
| `HIPPO_COOKIE_SECURE` | `0` | 置 `1` 时给会话 Cookie 加 `Secure`（HTTPS 部署请打开） |
| `HIPPO_SESSION_TTL_HOURS` | `168` | 会话有效期（小时） |
| `HIPPO_ENABLE_INPROCESS_SYNC` | — | 在 Web 进程里跑同步（仅开发用） |
| `HIPPO_LOG_LEVEL` / `HIPPO_LOG_FORMAT` / `HIPPO_LOG_FILE` / `HIPPO_LOG_TAIL_LINES` | `WARNING` / `json` / — / `500` | 日志 |
| `OTEL_EXPORTER_OTLP_ENDPOINT` / `HIPPO_OTEL_ENABLED` / `HIPPO_OTEL_SAMPLE_RATIO` / `HIPPO_ENV` | — | 遥测 |
| `HIPPO_SIGNOZ_URL` | — | 管理台「看日志」深链的目标 |

## 开发

```bash
uv run pytest                # 146 个用例，约 1 秒；测试不连数据库
uv run ruff check hippo tests scripts
uv run ruff format --check hippo scripts

cd frontend
npx vitest run               # 组件与纯函数测试
npm run build                # 产出 frontend/dist
npx eslint src --max-warnings 0
```

测试工具在 uv 的 `dev` 依赖组里，`uv run pytest` 开箱可用；`docs/` 已不再存在，
API 参考在根目录的 [API.md](API.md)。

## 目录结构

```
hippo/
├── cli.py               # Typer CLI 入口
├── server.py            # FastAPI 应用与静态资源
├── api/                 # 路由（routers/）、依赖、错误处理、限流
├── storage.py           # 连接池、schema 版本、通用查询
├── repositories/        # 数据访问层（account/article/image/chat/audit/…）
├── article_queries.py   # 文章查询、全文检索、图片代理
├── downloader.py        # 正文抓取与图片下载
├── weixin_source.py     # weixin-rs daemon 的 RPC 封装
├── weixin_worker.py     # 列表入队 + 正文落库
├── weixin_watch.py      # daemon 登录态守护
├── sync_worker.py       # 任务领取与执行
├── sync_service.py      # 同步流程编排
├── sync_jobs.py / sync_tasks.py / sync_types.py / sync_core.py / sync_settings.py
├── sync_scheduler.py    # 定时同步
├── image_backfill.py    # 图片回填循环
├── image_store.py / image_hashes.py / s3.py / file_storage.py
├── avatar.py            # 公众号头像缓存
├── wechat_parser.py / normalize_html.py   # 微信页面解析与清洗
├── report/              # 日报：query / render / delivery
├── report_scheduler.py
├── llm/                 # 对话客户端与提示词
├── emailer.py           # SMTP
├── security.py          # 口令哈希
├── site_settings.py     # 站点级设置
└── observability/       # 日志与 OTLP

schema/postgres.sql      # 建表脚本（含 pg_jieba 扩展与全文索引）
frontend/                # React + Vite SPA
tests/                   # pytest 用例
scripts/                 # 一次性维护脚本（gh_id 解析、头像回填等）
API.md                   # HTTP API 参考
hippo.service / hippo-sync-worker.service   # systemd 单元示例
```

## 部署

自托管按「rsync 源码 + systemd」即可：构建前端后把仓库同步到目标机，
`uv sync --frozen --no-dev --extra weixin` 对齐依赖，跑 `db init`，再用附带的两个 unit 分别跑
`serve` 与 `sync-worker`。注意顺序：先迁移（`db init`）、后重启服务，schema 版本不匹配时服务会拒绝启动。

`Dockerfile` 只打包 API/UI：容器里没有 weixin-rs daemon 与其 SDK，因此**不能同步**，仅适合看界面或调 API。
