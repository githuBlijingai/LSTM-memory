# Memory System

面向个人/小团队的**分层记忆系统**。事件先由 LSTM 事件编码器编码成向量，再逐层沉淀为实体状态与 Wiki 知识；检索时按意图把三层结果融合起来。

- 事件（Event）：发生了什么，原子记录，带时间、主体、重要度、可见性。
- 实体（Entity）：围绕某个人/项目/主题，事件如何累积成状态（当前状态版本、摘要、最后事件时间）。
- Wiki：从事件之外同步进来的稳定知识条目，按块向量化索引。

三层分开存、分开更新：事件保持事实性，实体负责"现在怎么样"，Wiki 负责"长期是什么"。

一句话概括两套机制：**LSTM 决定一条事件怎么和历史一起被编码，后台维护任务决定这条记忆还能留多久**。本文所有行为以代码为准，关键位置给出可点击的源码引用。

---

## 0. 三个问题的最小答案

| 问题 | 结论 | 详见 |
|---|---|---|
| 事件怎么变成向量、怎么进 LSTM | API 只落 `pending` + Outbox；worker 取实体最近事件窗口，1024 维基础向量经 `Linear(1024→256)` 进单层 LSTM，最后有效步 hidden 归一化后写入 `event_memory.embedding`（`vector(256)`）；`(h,c)` 存 `entity_state_snapshot` | 第 1 章 |
| 检索怎么召回 | 查询文本编码成 256 维后，事件与 Wiki 各自用 pgvector 余弦距离召回（阈值 0.15、候选 100），可选精排；最终分 = 意图权重 × 相关性 × 记忆权重。**没有"自定义维度"参数，也没有词法召回通道** | 第 2 章 |
| 想起越多越不容易遗忘？ | **只部分成立**：用户标记"有用"会加熟悉度并可能触发状态重放；**被检索命中不会加熟悉度**，只写一条曝光日志 | 第 3 章 |

---

## 1. 事件如何变成向量、如何进入 LSTM

**结论**：写入是异步两段式。HTTP 请求只负责校验、落 `pending` 事件和 Outbox 记录；真正的编码发生在 Celery worker 里。编码时把"该实体最近 16 条 active 事件 + 当前事件"拼成序列，每条先算 1024 维基础向量，投影到 256 维后逐时间步喂给单层 LSTM，取最后一个有效步的 hidden 做 L2 归一化，得到 256 维事件向量写入 `event_memory.embedding`；同时得到新的 `(h,c)`，序列化写入 `entity_state_snapshot`，供该实体下一条事件恢复初始状态。

### 1.1 全链路：六个步骤

1. **接收与校验**：`POST /api/v1/memory/events` 由 `EventCreateRequest` 做严格校验（多字段禁止、`schema_version` 固定为 1、`event_time` 必须带时区），并要求 `Idempotency-Key` 请求头。见 [memory.py](file:///d:/记忆库/memory-system/backend/app/api/v1/memory.py#L49-L77) 与 [schemas/memory.py](file:///d:/记忆库/memory-system/backend/app/schemas/memory.py#L13-L31)。
2. **落库并入队**：`EventCommandService.create_event` 在**同一个事务**里完成三件事：按 `(owner_id, entity_name, entity_type)` 解析或创建 `entity` 行；写 `event_memory`（`status=pending`、`initial_decay_score=1.0`、`content_hash`）；创建 `background_job`（类型 `EVENT_PIPELINE`）和 `outbox_event`（`event_type=memory.event.created`）。接口返回 202，响应体是 `data.event_id / data.status / data.job_id`。见 [memory_writes.py](file:///d:/记忆库/memory-system/backend/app/services/memory_writes.py#L68-L89)、[memory_writes.py](file:///d:/记忆库/memory-system/backend/app/services/memory_writes.py#L253-L289)、[memory_writes.py](file:///d:/记忆库/memory-system/backend/app/services/memory_writes.py#L320-L343)。
3. **派发**：Celery beat 每 1 秒执行 `dispatch_outbox`，用 `FOR UPDATE SKIP LOCKED` 认领一批 Outbox（批量 100，认领后可见性超时 120 秒），把 `memory.event.created` 转成 `process_event.delay(event_id)`。派发失败最多重试 5 次，之后置 `dead_letter`。见 [worker.py](file:///d:/记忆库/memory-system/backend/app/worker.py#L31-L43)、[outbox.py](file:///d:/记忆库/memory-system/backend/app/services/outbox.py#L47-L72)、[tasks/outbox.py](file:///d:/记忆库/memory-system/backend/app/tasks/outbox.py#L40-L71)。
4. **worker 加锁与状态检查**：`EventPipelineService.process_event` 在 `UnitOfWork` 中执行，先对事件行加锁；若事件已是 `active/archived/purged`，直接返回 `already_processed`（幂等）。随后取实体行锁和 `pg_advisory_xact_lock`，读取 `entity.current_state_version` 对应的快照。快照缺失，或快照的 `model_version` 与当前事件模型不一致时，不做编码：创建 `STATE_REBUILD` 任务、把实体置为 `rebuilding`、事件保持 `pending`，本次返回 `deferred`。见 [event_memory.py](file:///d:/记忆库/memory-system/backend/app/services/event_memory.py#L75-L208)、[event_memory.py](file:///d:/记忆库/memory-system/backend/app/services/event_memory.py#L264-L297)。
5. **装配窗口并编码**：`_encode_window` 取窗口文本 → 基础编码器算向量 → `EntityStateService.advance` 调 LSTM 前向 → 得到事件向量和新的 `(h,c)`（见 1.3–1.5）。
6. **回写**：同一个事务里写 `event_memory.embedding`、`event_model_version`、`sequence_no`，状态转 `active` 并清空 `failure_reason`；更新 `entity.last_sequence_no / last_event_id / last_event_time` 和实体摘要；写 `entity_state_snapshot`；写审计日志。提交后刷新快照并写 Redis 缓存（键 `lstm:state:{entity_id}:{state_version}`）。见 [event_memory.py](file:///d:/记忆库/memory-system/backend/app/services/event_memory.py#L145-L187)、[entity_state.py](file:///d:/记忆库/memory-system/backend/app/services/entity_state.py#L506-L507)。

每一跳落在哪张表：

| 步骤 | 执行位置 | 表 / 字段 |
|---|---|---|
| 1 校验 | API 进程 | 不落库 |
| 2 写入 | API 进程，单事务 | `event_memory`（`event_id/entity_id/subject/event_type/event_time/title/raw_content/content_hash/importance/visibility/owner_id/status=pending`）；`background_job`；`outbox_event`（`event_type/payload/status=pending`） |
| 3 派发 | beat 进程 | `outbox_event.status/attempt_count/available_at` |
| 4 取状态 | worker 进程 | `event_memory`（行锁）、`entity`（`current_state_version/current_model_version/last_sequence_no/last_event_time/status`）、`entity_state_snapshot` |
| 5 编码 | worker 进程（纯计算 + 模型） | 不落库；读取 `event_memory.raw_content/title` |
| 6 回写 | worker 进程，单事务 | `event_memory.embedding/event_model_version/sequence_no/status/failure_reason`；`entity_state_snapshot`（`h_tensor/c_tensor/tensor_dtype/tensor_shape/checksum/model_version/state_version`）；`entity` 计数与摘要 |

> 实现注意：实体匹配键是 `(owner_id, entity_name, entity_type)`。写入 API 的 `entity_type` 默认 `topic`，而 worker 的 `process_event(..., entity_type="user")` 默认 `user`；两者不一致时 worker 会新建一个 `user` 实体，并把事件的 `entity_id` 改写过去。见 [memory_writes.py](file:///d:/记忆库/memory-system/backend/app/services/memory_writes.py#L291-L318) 与 [event_memory.py](file:///d:/记忆库/memory-system/backend/app/services/event_memory.py#L219-L254)。

### 1.2 写入校验的实际规则

| 字段 | 规则 | 代码 |
|---|---|---|
| `schema_version` | 只能为 `1` | [schemas/memory.py](file:///d:/记忆库/memory-system/backend/app/schemas/memory.py#L13-L15) |
| `subject` | 1–128 字符 | 同上 |
| `entity_type` | 1–32 字符，默认 `topic` | 同上 |
| `event_type` | 1–64 字符 | 同上 |
| `event_time` | 必填，且必须带时区 | 同上 |
| `title` | 1–256 字符 | 同上 |
| `content` | 1–20000 字符 | 同上 |
| `importance` | 0–1，默认 0.5 | 同上 |
| `visibility` | `private` / `shared`，默认 `private` | 同上 |
| `Idempotency-Key` | 写接口必填；重放同一 key 返回首次响应 | [memory.py](file:///d:/记忆库/memory-system/backend/app/api/v1/memory.py#L49-L77)、[idempotent.py](file:///d:/记忆库/memory-system/backend/app/api/idempotent.py#L23-L57) |

### 1.3 窗口：16 条、左补零、mask

`event_window_size` 默认 16（允许 2–128）。`_encode_window` 的实际行为是：

1. 按 `event_time DESC, event_id DESC` 取该实体最近 16 条 `status=active` 事件。
2. 把当前事件追加进去，按 `(event_time, event_id)` 升序排序。
3. 只保留排序后的**最后 16 条**——所以满窗口时通常是"15 条历史 + 当前事件"。
4. 每条事件的文本取 `raw_content or title`，一次性交给基础编码器批量编码。
5. 用 `left_padded_mask(len(window), 16)` 生成 mask：前导 `0.0` 表示填充，尾部 `1.0` 表示有效；向量用 `pad_window_vectors` 左补零向量。

见 [event_memory.py](file:///d:/记忆库/memory-system/backend/app/services/event_memory.py#L299-L328)、[sequence.py](file:///d:/记忆库/memory-system/backend/app/domain/sequence.py#L50-L69)。

两个直接后果，读代码时容易忽略：

- **迟到事件可能被挤出窗口**：如果该实体已有 16 条比当前事件更新的事件，当前事件排序后落在第 17 位，会被切掉；这一次编码实际推进的是窗口里最新那条事件。代码会在推进后依据 `is_late_event` 再创建一个 `STATE_REBUILD` 任务，把实体置为 `rebuilding` 来纠正。见 [event_memory.py](file:///d:/记忆库/memory-system/backend/app/services/event_memory.py#L122-L168)。
- **空历史只有当前事件**：窗口长度为 1，补 15 个零向量，mask 是 15 个 0 加 1 个 1。

`EventEncoder.encode` 内部逐时间步循环，并用 mask 决定是否接受这一步的新状态：

```text
h = next_h * valid + h * (1 - valid)
c = next_c * valid + c * (1 - valid)
```

`valid=0` 时 `h/c` 原样保留，不会给不存在的历史伪造状态；只有 `valid>0` 的步才会成为 `last_valid_output`。见 [event_encoder.py](file:///d:/记忆库/memory-system/backend/app/ml/event_encoder.py#L145-L177)。

### 1.4 投影与 LSTM 的具体维度

| 项目 | 值 | 位置 |
|---|---|---|
| 基础向量维度 | 1024（`BASE_EMBEDDING_DIM`） | [config.py](file:///d:/记忆库/memory-system/backend/app/core/config.py#L84) |
| 投影层 | `nn.Linear(1024, 256)` | [event_encoder.py](file:///d:/记忆库/memory-system/backend/app/ml/event_encoder.py#L78-L86) |
| LSTM 输入维度 | 256（`LSTM_INPUT_DIM`） | [config.py](file:///d:/记忆库/memory-system/backend/app/core/config.py#L85) |
| LSTM 隐藏维度 | 256（`LSTM_HIDDEN_DIM`） | [config.py](file:///d:/记忆库/memory-system/backend/app/core/config.py#L86) |
| LSTM 层数 | 1（`LSTM_NUM_LAYERS`，允许 1–4） | [config.py](file:///d:/记忆库/memory-system/backend/app/core/config.py#L87) |
| 窗口长度 | 16（`EVENT_WINDOW_SIZE`） | [config.py](file:///d:/记忆库/memory-system/backend/app/core/config.py#L83) |
| 事件向量 | 256 维，L2 归一化 | [event_encoder.py](file:///d:/记忆库/memory-system/backend/app/ml/event_encoder.py#L169-L175) |
| 事件向量列 | `event_memory.embedding vector(256)` | [models/memory.py](file:///d:/记忆库/memory-system/backend/app/models/memory.py#L71) |
| 快照张量形状 | `(1, 256)`，dtype `float32` | [event_encoder.py](file:///d:/记忆库/memory-system/backend/app/ml/event_encoder.py#L176)；[models/state.py](file:///d:/记忆库/memory-system/backend/app/models/state.py#L35-L39) |

基础向量由 `EmbeddingProvider` 产出并统一 L2 归一化：本地档取 BGE 的 CLS 向量（`max_length=512`），远程档走 OpenAI 兼容 `/embeddings`，两者都校验维度等于 `BASE_EMBEDDING_DIM`、拒绝零向量和 NaN。见 [embedding.py](file:///d:/记忆库/memory-system/backend/app/ml/embedding.py#L26-L51)、[embedding.py](file:///d:/记忆库/memory-system/backend/app/ml/embedding.py#L176-L195)、[embedding.py](file:///d:/记忆库/memory-system/backend/app/ml/embedding.py#L74-L106)。

模型包加载是硬校验：`manifest.json` 必须包含 `model_version`、`base_embedding_dim`、`input_dim`、`hidden_dim`、`num_layers`、`weights_sha256`；版本、维度、权重 SHA-256 任一不符就抛 `MODEL_UNAVAILABLE`，权重以 `load_state_dict(strict=True)` 载入。运行期从不传 `allow_untrained`。见 [registry.py](file:///d:/记忆库/memory-system/backend/app/ml/registry.py#L91-L151)、[registry.py](file:///d:/记忆库/memory-system/backend/app/ml/registry.py#L198-L237)。

### 1.5 `(h, c)` 从哪来、写回哪

- **首次编码**（实体 `current_state_version == 0`）：`init_state=None`，LSTM 用全零 `(h,c)` 起步。
- **后续编码**：读取 `entity.current_state_version` 对应的最新快照，校验 checksum/shape/dtype 后反序列化成 `init_state`；若快照缺失或模型版本不符，转重建而不是硬接。见 [event_memory.py](file:///d:/记忆库/memory-system/backend/app/services/event_memory.py#L264-L297)。
- **写回**：`EntityStateService.write_snapshot` 把 `h_bytes/c_bytes` 写进 `entity_state_snapshot.h_tensor/c_tensor`，同时记录 `state_version`、`model_version`、`last_event_id`、`last_sequence_no`、`tensor_dtype`、`tensor_shape`、`checksum`、`snapshot_kind=UPDATE`。见 [entity_state.py](file:///d:/记忆库/memory-system/backend/app/services/entity_state.py#L159-L177)。
- **Redis 缓存**：提交后把同一份快照写入 `lstm:state:{entity_id}:{state_version}`，读路径先查缓存、校验失败再回退 PostgreSQL。缓存失败只记日志，不影响权威状态。见 [entity_state.py](file:///d:/记忆库/memory-system/backend/app/services/entity_state.py#L220-L251)、[entity_state.py](file:///d:/记忆库/memory-system/backend/app/services/entity_state.py#L523-L591)。
- **重建**：`EntityStateService.rebuild` 从可用快照出发，按 `(event_time, event_id)` 重放 `sequence_no` 之后的所有 active 事件，逐个事件算窗口、推进状态、**覆盖事件的 `embedding` 和 `event_model_version`**，最后写一条 `snapshot_kind=REBUILD` 的快照并恢复实体为 `active`。见 [entity_state.py](file:///d:/记忆库/memory-system/backend/app/services/entity_state.py#L377-L502)。

### 1.6 查询侧用同一个编码器

检索时查询文本先算 1024 维基础向量，再由 `QueryAdapter` 复用同一个投影层和 LSTM：**零初始状态、单步前向、取输出做 L2 归一化**，得到与事件同一空间的 256 维查询向量。它不读实体快照，也不写状态。见 [query_encoder.py](file:///d:/记忆库/memory-system/backend/app/ml/query_encoder.py#L15-L27)、[event_encoder.py](file:///d:/记忆库/memory-system/backend/app/ml/event_encoder.py#L179-L192)。

### 1.7 失败与重试

- 编码抛错时事务回滚，事件**保持 `pending`**；流水线没有把事件写成 `failed` 的路径（`EventStatus.FAILED` 枚举存在，只被概览统计读取，始终为 0）。
- `process_event` 任务最多重试 5 次，退避从 30 秒指数增长到 1800 秒上限；耗尽后把 `background_job` 标记为死信并抛出。见 [tasks/event_pipeline.py](file:///d:/记忆库/memory-system/backend/app/tasks/event_pipeline.py#L23-L86)。
- Outbox 层另有自己的 5 次重试和死信，可用 `GET /api/v1/outbox/dead-letters` 查看、`POST .../{id}/replay` 重放。见 [outbox.py](file:///d:/记忆库/memory-system/backend/app/services/outbox.py#L80-L146)。

### 1.8 本章关键文件

| 作用 | 文件 |
|---|---|
| 路由与请求模型 | [memory.py](file:///d:/记忆库/memory-system/backend/app/api/v1/memory.py#L49-L77)、[schemas/memory.py](file:///d:/记忆库/memory-system/backend/app/schemas/memory.py#L13-L31) |
| 写入与入队 | [memory_writes.py](file:///d:/记忆库/memory-system/backend/app/services/memory_writes.py#L68-L89) |
| Outbox 派发 | [services/outbox.py](file:///d:/记忆库/memory-system/backend/app/services/outbox.py#L47-L72)、[tasks/outbox.py](file:///d:/记忆库/memory-system/backend/app/tasks/outbox.py#L40-L71) |
| worker 流水线 | [event_memory.py](file:///d:/记忆库/memory-system/backend/app/services/event_memory.py#L75-L328) |
| 编码器 | [event_encoder.py](file:///d:/记忆库/memory-system/backend/app/ml/event_encoder.py#L145-L192) |
| 状态快照 | [entity_state.py](file:///d:/记忆库/memory-system/backend/app/services/entity_state.py#L159-L502) |
| 模型包加载 | [registry.py](file:///d:/记忆库/memory-system/backend/app/ml/registry.py#L91-L237) |

---

## 2. 检索时的「维度」与召回

**结论**：检索是"向量双路召回 + 可选精排 + 三因子相乘排序"。查询和事件都在 256 维事件空间比较，Wiki 在 1024 维基础空间比较；两路都用 pgvector 余弦距离，先按相似度阈值和候选上限截断，再乘上时间衰减、熟悉度、意图权重。代码里**没有名为"维度"的检索参数**，也没有关键词/全文召回通道；模型不可用时检索直接 503，不会降级到词法检索。

### 2.1 查询侧怎么编码

`MemorySearchService.search` 先 `embedding.embed([query])[0]` 得到 1024 维基础向量，再 `query_encoder.encode(base_vector)` 得到 256 维查询向量。事件召回用后者，Wiki 召回用前者。见 [memory_search.py](file:///d:/记忆库/memory-system/backend/app/services/memory_search.py#L86-L104)。

### 2.2 两路召回

| 项目 | 事件 | Wiki |
|---|---|---|
| 比较对象 | `event_memory.embedding`（256 维） vs 查询向量 | `wiki_chunk.embedding`（1024 维） vs 基础向量 |
| 算子 | `cosine_distance`（pgvector `<=>`，HNSW `vector_cosine_ops`） | 同左 |
| 相似度下限 | `distance <= 1 - SEARCH_MIN_SIMILARITY`（默认 0.15，即余弦 ≥ 0.15） | 同左 |
| 候选上限 | `SEARCH_CANDIDATE_K`（默认 100），按距离升序 | 同左 |
| 状态过滤 | `status=active`、`embedding IS NOT NULL`、`deleted_at IS NULL` | `wiki_chunk.status=active` 且 `wiki_article.status=active` |
| 权限过滤 | 按 `owner_id`/`visibility` 过滤（admin 全可见） | 不做 owner 级过滤，Wiki 视为共享知识 |
| 额外过滤 | `subject`、`entity_ids`、`event_types`、`start_time`、`end_time` | `wiki_categories`（数组包含）、`wiki_languages` |
| 代码 | [memory_search.py](file:///d:/记忆库/memory-system/backend/app/services/memory_search.py#L170-L261) | [memory_search.py](file:///d:/记忆库/memory-system/backend/app/services/memory_search.py#L263-L355) |

**没有词法通道**：检索路径只发向量查询。代码里唯一的 `ilike` 在 Wiki 列表接口的标题过滤（`wiki_reads.py`），不参与检索排序。

### 2.3 相关性：余弦 + 可选精排

```text
cos_norm = (cosine_similarity + 1) / 2
relevance = cos_norm                                  # 未启用精排
relevance = 0.4 * cos_norm + 0.6 * rerank_score       # 启用精排
```

精排只在 `RERANKER_ENABLED=true` 且模型加载成功时发生；`rerank` 分数数量与文档数不一致会直接报 502。见 [scoring.py](file:///d:/记忆库/memory-system/backend/app/domain/scoring.py#L20-L32)、[memory_search.py](file:///d:/记忆库/memory-system/backend/app/services/memory_search.py#L357-L363)。

### 2.4 记忆权重：时间衰减与熟悉度

| 记忆 | 权重公式 | 参数 |
|---|---|---|
| 事件 | `W = min(D * (1 + β * F), CAP)` | `β=FAMILIARITY_BETA=0.5`、`CAP=MEMORY_WEIGHT_CAP=2.5` |
| Wiki | `W = min(importance * (1 + α * F), CAP)` | `α=WIKI_FAMILIARITY_ALPHA=0.6` |

- `D = initial_decay_score * exp(-DECAY_LAMBDA * age_days)`，`DECAY_LAMBDA=0.01`，`age_days` 从 `created_at` 算起。
- `F` 是"物化熟悉度"：先按 `F * exp(-FAMILIARITY_GAMMA * elapsed_days)` 衰减（`γ=0.003`），48 小时保护期内不衰减；锚点是 `familiarity_updated_at`，为空则用 `created_at`。
- 事件衰减基于 `initial_decay_score`（写入默认 1.0），不是基于 `decay_score` 字段，所以重复计算不会叠加衰减。

见 [scoring.py](file:///d:/记忆库/memory-system/backend/app/domain/scoring.py#L39-L116)。

### 2.5 融合、排序、阈值与 top-k

```text
final_score = clamp(intent_weight) * clamp(relevance) * clamp(memory_weight)
```

- 三个因子各自 clamp 到 `[0,1]`；因此 `MEMORY_WEIGHT_CAP=2.5` 只影响权重本身，进入最终分时超过 1 的部分会被截断。
- 排序键：`(-final_score, memory_type, memory_id, tie_break)`，稳定排序。
- `top_n` 取请求里的 `top_n`（默认 10，上限 50）；配置项 `SEARCH_TOP_N` 在当前检索路径中未被使用。
- 同一篇 Wiki 文章最多保留 2 个分块（`MAX_WIKI_CHUNKS_PER_ARTICLE=2`），超出部分跳过而不是挤掉其他结果。
- 每次检索都会写一条 `memory_query` 和若干 `memory_access_log`（`feedback_type=exposure`、`is_counted=false`）。见 [memory_search.py](file:///d:/记忆库/memory-system/backend/app/services/memory_search.py#L106-L159)、[retrieval.py](file:///d:/记忆库/memory-system/backend/app/domain/retrieval.py#L27-L54)。

参数与阈值集中位置：

| 参数 | 默认 | 作用 | 位置 |
|---|---|---|---|
| `SEARCH_MIN_SIMILARITY` | 0.15 | 两路召回的余弦下限 | [config.py](file:///d:/记忆库/memory-system/backend/app/core/config.py#L53) |
| `SEARCH_CANDIDATE_K` | 100 | 每路进入融合前的候选数 | [config.py](file:///d:/记忆库/memory-system/backend/app/core/config.py#L51) |
| `SEARCH_TOP_N` | 10 | 配置存在，检索未使用 | [config.py](file:///d:/记忆库/memory-system/backend/app/core/config.py#L52) |
| 请求 `top_n` | 10（1–50） | 实际返回条数 | [schemas/memory.py](file:///d:/记忆库/memory-system/backend/app/schemas/memory.py#L122-L129) |
| `MEMORY_WEIGHT_CAP` | 2.5 | 记忆权重上限 | [config.py](file:///d:/记忆库/memory-system/backend/app/core/config.py#L54) |
| `DECAY_LAMBDA` | 0.01 | 事件时间衰减 | [config.py](file:///d:/记忆库/memory-system/backend/app/core/config.py#L88) |
| `FAMILIARITY_GAMMA` | 0.003 | 熟悉度日衰减 | [config.py](file:///d:/记忆库/memory-system/backend/app/core/config.py#L89) |
| `FAMILIARITY_BETA` / `WIKI_FAMILIARITY_ALPHA` | 0.5 / 0.6 | 熟悉度对记忆权重的放大 | [config.py](file:///d:/记忆库/memory-system/backend/app/core/config.py#L92-L93) |

### 2.6 意图权重：为什么结果会偏向事件或 Wiki

`classify_intent` 先看显式条件，再看关键词：

| 条件 | 意图 | 事件权重 / Wiki 权重 |
|---|---|---|
| `memory_type=event` | event | 1.0 / 0 |
| `memory_type=wiki` | wiki | 0 / 1.0 |
| 带 `wiki_categories` 或 `wiki_languages` | wiki | 0.2 / 0.8 |
| 带 `subject`、`entity_ids`、`start_time` 或 `end_time` | event | 0.8 / 0.2 |
| 事件类关键词命中更多（"上次/会议/决定/deadline"等） | event | 0.8 / 0.2 |
| Wiki 类关键词命中更多（"是什么/定义/原理/区别"等） | wiki | 0.2 / 0.8 |
| 持平或都没命中 | mixed | 0.5 / 0.5 |

见 [intent.py](file:///d:/记忆库/memory-system/backend/app/domain/intent.py#L54-L123)。

### 2.7 代码里有没有「自定义维度」？

**没有。** 检索请求里不存在名为 `dimension` 或"记忆维度"的参数，也不支持按"维度"切换召回策略。最接近的现有能力是以下请求字段：

| 字段 | 作用 | 取值 |
|---|---|---|
| `memory_type` | 只搜某一类记忆 | `all` / `event` / `wiki` |
| `filters.subject` | 按主体精确过滤事件 | 字符串 |
| `filters.entity_ids` | 按实体过滤事件 | UUID 列表（≤100） |
| `filters.event_types` | 按事件类型过滤 | 字符串列表（≤20） |
| `filters.start_time` / `end_time` | 按事件时间范围过滤 | 带时区时间 |
| `filters.wiki_categories` | 按 Wiki 分类过滤 | 字符串列表（≤20） |
| `filters.wiki_languages` | 按 Wiki 语言过滤 | 字符串列表（≤5） |
| `enable_familiarity` | 是否让熟悉度参与排序 | 默认 `true`；`false` 时 `F=0` |
| `top_n` | 返回条数 | 1–50 |
| `include_score_breakdown` | 是否返回打分拆解 | 默认 `true` |
| `session_id` | 关联工作记忆会话（会校验归属） | UUID |

见 [schemas/memory.py](file:///d:/记忆库/memory-system/backend/app/schemas/memory.py#L106-L129)。

### 2.8 降级与兜底

- **模型不可用时没有检索兜底**：`/memory/search` 依赖 `ModelBundle`，模型包缺失/校验失败/基础编码器不可用都会让请求返回 503（`MODEL_UNAVAILABLE`），不会退化成词法检索。见 [memory.py](file:///d:/记忆库/memory-system/backend/app/api/v1/memory.py#L220-L235)、[registry.py](file:///d:/记忆库/memory-system/backend/app/ml/registry.py#L198-L237)。
- **精排是真正的可选降级**：没有 reranker 时 `_rerank` 返回 `None`，`relevance` 退化为纯归一化余弦，其余打分不变。
- **就绪探针**只检查事件模型包（manifest 版本、权重文件存在且非空）、Embedding/Reranker 提供方配置与远程可达性，结果缓存 30 秒；它不校验向量维度。见 [readiness.py](file:///d:/记忆库/memory-system/backend/app/services/readiness.py#L101-L141)。
- **词法代理只存在于离线评估**（`evaluation/` 的 hash embedding + 词法精排），不是运行期路径。

### 2.9 本章关键文件

| 作用 | 文件 |
|---|---|
| 检索编排与曝光日志 | [memory_search.py](file:///d:/记忆库/memory-system/backend/app/services/memory_search.py#L86-L159) |
| 两路召回 | [memory_search.py](file:///d:/记忆库/memory-system/backend/app/services/memory_search.py#L170-L355) |
| 意图分类 | [intent.py](file:///d:/记忆库/memory-system/backend/app/domain/intent.py#L54-L123) |
| 相关性/衰减/权重 | [scoring.py](file:///d:/记忆库/memory-system/backend/app/domain/scoring.py#L20-L116) |
| 融合与条数限制 | [retrieval.py](file:///d:/记忆库/memory-system/backend/app/domain/retrieval.py#L27-L54) |
| 请求参数 | [schemas/memory.py](file:///d:/记忆库/memory-system/backend/app/schemas/memory.py#L106-L129) |

---

## 3. 「想起越多越不容易遗忘」

**结论**：这条闭环**只实现了一半**。

- **"被用户标记有用"确实会提高熟悉度**：`adopted` / `helpful` 反馈会先物化衰减，再加 `FAMILIARITY_STEP=0.08`，受每日上限和去重约束；熟悉度跨过 1.0 时还会创建一个重放任务，用权威快照重新推进实体状态。
- **"被检索到"不会提高熟悉度**：检索只写 `memory_access_log(feedback_type=exposure, is_counted=false)`，维护任务不消费这张表，`access_count` 也只在计分反馈时才加一。
- 熟悉度不是实体的属性，而是**每条事件、每个 Wiki 分块各自的字段**；实体表没有 `familiarity_score`。

### 3.1 熟悉度存在哪

| 表 | 字段 |
|---|---|
| `event_memory` | `familiarity_score`（默认 0，约束 0–2）、`familiarity_updated_at`、`last_access_time`、`access_count`、`replay_count`（0–2） |
| `wiki_chunk` | `familiarity_score`、`familiarity_updated_at`、`last_access_time`、`access_count` |
| `entity` | **没有**熟悉度字段；只有状态版本、摘要、最后事件时间 |

见 [models/memory.py](file:///d:/记忆库/memory-system/backend/app/models/memory.py#L59-L129)、[models/wiki.py](file:///d:/记忆库/memory-system/backend/app/models/wiki.py#L52-L91)。

### 3.2 检索命中：只记曝光，不加分

`MemorySearchService.search` 对每条入选结果写一条 `MemoryAccessLog`：

```text
feedback_type = "exposure"
is_counted = False
result_rank = 名次
similarity = 余弦相似度
final_score = 最终分
```

之后没有任何代码把 `exposure` 汇总成熟悉度增量：`maintenance.py` 只处理 `event_memory` 和 `wiki_chunk` 自身的字段，不读 `memory_access_log`；`access_count` 也只在反馈计分路径里 `+= 1`。所以"反复被搜到"目前完全不影响衰减、归档或排序。见 [memory_search.py](file:///d:/记忆库/memory-system/backend/app/services/memory_search.py#L137-L153)、[maintenance.py](file:///d:/记忆库/memory-system/backend/app/tasks/maintenance.py#L35-L134)。

### 3.3 用户反馈：唯一的加分入口

`POST /api/v1/memory/feedback` 必须引用一次检索返回的 `query_id` 和结果 `memory_id`。处理规则：

| 反馈类型 | 是否计分 | 行为 |
|---|---|---|
| `adopted`、`helpful` | 是 | 物化衰减后 `familiarity_score += gain`，写 `last_access_time`、`access_count += 1`、`familiarity_daily_gain`，并记一条 `is_counted=true` 的访问日志 |
| `clicked`、`not_helpful` | 否 | 只写日志，`not_counted_reason=not_consolidating_feedback` |
| `exposure` | 禁止 | 请求直接 422 |

计分约束：

- **单次增量** `FAMILIARITY_STEP=0.08`。
- **每日上限** `FAMILIARITY_DAILY_CAP=0.3`，按 `(memory_type, memory_id, gain_date)` 记在 `familiarity_daily_gain`；达到上限返回 `counted=false, reason=daily_cap_reached`。
- **幂等/去重**：同一 `query_id + memory_type + memory_id` 只能有一条计分反馈；同一 `(actor, session, query_hash, memory_id)` 在 `FEEDBACK_DEDUP_MINUTES=10` 内重复提交不计分。
- **上限** `familiarity_score` 不超过 `FAMILIARITY_MAX=2.0`。

见 [feedback.py](file:///d:/记忆库/memory-system/backend/app/services/feedback.py#L54-L186)、[feedback.py](file:///d:/记忆库/memory-system/backend/app/services/feedback.py#L188-L327)、[telemetry.py](file:///d:/记忆库/memory-system/backend/app/models/telemetry.py#L94-L107)。

### 3.4 衰减、保护期、归档、清除

每日维护任务在 `MAINTENANCE_UTC_HOUR:MAINTENANCE_UTC_MINUTE`（默认 02:00 UTC）执行，一轮做五件事：

| 动作 | 规则 | 默认参数 |
|---|---|---|
| 事件衰减 | `decay_score = initial_decay_score * exp(-λ * age_days)`；熟悉度 `F *= exp(-γ * elapsed_days)` | `λ=0.01`、`γ=0.003` |
| 新记忆保护 | 创建不足 48 小时不衰减熟悉度、不自动归档；保护期内锚点推进到当前时间，避免保护期结束后补算 | `NEW_MEMORY_PROTECT_HOURS=48` |
| 自动归档 | `delete_score = decay_score * (1 + 0.3 * F) < 0.1`，且不在保护期、未 `protected`、无待处理纠偏、实体不在 `rebuilding` | `DELETE_THRESHOLD=0.1` |
| 归档后清除 | 归档满 30 天后转 `purged`：标题改 `[purged]`、清空 `raw_content` 与 `embedding`、写审计墓碑 | `ARCHIVE_RETENTION_DAYS=30` |
| 工作记忆清理 | 已关闭且 `expires_at` 到期的会话删除临时条目、状态转 `expired` | `WORKING_SESSION_TTL_HOURS=24` |

同一轮维护里，`event_memory` 与 `wiki_chunk` 各最多处理 `MAINTENANCE_BATCH_SIZE=500` 行（按 `created_at` 排序）。数据量超过 500 时，单轮只会覆盖最旧的一批，需要多轮或调大该值。

见 [maintenance.py](file:///d:/记忆库/memory-system/backend/app/tasks/maintenance.py#L79-L214)、[lifecycle.py](file:///d:/记忆库/memory-system/backend/app/domain/lifecycle.py#L20-L53)、[scoring.py](file:///d:/记忆库/memory-system/backend/app/domain/scoring.py#L119-L123)、[worker.py](file:///d:/记忆库/memory-system/backend/app/worker.py#L36-L42)。

人工遗忘也走同一套状态：

| 操作 | 效果 |
|---|---|
| `DELETE /memory/events/{id}` + `mode=archive` | 转 `archived`，设 `purge_after=+30 天`，active 事件还会触发实体重建 |
| `DELETE /memory/events/{id}` + `mode=purge` | 立即清空正文与向量，转 `purged`，触发实体重建 |
| `POST /memory/events/{id}/restore` | 仅归档事件可恢复；有向量回 `active`，无向量回 `pending` |
| `POST /memory/events/{id}/corrections` | 生成新事件入队，旧事件转 `archived`（`purge_after` 置空），触发实体重建 |

见 [memory_writes.py](file:///d:/记忆库/memory-system/backend/app/services/memory_writes.py#L125-L249)。

### 3.5 重放做了什么、没做什么

熟悉度从 `<1.0` 跨到 `>=1.0`，且 `replay_count < REPLAY_MAX_COUNT`（默认 2）时，反馈服务创建 `REPLAY` 任务：

- **做**：读取实体权威快照作为 `init_state`，只把该条事件编码一步（`mask=[1.0]`），推进 `state_version`，写一条 `snapshot_kind=REPLAY` 的新快照并更新实体版本。
- **没做**：不修改 `event_memory.embedding`，也不再加熟悉度；重放记录写在 `state_replay_log`。

见 [feedback.py](file:///d:/记忆库/memory-system/backend/app/services/feedback.py#L237-L256)、[tasks/replay.py](file:///d:/记忆库/memory-system/backend/app/tasks/replay.py#L32-L113)、[entity_state.py](file:///d:/记忆库/memory-system/backend/app/services/entity_state.py#L297-L375)。

### 3.6 缺口在哪一环

"被检索到 → 强化"这一环不存在，缺口位置很具体：

1. [MemorySearchService.search](file:///d:/记忆库/memory-system/backend/app/services/memory_search.py#L137-L153) 只写 `is_counted=false` 的曝光日志，没有调用任何熟悉度更新。
2. [run_maintenance](file:///d:/记忆库/memory-system/backend/app/tasks/maintenance.py#L35-L52) 只重算两条记忆表自身的 `decay_score/familiarity_score`，不聚合 `memory_access_log`。
3. 没有任何 Celery 任务或 SQL 把"曝光次数/排名"换算成熟悉度增量。

因此当前语义是：**用户显式说"这条有用"才会被记住得更牢；系统自己反复搜到它并不会。** 想让"想起越多越不易遗忘"成立，需要新增一条从曝光日志到熟悉度的聚合路径（例如在维护任务里按 `memory_id` 统计曝光并加分），目前代码里没有。

### 3.7 本章关键文件

| 作用 | 文件 |
|---|---|
| 反馈计分与重放触发 | [feedback.py](file:///d:/记忆库/memory-system/backend/app/services/feedback.py#L54-L327) |
| 熟悉度物化与衰减公式 | [scoring.py](file:///d:/记忆库/memory-system/backend/app/domain/scoring.py#L60-L88) |
| 归档判定与清除时间 | [lifecycle.py](file:///d:/记忆库/memory-system/backend/app/domain/lifecycle.py#L20-L53) |
| 每日维护 | [maintenance.py](file:///d:/记忆库/memory-system/backend/app/tasks/maintenance.py#L79-L236) |
| 熟悉度相关字段 | [models/memory.py](file:///d:/记忆库/memory-system/backend/app/models/memory.py#L80-L84)、[models/wiki.py](file:///d:/记忆库/memory-system/backend/app/models/wiki.py#L67-L71) |
| 参数默认值 | [config.py](file:///d:/记忆库/memory-system/backend/app/core/config.py#L88-L98) |

---

## 4. 架构与目录结构

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

写入与检索的主链路已在第 1、2 章展开；这里只保留组件关系。

```text
memory-system/
├── backend/
│   ├── app/
│   │   ├── api/v1/        # REST 路由：events/search/working/entities/jobs/wiki/outbox/health
│   │   ├── core/          # 配置、安全、错误、中间件、限流、指标、追踪
│   │   ├── domain/        # 纯函数：意图、融合排序、衰减、熟悉度、序列窗口、生命周期
│   │   ├── ml/            # Embedding/Reranker/事件模型加载、LSTM 编码器、查询适配器
│   │   ├── models/        # SQLAlchemy 模型与枚举
│   │   ├── repositories/  # 查询、游标分页、权限谓词
│   │   ├── schemas/       # Pydantic 请求/响应
│   │   ├── services/      # 业务编排：写入、搜索、反馈、Outbox、状态快照、Wiki 同步
│   │   ├── tasks/         # Celery 任务：Outbox 派发、事件流水线、重建、重放、维护
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

## 5. 快速开始

### 5.1 前置

- Docker Desktop（Compose v2）。
- 模型资产：`bge-large-zh-v1.5`、事件模型包；启用精排时再加 `bge-reranker-v2-m3`。仓库不含模型，获取方式见 [MIGRATION_CHECKLIST.md](docs/MIGRATION_CHECKLIST.md) 第 1.2.1 节。
- 宿主机 Python 3.12（只有跑测试/训练/评估时需要）。
- Node 20.9+（只有本地起前端时需要；Next 16 要求）。

### 5.2 选镜像档

| 档位 | `INSTALL_ML` | 用途 |
|---|---|---|
| 基础档 | `false` | 只跑 API/迁移/测试，事件编码不可用 |
| ml 档 | `true`（Compose 默认） | 正常使用，事件编码可用 |

要验证写入→编码→搜索，必须用 ml 档。

### 5.3 最少 `.env`

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
- `BASE_EMBEDDING_DIM` 默认 1024。虽然配置可改，但 `event_memory.embedding` 固定 `vector(256)`、`wiki_chunk.embedding` 固定 `vector(1024)`，换维度需要改表结构并重训事件模型。
- 完整变量与两种模式见 [DEPLOYMENT.md](docs/DEPLOYMENT.md) 第 3–4 节。

### 5.4 启动

```powershell
docker compose --env-file .env -f infra/docker-compose.yml up -d
docker compose --env-file .env -f infra/docker-compose.yml ps -a
```

`bootstrap` 是一次性预检，必须 `exited (0)` 后 `api`/`worker`/`beat` 才会启动。`api` 启动时会自动执行 `alembic upgrade head`。

本清单示例端口：API `8000`、Web `3000`、Nginx `8080`、Postgres `55432`、Redis `56379`（后两者由 `.env` 覆盖）。

### 5.5 确认成功

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

### 5.6 起前端

```powershell
cd frontend
Copy-Item .env.example .env.local   # 确认 BACKEND_URL 指向后端
npm ci
npm run dev -- --port 3100
```

打开 `http://127.0.0.1:3100`。Compose 的 `web` 固定 3000；要 3100 时用上面的开发模式，或复用镜像 `docker run -p 3100:3000`。

---

## 6. 配置说明

完整清单见 [.env.example](.env.example) 与 [DEPLOYMENT.md](docs/DEPLOYMENT.md)。

### 6.1 基础设施

| 变量 | 默认 | 说明 |
|---|---|---|
| `DATABASE_URL` | 容器内由 Compose 注入 | 宿主直跑后端时必填 |
| `REDIS_URL` | 容器内由 Compose 注入 | 宿主直跑后端时必填 |
| `POSTGRES_PASSWORD` | `change-me` | 生产必须改 |
| `POSTGRES_PORT` / `REDIS_PORT` | 5432 / 6379 | 本机有冲突时改 |
| `INSTALL_ML` | `true` | Compose 镜像档位 |

### 6.2 模型（必填）

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
| `BASE_EMBEDDING_DIM` | 默认 1024；向量列固定，换维度需迁移 |
| `LSTM_INPUT_DIM` / `LSTM_HIDDEN_DIM` / `LSTM_NUM_LAYERS` | 默认 256 / 256 / 1，必须与模型包一致 |

### 6.3 认证与安全

| 变量 | 默认 | 说明 |
|---|---|---|
| `ENVIRONMENT` | `development` | `production` 时禁用 Dev Auth、要求 OIDC、禁止本地 CPU 模型 |
| `DEV_AUTH_ENABLED` | `false` | 本地登录开关 |
| `OIDC_ISSUER` / `OIDC_AUDIENCE` / `OIDC_JWKS_URL` | 空 | 生产必填 |
| `RATE_LIMIT_ENABLED` | `true` | 搜索 60/分、反馈 120/分、同步 10/时 |
| `MAX_REQUEST_BODY_BYTES` | 1048576 | 超限 413 |
| `SECURITY_HEADERS_ENABLED` | `true` | CSP/X-Frame-Options 等 |

### 6.4 观测

| 变量 | 默认 | 说明 |
|---|---|---|
| `METRICS_ENABLED` | `true` | 关闭后 `/metrics` 404 |
| `METRICS_TOKEN` | 空 | 空表示不鉴权 |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | 空 | 非空才启用链路导出 |
| `READINESS_MODEL_CACHE_SECONDS` | 30 | 就绪探针模型检查缓存 |
| `READINESS_MODEL_TIMEOUT_SECONDS` | 2.0 | 远程可达性探测超时 |

### 6.5 记忆与检索参数

| 变量 | 默认 | 说明 |
|---|---|---|
| `EVENT_WINDOW_SIZE` | 16 | LSTM 序列窗口长度 |
| `DECAY_LAMBDA` | 0.01 | 事件时间衰减系数 |
| `FAMILIARITY_GAMMA` | 0.003 | 熟悉度日衰减系数 |
| `FAMILIARITY_STEP` | 0.08 | 单次有效反馈的熟悉度增量 |
| `FAMILIARITY_DAILY_CAP` | 0.3 | 单条记忆每日熟悉度增量上限 |
| `FAMILIARITY_MAX` | 2.0 | 熟悉度上限 |
| `FAMILIARITY_BETA` / `WIKI_FAMILIARITY_ALPHA` | 0.5 / 0.6 | 熟悉度对记忆权重的放大系数 |
| `DELETE_THRESHOLD` | 0.1 | 自动归档的 `delete_score` 阈值 |
| `NEW_MEMORY_PROTECT_HOURS` | 48 | 新记忆保护期 |
| `ARCHIVE_RETENTION_DAYS` | 30 | 归档后保留天数 |
| `REPLAY_THRESHOLD` / `REPLAY_MAX_COUNT` | 1.0 / 2 | 触发状态重放的熟悉度阈值与次数上限 |
| `FEEDBACK_DEDUP_MINUTES` | 10 | 同一查询重复反馈的去重窗口 |
| `SEARCH_CANDIDATE_K` | 100 | 每路召回候选数 |
| `SEARCH_MIN_SIMILARITY` | 0.15 | 召回余弦下限 |
| `MEMORY_WEIGHT_CAP` | 2.5 | 记忆权重上限 |
| `MAINTENANCE_UTC_HOUR` / `MAINTENANCE_UTC_MINUTE` | 2 / 0 | 每日维护时间 |
| `MAINTENANCE_BATCH_SIZE` | 500 | 单轮每表最多处理行数 |
| `WORKING_SESSION_TTL_HOURS` | 24 | 工作记忆会话有效期 |

---

## 7. 接口与权限

所有业务接口在 `/api/v1` 下；响应统一为 `{data, meta}`。除搜索外的写操作要求 `Idempotency-Key` 请求头。

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

认证用 JWT Bearer；本地开发用 `Authorization: Dev <subject>` + `X-Dev-Roles`。三档角色：

| 角色 | 能做 |
|---|---|
| `viewer` | 写入/查看自己的事件、搜索、反馈、管理自己的工作记忆会话；读实体、Wiki 公开内容、自己的任务、概览 |
| `operator` | viewer 全部；创建/重试 Wiki 同步任务；查看他人任务；查看非 active 的 Wiki 内容 |
| `admin` | operator 全部；事件纠偏/归档/恢复、实体重建、熟悉度调整、Outbox 死信查询与重放；查看所有工作记忆会话 |

权限在路由依赖与服务层双重校验；越权返回 403 `FORBIDDEN`。

---

## 8. 前端

技术栈：Next.js 16 App Router、React 19、Ant Design 6、TanStack Query、ECharts。

| 路径 | 内容 |
|---|---|
| `/dashboard` | 概览：事件/实体/Wiki/任务数量与积压 |
| `/memories/events`、`/memories/events/{id}` | 事件列表与详情 |
| `/entities`、`/entities/{id}` | 实体列表、状态与事件时间线 |
| `/search` | 检索与反馈 |
| `/wiki`、`/wiki/{articleId}` | Wiki 列表与详情 |
| `/jobs`、`/jobs/wiki-sync` | 后台任务与同步任务 |

所有请求由 Next.js 服务端代理到 `BACKEND_URL`，浏览器不直接跨域访问后端。

```powershell
npm run dev -- --port 3000     # 开发
npm run build                  # 生产构建
npm run start -- --port 3000   # 生产启动
```

Dev Auth 生效条件：`NODE_ENV !== "production"` **且** `DEV_AUTH_ENABLED=true`，同时设置 `DEV_AUTH_SUBJECT` 与 `DEV_AUTH_ROLES`。生产构建下即使开启也不会生效，必须接真实 OIDC。

---

## 9. 评估与测试

### 9.1 离线评估（AG-07）

`evaluation/` 是独立评估区，复用 `app.domain` 的排序/衰减/熟悉度纯函数，保证评估口径与运行期一致。用途是**回归护栏**：改检索或排序后跑同一份确定性数据集，看指标有没有退化。

```powershell
.\evaluation\run_all.ps1
# 等价
.\.venv312\Scripts\python.exe -m evaluation.cli all
.\.venv312\Scripts\python.exe -m pytest evaluation/tests -q
```

- 数据集：34 条查询、51 条事件、24 个 Wiki 块，UUIDv5 确定性生成。
- 产物：`evaluation/results/baseline.json`、`evaluation/results/REPORT.md`。
- 仓库记录的基线（词法代理：hash embedding + 词法精排）：Recall@10 0.451、Hit@10 0.625、MRR@10 0.625、nDCG@10 0.597。
- **定位说明**：这是缺少真实模型资产时的下界代理，真实模型档的验收门槛仍未通过，不能当作发布结论；只用于同口径版本间对比。运行期检索不会走这条词法代理。

### 9.2 测试与质量

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
- **生产禁止本地 CPU 模型**：`ENVIRONMENT=production` 时若 `ALLOW_CPU_MODELS=true` 且 `MODEL_DEVICE=cpu`，配置校验会直接拒绝；本地 CPU 仅用于开发/测试。见 [config.py](file:///d:/记忆库/memory-system/backend/app/core/config.py#L161-L168)。
- **向量维度由表结构固定**：`event_memory.embedding` 是 `vector(256)`、`wiki_chunk.embedding` 是 `vector(1024)`；`BASE_EMBEDDING_DIM` 虽可配置，换维度要改列、迁移向量数据并重训事件模型。
- **换 Embedding 供应商不会自动重建旧向量**：检索不按模型版本过滤，混用不同模型的向量结果不可信；Wiki 可用 `mode=reembed`，事件没有批量重编码接口（只能靠实体重建或逐条重放）。
- **检索没有词法通道**：纯向量召回，专有名词/精确编号的召回依赖基础模型质量。
- **检索命中不强化熟悉度**：曝光只写日志，不计分，详见第 3 章。
- **维护任务是批量上限**：每轮每表最多 500 行，数据量大时需要多轮执行。
- **迟到事件可能被挤出窗口**：实体已有 16 条更新事件时，当前迟到事件不参与本次编码，由随后的重建任务纠正。
- Wiki 同步**不支持 `s3://`**：数据包解析会明确报错，需先落地本地或 HTTP(S)。见 [wiki_parser.py](file:///d:/记忆库/memory-system/backend/app/services/wiki_parser.py#L145-L150)。
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
