# WeChat 文章导出 CLI (Python 版)

基于 [Typer](https://typer.tiangolo.com) 的命令行工具，用于管理公众号、同步文章列表、下载文章内容，数据存储使用 PostgreSQL。

---

## 安装与运行

### 方式 A：虚拟环境 + 本地运行

```bash
python -m venv .venv
source .venv/bin/activate          # Windows 使用 .venv\Scripts\activate
pip install -r requirements-cli.txt
python -m hippo --help
```

### 方式 B：开发模式安装

```bash
pip install -e .
hippo --help
```

### 方式 C：Docker

```bash
# Build image
docker build -t hippo .

# Run with PostgreSQL
docker run -it --rm -e HIPPO_PG_DSN="postgresql://user:pass@host:5432/dbname" hippo --help

# Example: login
docker run -it --rm -e HIPPO_PG_DSN="postgresql://user:pass@host:5432/dbname" hippo login

# Example: sync articles
docker run -it --rm -e HIPPO_PG_DSN="postgresql://user:pass@host:5432/dbname" hippo article sync-all
```

---

## 登录与账号管理

### 登录

```bash
python -m hippo login
```

扫码后会保存登录会话（token + cookies）。

### 搜索公众号（推荐交互式）

```bash
python -m hippo account search 关键词 --interactive
```

- 默认每页 10 条
- 已添加的账号会在昵称后标注“（已添加）”
- 交互式模式：输入 `1,3-5` 选择添加；输入 `q` 退出

### 管理已保存账号

```bash
python -m hippo account list
python -m hippo account add        # 手动保存 fakeid
python -m hippo account remove <biz>
python -m hippo account set-default <biz>
```

---

## 同步文章列表

### 账号同步（文章列表）

```bash
python -m hippo account sync --biz <fakeid>
python -m hippo account sync-all
```

默认按最新 -> 更早 翻页，写入文章列表缓存。

特点：
- 每 60 次请求强制等待 15 秒
- 触发频率控制（freq control）后自动等待重试
- 支持断点续传（按账号保存 offset）
- 已全量完成过的账号可快速增量（遇到已存在整页直接停止）
- 进度条展示“已抓取/总数 + 成功/未完成/失败”

### 跳过逻辑

默认会跳过“今日已完成”的账号。可用以下参数控制：

```bash
python -m hippo account sync-all --force
python -m hippo account sync-all --skip-time 30
```

- `--force`：忽略跳过逻辑
- `--skip-time 30`：30 分钟内同步过的账号跳过
- 两者不可同时使用

### 断点与完成标记

- 断点进度：保存在 meta 表，key 为 `sync_progress:<biz>`
- 当页为空时判定“已完成”，写入 `sync_complete:<biz>`（当天有效）

### Web 同步接口说明

- `GET /api/sync`
  - 返回全局同步状态与最近同步历史。
  - 默认仅返回最近 `5` 条 `history`（可通过 `limit` 参数调整，范围 `1~50`）。
- `GET /api/sync/tasks`
  - 返回任务实例列表（含 `pending/running` 任务的细粒度进度，如当前账号、当前文章、已完成账号数等）。
  - 默认返回最近 `5` 个任务（可通过 `limit` 参数调整）。
- `GET /api/sync/settings` / `PATCH /api/sync/settings`
  - 同步设置包含 `window_start_hour` 与 `window_end_hour`（默认 `6` 到 `24`）。
  - 自动调度只会在该时间段内触发执行。

---

## Download Articles

### Basic usage

```bash
python -m hippo article sync --biz <fakeid>
python -m hippo article sync-all
python -m hippo article download "https://mp.weixin.qq.com/..."
```

- `article sync` / `sync-all`: download content based on the synced article list
- `article download`: download a single article URL

### Worker configuration

- Article HTML can be fetched via a Cloudflare Worker (images are still direct):
  - `--worker-prefix`: worker prefix or template (`{url}` placeholder), defaults to `HIPPO_ARTICLE_WORKER`
  - `--worker-proxy`: proxy for worker requests (HTTP/SOCKS5), defaults to `HIPPO_ARTICLE_WORKER_PROXY`
  - `--workers`: article download concurrency, defaults to `HIPPO_ARTICLE_MAX_CONNECTIONS` (alias: `--worker-max-connections`)

### Download behavior
- Article HTML fetch retries up to 5 times before skipping the article.
- Image downloads run on 2 background threads and retry up to 3 times.
- Failed image downloads are marked in `article_images.failed_*` and can be retried via `article backfill-images`.
- For `sync-all`, each account waits for its image queue to finish before moving to the next account.
- If interrupted with Ctrl+C, any queued images are marked as failed for later backfill.

---

## 数据库存储

### PostgreSQL

设置环境变量：

```bash
export HIPPO_PG_DSN="postgresql://user:pass@host:5432/dbname"
```

程序将使用 PostgreSQL 进行读写。

**PostgreSQL 时间字段使用 `timestamptz`**（带时区）。

### PostgreSQL 文章内容与图片存储

当设置 `HIPPO_PG_DSN` 时，`article download/sync/sync-all` 会将文章内容与图片写入 PG：

- `articles` 表保存文章元数据（包含封面 URL）
- `article_content` 表保存正文内容：
  - `url_token`（文章 URL 中 `/s/<token>`）
  - `clean_html`（清理后的正文 HTML，图片仍为原始 URL）
  - `content_markdown`（正文 Markdown，不含封面图与标题）
  - `content_json`（图文混排的 blocks）
- `article_images` 表保存图片二进制（`BYTEA`），PG 会自动 TOAST 压缩

`content_json` 的 block 结构示例：

```json
[
  {"type": "heading", "level": 1, "text": "标题"},
  {"type": "paragraph", "text": "正文段落"},
  {"type": "image", "kind": "inline", "orig_url": "https://..."},
  {"type": "link", "text": "https://...", "href": "https://..."}
]
```

---

## 全文检索（pg_jieba）

系统使用 PostgreSQL 的 `pg_jieba` 扩展实现分词全文检索。首次启用或升级后，需要重新执行：

```bash
python -m hippo db init
```

实现细节：
- 使用 `jiebaqry` 配置生成 `tsvector`，标题权重 A、摘要/正文权重 B、作者权重 C。
- 为避免超大文档导致 `tsvector` 过大，正文仅取前 50,000 字符参与索引。
- 查询入口为 HTTP API：`/api/article?q=关键词`（按相关度排序）。

---

## 日志系统

CLI 集成了 Python 标准库 logging，默认仅输出必要信息。

### 启用详细控制台日志

使用 `--verbose` 或 `-v` 选项：

```bash
hippo --verbose article sync-all
hippo -v account sync-all
```

详细模式会将日志输出到终端。

---

## 配置与路径

### Environment variables

核心配置：`hippo/config.py`

| 配置项 | 说明 |
| --- | --- |
| `HIPPO_PG_DSN` | PostgreSQL DSN |
| `HIPPO_PG_JIEBA_WARMUP` | 是否在新建 PG 连接时预热 `jiebaqry`（默认 `1`） |
| `HIPPO_PG_JIEBA_WARMUP_TEXT` | 预热分词使用的文本（默认 `hippo`） |
| `HIPPO_ARTICLE_WORKER` | 文章 HTML worker 前缀或模板 |
| `HIPPO_ARTICLE_WORKER_PROXY` | worker 访问代理 |
| `HIPPO_ARTICLE_MAX_CONNECTIONS` | worker 最大并发连接数 |

## Backfill missing images (PostgreSQL)

If you have pending `article_images` rows without binary data, you can backfill them:

```bash
python scripts/fill_pg_images.py --pg-dsn "postgresql://user:pass@host:5432/dbname"
```

CLI alternative:

```bash
python -m hippo article backfill-images --pg-dsn "postgresql://user:pass@host:5432/dbname"
```

Optional flags:
- `--limit 1000` to cap the number of images per run
- `--workers 8` to control concurrent downloads
- `--retries 3` to control download retries
- `--dry-run` to list targets without writing

### Article HTML proxy (images stay direct)

- `HIPPO_ARTICLE_WORKER`: Cloudflare Workers relay for article HTML, e.g. `https://c0c0.sunnysab.workers.dev/?url=` or `https://c0c0.sunnysab.workers.dev/?url={url}`.
- `HIPPO_ARTICLE_WORKER_PROXY`: Optional HTTP/SOCKS5 proxy for reaching the worker (e.g. `http://192.168.133.201:8080/`); omit to access the worker directly.
- `HIPPO_ARTICLE_MAX_CONNECTIONS`: Optional max concurrent connections to the worker client (e.g. `2`) to keep proxy usage under control.
- When the downloader sees `https://mp.weixin.qq.com/s/...`, it requests the worker URL (with the encoded article URL) using the configured proxy; images are fetched without any proxy using the original URLs.

---

## 目录结构

```
.
├── README.md
├── requirements-cli.txt
├── pyproject.toml
├── hippo/normalize_html.py
├── scripts/
└── hippo/
    ├── cli.py
    ├── config.py
    ├── downloader.py
    ├── http.py
    ├── models.py
    └── storage.py
```

---

## 常见问题

**Q: 频率控制怎么办？**
A: 程序检测到 “freq control” 会自动等待重试；每 60 次请求自动等待 15 秒。

**Q: 为什么显示“未完成”？**
A: 说明这次没有遍历到空页（比如中断、限制页数等）。下次会继续断点。

**Q: 只想增量同步？**
A: 全量完成过的账号会在同步时遇到整页已存在即停止。
