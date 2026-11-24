# 当前实施状态

- 并行分工：本会话负责后端与基础设施（`backend/`、`infra/`、数据库与迁移、Celery），**不再改动 `frontend/`**；前端由另一会话并行开发。契约以 `docs/API_CONTRACT.md` 和 `docs/api/openapi.json` 为准。
- 当前阶段：M4（安全与观测）已完成；M3 联调反馈与 M4 的质量评估/故障恢复验收待推进。
- 已完成：
  - 后端工程骨架、17 表可逆迁移（初始 16 表 + `retrieval_hit_gain`）、认证与基础服务、启动预检、Next.js 路由骨架、容器编排（M0）。
  - 事件写入/纠正/删除/恢复、工作记忆巩固、双路检索与反馈闭环、实体状态/Replay/Rebuild、Job、Dashboard、管理员熟悉度 API（M1）。
  - Wiki 词条/知识块/来源链、全量/增量/重编码同步、同步任务与 Celery 任务、7 条 `/api/v1/wiki/*` 路由（M2）。
  - 联调对齐：新增 `GET /entities/{entity_id}/events`；Dashboard 恢复 viewer+；Wiki 同步任务读取改为 viewer+、搜索参数定为 `q`。
  - 训练脚本、基线评估（Recall@10 / MRR@10 / nDCG@10）、模型包清单与运行期一致性校验（M1）。
  - OpenAPI 导出与前端 TypeScript 类型生成。
  - 新增 [MIGRATION_CHECKLIST.md](MIGRATION_CHECKLIST.md)：新机器从零部署与离线迁移清单（资产清点、模型挂载、`.env` 必填项、两档镜像与 `docker save/load`、启动顺序、验证清单、常见失败）；[DEPLOYMENT.md](DEPLOYMENT.md) 顶部已加入口。
  - 迁移清单补充模型下载（HuggingFace / ModelScope、目录对应、验证、离线拷贝）与第三方 Embedding/Reranker 对接（按代码现状：OpenAI 兼容 `/embeddings`、`POST /rerank`、重试与错误映射、维度与旧数据重建限制）；部署文档新增 3.1、4.3 两节。
  - 新增仓库根 [README.md](../README.md)：项目定位、架构与数据流、目录职责、快速开始、配置分组、接口概览、权限模型、前端、AG-07 评估定位、测试命令与已知限制。
  - README 补充第 0 节：按代码讲清 LSTM 事件编码器（窗口 16、1024→256 投影、单层 LSTM hidden 256、`(h,c)` 快照、版本不一致重建），并把衰减/归档/清除/过期等记忆层遗忘机制与模型层门控分开表述。
  - 检索命中强化闭环：`POST /memory/search` 提交后以 BackgroundTasks 异步执行 `delta = 0.02 × (1 - F)`，独立 Session/UoW、失败只记日志；`retrieval_hit_gain` 提供 60 分钟时间窗 + 自然日唯一约束两层去重，不占反馈的每日 0.3 上限；反馈路径与 REPLAY 触发不变。
- 已验证：
  - 后端 115 项单元测试通过；5 项 PostgreSQL 集成测试在启用 `RUN_POSTGRES_INTEGRATION=1` 时运行并通过（`test_postgres_integration.py` 3 项、`test_wiki_integration.py` 2 项）。
  - M4：安全响应头、413/429、`/metrics`、就绪模型检查、Outbox 死信重放均已通过测试；Outbox 死信另在真实 PostgreSQL 上验证通过。
  - 事件写入 → 检索 → 反馈 → 重放 → 重建完整链路在真实 PostgreSQL + Pgvector 上通过。
  - Wiki 全量同步、重复同步去重、内容更新块失效、来源删除、重编码、失败重试与并发冲突在真实 PostgreSQL + Pgvector 上通过。
  - 实体来源事件列表与 viewer 视角 Dashboard 在真实 PostgreSQL + Pgvector 上通过。
  - `ruff check backend` 通过。
  - 训练 CLI 端到端通过，模型包可被运行期 `LSTMEventEncoder` 严格加载。
  - 前端 lint、typecheck、2 项测试、生产构建通过。
- 数据状态：迁移脚本 head 为 `3c9d2a7e5b11`（17 张业务表 + Pgvector 0.8.7）；Compose 数据库上次恢复到 `74a69ff68f54`，启动前需执行 `alembic upgrade head` 增加 `retrieval_hit_gain`。
- 当前端口：Compose 数据库使用 `POSTGRES_PORT=55432`（本机 PostgreSQL 18 占用 5432），Redis 使用 `REDIS_PORT=56379`（本机 6379 被占用）。
- 尚未启动：完整 API/Worker/Web/Nginx 栈。必须先提供有效 Embedding/Reranker/事件模型配置并通过 `python -m app.bootstrap`，不使用伪造模型结果绕过。
- 解释器：统一使用 `.venv312`（Python 3.12.9）；依赖镜像配置见 `infra/pip.ini` 与 `scripts/install.ps1`。

## 待办

1. M3：为前端联调补齐后端接口支撑（前端页面由另一会话开发），按反馈更新契约差异表。
   - 已完成：事件列表 `owner_id` 筛选与 `sort=event_time.desc|event_time.asc`；全部游标列表 `limit` 上限对齐设计 6.1 的 100（C-03/C-04 已关闭）；`POST /entities/{id}/rebuild` 路由层与服务层均收紧为 admin（与设计 8.2、C-07 一致）。
   - 已完成：Compose 增加 `beat` 服务；`/dashboard/summary` 事件计数按可见性过滤；Wiki `include_inactive` 的 403 行为与搜索 `session_id` 约束写入契约（C-20、C-23）。
   - 已完成：beat 镜像补齐 `packaging` 依赖，调度文件改到可写的 `/app/state` 命名卷；干净镜像验证 beat 能调度、worker 能消费 Outbox。
   - 已完成：事件纠偏与归档权限按设计 8.2 收紧到 admin（路由层与服务层一致），清除仍为 admin；契约 3.2、C-05、C-06 同步。
   - 已完成：工作记忆过期清理（`run_maintenance` 删除已过期 `closed` 会话的原始条目并置 `expired`，保留会话元数据）；`/dashboard/summary` 的 `wiki.last_sync_at` 与契约对齐。
   - 已完成：后端镜像分两档（`INSTALL_ML=false` 基础档为 Dockerfile 默认，`true` 为 ml 档）；ml 档装 CPU-only 的 `torch==2.6.0+cpu`/`safetensors`/`transformers`，不含 CUDA 运行时，干净镜像内事件编码已验证可用；Compose 默认 ml 档。
   - 已完成（M4 入口防护）：安全响应头、请求体上限（1 MiB → 413）、按用户限流（搜索 60/分、反馈 120/分、同步创建 10/时），Redis 不可用时退回进程内计数；未引入 CSRF（Bearer JWT、无 Cookie 会话）。
   - 已完成（M4 观测）：Prometheus `/metrics`（请求量/时延/错误/限流拒绝/事件处理/Outbox 积压与死信），OTel OTLP HTTP 导出与 FastAPI 自动插桩。
   - 已完成（M4 就绪探针）：`/health/ready` 增加 `models` 检查（本地模型包清单校验 + 远程可达性），默认缓存 30 秒。
   - 已完成（M4 Outbox 死信）：admin 级 `GET /outbox/dead-letters` 与 `POST /outbox/dead-letters/{id}/replay`，与业务 `/jobs` 区分。
   - 已完成（文档对齐）：设计文档 8.4 的 Wiki 非 active 可见性改为 operator 及以上；契约 C-20 改为“已与设计对齐”，权限代码未改。
   - 待处理：前端端到端联调反馈的后端问题；前端 `DashboardView.tsx` 仍读 `wiki.last_sync_time`，需前端会话改为 `last_sync_at`。
2. M4：安全、观测、质量评估与故障恢复验收。
   - 已完成：入口防护、Prometheus/OTel 观测、模型就绪检查、Outbox 死信查询与重放；实体重建权限已按设计 8.2 收紧为 admin。
   - 待拍板：`/metrics` 是否默认强制 `METRICS_TOKEN`（当前默认留空，由部署层限制来源）。
3. 生产环境仍需配置有效 Embedding/Reranker/事件模型后启动完整 API/Worker/Beat/Web/Nginx 栈；模型统一走 `MODEL_DIR` 只读挂载到容器 `/models`，容器内路径以 `/models/` 开头，详见 [DEPLOYMENT.md](DEPLOYMENT.md)。

## Wiki 同步已知限制

- 数据包仅支持 WikiExtractor JSONL；`s3://` 未支持（返回 422），需要先落地本地或 HTTP(S)。
- 整包解析为逐行流式读取，但 `total_count` 需要一次行数预扫描；`full` 模式来源删除推断需要记录本次出现的所有 page_id，超大包内存占用随词条数增长。
- 知识块状态保留历史版本（`superseded`），未做定期清理。

## AG-07 质量评估（独立评估区）

- 已交付：`evaluation/` 独立目录，不改 `backend/`；数据集、指标库、离线执行器、在线口径、报告与测试均在其中。
- 数据集：51 条事件、8 篇 Wiki / 24 个知识块、34 条带多级相关性标注的查询；覆盖 event/wiki/mixed 意图与 time_sensitive/cold_start/long_sequence 切片，确定性生成（UUIDv5）。
- 离线基线（词法代理，下限参考，非发布结论）：事件 nDCG@10 79.1% / Recall@10 91.7%；Wiki nDCG@10 68.8% / Recall@10 73.1%；统一搜索 nDCG@10 74.3% / Recall@10 84.3%。
- 已定位短板：自动意图准确率 24.0%（无关键词命中即退化为 mixed）、Wiki 候选召回 73.9%（生产阈值 0.15 下 34.8%）、长序列切片 nDCG@10 58.0%、反馈熟悉度对排序增益 +0.004、实体别名精确匹配会拆分实体。
- 门禁：blocked。缺少真实 BGE Embedding 与事件模型包，无法执行设计 4.4 的“模型 vs 纯 BGE”门槛；报告与 README 均写明该限制。
- 复现：`.\evaluation\run_all.ps1`（等价 `python -m evaluation.cli all` + `pytest evaluation/tests`）；产物为 `evaluation/results/baseline.json` 与 `REPORT.md`。
- 在线口径：`python -m evaluation.cli online`，只读 `memory_query` / `memory_access_log`，需 `DATABASE_URL`；不参与离线门禁。
- 待办：配置真实 Embedding/Reranker 与事件模型包后，用 `--embedding real` 复测并替换基线；扩展切片样本量；前端 `last_sync_at` 字段对齐仍待前端会话处理。

## 当前环境复现

```powershell
$env:POSTGRES_PORT="55432"
$env:REDIS_PORT="56379"
docker compose -f infra/docker-compose.yml up -d --wait db redis
$env:DATABASE_URL="postgresql+psycopg://memory:change-me@127.0.0.1:55432/memory"
$env:RUN_POSTGRES_INTEGRATION="1"
$env:TESTCONTAINERS_RYUK_DISABLED="true"
.\.venv312\Scripts\python.exe -m pytest backend/tests/test_postgres_integration.py -q
```

训练与评估：

```powershell
.\.venv312\Scripts\python.exe -m training.export --output artifacts/training.json
.\.venv312\Scripts\python.exe -m training.train --dataset artifacts/training.json --output-dir artifacts/event-model
.\.venv312\Scripts\python.exe -m training.evaluate --dataset artifacts/training.json --model-dir artifacts/event-model
```

契约导出（本会话只写入 docs，不改前端）：

```powershell
.\.venv312\Scripts\python.exe scripts\export_openapi.py
```

前端会话从 `docs/api/openapi.json` 生成类型，不再依赖 `frontend/openapi.json`。

每个工作包完成后必须更新 `docs/CHANGELOG.md`，记录实际修改、验证命令、结果、已知限制和下一步。
