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

## 数据来源

四个来源共用一条流水线：**列表 → 建文章行（标题封面立刻可见）→ 转写队列 → 正文**。
来源之间的差异只有两处：怎么列条目、音频从哪来，其余（分片、转写、时间戳、归档、播放）完全相同。

| 来源 | `accounts.source` | `biz` | 列表 | 正文 |
|---|---|---|---|---|
| 微信公众号 | `weixin` | `Mz…==`（fakeid） | weixin-rs daemon | daemon 取正文 → markdown |
| 哔哩哔哩 | `bilibili` | `bilibili:<mid>` | `/x/space/wbi/arc/search`（**需要 SESSDATA**） | CC 字幕优先，否则下载音频转写 |
| YouTube | `youtube` | `youtube:<UC…>` | yt-dlp | 下载音频转写 |
| RSS 播客 | `podcast` | `podcast:<feed 的短哈希>` | feed 本身 | `podcast:transcript` 优先，否则转写 |

订阅任意一个地址（视频、频道、feed 都行）：

```bash
uv run python -m hippo source add https://space.bilibili.com/946974/video
uv run python -m hippo source add https://feeds.megaphone.fm/darknetdiaries
uv run python -m hippo source list          # 只看媒体来源
uv run python -m hippo source cookie        # 自己的 B站 Cookie（管理台也可设实例级的）
```

接口等价于 `GET /api/source/preview` + `POST /api/source/channel`；订阅会立刻投递一轮列表同步。

### 转写

一个 `media_job` 一条任务，由 `sync-worker` 里与正文抓取并行的常驻循环消费：

1. **现成字幕优先**：播客的 `podcast:transcript`、B站的 CC 字幕。时间戳是真的，且不花算力。
2. 否则**下载音频 → 留档（128k mp3，不设体积上限）→ 切片 → 逐片转写 → 校验 → 落 blocks**。
3. 切片长度恒定（默认 300s）：太长的片会被模型截断，太短的尾片会让解码器输出乱码，两者都实测过；
   短尾会并进前一片，输出再用字/秒与重复字符占比校验一次（`hippo/media/validate.py`）。

转写服务是一个 **OpenAI 兼容的 `/v1/audio/transcriptions` 端点**，在管理台「媒体与转写」里配置两个档位：

| 档 | 用途 | 实测 |
|---|---|---|
| `accurate`（默认） | 要精度 | `qwen3-asr-1.7b-q8_0`，与既有生产基线字级一致度 0.992 |
| `fast` | 批量粗转，事后可按需重转 | `sensevoice-small`，快约 15 倍 |

需要的外部组件：`ffmpeg`/`ffprobe`（切片、转码）、`yt-dlp`（仅 YouTube）、以及上面那个 ASR 端点
（本实例跑在 archlinux31 上的 [CrispASR](https://github.com/CrispStrobe/CrispASR) `--server`）。
`deploy.sh` 会在远端检查前两个。

### 多源约定

- **区分来源靠 `accounts.source` 列**，不是路由前缀。`source` 是开放的标识符（不是枚举）。
  `accounts.biz` 是主键，语义是「该来源内的频道标识」（B站是 mid、YouTube 是 channel id、播客是 feed 哈希）。
- **写入端必须显式设置 `source`。** 列上的 `DEFAULT 'weixin'` 只是为了让已有数据继续工作；
  依赖默认值会被静默标成微信。
- **接口不加前缀。** 列表用 `GET /api/article?source=a,b` 过滤，每条文章响应都带 `source`；
  只有来源**特有的能力**才用独立路由（微信的 `POST /api/article/{id}/refetch`）。
- **前端不做来源分支。** 来源的名称与配色在 `frontend/src/utils/source.ts` 的 `SOURCE_META`，
  内容类型在 `CONTENT_KIND_META`（列表徽章读它，微信的 `item_show_type` 只留给分面筛选）。
  未登记的来源会原样显示而不是消失。
- **后端同步适配器是唯一的改造点。** `hippo/sources/` 里 `ADAPTERS` 按 `account.source` 选适配器；
  新增来源 = 加一个 `MediaSource`（`preview` + `list_items`）与一个 `MediaResolver`（把队列里的条目变成音频或字幕）。
- **卡片与阅读器不假设内容是段落流。** 视频条目可能整篇只有一个媒体块，目录在无小标题时整列隐藏。

### 已读状态

已读是**按用户**的，存在 `article_read` 边表里（`(user_id, article_pk)` 主键），不写回共享的 `articles`。
读文章即标记已读（`PUT /api/article/{id}/read`）；重复标记不会再刷新 `read_at`；
列表的 `read=unread` 走同一张表。

### 收藏

收藏和已读一样是按用户的边表 `article_star`：`PUT /api/article/{id}/star` 切换，列表用 `starred=true` 过滤。
收藏过的条目在列表里始终显示星标按钮，方便随时取消。

### 文章计数

`accounts.article_count` / `account_groups.article_count` 是**派生的缓存**，由触发器维护，
供分组页展示；文章列表的 `total` 不走它，而是和分面统计一起实时统计，避免同一个响应里出现两个口径。
缓存漂移（例如批量导入绕过了触发器）可以用 `SELECT hippo_rebuild_article_counts();` 重建。

## 功能

- **多用户**：注册 + 邮箱验证 + 找回密码（可关闭注册）、管理员后台、审计日志；分组与订阅按用户隔离。
- **公众号**：搜索添加（微信 H5 搜索）、`gh_id` ↔ fakeid 双向映射、头像缓存、批量分组与同步间隔、停用。
- **同步**：列表阶段只入队（长链会失效），正文阶段拿到永久短链才落库；原始 HTML 存 `article_document`，
  派生内容（markdown + JSON blocks）存 `article_content`；图片按独立队列回填到对象存储；
  支持历史回填、失败重试与逐条失败诊断（`content_fetch_diagnostic`）。
- **媒体来源**：B站 / YouTube / RSS 播客订阅（粘一个 URL 即可），音频转写、现成字幕优先、音频留档与播放、
  转写稿导出（md/txt/srt）、单篇按需重转。
- **公开 API**：只读 API Key，供脚本与阅读器读取文章、转写稿与留档音频。
- **阅读器**：`pg_jieba` 中文分词全文检索、相关度排序、按版式（图文/视频/图片消息…）分面筛选、
  正文目录（TOC）、排版设置（宽度 / 字号 / 行高 / 字距 / 衬线）、未读箱、已读标记与收藏、
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

配置分两处：

- **`config.toml`**（不入库，每台机器一份）：部署选择，目前只有遥测。先
  `cp config.example.toml config.toml` 再编辑；与 `.env` 一样，`deploy.sh` 不会覆盖也不会删除它。
- **`.env` / 环境变量**：密钥与按机器而异的值。`hippo/config.py` 启动时 `load_dotenv()`，
  因此**必须在仓库目录下启动**，或自行注入环境变量。

### config.toml

| 键 | 默认 | 说明 |
|---|---|---|
| `observability.enabled` | `true` | 置 `false` 可停掉导出而不丢 endpoint |
| `observability.otlp_endpoint` | — | OTLP/gRPC 端点；留空即整个遥测关闭 |
| `observability.insecure` | `false` | 采集器走明文 gRPC（`http://` 端点）时置 `true` |
| `observability.sample_ratio` | `1.0` | `1.0` 导出全部 span，失败的 trace 也在内；调低会在 SDK 里丢掉整条 trace，**错误一并丢**。要降量请在采集器侧做 tail sampling |
| `observability.environment` | `prod` | 写入 `deployment.environment` |

### 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `HIPPO_PG_DSN` | — | **必填**，PostgreSQL 连接串 |
| `HIPPO_PG_POOL_MIN` / `HIPPO_PG_POOL_MAX` | `1` / `8` | 连接池大小 |
| `HIPPO_PG_JIEBA_WARMUP` | `1` | 预热 jieba 分词表 |
| `HIPPO_PG_JIEBA_WARMUP_TEXT` | `hippo` | 预热时喂给分词器的文本 |
| `HIPPO_PG_DISABLE_JIT` | — | 关闭 PG 的 JIT（大查询更稳） |
| `HIPPO_DB_INIT_LOG` | — | 置 `1` 时把数据库初始化过程打进日志 |
| `HIPPO_S3_ENDPOINT` 等 `HIPPO_S3_*` | — | 对象存储（endpoint/bucket/access_key/secret_key/region/prefix，按 path-style 寻址） |
| `WEIXIN_SDK_PATH` / `WEIXIN_DAEMON_HOST` / `WEIXIN_DAEMON_PORT` | 见上表 | daemon 连接 |
| `HIPPO_HTTP_PROXY` | — | 抓取文章页/头像时使用的代理 |
| `HIPPO_ARTICLE_WORKER` / `HIPPO_ARTICLE_WORKER_PROXY` / `HIPPO_ARTICLE_MAX_CONNECTIONS` | — | 用 Cloudflare Worker 中转抓正文 |
| `HIPPO_COOKIE_SECURE` | `0` | 置 `1` 时给会话 Cookie 加 `Secure`（HTTPS 部署请打开） |
| `HIPPO_SESSION_TTL_HOURS` | `168` | 会话有效期（小时） |
| `HIPPO_ENABLE_INPROCESS_SYNC` | — | 在 Web 进程里跑同步（仅开发用） |
| `HIPPO_LOG_LEVEL` / `HIPPO_LOG_FORMAT` / `HIPPO_LOG_FILE` / `HIPPO_LOG_TAIL_LINES` | `WARNING` / `json` / — / `500` | 日志 |
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
