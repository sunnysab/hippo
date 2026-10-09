# Hippo API 参考

面向 `hippo serve` 暴露的 HTTP 接口。文中所有端点都以 `/api` 开头，覆盖当前全部 **78** 个端点。
服务同时提供框架自带的 OpenAPI 描述与交互界面：`GET /openapi.json`、`GET /docs`（Swagger UI）。
本文件是它们的说明：约定、权限、参数语义与响应结构。

## 目录

- [约定](#约定)
- [1. 用户认证](#1-用户认证-apiauth)
- [2. 微信登录（daemon）](#2-微信登录daemonapilogin)
- [3. 分组](#3-分组apigroup)
- [4. 公众号](#4-公众号apiaccount)
- [5. 文章与图片](#5-文章与图片apiarticle-apiimage)
- [6. 标注](#6-标注)
- [7. 混合 Feed / RSS](#7-混合-feed--rss)
- [8. 同步设置与任务](#8-同步设置与任务apisettings)
- [9. 日报](#9-日报apireport)
- [10. AI 对话](#10-ai-对话apichat)
- [11. LLM Provider](#11-llm-provider-apillm)
- [12. 站点设置与管理](#12-站点设置与管理apiadmin)
- [附录 A：端点总览](#附录-a端点总览)
- [附录 B：状态码](#附录-b状态码)

## 约定

### 基址

单进程同时提供 SPA 与 API：`/` 是前端（构建产物由 `--static-dir` 指定），`/api` 是接口。
开发时前端 dev server 通过代理访问同一路径。

### 认证

登录态是**服务端会话 + HttpOnly Cookie**，没有 Bearer Token：

| 项 | 值 |
|---|---|
| Cookie 名 | `hippo_session` |
| 属性 | `HttpOnly`、`SameSite=Lax`、`Path=/`；`Secure` 由 `HIPPO_COOKIE_SECURE=1` 打开（纯 HTTP 开发环境必须关掉） |
| 有效期 | 默认 168 小时（`HIPPO_SESSION_TTL_HOURS`，最小 1 小时） |

请求带上 Cookie 即可，前端不需要读取 token（JS 也读不到）。
权限分三档，各端点表格里的「权限」列即此：

| 权限 | 含义 |
|---|---|
| 公开 | 不需要登录（注册、找回密码、`/api/login/*` 之外的登录相关） |
| 登录 | 任意已启用、邮箱已验证的用户 |
| 管理员 | `role = admin`，否则 `403` |

唯一的例外是 RSS 阅读器：`GET /api/feed/mixed` 无法携带 Cookie，可以用查询参数 `token=<会话令牌>` 代替登录
（令牌是登录时下发的会话 token，见 [`/api/auth/session`](#会话与安全)）。

### 错误

所有业务错误统一为：

```json
{ "error": "错误描述" }
```

HTTP 状态码表示类别，不把错误塞进 `200`。未预期异常返回 `500` 与 `{"error": "Internal server error"}`。
参数校验失败（如非法 `sort`、日期格式错误、缺少必填字段）是 `400`；越权访问别人的资源一律 `404` 而不是 `403`，避免泄露存在性。

### 分页与时间

- 分页参数为 `page`（从 1 开始）与 `page_size`，上限因端点而异，响应统一带 `total`；审计日志额外带 `pages`。
- 日期参数 `since`/`until` 接受 `YYYY-MM-DD` 或 ISO 时间串；`until` 只给日期时按当天 23:59:59 处理。
- 时间戳 `publish_at` 是 **Unix 秒**；`created_at`、`updated_at` 是 **ISO 8601 字符串**（数据库 `timestamptz` 经统一序列化）。

### 图片与头像

图片二进制不直接暴露对象存储地址，而是一律经过代理端点：

| 需求 | 端点 |
|---|---|
| 文章图片 | `GET /api/image/{image_id}` |
| 公众号头像（订阅目录） | `GET /api/account/{biz}/avatar` |
| 公众号头像（搜索候选） | `GET /api/account/search/{gh_id}/avatar` |

三者都返回带正确 `Content-Type` 的二进制，并带 `Cache-Control: public, max-age=259200`（3 天）。
头像接口在缓存未命中时会回源抓取并落库，因此首次请求可能稍慢；源地址缺失时才 `404`。

## 1. 用户认证 `/api/auth`

| 方法 | 路径 | 权限 | 用途 |
|---|---|---|---|
| POST | `/api/auth/login` | 公开 | 登录，下发会话 Cookie |
| POST | `/api/auth/logout` | 公开 | 注销当前会话 |
| GET | `/api/auth/me` | 登录 | 当前用户 |
| POST | `/api/auth/password` | 登录 | 修改密码（并轮换所有会话） |
| POST | `/api/auth/register` | 公开 | 注册（发送验证邮件） |
| POST | `/api/auth/verify` | 公开 | 消费邮箱验证令牌 |
| POST | `/api/auth/reset-request` | 公开 | 发送重置密码邮件 |
| POST | `/api/auth/reset` | 公开 | 用令牌设置新密码 |
| GET | `/api/auth/session` | 登录 | 列出我的会话 |
| POST | `/api/auth/session/revoke` | 登录 | 注销全部会话 |

### 登录

```
POST /api/auth/login
{ "username": "admin", "password": "…" }
```

成功：`204` + `Set-Cookie: hippo_session=…`（无响应体）。
失败：`400` 字段为空、`401` 用户名或密码错误、`403` 账号被禁用或邮箱未验证、`429` 同 IP 5 分钟内超过 10 次尝试。
失败会写审计日志（`auth.login_failed`），成功写 `auth.login`。

### 注册

```
POST /api/auth/register
{ "username": "u", "email": "u@example.com", "password": "…" }
```

`202` 返回 `{"status": "pending_verification", "email_sent": true}`。
`email_sent` 为 `false` 表示站点未配置 SMTP，邮件没发出去（账号仍然创建，可由管理员直接置为已验证）。
开关来自站点设置 `registration_enabled`，关闭时 `403`；用户名/邮箱重复 `409`；密码至少 8 位；同 IP 15 分钟限 5 次（`429`）。

### 邮箱验证与重置

```
POST /api/auth/verify        { "token": "…" }        → 204，令牌无效或过期 400
POST /api/auth/reset-request { "email": "…" }        → 202 {"status": "accepted"}
POST /api/auth/reset         { "token": "…", "password": "…" } → 204
```

`reset-request` **永远返回 `accepted`**，以免被用来探测哪些邮箱已注册；邮件里的令牌 1 小时内有效（验证邮件 24 小时）。
`reset` 成功后该账号所有会话立即失效。

### 会话与安全

```
GET  /api/auth/me      → {id, username, email, email_verified, role, timezone}
GET  /api/auth/session → { "sessions": [ {id, created_at, expires_at, user_agent, ip}, … ] }
POST /api/auth/session/revoke → 204，注销本账号全部会话
POST /api/auth/password { "current_password": "…", "new_password": "…" }
```

`/api/auth/password` 成功返回 `204` 并**换发新 Cookie**：改密码会吊销所有会话（包括其他设备），
但当前这次请求会拿到一张新会话，不会被踢下线。响应里不会出现令牌哈希。

## 2. 微信登录（daemon）`/api/login`

抓取用的微信号登录由 weixin-rs daemon 负责，hippo 不保存微信凭据。这些端点全部要求**登录**（Hippo 用户）。

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/api/login` | daemon 登录状态 |
| POST | `/api/login/auto` | 用 daemon 本地 `auto_auth_key` 免扫重登 |
| POST | `/api/login/qr` | 取扫码二维码 |
| POST | `/api/login/wait` | 等扫码确认（daemon 侧轮询，最长约 5 分钟） |

`GET /api/login` 在 daemon 不可达时**也返回 200**，只是 `status: "unreachable"` 并带 `error`：

```json
{ "logged_in": true, "status": "online", "need_relogin": false,
  "wxid": "…", "nickname": "…", "head_url": "…", "clients_connected": 1, "error": "" }
```

`POST /api/login/qr` → `{"uuid", "url", "png_base64", "expires_in"}`；拿到后轮询 `POST /api/login/wait`。
daemon 通信失败返回 `502`。

## 3. 分组 `/api/group`

| 方法 | 路径 | 权限 | 用途 |
|---|---|---|---|
| GET | `/api/group` | 登录 | 分组列表（含默认分组 ID） |
| POST | `/api/group` | 登录 | 新建分组 |
| GET | `/api/group/{group_id}` | 登录 | 分组详情 |
| PATCH | `/api/group/{group_id}` | 登录 | 重命名 |
| DELETE | `/api/group/{group_id}` | 登录 | 删除（组内公众号移到默认分组） |

```
GET /api/group → { "default_group_id": 1, "groups": [ {"id": 1, "name": "默认分组"}, … ] }
POST /api/group { "name": "经济&金融" } → 201
DELETE /api/group/3 → 204
```

新建/重命名时 `name` 为空是 `400`；删除不存在的分组是 `404`。**分组按用户隔离**，别人的 `group_id` 对你是 `404`。

## 4. 公众号 `/api/account`

| 方法 | 路径 | 权限 | 用途 |
|---|---|---|---|
| GET | `/api/account` | 登录 | 已关注公众号列表（可筛选、分页） |
| POST | `/api/account` | 登录 | 添加公众号（同时订阅） |
| GET | `/api/account/{biz}` | 登录 | 公众号详情 |
| PATCH | `/api/account/{biz}` | 登录 | 更新昵称/别名/头像/分组/停用/同步间隔 |
| DELETE | `/api/account/{biz}` | 登录 | 取消关注 |
| POST | `/api/account/move` | 登录 | 批量移动到分组 |
| POST | `/api/account/batch` | 登录 | 批量设置同步间隔 |
| GET | `/api/account/search` | 登录 | 搜索公众号（候选） |
| GET | `/api/account/search/{gh_id}/avatar` | 登录 | 候选头像 |
| GET | `/api/account/{biz}/avatar` | 登录 | 已关注公众号头像 |

### 列表

```
GET /api/account?group_id=2&group_ids=1,3&q=金融&page=1&page_size=20
```

| 参数 | 类型 | 说明 |
|---|---|---|
| `group_id` | int | 单个分组（向后兼容） |
| `group_ids` | str | 逗号分隔的多个分组，优先于 `group_id` |
| `q` | str | 关键词：昵称 / 微信号 / biz |
| `page` / `page_size` | int | 默认 1 / 20，`page_size` 上限 200 |

响应为 `{accounts: [...], page, page_size, total}`。列表会返回 `alias`（`null` 归一为 `""`）、
`avatar_url`（即 `/api/account/{biz}/avatar`）以及订阅侧的 `group_id`、`is_disabled`、`sync_interval_days`。

### 添加

```
POST /api/account
{ "biz": "gh_abc123", "nickname": "某号", "alias": "gh_abc", "round_head_img": "…", "group_id": 2 }
```

`biz` 与 `nickname` 必填。传 `gh_…` 形状的 `biz` 时，服务端会先解析成 fakeid（`Mz…==`），
解析失败返回 `502`。添加成功后：写入共享目录 + 订阅你的分组，并在 `backfill_state = pending` 时
投递一条历史回填任务（worker 接手，已回填过的号不会重复）。

### 更新与批量

```
PATCH /api/account/Mz123
{ "nickname": "…", "alias": "…", "round_head_img": "…", "group_id": 3,
  "is_disabled": false, "sync_interval_days": 7 }
```

字段分两类：`nickname`/`alias`/`round_head_img` 是**全站共享**的目录字段；
`group_id`/`is_disabled`/`sync_interval_days` 属于**你自己的订阅**。`group_id: null` 会落回默认分组。
没有任何可更新字段时 `400`，公众号不存在（或不在你的订阅里）时 `404`。

```
POST /api/account/move  { "biz_list": ["Mz1", "Mz2"], "group_id": 3 }
POST /api/account/batch { "biz_list": ["Mz1"], "sync_interval_days": 3 }
```

`sync_interval_days: null` 表示清除覆盖、回到按发文历史自动推导。两者都返回受影响条数。

### 搜索候选

```
GET /api/account/search?q=中投&page=1&page_size=10
```

返回 `{results: [{biz, nickname, alias, round_head_img, is_added, avatar_url}], page, page_size, total}`。
这里的 `biz` 是微信 `gh_…`（搜索结果只给这个），头像缓存在 `gh_…` 键下；
`is_added` 表示该号是否已在你的目录里。daemon 搜索失败返回 `502`。

## 5. 文章与图片 `/api/article` `/api/image`

| 方法 | 路径 | 权限 | 用途 |
|---|---|---|---|
| GET | `/api/article` | 登录 | 文章列表（筛选 + 全文检索 + 分面） |
| GET | `/api/article/{id}` | 登录 | 文章详情（含正文块与图片） |
| GET | `/api/article/{id}/image` | 登录 | 文章图片列表 |
| POST | `/api/article/{id}/refetch` | 登录 | 重新抓取正文 |
| GET | `/api/article/refetch/{task_id}` | 登录 | 轮询重新抓取进度 |
| GET | `/api/image/{image_id}` | 登录 | 图片二进制 |
| POST | `/api/image/{image_id}/block` | 登录 | 按内容哈希全局屏蔽该图 |

### 文章列表

```
GET /api/article
```

| 参数 | 类型 | 说明 |
|---|---|---|
| `page` / `page_size` | int | 默认 1 / 20，上限 200 |
| `q` | str | 全文检索（标题 + 正文 + 作者，`pg_jieba` 中文分词） |
| `sort` | str | `publish_at_desc`（默认）或 `relevance_desc`（仅 `q` 非空时有意义） |
| `group_id` | int | 单个分组（向后兼容） |
| `group_ids` | str | 逗号分隔的多个分组，优先于 `group_id` |
| `biz` | str | 按公众号筛选 |
| `article_id` | str | 按微信原始 article_id 精确匹配 |
| `item_show_type` | int | 版式类型，见下表 |
| `since` / `until` | str | 发布时间范围 |
| `exclude_keywords` | str | 排除关键词（逗号/分号/换行分隔），命中标题、摘要或作者即过滤；不传则用个人设置里的 `article_exclude_keywords` |

`item_show_type` 取值：`0` 图文、`5` 视频、`6` 直播、`7` 专辑、`8` 话题、`10` 纯视频、`11` 图片消息、`17` 付费。
数据库里为 `null` 的按 `0` 返回。

```json
{
  "articles": [
    {
      "id": 42,
      "biz": "MzIxMjM0NTY3OA==",
      "article_id": "MjM5MTA1MjI4MA==",
      "title": "文章标题",
      "item_show_type": 0,
      "author": "作者名",
      "digest": "摘要",
      "cover": 4488748,
      "link": "https://mp.weixin.qq.com/s/…",
      "source_url": "https://…",
      "publish_at": 1700000000,
      "created_at": "2026-05-02T12:00:00+00:00",
      "account_nickname": "公众号名称",
      "account_alias": "gh_abc123",
      "account_avatar": "http://wx.qlogo.cn/mmhead/…/132",
      "account_avatar_url": "/api/account/MzIxMjM0NTY3OA==/avatar",
      "group_id": 1,
      "group_name": "经济&金融",
      "image_id": 4488748
    }
  ],
  "page": 1,
  "page_size": 20,
  "total": 380178,
  "item_show_type_facets": [ { "item_show_type": 0, "count": 120 }, { "item_show_type": 8, "count": 3 } ]
}
```

要点：

- `cover` 与 `image_id` 都是 **`article_images.id`（整数）**，不是 URL；图片要走 `/api/image/{id}`。`cover` 可能为 `null`。
- `image_id` 是「优先封面、否则第一张已落 S3 的图」，可直接当缩略图用；没有可用图时为 `null`。
- `account_avatar` 是原始腾讯 CDN 地址，仅作信息保留；前端应使用 `account_avatar_url` 代理路径。
- `item_show_type_facets` 是按当前筛选条件统计的版式分布（不受 `item_show_type` 自身影响）。

### 文章详情

```
GET /api/article/42
```

```json
{
  "article": { "…": "与列表中的单条结构一致，另有 account_avatar_url" },
  "content": [ { "type": "paragraph", "text": "…" }, { "type": "image", "image_id": 43, "alt": "" } ],
  "content_status": "ok",
  "content_updated_at": "2026-05-02T12:00:00+00:00",
  "content_fetch_diagnostic": null,
  "images": [ { "id": 43, "position": 0, "kind": "cover", "content_type": "image/jpeg",
                "hash_algo": "sha256", "content_hash": "…" } ]
}
```

- `content` 是正文块数组，块类型：`paragraph`/`heading`（带 `level`）/`image`（带 `image_id`、`alt`、`orig_url`）/`code`/`link`。已被屏蔽的图片块会被过滤掉。
- `content_status`：`ok` | `missing`（没抓过）| `empty` | `invalid`（解析失败）。
- `content_fetch_diagnostic`：非 `ok` 时给出抓取失败诊断（`last_error`、`error_type`、`retryable`、`attempts`），否则 `null`。
- `images` 只含 `article_images` 的元数据；`kind` 目前是 `cover` 或 `inline`。

### 重新抓取

```
POST /api/article/42/refetch  → 200 { "task_id": "…", "status": "started" }
GET  /api/article/refetch/{task_id} → { "task_id": "…", "status": "running"|"done"|"error",
                                       "phase": "downloading"|null, "started_at": 12.34,
                                       "finished_at": 15.01, "error": null }
```

重新抓取会**落回原文章行**（沿用原文的 `biz` 与 `article_id`），刷新正文、图片清单与封面，
不会新增副本。任务在后台线程里跑，`GET` 轮询状态即可；已结束的任务 5 分钟后从内存里清理（`404`）。

### 图片

```
GET  /api/image/43          → 二进制；本地已有则直接返回，否则回源抓取后落库
POST /api/image/43/block    → 200 { "image_id": 43, "hash_algo": "sha256", "content_hash": "…", "blocked": true }
```

`block` 之后，任何文章里的同一张图（哈希相同）都会从正文块与图片列表中消失。回源失败返回 `502`。

## 6. 标注

用户在文章上的高亮与批注，按用户隔离。

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/api/article/{id}/annotation` | 该文章下我的全部标注，响应为 `{"items": [...]}` |
| POST | `/api/article/{id}/annotation` | 新建标注 |
| DELETE | `/api/article/{id}/annotation/{annotation_id}` | 删除标注 |

```
POST /api/article/42/annotation
{ "quote": "被高亮的原文", "prefix": "前文", "suffix": "后文", "note": "批注", "color": "default" }
```

`quote` 必填且不超过 2000 字；`color` 取值 `default`/`yellow`/`green`/`blue`/`pink`，其它值 `400`；
`prefix`/`suffix` 用于在正文里重新定位锚点，超长会被截断。删除别人的标注返回 `404` 而不是 `403`。

## 7. 混合 Feed / RSS

```
GET /api/feed/mixed
```

| 参数 | 类型 | 说明 |
|---|---|---|
| `group_id` / `group_ids` | int / str | 分组筛选（`group_ids` 优先） |
| `biz` | str | 按公众号筛选 |
| `q` | str | 关键词 |
| `limit` | int | 条数，默认 50，上限 500 |
| `format` | str | `rss` 输出 RSS 2.0 XML，其它值输出 JSON |
| `since` / `until` | str | 时间范围 |
| `days` | int | 最近 N 天（与 `since` 同时给出时以 `days` 为准） |
| `token` | str | 会话令牌，供无法携带 Cookie 的 RSS 阅读器使用 |

JSON 形态返回 `{"articles": [...]}`；`format=rss` 时返回 `application/rss+xml`，条目里的 `link` 指向本地文章页（用请求头 `Host` 拼绝对地址，建议部署时配置反代传递真实 Host）。
未提供 Cookie 或有效 `token` 时 `401`。

## 8. 同步设置与任务 `/api/settings`

| 方法 | 路径 | 权限 | 用途 |
|---|---|---|---|
| GET | `/api/settings` | 管理员 | 全站同步配置 + SMTP |
| PATCH | `/api/settings` | 管理员 | 更新同步配置 |
| GET | `/api/settings/preferences` | 登录 | 我的阅读偏好 |
| PATCH | `/api/settings/preferences` | 登录 | 更新我的阅读偏好 |
| POST | `/api/settings/run` | 登录 | 手动触发同步 |
| GET | `/api/settings/status` | 登录 | 同步总体状态 + 队列水位 |
| GET | `/api/settings/tasks` | 管理员 | 任务列表 |
| GET | `/api/settings/tasks/{task_id}` | 管理员 | 任务进度 |
| POST | `/api/settings/tasks/{task_id}/cancel` | 管理员 | 取消任务 |
| POST | `/api/settings/test-email` | 管理员 | 发测试邮件 |

### 配置

```
GET /api/settings
→ { "enabled": true, "interval_minutes": 30, "sleep_seconds": 3, "skip_minutes": 0,
    "download_content": true, "download_images": true,
    "alert_enabled": false, "alert_email": "",
    "email": { "smtp_host": "", "smtp_port": 587, "smtp_user": "", "smtp_password": "",
               "smtp_tls": true, "from_email": "" } }
```

`PATCH /api/settings` 只更新传入的字段（`enabled`、`interval_minutes`、`sleep_seconds`、`skip_minutes`、
`download_content`、`download_images`、`alert_enabled`、`alert_email`）；`email` 传对象时按其子字段更新 SMTP。
仅传小数范围内的字段也接受，但 `smtp_port` 非法会 `400`。打开 `enabled` 会立即唤醒调度器。

### 阅读偏好

```
GET   /api/settings/preferences → { "timezone": "Asia/Shanghai", "article_exclude_keywords": "" }
PATCH /api/settings/preferences { "timezone": "Asia/Shanghai", "article_exclude_keywords": "招聘,抽奖" }
```

`timezone` 同时决定日报的「今天」与发送时刻；`article_exclude_keywords` 是文章列表的默认排除词。

### 手动同步

```
POST /api/settings/run                       # 同步我订阅的全部
POST /api/settings/run { "group_id": 2 }     # 只同步某个分组
POST /api/settings/run { "biz_list": ["Mz1"] } # 只同步指定公众号
```

`202` 返回 `{"status": "...", "task_id": "...", ...}`（附带回显的 `group_id` / `biz_list`）。
`group_id` 不属于你、或 `biz_list` 里有未订阅的号都是 `404`；请求体字段非法 `400`。

### 状态与任务

```
GET /api/settings/status?limit=5
→ { "status": "idle"|"running"|..., "last_started_at": "...", "last_finished_at": "...",
    "last_ingest_at": "...", "last_error": "", "history": [ … ],
    "queue": { … , "failed_items": [ … ] }, "worker_heartbeat_at": "..." }

GET /api/settings/tasks?limit=5&detail=false   → { "tasks": [ … ] }
GET /api/settings/tasks/{task_id}             → 任务完整状态（404 表示不存在）
POST /api/settings/tasks/{task_id}/cancel     → 可取消状态返回更新后的任务，否则 404
```

任务状态字段：`task_id`、`status`、`trigger_type`（`manual`/`scheduled`/`backfill`）、`phase`、
`accounts_total`、`accounts_done`、`current_account`、`current_article`、`last_log`、`report`、`error`、
`created_at`/`started_at`/`finished_at`，`detail=false` 时返回精简摘要。取消是协作式的：worker 在下一个检查点停下。

### 测试邮件

```
POST /api/settings/test-email { "to_email": "me@example.com", "email": { "smtp_host": "…" } }
```

不给 `to_email` 时回落到配置里的 `alert_email`；两者都空则 `400`。传 `email` 对象则用这份临时 SMTP 配置发送而不落库。

## 9. 日报 `/api/report`

| 方法 | 路径 | 权限 | 用途 |
|---|---|---|---|
| GET | `/api/report/setting` | 登录 | 我的日报设置 |
| PATCH | `/api/report/setting` | 登录 | 更新日报设置 |
| GET | `/api/report/{date}` | 登录 | 某天日报的 JSON |
| GET | `/api/report/{date}/html` | 登录 | 某天日报的 HTML（与邮件正文一致） |
| POST | `/api/report/{date}/send` | 登录 | 立即发送该天日报 |

```
GET   /api/report/setting → { "enabled": false, "send_hour": 9, "recipients": [], "group_ids": [], "include_read": false }
PATCH /api/report/setting { "enabled": true, "send_hour": 9, "recipients": ["me@example.com"],
                            "group_ids": [1, 2], "include_read": false }
```

`send_hour` 必须是 `0-23` 的整数（按时区解释）；`date` 是**用户时区下的自然日**，可传 `today` 之外的具体日期，
非法日期落回当天。响应包含 `{date, timezone, total, groups, setting}`。

**幂等**：`POST /api/report/{date}/send` 走投递台账，同一天同一个渠道只会发送一次。
如果当天已经投递过，返回 `202` 但 `{"sent": false, "reason": "already_delivered"}`；
未配置 SMTP 时把该次记为 `skipped`，返回 `reason: "smtp_not_configured"`；未配置收件人且账号没有邮箱时 `400`。

## 10. AI 对话 `/api/chat`

对文章提问，按用户隔离。

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/api/chat/session` | 我的会话列表（新的在前） |
| POST | `/api/chat/session` | 新建会话（同文章的已有会话会被复用） |
| GET | `/api/chat/session/{session_id}` | 会话 + 完整消息历史 |
| POST | `/api/chat/session/{session_id}/rename` | 重命名 |
| DELETE | `/api/chat/session/{session_id}` | 删除会话 |
| GET | `/api/chat/session/{session_id}/preset/{preset}` | 读取已缓存的预设回答 |
| POST | `/api/chat/session/{session_id}/message` | 发消息，SSE 流式返回 |

```
POST /api/chat/session { "article_id": 42, "title": "文章解读" }
```

`article_id` 必须是你能看到的文章（否则 `404`）；省略时创建普通会话（标题默认「新对话」）。

```
POST /api/chat/session/{id}/message
{ "content": "这篇文章的核心结论是什么？" }     # 自由提问
{ "preset": "summary" }                        # 预设指令：summary | points
```

返回 `text/event-stream`，每行一个 `data: JSON`：

| 载荷 | 含义 |
|---|---|
| `{"delta": "文本"}` | 增量内容 |
| `{"truncated": true}` | 文章太长，上下文被截断 |
| `{"done": true, "message_id": 7}` | 结束（`cached: true` 表示复用预设缓存） |
| `{"error": "…"}` | 生成失败 |

`preset` 命中缓存时直接回放，不调用模型。`content` 与 `preset` 至少给一个，否则 `400`；
会话不属于你返回 `404`。

## 11. LLM Provider `/api/llm`

全部要求**管理员**。API Key 只以「后四位」形式回显，明文不返回。

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/api/llm/provider` | 列表 |
| POST | `/api/llm/provider` | 新建 |
| PATCH | `/api/llm/provider/{id}` | 更新（`api_key` 省略或留空则保留原值） |
| DELETE | `/api/llm/provider/{id}` | 删除 |
| POST | `/api/llm/provider/{id}/test` | 探测 `GET {base_url}/models` |

```
POST /api/llm/provider
{ "name": "OpenAI", "base_url": "https://api.openai.com/v1", "api_key": "sk-…",
  "model": "gpt-4o-mini", "is_default": true, "enabled": true }
```

四个必填字段任一为空即 `400`；`base_url` 会去掉尾部斜杠。同一个 provider 的 `is_default` 互斥。

`POST …/test` 把「连不上」当作正常答案：始终 `200`，用 `{"ok": false, "error": "…"}` 表达失败；
成功时返回 `latency_ms`、`models`（模型 ID 列表）与 `model_available`（配置的模型是否在列表里，列表为空时为 `null`）。

## 12. 站点设置与管理 `/api/admin`

全部要求**管理员**。

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/api/admin/site` | 站点设置 |
| PATCH | `/api/admin/site` | 更新站点设置 |
| GET | `/api/admin/user` | 用户列表（含验证、活跃、会话统计） |
| POST | `/api/admin/user` | 创建用户（直接设密码，邮箱可空） |
| PATCH | `/api/admin/user/{user_id}` | 停用/启用、改角色、改时区、置为已验证 |
| POST | `/api/admin/user/{user_id}/password` | 重置密码并吊销该用户全部会话 |
| DELETE | `/api/admin/user/{user_id}/session` | 吊销该用户全部会话 |
| GET | `/api/admin/audit` | 审计日志（筛选 + 分页） |
| GET | `/api/admin/audit/action` | 出现过的 action 去重列表 |
| GET | `/api/admin/log/tail` | 日志文件末尾若干行 |
| GET | `/api/admin/log/link` | SignOz 深链 |

### 站点设置

```
GET   /api/admin/site → { "site_name": "Hippo", "public_base_url": "", "registration_enabled": false }
PATCH /api/admin/site { "site_name": "Hippo", "registration_enabled": true, "public_base_url": "https://hippo.example" }
```

`registration_enabled` 关掉只是停止新注册，不影响已有账号；`public_base_url` 用于拼邮件里的链接，留空则按请求头推断。
改动会写审计（`admin.site_settings_updated`，只记变化的键）。

### 用户管理

```
GET   /api/admin/user → { "items": [ { id, username, email, email_verified, role, is_disabled, timezone, … } ] }
POST  /api/admin/user { "username": "u", "password": "…", "email": "u@example.com",
                        "role": "user", "email_verified": true }
PATCH /api/admin/user/3 { "is_disabled": true, "role": "admin", "timezone": "Asia/Shanghai", "email_verified": true }
POST  /api/admin/user/3/password { "password": "…" }   → 204
DELETE /api/admin/user/3/session                       → 204
```

创建用户直接设密码，因此不强制邮箱；`username` 必填，密码至少 8 位，`role` 只能是 `user` 或 `admin`（非法值 `400`）。
用户名已存在或邮箱已被使用是 `409`。

### 审计与日志

```
GET /api/admin/audit?action=auth.login&user_id=1&since=2026-05-01&until=2026-05-31&page=1&page_size=50
→ { "items": [ … ], "page": 1, "page_size": 50, "total": 123, "pages": 3 }
```

`page_size` 上限 200。`GET /api/admin/log/tail?lines=500`（上限 5000）读 `HIPPO_LOG_FILE` 指向的文件，
未配置时返回 `{"available": false, "reason": "…", "lines": []}`（同样是 `200`）。
`GET /api/admin/log/link?minutes=60` 未配置 `HIPPO_SIGNOZ_URL` 时返回 `{"available": false, "reason": "…"}`。

## 附录 A：端点总览

| 方法 | 路径 | 权限 |
|---|---|---|
| GET | `/api/account` | 登录 |
| POST | `/api/account` | 登录 |
| POST | `/api/account/batch` | 登录 |
| POST | `/api/account/move` | 登录 |
| GET | `/api/account/search` | 登录 |
| GET | `/api/account/search/{biz}/avatar` | 登录 |
| GET | `/api/account/{biz}` | 登录 |
| PATCH | `/api/account/{biz}` | 登录 |
| DELETE | `/api/account/{biz}` | 登录 |
| GET | `/api/account/{biz}/avatar` | 登录 |
| GET | `/api/admin/audit` | 管理员 |
| GET | `/api/admin/audit/action` | 管理员 |
| GET | `/api/admin/log/link` | 管理员 |
| GET | `/api/admin/log/tail` | 管理员 |
| GET | `/api/admin/site` | 管理员 |
| PATCH | `/api/admin/site` | 管理员 |
| GET | `/api/admin/user` | 管理员 |
| POST | `/api/admin/user` | 管理员 |
| PATCH | `/api/admin/user/{user_id}` | 管理员 |
| POST | `/api/admin/user/{user_id}/password` | 管理员 |
| DELETE | `/api/admin/user/{user_id}/session` | 管理员 |
| GET | `/api/article` | 登录 |
| GET | `/api/article/refetch/{task_id}` | 登录 |
| GET | `/api/article/{article_id}` | 登录 |
| GET | `/api/article/{article_id}/annotation` | 登录 |
| POST | `/api/article/{article_id}/annotation` | 登录 |
| DELETE | `/api/article/{article_id}/annotation/{annotation_id}` | 登录 |
| GET | `/api/article/{article_id}/image` | 登录 |
| POST | `/api/article/{article_id}/refetch` | 登录 |
| POST | `/api/auth/login` | 公开 |
| POST | `/api/auth/logout` | 公开 |
| GET | `/api/auth/me` | 登录 |
| POST | `/api/auth/password` | 登录 |
| POST | `/api/auth/register` | 公开 |
| POST | `/api/auth/reset` | 公开 |
| POST | `/api/auth/reset-request` | 公开 |
| GET | `/api/auth/session` | 登录 |
| POST | `/api/auth/session/revoke` | 登录 |
| POST | `/api/auth/verify` | 公开 |
| GET | `/api/chat/session` | 登录 |
| POST | `/api/chat/session` | 登录 |
| GET | `/api/chat/session/{session_id}` | 登录 |
| DELETE | `/api/chat/session/{session_id}` | 登录 |
| POST | `/api/chat/session/{session_id}/message` | 登录 |
| GET | `/api/chat/session/{session_id}/preset/{preset}` | 登录 |
| POST | `/api/chat/session/{session_id}/rename` | 登录 |
| GET | `/api/feed/mixed` | 登录或 `token` |
| GET | `/api/group` | 登录 |
| POST | `/api/group` | 登录 |
| GET | `/api/group/{group_id}` | 登录 |
| PATCH | `/api/group/{group_id}` | 登录 |
| DELETE | `/api/group/{group_id}` | 登录 |
| GET | `/api/image/{image_id}` | 登录 |
| POST | `/api/image/{image_id}/block` | 登录 |
| GET | `/api/llm/provider` | 管理员 |
| POST | `/api/llm/provider` | 管理员 |
| PATCH | `/api/llm/provider/{provider_id}` | 管理员 |
| DELETE | `/api/llm/provider/{provider_id}` | 管理员 |
| POST | `/api/llm/provider/{provider_id}/test` | 管理员 |
| GET | `/api/login` | 登录 |
| POST | `/api/login/auto` | 登录 |
| POST | `/api/login/qr` | 登录 |
| POST | `/api/login/wait` | 登录 |
| GET | `/api/report/setting` | 登录 |
| PATCH | `/api/report/setting` | 登录 |
| GET | `/api/report/{date}` | 登录 |
| GET | `/api/report/{date}/html` | 登录 |
| POST | `/api/report/{date}/send` | 登录 |
| GET | `/api/settings` | 管理员 |
| PATCH | `/api/settings` | 管理员 |
| GET | `/api/settings/preferences` | 登录 |
| PATCH | `/api/settings/preferences` | 登录 |
| POST | `/api/settings/run` | 登录 |
| GET | `/api/settings/status` | 登录 |
| GET | `/api/settings/tasks` | 管理员 |
| GET | `/api/settings/tasks/{task_id}` | 管理员 |
| POST | `/api/settings/tasks/{task_id}/cancel` | 管理员 |
| POST | `/api/settings/test-email` | 管理员 |

## 附录 B：状态码

| 状态码 | 何时出现 |
|---|---|
| `200` | 成功 |
| `201` | 创建成功（分组、公众号、标注、用户、LLM Provider、会话） |
| `202` | 已受理：注册、找回密码、手动同步、日报发送、重新抓取 |
| `204` | 成功且无响应体（登录、注销、删除、更新类操作） |
| `400` | 参数缺失或非法（密码太短、`send_hour` 越界、日期/排序非法等） |
| `401` | 未登录或会话过期 |
| `403` | 权限不足：非管理员访问管理接口、账号被禁用、邮箱未验证、注册未开放 |
| `404` | 资源不存在，或存在但不属于当前用户 |
| `409` | 冲突：用户名/邮箱已被占用 |
| `429` | 触发限流：登录（10 次 / 5 分钟 / IP）、注册与找回（5 次 / 15 分钟 / IP） |
| `500` | 未预期异常，统一返回 `{"error": "Internal server error"}` |
| `502` | 上游不可用：daemon 搜索/登录失败、图片回源失败、公众号 ID 解析失败 |

## 使用示例

```bash
BASE=http://localhost:8000

# 登录（Cookie 存进 jar，后续请求复用）
curl -sS -c jar.txt -X POST "$BASE/api/auth/login" \
  -H 'content-type: application/json' \
  -d '{"username":"admin","password":"…"}'

# 文章列表：某分组下的图文，按发布时间倒序
curl -sS -b jar.txt "$BASE/api/article?group_ids=1,2&item_show_type=0&page=1&page_size=20"

# 全文检索 + 相关度排序
curl -sS -b jar.txt "$BASE/api/article?q=量化&sort=relevance_desc"

# 文章详情（正文块 + 图片元数据）
curl -sS -b jar.txt "$BASE/api/article/42"

# 订阅一个公众号（先搜索拿 gh_ id）
curl -sS -b jar.txt "$BASE/api/account/search?q=中投数研"
curl -sS -b jar.txt -X POST "$BASE/api/account" \
  -H 'content-type: application/json' \
  -d '{"biz":"gh_13108fd37cba","nickname":"中投数研"}'

# 手动同步一个分组
curl -sS -b jar.txt -X POST "$BASE/api/settings/run" \
  -H 'content-type: application/json' -d '{"group_id":1}'

# RSS
curl -sS "$BASE/api/feed/mixed?format=rss&limit=50&token=<会话令牌>"
```
