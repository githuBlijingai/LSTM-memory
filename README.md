# Memory System

面向个人/小团队的**分层记忆系统**。事件先由 LSTM 事件编码器编码成向量，再逐层沉淀为实体状态与 Wiki 知识；检索时按意图把三层结果融合起来。

- 事件（Event）：发生了什么，原子记录，带时间、主体、重要度、可见性。
- 实体（Entity）：围绕某个人/项目/主题，事件如何累积成状态（熟悉度、衰减、摘要）。
- Wiki：从事件中沉淀出的稳定知识条目，按块索引。

三层分开存、分开更新。事件保持事实性，实体负责“现在怎么样”，Wiki 负责“长期是什么”。详细设计见 [设计文档](../基于LSTM的分层记忆系统设计文档V1.2.md) 第 6 章。

---

## 0. 会筛选、会遗忘、会固化的记忆

只增不减的记忆最终等于没有记忆：半年前的每句闲聊和昨天的关键决策权重一样，检索会被噪声淹没，隐私内容也永远删不干净。所以这套系统有两个不同层面的“记忆”机制，必须分开看：

- **模型层**：LSTM 事件编码器真实存在，门控结构在单元内部逐时间步运算，决定当前事件如何写入隐状态、历史保留多少、输出什么向量。
- **记忆层**：数据库里**没有**门控单元。条目何时衰减、归档、过期、清除，由后台维护任务和策略规则决定，不是 LSTM 门的直接输出。

### 0.1 模型层：LSTM 事件编码器

事件不是直接存一个文本向量就完事。每条事件进入编码器时，会带上一段实体历史：

1. 取该实体最近 `EVENT_WINDOW_SIZE`（默认 16）条 `active` 事件，加上当前事件，按时间排序。
2. 每条事件的文本先由基础编码器（本地 BGE 或远程 Embedding）编码成 1024 维基础向量。
3. 基础向量经 `Linear(1024 → 256)` 投影后，逐个时间步送入单层 `nn.LSTM(input_size=256, hidden_size=256, num_layers=1)`。
4. 最后一个有效时间步的 hidden 输出做 L2 归一化，得到 256 维事件向量，写入 `event_memory.embedding`。
5. 同步得到新的 `(h, c)`，形状均为 `(1, 256)`，序列化后存进 `entity_state_snapshot` 的 `h_tensor` / `c_tensor`；下一条事件从这份快照恢复初始状态，而不是从零开始。

门控在这里是真实运算，对应关系如下：

| LSTM 单元内部 | 在本系统里的作用 |
|---|---|
| 输入门 | 控制当前事件经投影后的信息有多少写入细胞状态 |
| 遗忘门 | 控制上一条事件留下的细胞状态保留多少，决定实体历史被继承还是淡化 |
| 输出门 | 控制细胞状态有多少暴露为 hidden 输出；最后一个有效步的 hidden 就是事件向量 |
| 细胞状态 `c` | 实体的历史积累，随快照持久化，下一条事件继续承接 |
| 隐状态 `h` | 当前时间步的压缩表示，同时作为事件向量来源 |

几个实现细节：

- 窗口左侧用 `mask=0` 补齐；编码器逐步更新 `h/c` 时，`mask=0` 的位置不推进状态（`h = next_h * valid + h * (1 - valid)`），不会为不存在的历史伪造状态。
- 事件向量不是随机初始化模型算出来的：模型包必须带已训练权重，生产路径禁止 `allow_untrained`。
- 模型包包含 `manifest.json`（`model_version`、`base_embedding_dim`、`input_dim`、`hidden_dim`、`num_layers`、`weights_sha256`）与 `weights.safetensors`；加载时校验版本、维度与 SHA-256，`strict=True` 载入，结构不匹配直接拒绝。
- 实体快照记录了 `model_version`。事件模型升级后，旧快照不会硬接着用：该实体会转 `rebuilding` 并创建重建任务，按新模型重放历史。
- 查询侧复用同一个编码器（`QueryAdapter`）：查询基础向量经同样的投影与 LSTM，以零初始状态单步编码，得到同空间的 256 维查询向量。
- 训练与导出在 `backend/training/`，产物就是上述模型包；容器内已验证过“训练 → 保存 → 加载 → 编码”全程。

### 0.2 记忆层：遗忘由维护任务实现

LSTM 的门只作用于一次编码，不会去删数据库里的行。真正让记忆“会忘”的是这些机制：

| 机制 | 实现 | 默认值 / 触发条件 |
|---|---|---|
| 事件衰减 | 每条事件计算 `decay_score` | `DECAY_LAMBDA=0.01`，按事件时间指数衰减 |
| 熟悉度衰减 | 实体熟悉度按天衰减 | `FAMILIARITY_GAMMA=0.003` |
| 新记忆保护 | 保护期内不自动归档 | `NEW_MEMORY_PROTECT_HOURS=48` |
| 自动归档 | 每日维护任务判断 `delete_score < 0.1` 且不在保护期、无待处理纠偏/重建/合规冻结 | 每天 `MAINTENANCE_UTC_HOUR`（默认 02:00 UTC） |
| 归档后清除 | 归档满 `ARCHIVE_RETENTION_DAYS` 后转 `purged`，清空正文与向量，只留墓碑 | 默认 30 天 |
| 工作记忆过期 | 已关闭且到期的会话清理 | `WORKING_SESSION_TTL_HOURS=24` |
| 人工遗忘 | `DELETE /memory/events/{id}` 的 `archive` / `purge` | admin |
| 纠偏覆盖 | 纠偏生成新事件，旧事件转 `archived` 并触发实体重建 | admin |

所以准确的对应关系是：

- **输入门 / 遗忘门 / 输出门**：LSTM 单元内部的真实计算，负责“这条事件怎么和历史一起被编码”。
- **衰减、归档、清除、过期**：后台任务与策略，负责“这条记忆还能留多久、以什么形态留”。
- 两者通过数据衔接：被归档/清除的事件不再进入编码窗口与召回，实体的 `(h, c)` 快照也会随重建任务按新的事件集合重算。

需要避免的误解：

- 数据库里没有跑着一个 LSTM 在“自动忘记”；遗忘是定时任务和状态机。
- LSTM 的输入门不是写入准入。事件写入由 API 校验与权限决定；编码失败时事件保持 `pending`，失败记录落在 Outbox / BackgroundJob 上（重试到上限后进死信），不是被模型按“价值低”拒收。
- `superseded` 是 Wiki 知识块的历史版本状态，不是事件遗忘；事件侧的覆盖走 `archived`。
- 检索时的意图分类、可见性过滤与精排是检索策略，可以类比“输出门选择输出什么”，但不是 LSTM 门控本身。

一句话：**模型层决定怎么记，维护层决定记多久**。下面先看整体架构与写入到检索的完整路径。

---

## 1. 架构与数据流

```text
浏览器
  │
  ▼
Next.js 前端（3000 / 3100）── 服务端代理 /api/v1/* ──► FastAPI（8000）
                                                          │
              ┌───────────────────────────────────────────┼──────────────────────┐
              ▼                                           ▼                      ▼
     PostgreSQL + pgvector                        Redis（broker + 限流）     Prometheus /metrics
              ▲                                           │
              │                                           ▼
        Celery worker ◄──── Celery beat（每秒派发 Outbox）
```

### 写入一条事件到可被检索

1. `POST /api/v1/memory/events` 在同一事务里写 `event_memory`（状态 `pending`）和一条 `outbox_event`，返回 202。
2. `beat` 每秒执行 `app.tasks.outbox.dispatch_outbox`，把待处理 Outbox 投递给 Celery。
3. `worker` 执行事件流水线：
   - 用基础编码器（本地 BGE 或远程 Embedding）编码标题/正文；
   - 用本地 LSTM 事件模型结合实体历史状态，算出 256 维事件向量与新的实体状态；
   - 写事件向量、序列号、`event_model_version`，状态转 `active`；
   - 生成实体状态快照，更新实体摘要与最后事件。
4. 搜索时：查询文本先经基础编码器 + 同一个 LSTM 得到查询向量，分别召回事件与 Wiki 块，可选 Reranker 精排，再按意图权重、重要度、衰减、熟悉度融合排序。

Outbox 处理失败会重试，超过上限进入死信，可用 `GET /api/v1/outbox/dead-letters` 查询、`POST .../{id}/replay` 重放（admin）。

---

## 2. 目录结构

```text
memory-system/
├── backend/
│   ├── app/
│   │   ├── api/v1/        # REST 路由：events/search/working/entities/jobs/wiki/outbox/health
│   │   ├── core/          # 配置、安全、错误、中间件、限流、指标、追踪
│   │   ├── domain/        # 纯函数：意图、融合排序、衰减、熟悉度
│   │   ├── ml/            # Embedding/Reranker/事件模型加载与远程调用
│   │   ├── models/        # SQLAlchemy 模型与枚举
│   │   ├── repositories/  # 查询、游标分页、权限谓词
│   │   ├── schemas/       # Pydantic 请求/响应
│   │   ├── services/      # 业务编排：写入、搜索、Outbox、Wiki 同步、就绪检查
│   │   ├── tasks/         # Celery 任务：Outbox 派发、事件流水线、Wiki 同步
│   │   ├── bootstrap.py   # 一次性预检
│   │   ├── main.py        # FastAPI 应用
│   │   └── worker.py      # Celery 应用与 beat 调度
│   ├── alembic/           # 数据库迁移
│   ├── tests/             # 单元 + 集成测试
│   └── training/          # 事件模型训练/导出/评估
├── frontend/              # Next.js 16 + React 19 + Ant Design
├── evaluation/            # AG-07 离线评估（确定性数据集与指标）
├── infra/                 # docker-compose、Dockerfile 依赖配置、pip 源
├── docs/                  # 部署、契约、迁移、变更记录
└── scripts/               # 安装、OpenAPI 导出等辅助脚本
```

---

## 3. 快速开始

### 3.1 前置

- Docker Desktop（Compose v2）。
- 模型资产：`bge-large-zh-v1.5`、事件模型包；启用精排时再加 `bge-reranker-v2-m3`。仓库不含模型，获取方式见 [MIGRATION_CHECKLIST.md](docs/MIGRATION_CHECKLIST.md) 第 1.2.1 节。
- 宿主机 Python 3.12（只有跑测试/训练/评估时需要）。
- Node 20.9+（只有本地起前端时需要；Next 16 要求）。

### 3.2 选镜像档

| 档位 | `INSTALL_ML` | 用途 |
|---|---|---|
| 基础档 | `false` | 只跑 API/迁移/测试，事件编码不可用 |
| ml 档 | `true`（Compose 默认） | 正常使用，事件编码可用 |

要验证写入→编码→搜索，必须用 ml 档。

### 3.3 最少 `.env`

```powershell
Copy-Item .env.example .env
```

改到下面这些值即可跑通本地栈：

```dotenv
ENVIRONMENT=development
DEV_AUTH_ENABLED=true

POSTGRES_PASSWORD=change-me
POSTGRES_PORT=55432
REDIS_PORT=56379
INSTALL_ML=true

MODEL_DIR=D:/models
EMBEDDING_PROVIDER=local
EMBEDDING_MODEL_NAME=/models/bge-large-zh-v1.5
EMBEDDING_MODEL_VERSION=bge-large-zh-v1.5-v1
RERANKER_ENABLED=false
EVENT_MODEL_URI=/models/event-model
EVENT_MODEL_VERSION=lstm-v1
MODEL_DEVICE=cpu
ALLOW_CPU_MODELS=true
```

- 有 reranker 时设 `RERANKER_ENABLED=true` 并补 `RERANKER_PROVIDER=local`、`RERANKER_MODEL_NAME=/models/bge-reranker-v2-m3`。
- 模型变量必须写**容器内路径**（`/models/...`）；`MODEL_DIR` 是宿主路径。
- 完整变量与两种模式见 [DEPLOYMENT.md](docs/DEPLOYMENT.md) 第 3–4 节。

### 3.4 启动

```powershell
docker compose --env-file .env -f infra/docker-compose.yml up -d
docker compose --env-file .env -f infra/docker-compose.yml ps -a
```

`bootstrap` 是一次性预检，必须 `exited (0)` 后 `api`/`worker`/`beat` 才会启动。`api` 启动时会自动执行 `alembic upgrade head`。

本清单示例端口：API `8000`、Web `3000`、Nginx `8080`、Postgres `55432`、Redis `56379`（后两者由 `.env` 覆盖）。

### 3.5 确认成功

```powershell
$Base = "http://127.0.0.1:8000"
$Admin = @{ Authorization = "Dev admin"; "X-Dev-Roles" = "admin" }

# 1) 就绪：configuration / database / redis / models 全为 true
Invoke-RestMethod "$Base/api/v1/health/ready"

# 2) 写一条事件（返回 202，初始 pending）
$body = @{
  schema_version = 1; subject = "alice"; entity_type = "topic"; event_type = "meeting"
  event_time = (Get-Date).ToUniversalTime().ToString("o")
  title = "发布计划评审"; content = "决定 12 月 1 日发布 1.0，先灰度 10%。"
  importance = 0.8; visibility = "private"
} | ConvertTo-Json
$created = Invoke-RestMethod -Method Post -Uri "$Base/api/v1/memory/events" `
  -Headers ($Admin + @{ "Idempotency-Key" = [guid]::NewGuid().ToString() }) `
  -ContentType "application/json" -Body $body

# 3) 等 beat/worker 处理完，状态应为 active
Invoke-RestMethod "$Base/api/v1/memory/events/$($created.data.event_id)" -Headers $Admin

# 4) 能被搜到
Invoke-RestMethod -Method Post -Uri "$Base/api/v1/memory/search" -Headers $Admin `
  -ContentType "application/json" `
  -Body (@{ query = "发布计划 灰度"; memory_type = "event"; top_n = 10 } | ConvertTo-Json)
```

完整验证清单（含 `/metrics`、死信、前端页面）见 [MIGRATION_CHECKLIST.md](docs/MIGRATION_CHECKLIST.md) 第 6 节。

### 3.6 起前端

```powershell
cd frontend
Copy-Item .env.example .env.local   # 确认 BACKEND_URL 指向后端
npm ci
npm run dev -- --port 3100
```

打开 `http://127.0.0.1:3100`。Compose 的 `web` 固定 3000；要 3100 时用上面的开发模式，或复用镜像 `docker run -p 3100:3000`。

---

## 4. 配置说明

完整清单见 [.env.example](.env.example) 与 [DEPLOYMENT.md](docs/DEPLOYMENT.md)。

### 4.1 基础设施

| 变量 | 默认 | 说明 |
|---|---|---|
| `DATABASE_URL` | 容器内由 Compose 注入 | 宿主直跑后端时必填 |
| `REDIS_URL` | 容器内由 Compose 注入 | 宿主直跑后端时必填 |
| `POSTGRES_PASSWORD` | `change-me` | 生产必须改 |
| `POSTGRES_PORT` / `REDIS_PORT` | 5432 / 6379 | 本机有冲突时改 |
| `INSTALL_ML` | `true` | Compose 镜像档位 |

### 4.2 模型（必填）

| 变量 | 说明 |
|---|---|
| `MODEL_DIR` | 宿主模型根目录，只读挂载到 `/models` |
| `EVENT_MODEL_URI` | 事件模型包容器内路径，**只支持本地目录** |
| `EVENT_MODEL_VERSION` | 必须与模型包 `manifest.json` 一致 |
| `EMBEDDING_PROVIDER` | `local` / `remote` |
| `EMBEDDING_MODEL_NAME` | 本地目录或远程模型名 |
| `EMBEDDING_MODEL_VERSION` | 向量版本标识，必填 |
| `RERANKER_ENABLED` | 关闭后跳过精排 |
| `RERANKER_PROVIDER` / `RERANKER_MODEL_NAME` | 开启时必填 |
| `BASE_EMBEDDING_DIM` | 默认 1024；当前实现实际只支持 1024 |

### 4.3 认证与安全

| 变量 | 默认 | 说明 |
|---|---|---|
| `ENVIRONMENT` | `development` | `production` 时禁用 Dev Auth、要求 OIDC |
| `DEV_AUTH_ENABLED` | `false` | 本地登录开关 |
| `OIDC_ISSUER` / `OIDC_AUDIENCE` / `OIDC_JWKS_URL` | 空 | 生产必填 |
| `RATE_LIMIT_ENABLED` | `true` | 搜索 60/分、反馈 120/分、同步 10/时 |
| `MAX_REQUEST_BODY_BYTES` | 1048576 | 超限 413 |
| `SECURITY_HEADERS_ENABLED` | `true` | CSP/X-Frame-Options 等 |

### 4.4 观测

| 变量 | 默认 | 说明 |
|---|---|---|
| `METRICS_ENABLED` | `true` | 关闭后 `/metrics` 404 |
| `METRICS_TOKEN` | 空 | 空表示不鉴权 |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | 空 | 非空才启用链路导出 |
| `READINESS_MODEL_CACHE_SECONDS` | 30 | 就绪探针模型检查缓存 |
| `READINESS_MODEL_TIMEOUT_SECONDS` | 2.0 | 远程可达性探测超时 |

---

## 5. 接口概览

所有业务接口在 `/api/v1` 下；除搜索外的写操作要求 `Idempotency-Key` 请求头。

| 分组 | 主要接口 |
|---|---|
| 健康 | `GET /health/live`、`GET /health/ready` |
| 事件 | `POST/GET /memory/events`、`GET/DELETE /memory/events/{id}`、`POST .../corrections`、`POST .../restore` |
| 检索 | `POST /memory/search`、`POST /memory/feedback` |
| 工作记忆 | `POST /working/sessions`、`POST/DELETE /working/sessions/{id}`、`POST .../items`、`POST .../consolidate` |
| 实体 | `GET /entities`、`GET /entities/{id}`、`GET .../state`、`GET .../events`、`GET .../replays`、`POST .../rebuild` |
| 任务 | `GET /jobs/{id}`、`POST /jobs/{id}/retry`、`GET /dashboard/summary` |
| Wiki | `GET /wiki/articles`、`GET /wiki/articles/{id}`、`GET .../chunks/{index}`、`GET/POST /wiki/sync-tasks`、`POST .../retry` |
| 运维 | `GET /outbox/dead-letters`、`POST /outbox/dead-letters/{id}/replay`、`GET /metrics` |

字段、错误码与权限细节以 [API_CONTRACT.md](docs/API_CONTRACT.md) 和 [openapi.json](docs/api/openapi.json) 为准。

---

## 6. 权限模型

认证用 JWT Bearer；本地开发用 `Authorization: Dev <subject>` + `X-Dev-Roles`。三档角色：

| 角色 | 能做 |
|---|---|
| `viewer` | 写入/查看自己的事件、搜索、反馈、管理自己的工作记忆会话；读实体、Wiki 公开内容、自己的任务、概览 |
| `operator` | viewer 全部；创建/重试 Wiki 同步任务；查看他人任务；查看非 active 的 Wiki 内容 |
| `admin` | operator 全部；事件纠偏/归档/恢复、实体重建、熟悉度调整、Outbox 死信查询与重放；查看所有工作记忆会话 |

权限在路由依赖与服务层双重校验；越权返回 403 `FORBIDDEN`。实体重建、事件纠偏/归档只允许 admin，与设计 8.2 一致。

---

## 7. 前端

技术栈：Next.js 16 App Router、React 19、Ant Design 6、TanStack Query、ECharts。

页面：

| 路径 | 内容 |
|---|---|
| `/dashboard` | 概览：事件/实体/Wiki/任务数量与积压 |
| `/memories/events`、`/memories/events/{id}` | 事件列表与详情 |
| `/entities`、`/entities/{id}` | 实体列表、状态与事件时间线 |
| `/search` | 检索与反馈 |
| `/wiki`、`/wiki/{articleId}` | Wiki 列表与详情 |
| `/jobs`、`/jobs/wiki-sync` | 后台任务与同步任务 |

所有请求由 Next.js 服务端代理到 `BACKEND_URL`，浏览器不直接跨域访问后端。

开发与构建：

```powershell
npm run dev -- --port 3000     # 开发
npm run build                  # 生产构建
npm run start -- --port 3000   # 生产启动
```

Dev Auth 生效条件：`NODE_ENV !== "production"` **且** `DEV_AUTH_ENABLED=true`，同时设置 `DEV_AUTH_SUBJECT` 与 `DEV_AUTH_ROLES`。生产构建下即使开启也不会生效，必须接真实 OIDC。

---

## 8. 评估（AG-07）

`evaluation/` 是独立评估区，复用 `app.domain` 的排序/衰减/熟悉度纯函数，保证评估口径与运行期一致。用途是**回归护栏**：改检索或排序后跑同一份确定性数据集，看指标有没有退化。

```powershell
.\evaluation\run_all.ps1
# 等价
.\.venv312\Scripts\python.exe -m evaluation.cli all
.\.venv312\Scripts\python.exe -m pytest evaluation/tests -q
```

- 数据集：34 条查询、51 条事件、24 个 Wiki 块，UUIDv5 确定性生成。
- 产物：`evaluation/results/baseline.json`、`evaluation/results/REPORT.md`。
- 当前基线（词法代理：hash embedding + 词法精排）：Recall@10 0.451、Hit@10 0.625、MRR@10 0.625、nDCG@10 0.597。
- **定位说明**：这是缺少真实模型资产时的下界代理，真实模型档的验收门槛仍未通过，不能当作发布结论；只用于同口径版本间对比。

---

## 9. 测试与质量

```powershell
# 后端静态检查
ruff check backend

# 后端单元测试
.\.venv312\Scripts\python.exe -m pytest backend/tests -q

# PostgreSQL 集成测试（需要 Docker，较慢）
$env:RUN_POSTGRES_INTEGRATION = "1"
.\.venv312\Scripts\python.exe -m pytest backend/tests/test_postgres_integration.py -q
```

```powershell
# 前端
cd frontend
npm run lint
npm run typecheck
npm test
npm run build
npm run test:e2e      # 需要 Playwright 浏览器
```

评估测试随 `run_all.ps1` 一起执行，也可单独 `pytest evaluation/tests -q`。

本地后端开发环境：

```powershell
python -m venv .venv312
.\.venv312\Scripts\python.exe -m pip install -e ".[dev,ml]"
Push-Location backend
..\.venv312\Scripts\python.exe -m alembic upgrade head
Pop-Location
```

---

## 10. 已知限制

- **事件模型只能是本地包**：`EVENT_MODEL_URI` 写远程 URL 只能绕过预检，运行期仍按本地目录加载。
- **生产禁止本地 CPU 模型**；本地 CPU 仅用于开发/测试，需 `ALLOW_CPU_MODELS=true`。
- **只支持 1024 维 Embedding**：就绪探针的维度常量写死 1024，换维度需要改代码、迁移向量列、重训事件模型。
- **换 Embedding 供应商不会自动重建旧向量**：检索不按模型版本过滤，混用不同模型的向量结果不可信；Wiki 可用 `mode=reembed`，事件没有批量重编码接口。
- Wiki 同步**不支持 `s3://`**：创建任务不拦截，同步执行时解析数据包会报 422，需先落地本地或 HTTP(S)。
- **增量同步不推断删除**：源文档被删掉时增量模式不会标记删除，需要跑全量同步；全量模式会把缺失文档标记为 deleted。
- **Wiki 知识块保留历史版本（`superseded`），没有定期清理**：旧块不会被自动删除，列表与存储会随同步次数增长。
- **编码失败不会把事件改成 `failed`**：事件保持 `pending`，失败落在 Outbox / BackgroundJob；需要按死信记录人工处理或重放。
- 前端 `/dashboard` 的 Wiki 同步时间仍读 `last_sync_time`，后端契约字段是 `last_sync_at`，前端待改。
- `/metrics` 默认不鉴权，生产需在网关层限制来源或设置 `METRICS_TOKEN`。
- 远程 Embedding/Reranker 只支持 OpenAI 兼容 `/embeddings` 与 `POST /rerank` 两种形态，详见 [MIGRATION_CHECKLIST.md](docs/MIGRATION_CHECKLIST.md) 第 9 节。

---

## 11. 相关文档

- [DEPLOYMENT.md](docs/DEPLOYMENT.md)：启动、模型挂载、两档镜像、入口防护与观测。
- [MIGRATION_CHECKLIST.md](docs/MIGRATION_CHECKLIST.md)：新机器从零部署、离线迁移、模型下载、第三方模型对接、验收清单。
- [API_CONTRACT.md](docs/API_CONTRACT.md)：接口、错误码、权限与契约差异。
- [docs/api/openapi.json](docs/api/openapi.json)：机器可读契约。
- [CHANGELOG.md](docs/CHANGELOG.md)：变更记录。
- [CURRENT_WORK.md](docs/CURRENT_WORK.md)：当前进度、待办与已知问题。
- [evaluation/README.md](evaluation/README.md)：AG-07 评估口径与命令。
