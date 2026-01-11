# WeChat 文章导出 CLI (Python 版)

基于 [Typer](https://typer.tiangolo.com) 的命令行工具，用于管理公众号、同步文章列表、下载文章内容，并支持 SQLite / PostgreSQL 存储。

---

## 安装与运行

### 方式 A：虚拟环境 + 本地运行

```bash
python -m venv .venv
source .venv/bin/activate          # Windows 使用 .venv\Scripts\activate
pip install -r requirements-cli.txt
python -m wechatcli --help
```

### 方式 B：开发模式安装

```bash
pip install -e .
wechatcli --help
```

---

## 登录与账号管理

### 登录

```bash
python -m wechatcli login
```

扫码后会保存登录会话（token + cookies）。

### 搜索公众号（推荐交互式）

```bash
python -m wechatcli account search 关键词 --interactive
```

- 默认每页 10 条
- 已添加的账号会在昵称后标注“（已添加）”
- 交互式模式：输入 `1,3-5` 选择添加；输入 `q` 退出

### 管理已保存账号

```bash
python -m wechatcli account list
python -m wechatcli account add        # 手动保存 fakeid
python -m wechatcli account remove <biz>
python -m wechatcli account set-default <biz>
```

---

## 同步文章列表

### 账号同步（文章列表）

```bash
python -m wechatcli account sync --biz <fakeid>
python -m wechatcli account sync-all
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
python -m wechatcli account sync-all --force
python -m wechatcli account sync-all --skip-time 30
```

- `--force`：忽略跳过逻辑
- `--skip-time 30`：30 分钟内同步过的账号跳过
- 两者不可同时使用

### 断点与完成标记

- 断点进度：保存在 meta 表，key 为 `sync_progress:<biz>`
- 当页为空时判定“已完成”，写入 `sync_complete:<biz>`（当天有效）

---

## 下载文章内容

```bash
python -m wechatcli articles sync --biz <fakeid>
python -m wechatcli articles sync-all
python -m wechatcli articles download "https://mp.weixin.qq.com/..."
```

- `articles sync` / `sync-all`：根据已同步的列表下载文章内容
- `articles download`：直接下载单篇 URL

默认输出目录：
`~/.local/share/wechatcli/downloads/`（可通过 `WECHATCLI_HOME` 改）

目录结构示例：

```
downloads/<账号名或biz>/<YYYY-MM-DD-标题>/
  ├── index.html / article.md / article.txt
  ├── metadata.json
  └── images/
```

---

## 数据库存储

### SQLite（默认）

默认数据库路径：
`~/.local/share/wechatcli/cli.db`

可通过环境变量覆盖：

```bash
export WECHATCLI_HOME=/path/to/custom
```

### PostgreSQL（可选）

设置环境变量即可切换：

```bash
export WECHATCLI_PG_DSN="postgresql://user:pass@host:5432/dbname"
```

程序将使用 PostgreSQL 进行读写。

**PostgreSQL 时间字段使用 `timestamptz`**（带时区）。

### PostgreSQL 文章内容与图片存储

当设置 `WECHATCLI_PG_DSN` 时，`articles download` 会将文章内容与图片写入 PG：

- `articles` 表会保存：
  - `url_token`（文章 URL 中 `/s/<token>`）
  - `clean_html`（清理后的正文 HTML，图片仍为原始 URL）
  - `content_markdown`（正文 Markdown，不含封面图与标题）
  - `content_json`（图文混排的 blocks）
  - `cover_image_id`（封面图对应的图片表记录）
- `article_images` 表保存图片二进制（`BYTEA`），PG 会自动 TOAST 压缩

`content_json` 的 block 结构示例：

```json
[
  {"type": "heading", "level": 1, "text": "标题"},
  {"type": "paragraph", "text": "正文段落"},
  {"type": "image", "kind": "inline", "orig_url": "https://...", "image_id": 123},
  {"type": "link", "text": "https://...", "href": "https://..."}
]
```

---

## SQLite -> PostgreSQL 导出

脚本路径：`scripts/export_to_pg.py`

```bash
python scripts/export_to_pg.py \
  --pg-dsn "postgresql://user:pass@host:5432/dbname" \
  --sqlite-path "/home/user/.local/share/wechatcli/cli.db"
```

可选参数：
- `--truncate`：导出前清空 PG 表

导出过程会显示进度条与速率。

---

## 配置与路径

核心配置：`wechatcli/config.py`

| 配置项 | 说明 |
| --- | --- |
| `WECHATCLI_HOME` | 覆盖数据目录 |
| `WECHATCLI_PG_DSN` | 使用 PostgreSQL |
| `DB_PATH` | SQLite DB 路径 |
| `DOWNLOAD_ROOT` | 下载目录 |

---

## 目录结构

```
.
├── README.md
├── requirements-cli.txt
├── pyproject.toml
├── normalize_html.py
├── scripts/
│   └── export_to_pg.py
└── wechatcli/
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
