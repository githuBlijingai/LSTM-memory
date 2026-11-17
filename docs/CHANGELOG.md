# 修改记录

本文件按工作包记录每次修改的范围、主要文件、验证结果和下一步。设计基线为 `../../基于LSTM的分层记忆系统设计文档V1.2.md`。

## 2026-11-12 · INIT-00 项目初始化

### 修改内容

- 创建 `memory-system` 工程目录。
- 创建 `docs` 修改记录目录和 `infra` 基础设施目录。
- 确认从 AG-00、AG-01 开始实施，不跳过后续验收记录。

### 环境检查

- 本机 Python：3.15.2；项目容器基线仍为 Python 3.12。
- Node.js：26.13.0；npm：12.2.2。
- Docker：29.9.2；Docker Compose：v5.6.1。

### 验证结果

目录创建成功，尚未安装依赖或启动服务。

### 下一步

并行实施后端 AG-00/AG-01、Next.js 基础骨架和本地容器编排。

## 2026-11-12 · AG-00/AG-01-BE 后端基础设施

### 修改内容

- 新建 FastAPI、Celery、Pydantic Settings、结构化日志、请求 ID、统一错误响应和健康检查。
- 新建 `python -m app.bootstrap`，覆盖 GPU/显存检测、首次配置向导、非交互失败、远程模型检查和 Secret 脱敏。
- 按设计第5章建立16张 ORM 表及可逆 Alembic 初始迁移，包含 Pgvector、ENUM、JSONB、约束和 HNSW 索引。
- 实现 Unit of Work、Repository、游标分页、OIDC/JWKS、Dev Auth、RBAC、Owner/Visibility 权限谓词。
- 实现幂等、Outbox、通用 Job、Audit Log 基础能力及测试。

### 主要文件

- `pyproject.toml`
- `backend/app/bootstrap.py`
- `backend/app/models/`
- `backend/app/core/`
- `backend/app/services/`
- `backend/app/repositories/`
- `backend/alembic/versions/74a69ff68f54_initial_schema.py`
- `backend/tests/`

### 验证结果

- `python -m pytest backend -q`：38 passed，1 skipped。
- Ruff 格式和静态检查、Python 编译检查、依赖一致性检查通过。
- Alembic PostgreSQL 离线 upgrade/downgrade SQL 生成成功。
- 真实 PostgreSQL 集成测试待主流程启动 Docker 后验证，不能以 SQLite 结果代替。

### 已知限制

本机测试使用 Python 3.15.2；容器基线为 Python 3.12，待执行容器构建验证。

## 2026-11-12 · AG-00-FE Next.js 基础骨架

### 修改内容

- 建立 Next.js 16 App Router、React 19、TypeScript、Ant Design、TanStack Query 和 ECharts 工程。
- 实现中文响应式布局、导航、通用加载/空/错误/403组件。
- 建立设计要求的全部前端路由；当前业务页面明确标记 AG-05 待实现，未使用伪造业务数据。
- 实现类型安全 API Client、请求 ID、错误规范化、同源代理和显式 Dev Auth 占位。
- 配置 Vitest、Playwright 和前端 Dockerfile。

### 主要文件

- `frontend/package.json`
- `frontend/app/`
- `frontend/components/`
- `frontend/lib/`
- `frontend/tests/`
- `frontend/Dockerfile`

### 验证结果

- `npm run lint`、`npm run typecheck`：通过。
- `npm test`：1个测试文件、2项测试通过。
- `npm run build`：全部设计路由构建成功，standalone 产物生成。
- Playwright 浏览器和真实 E2E 留待后续业务页面完成后执行。

## 2026-11-12 · AG-00-INFRA 本地容器编排

### 修改内容

- 新增 PostgreSQL/Pgvector、Redis、Bootstrap、API、Worker、Web、Nginx 的 Compose 编排。
- 新增后端和前端 Docker 构建上下文排除规则、根级 npm 命令和环境变量模板。
- 数据库和 Redis 宿主机端口支持 `POSTGRES_PORT`、`REDIS_PORT`，避免与本机已有服务冲突；容器间端口保持不变。
- 本机 PostgreSQL 18 已占用 5432，因此本次验收使用宿主机 55432 映射到容器 5432。

### 主要文件

- `infra/docker-compose.yml`
- `infra/nginx.conf`
- `.env.example`
- `.dockerignore`
- `package.json`

### 验证结果

- `docker compose -f infra/docker-compose.yml config`：通过。
- `db`、`redis` 容器健康检查通过，Redis `PING` 返回 `PONG`。
- Docker Hub 直连受网络阻断；本次通过本机已有 DaoCloud 镜像源拉取后使用相同目标镜像标签。
- 完整 API/Worker/Web/Nginx 栈尚未启动，需先提供有效模型配置并通过 Bootstrap。

## 2026-11-12 · FIX-00 前端 API 契约一致性

### 修改内容

- 同源代理生成的错误响应改为统一 `error + meta.request_id + meta.next_cursor` 结构，并补充空 `details`。
- 代理仅接受 UUID 格式的传入请求 ID，其他值由系统重新生成。
- API Client 改为从 `meta.request_id` 读取响应请求 ID，不再读取顶层字段。

### 主要文件

- `frontend/app/api/v1/[...path]/route.ts`
- `frontend/lib/api/client.ts`

### 验证结果

- 修改后 `npm run lint`、`npm run typecheck`：通过。
- 修改后 `npm run test`：2 项测试通过。
- 修改后 `npm run build`：全部路由和 standalone 产物构建成功。

## 2026-11-12 · AG-01-FIX 真实 PostgreSQL 验收与降级保护

### 修改内容

- 真实兼容数据库测试发现 `downgrade` 删除共享 `vector` 扩展会与其他扩展依赖冲突。
- 初始迁移降级现在只删除本系统表、索引和 ENUM，不再删除可能由其他数据库对象共用的 Pgvector 扩展。
- 更新迁移测试，防止后续重新引入共享扩展删除行为。
- 安装项目 development/integration 依赖到 `.venv`。

### 主要文件

- `backend/alembic/versions/74a69ff68f54_initial_schema.py`
- `backend/tests/test_migration.py`
- `pyproject.toml` 的 integration 依赖组

### 验证结果

- `.\.venv\Scripts\python.exe -m pytest backend -q`：38 passed，1 skipped；跳过项为需要显式启用的 PostgreSQL 集成测试。
- `RUN_POSTGRES_INTEGRATION=1` 后，目标 `pgvector/pgvector:pg16` 集成测试：1 passed，覆盖 upgrade → downgrade → upgrade、权限过滤、游标分页、幂等冲突和审计脱敏。
- 在 Compose 的 PostgreSQL 16.15 容器上再次执行 upgrade → downgrade → upgrade：通过。
- 最终确认 16 张业务表、Alembic revision `74a69ff68f54`、Pgvector 0.8.7。
- `.\.venv\Scripts\python.exe -m ruff check backend`：通过。

### 已知限制

- 本机单元测试使用 Python 3.15.2，后端容器基线 Python 3.12 的镜像构建尚未执行。
- 完整业务栈仍需配置远程或本地 Embedding、Reranker 和事件模型；不会使用伪造向量绕过预检。

### 下一步

进入 M1，按设计实施 AG-02 记忆写入管线和 AG-03 统一检索与反馈。

## 2026-11-13 · M1-DEPS 依赖安装与国内镜像固化

### 修改内容

- 新增 `infra/pip.ini`：清华 PyPI 主源 + 阿里云备源，`scripts/install.ps1` 自动复制到 `.venv312/pip.ini` 并设置 `PIP_CONFIG_FILE`，不再临时追加镜像参数。
- 统一使用 `.venv312`（Python 3.12.9，来自项目内置 `.python312`）；系统 `.venv` 为 Python 3.15.2，PyYAML 等包没有 wheel 导致源码编译失败，已停用。
- 通过国内镜像补齐 ml 依赖（torch 2.15.0+cpu、transformers 5.22.2、safetensors、PyYAML 6.0.3），未跳过或替换任何依赖。
- 新增 `frontend/.npmrc` 使用 npmmirror，前端新增 `openapi-typescript` 开发依赖。

### 验证结果

- `.venv312` 下 `import yaml, torch, transformers, safetensors` 成功。
- `pip install -e ".[dev,ml]"` 通过 `scripts/install.ps1` 完成。

## 2026-11-13 · M1-BE 记忆写入、检索与反馈闭环

### 修改内容

- 新增领域模块：`app/domain/intent.py`（意图分类与权重）、`app/domain/retrieval.py`（融合排序、Wiki 单文章最多 2 块、每日增量上限）。
- 新增服务：`memory_writes.py`（事件创建/纠正/删除/恢复、工作记忆巩固事件、审计、Outbox）、`memory_reads.py`（事件、实体状态、Replay、Job、Dashboard）、`memory_search.py`（双路召回、可选精排、融合、曝光日志）、`feedback.py`（有效反馈规则、每日额度、重放触发）。
- 新增 API：`/api/v1/memory/*`、`/working/*`、`/entities/*`、`/jobs/*`、`/dashboard/summary`、`/admin/memories/{type}/{id}/familiarity`，写接口统一使用 `Idempotency-Key`。
- Outbox 派发改为 `claim_batch` 领取 pending/processing 并携带 visibility timeout，Worker 崩溃后事件不会永久卡住；新增 `app/tasks/outbox.py` 与 `app/api/idempotent.py`。
- `LSTMEventEncoder`、`QueryAdapter`、本地/远程 Embedding、Reranker 与模型包校验沿用 AG-02 实现。

### 验证结果

- `.\.venv312\Scripts\python.exe -m pytest backend/tests -q`：83 passed，1 skipped（Postgres 集成测试需显式开启）。
- `.\.venv312\Scripts\python.exe -m ruff check backend`：通过。
- OpenAPI 共 21 条路径；19 条 M1 路由注册与写接口幂等头由测试覆盖。

## 2026-11-13 · M1-TRAIN 训练脚本、基线评估与模型包清单

### 修改内容

- 新增 `backend/training/`：
  - `dataset.py`：训练数据解析、按实体切分 + 时间截断、删除/隐私擦除排除、上下文窗口。
  - `model.py`：与运行期 `LSTMEventEncoder` 完全同构的投影 + LSTM 模块。
  - `train.py`：InfoNCE（批大小 64、温度 0.07、最多 20 Epoch、验证集早停），导出 `weights.safetensors`、`config.json`、`manifest.json`、`metrics.json`、`dataset.json`。
  - `evaluate.py`：Recall@10、MRR@10、nDCG@10，与纯 BGE 基线对比并检查发布门槛。
  - `manifest.py`：模型包写入与运行期一致性校验（复用 `load_model_package`）。
  - `vectors.py`：预计算向量优先，缺失时通过 Embedding Provider 批量计算并写 JSONL 缓存。
  - `export.py`：从数据库导出 ACTIVE 事件与计入熟悉度的 helpful 反馈。
- `pyproject.toml` 打包加入 `training*`。

### 验证结果

- `pytest backend/tests/test_training_pipeline.py`：9 项测试通过（切分/截断/窗口/指标/清单篡改/训练冒烟）。
- 训练 CLI 端到端：生成 6 实体 × 4 事件合成数据（1024 维）→ 训练 2 Epoch → 模型包清单校验通过 → 运行期 `LSTMEventEncoder` 严格加载并输出 256 维查询向量。

### 已知限制

- 真实数据训练需要配置 Embedding Provider 或提供预计算向量；首次训练质量取决于历史反馈量。
- 发布门槛失败时训练 CLI 返回码为 1，模型包仍保留供排查。

## 2026-11-13 · M1-FE-API 前端类型生成

### 修改内容

- 新增 `scripts/export_openapi.py` 导出 `frontend/openapi.json`。
- 前端新增 `npm run generate:api`，通过 `openapi-typescript` 生成 `frontend/lib/api/schema.ts`。
- `frontend/.npmrc` 固定 npmmirror 源。

### 验证结果

- `python scripts/export_openapi.py`：21 条路径。
- `npm run generate:api`、`npm run lint`、`npm run typecheck`：通过。
- `npm test`：2 项测试通过；`npm run build`：全部路由构建成功。

## 2026-11-13 · BE-CONTRACT 并行分工与对外契约冻结

### 修改内容

- 明确并行边界：本会话只维护 `backend/`、`infra/`、数据库迁移与 Celery；不再改动 `frontend/`。
- `scripts/export_openapi.py` 输出目标从 `frontend/openapi.json` 改为 `docs/api/openapi.json`，避免触碰前端目录；新增 `docs/api/openapi.json`。
- 新增 `docs/API_CONTRACT.md`：逐接口请求/响应字段、权限、状态码、幂等要求，以及 18 条与设计第 6 章的差异（C-01～C-18），供前端对齐。
- 修复 `EventCommandService` 引用不存在的 `EventStatus.DELETED` 导致的运行时 `AttributeError`（纠偏、删除事件必现）；非标准错误码 `INVALID_STATE` 改为第 6.1 节的 `CONFLICT`。

### 主要文件

- `scripts/export_openapi.py`
- `docs/API_CONTRACT.md`
- `docs/api/openapi.json`
- `backend/app/services/memory_writes.py`

### 验证结果

- `python scripts/export_openapi.py`：21 条路径，24 个操作。
- `pytest backend/tests -q`：83 passed，1 skipped。
- 差异条目已逐条核对实现代码（`api/v1/*.py`、`schemas/memory.py`、`services/memory_reads.py`）。

### 下一步

启动 Compose db/redis，运行 `RUN_POSTGRES_INTEGRATION=1` 集成测试，并补充事件写入 → 检索 → 反馈 → 重放/重建的端到端用例。

## 2026-11-13 · M1-E2E 集成验收与流水线缺陷修复

### 修改内容

- 启动 Compose `db`、`redis`（本机 5432/6379 被占用，使用 `POSTGRES_PORT=55432`、`REDIS_PORT=56379`）。
- 新增集成用例 `test_event_search_feedback_replay_rebuild_roundtrip`：真实 PostgreSQL + Pgvector，覆盖事件写入 → 事件管线（编码、快照、实体推进）→ 统一检索 → `helpful` 反馈（熟悉度增长并触发重放）→ 重放任务 → 重建任务 → 状态查询。测试内允许未训练 LSTM 与确定性 Embedding，仅用于编排验证；生产路径仍强制加载模型包。
- 修复事件管线窗口编码缺陷：`mask` 按窗口大小左补零，但 `base_vectors` 未补零，导致 `mask length must match base_vectors`。新增 `domain.sequence.pad_window_vectors`，在事件管线与状态重建两处对齐。
- 修复 `EventPipelineService.process_event` 在 `commit` 后读取已过期 ORM 属性导致的 `DetachedInstanceError`（日志与返回值），并在写 Redis 缓存前刷新快照。

### 主要文件

- `backend/app/domain/sequence.py`
- `backend/app/services/event_memory.py`
- `backend/app/services/entity_state.py`
- `backend/tests/test_postgres_integration.py`
- `backend/tests/test_domain_sequence.py`

### 验证结果

- `RUN_POSTGRES_INTEGRATION=1 pytest backend/tests -q`：86 passed（无跳过）。
- `.\.venv312\Scripts\python.exe -m ruff check backend`：通过。
- 事件写入 → 检索 → 反馈 → 重放 → 重建链路在真实数据库上通过：重放后状态版本 1→2，重建后状态版本 ≥2 且 `last_sequence_no=1`。

### 下一步

进入 M2（AG-04 Wiki 同步），实现词条/知识块/来源链、同步任务与 `/api/v1/wiki/*`，并同步 `docs/API_CONTRACT.md`。


## 2026-11-13 · M2 / AG-04 Wiki 同步

### 修改内容

- 新增 WikiExtractor JSONL 解析适配（`id/revid/title/url/text`，可选 `lang/timestamp/redirect`），支持本地文件、目录、`file://`、`http(s)://`；`s3://` 明确返回 422。
- 新增知识块切分：按 `== 标题 ==` 层级维护 `heading_path`，默认 200～500 字、重叠 50 字，超长段落按句末标点切窗。
- 新增 `WikiSyncService`：词条 upsert、内容哈希判定、旧块 `superseded`、来源删除 `deleted`、断点续传游标、错误样本（最多 20 条）、`reembed` 全量重编码；单条失败不阻断整包，模型/数据库错误触发任务重试。
- 新增 `/api/v1/wiki/*` 7 条路由，复用成功信封、幂等键、角色校验与游标分页；同步任务复用 `background_job`、outbox 与 Celery。
- 新增 Celery 任务 `app.tasks.wiki_sync.sync_wiki`（最多重试 3 次），outbox 事件 `wiki.sync.requested`。
- 无需新增迁移：初始迁移 `74a69ff68f54` 已包含 `wiki_article`、`wiki_chunk`、`wiki_sync_task`。
- 修复集成测试连接问题：testcontainers 的 `get_connection_url()` 固定使用 5432，会误连宿主机 PostgreSQL；测试改为显式使用 `get_exposed_port(5432)`。

### 主要文件

- `backend/app/domain/wiki.py`
- `backend/app/services/wiki_parser.py`
- `backend/app/services/wiki_sync.py`
- `backend/app/services/wiki_reads.py`
- `backend/app/schemas/wiki.py`
- `backend/app/api/v1/wiki.py`
- `backend/app/tasks/wiki_sync.py`
- `backend/app/tasks/outbox.py`、`backend/app/tasks/dispatch.py`、`backend/app/worker.py`
- `backend/app/core/config.py`（`WIKI_CHUNK_*`、`WIKI_EMBED_BATCH_SIZE`、`WIKI_PACKAGE_MAX_BYTES`）
- `backend/tests/test_wiki_sync.py`、`backend/tests/test_wiki_integration.py`
- `docs/API_CONTRACT.md`、`docs/api/openapi.json`

### 验证结果

- `pytest backend/tests -q`（`RUN_POSTGRES_INTEGRATION=1`）：100 passed。
- `ruff check backend`：通过。
- `python scripts/export_openapi.py`：27 条路径，31 个操作。
- 集成用例覆盖：全量同步、重复同步不重复建块、内容更新旧块失效、来源缺失删除、`reembed` 版本升级、失败任务重试、同来源并发冲突与角色拦截。

### 接口契约变更

- 新增 `GET /wiki/articles`、`GET /wiki/articles/{id}`、`GET /wiki/articles/{id}/chunks/{chunk_index}`、`POST /wiki/sync-tasks`、`GET /wiki/sync-tasks`、`GET /wiki/sync-tasks/{id}`、`POST /wiki/sync-tasks/{id}/retry`。
- `docs/API_CONTRACT.md` 新增第 3.8 节，差异表 C-17 改为已实现，并新增 C-19～C-22（数据包来源、状态可见性、重编码字段、进度计算）。

### 下一步

进入 M3：按前端联调反馈补齐接口支撑，并推进 M4 安全/观测/恢复验收。

## 2026-11-13 · 前后端联调对齐（C-09/C-13/C-17）

### 修改内容

- `GET /dashboard/summary` 恢复为设计 8.2 的 viewer+ 权限，移除服务层 operator 限制。
- 新增 `GET /api/v1/entities/{entity_id}/events`，复用事件摘要与游标分页，按 `event_time` 倒序；补充实体存在性校验。
- Wiki 词条列表搜索参数正式定为 `q`，与前端已实现参数一致；`category`、`language`、`status` 保持不变并写入契约。
- `GET /wiki/sync-tasks`、`GET /wiki/sync-tasks/{id}` 按设计 8.2 调整为 viewer+（创建与重试仍为 operator+）。

### 验证结果

- `pytest backend/tests -q`（`RUN_POSTGRES_INTEGRATION=1`）：100 passed。
- `ruff check backend`：通过。

### 接口契约变更

- `docs/API_CONTRACT.md`：C-09 改为已实现、C-13 改为 viewer+、第 3.6 节新增实体事件接口、第 3.8 节参数改为 `q` 并调整同步任务读取权限。

## 2026-11-13 · M3 接口支撑第一批（C-03/C-04）

### 修改内容

- 事件列表 `GET /api/v1/memory/events` 新增 `owner_id` 筛选与 `sort=event_time.desc|event_time.asc`（默认 `desc`）；游标在双向下均可续页，`owner_id` 只在通过可见性谓词后的结果集中收窄。
- 游标列表 `limit` 上限由 50 对齐到设计 6.1 的 100（事件、实体、实体来源事件、Wiki 词条、Wiki 同步任务），默认值仍为 20。
- `MemoryReadService.list_events` 增加 `owner_id`、`descending` 参数，排序与游标比较按方向切换。

### 主要文件

- `backend/app/api/v1/memory.py`、`backend/app/api/v1/entities.py`、`backend/app/api/v1/wiki.py`
- `backend/app/services/memory_reads.py`
- `backend/tests/test_postgres_integration.py`（新增 `owner_id` 过滤与升序续页断言）
- `docs/API_CONTRACT.md`、`docs/api/openapi.json`、`docs/CURRENT_WORK.md`

### 验证结果

- `pytest backend/tests -q`：96 passed，2 skipped（未启用集成）。
- `RUN_POSTGRES_INTEGRATION=1` + `testcontainers`：`test_postgres_integration.py` 2 passed。
- `ruff check backend`：通过。
- `python scripts/export_openapi.py`：28 条路径，事件列表已含 `owner_id` 与 `sort` 枚举。

### 接口契约变更

- C-03 关闭：所有游标列表最大 100。
- C-04 关闭：事件列表支持 `owner_id` 与双向 `sort`，前端可直接传参。

### 下一步

等待前端端到端联调反馈，逐项修复后端问题；随后推进 M4 安全、观测与故障恢复验收。

## 2026-11-13 · M3 权限校验修正（重建接口）

### 修改内容

- 核实 `POST /api/v1/entities/{entity_id}/rebuild`：服务层 `request_rebuild` 已要求 operator，但路由依赖原先声明为 `require_viewer`，属于声明与契约 C-07 不一致。已改为 `require_operator`。
- 补充回归测试 `test_rebuild_requires_operator`，直接从端点签名校验路由层依赖，防止再次退化。
- 同批核查其他管理员类写接口：
  - `POST /memory/events/{id}/restore`：路由与服务层均为 admin，一致。
  - `DELETE /memory/events/{id}`：`purge` 由服务层强制 admin，`archive` 为 viewer，与契约 3.2/C-05 一致。
  - `POST /wiki/sync-tasks`、`POST /wiki/sync-tasks/{id}/retry`：operator，一致。
  - `POST /admin/memories/{type}/{id}/familiarity`：admin，一致。
  - 设计 8.2 将「纠偏、归档」列为 admin，而契约 3.2 与实现为 viewer；该项涉及前端交互，未擅自改动，已记入 `CURRENT_WORK.md` 待拍板。

### 主要文件

- `backend/app/api/v1/entities.py`
- `backend/tests/test_memory_api.py`
- `docs/API_CONTRACT.md`（C-07 当前实现描述）、`docs/CURRENT_WORK.md`

### 验证结果

- `ruff check backend`：通过。
- `pytest backend/tests/test_memory_api.py -q`：10 passed。
- `python scripts/export_openapi.py`：28 条路径。

### 接口契约变更

- 契约字段与路径不变；C-07 明确路由层与服务层均按 operator 校验，前端按 operator 权限展示重建按钮。
- C-03（limit 最大 100）、C-04（`owner_id`/`sort`）文档描述已与实现同步，无残留“最大 50”或“传了会被忽略”的表述。

### 下一步

等待前端对联调问题的反馈；确认「纠偏/归档」是否按设计 8.2 收紧到 admin。

## 2026-11-16 · M3 联调问题第一轮（beat、概览可见性、Wiki/搜索契约）

### 修改内容

- **Compose 补齐 Celery beat**：新增 `beat` 服务（`celery -A app.worker.celery_app beat`），依赖 `bootstrap` 成功与 `redis` 健康；标准启动流程不再需要手工起 beat，`dispatch_outbox` 周期任务会自动把 Outbox 推进到 worker。
- **`/dashboard/summary` 事件计数按可见性过滤**：`events` 分组统计加入 `visibility_predicate`，与事件列表同口径；`alerts.failed_events` 同步只统计可见事件，不再向 viewer 暴露他人私有事件规模。
- **Wiki `include_inactive` 契约补充**：viewer 在词条/知识块详情传 `include_inactive=true` 返回 403 `FORBIDDEN`（先于存在性判断），行为保持现状；经核对设计只要求 viewer 查看 `active` 内容、非活跃内容属于运维排查能力，未给 viewer 开放，因此不调整实现，仅补契约与回归测试。
- **搜索 `session_id` 约束写入契约**：可省略；传入时必须是调用者可见的已有工作会话（不存在 404、他人会话 403）；按设计不引入自动建会话策略，需要会话关联时先 `POST /working/sessions`。

### 主要文件

- `infra/docker-compose.yml`
- `backend/app/services/memory_reads.py`
- `backend/tests/test_postgres_integration.py`、`backend/tests/test_wiki_sync.py`
- `docs/API_CONTRACT.md`（3.3、3.7、3.8、C-20、新增 C-23）、`docs/CURRENT_WORK.md`

### 验证结果

- `docker compose -f infra/docker-compose.yml config --services`：包含 `beat`。
- `ruff check backend`：通过。
- `pytest backend/tests/test_wiki_sync.py backend/tests/test_memory_api.py -q`：23 passed。
- `RUN_POSTGRES_INTEGRATION=1`：`test_postgres_integration.py` 2 passed（新增 viewer 概览计数断言：active=2、archived=1，Bob 私有事件不计入）。

### 接口契约变更

- 新增 C-23：搜索 `session_id` 必须为已有会话，不自动创建。
- C-20：明确 viewer 传 `include_inactive=true` 返回 403。
- 仪表盘事件计数口径写入第 3.7 节，无字段或路径变更。
- 「纠偏/归档」权限仍为待拍板项，未改动实现。

### 下一步

按前端剩余联调问题继续修复；M4 前完成「纠偏/归档」权限确认。

## 2026-11-16 · M3 联调问题第二轮（beat 镜像依赖与调度文件）

### 修改内容

- **镜像显式声明 `packaging`**：`pyproject.toml` 依赖增加 `packaging>=24,<26`。`kombu 5.7` 的 Redis 传输在运行时会 `import packaging.version`，但未把它声明为依赖，导致干净镜像启动 beat 直接退出；本地 venv 因其它包间接带入而未暴露。
- **beat 调度文件可写**：`backend/Dockerfile` 在构建时创建 `/app/.memory-system`、`/app/state` 并 `chown` 给 uid 10001，命名卷初始化时继承属主；compose 的 `beat` 增加 `--schedule=/app/state/celerybeat-schedule` 与 `beat-state` 卷，不再向 root 属主的 `/app/backend` 写文件。
- **模型目录挂载约定（可行性确认）**：compose 为 `bootstrap`/`api`/`worker` 增加 `"${MODEL_DIR:-../models}:/models:ro"`，`.env.example` 记录“宿主目录挂载到容器 `/models`、容器内模型路径以 `/models/` 开头”的约定。未改动设计文档与 bootstrap 校验逻辑；本机全量启动时把 `EVENT_MODEL_URI` 配成 `/models/...` 即可通过 `_validate_local_files`。

### 主要文件

- `pyproject.toml`、`backend/Dockerfile`、`infra/docker-compose.yml`、`.env.example`
- `docs/CHANGELOG.md`、`docs/CURRENT_WORK.md`

### 验证结果

- `docker build -f backend/Dockerfile -t memory-system-backend:local .`：成功（镜像内 `packaging 25.0`，`import kombu.transport.redis` 通过）。
- 干净启动 beat（无临时补丁）：日志出现 `beat: Starting...`，之后每秒 `Scheduler: Sending due task outbox-dispatch`；`/app/state/celerybeat-schedule` 属主为 `10001:10001`，无 `Permission denied`。
- 同镜像启动 worker：持续消费 `app.tasks.outbox.dispatch_outbox` 并 `succeeded`。
- 真实 Outbox 行验证：插入一条 `wiki.sync.requested` 探针记录，状态由 `pending` 变为 `processed`（`attempt_count=1`），验证后已删除探针行并清理临时容器与卷。
- `docker compose config --services` 含 `beat`；`ruff check backend` 通过。

### 接口契约变更

- 无接口字段或路径变更。

### 下一步

- 与前端确认后，把 `MODEL_DIR`/`/models` 约定补充到启动文档；「纠偏/归档」权限仍待拍板。

## 2026-11-16 · 启动文档补充（模型挂载与校验时机）

### 修改内容

- 新增 [docs/DEPLOYMENT.md](DEPLOYMENT.md)：记录 Compose 启动步骤、`MODEL_DIR` 宿主目录只读挂载到容器 `/models` 的约定、各模型路径必须使用容器内路径、本地/远程两种模式的完整变量示例，以及预检两步校验（`missing_configuration()` 判空 → `_validate_local_files()` 判路径）的触发时机。
- 明确实现现状：`EVENT_MODEL_URI` 含 `://` 只会跳过预检路径检查，运行时 `load_model_package` 仍按本地目录加载并要求 `manifest.json`，因此事件模型必须挂载进容器，不能使用远程 URL。

### 主要文件

- `docs/DEPLOYMENT.md`（新增）、`docs/CURRENT_WORK.md`

### 验证结果

- 内容与 `backend/app/core/config.py`、`backend/app/bootstrap.py`、`backend/app/ml/registry.py` 当前实现逐项核对。

### 接口契约变更

- 无。

## 2026-11-16 · 事件纠偏/归档权限按设计 8.2 收紧到 admin

### 修改内容

- `POST /memory/events/{event_id}/corrections` 与 `DELETE /memory/events/{event_id}` 的路由层从 `require_viewer` 改为 `require_admin`。
- 服务层 `EventCommandService.correct_event`、`delete_event` 增加 admin 校验（`FORBIDDEN` 403），`archive` 与 `purge` 统一为 admin，`purge` 原有行为不变；避免出现重建接口那样“路由声明宽于服务层”的不一致。
- 契约第 2.1 权限矩阵、3.2 纠偏/删除说明、C-05/C-06 同步为 admin 口径；`docs/api/openapi.json` 重新导出。

### 主要文件

- `backend/app/api/v1/memory.py`、`backend/app/services/memory_writes.py`
- `backend/tests/test_memory_api.py`、`backend/tests/test_postgres_integration.py`
- `docs/API_CONTRACT.md`、`docs/api/openapi.json`、`docs/CURRENT_WORK.md`

### 验证结果

- `ruff check backend`：通过。
- `pytest backend/tests/test_memory_api.py -q`：11 passed（新增路由层依赖断言：纠偏/归档均为 `require_admin`）。
- `RUN_POSTGRES_INTEGRATION=1`：2 passed（新增 viewer 纠偏/归档返回 `FORBIDDEN`、admin 纠偏与归档成功）。

### 接口契约变更

- 纠偏、归档权限由 viewer 收紧为 admin，与设计 8.2「修改 admin」一致；清除仍为 admin。

### 下一步

- 输出现状盘点清单（未实现/待完善/已知缺口），待确认后排序推进。

## 2026-11-16 · P0 收尾（工作记忆过期清理、概览字段名对齐）

### 修改内容

- **工作记忆 24 小时过期清理**：`run_maintenance` 新增 `_expire_working_memory`。按 `status=closed AND expires_at<=now` 取会话（受 `maintenance_batch_size` 限制），删除这些会话的 `working_item` 原始条目并把会话状态置为 `expired`；会话元数据、摘要与 `consolidated_event_id` 事件关联保留。`active` 会话与未到期会话不受影响。维护统计新增 `working_sessions_expired`、`working_items_cleared`。
- **概览字段名对齐契约**：`dashboard_summary` 的 `wiki.last_sync_time` 改为契约第 3.7 节规定的 `wiki.last_sync_at`。

### 主要文件

- `backend/app/tasks/maintenance.py`、`backend/app/services/memory_reads.py`
- `backend/tests/test_postgres_integration.py`
- `docs/CHANGELOG.md`

### 验证结果

- `ruff check backend`：通过。
- `pytest backend/tests/test_working_memory.py backend/tests/test_memory_api.py backend/tests/test_services.py -q`：22 passed。
- `RUN_POSTGRES_INTEGRATION=1 pytest backend/tests/test_postgres_integration.py -q`：2 passed。新增断言：过期会话条目计数 0、未过期与活跃会话各保留 1 条、过期会话状态为 `expired` 且摘要保留；`summary.wiki` 仅含 `last_sync_at`，不含 `last_sync_time`。

### 接口契约变更

- 无路径或字段定义变更；实现已与 `docs/API_CONTRACT.md` 第 3.7 节的 `wiki.last_sync_at` 对齐。

### 待前端同步

- `frontend/features/dashboard/DashboardView.tsx` 第 145、203 行仍读取 `summary.wiki.last_sync_time`，字段改名后需前端会话同步；后端会话不改 `frontend/`。

### 下一步

- 镜像档位调整见下一条记录。

## 2026-11-16 · P0 收尾（后端镜像两档：基础档 / ml 档）

### 修改内容

- `backend/Dockerfile` 改为两档构建，通过 `INSTALL_ML` build arg 选择：
  - **基础档（默认，`INSTALL_ML=false`）**：只装基础依赖，镜像精简；事件编码会报 `MODEL_UNAVAILABLE`。
  - **ml 档（`INSTALL_ML=true`）**：额外装 `torch`/`safetensors`/`transformers`，事件编码可用。
- ml 档的 torch 固定为 **CPU-only wheel**：先 `pip install --no-deps` 安装 `torch==2.6.0+cpu`（默认 `TORCH_CPU_VERSION`，来源 `TORCH_CPU_INDEX_URL` 默认 SJTU pytorch-wheels 镜像），再装 `.[ml]`，不引入 cudnn/cublas 等 CUDA 运行时。取 2.6 的原因：`transformers 5.23` 要求 torch>=2.6；`pyproject.toml` 的 `torch>=2.4` 约束仍满足。
- 所有 ML 依赖在构建期落盘，容器启动不下载、可离线运行。
- `infra/docker-compose.yml` 显式传 `INSTALL_ML`（缺省 `true`，即 Compose 默认 ml 档，保证事件写入能完成编码）和 `TORCH_CPU_INDEX_URL`。
- `.env.example`、`docs/DEPLOYMENT.md` 补充两档说明与构建命令。

### 主要文件

- `backend/Dockerfile`、`infra/docker-compose.yml`、`.env.example`
- `docs/DEPLOYMENT.md`、`docs/CHANGELOG.md`、`docs/CURRENT_WORK.md`

### 验证结果

- `docker build -f backend/Dockerfile -t memory-system-backend:base .`（默认档）：成功，325MB；`import app.main` 正常；`import torch` 报 `ModuleNotFoundError`。
- `docker build -f backend/Dockerfile --build-arg INSTALL_ML=true -t memory-system-backend:ml .`：成功，1.73GB。
- 干净 ml 容器内：`torch 2.6.0+cpu`（`torch.version.cuda is None`）、`safetensors 0.8.0`、`transformers 5.23.0`，`transformers.utils.is_torch_available()` 为 `True`；`pip list` 中 nvidia 包计数为 0。
- 干净 ml 容器内跑通完整事件编码链路：合成数据训练 → 写出模型包 → `load_model_package`/`load_model_weights` → `LSTMEventEncoder.encode`，输出维度 4、L2 范数 1.0（`ML_CHECK_OK`）。

### 接口契约变更

- 无。

### 备注

- 构建期间曾临时把 `infra/pip.ini` 改为清华单源以绕过阿里云对大文件的限速（torch 仅 22 kB/s），**已还原为原样**（清华主源 + 阿里云备源）；仓库无临时态残留。
- 构建过程中的日志与验证脚本均放在工作区外，已清理临时日志。

## 2026-11-17 · M4 安全与观测

### 修改内容

- **入口防护**：新增 `SecurityMiddleware`，统一追加 `X-Content-Type-Options`、`X-Frame-Options`、`Referrer-Policy`、`Permissions-Policy`、`Content-Security-Policy`、`Cross-Origin-Opener-Policy`、`Cross-Origin-Resource-Policy`；`HSTS_MAX_AGE_SECONDS>0` 时追加 HSTS。请求体默认上限 1 MiB，超过返回 413 `PAYLOAD_TOO_LARGE`。
- **速率限制**：`POST /memory/search` 60 次/分、`POST /memory/feedback` 120 次/分、`POST /wiki/sync-tasks` 10 次/时，按登录用户/操作员计数；Redis 固定窗口，Redis 不可用时退回进程内计数。超限返回 429 `RATE_LIMITED` 与 `Retry-After`。
- **CSRF**：未引入。认证为 `Authorization` Bearer JWT，无 Cookie 会话；设计前提变化时需重新评估。
- **观测**：新增 `GET /metrics`（不在 `/api/v1` 下），暴露请求量、时延直方图、5xx、限流拒绝、事件处理结果、Outbox 派发结果与积压/死信 Gauge；每次抓取刷新 Outbox 聚合指标。新增 OTel OTLP HTTP 链路导出与 FastAPI 自动插桩，仅当 `OTEL_EXPORTER_OTLP_ENDPOINT` 非空时启用。
- **就绪探针**：`/health/ready` 增加 `models` 检查与 `model_checks` 明细；本地模型包只做目录/清单/权重/版本校验，远程模型用 2 秒短超时探测可达性；结果默认缓存 30 秒，不在启动时预热。
- **Outbox 死信运维**：新增 admin 级 `GET /outbox/dead-letters` 与 `POST /outbox/dead-letters/{outbox_id}/replay`；重放仅接受 `dead_letter`，重置为 `pending`/`attempt_count=0` 并写审计日志。与业务 `/jobs` 明确区分。
- **文档对齐**：设计文档 8.4 的 Wiki 非 active 可见性由“仅管理员”改为 operator 及以上；契约 C-20 改为“已与设计对齐”，权限代码未改。

### 主要文件

- `backend/app/core/security_middleware.py`、`backend/app/core/rate_limit.py`、`backend/app/core/metrics.py`、`backend/app/core/tracing.py`
- `backend/app/api/metrics.py`、`backend/app/api/v1/outbox.py`、`backend/app/schemas/outbox.py`
- `backend/app/services/readiness.py`、`backend/app/services/outbox.py`、`backend/app/api/v1/health.py`
- `backend/app/core/config.py`、`backend/app/core/errors.py`、`backend/app/core/middleware.py`、`backend/app/main.py`
- `pyproject.toml`、`.env.example`、`docs/API_CONTRACT.md`、`docs/DEPLOYMENT.md`、`docs/api/openapi.json`
- `backend/tests/test_security_observability.py`、`backend/tests/conftest.py`、`backend/tests/test_postgres_integration.py`

### 验证结果

- `ruff check backend`：All checks passed。
- `pytest backend/tests -q`：110 passed、2 skipped（未启用 PostgreSQL 集成）。
- `RUN_POSTGRES_INTEGRATION=1 pytest backend/tests/test_postgres_integration.py -q`：两项集成测试均已通过（首轮首项因 testcontainers 启动 PostgreSQL 超时失败，重试通过；另一项通过）。
- 另用本机 Compose PostgreSQL 对 Outbox 死信列表、重放与非死信 409 做了独立校验（输出 `OUTBOX_PG_OK`）。
- `pytest backend/tests/test_security_observability.py -q`：11 passed，覆盖安全头、413、429/Retry-After、限流关闭、`/metrics` 与 token、就绪缓存与模型检查。
- OpenAPI 导出成功：31 条路径。

### 接口契约变更

- `/health/ready` 的 `checks` 新增 `models`，响应新增 `model_checks`（前端如整体展示 checks 需容纳新键）。
- 新增 `GET /metrics`、`GET /outbox/dead-letters`、`POST /outbox/dead-letters/{id}/replay`。
- 错误码新增 `PAYLOAD_TOO_LARGE`；限流返回 429 `RATE_LIMITED`。

### 已知限制与待拍板

- `/metrics` 默认不鉴权，依赖部署层限制来源；是否默认强制 `METRICS_TOKEN` 待用户拍板。
- 本地模型就绪检查只校验文件与清单，不加载权重，避免探针拖慢；实际可加载性仍由启动预检与运行时加载验证。
- 前端 `DashboardView.tsx` 仍读 `wiki.last_sync_time`，需前端会话改为 `last_sync_at`（本会话不改 `frontend/`）。

## 2026-11-17 · 实体重建权限收紧到 admin

### 修改内容

- `POST /entities/{entity_id}/rebuild` 路由层由 `require_operator` 改为 `require_admin`。
- `MemoryReadService.request_rebuild` 服务层同步改为只允许 `Role.ADMIN`，与设计 8.2 及事件纠偏/归档口径一致。
- 契约 3.6 权限表与 C-07 同步为 admin，并删除“operator 及以上”的旧表述。
- 测试改为 admin 放行、operator 与 viewer 403，路由层依赖断言保留。

### 验证结果

- `ruff check backend` 通过。
- `pytest backend/tests -q`：110 passed、2 skipped；`RUN_POSTGRES_INTEGRATION=1` 重建流程测试 1 passed（73.86s）。
- `scripts/export_openapi.py` 重新导出。

## 2026-11-17 · AG-07 质量评估基线

### 修改内容

- 新增独立评估区 `evaluation/`，不修改 `backend/` 生产代码；仅复用 `app.domain`
  的排序、衰减、熟悉度与意图纯函数保持口径一致。
- 新增确定性数据集构造器：51 条事件、8 篇 Wiki / 24 个知识块、34 条带多级
  相关性标注的查询，覆盖 event/wiki/mixed 意图与 time_sensitive/cold_start/
  long_sequence 切片；ID 使用 UUIDv5，重复生成逐字节一致。
- 新增指标库（Recall/Precision/Hit/MRR/MAP/nDCG + bootstrap 95% CI）、
  词法哈希向量与 IDF 词法精排代理、离线执行器与消融配置。
- 新增在线评估口径与只读脚本，基于 `memory_query` / `memory_access_log`
  计算 CTR@10、采纳率、有帮助率、反馈 nDCG@10、零结果率、候选规模与
  反馈去重原因分布。
- 新增 `evaluation/README.md`、`results/baseline.json`、`results/REPORT.md`
  与 `evaluation/tests/test_evaluation.py`；`run_all.ps1` 一条命令重跑
  数据集、评估与测试。
- 追加更新 `docs/CURRENT_WORK.md`，未重写既有段落。

### 主要文件

- `evaluation/dataset.py`、`evaluation/embedding.py`、`evaluation/metrics.py`
- `evaluation/runner.py`、`evaluation/report.py`、`evaluation/online.py`
- `evaluation/cli.py`、`evaluation/run_all.ps1`、`evaluation/README.md`
- `evaluation/data/eval_dataset.json`、`evaluation/results/baseline.json`、
  `evaluation/results/REPORT.md`
- `evaluation/tests/test_evaluation.py`、`docs/CURRENT_WORK.md`

### 验证结果

- `ruff check evaluation`：All checks passed。
- `pytest evaluation/tests -q`：8 passed。
- `python -m evaluation.cli all`：数据集、`baseline.json`、`REPORT.md` 均生成。
- 第一版基线（词法代理，非发布结论）：事件 nDCG@10 79.1% / Recall@10 91.7%；
  Wiki nDCG@10 68.8% / Recall@10 73.1%；统一搜索 nDCG@10 74.3% /
  Recall@10 84.3%；自动意图准确率 24.0%；门禁 blocked。

### 已知限制与下一步

- 未配置真实 BGE Embedding 与事件模型包，无法执行设计 4.4 的发布门槛；
  当前数字仅作为下限基线与回归护栏。
- 词法代理阈值按自身余弦尺度校准；生产阈值 0.15 下的候选召回单独报告。
- 下一步：配置真实模型后用 `--embedding real` 复测；扩展冷启动/长序列样本；
  按报告第 8 节修复意图分类、Wiki 候选召回与实体别名。

## 2026-11-17 · DOC-01 新机器迁移清单

### 修改内容

- 新增 `docs/MIGRATION_CHECKLIST.md`：面向“迁到另一台机器”的从零部署清单，
  以仓库现状为准，覆盖必须带走的资产、模型目录结构与挂载、`.env` 两种模式的
  必填项、两档镜像与离线 `docker save/load`、端口与启动顺序、可照敲的验证
  清单、常见失败处置与迁移完成判定。
- `docs/DEPLOYMENT.md` 顶部增加指向该清单的入口。
- `docs/CURRENT_WORK.md` 记录本次文档交付。

### 主要文件

- `docs/MIGRATION_CHECKLIST.md`（新增）
- `docs/DEPLOYMENT.md`
- `docs/CURRENT_WORK.md`

### 验证结果

- 文档内容与当前代码核对：`missing_configuration()`、`_validate_local_files()`、
  `app.ml.registry.load_model_package()`、`app.worker` 的 beat 调度（1 秒）、
  `infra/docker-compose.yml` 的端口与依赖、`frontend/lib/auth` 的生产模式限制。
- 镜像体积取自本机 `docker images`：基础档 325 MB、ml 档 1.73 GB。

### 已知限制

- 清单不提供模型文件，也不自动下载；`EVENT_MODEL_URI` 仍只支持本地目录。
- 前端 3100 需要额外指定（Compose 的 `web` 固定 3000），且生产构建下前端
  Dev Auth 不生效。

## 2026-11-17 · DOC-02 模型获取、第三方模型对接与项目 README

### 修改内容

- `docs/MIGRATION_CHECKLIST.md` 新增 1.2.1：`bge-large-zh-v1.5` 与
  `bge-reranker-v2-m3` 的 HuggingFace / ModelScope 下载命令、目录对应关系、
  完整性验证、容器内挂载检查、无网目标机的拷贝方式。
- `docs/MIGRATION_CHECKLIST.md` 新增第 9 节：按代码现状说明第三方
  Embedding / Reranker 对接，包括 OpenAI 兼容 `/embeddings` 的请求/响应字段、
  Bearer 鉴权、超时与重试、错误映射、维度校验与旧数据重建、`/rerank` 三种
  响应形态、`RERANKER_ENABLED=false` 的降级、不能远程替代的能力、换供应商
  最小改动清单，并写明当前实际只支持 1024 维、事件无批量重编码接口。
- `docs/DEPLOYMENT.md` 新增 3.1（模型下载）与 4.3（第三方模型对接摘要），
  细节指向迁移清单。
- 新增仓库根 `README.md`：项目定位、架构与写入到检索的数据流、目录职责、
  快速开始（与部署/迁移文档一致）、配置分组、接口概览、权限模型、前端、
  AG-07 评估定位、测试命令、已知限制与文档索引。

### 主要文件

- `README.md`（新增）
- `docs/MIGRATION_CHECKLIST.md`
- `docs/DEPLOYMENT.md`
- `docs/CHANGELOG.md`
- `docs/CURRENT_WORK.md`

### 验证结果

- 远程 Embedding / Reranker 的接口形态、字段、鉴权、超时、重试与错误码取自
  `backend/app/ml/embedding.py`、`backend/app/ml/reranker.py`、
  `backend/app/services/remote_checks.py`，不是按设计文档推测。
- 维度与存储取自模型定义：`wiki_chunk.embedding` 为 `vector(1024)`、
  `event_memory.embedding` 为 `vector(256)`；检索未按模型版本过滤。
- README 中的命令与 `docker-compose.yml`、`pyproject.toml`、
  `frontend/package.json`、`evaluation/README.md` 核对。

### 已知限制

- 就绪探针维度常量写死 1024，文档已按现状标注“实际只支持 1024 维”。
- 事件侧没有批量重编码接口，换 Embedding 后的历史向量需另行处理。

## 2026-11-17 · DOC-03 README 补充 LSTM 记忆立意

### 修改内容

- `README.md` 新增第 0 节“会筛选、会遗忘、会固化的记忆”：
  - 0.1 按代码讲清 LSTM 事件编码器：16 条事件窗口、1024 维基础向量、
    `Linear(1024→256)` 投影、单层 LSTM（hidden 256）、mask 逐步更新、
    最后有效步输出归一化为 256 维事件向量、`(h, c)` 持久化到实体快照、
    模型版本不一致触发重建、查询侧复用同一编码器。
  - 0.2 说明存储层没有门控：遗忘由每日维护、衰减、自动归档、归档后清除、
    工作记忆过期、人工归档/清除与纠偏覆盖实现，并给出默认阈值。
  - 明确区分“模型层门控”与“记忆层维护策略”，修正“LSTM 只是隐喻”的错误印象。
- 修正 README 已知限制：`s3://` 在执行同步时返回 422；`superseded` 是 Wiki
  知识块历史版本；编码失败不会把事件置为 `failed`。

### 主要文件

- `README.md`
- `docs/CHANGELOG.md`
- `docs/CURRENT_WORK.md`

### 验证结果

- LSTM 结构、维度、mask 与状态持久化核对自
  `backend/app/ml/event_encoder.py`、`backend/app/ml/query_encoder.py`、
  `backend/app/services/event_memory.py`、`backend/app/models/state.py`。
- 遗忘机制核对自 `backend/app/tasks/maintenance.py`、
  `backend/app/domain/lifecycle.py`、`backend/app/services/memory_writes.py`
  与 `backend/app/core/config.py` 的默认值。










