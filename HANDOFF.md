# Hippo 私有知识库交接文档

## 1. 当前结论

私有知识库的核心代码闭环已在**隔离整合候选**完成离线验证，尚未应用到主仓库工作区、迁移生产数据库或部署。

已实现：私有库和文档管理、PDF/TXT 批量上传、逐文件进度、导入处理、失败重试、processing 心跳与过期回收、分页正文、全文搜索接口、页段与版本引用、前端知识库页面和导航。

**没有完成：**真实 PostgreSQL / `pg_jieba` / S3 验证、生产端到端与浏览器/真机验收、过期 `uploading` 的安全恢复、文档标注、跨文档 AI 问答、原版 PDF 画布与原文件下载入口。不能将本轮结果宣传为完整需求已上线。

## 2. 最新用户授权与不可越过的边界

用户最新指令：

> 可以在线上测试，当前生产环境没有人用。只要你不删数据就行。先写一份 handoff 文档。

执行约定：

- **本次先写交接文档，不执行任何线上连接、部署、迁移、容器或服务命令。**
- 后续允许在生产环境开展测试；“当前无人使用”是用户提供的信息，不等于数据库为空，也不是可重建生产库的授权。
- **不得删除既有数据。**禁止清库、重建现有库、`DROP TABLE`、`TRUNCATE`、业务 `DELETE`、覆盖现有对象、批量删除 S3 对象，以及通过软删除接口让既有数据不可访问。
- 新增测试数据也**默认保留，不做自动清理**。如果要删除新建测试记录、对象或测试数据库，先取得明确授权。此前“测试后清理”的临时容器方案不应直接沿用到生产环境。
- 删除 API、重复上传去重、上传失败重试、故障注入等路径可能自动调用 S3 删除，不能因为目标是测试文件就默认执行。先审代码及权限边界。
- 优先在生产主机旁路使用独立测试数据库/测试桶或专用命名空间；不以线上测试为由操作其他应用、数据库或桶。
- 测试数据使用专用用户/知识库和明确前缀，例如 `kb-acceptance-<unique-id>`，只上传合成 PDF/TXT，不复制真实文章或私有文档作为 fixture。
- 不把 DSN、密码、Cookie、API Key、S3 密钥写入此文档、聊天、命令历史或测试输出。配置访问应最小化，不打印完整 `config.toml` 或敏感日志。
- 代码实施继续使用 **DeepSeek 子代理**；父侧负责读取结果、验证、范围仲裁与安全集成。未经用户要求，不暂存、提交、推送或部署。

## 3. 关键位置

| 内容 | 路径/状态 |
|---|---|
| 主仓库 | `/home/sunnysab/Code/hippo` |
| 最新整合候选 | `/home/sunnysab/Code/worktrees/hippo/knowledge_integration_z0neswrc` |
| 整合记录 | `/tmp/hippo_knowledge_integration_MDvuHs` |
| 最终离线结果 | `/tmp/hippo_knowledge_integration_MDvuHs/final-offline-result.md` |
| 最终候选指纹 | `/tmp/hippo_knowledge_integration_MDvuHs/final-candidate-sha256.json` |
| 后端最终测试日志 | `/tmp/hippo_knowledge_integration_MDvuHs/final-offline-tests.log` |
| 同候选前端测试日志 | `/tmp/hippo_knowledge_integration_MDvuHs/frontend-tests.log` |
| 前端原型/竞态审查记录 | `/tmp/hippo_knowledge_ui_parts_A9C2ak` |
| 离线 Python 测试包装器 | `/tmp/hippo-source-fixes-NmGxDV/pytest-offline.py` |
| 已知部署目标 | `sab@archlinux30`，目录 `/home/sab/hippo`（来自项目指引，本轮未连接核实） |

主仓库当前 HEAD：`e5c23efa4ad8fd8d7cc160ddfea34e73303aadb1`。

- 本地主仓库 `SCHEMA_VERSION = '28'`。
- 隔离候选 `SCHEMA_VERSION = '29'`。
- **生产代码、真实库版本、服务状态及备份状态尚未核实，不得用本地版本推断生产版本。**

`/tmp` 和子代理产物有保留期；不要把它们当永久归档。工作树中有未提交代码，依赖包也存在指向已有安装的链接，**不要先清理工作树或依赖目录**。需要长期归档时先核对并保存候选、补丁与指纹，不要以清理为由删除现场。

## 4. 验证结果及其准确含义

同一隔离候选的父侧结果：

- 后端：**1115 passed、4 skipped、48 subtests passed**。
- 前端：**288 tests passed，44 个测试文件**。
- `ruff check hippo tests --no-cache` 通过。
- ESLint（`--max-warnings 0`）、TypeScript、Vite 生产构建通过。
- `git diff --check` 通过。
- 35 个已验前端/后端新增文件先按指纹复制；4 个整合胶水文件定点适配；后续包装与日志改动也已有测试。
- 已核对：原 schema 为追加段前缀保留，storage 生命周期函数 AST 未改；worker 除 `main()` 日志初始化外的处理阶段 AST 未变。

边界：

- Python 测试阻止外部 socket/DNS，仅允许既有数字 loopback fixture。
- 仓储测试主要使用真实执行 SQL 的 SQLite 适配器，**不证明 PostgreSQL 行锁、EPQ、GIN 或生成列行为**。
- 搜索 API 测试明确 mock PostgreSQL FTS 调用；没有用 `LIKE` 假装中文分词。中文命中、相关度和实际查询计划未验证。
- 对象存储主要使用 fake SDK；没有真实 S3 上传、分片、删除或匿名访问测试。
- 前端使用 Vitest/RTL/jsdom；没有真实浏览器、移动端触控或整 HTTP 应用联调。
- 旧页面仍有 React `act`、Router future flag 等预存测试提示；新知识库测试自身的 Toast/Reader 告警已收尾。不能声称全部运行时告警为零。

可在候选中重复离线验证（以下是说明，不是本次已执行的线上步骤）：

```bash
cd /home/sunnysab/Code/worktrees/hippo/knowledge_integration_z0neswrc

PYTHONPATH="$PWD" timeout 120s /home/sunnysab/Code/hippo/.venv/bin/python \
  /tmp/hippo-source-fixes-NmGxDV/pytest-offline.py \
  -q --tb=short -p no:cacheprovider

timeout 30s /home/sunnysab/Code/hippo/.venv/bin/ruff check hippo tests --no-cache

cd frontend
timeout 45s npm run lint
timeout 90s npm run test
timeout 90s npm run build
```

若临时包装器已不存在，先恢复其准确内容或采用等效网络隔离，不能直接运行可能触网的测试并声称是同一验证环境。不要为重跑而自动安装依赖。

## 5. 核心实现与入口

### 后端

- `hippo/repositories/knowledge.py`：owner 范围查询、上传 CAS、导入领取/完成/失败/重试、processing 续租与过期回收、正文快照与搜索 SQL。
- `hippo/knowledge/parse.py`：PDF/TXT 解析，页段信息、编码识别、资源限额。
- `hippo/knowledge/store.py`：源对象上传、精确单 key 删除、有界下载及 SHA/大小复核。
- `hippo/knowledge/upload.py`：raw 流接收、attempt key/token journal、提交不确定性与取消收尾。
- `hippo/knowledge/retry.py`：上传失败与解析失败分流重试。
- `hippo/knowledge/worker.py`：领取→下载→解析→原子发布，独立心跳与回收循环。
- `hippo/api/routers/knowledge.py`：13 个接口，公开字段白名单。

三个运行入口：

```text
python -m hippo.server
python -m hippo.sync_worker
python -m hippo.knowledge.worker
```

知识导入 worker 不依赖微信 daemon，不应为 PDF/TXT 导入启动微信同步。其 `main()` 已复用 `hippo.logger.setup_logger`，service 名为 `hippo-knowledge-worker`，配置从 `config.toml` 读取；import 模块不会主动配置日志。

### 前端

- `frontend/src/api/knowledge.ts`：12 个 JSON 方法 + raw XHR 上传，ID/query 编码、错误码、AbortSignal。
- `frontend/src/pages/knowledge/useUploads.ts`：FIFO、最多 2 项并发、逐文件恢复与不确定结果核对。
- `frontend/src/pages/knowledge/KnowledgePage.tsx`：私有库管理、文档窗口、状态、上传、搜索与阅读切换。
- `frontend/src/pages/knowledge/UploadQueue.tsx`：真实传输状态、核对/重试/移除入口。
- `frontend/src/pages/knowledge/DocumentReader.tsx`：每窗最多 100 块、页码/段落/版本引用、旧响应隔离。
- `/knowledge` 已在候选的受认证业务路由内，TopBar 有入口；不是主仓库或生产已可访问的声明。

### 包装

- `Dockerfile` 增加 `poppler-utils`、`util-linux`、`ffmpeg`，未实际构建镜像。
- `hippo-knowledge-worker.service` 为模板：沿现 `User=sab` 和 `/home/sab/hippo`，`KillSignal=SIGINT`、`TimeoutStopSec=360`，未安装/启用。
- `README.md`、`API.md` 已补运行、状态、限额、权限与部署门槛。
- 被忽略的本地 `deploy.sh` **尚未接入新 unit 安装及迁移前停止知识 worker**。不能直接运行并假设它会正确处理所有新进程。

## 6. API 与数据语义

| 方法 | 路径 |
|---|---|
| GET / POST | `/api/knowledge-base` |
| GET / PATCH / DELETE | `/api/knowledge-base/{id}` |
| GET / POST | `/api/knowledge-base/{id}/document` |
| GET / DELETE | `/api/document/{id}` |
| PUT | `/api/document/{id}/file` |
| POST | `/api/document/{id}/retry` |
| GET | `/api/document/{id}/content` |
| GET | `/api/knowledge-base/{id}/search` |

线上“不删数据”测试中，表中的 **DELETE 不执行**。

- 所有权只由当前认证用户及知识库归属决定；admin 不可跨用户读取私有库。坏/未知/他人/已删除 ID 统一 404。
- `PUT file` 为原始 File/bytes，不是 multipart；202 仅为 `queued`，不等于可阅读。
- 8 态：`pending_upload / uploading / queued / processing / ready / failed / unsupported / deleted`。
- `needs_ocr` 是错误码，不是第九种状态；未实现 OCR。
- 上传失败：可能需要删除旧 attempt 对象后回 `pending_upload`，客户端再发 File。因此**不能默认在线上跑此类重试测试**。
- 解析失败：保留源对象，回 `queued` 重解析；应只对新建专用测试文档操作。
- `complete_import` 原子替换派生块，会执行针对该 document 的 `DELETE FROM document_chunk` 再插入。首导入的新测试文档应无旧块，但仍必须核对目标白名单；不得拿既有文档重跑来测试“不删数据”。
- 正文 API 每次最多 100 块，页内 offset，空白 PDF 页合法；`version` 与块内容在同一短事务快照内读取。
- 搜索命中包含文档 ID、版本、页和 ordinal；ordinal 是 0-based，界面显示时加 1。
- offset API 上界为 PostgreSQL signed int8；内部 `_offset` 也有上界保护。

## 7. 固定限制与恢复边界

- 单文件 64 MiB；PDF 最多 1000 页。
- 全文最多 2,000,000 字符、50,000 块、每块 4000 字符；超限失败，**不静默截断全文**。
- raw 上传每个 app 最多 2 项；请求体接收预算 120 秒。
- 下载预算 120 秒，在 I/O 边界检查；不能硬中断某次同步 SDK 调用。
- 前端单请求等待 300 秒；超时或 abort 不证明服务器停止，重试前必须 GET 核对。
- processing：每 30 秒续租，600 秒过期，worker 启动及每 60 秒检查回收。
- 维护事务局部 `lock_timeout=5s`、`statement_timeout=10s`；不等于网络断连、连接回收或整业务的硬总期限。
- 同步 SDK 线程必须真正结束后才清临时文件或结算 attempt，不能通过遗弃线程伪装已取消。
- **过期 uploading 尚无安全回收。**不能直接按 TTL 清 key、重置或删除对象，旧上传可能仍在执行，提交也可能已经落地但确认丢失。
- 元数据 POST 结果未知且无文档 ID 时，UI 不再显示会重新 POST 的“检查状态”；先刷新列表找 pending，再选择源文件恢复。
- 阅读引用的窗口跳转依赖当前 parser 每页 ordinal 连续；找不到或版本改变会提示，不猜中段落。

## 8. 已关闭的审查问题

- 页面慢 rename/create 覆盖新 scope 的 P1：原 BLOCK 审查者已复核为 ACCEPT，正式撤销 BLOCK。
- A→B→A 的 rename 标题陈旧：已补详情失效条件和回归。
- 401/403/404 后保留旧行并继续轮询：已改为清行、停止；仅网络/5xx 暂时故障保留同 scope 数据与错误条。
- uncertain 上传的核对入口、active 行移除、重复 resume 同文档：已修。
- close/flush 错误被吞、临时目录失败覆盖取消：已修并有回归。
- 整合胶水、上传/下载、仓储、worker、重试、正文、搜索、前端数据层/队列/阅读器均有独立 ACCEPT。

不要采纳审查中的误报：项目 Python 3.14 支持 PEP 758 的 `except A, B:`，父侧已 compile/Ruff/测试验证；它不是待修的 Python 2 语法。

当前单事件循环中，`semaphore.locked()` 与立即完成的可用槽 acquire 之间不会挂起；所谓该路径 TOCTOU 排队建议已用断言核对而不采纳，不应再加无关锁框架。

## 9. 建议的线上验证顺序（本次未执行）

### A. 只读盘点

1. 先读取已知部署说明、线上 unit 和代码版本，确认实际目录、进程、库版本、工具与备份位置。
2. 只查看必要元信息/计数，避免打印真实正文、配置密钥或完整敏感日志。
3. 确认 `pdftotext`、`pdfinfo`、`prlimit`；确认 PostgreSQL 扩展与 `jiebaqry` 配置。不要为缺工具就自动全局安装或更换基础环境。
4. 核对 S3 bucket/前缀权限：`knowledge/*` 必须拒绝匿名 GET。图片若公开，只开放图片前缀，不让公开整桶策略覆盖知识库。不要由程序自动修改桶策略。

### B. 保存可回退现场

1. 为现有数据库、代码、构建产物及配置建立独立备份/快照，备份不覆盖旧备份，配置备份不输出秘密。
2. 审查部署脚本的 `rsync --delete` 范围和 excludes。不能同步覆盖线上 `config.toml`、`.venv`、logs、数据目录或对象文件。
3. 完整审查实际迁移入口与 SQL，而不是因为本轮仅追加三表就断言整个 `init-db` 无历史清理语句。
4. 任一迁移需要删除、重建或不明数据修复时停止，另行确认；不能直接运行完整测试套件指向生产 DSN。

### C. 优先旁路隔离验证

- 优先在同一生产主机新建旁路测试实例/数据库/桶，保留现有实例。只使用合成数据，产物不自动清理。
- 若必须升级现有库，先列出逐项增量及备份方案；停相关进程后做受审的追加式变更，再切换同版本代码，不能用修改 meta 版本号欺骗旧服务。
- schema 从 28 升到 29 后，旧 28 代码会拒绝库版本，不能盲目回滚代码。不要为回滚而 DROP 新表或回写版本号，先检查真实状态。
- 新 worker 的队列读取是全局候选，**不是按测试用户过滤**。启动前确认没有非测试 queued/processing 文档；有则暂停，不能把它当成只消费测试库的进程。

### D. 非删除的合成数据闭环

1. 使用专用测试用户/私有库，验证 owner 隔离与 admin 无跨用户读取特权。
2. 新建小 TXT、正常带文本层 PDF；上传→queued→processing→ready→正文/搜索→引用跳转。
3. 验证多文件并发与上传 ACK 不等于解析完成，验证分页、中文检索和版本字段。
4. 通过新测试对象验证匿名访问被拒，不探测既有对象内容。
5. 真实测试身份尽可能无删除权限，尤其 S3；若现有流程需要删除权限才能继续，先停止，不临时扩大权限。
6. 暂不执行 DELETE、重复上传、上传失败重试、强杀真实 worker、断网等可能触发自动清理/改写既有状态的故障测试。需要这些测试时使用专门隔离环境或先明确新建数据删除边界。
7. 测试记录、对象与备份全部保留，提供清单供用户决定后续处置。

### E. 发布核对

- 生产 SPA 从 `frontend/dist` 提供，不能只同步 TSX；核对资产 hash 与本次构建。
- 新 unit 不能只复制文件不 reload；迁移前须考虑停止 Web、sync 和 knowledge worker。具体执行方案先说明，不直接跑旧部署脚本。
- 验证只启动一次知识 worker，且日志和 S3 ACL 正确；不要因“生产无人用”就启动大批历史同步、真实媒体下载或外发邮件测试。

## 10. 仍需推进的工作

1. 按上述不删数据约束完成真实 PG：DDL、版本、UUID、部分唯一索引、GIN/中文 FTS、排序、CAS/锁/EPQ、SET LOCAL 和池复用。
2. 真实 SDK/S3、浏览器、移动端和 systemd 优雅退出；删除/故障路径暂不获默认授权。
3. 过期 uploading 安全恢复：需要保留 attempt/journal 与提交不确定性，不得用清 key/批量删对象代替设计。
4. 文档标注/个人理解持久化、跨文档 AI 问答、原文件访问等后续能力。
5. 新 worker 的实际部署流程接线及完整发布/回退验证。
6. 原较大任务中的共享划线、触控标注、筛选/锚点/正文版本缓存和更多业务可观测性，未因此全部完成。

## 11. 后续代理工作规则

- 实施使用 `thinkwatch/deepseek/deepseek-v4-flash:medium`；只读审查使用相同已批准模型。
- 受限 file-worker/file-reviewer 不执行命令。父侧可执行获准范围内验证；线上动作另按最新授权与不删数据边界。
- 每个工作树同时最多一个 writer，明确文件所有权；共享仓储文件只能按不重叠增量合并，先 `git apply --check`，不整文件覆盖主仓库。
- 超时/工具失败先停止报告、确认 terminal、备份部分成果并跑父侧验证，再同协议定点恢复；不能换模型/CLI 或从头重做已通过模块。
- 产物报告必须读取；代码能编译/测试通过不代表用户批准线上操作，也不代表所有任务完成。
- 过去曾误发一个默认模型 `noop`，已确认 0 工具调用、无源码访问/修改，不计入实现；不要复用该任务。后续仍只用指定 DeepSeek。
- 当前主仓库还有其他会话未提交工作；禁止 reset/stash/clean、批量删除工作树或自动提交。

**本交接完成前仅写了本文档；没有执行线上测试或修改生产环境。**
