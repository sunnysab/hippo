# Hippo

管理微信公众号文章的工具集。提供 CLI 和 Web UI，支持文章同步、下载、全文检索与图片管理。

## 快速开始

```bash
# 安装
pip install -e .

# 配置数据库
cp .env.example .env
# 编辑 .env 设置 HIPPO_PG_DSN

# 初始化数据库
hippo db init

# 登录微信公众号平台
hippo login

# 搜索并添加公众号
hippo account search 关键词 --interactive

# 同步文章列表 + 下载正文
hippo account sync-all
hippo article sync-all
```

## Web UI

```bash
# 构建前端
npm --prefix frontend ci && npm --prefix frontend run build

# 启动 Web 服务
hippo serve --host 0.0.0.0 --port 8000
```

也支持 Docker：

```bash
docker build -t hippo .
docker run -it --rm -e HIPPO_PG_DSN="postgresql://user:pass@host:5432/dbname" hippo --help
```

## 功能

### 登录与会话

扫码登录微信公众号后台，持久化保存 session（token + cookies），支持多会话管理。

### 公众号管理

- 通过微信接口搜索公众号（支持交互式筛选）
- 分组管理、批量设置同步参数
- 头像缓存

### 文章同步

- **列表同步**：从公众号拉取文章元数据（标题、摘要、封面、发布时间等），支持断点续传
- **内容下载**：下载正文 HTML，转换为 Markdown 和结构化 JSON blocks
- **图片下载**：下载文章内嵌图片，存入 PostgreSQL 或 S3 对象存储
- 频率控制自动退避、失败重试、增量跳过

### 全文检索

基于 PostgreSQL `pg_jieba` 扩展实现中文分词全文检索：

- `articles.search_vector` 列（`tsvector`），标题权重 A、摘要/正文权重 B、作者权重 C
- 正文取前 50000 字符索引
- 支持关键词搜索、排除关键词过滤、相关度排序
- 查询入口：`GET /api/article?q=关键词`

### Web API

FastAPI 提供 REST API：

| 端点 | 说明 |
|---|---|
| `GET /api/article` | 文章列表（支持分页、搜索、筛选、排序） |
| `GET /api/article/{id}` | 文章详情（包含正文内容和图片） |
| `GET /api/group` | 分组管理 |
| `GET /api/account` | 公众号管理 |
| `POST /api/login/start` | 扫码登录 |
| `GET/PATCH /api/settings` | 同步设置 |
| `POST /api/settings/run` | 手动触发同步 |
| `GET /api/image/{id}` | 图片获取 |
| `POST /api/image/{id}/block` | 屏蔽图片 |
| `GET /api/feed/mixed` | 混合 Feed（支持 RSS） |

### 后台同步

同步逻辑拆分为独立 worker：

```bash
# Web 服务（仅提供 API/UI）
hippo serve --host 0.0.0.0 --port 8000

# 同步 worker（定时检查并执行同步任务）
hippo sync-worker
```

配合 systemd 部署（附 `hippo.service` / `hippo-sync-worker.service`）。

## 数据库

### PostgreSQL 表结构

| 表 | 说明 |
|---|---|
| `accounts` | 公众号信息（biz、昵称、分组、同步参数） |
| `articles` | 文章元数据 + `search_vector` 全文索引 |
| `article_content` | 正文（clean_html、markdown、JSON blocks） |
| `article_images` | 图片元数据（S3 key、内容哈希、失败记录） |
| `blocked_image_hashes` | 用户屏蔽的图片内容哈希 |
| `account_groups` | 公众号分组 |
| `sync_jobs` | 同步任务队列 |
| `login_sessions` | 登录会话 |
| `meta` | 键值存储（schema 版本、同步进度等） |

### 图片存储

支持 PostgreSQL 和 S3 兼容对象存储。图片下载失败会记录原因，可通过 `hippo article backfill-images` 重试。

## 配置

核心环境变量（参见 `hippo/config.py`）：

| 变量 | 默认值 | 说明 |
|---|---|---|
| `HIPPO_PG_DSN` | — | PostgreSQL 连接串 |
| `HIPPO_PG_POOL_MIN` | `1` | 连接池最小连接数 |
| `HIPPO_PG_POOL_MAX` | `8` | 连接池最大连接数 |
| `HIPPO_PG_JIEBA_WARMUP` | `1` | 是否预热 jieba 分词 |
| `HIPPO_ARTICLE_WORKER` | — | Cloudflare Worker 中转地址 |
| `HIPPO_ARTICLE_WORKER_PROXY` | — | Worker 访问代理 |
| `HIPPO_ARTICLE_MAX_CONNECTIONS` | — | Worker 最大并发连接 |
| `HIPPO_LOG_LEVEL` | `WARNING` | 日志级别 |
| `HIPPO_ENABLE_INPROCESS_SYNC` | — | 是否在 Web 进程内执行同步 |

## 开发

```bash
# 安装开发依赖
pip install -e .

# 代码检查
ruff check hippo/
ruff format --check hippo/

# 测试
python -m pytest tests/
```

## 目录结构

```
hippo/
├── cli.py              # Typer CLI 入口
├── server.py           # FastAPI Web 服务
├── storage.py          # PostgreSQL 连接池与存储抽象
├── repositories.py     # 数据访问层
├── article_queries.py  # 文章查询与全文搜索
├── downloader.py       # 文章与图片下载器
├── wechat_api.py       # 微信公众平台 API 客户端
├── wechat_parser.py    # 微信 HTML 解析
├── normalize_html.py   # HTML 清洗
├── sync_service.py     # 同步调度
├── sync_worker.py      # 后台同步 worker
├── container.py        # DI 容器
├── config.py           # 配置与常量
├── exceptions.py       # 应用异常
├── models.py           # Pydantic 数据模型
├── http.py             # HTTP 客户端（支持 Worker 代理）
├── image_store.py      # 图片存储服务
├── image_hashes.py     # 图片内容哈希
├── file_storage.py     # S3 文件存储
├── s3.py               # S3 工具
├── emailer.py          # 邮件通知
├── avatar.py           # 公众号头像缓存
├── rss.py              # RSS Feed
├── utils.py            # 工具函数
├── controllers/sync.py # 同步控制器
└── schema/
    └── postgres.sql    # 数据库建表脚本

frontend/               # React Web UI
tests/                  # 测试
scripts/                # 迁移与调试脚本
```
