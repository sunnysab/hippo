# Hippo

自托管的多来源知识平台：把订阅的公众号、B站 / YouTube 频道与 RSS 播客同步到自己的 PostgreSQL，
提供统一阅读器、中文全文检索、高亮标注、媒体转写、每日邮件摘要、私有知识库、RSS，
以及「对单篇文章提问」的 AI 对话。后端 Python + FastAPI，前端 React + Vite。

## 架构

```
        ┌──────────────────────────┐
        │  weixin-rs daemon        │  微信登录态与协议（列表 / 正文 / 搜索）
        │  /opt/weixin-rs          │  运行在独立进程，持有唯一的微信会话
        └───────────┬──────────────┘
                    │ RPC (msgpack)
        ┌───────────▼──────────────┐        ┌────────────────────┐
        │  hippo.sync_worker       │───────▶│  PostgreSQL        │
        │  列表入队 → 正文落库      │        │  + pg_jieba 全文   │
        └──────────────────────────┘        │  + S3 兼容对象存储 │
        ┌──────────────────────────┐        └─────────▲──────────┘
        │  hippo.server (API+SPA)  │──────────────────┘
        └──────────────────────────┘
```

两个常驻进程加一个可选进程：`hippo.server` 只提供 API/UI，`hippo.sync_worker` 独占同步任务，
`hippo.knowledge.worker` 在需要私有知识库时额外启动，负责把已上传的 PDF/TXT 解析入库。
微信凭据只存在 daemon 里，hippo 不保存。知识库与微信登录态完全无关，**不依赖 daemon**，
没有微信账号也能用；不启动知识 worker 时，文档会停在 `queued`。

## 快速开始

依赖：Python ≥ 3.14、PostgreSQL（需 `pg_jieba` 与 `pg_trgm` 扩展）、[uv](https://docs.astral.sh/uv/)、
Node.js 24（仅构建前端）、S3 兼容对象存储（可选，未配置时图片按需回源）、weixin-rs daemon。

```bash
uv sync --extra weixin            # --extra weixin 提供 daemon SDK 需要的 msgpack
cp config.example.toml config.toml   # 至少填 [database] dsn；有 daemon 时填 [weixin] sdk_path

uv run python -m hippo.ops init-db    # 建表（幂等），校验 schema 版本
uv run python -m hippo.ops create-admin --username admin   # 密码不写在命令行，交互隐藏输入

cd frontend && npm ci && npm run build && cd ..   # 构建 SPA（hippo.server 默认读 frontend/dist）

# 生产：只开 Unix socket，由 Caddy 之类的反向代理在前面接（Caddyfile 里 reverse_proxy unix//run/hippo/hippo.sock）
uv run python -m hippo.server --unix-socket /run/hippo/hippo.sock
# 开发：直接开 TCP
uv run python -m hippo.server --host 127.0.0.1 --port 8000
uv run python -m hippo.sync_worker                 # 另开一个终端
uv run python -m hippo.knowledge.worker            # 可选：需要私有知识库时再开（见下节）
```

打开页面 → 用刚建的管理员登录 → **管理 → 来源 → 微信公众号** 扫码（`POST /api/login/qr` + `/api/login/wait`）
→ **账号** 页搜索并添加要同步的号（其他来源在**分组**页粘一个链接即可，见「数据来源」）。
添加公众号时会先确认机器人微信已关注该号，再投递历史回填任务，由 worker 从新到旧翻完。
关注失败会返回错误，可重试添加；公众号后续靠实时推送更新。

**更新方式按来源区分：**

- **公众号。** 实时推送更新，取消周期列表检查。添加时补全历史，已有历史不会重复补全。
- **RSS、YouTube、B站、播客。** 添加后立即检查；后续按「管理 → 来源 → 对应来源」的更新间隔运行，
  默认每天一次。账号可单独设置周期，默认跟随来源。1 天表示距离上次成功检查满 24 小时。
- **手动更新。** 任意登录用户可在「分组」页对自己订阅的账号手动触发（`POST /api/settings/run`），
  管理员可对全站目录触发（`POST /api/settings/admin/run`）；手动触发会跳过日频次间隔，但仍受短冷却限制。

升级到按来源更新时，旧全局开关和分钟间隔不再生效，非微信来源默认开始按天更新。
部署前需核对存量公众号是否已被机器人微信关注；仓库保留 `scripts/wx_follow_accounts.py` 用于补关注。
Hippo 的推送监听会重连，但目前没有持久化事件游标或主动补发请求；daemon 断线补发能力尚未验证，
发现缺文时可手动检查近期文章。

验证：`GET /api/settings/status` 里 `status` 在同步时变为 `running`；`GET /api/article?page=1` 能返回文章。
头像（`GET /api/account/{biz}/avatar`）与文章封面图首次访问可能回源并落库/对象存储，稍后才变快；
封面与内文图片由 worker 的图片回填循环异步补全（`queue.images` 是待办水位）。

## 个人 ASR 纠错

在设置的纠错页面管理个人规则，也可在有 ASR 转写的文章中选词或使用「添加纠错」。规则仅对当前用户生效，可覆盖全部账号或指定未停用的订阅账号；保存后当前文章会刷新。旧规则中已停用或取消订阅的账号会显示为不可用，可在编辑时移除。匹配区分大小写、从左到右按最长原词匹配，同原词的账号专属规则优先于通用规则，替换结果不会再次参与匹配。停用或删除规则即可恢复原文，共享原始正文不被改写。

只有明确标记为 ASR 来源的段落参与纠错；字幕、节目说明、标题、普通正文和时间戳不受影响，没有来源标记的历史内容不会猜测。阅读器可切换查看原文，纯文本复制与 Markdown 复制使用当前显示版本；服务端转写导出 md、txt、srt 默认使用纠错版。后续文章 AI 输入使用当前用户的纠错正文，但已生成的 AI 输出和搜索索引不会重新计算。

验证（在仓库根目录执行）：

```bash
.venv/bin/python -m pytest -q
.venv/bin/python -m ruff check hippo tests scripts
cd frontend && npm run lint && npm test && npm run build
```

## 私有知识库

知识库是**按用户隔离**的私有文档集合。假设是「owner 自己上传、自己阅读」：每个请求都用当前登录用户
解析归属，别人的知识库、文档、块一律 `404`，没有管理员特权可以越过。相应的 13 个路由见 [API.md](API.md)。

### 前置条件

- **对象存储必须配置。** 原始 PDF/TXT 存在 S3 兼容存储的 `knowledge/` 前缀下，`article_images` 那套
  「未配置则回源」的降级**不适用于知识库**：`[s3]` 的 endpoint/bucket/access_key/secret_key 空缺时
  上传直接失败，不会退化成明文落本地盘。图片按需回源的原有说明不受影响。
- **桶/前缀必须私有。** 所有权检查只在 Hippo 自己的 API 里生效，它**约束不了匿名对象访问**：
  如果 bucket 或 `knowledge/*` 前缀允许匿名 `GET`，任何拿到（或猜到）key 的人都能直接读原文。
  必须保证 bucket 私有，或至少让 `knowledge/*` 前缀拒绝匿名读取。只想公开文章图片时，
  只放开 `mp/image/*`，**不要**用 `Principal=*` 的桶策略公读整桶——那会连同 `knowledge/` 一起公开。
  对象 key 里的 UUID 只是命名，不是授权。Hippo **不会**自动修改 bucket policy，也不调用对象存储的
  策略接口；这部分由部署方在存储侧配置。真实环境需把**「匿名 GET `knowledge/*` 必须被拒」**
  作为上线门槛逐项验收（带授权后执行）。
- **解析工具。** PDF 走 Poppler 的 `pdftotext`/`pdfinfo`，并用 `util-linux` 的 `prlimit` 限制资源：

  ```bash
  pdftotext -v && pdfinfo -v && prlimit --version
  ```

  这些命令在**真实隔离环境验证完成并经授权后**再执行；上面只列出检查方式。TXT 不需要额外工具。

### 限额（固定内部值，不是可调选项）

| 项 | 值 |
|---|---|
| 单文件 | 64 MiB |
| PDF 页数 | 1000 页 |
| 文本字符 | 2,000,000 |
| 总块数 | 50,000 |
| 单块字符 | 4,000 |

超过任一上限**直接失败**并记录错误码，不会静默截断、不会返回部分文本。解析层不做 OCR、向量化或
AI 处理：只提取已有的文本层，扫描件会以「需要 OCR」失败。目前只有「解析后的文本阅读」，**没有原
PDF 画布、没有文档标注**，后续能力不在此虚报。

### 状态机

```
pending_upload --PUT file--> uploading --ACK--> queued --worker--> processing --> ready
                                                 |                              |
                                                 +-------- failed <--------------+
```

- `PUT /api/document/{id}/file` 返回 `202` 只表示**已受理**，不等于 `ready`；客户端要读文档就用
  `GET /api/document/{id}` 轮询 `state`，`state=ready` 之后再取 `/content`。
- 同一 app 最多 2 个并发上传，超出 `429`；请求体读取预算 120s，下载预算 120s（同步单次 IO，不是硬期限）。
  上传端点自身没有 300s 上限——某些客户端自己等 300s，那只是客户端行为，不是服务端处理期限。
- `uploading` 阶段崩溃或断连不会自动恢复（还没有 reaper）：留在 `uploading` 的行需要人工处理。
- `uploaded` 不表示 `ready`，`queued` 也不表示已被 worker 领走。

### Worker 的租约

`hippo.knowledge.worker` 领走一条 `queued` 时写入服务端租约：处理期间每 **30s** 心跳续租，租约
**600s** 未被续租即视为过期，启动时以及之后最多每 **60s** 回收一次过期的 `processing`（上限 50 条）。
SQL 里另有维护锁 5s / 语句 10s：这是**服务端事务**的两个上限，不是网络或整个业务链路的硬截止。
回收只覆盖 `processing`，**不碰 `active`，也不回收过期的 `uploading`**——这仍是已知缺口。

### 上传失败后的重试

- 上传失败（`sha256 IS NULL`）重试前先确认该次尝试的对象**已经删除**，再回到 `pending_upload`：
  行里的 journalled key 是「这一次上传」的键，删除它不会碰已发布内容。回到 `pending_upload` 后
  **必须重新发送文件**，重试本身不会重新上传。
- 解析失败保留源对象，直接回到 `queued`，不需要重传。
- 结果不确定时**先 `GET` 核对**，不要盲重试：创建响应丢失（比如网络中断、没有拿到 `id`）时，先
  刷新 `GET /api/knowledge-base/{id}/document` 找 `pending_upload` 的行，不要重复 `POST`。

### 删除与清理

删除文档/知识库只写 **tombstone**：行不再可见，已发布状态不再返回；对象存储里的物理清理是**分开的**
后续动作，这里不声称已自动完成。个人标注、AI 问答与「知识库已完整」的能力都不存在。

## 数据源：weixin-rs daemon

hippo 只通过 RPC 使用 daemon，不接触微信协议细节：

| 能力 | daemon 接口 |
|---|---|
| 文章列表 | `get_biz_articles`（给 alias 或 `gh_…`，返回长链、游标、解析出的 `gh_`） |
| 正文 | `get_article_bodies`（短链优先，daemon 侧限流与分批） |
| 搜索公众号 | `search_biz`（H5 搜索，返回 `gh_…` 与头像） |
| 登录 | 扫码 / `auto_auth_key` 免扫重登 |

配置（默认值见 `hippo/weixin_daemon.py`）：

| 变量 | 默认 | 说明 |
|---|---|---|
| `WEIXIN_SDK_PATH` | `/opt/weixin-rs/sdk/python` | daemon 的 Python SDK 位置，hippo 从这里 `import weixin_bot` |
| `WEIXIN_DAEMON_HOST` | `127.0.0.1` | daemon 监听地址 |
| `WEIXIN_DAEMON_PORT` | `9099` | daemon 监听端口 |

daemon 掉线或会话失效时，同步会整体暂停（不把这种错记成文章失败），页面顶部会提示需要重新扫码（跳转到「管理 → 来源 → 微信公众号」）。

## 数据来源

音视频来源共用列表、文章存储与转写队列；财新与纯文本 RSS 直接存正文，不进入转写队列。

| 来源 | `accounts.source` | `biz` | 列表 | 正文 |
|---|---|---|---|---|
| 财新 | `caixin` | `caixin:<频道 ID>` | Rust `caixin` CLI | 登录态授权正文 → HTML / Markdown |
| 微信公众号 | `weixin` | `Mz…==`（fakeid） | weixin-rs daemon | daemon 取正文 → markdown |
| 哔哩哔哩 | `bilibili` | `bilibili:<mid>` / `bilibili:season:<mid>:<sid>` | UP 投稿列表 / 合集列表（可能需要 **SESSDATA**） | CC 字幕优先，否则下载音频转写 |
| YouTube | `youtube` | `youtube:<UC…>` | yt-dlp | 下载音频转写 |
| RSS 播客 | `podcast` | `podcast:<feed 的短哈希>` | feed 本身 | `podcast:transcript` 优先，否则转写 |

订阅任意一个地址（视频、频道、feed 都行）：在「分组」页点「添加订阅」粘贴地址即可，
这一步与微信来源共用同一个 `POST /api/source/channel`；订阅会立刻投递一轮列表同步。

B站合集支持 `https://space.bilibili.com/<mid>/lists?sid=<sid>` 和
`https://space.bilibili.com/<mid>/lists/<sid>?type=season`。预览显示合集名称，
同步只读取该合集，按最新投稿优先分页；暂不支持 `type=series` 的旧版视频列表。
如果之前已将合集误订阅成 UP 主，需要重新粘贴链接订阅合集；旧的 UP 主订阅不会被自动修改或删除。
验证：`uv run pytest tests/test_source_bilibili.py tests/test_source_api.py`。

Bilibili 的 HTTP、WBI 签名和响应解析集中在 `hippo/sources/bilibili_api.py`；
`bilibili.py` 保留来源与媒体解析适配，列表入库和转写继续共用现有流程。

### 财新

hippo 通过带超时、可取消的异步子进程调用 trade-rs 的 `caixin fetch`；取消或超时会终止并回收子进程。
协议、签名与解析归 Rust crate，来源适配、凭证选择、访问节奏和存储归 hippo。
开发时可在 trade-rs 仓库构建到 hippo 的本地临时目录：

```bash
CARGO_INCREMENTAL=0 CARGO_PROFILE_DEV_DEBUG=0 cargo build -p caixin --target-dir /home/sunnysab/Code/hippo/.tmp/caixin-target
```

在本机 `config.toml` 配置二进制和 crate 的代理文件路径；`binary` 默认从 `PATH` 查找：

```toml
[caixin]
binary = "/home/sunnysab/Code/hippo/.tmp/caixin-target/debug/caixin"
proxy_config = "/home/sunnysab/Code/5-Trade/trade-rs/crates/caixin/proxy.toml"
```

代理文件管理公司代理凭证，来源页不再重复填写财新代理。Rust 客户端固定只让
`gateway.caixin.com` 与 `deepview.caixin.com` 走该代理，其他财新域名直连。
`.tmp/` 只存构建产物和验证记录，不提交。
生产部署需要安装同一版本的 `caixin`；仅同步 hippo 的 Python 文件不足以启用抓取。

在来源管理页配置财新 Cookie（`UID=…; SA_USER_auth=…`），也可在个人设置提交自己的 Cookie。
粘贴财新频道或文章链接即可按所属频道订阅；支持金融、经济、公司、政经、世界，以及财新要闻、观点、周刊。
财新要闻使用站内要闻专题列表；观点和周刊仅扫描当前频道首页可见的文章窗口，不宣称完整历史回填，
首页不提供的发布时间与付费标记不作推断，是否能获取全文以正文接口授权结果为准。

凭证通过子进程标准输入传递，不出现在命令行、日志或 API 响应中。列表先去重并保存元数据，后台再补正文，
逐篇提交；跨频道相同文章复用已存原文，后台补抓与手动重抓按文章 ID 互斥，避免提交前被并发重复抓取。
失败记录保留，最多自动尝试三次，后续尝试轮换可用凭证，
不会在同一次请求中连续尝试。重新抓取沿用原文章标识；授权失败不会覆盖已有正文。

关闭转写后财新仍可同步。财新使用全实例统一的小时级周期，默认 6 小时；每轮检查各启用板块的最新页，
再轮换一个板块补抓一页旧文，持续扩展历史知识库。账号和个人订阅不能覆盖财新周期。
所有 hippo 财新入口共用 PostgreSQL 中的访问预算，重启不会重置：
列表间隔 15–30 秒，正文间隔 30–90 秒，每看 3–5 篇休息 30–90 分钟；每天最多 60 次操作、20 次正文操作，
按 UTC 自然日计数。付费权限探测算一次正文操作，内部包含列表和正文请求。失败至少休息 15 分钟，
HTTP 429 遵守 `Retry-After`。休息时立即返回暂停，不让子进程或请求长时间等待。
该预算约束 hippo；独立运行 Rust CLI 不使用 hippo 的数据库限速。

财新 Cookie 可以用 crate 附带工具从已登录 Chromium 的本机 CDP 连接导出。Cookie 文件需妥善保存，
导出工具使用 `0600` 权限且不打印值。正文依赖账号实际订阅权限；凭证测试会尝试当前付费文章。

### 转写

一个 `media_job` 一条任务，由 `hippo.sync_worker` 里与正文抓取并行的常驻循环消费：

1. **现成字幕优先**：播客的 `podcast:transcript`、B站的 CC 字幕。时间戳是真的，且不花算力。
2. 否则**下载音频 → 留档（128k mp3，不设体积上限）→ 切片 → 逐片转写 → 校验 → 落 blocks**。
3. 切片长度恒定（默认 300s）：太长的片会被模型截断，太短的尾片会让解码器输出乱码，两者都实测过；
   短尾会并进前一片，输出再用字/秒与重复字符占比校验一次（`hippo/media/validate.py`）。

转写服务是一个 **OpenAI 兼容的 `/v1/audio/transcriptions` 端点**，在管理台「转写」里配置两个档位：

| 档 | 用途 | 实测 |
|---|---|---|
| `accurate`（默认） | 要精度 | `qwen3-asr-1.7b-q8_0`，与既有生产基线字级一致度 0.992 |
| `fast` | 批量粗转，事后可按需重转 | `sensevoice-small`，快约 15 倍 |

需要的外部组件：`ffmpeg`/`ffprobe`（切片、转码）、`yt-dlp`（仅 YouTube）、以及上面那个 ASR 端点
（本实例跑在 archlinux31 上的 [CrispASR](https://github.com/CrispStrobe/CrispASR) `--server`）。
`deploy.sh` 会在远端检查前两个。

### 原生视频与留档

视频和音频走同一条流水线，只是归档对象不同（`article_media.kind` 为 `video` 或 `audio`）：

- **播放。** 留档成功的视频由 Hippo 自己的路由提供：`GET /api/media/{article_pk}/video`。
  它与音频共用订阅鉴权与 Range 语义（`Accept-Ranges`、206/`Content-Range`、越界 416），
  播放器可以拖动进度；对象已经是 `stored` 时，转写还没完成也能先播，阅读器不再加载第三方 iframe。
- **工具链。** 除 `ffmpeg`/`ffprobe` 与 `yt-dlp`（仅 YouTube）外，给经 ffmpeg 的下载/合并设
  `util-linux` 的 `prlimit` 资源与时长上限。部署前逐条确认即可，不需要现在安装：

  ```bash
  ffmpeg -version && ffprobe -version
  prlimit --version        # util-linux
  yt-dlp --version         # 仅 YouTube 来源需要
  ```

- **媒体设置。** 实例级开关在管理台「转写」（存 `meta`，默认值见 `hippo/media_settings.py`）；
  凭证与代理属于各自的来源，在「来源」页按源配置：
  `enabled` 关掉整个媒体流水线，`keep_media` 决定下载的文件是否留档到对象存储。
  视频轨优先选 H.264 + AAC、分辨率不超过 1080p 的兼容轨；视频轨**原样 copy**（不重编码），
  只有音频按 `keep_media_bitrate`（`original` 或 64k–256k）归档，两者的设置不要混用。
  单任务的下载总量上限 2 GiB，单条时长上限 `max_duration_seconds`（默认 14400 秒），
  超限的任务失败或跳过，而不是无限重试。
- **处理时机。** 新条目进队列后由 `hippo.sync_worker` 自动处理；历史条目只按需单篇重试
  （阅读器里的「转写这篇」），不对全库回填。

验证（不连真实平台，用短本地 fixture 与假 S3/HTTP）：

```bash
uv run pytest tests/test_media_delivery.py tests/test_article_transcript_api.py
uv run pytest                     # 全量用例，媒体流水线在其中
uv run ruff check hippo tests
```

预期：已留档视频在 `_get_article` 响应里是 `type=video`、`url=/api/media/{article_pk}/video`、
不带 `embed_url`；`GET /api/media/{pk}/video` 返回 200/206，越界范围返回 416；
未留档的条目不出现播放器。

限制：本轮只保证**单个媒体消费者**。多实例 worker 的长任务缺少心跳/租约，同一任务可能被
重复领取，所以不要并行启动多个消费者。平台 cookie、地区可用性与账号权限仍会限制能否取到资源，
这里**不包含 DRM 或付费绕过**。来源没有兼容视频轨时会退回音频/字幕，此时不能声称视频留档成功。

### 多源约定

- **区分来源靠 `accounts.source` 列**，不是路由前缀。`source` 是开放的标识符（不是枚举）。
  `accounts.biz` 是主键，语义是「该来源内的频道标识」（B站是 mid、合集标识或视频号，YouTube 是 channel id，播客是 feed 哈希）。
- **写入端必须显式设置 `source`。** 列上的 `DEFAULT 'weixin'` 只是为了让已有数据继续工作；
  依赖默认值会被静默标成微信。
- **接口不加前缀。** 列表用 `GET /api/article?source=a,b` 过滤，每条文章响应都带 `source`；
  只有来源**特有的能力**才用独立路由（微信的 `POST /api/article/{id}/refetch`）。
- **前端不做来源分支。** 来源的名称与配色在 `frontend/src/utils/source.ts` 的 `SOURCE_META`，
  内容类型在 `CONTENT_KIND_META`（列表徽章读它，微信的 `item_show_type` 只留给分面筛选）。
  未登记的来源会原样显示而不是消失。
- **后端同步适配器是唯一的改造点。** `hippo/sources/` 里 `ADAPTERS` 按 `account.source` 选适配器；
  新增来源 = 加一个 `MediaSource`（`preview` + `list_items`）与一个 `MediaResolver`（把队列里的条目变成音频或字幕）。
- **卡片与阅读器不假设内容是段落流。** 视频条目可能整篇只有一个媒体块，没有正文时不生成 AI 大纲；没有小标题时仍可打开目录面板。
- **凭证是「订阅者自己的优先、实例兜底」。** 频道是共享的（`accounts`），cookie 是个人的
  （`user_credential`），所以「用谁的」没有唯一答案，顺序固定为：该频道的订阅者各自那一份
  （最近更新的先试）→ 实例那份（管理台「来源」页配的）。只有**凭证被拒**才换下一份：
  网络错误、视频被删这些换谁都一样，重试只会把请求数乘上订阅者数。实现在
  `hippo/sources/chain.py`，列表阶段（`MediaSync`）与转写阶段（`MediaResolverRegistry`）走同一条链。
- **「转写」页只管跨源参数。** ASR 端点、切片长度、归档码率是一套流水线服务所有媒体源，
  所以不挂在某个来源下；凭证与代理按来源走「来源」页。

### 已读状态

已读是**按用户**的，存在 `article_read` 边表里（`(user_id, article_pk)` 主键），不写回共享的 `articles`。
读文章即标记已读（`PUT /api/article/{id}/read`）；重复标记不会再刷新 `read_at`；
列表的 `read=unread` 走同一张表。

### 收藏

收藏和已读一样是按用户的边表 `article_star`：`PUT /api/article/{id}/star` 切换，列表用 `starred=true` 过滤。
收藏过的条目在列表里始终显示星标按钮，方便随时取消。

### 稍后再看

在文章卡片、右键菜单或阅读器工具栏点击「加入稍后再看」，再从「全部 / 未读 / 收藏」旁的
「稍后再看」进入列表。文章、视频和音频共用一份个人清单，重复加入不会改变顺序；支持上移、下移、置顶和移除。
列表不继承信息流的来源、搜索、未读或排除词筛选，只显示当前仍有权阅读的内容。

从列表打开音视频后，播放结束会按保存的顺序进入下一项并尝试播放；普通文章需要手动切换，最后一项结束后停止，
不循环。媒体不可用或浏览器拒绝自动播放时会提示，不自动跳过。离开列表后不再自动切换；不支持离开阅读器后后台连续播放。
已读、收藏与清单互不影响，打开或播放完不会自动移除。

清单保存在 `article_watch_later`，用户外键和文章外键均为 `ON DELETE CASCADE`；删除用户或文章会自动清理对应条目。
升级需先执行 `python -m hippo.ops init-db`，schema 版本为 `36`。接口和权限规则见 [API.md](API.md#稍后再看-apiwatch-later)。

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
  原文目录与可隐藏的 AI 导航大纲、排版设置（宽度 / 字号 / 行高 / 字距 / 衬线）、未读箱、已读标记与收藏、
  高亮标注、按内容哈希全局屏蔽图片。
- **日报**：按用户时区生成当天摘要并邮件发送；投递台账保证同一天同一渠道只发一次（`POST /api/report/{date}/send` 幂等）。
- **AI 对话**：对单篇文章提问或走预设（摘要 / 要点）；模型供应商在管理台配置，密钥只以末四位回显。
- **RSS**：`GET /api/feed/mixed?format=rss`（用 `token=` 代替 Cookie）。
- **可观测**：structlog 结构化日志、可选 OTLP 上报 SignOz、`GET /api/admin/log/tail` 兜底看日志文件。

### 公众号引用链接

阅读文章时会识别正文中的公众号长链、短链和旧式文章链接，包括加粗里的超链接与裸 URL。
先按文章身份查询本地：已有正文直接在 Hippo 内打开，缺失正文自动进入现有微信抓取队列；页面显示排队、抓取和失败状态，失败可单独重试。
保留原锚文本，裸 URL 命中后显示文章标题，已收录链接旁可打开微信原文。

- 前提：运行 schema 33 初始化，并启用 sync worker、保持微信 daemon 已登录；不需要新服务或依赖。
- 只处理已订阅来源文章的直接引用，每篇最多 200 个 URL；不递归抓取，不根据无 URL 的标题猜测文章。
- 引用其他公众号时只开放对应单篇及其图片，不自动订阅该公众号，也不开放它的其他文章。
  来源退订、禁用或正文版本变化后，引用授权失效；阅读器重新处理当前正文后才建立新关联。
- `GET /api/article/{id}/reference` 只读；`POST` 接受空对象以关联和入队，`{"retry_url":"正文中的URL"}` 单独重试失败引用。
- 验证：`uv run pytest tests/test_wechat_links.py tests/test_article_references.py tests/test_reference_repository.py tests/test_reference_ingest.py`；
  前端运行 `cd frontend && npm test -- src/utils/markdown.test.tsx src/pages/articles/ArticleReferences.test.tsx`。
  隔离 PostgreSQL 上可设置 `HIPPO_TEST_PG_DSN` 后运行 `uv run pytest tests/test_reference_postgres.py`，每个用例创建并删除独立测试 schema，不读取生产配置。

### 文章导航大纲

打开文章后点击「目录」：有原文标题时默认展示原文目录，无标题时可切换或手动「生成大纲」。
AI 大纲按原文顺序组织为章节和子话题，可折叠、跟随阅读位置高亮；点击标题只定位正文，
点击时间戳才播放对应音视频（无本地留档时打开来源链接）。桌面为阅读侧栏，手机为底部抽屉，
都可用关闭按钮收起，或按 Escape 关闭。

- 前提：先在管理台配置可用的默认 LLM Provider。微信文章、B站、YouTube、播客与后续来源使用同一文本块契约，不按来源限制。
- 只在手动触发时调用模型，结果独立缓存，不修改原文、划线或导出。正文、时间戳或生成规则变化后，旧大纲不再提供跳转。
- 长内容按完整文本分块再合并，不截取文章开头冒充全文。单篇上限 240,000 个正文字符，过大内容明确拒绝。
- 仅有图片或尚未转写的媒体不能生成；本功能不做 OCR，也不能提升现有 ASR 时间戳精度。
- 生成队列持久化在 PostgreSQL，由 Web 进程领取，无新增 systemd 服务。关闭面板不取消任务；正常退出归还租约，崩溃后的租约在 15 分钟后可重领。
  单任务最多运行 10 分钟；模型的上下文容量需容纳约 12,000 字符的分块、提示词及输出。
- 验证：`uv run pytest tests/test_outline.py tests/test_outline_api.py tests/test_outline_worker.py tests/test_outline_repository.py`；
  前端运行 `cd frontend && npm test -- src/pages/articles/TableOfContents.test.tsx src/pages/articles/outline.test.ts`。
  真实模型还应人工核对全文末尾是否覆盖、章节顺序和跳转位置。

## 入口命令

hippo 只保留两个常驻模块入口、一个可选的知识 worker 与一组运维命令，日常业务（订阅、同步、RSS、导出）都在 Web 管理台完成。

```bash
uv run python -m hippo.server --help        # API + SPA
uv run python -m hippo.sync_worker --help   # 同步 worker
uv run python -m hippo.knowledge.worker     # 私有知识库导入 worker（可选，无参数）
uv run python -m hippo.ops --help            # 数据库与账号运维
```

| 命令 | 用途 |
|---|---|
| `python -m hippo.server` | 启动 API + SPA（`--unix-socket`、`--host/--port`、`--unix-socket-mode`、`--static-dir`、`--inprocess-sync`）；没有完整的 `--host/--port` 对时必须给 `--unix-socket` |
| `python -m hippo.sync_worker` | 独立同步进程（`--poll-interval`，默认 5s 轮询任务队列） |
| `python -m hippo.knowledge.worker` | 私有知识库导入进程（无参数；配置与日志全部读 `config.toml`） |
| `python -m hippo.ops init-db [--dsn …]` | 建表/升级 schema（幂等） |
| `python -m hippo.ops create-admin --username … [--password …] [--email …] [--timezone …]` | 创建管理员（CLI 建的账号直接视为已验证；密码缺省时交互隐藏输入） |
| `python -m hippo.ops assign-owner --owner <用户名>` | 把多用户改造前的无主分组/订阅归到该用户（可重复执行） |

四个循环（列表 / 正文 drain / 图片回填 / 推送监听）全部在 `hippo.sync_worker` 里并行走，
不再有单独的 `article sync`、`account sync` 等业务命令。正文重放等一次性维护仍用
`scripts/replay_article_documents.py`。

## HTTP API

全部接口见 **[API.md](API.md)**；服务本身也暴露 `GET /docs`（Swagger UI）与 `GET /openapi.json`。

## 数据库

- schema 定义在 `schema/postgres.sql`，`hippo.ops init-db` 幂等执行；`hippo/storage.py` 里的 `SCHEMA_VERSION`
  与库中记录不一致时服务拒绝启动（迁移总是先于重启）。
- 主要表：`accounts`/`subscription`/`account_groups`（目录 + 每用户订阅）、`articles`/`article_content`/
  `article_document`/`article_images`（文章与派生内容）、`article_queue`（列表→正文的队列）、
  `sync_jobs`（同步任务）、`avatar_images`（头像缓存）、`blocked_image_hashes`（图片屏蔽）、
  `users`/`user_session`/`user_token`（账号与会话）、`annotation`/`article_read`（阅读数据）、
  `chat_session`/`chat_message`、`llm_provider`、`report_setting`/`report_delivery`、`audit_log`、`meta`（键值）。
- 文章图片存在 S3 兼容对象存储，`article_images` 只存元数据与 `s3_key`；下载失败的记录由
  `hippo.sync_worker` 的图片回填循环重试。

## 配置

部署配置只有一处：仓库根目录的 `config.toml`（不入库，每台机器一份）。所有键都有默认值，
唯一必填的是 `[database] dsn`。

```bash
cp config.example.toml config.toml
```

`hippo/config.py` 在首次使用时读取它并缓存在进程内（改完要重启），路径相对工作目录，
所以服务与运维命令都要在仓库根下运行。**没有环境变量这一层**：`.env` 已不再参与配置。

| 段 | 键 | 默认 | 说明 |
|---|---|---|---|
| `[database]` | `dsn` | — | **必填**，PostgreSQL 连接串 |
| | `pool_min` / `pool_max` | `1` / `8` | 连接池大小（`pool_max` 不会小于 `pool_min`）|
| | `warmup_jieba` / `warmup_text` | `true` / `hippo` | 启动时预热分词器 |
| | `disable_jit` | `true` | 关掉 PG 的 JIT（大查询更稳）|
| | `init_log` | `false` | 打印库初始化过程 |
| `[s3]` | `endpoint` / `bucket` / `access_key` / `secret_key` | — | 对象存储；四项缺任一即视为未配置，图片按需回源 |
| | `region` / `prefix` | `us-east-1` / `mp/image/` | 按 path-style 寻址 |
| `[web]` | `cookie_secure` | `false` | HTTPS 部署置 `true` |
| | `session_ttl_hours` | `168` | 会话有效期 |
| | `inprocess_sync` | `false` | 在 Web 进程内运行持久化队列 worker（仅开发用；勿与独立 worker 同时启用）|
| | `signoz_url` | — | 管理台「看日志」深链目标 |
| `[logging]` | `level` | `WARNING` | 日志级别 |
| | `format` | `json` | `json`（SignOz 吃的格式）或 `console` |
| | `file` | `logs/{service}.log` | `{service}` 换为 `hippo-web` / `hippo-sync-worker` / `hippo-knowledge-worker`；空即不写文件 |
| | `tail_lines` | `500` | 管理台日志尾行数 |
| `[http]` | `article_worker` / `article_worker_proxy` / `article_max_connections` | — | 用 Cloudflare Worker 中转抓正文；代理不再在这里配置 |
| `media:settings` | `proxy_default` / `proxy_<source>` | 见管理台 | **按来源**的代理，在管理台「来源 → 某个来源」页配置（来源：公众号 / B站 / YouTube / 播客）|
| `[weixin]` | `sdk_path` | `/opt/weixin-rs/sdk/python` | weixin-rs SDK 路径 |
| | `daemon_host` / `daemon_port` | `127.0.0.1` / `9099` | daemon 地址 |
| `[observability]` | `enabled` | `true` | 置 `false` 可停掉导出而不丢 endpoint |
| | `otlp_endpoint` | — | OTLP/gRPC 端点；留空即整个遥测关闭 |
| | `insecure` | `false` | 采集器走明文 gRPC（`http://` 端点）时置 `true` |
| | `sample_ratio` | `1.0` | `1.0` 导出全部 span，失败的 trace 也在内；调低会在 SDK 里丢掉整条 trace，**错误一并丢**。要降量请在采集器侧做 tail sampling |
| | `environment` | `prod` | 写入 `deployment.environment` |

systemd unit 里只剩 `PYTHONUNBUFFERED=1`（Python 运行时开关，不是应用配置）以及 `RuntimeDirectory`
这类 systemd 自身的东西。

## 开发

```bash
uv run pytest                # 全量用例（不连数据库）
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
├── server.py            # FastAPI 应用与静态资源（python -m hippo.server 入口）
├── knowledge/           # 私有知识库：上传状态机、PDF/TXT 解析、导入 worker
├── knowledge/worker.py  # 知识导入 worker（python -m hippo.knowledge.worker 入口）
├── sync_worker.py       # 同步 worker（python -m hippo.sync_worker 入口）
├── ops.py               # 运维命令：init-db / create-admin / assign-owner
├── api/                 # 路由（routers/）、依赖、错误处理、限流
├── storage.py           # 连接池、schema 版本、通用查询
├── repositories/        # 数据访问层（account/article/image/chat/audit/…）
├── article_queries.py   # 文章查询、全文检索、图片代理
├── downloader.py        # 正文抓取与图片下载
├── weixin_daemon.py     # weixin-rs daemon 的 RPC 客户端
├── weixin_ingest.py     # 微信列表入队与正文落库
├── weixin_watch.py      # 微信文章推送监听与入队
├── sync_worker.py       # 任务领取与执行
├── sync_service.py      # 同步流程编排
├── sync_jobs.py / sync_types.py / sync_core.py / sync_settings.py / account_sync.py
├── sync_runner.py       # 开发模式下运行同一持久化队列 worker
├── image_backfill.py    # 图片回填循环
├── image_store.py / image_hashes.py / s3.py / file_storage.py
├── avatar.py            # 各来源头像缓存
├── public_image.py      # 头像公网目标校验、连接固定与有界下载
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
hippo-knowledge-worker.service              # 知识导入 worker 的 systemd 单元示例（可选）
hippo/knowledge/worker.py                   # 知识导入 worker（python -m hippo.knowledge.worker 入口）
```

通用同步统计类型位于 `sync_types.py`，来源适配器不依赖微信入库模块。微信专属代码仍保留明确的
`weixin_*` 名称，不为统一命名更改数据库中的来源标识。

## 部署

自托管按「rsync 源码 + systemd」即可：构建前端后把仓库同步到目标机，
`uv sync --frozen --no-dev --extra weixin` 对齐依赖，跑 `python -m hippo.ops init-db`，再用附带的三个 unit 分别跑
`hippo.server`、`hippo.sync_worker` 与（需要知识库时）`hippo.knowledge.worker`。注意顺序：先迁移（`init-db`）、
后重启服务，schema 版本不匹配时服务会拒绝启动。

上线前须完成隔离验证并取得部署授权：

1. 当前代码 `SCHEMA_VERSION = '37'`，36 → 37 为同步任务追加 `scope_sources TEXT[]`，
   用于固定运行期间的来源互斥范围。31 → 32 追加 `article_outline` 缓存/任务表与待处理索引；
   32 → 33 追加 `article_reference` 直接引用表、身份查询及队列关联索引，
   使用**纯追加、幂等**的 `CREATE TABLE IF NOT EXISTS` / `CREATE INDEX IF NOT EXISTS`；
   完整初始化脚本仍有历史清理语句，执行前必须核对这些清理项不会触及现存数据。
   还需在隔离 PG 上验证 DDL、`pg_jieba`/`pg_trgm`、GIN 索引、UUID 类型、CAS 语义、部分唯一索引与并发锁行为，
   并验证 S3 的上传/下载/删除。
2. 正式迁移流程：**先备份代码和配置**，数据库转储仅在操作者明确要求时执行，不作为日常部署默认步骤；
   然后**停掉 Web、sync worker 与知识 worker 三个进程**（
   知识 worker 持有 lease/长事务，迁移前必须停），再人工跑 `python -m hippo.ops init-db`，确认库的 `schema_version` 与代码一致
   后再重启。不要让旧进程在新 schema 上继续跑。
3. **schema 只能前进**：库升级后，回退到较旧 schema 版本的代码会因版本不匹配
   拒绝启动；优先修复并继续迁移，不能改 `meta.schema_version` 蒙混过关。确需恢复时，先将备份恢复到
   **新的隔离数据库**核对，再由操作者批准切换；不要直接用旧快照覆盖生产库，避免丢失备份之后的写入。
4. **保留调度配置**：部署不改变 `enabled` 与检查周期。普通用户可调用 `POST /api/settings/run`；
   管理员全站更新使用 `POST /api/settings/admin/run`。定时检查关闭不停止已入队任务。
5. **`systemctl is-active` 不等于功能健康**：验收要看 sync worker 新日志（如图片回填是否仍有持续失败）、
   页面 `frontend/dist/assets/` 的哈希是否与新 build 一致，以及只启一个媒体消费者。
6. `deploy.sh` 是被 Git 忽略的本地运维工具，不随仓库分发。使用前必须核对它是否在覆盖代码前备份、
   是否同步三个 unit 并重载 systemd、是否在迁移前停掉三个数据库服务，以及失败时是否避免启动版本不匹配的代码。
   本机脚本已按这些要求做离线演练；其他机器上的同名脚本不能据此视为已验证。

```bash
# 以下都需要先完成上面步骤并取得授权，在真实环境执行
sudo cp hippo-knowledge-worker.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now hippo-knowledge-worker
systemctl status hippo-knowledge-worker
journalctl -u hippo-knowledge-worker -n 50
```

`Dockerfile` 只打包 API/UI：容器里没有 weixin-rs daemon 与其 SDK，因此**不能同步**，仅适合看界面或调 API。
它同样没有知识 worker 的入口，需要知识库时请用 systemd 单元而不是这个镜像。


### 公众号采集的四个阶段

新公众号登记后自动进入历史列表补全队列，分页游标随进度保存；完成只表示
已发现历史文章。推送事件与定时近期列表检查也将文章写入同一正文队列，
由独立消费者抓正文并入库，再处理图片。列表任务的新入队数不等于已收录数。

管理台的「定时检查近期文章」用于推送补漏，沿用原 `enabled` 与周期配置；
关闭后推送、历史补全和已入队下载仍继续。正文、图片下载开关分别控制各自消费者。
公众号卡片和来源页共用 `sync` 状态：历史列表进度、正文和图片积压、近期检查时间分开显示。
历史任务暂停保留游标，使用「继续补全历史」恢复，不会重新翻完已完成的历史。

后台任务以 PostgreSQL `sync_jobs` 为唯一执行入口。开发用 `web.inprocess_sync`
也运行同一 worker，不再直接执行另一套定时同步；生产继续使用独立 systemd worker。

不同来源（公众号、YouTube、RSS 等）的列表任务并行执行，同一来源保持串行请求和限流。
正常重启会归还未完成任务，历史补全从已保存游标继续；正文批次归还队列且不增加失败次数。

同步 worker 要求 `[database].pool_max >= 6`（默认 8），为来源请求、任务进度和后台消费者保留连接。
并发数量随现有连接池上限调整，不新增并发配置；配置读取完成后即释放连接，再开始来源请求。
