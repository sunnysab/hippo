# Hippo API 参考

面向 `hippo.server` 暴露的 HTTP 接口。文中所有端点都以 `/api` 开头。
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
- [13. 媒体来源与转写](#13-媒体来源与转写apisource-apimedia)
- [14. 公开 API Key](#14-公开-api-key-apiapikey)
- [15. 私有知识库](#15-私有知识库-apiknowledge-base-apidocument)
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

所有失败响应统一为同一信封，`code` 是稳定的机器标识：

```json
{ "error": "错误描述", "code": "request.invalid" }
```

HTTP 状态码表示类别，不把错误塞进 `200`。未预期异常返回 `500` 与 `{"error": "Internal server error", "code": "internal_error"}`。

**请求的形状错误是 `422`**：未知字段、类型不对、缺少必填字段、超出取值范围等由请求模型拦截，返回 `422`
与 `code: "request.invalid"`，`error` 里点名失败的字段。**业务语义错误仍是 `400`**（如日期非法、业务范围不合法），
由路由自己抛出；两者不要混为一谈。越权访问别人的资源一律 `404` 而不是 `403`，避免泄露存在性。

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
邮箱验证只约束绑定了邮箱的账号：管理员通过 CLI/控制台创建的账号即便未验证也能登录（没有邮箱时无从验证）。
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
PATCH /api/auth/email  { "current_password": "…", "email": "u@example.com" }
```

`/api/auth/password` 成功返回 `204` 并**换发新 Cookie**：改密码会吊销所有会话（包括其他设备），
但当前这次请求会拿到一张新会话，不会被踢下线。响应里不会出现令牌哈希。

`/api/auth/email` 修改接收系统邮件的地址，返回 `204`。`email` 传空串则清空邮箱。
需要当前密码（`400`）、邮箱格式错误（`400`）、邮箱已被他人使用（`409`）。
调用者已登录并用密码自我证明，所以新地址直接记为已验证，不会重置登录门禁（`email_verified`）；
改动写审计（`auth.email_changed`）。

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

## 4. 订阅账号 `/api/account`

一个 `account` 是一份**订阅的频道（账号）**，不限微信：`accounts.source` 区分来源
（`weixin` / `bilibili` / `youtube` / `podcast` / `feed`）。本节描述账号/订阅的通用语义；
带微信专属字样的地方（`gh_…`/fakeid 解析、H5 搜索、`item_show_type`）只适用于 `weixin` 来源。

| 方法 | 路径 | 权限 | 用途 |
|---|---|---|---|
| GET | `/api/account` | 登录 | 已关注账号列表（可筛选、分页） |
| POST | `/api/account` | 登录 | 添加账号（同时订阅） |
| GET | `/api/account/{biz}` | 登录 | 账号详情 |
| PATCH | `/api/account/{biz}` | 登录 | 更新昵称/别名/头像/分组/停用/同步间隔 |
| DELETE | `/api/account/{biz}` | 登录 | 取消关注 |
| POST | `/api/account/move` | 登录 | 批量移动到分组 |
| POST | `/api/account/batch` | 登录 | 批量设置同步间隔 |
| GET | `/api/account/search` | 登录 | 搜索账号（候选，仅微信来源） |
| GET | `/api/account/search/{gh_id}/avatar` | 登录 | 候选头像（仅微信来源） |
| GET | `/api/account/{biz}/avatar` | 登录 | 已关注账号头像 |

### 列表

```
GET /api/account?group_id=2&group_ids=1,3&source=bilibili&q=金融&page=1&page_size=20
```

| 参数 | 类型 | 说明 |
|---|---|---|
| `group_id` | int | 单个分组（向后兼容） |
| `group_ids` | str | 逗号分隔的多个分组，优先于 `group_id` |
| `source` | str | 只看某个来源（`weixin` / `bilibili` / `youtube` / `podcast` / `feed`） |
| `q` | str | 关键词：昵称 / 微信号 / biz |
| `page` / `page_size` | int | 默认 1 / 20，`page_size` 上限 200 |

响应为 `{accounts: [...], page, page_size, total}`。列表会返回 `alias`（`null` 归一为 `""`）、
`source`、`avatar_url`（即 `/api/account/{biz}/avatar`）以及订阅侧的 `group_id`、`is_disabled`、`sync_interval_days`。

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
没有任何可更新字段时 `400`，账号不存在（或不在你的订阅里）时 `404`。

```
POST /api/account/move  { "biz_list": ["Mz1", "Mz2"], "group_id": 3 }
POST /api/account/batch { "biz_list": ["Mz1"], "sync_interval_days": 3 }
```

`sync_interval_days: null` 表示清除覆盖、回到按发文历史自动推导。两者都返回受影响条数。

### 搜索候选（仅微信来源）

微信 H5 搜索只适用于 `weixin` 来源，其他来源用 `GET /api/source/preview`（见第 13 节）。

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
| PUT | `/api/article/{id}/read` | 登录 | 标记单篇文章已读 / 未读 |
| PUT | `/api/article/{id}/star` | 登录 | 收藏 / 取消收藏单篇文章 |
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
| `read` | str | 传 `unread` 只返回未读文章；不传即全部 |
| `starred` | str | 传 `true`（或 `1`）只返回已收藏文章 |
| `source` | str | 按来源筛选，逗号分隔（如 `weixin,bilibili`），不传即全部 |
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
      "image_id": 4488748,
      "source": "weixin",
      "read_at": null,
      "starred_at": null
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
- `read_at` / `starred_at` 是**当前用户**的已读 / 收藏时间（ISO 8601），未读或未收藏为 `null`。
  两者都按用户隔离，不影响他人。
- `source` 是数据来源标识（`accounts.source`），目前只有 `weixin`；新来源接入后列表与详情都会带上它。
  来源取值是开放集合，不是枚举：服务端只做长度与 ASCII 校验，不做白名单。
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

### 已读状态

```
PUT /api/article/42/read
{ "read": true }
→ 200 { "article_id": 42, "read_at": "2026-05-02T11:00:00+00:00" }
```

`read` 缺省为 `true`，只接受布尔值（否则 `400`）。重复标记已读**不会**刷新 `read_at`，
首次已读时间会一直保留；标记未读（`read: false`）返回 `read_at: null`。
文章不属于调用者的订阅时返回 `404`。

### 收藏

```
PUT /api/article/42/star
{ "starred": true }
→ 200 { "article_id": 42, "starred_at": "2026-05-02T10:00:00+00:00" }
```

与已读同样的规则：`starred` 缺省为 `true`、只接受布尔值、重复收藏不刷新 `starred_at`、
取消收藏（`false`）返回 `starred_at: null`，文章不可见时 `404`。

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
| GET | `/api/settings` | 管理员 | 全站同步配置 + SMTP（密码只回显掩码）|
| PATCH | `/api/settings` | 管理员 | 更新同步配置 |
| GET | `/api/settings/preferences` | 登录 | 我的阅读偏好 |
| PATCH | `/api/settings/preferences` | 登录 | 更新我的阅读偏好 |
| POST | `/api/settings/run` | 登录 | 手动触发同步（只限自己订阅的账号）|
| POST | `/api/settings/admin/run` | 管理员 | 显式触发全站同步 |
| GET | `/api/settings/status` | 管理员 | 同步总体状态 + 队列水位 |
| GET | `/api/settings/tasks` | 管理员 | 任务列表 |
| GET | `/api/settings/tasks/{task_id}` | 管理员 | 任务进度 |
| POST | `/api/settings/tasks/{task_id}/cancel` | 管理员 | 取消任务 |
| POST | `/api/settings/test-email` | 管理员 | 发测试邮件 |

### 配置

```
GET /api/settings
→ { "enabled": true, "interval_minutes": 30, "sleep_seconds": 3, "skip_minutes": 0,
    "download_content": true, "download_images": true, "article_exclude_keywords": "",
    "alert_enabled": false, "alert_email": "",
    "email": { "smtp_host": "smtp.example.com", "smtp_port": 587, "smtp_user": "alerts",
               "smtp_password": "••••word", "smtp_password_set": true,
               "smtp_tls": true, "from_email": "no-reply@example.com" } }
```

**SMTP 密码是只写的。** 读接口只返回掩码（`••••` + 末四位，未配置时为空串），并用
`smtp_password_set` 告诉前端“库里到底有没有密码”；明文永远不出现在响应、日志或本文档里。
写回时，若 `smtp_password` 里还带着掩码（前端把回显值原样提交），服务端会**丢弃该字段**，不会把已存密码
覆盖成一串圆点；传空串才是“清除密码”，传新值才是“替换”。

`PATCH /api/settings` 只更新传入的字段（`enabled`、`interval_minutes`、`sleep_seconds`、`skip_minutes`、
`download_content`、`download_images`、`article_exclude_keywords`、`alert_enabled`、`alert_email`）；`email` 传对象时
按其子字段更新 SMTP。
请求体是封闭的：**未知字段、类型不对、`smtp_port < 1`、`interval_minutes < 1`、`skip_minutes < 0` 都返回
`422`（`request.invalid`）**，不会静默忽略或夹紧成最小值。打开 `enabled` 会立即唤醒调度器。

### 阅读偏好

```
GET   /api/settings/preferences → { "timezone": "Asia/Shanghai", "article_exclude_keywords": "" }
PATCH /api/settings/preferences { "timezone": "Asia/Shanghai", "article_exclude_keywords": "招聘,抽奖" }
```

`timezone` 同时决定日报的「今天」与发送时刻；`article_exclude_keywords` 是文章列表的默认排除词。
只写入请求里出现的字段（未传的字段保持原值），未知字段是 `422`。

### 手动同步

```
POST /api/settings/run                          # 同步我订阅的全部
POST /api/settings/run { "group_id": 2 }        # 只同步某个分组
POST /api/settings/run { "biz_list": ["Mz1"] }  # 只同步指定账号
POST /api/settings/run { "source": "bilibili" } # 只同步某个来源（我订阅的那些）
POST /api/settings/admin/run                    # 管理员：全站目录同步
POST /api/settings/admin/run { "source": "podcast" }  # 全站目录里只看某个来源
```

**“自己”和“全站”是两条显式路由，不靠角色改变同一条路径的含义。**
`POST /api/settings/run`（任意登录用户）只会触及自己已启用的订阅：`group_id` / `source` / `biz_list`
取交集，空请求表示「我的全部订阅」，**不会**变成全站同步；没有可同步的订阅返回 `404`。全站语义只在
管理员专用的 `POST /api/settings/admin/run` 出现。路由与权限见上表。

`202` 返回 `SyncRunResponse`：`task_id`、`status`、`scope`（`own` / `site`），并回显 `group_id` / `biz_list` / `source`。

**冲突（409）**：同一个用户已有一条活动的手动任务时，`POST /api/settings/run` 不会静默丢第二次请求。
作用域相同则返回那条旧任务（`task_id`/`status`/`scope` 均为旧任务真实值，`202`）；作用域不同则返回 `409`
与 `code: sync.task_conflict`，提示先等它结束或取消。

**冷却（`skip_minutes` 对手动同步同样生效）**：手动触发只跳过“按账号日频次间隔”，不会跳过短冷却——
最近 `skip_minutes` 内同步过的账号仍会被跳过（`skip_reason: recently_synced`），避免每次点按都把源站打一遍。

`group_id` 不属于你、`biz_list` 里有未订阅的号、或 `source` 下没有你的订阅都是 `404`；
显式传空的 `biz_list` 是 `400`；请求体字段未知或类型不对是 `422`。`source` 会把「我订阅的该来源频道」
展开成 `biz_list` 写进任务，任务行上的 `source` 用于按来源筛历史。

### 状态与任务

`GET /api/settings/status` 是**管理员**接口（队列水位与 worker 心跳是全站视角）。响应为封闭模型：

```
GET /api/settings/status?limit=5
→ {
    "status": "idle"|"running"|...,
    "last_started_at": "...", "last_finished_at": "...", "last_ingest_at": "...",
    "last_error": "",
    "history": [ { "started_at": "...", "finished_at": "...", "status": "success",
                   "error": "", "saved": 12, "downloaded": 3, "skipped_accounts": 1,
                   "failed_accounts": 0, "accounts_total": 8, "accounts_done": 8,
                   "current_account": {…}|null }, … ],
    "queue": {
      "articles": { "pending": 5, "processing": 1, "failed": 0, "done": 40 },
      "images":   { "pending": 12, "failed": 2 },
      "media":    { "<source>": { "pending": 0, "processing": 1, "failed": 0, "done": 9 } },
      "failed_items": [ { "biz": "...", "nickname": "...", "sn": "...",
                          "attempts": 3, "retryable": false, "last_error": "...",
                          "updated_at": "..." } ]
    },
    "worker_heartbeat_at": "..."
  }
```

队列分三个固定子对象：`articles` 与每个 `media.<source>` 都是 `pending/processing/failed/done` 四态计数，
`images` 只有 `pending/failed`。`media` 以**来源名**为键，是开放集合（由后端来源注册表决定），所以是动态字典；
`history` 与 `failed_items` 的元素字段固定。

```
GET /api/settings/tasks?limit=5&detail=false   → { "tasks": [ … ] }
GET /api/settings/tasks?source=bilibili        # 只看某个来源的任务
GET /api/settings/tasks/{task_id}             → 任务完整状态（不存在或不是你的任务都是 404）
POST /api/settings/tasks/{task_id}/cancel     → 可取消状态返回更新后的任务，否则 404
```

任务列表只给当前用户自己的任务（管理员看全部）；`detail=false` 返回精简摘要，`detail=true` 才带逐账号进度。
任务状态字段：`task_id`、`status`、`trigger_type`（`manual`/`scheduled`/`backfill`）、`phase`、
`accounts_total`、`accounts_done`、`current_account`、`current_article`、`last_log`、`report`、`error`、
`created_at`/`started_at`/`finished_at`，`detail=false` 时返回精简摘要。取消是协作式的：worker 在下一个检查点停下。

### 测试邮件

```
POST /api/settings/test-email { "to_email": "me@example.com", "email": { "smtp_host": "…" } }
```

不给 `to_email` 时回落到配置里的 `alert_email`；两者都空则 `400`。传 `email` 对象则用这份临时 SMTP 配置发送
而不落库；`email` 里的 `smtp_password` 同样是只写的，放回回显掩码会被丢弃。成功为 `200` 与
`{"status": "sent", "to_email": "…"}`；`smtp_port` 非法或字段未知是 `422`；邮件服务器侧失败是 `502`，超时是 `504`。

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

### 来源概览

```
GET /api/admin/source
→ { "sources": [ { "source": "bilibili", "channels": 3, "enabled_channels": 3,
                   "followers": 1, "last_synced_at": "...",
                   "credential": { "field": "bilibili_cookie", "configured": true },
                   "proxy": "direct",
                   "backlog": { "pending": 5, "processing": 1, "failed": 0, "done": 40 } } ],
    "media_enabled": true }
```

每个来源的目录规模、凭证是否配置、生效的代理与 `media_job` 积压。来源清单由后端给出
（`hippo/sources/registry.py` 的 `source_names()`），前端不维护副本；不收凭证的来源
`credential` 为 `null`，没有被配置代理的来源 `proxy` 为解析后的默认值。

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
`email_verified` 缺省为 `true`：管理员手工建号即视为可信，且这一路径不发验证邮件；
传 `false` 可显式建一个待验证账号（此时该账号若有邮箱则无法登录，需用 `PATCH /api/admin/user/{id}` 置回）。

### 审计与日志

```
GET /api/admin/audit?action=auth.login&user_id=1&since=2026-05-01&until=2026-05-31&page=1&page_size=50
→ { "items": [ … ], "page": 1, "page_size": 50, "total": 123, "pages": 3 }
```

`page_size` 上限 200。`GET /api/admin/log/tail?lines=500`（上限 5000）读 `HIPPO_LOG_FILE` 指向的文件，
未配置时返回 `{"available": false, "reason": "…", "lines": []}`（同样是 `200`）。
`GET /api/admin/log/link?minutes=60` 未配置 `HIPPO_SIGNOZ_URL` 时返回 `{"available": false, "reason": "…"}`。

## 13. 媒体来源与转写 `/api/source` `/api/media`

B站视频、YouTube 视频与 RSS 播客都是「媒体来源」：它们的文章行建立时只有元数据，正文由 `media_job`
队列转写而来（见 README「数据来源」）。凭证与代理在管理台「来源」页按源配置，跨源的转写参数在「转写」页。

### 列出已订阅的来源

```
GET /api/source
```

只统计调用者**订阅的**来源，`channels` 是该来源下的频道数。左栏的「来源」列表用它。

```json
{"sources": [{"source": "bilibili", "channels": 3}, {"source": "weixin", "channels": 264}]}
```

### 解析一个链接

```
GET /api/source/preview?url=https://space.bilibili.com/946974/video
```

| 参数 | 类型 | 说明 |
|---|---|---|
| `url` | str | 视频 / 频道 / 订阅地址。来源按域名判断，其余 http(s) 一律当 feed 试 |

不写任何东西，只回答「这是什么」。`subscribed` 表示目录里已经有了。

```json
{"source": "bilibili", "biz": "bilibili:946974", "nickname": "影视飓风", "avatar_url": null, "subscribed": false}
```

| 状态码 | 何时 |
|---|---|
| `400` | 不是 http(s) 地址 / 媒体来源未启用 / 该来源不接受这个链接 |
| `502` | 上游失败（B站风控、feed 解析不出来） |

### 订阅

```
POST /api/source/channel
```

| 字段 | 类型 | 说明 |
|---|---|---|
| `url` | str | 必填，同上 |
| `group_id` | int | 可选，默认分组 |

建账号 + 给调用者建订阅 + 投递一轮列表同步（`202`）。

```json
{"biz": "bilibili:946974", "nickname": "影视飓风", "source": "bilibili", "group_id": 1}
```

### 我的来源凭证

B站投稿列表接口有风控，需要 SESSDATA。**实例级**的在管理台「来源」页设，**用户级**的在这里设；
界面只回答「是否已配置」，永不回显明文。

后台同步按「该频道的订阅者各自的凭证（最近更新的先试）→ 实例凭证」的顺序尝试，
只有凭证被拒（B站风控 / YouTube 要求登录）才换下一份；用户级凭证仅用于自己触发的操作时，
走的是同一份优先级。

```
GET    /api/source/credential                     -> {"bilibili": {"configured": true}}
PUT    /api/source/credential/bilibili             {"secret": "SESSDATA=…"}  -> 204
DELETE /api/source/credential/bilibili             -> 204
POST   /api/source/credential/bilibili/test[?scope=user|instance] -> 探测是否可用
```

`POST …/test` 只回答凭证能不能用，不回显凭证本身：返回 `{source, credential, ok, detail, account}`，
`credential` 是 `user`/`instance`/`none`（哪一份被采用），`account` 是它被接受为的账号。
默认 `scope=user`（自己的凭证，没有再退到实例凭证），`scope=instance` 只有管理员能用（否则 `403`）。
`scope` 不是 `user`/`instance` 为 `422`；不支持的来源为 `400`。

### 留档媒体

```
GET /api/media/{article_pk}/{kind}        # kind = audio | video
```

`audio` 与 `video` 共用同一套订阅鉴权与 Range 语义。`200`（或带 `Range` 时的 `206`，浏览器拖动进度条
靠它）。没有留档时 `302` 到原始地址，既没有留档也没有原始地址时 `404`，范围越界 `416`。

### 转写稿

```
GET  /api/article/{article_id}/transcript?format=md|txt|srt
POST /api/article/{article_id}/transcribe[?force_asr=true]
```

`format=md` 每段前带 `**[mm:ss]**` 锚点，`srt` 输出标准字幕。文章没有段落时 `404`。
`transcribe` 把该文章已有的转写任务重新入队（`202`）；`force_asr=true` 让解析器**跳过现成字幕**
（CC 字幕、播客转录稿）改用 ASR 重转，用于字幕质量不如模型的情况。

### 管理台设置

```
GET   /api/admin/media          当前设置（API Key 与 Cookie 只回显末 4 位）
PATCH /api/admin/media          合并更新；提交回显的掩码不会被写回
POST  /api/admin/media/test     逐档探测 ASR 端点可达性
```

## 14. 公开 API Key `/api/apikey`

给无法持 Cookie 的脚本与阅读器用。密钥只在**创建时**返回一次明文，库里存 sha256 与前缀，
所以列表接口只能显示 `hippo_a1b2c3d4…`。

### 认证与边界

`Authorization: Bearer <key>` 或 `X-API-Key: <key>`。所有只读端点都接受它（认证解析在
`current_user` 依赖里完成一处，路由无需逐个改动）。

| 限制 | 说明 |
|---|---|
| 管理接口 | `/api/admin`、`/api/settings`（及 `/api/settings/*`）、`/api/apikey`、`/api/auth`、`/api/login`、`/api/source/credential` 一律 `403`——一个泄露的 key 不能给自己签一个更大的 |
| 写操作 | 非 `GET`/`HEAD` 需要 `write` scope，当前控制台只签发 `read` |
| 使用记录 | `last_used_at` 每 5 分钟最多写一次，避免每次读都变成一次写 |

> 集成注记：上表是目标契约。基线里 `require_admin` 只检查角色，若某一管理员用户的 API Key 被带到
> `/api/settings`，可能仍被放行；让 `require_admin` 也拒 API Key 的改动在核心 writer 一侧，本文档
> 按集成后的契约写。

### 端点

```
GET    /api/apikey                     我的密钥列表（不含明文）
POST   /api/apikey                     创建，唯一的明文在这里
DELETE /api/apikey/{key_id}            吊销
GET    /api/whoami                     这个凭证是什么
```

`POST` 接受 `name`、`scopes`（默认 `["read"]`）、`expires_in_days`（不传则不过期）：

```json
{"id": 1, "name": "CI", "prefix": "a1b2c3d4", "scopes": ["read"], "secret": "hippo_…"}
```

`whoami` 同时适用于会话与密钥，脚本可以先探一下再干活：

```json
{"via": "api_key", "user": {"id": 1, "username": "admin", "role": "admin"}, "scopes": ["read"], "key": {"id": 1, "name": "CI"}}
```

```bash
curl -H "Authorization: Bearer hippo_…" "https://hippo.example/api/article?source=podcast&page_size=5"
```

## 15. 私有知识库 `/api/knowledge-base` `/api/document`

按用户隔离的私有 PDF/TXT 集合。**没有管理员特权**：每条记录都以 `current_user` 为唯一归属来源，
别人的知识库与文档一律 `404`（而不是 `403`），id 不合法、不存在、已 tombstone 也一样。

| 方法 | 路径 | 权限 | 用途 |
|---|---|---|---|
| GET | `/api/knowledge-base` | 登录 | 列出我的知识库 |
| POST | `/api/knowledge-base` | 登录 | 新建知识库 |
| GET | `/api/knowledge-base/{knowledge_base_id}` | 登录 | 知识库详情 |
| PATCH | `/api/knowledge-base/{knowledge_base_id}` | 登录 | 重命名 |
| DELETE | `/api/knowledge-base/{knowledge_base_id}` | 登录 | 删除（tombstone），`204` |
| GET | `/api/knowledge-base/{knowledge_base_id}/document` | 登录 | 列出该库的文档 |
| POST | `/api/knowledge-base/{knowledge_base_id}/document` | 登录 | 登记文档元数据，`201` |
| GET | `/api/document/{document_id}` | 登录 | 文档详情（含 `state`）；未知/他人/已删均 `404` |
| DELETE | `/api/document/{document_id}` | 登录 | 删除（tombstone），`204` |
| PUT | `/api/document/{document_id}/file` | 登录 | 上传原始文件，`202` |
| POST | `/api/document/{document_id}/retry` | 登录 | 准备重试一个 `failed` 文档 |
| GET | `/api/document/{document_id}/content` | 登录 | 读已解析文本的分页块 |
| GET | `/api/knowledge-base/{knowledge_base_id}/search` | 登录 | 在该库的已就绪文本内全文检索 |

### 公开字段

所有响应都是**白名单投影**，不会出现对象键、内容哈希、租约或归属 id：

| 对象 | 字段 |
|---|---|
| 知识库 | `id`、`name`、`created_at`、`updated_at` |
| 文档 | `id`、`knowledge_base_id`、`filename`、`mime_type`、`size_bytes`、`state`、`attempts`、`error_code`、`error_message`、`page_count`、`version`、`created_at`、`updated_at` |
| 文本块 | `page_no`、`ordinal`、`text`、`chunk_type`、`heading_level`（不含内部 `document_id`） |
| 搜索命中 | `document_id`、`knowledge_base_id`、`filename`、`version`、`page_no`、`ordinal`、`text`、`chunk_type`、`heading_level` |

### 状态与上传

`state` 的取值是 `pending_upload` → `uploading` → `queued` → `processing` → `ready`，失败时为 `failed`。
只有 `ready` 的文档才有文本块，也只有 `ready` 的文档/库能被 `/search` 命中。

| 方法 | 路径 | 请求体 |
|---|---|---|
| `POST /api/knowledge-base` | | `{"name": "研究报告"}` |
| `POST /api/knowledge-base/{kb}/document` | | `{"filename": "report.pdf", "mime_type": "application/pdf"}` |
| `PUT /api/document/{id}/file` | | 原始文件字节 |

`filename` 与 `mime_type` 都必填，`mime_type` 只接受规范拼写的 `application/pdf` 或 `text/plain`，
其他值是 `415`；两个端点都拒绝未知字段（`422`）。上传端点的请求体就是**原始字节**（不是 multipart）：

```http
PUT /api/document/00000000-0000-0000-0000-000000000000/file
Content-Type: application/pdf

<PDF 原始字节>
```

`Content-Type` 只接受缺失、`application/octet-stream` 或该文档自身的规范类型；`Content-Encoding` 必须是
`identity`，压缩体会被拒。服务端统计实际收到的字节数，不信任 `Content-Length`。

`202` 只表示**上传已受理**，不代表 `ready`：返回的文档 `state` 一般是 `queued`，客户端随后用
`GET /api/document/{id}` 轮询。`state=ready` 之后才读 `/content`，否则 `409`。

### 分页与限额

| 端点 | 参数 | 默认 | 上限 |
|---|---|---|---|
| 列表类（知识库、文档） | `limit` / `offset` | `50` / `0` | `limit` ≤ `200`，`offset` 为 int8 范围 |
| `/content` | `limit` / `offset` / `page_no` | `100` / `0` / 无 | `limit` ≤ `100`，`page_no` ≤ `1000` |
| `/search` | `q` / `limit` / `offset` / `document_id` | — / `20` / `0` / 无 | `limit` ≤ `100`，`q` ≤ 500 字符 |

列表返回 `{..., "limit": ..., "offset": ...}`；`/content` 与 `/search` 在**确实还有下一行**时才给
`next_offset`，正好满一页不会被误报为还有后续。`q` 去空白后为空是 `400`；`document_id` 必须是同一知识库
内、属于调用者且已 `ready` 的文档，否则 `404`（未就绪 `409`）。搜索返回的文本是**原文**（不转义 HTML，
也不做高亮），`/search` 只接受 `GET`，是只读查询。

单文件上限 **64 MiB**，PDF 最多 **1000 页**，文本最多 **2,000,000 字符**，总计最多 **50,000 块**，单块最多
**4,000 字符**；超限直接失败并返回错误码，**不会静默截断**。解析只提取已有文本层，不做 OCR、向量化或 AI
处理；扫描件以 `document.needs_ocr` 失败。

### 错误

错误体仍是统一的 `{"error": "…"}`，`error` 旁边可能带一个稳定的 `code`：

| 状态码 | 何时 |
|---|---|
| `400` | 名称或文件名非法、`Content-Length` 非法、`q` 为空等业务校验 |
| `404` | 知识库/文档不存在，或存在但不属于当前用户 |
| `409` | 状态冲突：重试非 `failed`、上传非 `pending_upload`、读 `/content` 时未 `ready`、同库同内容重复 |
| `413` | 单文件超过 64 MiB |
| `415` | MIME 类型不在允许列表，或请求体被压缩 |
| `422` | 字段缺失/未知、类型不对等请求形状错误（`code: request.invalid`）|
| `408` | 读取请求体超过 120s 预算 |
| `429` | 同一 app 已有 2 个上传在进行（原始上传与重试共用这个闸门） |
| `502` | 对象存储传输失败 |
| `503` | 结果无法确认（提交结果未知） |

### 重试

`POST /api/document/{id}/retry` 只接受 `failed` 文档，**没有请求体**，也**不会重新上传任何字节**：
解析失败回到 `queued`（源对象保留），上传失败清除该次尝试的对象后回到 `pending_upload`（客户端必须
重新发文件）。响应 `200` 表示「重试已准备好」，不是「已重新上传」。

### 鉴权与 Key

13 个路由全部要求**登录**（服务端会话 Cookie）。公开 API Key 同样可以读它们（只读端点在
`current_user` 依赖里统一解析），但非 `GET`/`HEAD` 需要 `write` scope——当前控制台只签发 `read`，
所以**只读 Key 不能创建、上传、重试或删除**任何知识库资源。

### 删除语义

`DELETE` 只写 tombstone：行不再可见、不再出现在任何列表或搜索结果里；对象存储中的原始文件是**分开的**
后续清理动作。同样地，知识库目前只提供「解析后的文本阅读」，没有原 PDF 画布与文档标注。

以上示例里的 id 均为合成占位符，不代表任何真实数据。

## 附录 A：端点总览

| 方法 | 路径 | 权限 |
|---|---|---|
| GET | `/api/account` | 登录 |
| POST | `/api/account` | 登录 |
| POST | `/api/account/batch` | 登录 |
| POST | `/api/account/move` | 登录 |
| GET | `/api/account/search` | 登录 |
| GET | `/api/account/search/{biz}/avatar` | 登录 |
| DELETE | `/api/account/{biz}` | 登录 |
| GET | `/api/account/{biz}` | 登录 |
| PATCH | `/api/account/{biz}` | 登录 |
| GET | `/api/account/{biz}/avatar` | 登录 |
| GET | `/api/admin/source` | 管理员 |
| GET | `/api/admin/audit` | 管理员 |
| GET | `/api/admin/audit/action` | 管理员 |
| GET | `/api/admin/log/link` | 管理员 |
| GET | `/api/admin/log/tail` | 管理员 |
| GET | `/api/admin/media` | 管理员 |
| PATCH | `/api/admin/media` | 管理员 |
| POST | `/api/admin/media/test` | 管理员 |
| GET | `/api/admin/site` | 管理员 |
| PATCH | `/api/admin/site` | 管理员 |
| GET | `/api/admin/user` | 管理员 |
| POST | `/api/admin/user` | 管理员 |
| PATCH | `/api/admin/user/{user_id}` | 管理员 |
| POST | `/api/admin/user/{user_id}/password` | 管理员 |
| DELETE | `/api/admin/user/{user_id}/session` | 管理员 |
| GET | `/api/apikey` | 登录 |
| POST | `/api/apikey` | 登录 |
| DELETE | `/api/apikey/{key_id}` | 登录 |
| GET | `/api/article` | 登录 |
| GET | `/api/article/refetch/{task_id}` | 登录 |
| GET | `/api/article/{article_id}` | 登录 |
| GET | `/api/article/{article_id}/annotation` | 登录 |
| POST | `/api/article/{article_id}/annotation` | 登录 |
| DELETE | `/api/article/{article_id}/annotation/{annotation_id}` | 登录 |
| GET | `/api/article/{article_id}/image` | 登录 |
| PUT | `/api/article/{article_id}/read` | 登录 |
| POST | `/api/article/{article_id}/refetch` | 登录 |
| PUT | `/api/article/{article_id}/star` | 登录 |
| POST | `/api/article/{article_id}/transcribe` | 登录 |
| GET | `/api/article/{article_id}/transcript` | 登录 |
| PATCH | `/api/auth/email` | 登录 |
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
| DELETE | `/api/chat/session/{session_id}` | 登录 |
| GET | `/api/chat/session/{session_id}` | 登录 |
| POST | `/api/chat/session/{session_id}/message` | 登录 |
| GET | `/api/chat/session/{session_id}/preset/{preset}` | 登录 |
| POST | `/api/chat/session/{session_id}/rename` | 登录 |
| GET | `/api/feed/mixed` | 登录或 `token` |
| GET | `/api/group` | 登录 |
| POST | `/api/group` | 登录 |
| DELETE | `/api/group/{group_id}` | 登录 |
| GET | `/api/group/{group_id}` | 登录 |
| PATCH | `/api/group/{group_id}` | 登录 |
| GET | `/api/image/{image_id}` | 登录 |
| POST | `/api/image/{image_id}/block` | 登录 |
| GET | `/api/knowledge-base` | 登录 |
| POST | `/api/knowledge-base` | 登录 |
| GET | `/api/knowledge-base/{knowledge_base_id}/document` | 登录 |
| POST | `/api/knowledge-base/{knowledge_base_id}/document` | 登录 |
| GET | `/api/knowledge-base/{knowledge_base_id}/search` | 登录 |
| DELETE | `/api/knowledge-base/{knowledge_base_id}` | 登录 |
| GET | `/api/knowledge-base/{knowledge_base_id}` | 登录 |
| PATCH | `/api/knowledge-base/{knowledge_base_id}` | 登录 |
| GET | `/api/document/{document_id}` | 登录 |
| DELETE | `/api/document/{document_id}` | 登录 |
| GET | `/api/document/{document_id}/content` | 登录 |
| PUT | `/api/document/{document_id}/file` | 登录 |
| POST | `/api/document/{document_id}/retry` | 登录 |
| GET | `/api/llm/provider` | 管理员 |
| POST | `/api/llm/provider` | 管理员 |
| DELETE | `/api/llm/provider/{provider_id}` | 管理员 |
| PATCH | `/api/llm/provider/{provider_id}` | 管理员 |
| POST | `/api/llm/provider/{provider_id}/test` | 管理员 |
| GET | `/api/login` | 登录 |
| POST | `/api/login/auto` | 登录 |
| POST | `/api/login/qr` | 登录 |
| POST | `/api/login/wait` | 登录 |
| GET | `/api/media/{article_pk}/{kind}` | 登录 |
| GET | `/api/report/setting` | 登录 |
| PATCH | `/api/report/setting` | 登录 |
| GET | `/api/report/{date}` | 登录 |
| GET | `/api/report/{date}/html` | 登录 |
| POST | `/api/report/{date}/send` | 登录 |
| GET | `/api/settings` | 管理员 |
| PATCH | `/api/settings` | 管理员 |
| POST | `/api/settings/admin/run` | 管理员 |
| GET | `/api/settings/preferences` | 登录 |
| PATCH | `/api/settings/preferences` | 登录 |
| POST | `/api/settings/run` | 登录 |
| GET | `/api/settings/status` | 登录 |
| GET | `/api/settings/tasks` | 管理员 |
| GET | `/api/settings/tasks/{task_id}` | 管理员 |
| POST | `/api/settings/tasks/{task_id}/cancel` | 管理员 |
| POST | `/api/settings/test-email` | 管理员 |
| GET | `/api/source` | 登录 |
| POST | `/api/source/channel` | 登录 |
| GET | `/api/source/credential` | 登录 |
| DELETE | `/api/source/credential/{source}` | 登录 |
| PUT | `/api/source/credential/{source}` | 登录 |
| POST | `/api/source/credential/{source}/test` | 登录 |
| GET | `/api/source/preview` | 登录 |
| GET | `/api/whoami` | 登录 / API Key |

## 附录 B：状态码

| 状态码 | 何时出现 |
|---|---|
| `200` | 成功 |
| `201` | 创建成功（分组、公众号、标注、用户、LLM Provider、会话） |
| `202` | 已受理：注册、找回密码、手动同步、日报发送、重新抓取 |
| `204` | 成功且无响应体（登录、注销、删除、更新类操作） |
| `400` | 业务参数非法（密码太短、`send_hour` 越界、日期/排序非法、显式空 `biz_list` 等） |
| `401` | 未登录或会话过期 |
| `422` | 请求形状错误：未知字段、类型不对、缺必填字段、超出取值范围（`code: request.invalid`） |
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
