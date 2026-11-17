# 前端改动记录

本文档只记录 `frontend/` 的改动，按日期续写，避免与后端/基础设施记录冲突。
后端契约见设计文档第 6 章；界面清单见第 8 章。

## 2026-11-13 · 控制台页面可用化（M1/M3 前端）

### 范围

仅修改 `frontend/`。未改动 `backend/`、`infra/`、数据库与迁移。

### 依赖

- 新增 `react-markdown`、`rehype-sanitize`：Wiki 正文白名单渲染（设计 8.4）。
- 显式声明 `dayjs`：Ant Design 日期组件所需，此前仅为传递依赖。
- 沿用现有 `package-lock.json` 与 `node_modules`，继续使用 npm（未切换包管理器），安装走项目已有国内镜像。

### 数据层

- `lib/api/client.ts`：新增 `requestEnvelope`，统一解析 `{data, meta:{request_id, next_cursor}}`；缺少 `data` 时按契约错误处理。
- `lib/api/errors.ts`：统一 401 / 403 / 404 / 501 / 503 分类，供页面降级。
- `lib/api/contracts.ts`：OpenAPI 只生成请求模型，响应模型按设计第 6 章与后端现有 schema 补齐（事件、反馈、实体状态、任务、概览、Wiki）。
- `lib/api/endpoints.ts`：封装全部页面所需端点；所有变更请求自动携带 `Idempotency-Key`；不发送契约之外的查询参数。
- 未实现接口不造数据：Wiki 列表、Wiki 详情、Wiki 同步任务在 404/405/501/503 时进入“接口未就绪”状态。

### 页面

- **概览 `/dashboard`**：真实汇总取数；事件/任务状态分布、Wiki 新鲜度、Outbox 与失败告警；无趋势接口时不用假序列填充。
- **统一搜索 `/search`**：筛选状态同步到 URL；游标式结果卡片；分数解释抽屉；`clicked / adopted / helpful / not_helpful` 反馈与 `counted=false` 原因展示；保留上一次结果并提示“正在更新”；空结果展示当前筛选，不自动放宽条件。
- **情景记忆 `/memories/events`**：主体、类型、状态、时间筛选；游标分页；归档入口。
- **事件详情 `/memories/events/[id]`**：原文、模型/分数信息、纠偏链；管理员可纠偏、归档/合规清除、恢复、手调熟悉度；变更后刷新列表与详情。
- **实体 `/entities`、`/entities/[id]`**：类型/状态筛选与游标分页；状态版本、来源事件、状态快照、重放记录；管理员可触发重建（异步任务 ID 返回后可见）。
- **Wiki `/wiki`、`/wiki/[articleId]`**：词条搜索与分类/语言/状态筛选；Markdown 白名单渲染；chunk 目录、`?chunk=N` 深链与高亮、上一块/下一块；来源修订、复制引用；非 active 块仅管理员可见。
- **Wiki 同步 `/jobs/wiki-sync`**：增量/全量任务、进度、游标、错误样本；进行中 3 秒轮询，页面不可见时暂停；operator 以上可创建/重试。

### 应用壳层

- 顶栏新增全局搜索入口、失败告警提示、当前用户与角色展示；离线提示与统一重试。
- `SessionProvider` 向客户端提供会话角色；管理员动作按角色隐藏，后端 403 仍有统一兜底。
- Dev Auth 代理改为 `Authorization: Dev <subject>` 并转发 `X-Dev-Roles`（与后端 `authenticate` 实现一致）；变更请求增加同源 Origin 校验；转发 `Idempotency-Key`。
- `.env.example` 增加 `DEV_AUTH_SUBJECT`、`DEV_AUTH_ROLES`。生产环境不会启用 Dev Auth。

### 验证

- `npm run lint`：通过（0 warning）。
- `npm run typecheck`：通过。
- `npm test`：9 个用例通过（新增 `QueryState` 降级、API 错误分类与格式化用例）。
- `npm run build`：通过。
- dev server + Edge 无头浏览器逐页冒烟：9 条路由均渲染；并在临时桩后端（未入库）下验证真实数据、搜索反馈 POST、Wiki `?chunk` 锚点与 Markdown 清洗（`<script>` 不执行/不输出）、Wiki 404 降级。

### 已知契约差异（需要后端/后续阶段对齐）

1. `GET /dashboard/summary` 后端当前要求 `operator`，设计第 8 章为 `viewer+`；viewer 打开概览会看到 403 权限态。
2. Wiki 读接口与 `/wiki/sync-tasks` 系列尚未实现（AG-04）；前端已按第 6.7 节准备，接口就绪后无需改页面。
3. 设计仅说明 Wiki 列表支持“词条搜索、分类/语言/状态筛选”，未固定参数名；前端暂用 `q / category / language / status / cursor / limit`，AG-04 落地时需确认。
4. 设计列出 `GET /entities/{id}/events`，后端未实现；实体详情改用 `entity.source_event_ids` 渲染来源事件。
5. 事件详情暂无访问记录接口，页面未展示访问次数/访问日志。

## 2026-11-16 · 对接真实后端的端到端联调

### 范围

仍只修改 `frontend/` 与本文件。未改 `backend/`、`infra/`、数据库与迁移。

### 真实响应对齐（前端修正）

- `lib/api/contracts.ts`：按真实响应补齐 `WikiArticleSummary.chunk_count`、`WikiChunk`、`WikiTocEntry`、`WikiArticleDetail`、`WikiSyncMode`（含 `reembed`）、`WikiSyncTask`（`job_id`/`package_uri`/`requested_by`/`retryable`/`error_sample[]`）等类型。
- `lib/api/endpoints.ts`：新增 `listEntityEvents`、`getWikiArticle(includeInactive)`；同步任务列表参数改为 `source/mode/status/cursor/limit`。
- `lib/auth/index.ts`：Dev Auth 后端只校验 `Dev <subject>`，不再要求 `DEV_AUTH_TOKEN`；缺少令牌不再导致代理 401。
- `features/search/SearchView.tsx`：不再向前端伪造 `session_id`（后端只接受已存在的工作记忆会话），搜索与反馈请求均省略该字段；反馈仍按 `counted` / `not_counted_reason` 回显。
- `features/entities/EntityDetailView.tsx`：来源事件改用 `GET /entities/{id}/events` 的 `useInfiniteQuery` 游标分页（每次 20 条），替换原先读取 `source_event_ids` 的占位渲染；新增 `.entity-event-row` 列表样式。
- `features/wiki/WikiArticleView.tsx`：`include_inactive=true` 仅在 operator 及以上发送（真实后端对 viewer 返回 403）；非生效知识块提示文案同步调整。
- `features/wiki/WikiSyncView.tsx`：支持 `reembed` 模式（`package_uri` 可省略、`package_version` 必填）；详情字段改为 `job_id/package_uri/requested_by/started_at/finished_at`；错误样本按数组渲染；重试按钮以 `retryable` 为准。
- `features/wiki/WikiListView.tsx`：卡片展示真实 `chunk_count`。
- `lib/labels.ts`：新增同步模式文案。
- `features/dashboard/DashboardView.tsx`：概览卡片的“最近同步”改读契约新字段 `wiki.last_sync_at`（后端已由 `last_sync_time` 更名）。

### 联调环境

- 真实 FastAPI + PostgreSQL/pgvector + Redis + Celery worker/beat。
- 真实模型：本地 BGE-large-zh-v1.5（1024 维）+ `training.train` 产出的 LSTM 事件模型包；Reranker 关闭。
- 数据：3 个主体 × 8 条事件（24 条 active）、3 篇 Wiki 词条 / 9 个知识块、25 条同步与 Outbox 任务成功。
- 浏览器：Edge 无头（Playwright），视口 1440×1000。

### 逐页结果

| 页面 | 结果 |
|---|---|
| `/dashboard` | 真实汇总：事件 24、Wiki 3 词条 / 9 知识块、任务 25 succeeded，无降级文案 |
| `/memories/events` | 首屏 20 条 + 游标翻页 21→25 行；筛选与状态标签正常 |
| `/memories/events/[id]` | 原文、模型、分数、纠偏链渲染正常 |
| `/entities`、`/entities/[id]` | 列表有数据；来源事件走新接口，8 条且可见；重建按钮仅 operator+ |
| `/search` | 10 条结果；反馈 POST 200 并回显“已反馈并计入”；分数抽屉正常 |
| `/wiki` | 真实词条与 `知识块：3` |
| `/wiki/[articleId]?chunk=2` | 深链滚动到目标块并高亮 1 处；目录/来源修订/上下块正常 |
| `/jobs/wiki-sync` | 任务列表（`local_mirror` / 全量）与详情（包版本等）正常 |

冒烟汇总：16/16 通过；页面运行期间无 4xx/5xx API 响应。

### 权限抽查（viewer）

- `GET /dashboard/summary`、`GET /wiki/sync-tasks`、事件/实体/Wiki 读接口：200。
- 事件详情不出现纠偏/归档/恢复按钮；实体详情不出现“触发重建”。
- Wiki 同步页显示只读提示，创建按钮禁用；Wiki 列表仅 active。
- `POST /entities/{id}/rebuild`、`POST /wiki/sync-tasks`、`POST /events/{id}/restore`：403；`POST /events/{id}/corrections`、`DELETE /events/{id}`：按当前契约对 viewer 放行（探针返回 404 说明已过权限）。
- 抽查汇总：11/11 通过。

### 静态检查

- `npm run lint`：通过（0 warning）。
- `npm run typecheck`：通过。
- `npm test`：3 个文件 / 9 个用例通过。
- `npm run build`：通过（12 条路由）。
- 临时冒烟脚本已删除，未留在仓库。

### 后端问题清单

1. ~~**Compose 未启动 Celery beat，事件会长期 pending**~~ **（后端已修复，2026-11-16 复验通过）**：`infra/docker-compose.yml` 已新增独立 `beat` 服务，`worker` 保持不带 `-B`。
2. ~~**概览汇总未按可见性过滤**~~ **（后端已修复，2026-11-16 复验通过）**：`/dashboard/summary` 已接可见性谓词。
3. **纠偏/归档权限与预期不一致（待产品确认）**：当前路由对 `POST /events/{id}/corrections`、`DELETE /events/{id}` 使用 `require_viewer`，viewer 可纠偏与归档；前端事件详情页按 admin 门控这两个按钮（“恢复”后端确为 admin）。设计意图若为 admin，需要后端收紧；按要求未改前端、未改后端，仅记录。
4. **后端镜像缺少 `packaging`，compose 的 worker/beat 无法连接 Redis**：`kombu.transport.redis` 在运行时 `from packaging.version import Version`，但 `pyproject.toml` 未声明 `packaging`。本地 venv 因其他包间接带入该依赖而正常，干净镜像里 `python -m celery ... beat` 直接 `ModuleNotFoundError: No module named 'packaging'` 退出。建议把 `packaging` 加进依赖。
5. **beat 默认 schedule 文件不可写**：镜像以 `memory`（uid 10001）运行、`WORKDIR /app/backend` 属 root，compose 的 `beat` 命令未指定 `--schedule` 也未挂可写卷，启动报 `[Errno 13] Permission denied: 'celerybeat-schedule'`。建议命令加 `--schedule /tmp/celerybeat-schedule` 或挂载可写目录。
6. ~~**`include_inactive` 契约未写明 viewer 行为**~~ **（契约已补充，行为一致）**：现契约明确 viewer 传 `include_inactive=true` 返回 403；实测 403，operator 传该参数 200 且返回全部知识块。前端仅在 operator+ 发送，路径不受影响。
7. ~~**搜索 `session_id` 语义**~~ **（契约已补充，行为一致）**：现契约说明可省略，传入则必须是本人已存在的工作会话；实测省略 200、随机 UUID 404。前端不传，路径不受影响。

### 后端修复的针对性复验（2026-11-16）

- **Outbox 自动消化**：在无 beat 的情况下通过 API 新建 3 条事件，`outbox.pending=3`、`events.pending=3`；随后仅启动 compose 的 beat 服务（本地 worker 消费），约 30 秒内 outbox 全部 `processed`、事件全部转 `active`（库内 27 active、0 pending），无需手工触发。
  - 过程中发现上述问题 4、5：干净的 compose beat 无法直接启动。为完成调度链路验证，只在临时探针容器里补装 `packaging` 并指定 `/tmp` schedule，未修改仓库代码；问题已进入清单。
  - 说明：本机 `docker compose up` 全量栈仍受 bootstrap 模型配置限制（模型在宿主 Windows 路径），本次只对 beat 服务做隔离验证。
- **可见性口径**：viewer 且无可见数据时，`GET /memory/events` 0 条、`/dashboard/summary` 的 `events` 为空对象；数据属主以 viewer 身份访问时，列表 27 条、概览 `events.active=27`，两端口径一致，概览不再暴露不可见规模。

## 2026-11-17 · 实体重建权限收紧到 admin

- `features/entities/EntityDetailView.tsx`：重建按钮门控由 `useHasRole("operator")` 收紧为 `useHasRole("admin")`，普通用户与 operator 均不渲染“触发重建”；页头描述同步改为“重建仅 admin 可触发”。
- operator 若直接调用 `POST /entities/{id}/rebuild` 会拿到 403，由页面既有 `message.error(error.message)` 兜底展示，未额外加工。
- 验证：`npm run lint`、`npm run typecheck`、`npm test`（3 文件 / 9 用例）、`npm run build` 全部通过。
