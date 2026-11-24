# 前后端 API 契约说明

| 项目 | 内容 |
|---|---|
| 适用版本 | 后端 0.1.0（M4 安全与观测已接入） |
| 契约基线 | `../../基于LSTM的分层记忆系统设计文档V1.2.md` 第 6 章 |
| 机器可读契约 | [`docs/api/openapi.json`](api/openapi.json)（由 `scripts/export_openapi.py` 生成） |
| 维护方 | 后端会话（backend/、infra/、数据库与迁移、Celery） |
| 更新日期 | 2026-11-17 |

> 本文是前后端并行开发时的**对外契约变更说明**。凡与设计文档第 6 章不一致的地方，以本文为准；本文未列出的行为，以第 6 章为准。接口签名或返回结构再调整时，必须同步更新本文、`docs/CHANGELOG.md` 和 `docs/api/openapi.json`。

## 1. 分工与联调方式

- 本会话只改动 `backend/`、`infra/`、`backend/alembic/`、数据库脚本和 Celery 任务；不再改动 `frontend/`。
- 前端类型请以 `docs/api/openapi.json` 为输入生成，不再依赖 `frontend/openapi.json`（旧文件保留但停止更新）。
- 后端每次导出契约：

```powershell
.\.venv312\Scripts\python.exe scripts\export_openapi.py
```

- 运行期 OpenAPI 仍可通过 `GET /api/v1/openapi.json` 获取；Swagger/ReDoc 页面默认关闭。

## 2. 通用规范

### 2.1 基础

- 前缀统一为 `/api/v1`；浏览器经 Next.js Route Handler 同源转发，外部应用可直连。
- 认证：OIDC JWT，角色 `viewer` / `operator` / `admin`；开发环境使用受控 Dev Auth。
- 时间：RFC 3339 带时区（UTC 或带偏移均可）；请求中不带时区的时间会返回 422。
- ID：UUID 字符串。
- 列表：游标分页，`limit` 默认 20、**最大 100**；`meta.next_cursor` 为 null 表示没有下一页。
- 请求 ID：中间件写入响应头 `X-Request-ID`，响应体统一放在 `meta.request_id`。Next.js 代理只会透传 UUID 形式的请求 ID，其他值由后端重新生成。

### 2.2 成功信封

所有成功响应（含 201/202）结构固定：

```json
{
  "data": {},
  "meta": {
    "request_id": "6f0dd8fa-f79f-4f24-b6f2-98a0db2bb782",
    "next_cursor": null
  }
}
```

单对象响应也带 `meta.next_cursor: null`，前端可统一按信封解析。

### 2.3 错误信封

```json
{
  "error": {
    "code": "IDEMPOTENCY_CONFLICT",
    "message": "同一幂等键不能用于不同请求",
    "details": {}
  },
  "meta": {
    "request_id": "6f0dd8fa-f79f-4f24-b6f2-98a0db2bb782",
    "next_cursor": null
  }
}
```

- 参数校验失败固定为 HTTP 422，`details.errors[]` 每项含 `location` / `message` / `type`。
- 错误 `message` 为中文可读文案；前端展示应使用 `code` 做分支，不要依赖 `message` 文本。
- 错误响应不返回堆栈、SQL、模型路径或敏感正文。

### 2.4 错误码

`VALIDATION_ERROR`、`UNAUTHORIZED`、`FORBIDDEN`、`NOT_FOUND`、`CONFLICT`、`IDEMPOTENCY_CONFLICT`、`RATE_LIMITED`、`PAYLOAD_TOO_LARGE`、`MODEL_UNAVAILABLE`、`TASK_CONFLICT`、`INTERNAL_ERROR`。

### 2.5 幂等

- 下列写接口**必须**携带请求头 `Idempotency-Key`（1～128 字符），缺失或超长返回 422：
  - `POST /memory/events`
  - `POST /memory/events/{event_id}/corrections`
  - `DELETE /memory/events/{event_id}`
  - `POST /memory/events/{event_id}/restore`
  - `POST /memory/feedback`
  - `POST /working/sessions`
  - `POST /working/sessions/{session_id}/items`
  - `POST /working/sessions/{session_id}/consolidate`
  - `DELETE /working/sessions/{session_id}`
  - `POST /entities/{entity_id}/rebuild`
  - `POST /jobs/{job_id}/retry`
  - `POST /admin/memories/{memory_type}/{memory_id}/familiarity`
  - `POST /wiki/sync-tasks`
  - `POST /wiki/sync-tasks/{task_id}/retry`
- 同一用户、同一路径、同一请求体重复提交会返回首次响应（状态码与 `data` 一致）。
- 同一幂等键用于不同请求体返回 409 `IDEMPOTENCY_CONFLICT`。
- `POST /memory/search` 是读接口，不需要幂等头。

### 2.6 入口防护

- **请求体上限**：默认 1 MiB（`MAX_REQUEST_BODY_BYTES`）。超过时返回 413 `PAYLOAD_TOO_LARGE`，`details.max_bytes` 为当前上限；错误信封与 `X-Request-ID` 正常返回。
- **速率限制**（按登录用户）：

| 接口 | 默认限制 | 配置项 |
|---|---|---|
| `POST /memory/search` | 60 次/分钟 | `RATE_LIMIT_SEARCH_PER_MINUTE` |
| `POST /memory/feedback` | 120 次/分钟 | `RATE_LIMIT_FEEDBACK_PER_MINUTE` |
| `POST /wiki/sync-tasks` | 10 次/小时 | `RATE_LIMIT_SYNC_PER_HOUR` |

超限返回 429 `RATE_LIMITED`，响应头带 `Retry-After`，`details.retry_after_seconds` 同值。Redis 不可用时退回进程内计数，不阻断联调；`RATE_LIMIT_ENABLED=false` 可整体关闭。健康检查与 `/metrics` 不受限流。
- **安全响应头**：所有响应（含错误响应）带 `X-Content-Type-Options: nosniff`、`X-Frame-Options: DENY`、`Referrer-Policy: no-referrer`、`Permissions-Policy`、`Content-Security-Policy`、`Cross-Origin-Opener-Policy: same-origin`、`Cross-Origin-Resource-Policy: same-site`。`HSTS_MAX_AGE_SECONDS > 0` 时追加 `Strict-Transport-Security`；默认关闭，生产启用 TLS 后再设置。
- **CSRF 说明**：当前认证使用 `Authorization: Bearer <JWT>`，没有 Cookie 会话，浏览器不会自动附带凭据，因此不启用 CSRF token。前端经 Next.js 同源 Route Handler 转发；若后续改为 Cookie 会话，必须重新评估并引入 CSRF 防护。

## 3. 已实现接口

机器可读字段定义见 `docs/api/openapi.json`；下表只列前端联调必需信息。

### 3.1 健康检查

| 方法 | 路径 | 权限 | 成功 |
|---|---|---|---|
| GET | `/health/live` | 公开 | 200 |
| GET | `/health/ready` | 公开 | 200 就绪 / 503 未就绪 |

- `/health/live`：`data = {"status": "ok", "ready": null}`。
- `/health/ready`：`data = {"status": "ok"|"unavailable", "ready": bool, "checks": {"configuration": bool, "database": bool, "redis": bool, "models": bool}, "model_checks": {"event_model": bool, "embedding": bool, "reranker": bool}}`；配置缺失时额外返回 `missing_configuration`。
- `models` 检查本地事件模型包（目录、`manifest.json`、权重文件与版本一致性，不加载权重）以及远程 Embedding/Reranker 的可达性；结果默认缓存 30 秒（`READINESS_MODEL_CACHE_SECONDS`），探针高频调用不会重复执行。`READINESS_MODEL_CHECK_ENABLED=false` 时 `models=true`、`model_checks={}`。

### 3.2 事件

| 方法 | 路径 | 权限 | 成功 |
|---|---|---|---|
| POST | `/memory/events` | viewer | 202 |
| GET | `/memory/events` | viewer | 200 |
| GET | `/memory/events/{event_id}` | viewer | 200 |
| POST | `/memory/events/{event_id}/corrections` | admin | 202 |
| DELETE | `/memory/events/{event_id}` | admin（archive/purge） | 202 |
| POST | `/memory/events/{event_id}/restore` | admin | 202 |

创建请求（`EventCreateRequest`）：

| 字段 | 类型 | 必填 | 约束/默认 |
|---|---|---|---|
| `schema_version` | int | 否 | 固定 `1` |
| `subject` | string | 是 | 1～128 |
| `entity_type` | string | 否 | 默认 `topic`，1～32 |
| `event_type` | string | 是 | 1～64 |
| `event_time` | datetime | 是 | 必须带时区 |
| `title` | string | 是 | 1～256 |
| `content` | string | 是 | 1～20000 |
| `source` | `api`\|`import` | 否 | 默认 `api` |
| `source_event_key` | string\|null | 否 | ≤256 |
| `importance` | float | 否 | 0～1，默认 0.5 |
| `visibility` | `private`\|`shared` | 否 | 默认 `private` |

创建响应 `data`：`{"event_id": UUID, "status": "pending", "job_id": UUID}`。`status` 初始恒为 `pending`，后续由 Worker 推进为 `active`/`failed`。

列表查询参数：`subject`、`entity_id`、`event_type`、`start_time`、`end_time`、`status`、`owner_id`、`sort`（`event_time.desc` 默认 / `event_time.asc`）、`cursor`、`limit`（1～100，默认 20）。`owner_id` 只会在已通过可见性过滤的范围内进一步收窄，不能用于越权读取；双向排序均可用 `next_cursor` 续页。

列表项（`EventSummaryResponse`）：`event_id`、`subject`、`event_type`、`title`、`event_time`、`importance`、`status`、`visibility`、`created_at`。

详情额外返回：`content`（清除后为 null）、`entity_id`、`decay_score`、`familiarity_score`、`source`、`supersedes_event_id`、`sequence_no`、`event_model_version`、`updated_at`。

纠偏请求：`title?`、`content`（必填）、`event_time?`、`reason`（必填，≤500）。权限 **admin**（设计 8.2），viewer 请求返回 403。响应 `data`：`{"new_event_id": UUID, "status": "pending", "rebuild_job_id": UUID|null}`。

删除请求（**必须带 JSON 请求体**）：`{"mode": "archive"|"purge", "reason": "..."}`。`archive` 与 `purge` 均要求 **admin**（设计 8.2），viewer 请求返回 403。响应 `data`：`{"event_id": UUID, "status": "archived"|"purged", "purge_after": datetime|null, "rebuild_job_id": UUID|null}`。

恢复请求：`{"reason": "..."}`。响应 `data`：`{"event_id": UUID, "status": "active", "rebuild_job_id": UUID|null}`。

事件状态枚举：`pending`、`active`、`failed`、`archived`、`purged`。

### 3.3 搜索

`POST /memory/search`，权限 viewer，200，无幂等头。

请求：

| 字段 | 类型 | 默认 |
|---|---|---|
| `query` | string（1～2000） | 必填 |
| `session_id` | UUID\|null | null |
| `memory_type` | `all`\|`event`\|`wiki` | `all` |
| `filters.subject` | string\|null | null |
| `filters.entity_ids` | UUID[]（≤100） | `[]` |
| `filters.event_types` | string[]（≤20） | `[]` |
| `filters.start_time` / `filters.end_time` | datetime\|null | null，且 `start_time <= end_time` |
| `filters.wiki_categories` | string[]（≤20） | `[]` |
| `filters.wiki_languages` | string[]（≤5） | `[]` |
| `top_n` | int（1～50） | 10 |
| `enable_familiarity` | bool | true |
| `include_score_breakdown` | bool | true |

响应 `data`：

```json
{
  "query_id": "ec8e4f5e-8928-46c6-bf78-cdbceac5bcfb",
  "intent": "mixed",
  "results": []
}
```

- `intent` ∈ `event`/`wiki`/`mixed`。
- `session_id` 可省略；一旦传入，必须是**已存在且属于调用者**的工作会话（admin 可访问他人会话），否则分别返回 404 `NOT_FOUND` / 403 `FORBIDDEN`。首版不会自动创建会话，需要会话关联时先调用 `POST /working/sessions`。省略时不关联会话，反馈计数中的“同会话去重”按无会话处理。
- 事件结果含：`memory_type="event"`、`memory_id`、`title`、`content`、`event_time`、`final_score`、`feedback_state`、`score_breakdown`、`model_version`。
- Wiki 结果含：`memory_type="wiki"`、`memory_id`（分块 ID）、`article_id`、`title`、`content`、`chunk_index`、`heading_path`、`source{name,url,page_id,revision_id}`、`final_score`、`feedback_state`、`score_breakdown`、`model_version`。
- `score_breakdown` 的事件键：`cosine_similarity`、`rerank_score`（启用精排时才有）、`relevance`、`decay_score`、`familiarity_score`、`memory_weight`、`intent_weight`；Wiki 键：`cosine_similarity`、`rerank_score`（可选）、`importance`、`familiarity_score`、`intent_weight`。数值保留 6 位小数。
- `include_score_breakdown=false` 时 `score_breakdown` 为 null。
- **当前 `feedback_state` 恒为 null**，前端不要依赖该字段展示状态。
- **检索副作用（异步、尽力而为）**：响应返回后，服务会对本次返回的每条结果执行命中强化：`delta = RETRIEVAL_HIT_FAMILIARITY_STEP × max(0, 1 - 当前熟悉度)`，并写回 `familiarity_score = min(1.0, ...)`。两层去重：同一查询指纹 + 同一记忆在 `RETRIEVAL_HIT_DEDUP_MINUTES=60` 内只计一次；同一指纹对同一记忆每天最多一次（`retrieval_hit_gain` 唯一约束）。写入失败只记日志，不影响本次响应；不改变 `access_count` / `last_access_time`，也不会令 `replay_job_created=true`。

### 3.4 反馈

`POST /memory/feedback`，viewer，200，需要 `Idempotency-Key`。

请求：`query_id`、`session_id?`、`memory_type`（`event`|`wiki`）、`memory_id`、`feedback_type`（`exposure`|`clicked`|`adopted`|`helpful`|`not_helpful`）。

响应 `data`：`{"accepted": bool, "counted": bool, "not_counted_reason": string|null, "familiarity_score": float, "replay_job_created": bool}`。

`not_counted_reason` ∈ `duplicate_feedback`、`recent_duplicate_query`、`daily_cap_reached`、`not_consolidating_feedback`。

命中强化使用独立的 `retrieval_hit_gain` 计数，不占用本节反馈的 `FAMILIARITY_DAILY_CAP`；反馈仍是唯一能跨过 `REPLAY_THRESHOLD` 并返回 `replay_job_created=true` 的路径。

### 3.5 工作记忆

| 方法 | 路径 | 权限 | 成功 |
|---|---|---|---|
| POST | `/working/sessions` | viewer | 201 |
| GET | `/working/sessions/{session_id}` | 本人或 admin | 200 |
| POST | `/working/sessions/{session_id}/items` | 本人或 admin | 201 |
| POST | `/working/sessions/{session_id}/consolidate` | 本人或 admin | 202 |
| DELETE | `/working/sessions/{session_id}` | 本人或 admin | 200 |

- 创建请求 `{"token_budget": int|null}`，范围 1000～32000，null 使用服务端默认值。
- 会话对象：`session_id`、`actor_id`、`status`（`active`/`closing`/`closed`/`expired`）、`summary`、`summary_version`、`token_budget`、`token_count`、`last_activity_at`、`expires_at`。
- 详情额外返回 `items[]`，条目含：`item_id`、`sequence_no`、`role`、`content`、`token_count`、`importance`、`explicit_keep`、`consolidated_event_id`、`created_at`。
- 追加条目请求：`role`（`user`/`assistant`/`system`/`tool`）、`content`、`importance`（0～1）、`explicit_keep`。
- 追加响应：`session_id`、`item_id`、`token_count`（本条）、`session_token_count`（累计）、`summary_triggered`、`summary_version`。
- 巩固（无请求体）响应：`{"session_id": UUID, "events": [{"event_id": UUID, "status": "pending"}]}`。
- 关闭响应：`{"session_id": UUID, "status": "closed", "expires_at": datetime|null}`。

### 3.6 实体

| 方法 | 路径 | 权限 | 成功 |
|---|---|---|---|
| GET | `/entities` | viewer | 200 |
| GET | `/entities/{entity_id}` | viewer | 200 |
| GET | `/entities/{entity_id}/state` | viewer | 200 |
| GET | `/entities/{entity_id}/events` | viewer | 200 |
| GET | `/entities/{entity_id}/replays` | viewer | 200 |
| POST | `/entities/{entity_id}/rebuild` | admin | 202 |

- 列表参数：`entity_type`、`status`（`active`/`rebuilding`/`archived`）、`cursor`、`limit`（1～100，默认 20）。
- 列表项与详情见 `EntitySummaryResponse` / `EntityDetailResponse`；详情含 `owner_id`、`source_event_ids`、`summary_version`、`summary_updated_at`、`last_event_id`、`last_event_time`。
- `/state` 不返回张量正文，字段与第 6.6 节一致：`entity_id`、`status`、`state_version`、`model_version`、`last_event_id`、`last_sequence_no`、`last_update_time`、`latest_snapshot_at`、`pending_replay_count`、`active_rebuild_job_id`。
- `/events` 按 `event_time` 倒序返回来源事件，参数 `event_type`、`start_time`、`end_time`、`cursor`、`limit`（1～100，默认 20）；列表项与 `/memory/events` 的 `EventSummaryResponse` 一致。实体不存在或不可见返回 404。
- `/replays` 响应为数组（无游标分页），参数 `limit` 1～100，默认 20；项含 `replay_id`、`event_id`、`reason`、`before_state_version`、`after_state_version`、`model_version`、`status`、`attempt_count`、`error_message`、`created_at`、`completed_at`。
- 重建（无请求体）响应：`{"entity_id": UUID, "job_id": UUID, "status": "queued"}`。

### 3.7 任务与管理

| 方法 | 路径 | 权限 | 成功 |
|---|---|---|---|
| GET | `/jobs/{job_id}` | viewer（仅本人任务或 operator 及以上） | 200 |
| POST | `/jobs/{job_id}/retry` | 同 GET 可见性 | 202 |
| GET | `/dashboard/summary` | viewer | 200 |
| POST | `/admin/memories/{memory_type}/{memory_id}/familiarity` | admin | 200 |

- 任务对象：`job_id`、`job_type`、`resource_type`、`resource_id`、`status`、`progress`、`attempt_count`、`last_error`、`requested_by`、`created_at`、`started_at`、`finished_at`。
- 任务类型：`event_pipeline`、`replay`、`state_rebuild`、`wiki_sync`、`decay`、`reembed`、`purge`。
- 任务状态：`queued`、`running`、`retry_wait`、`succeeded`、`failed`、`dead_letter`、`cancelled`。
- 重试无请求体，返回更新后的任务对象。
- 仪表盘 `data`：`events`（按状态计数）、`wiki`（`active_articles`/`active_chunks`/`last_sync_at` 等）、`jobs`、`outbox`、`alerts`。
- 仪表盘的 `events` 计数与事件列表同口径：只统计调用者可见的事件（`shared` 或本人 `private`），admin 统计全部；`alerts.failed_events` 同样基于可见集合。
- 熟悉度手调请求：`{"score": 0～2, "reason": "..."}`；`memory_type` 仅接受 `event`|`wiki`，其他值 422。响应：`{"memory_type", "memory_id", "familiarity_score", "updated_at"}`。

### 3.8 Wiki 词条与同步

| 方法 | 路径 | 权限 | 成功 |
|---|---|---|---|
| GET | `/wiki/articles` | viewer | 200 |
| GET | `/wiki/articles/{article_id}` | viewer（非 active 词条需 operator 及以上） | 200 |
| GET | `/wiki/articles/{article_id}/chunks/{chunk_index}` | 同上 | 200 |
| POST | `/wiki/sync-tasks` | operator 及以上 | 202 |
| GET | `/wiki/sync-tasks` | viewer | 200 |
| GET | `/wiki/sync-tasks/{task_id}` | viewer | 200 |
| POST | `/wiki/sync-tasks/{task_id}/retry` | operator 及以上 | 202 |

词条列表参数：`q`（标题模糊匹配，≤200；前端已按此参数实现）、`category`、`language`、`status`、`cursor`、`limit`（1～100，默认 20）。

- 不传 `status` 时只返回 `active` 词条；传 `active` 以外的状态需要 operator 及以上，否则 403。
- 列表项：`article_id`、`title`、`summary`、`language`、`categories`、`domain`、`source`、`source_page_id`、`revision_id`、`source_url`、`source_updated_at`、`sync_time`、`status`、`chunk_count`（active 块数量）。列表**不返回** `content`，正文请取详情。
- 详情在列表字段基础上增加：`content`、`toc[]`（`heading_path` + 首个 `chunk_index`）、`chunks[]`。`chunks[]` 项含 `chunk_id`、`chunk_index`、`heading_path`、`raw_text`、`token_count`、`status`、`model_version`。
- 非 `active` 词条对 viewer 返回 404；operator 及以上可加 `include_inactive=true` 查看全部知识块（含 `superseded`/`deleted`）。
- viewer 在详情接口传 `include_inactive=true` 返回 403 `FORBIDDEN`（先于资源存在性判断），不会降级为 404；普通用户界面不要暴露该开关。
- 单块响应：`article_id`、`article_title`、`chunk`（同上的块对象）、`previous_chunk_index`、`next_chunk_index`、`source{name,url,page_id,revision_id}`。导航只按当前可见状态计算，首/末块对应字段为 null。

同步请求：

| 字段 | 类型 | 必填 | 约束 |
|---|---|---|---|
| `source` | string | 是 | 1～64，同一来源同时只允许一个进行中任务 |
| `mode` | `full`\|`incremental`\|`reembed` | 否 | 默认 `incremental` |
| `package_uri` | string\|null | full/incremental 必填 | 1～512；`reembed` 不需要 |
| `package_version` | string | 是 | 1～128，重试必须使用同一版本 |

- 数据包格式：WikiExtractor JSONL，每行 `{id, revid, title, url, text, lang?, timestamp?, redirect?}`；`text` 支持 `== 标题 ==` 层级。首版不接受 `s3://`（返回 422）。
- `package_uri` 支持本地文件、本地目录（读取 `.jsonl`/`.json`/`.txt`）、`file://` 与 `http(s)://`。
- 响应 `data` 为同步任务对象：`task_id`、`job_id`、`source`、`mode`、`package_version`、`package_uri`、`status`、`total_count`、`processed_count`、`success_count`、`failed_count`、`error_sample[]`、`requested_by`、`retryable`、`created_at`、`started_at`、`finished_at`。`error_sample[]` 项为 `{code, message, source_item_id?}`。
- `status` 复用任务状态枚举（`queued`/`running`/`retry_wait`/`succeeded`/`failed`/`dead_letter`/`cancelled`）。
- `full` 模式会解析完整数据包，并把本次未出现的同来源 active 词条标记为 `deleted`；`incremental` 不做删除推断；`reembed` 复用已有正文，仅按当前模型版本批量重算向量。
- 同一来源存在排队/运行/等待重试的任务时返回 409 `TASK_CONFLICT`；重试仅接受 `failed`/`dead_letter`，否则 409 `TASK_CONFLICT`。
- 列表参数：`source`、`mode`、`status`、`cursor`、`limit`（1～100，默认 20）。

### 3.9 观测与 Outbox 死信运维

| 方法 | 路径 | 权限 | 成功 |
|---|---|---|---|
| GET | `/metrics` | 公开；设置 `METRICS_TOKEN` 后要求 Bearer | 200 |
| GET | `/outbox/dead-letters` | admin | 200 |
| POST | `/outbox/dead-letters/{outbox_id}/replay` | admin，需 `Idempotency-Key` | 202 |

- `/metrics` **不在** `/api/v1` 下，返回 Prometheus 文本；包含 `http_requests_total`、`http_request_duration_seconds`、`http_request_errors_total`、`rate_limit_rejections_total`、`events_processed_total`、`outbox_dispatched_total`、`outbox_pending`、`outbox_dead_letter`、`outbox_oldest_pending_seconds`。`METRICS_ENABLED=false` 时返回 404。部署时必须由 Nginx/内网限制来源。
- Outbox 死信列表参数：`event_type`、`aggregate_type`、`cursor`、`limit`（1～100，默认 20）；项含 `outbox_id`、`aggregate_type`、`aggregate_id`、`event_type`、`payload`、`status`、`attempt_count`、`last_error`、`available_at`、`created_at`、`processed_at`。
- 重放只接受 `dead_letter` 状态：重置为 `pending`、`attempt_count=0`、`last_error=null`，由 beat 的 `dispatch_outbox` 重新派发；不存在返回 404，非死信返回 409 `CONFLICT`。响应：`{"outbox_id", "status": "pending", "attempt_count": 0, "available_at"}`。
- **与 `/jobs` 的区别**：`/jobs` 管理业务 `BackgroundJob`（事件管道、重放、重建、Wiki 同步等）；`/outbox/dead-letters` 管理 Outbox 投递死信，属于基础设施层补偿入口，权限固定为 admin。

## 4. 与设计文档第 6 章的差异

| 编号 | 项目 | 第 6 章 | 当前实现 | 前端处理 |
|---|---|---|---|---|
| C-01 | 反馈幂等 | 可用业务唯一约束兼容重试 | `POST /memory/feedback` 同样要求 `Idempotency-Key` | 所有写请求统一生成并携带幂等键 |
| C-02 | 错误信封 | `meta` 只有 `request_id` | `meta` 额外含 `next_cursor: null` | 统一按信封解析，忽略该字段即可 |
| C-03 | 列表 `limit` | 最大 100 | 已对齐：所有游标列表最大 100（默认 20） | 分页大小不超过 100 |
| C-04 | 事件列表参数 | 含 `owner_id`、`sort=event_time.desc\|event_time.asc` | 已实现：`owner_id` 在可见性过滤后收窄；`sort` 支持双向并用同一 `next_cursor` 续页 | 可直接传这两个参数 |
| C-05 | 删除事件 | 请求体含 `mode`、`reason` | 同第 6 章，但 **DELETE 必须发送 JSON 请求体**；`archive` 与 `purge` 均仅 admin | 非管理员不展示归档/清除入口，403 按无权限处理 |
| C-06 | 纠偏/删除/恢复响应 | 未定义响应体 | 返回 `new_event_id`/`event_id`、`status`、`rebuild_job_id`、`purge_after`；纠偏、删除、恢复均为 admin | 按第 3.2 节字段对接 |
| C-07 | 重建路径与权限 | `POST /entities/{id}/state/rebuild`，管理员 | `POST /entities/{id}/rebuild`，**admin**；路由层与服务层均已按 admin 校验，与设计 8.2 一致 | 使用新路径；重建按钮仅 admin 显示 |
| C-08 | 重放记录路径 | `GET /entities/{id}/state/replays` | `GET /entities/{id}/replays` | 使用新路径 |
| C-09 | 实体来源事件 | `GET /entities/{id}/events` | 已实现，返回事件摘要并支持游标分页；也可继续用 `GET /memory/events?entity_id=` | 优先使用实体事件接口 |
| C-10 | 工作记忆状态码 | 未明确 | 创建/追加 201，巩固 202，关闭 200 | 按状态码判断成功 |
| C-11 | 关闭会话响应 | 未定义 | `{session_id, status, expires_at}` | 直接使用 |
| C-12 | 任务可见性 | 未明确 | 非本人且非 operator 返回 404（不泄露存在性） | 404 时展示“任务不存在或无权查看” |
| C-13 | 仪表盘权限 | 未明确 | 按设计 8.2 对 viewer 及以上开放 | 所有登录用户展示概览入口 |
| C-14 | `feedback_state` | 搜索结果可含反馈状态 | 恒为 null（首版未回填） | 不要依赖该字段 |
| C-15 | 事件状态 | 未枚举完整 | `pending`/`active`/`failed`/`archived`/`purged` | 按此渲染状态标签 |
| C-16 | 实体绑定 | 创建事件未含 `entity_id` | 由 `subject + entity_type` 解析/创建实体 | 不需要传 `entity_id` |
| C-17 | Wiki 接口 | 第 6.7 节 7 条 | 已全部实现，路径与第 6.7 节一致；字段细节见第 3.8 节 | 按第 3.8 节对接，无需占位 |
| C-18 | 搜索文档同步 | 未定义 | `score_breakdown` 在 `include_score_breakdown=false` 时为 null | 关闭时不要读取分数明细 |
| C-19 | Wiki 数据包来源 | 示例为 `s3://` | 首版只支持本地路径、`file://`、`http(s)://`；`s3://` 返回 422 `VALIDATION_ERROR` | 同步入口不要提供 S3 选项 |
| C-20 | Wiki 状态可见性 | 非 active 词条 operator 及以上可见（设计 8.4 已同步） | 已与设计对齐：词条/知识块的非 `active` 状态需要 operator 及以上，viewer 访问返回 404；operator 通过 `include_inactive=true` 查看；viewer 传 `include_inactive=true` 返回 403 | 普通用户只展示 active，管理端加开关，权限不足时按 403 提示 |
| C-21 | 重编码任务 | 未定义请求字段 | `mode=reembed` 时 `package_uri` 可省略，`package_version` 仍必填 | 重编码表单隐藏数据包地址 |
| C-22 | 同步任务进度 | 仅要求进度与错误样本 | `total_count` 由数据包行数预估；`progress` 在任务对象中不单独返回，按 `processed_count/total_count` 计算 | 用两个计数字段自行计算百分比 |
| C-23 | 搜索 `session_id` 约束 | 示例含 `session_id`，未说明约束 | 可省略；传入时必须是调用者可见的已有工作会话，尚未创建返回 404，访问他人会话返回 403；首版不自动建会话 | 需要会话关联时先 `POST /working/sessions`，否则省略该字段 |
| C-24 | 入口防护 | 设计 9.8/9.9 要求限流、安全头与请求体约束 | 已实现：搜索 60/分、反馈 120/分、同步创建 10/时；请求体默认 1 MiB；所有响应带安全头；超限分别为 429/413 | 按 `RATE_LIMITED`/`PAYLOAD_TOO_LARGE` 分支提示，尊重 `Retry-After` |
| C-25 | Outbox 死信运维 | 未定义运维接口 | 新增 admin 级 `GET /outbox/dead-letters` 与 `POST /outbox/dead-letters/{id}/replay`，与 `/jobs` 区分 | 仅管理端展示；重放为非幂等补偿操作，按钮需二次确认 |

## 5. 后续里程碑

M2（Wiki 同步）已完成，以下工作仍待推进：

- M3：为前端联调补齐后端接口支撑，按前端反馈增补契约条目。
- M4：安全与观测已完成；质量评估与故障恢复验收待推进。

新增或调整接口时，先更新本文第 3、4 节，再导出 `docs/api/openapi.json`。

## 6. 变更流程

1. 后端修改路由、请求字段、响应字段、状态码或权限时，在同一提交内更新：`docs/API_CONTRACT.md`（含差异表编号）、`docs/CHANGELOG.md`、`docs/api/openapi.json`。
2. 破坏性变更必须新增差异条目并在标题中标注“**破坏性**”，同时给出前端迁移动作。
3. 前端如发现契约缺口，先记录到本文第 4 节，由后端确认后再实现，不自行创造响应字段。
