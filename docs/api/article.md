# 文章 API 参考

## 文章列表

```
GET /api/article
```

查询参数：

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `page` | int | 否 | 页码，从 1 开始，默认 `1` |
| `page_size` | int | 否 | 每页条数，默认 `20`，最大 `200` |
| `q` | str | 否 | 全文搜索关键词（标题+正文+作者），基于 `pg_jieba` 中文分词 |
| `sort` | str | 否 | 排序方式：`publish_at_desc`（按发布时间）、`relevance_desc`（按相关度，仅 `q` 非空时生效）。默认：有查询时按相关度，否则按发布时间 |
| `group_id` | int | 否 | 按公众号分组筛选 |
| `biz` | str | 否 | 按公众号 biz 筛选 |
| `item_show_type` | int | 否 | 按文章版式类型筛选：`0`(图文)、`5`(视频)、`6`(直播)、`7`(专辑)、`8`(话题)、`10`(纯视频)、`11`(图片消息)、`17`(付费) |
| `article_id` | str | 否 | 按微信原始 article_id 精确匹配 |
| `since` | str | 否 | 起始日期（ISO 格式，如 `2024-01-01` 或 `2024-01-01T00:00:00`） |
| `until` | str | 否 | 结束日期（ISO 格式） |
| `exclude_keywords` | str | 否 | 排除关键词（逗号/分号/换行分隔），命中标题/摘要/作者时过滤。不传则使用全局设置 `article_exclude_keywords` |
| `content` | str | 否 | 若为 `"1"` / `"true"` / `"yes"`，在返回数据中同时包含文章正文内容（目前该参数已内置处理，通过 API 直接使用即可） |

响应格式：

```json
{
  "articles": [
    {
      "id": 1,
      "biz": "MzIxMjM0NTY3OA==",
      "article_id": "MjM5MTA1MjI4MA==",
      "title": "文章标题",
      "item_show_type": 0,
      "author": "作者名",
      "digest": "文章摘要",
      "cover": "https://mmbiz.qpic.cn/...",
      "link": "https://mp.weixin.qq.com/s/...",
      "source_url": "https://...",
      "publish_at": 1700000000,
      "created_at": 1700000000,
      "account_nickname": "公众号名称",
      "account_alias": "公众号别名",
      "account_avatar": "https://mmbiz.qpic.cn/...",
      "account_avatar_url": "/api/account/MzIxMjM0NTY3OA==/avatar",
      "group_id": 1,
      "group_name": "分组名称",
      "image_id": 42
    }
  ],
  "page": 1,
  "page_size": 20,
  "total": 150,
  "item_show_type_facets": [
    { "item_show_type": 0, "count": 120 },
    { "item_show_type": 5, "count": 30 }
  ]
}
```

返回字段说明：

| 字段 | 类型 | 说明 |
|------|------|------|
| `articles[].id` | int | 文章主键 ID（用于详情接口） |
| `articles[].biz` | str | 公众号唯一标识 |
| `articles[].article_id` | str | 微信原始文章 ID |
| `articles[].title` | str | 文章标题 |
| `articles[].item_show_type` | int | 版式类型，`null` 时返回 `0` |
| `articles[].author` | str | 作者 |
| `articles[].digest` | str | 摘要 |
| `articles[].cover` | str | 封面图 URL |
| `articles[].link` | str | 文章链接 |
| `articles[].source_url` | str | 原文链接 |
| `articles[].publish_at` | int | 发布时间（Unix 时间戳） |
| `articles[].created_at` | int | 入库时间（Unix 时间戳） |
| `articles[].account_nickname` | str | 公众号昵称 |
| `articles[].account_alias` | str | 公众号别名 |
| `articles[].account_avatar` | str | 公众号头像 URL |
| `articles[].account_avatar_url` | str | 头像代理 API 路径 |
| `articles[].group_id` | int | 分组 ID |
| `articles[].group_name` | str | 分组名称 |
| `articles[].image_id` | int | 首张图片 ID（可用作缩略图，若无图片则为 `null`） |
| `total` | int | 符合筛选条件的文章总数 |
| `item_show_type_facets` | array | 版式类型分布统计，用于前端分面筛选 |

---

## 文章详情

```
GET /api/article/{id}
```

路径参数：

| 参数 | 类型 | 说明 |
|------|------|------|
| `id` | int | 文章主键 ID（非微信原始 article_id） |

响应格式：

```json
{
  "article": {
    "id": 1,
    "biz": "MzIxMjM0NTY3OA==",
    "article_id": "MjM5MTA1MjI4MA==",
    "title": "文章标题",
    "item_show_type": 0,
    "author": "作者名",
    "digest": "文章摘要",
    "cover": "https://mmbiz.qpic.cn/...",
    "link": "https://mp.weixin.qq.com/s/...",
    "source_url": "https://...",
    "publish_at": 1700000000,
    "created_at": 1700000000,
    "account_nickname": "公众号名称",
    "account_alias": "公众号别名",
    "account_avatar": "https://mmbiz.qpic.cn/...",
    "account_avatar_url": "/api/account/MzIxMjM0NTY3OA==/avatar",
    "group_id": 1,
    "group_name": "分组名称"
  },
  "content": [
    {
      "type": "text",
      "text": "段落文本内容"
    },
    {
      "type": "image",
      "image_id": 42,
      "url": "https://mmbiz.qpic.cn/..."
    },
    {
      "type": "image",
      "image_id": 43,
      "url": "https://mmbiz.qpic.cn/..."
    }
  ],
  "content_status": "ok",
  "content_updated_at": "2024-01-01T00:00:00+00:00",
  "images": [
    {
      "id": 42,
      "article_pk": 1,
      "url": "https://mmbiz.qpic.cn/...",
      "kind": "cover",
      "kind_v2": "cover",
      "position": 0,
      "width": 800,
      "height": 600,
      "file_size": 123456,
      "content_type": "image/jpeg",
      "content_hash": "sha256:abc123...",
      "s3_key": "images/abc123.jpg",
      "s3_bucket": "hippo-images",
      "s3_region": "us-east-1",
      "failed_reason": null,
      "created_at": 1700000000
    }
  ]
}
```

响应字段说明：

| 字段 | 类型 | 说明 |
|------|------|------|
| `article` | object | 文章元数据，结构与列表接口中的单条记录一致 |
| `content` | array | 正文结构化内容块（JSON blocks），从 `article_content` 表读取；内容可能包含 `type: "text"` 文本块和 `type: "image"` 图片块。已屏蔽的图片块会被自动过滤 |
| `content_status` | string | 正文状态：`"ok"`（正常）、`"missing"`（未下载）、`"invalid"`（解析失败）、`"empty"`（空内容） |
| `content_updated_at` | str | 正文内容最近更新时间（ISO 格式） |
| `images` | array | 文章关联图片元数据列表（已过滤被屏蔽的图片） |

图片元数据字段说明：

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | int | 图片 ID（用于 `/api/image/{id}` 获取内容） |
| `url` | str | 图片源 URL |
| `kind` | str | 图片类型（`"cover"` 封面图、`"body"` 正文图等） |
| `kind_v2` | str | 新版图片类型标注 |
| `position` | int | 图片在文章中的顺序 |
| `width` / `height` | int | 图片尺寸（像素） |
| `file_size` | int | 文件大小（字节） |
| `content_type` | str | MIME 类型 |
| `content_hash` | str | 内容哈希值（格式 `算法:哈希值`，如 `sha256:abc...`） |
| `s3_key` | str | S3 对象键（若已上传至 S3） |
| `failed_reason` | str | 下载失败原因（若下载失败） |
| `created_at` | int | 入库时间（Unix 时间戳） |

---

## 相关接口

| 端点 | 说明 |
|------|------|
| `GET /api/article/{id}/image` | 仅获取文章图片列表 |
| `GET /api/image/{id}` | 获取图片文件内容（二进制，支持 S3/源站回源） |
| `POST /api/image/{id}/block` | 屏蔽某张图片（按内容哈希全局屏蔽） |
| `POST /api/article/{id}/refetch` | 触发文章内容重新下载 |
| `GET /api/article/refetch/{task_id}` | 轮询重新下载任务状态 |
| `GET /api/feed/mixed` | 混合 Feed（支持 RSS 格式输出） |

---

## 错误处理

所有接口在出错时返回以下格式：

```json
{
  "detail": "错误描述信息"
}
```

常见 HTTP 状态码：

| 状态码 | 说明 |
|--------|------|
| `200` | 成功 |
| `400` | 参数错误（如无效的 `sort`、`item_show_type`、日期格式等） |
| `404` | 文章/图片不存在 |
| `502` | 图片回源获取失败 |

---

## 使用示例

```bash
# 获取第 1 页文章，每页 20 条
curl 'http://localhost:8000/api/article'

# 搜索包含"机器学习"的文章，按相关度排序
curl 'http://localhost:8000/api/article?q=机器学习'

# 筛选某个分组的纯视频文章
curl 'http://localhost:8000/api/article?group_id=2&item_show_type=10'

# 按时间范围筛选
curl 'http://localhost:8000/api/article?since=2024-01-01&until=2024-06-30'

# 获取文章详情
curl 'http://localhost:8000/api/article/42'
```
