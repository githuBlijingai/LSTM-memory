# 启动与部署

面向本地 Docker Compose 与容器化部署。模型文件不入镜像，统一通过宿主目录只读挂载；本节约定 `MODEL_DIR` 与容器内路径的写法，以及各模型变量在预检中的校验时机。

> 迁移到另一台机器时，按 [MIGRATION_CHECKLIST.md](MIGRATION_CHECKLIST.md) 执行：资产清点、`.env` 必填项、离线镜像搬运、启动顺序与验收步骤都在这份清单里。

## 1. 启动前准备

- Docker Desktop（Compose v2）。
- 一份可用的模型文件目录（见第 3 节）。M0 预检不会自动下载模型。
- 环境变量文件：仓库根的 `.env`（可从 `.env.example` 复制）。使用 `-f infra/docker-compose.yml` 时 Compose 默认只读取 `infra/` 下的 `.env`，因此从仓库根启动请显式带 `--env-file .env`。

## 2. 快速启动

```powershell
copy .env.example .env
# 编辑 .env：至少设置 MODEL_DIR、EVENT_MODEL_URI、EVENT_MODEL_VERSION 与 Embedding/Reranker 配置
docker compose --env-file .env -f infra/docker-compose.yml up -d --build
docker compose --env-file .env -f infra/docker-compose.yml ps
docker compose --env-file .env -f infra/docker-compose.yml logs -f bootstrap beat worker
```

- `bootstrap` 只做一次性预检，必须成功退出后 `api`/`worker`/`beat` 才会启动。
- 端口可用 `POSTGRES_PORT`、`REDIS_PORT` 覆盖（默认 5432/6379，本机已有 PostgreSQL/Redis 时需要改）。
- 开发登录用 `DEV_AUTH_ENABLED=true` 与 `ENVIRONMENT=development`；生产必须配置 OIDC 三项。

## 2.1 后端镜像两档（基础档 / ml 档）

同一份 `backend/Dockerfile`，通过 `INSTALL_ML` build arg 选择档位；所有依赖都在构建期装好，容器启动时不下载、可离线运行。

| 档位 | build arg | 内容 | 事件编码 |
|---|---|---|---|
| 基础档（Dockerfile 默认） | `INSTALL_ML=false` | 只装 `pyproject.toml` 基础依赖，镜像精简 | 不可用，报 `MODEL_UNAVAILABLE` |
| ml 档（显式开启） | `INSTALL_ML=true` | 额外装 `torch`/`safetensors`/`transformers` | 可用 |

- ml 档的 torch 固定安装 **CPU-only wheel**（`TORCH_CPU_VERSION`，默认 `2.6.0+cpu`；`transformers>=5.23` 要求 torch>=2.6），从 `TORCH_CPU_INDEX_URL`（默认 `https://mirror.sjtu.edu.cn/pytorch-wheels/cpu`）获取，不会带入 cudnn/cublas 等 CUDA 运行时。
- **Compose 默认使用 ml 档**（`INSTALL_ML` 缺省为 `true`），因为设计上事件写入必须能完成编码；只跑 API/迁移等基础能力时，在 `.env` 设置 `INSTALL_ML=false`。
- 直接构建镜像时 Dockerfile 默认是基础档，需要 ml 档要显式传参：

```powershell
# 基础档（精简）
docker build -f backend/Dockerfile -t memory-system-backend:base .
# ml 档（CPU-only 事件编码依赖）
docker build -f backend/Dockerfile --build-arg INSTALL_ML=true -t memory-system-backend:ml .
```


## 3. 模型目录与路径约定

- `MODEL_DIR`：**宿主**上的模型根目录。Compose 将其只读挂载到 `bootstrap`/`api`/`worker` 容器的 `/models`。
- 所有模型路径变量必须写**容器内路径**，以 `/models/` 开头。写 `D:\models\...` 或 `C:\...` 在容器里不存在，预检会报路径不存在。
- Windows 宿主路径建议用正斜杠：`MODEL_DIR=D:/models`。

推荐的宿主目录结构：

```text
D:\models
├── bge-large-zh-v1.5\          # config.json、tokenizer 文件、模型权重
├── bge-reranker-v2-m3\         # 启用 Reranker 时
└── event-model\                # LSTM 事件模型包（Safetensors）
```

```dotenv
MODEL_DIR=D:/models
EMBEDDING_MODEL_NAME=/models/bge-large-zh-v1.5
RERANKER_MODEL_NAME=/models/bge-reranker-v2-m3
EVENT_MODEL_URI=/models/event-model
```

若 `EVENT_MODEL_URI` 是远程地址（含 `://`），预检会跳过路径检查；但当前实现加载事件模型时仍按本地目录读取（要求存在 `manifest.json`），因此实际部署必须把模型包挂载进容器，远程 URL 不可用。

### 3.1 获取 Embedding / Reranker 模型（HuggingFace / ModelScope）

仓库不包含模型文件。下载到 `MODEL_DIR` 下的对应目录，目录名要与 `.env` 变量一致：

| 模型 | 目录 | 变量 |
|---|---|---|
| `BAAI/bge-large-zh-v1.5` | `D:\models\bge-large-zh-v1.5` | `EMBEDDING_MODEL_NAME=/models/bge-large-zh-v1.5` |
| `BAAI/bge-reranker-v2-m3` | `D:\models\bge-reranker-v2-m3` | `RERANKER_MODEL_NAME=/models/bge-reranker-v2-m3` |

```powershell
# HuggingFace（国内先设镜像）
.\.venv312\Scripts\python.exe -m pip install -U huggingface_hub
$env:HF_ENDPOINT = "https://hf-mirror.com"
hf download BAAI/bge-large-zh-v1.5 --local-dir D:/models/bge-large-zh-v1.5
hf download BAAI/bge-reranker-v2-m3 --local-dir D:/models/bge-reranker-v2-m3
```

```powershell
# ModelScope（国内通常更快）
.\.venv312\Scripts\python.exe -m pip install -U modelscope
modelscope download --model BAAI/bge-large-zh-v1.5 --local_dir D:/models/bge-large-zh-v1.5
modelscope download --model BAAI/bge-reranker-v2-m3 --local_dir D:/models/bge-reranker-v2-m3
```

- 必须用 `--local-dir` / `--local_dir` 落盘，不要直接用 `~/.cache/huggingface`（blobs + 符号链接，容器里加载不了）。
- 目录里应有 `config.json`、`model.safetensors`、tokenizer 文件；缺文件会在首次编码时报 `MODEL_UNAVAILABLE`。
- 默认维度对应 `bge-large-zh-v1.5`（1024）。不要下成 `bge-small-zh-v1.5`（512 维）。
- 目标机没网时，在有网机器上下载后整目录拷到目标机 `MODEL_DIR`，无需在目标机安装下载工具。
- 完整校验步骤见 [MIGRATION_CHECKLIST.md](MIGRATION_CHECKLIST.md) 第 1.2.1 节。

## 4. 两种模式下的变量与校验时机

校验分两步，均在 `bootstrap` 预检（`python -m app.bootstrap`）中执行。Compose 固定 `BOOTSTRAP_NON_INTERACTIVE=true`，因此不会进入交互向导，直接校验并退出。

1. **判空**：`Settings.missing_configuration()`，只检查变量是否提供，不检查路径。
2. **判路径**：`_validate_local_files()`，只在需要本地文件时检查路径是否存在。

`api` 与 `worker` 启动时**不重复**上述预检；路径写错通常要等到真正加载模型时才报错。

### 4.1 本地模式（`EMBEDDING_PROVIDER=local`）

```dotenv
EMBEDDING_PROVIDER=local
EMBEDDING_MODEL_NAME=/models/bge-large-zh-v1.5
EMBEDDING_MODEL_VERSION=bge-large-zh-v1.5-v1
RERANKER_ENABLED=true
RERANKER_PROVIDER=local
RERANKER_MODEL_NAME=/models/bge-reranker-v2-m3
EVENT_MODEL_URI=/models/event-model
EVENT_MODEL_VERSION=lstm-v1
MODEL_DEVICE=cuda            # 或 cpu
ALLOW_CPU_MODELS=true        # 仅本地 CPU 开发需要；生产禁止本地 CPU
```

| 变量 | 预检要求 | 说明 |
|---|---|---|
| `EVENT_MODEL_URI` | 必填；无 `://` 时路径必须存在 | 本地模式与远程模式都一样，指向 LSTM 模型包 |
| `EVENT_MODEL_VERSION` | 必填 | 两种模式都一样 |
| `EMBEDDING_PROVIDER` / `EMBEDDING_MODEL_NAME` / `EMBEDDING_MODEL_VERSION` | 必填 | 本地模式下 `EMBEDDING_MODEL_NAME` 必须是已挂载的目录 |
| `EMBEDDING_API_BASE` / `EMBEDDING_API_KEY` | 不校验 | 本地模式不使用 |
| `RERANKER_PROVIDER` / `RERANKER_MODEL_NAME` | `RERANKER_ENABLED=true` 时必填 | 本地模式下 `RERANKER_MODEL_NAME` 必须是已挂载的目录 |
| `RERANKER_MODEL_VERSION` | 不强制 | 可选 |
| `MODEL_DEVICE=cpu` + 本地模型 | 需 `ALLOW_CPU_MODELS=true` | 仅开发/测试；生产环境会直接拒绝本地 CPU 模型 |

### 4.2 远程模式（`EMBEDDING_PROVIDER=remote`）

```dotenv
EMBEDDING_PROVIDER=remote
EMBEDDING_MODEL_NAME=BAAI/bge-large-zh-v1.5
EMBEDDING_MODEL_VERSION=bge-large-zh-v1.5-v1
EMBEDDING_API_BASE=http://host.docker.internal:8080/v1
EMBEDDING_API_KEY=***
RERANKER_ENABLED=true
RERANKER_PROVIDER=remote
RERANKER_MODEL_NAME=BAAI/bge-reranker-v2-m3
RERANKER_API_BASE=http://host.docker.internal:8081/v1
RERANKER_API_KEY=***
EVENT_MODEL_URI=/models/event-model
EVENT_MODEL_VERSION=lstm-v1
```

远程模式只影响 Embedding 与 Reranker；事件模型仍需本地模型包目录。

| 变量 | 预检要求 | 说明 |
|---|---|---|
| `EMBEDDING_API_BASE` / `EMBEDDING_API_KEY` | 远程模式必填 | 容器内不能用 `127.0.0.1` 指宿主，用 `host.docker.internal` 或服务名 |
| `EMBEDDING_MODEL_NAME` | 必填，不校验本地路径 | 填服务端模型名 |
| `EVENT_MODEL_URI` | 必填；无 `://` 时检查路径存在 | 必须是已挂载的本地模型包目录；含 `://` 只会跳过预检，运行时仍按本地目录加载 |
| `EVENT_MODEL_VERSION` | 必填 | 用于状态与向量版本 |
| `RERANKER_API_BASE` / `RERANKER_API_KEY` | `RERANKER_ENABLED=true` 且远程时必填 | 关闭 Reranker 时置 `RERANKER_ENABLED=false` |
| `EMBEDDING_MODEL_VERSION` | 必填 | 远程模式同样要求 |

密钥只通过环境变量或 Secret Manager 注入，不要写入镜像或仓库。

### 4.3 第三方 Embedding / Reranker（按代码现状）

可以接别家服务，但代码只认下面两种 HTTP 形态，不做厂商 SDK 适配。

**Embedding：OpenAI 兼容 `/embeddings`**

```http
POST {EMBEDDING_API_BASE}/embeddings
Authorization: Bearer {EMBEDDING_API_KEY}
Content-Type: application/json

{"model": "<EMBEDDING_MODEL_NAME>", "input": ["文本1", "文本2"], "encoding_format": "float"}
```

响应需要 `{"data": [{"index": 0, "embedding": [...]}]}`，条数与输入一致；`model` 若返回必须与配置一致。向量必须为非零有限数值，长度为 `BASE_EMBEDDING_DIM`（默认 1024）。代码会做 L2 归一化。

**Reranker：`POST /rerank`**

```http
POST {RERANKER_API_BASE}/rerank
Authorization: Bearer {RERANKER_API_KEY}
Content-Type: application/json

{"model": "<RERANKER_MODEL_NAME>", "query": "...", "documents": ["文档1", "文档2"]}
```

响应接受 `{"results": [{"index": 0, "relevance_score": 0.9}]}`、`{"scores": [0.9, 0.1]}` 或裸数组；分数数量要与文档数一致，`[0,1]` 之外视为 logit 并做 sigmoid。

共同行为：

- 只支持 `Authorization: Bearer`；Base URL 必须为 `http(s)`，生产环境强制 `https`，且不能带账号密码、query 或 fragment。
- 单次超时 `MODEL_HTTP_TIMEOUT_SECONDS`（默认 30 秒），不跟随重定向；最多 3 次尝试，网络错误/超时、429、5xx 按 0.25s、0.5s 退避重试，401/403 立即失败。
- 错误码：`MODEL_AUTH_FAILED`(502)、`MODEL_RATE_LIMITED`(503)、`MODEL_NETWORK_ERROR`(503)、`MODEL_ERROR`(502)、`EMBEDDING_INVALID`/`RERANK_INVALID`(502)、`MODEL_UNAVAILABLE`(503)。
- `RERANKER_ENABLED=false` 时跳过精排，只做向量与权重融合；搜索 `score_breakdown` 不再有 `rerank_score`。
- **事件模型不能远程替代**：`EVENT_MODEL_URI` 必须是本地模型包，查询编码器也复用该模型。

维度与已有数据：

- 每次响应都校验维度；不等于 `BASE_EMBEDDING_DIM` 直接 502，不写库。就绪探针的维度常量写死为 1024，因此当前实现实际只支持 1024 维。
- Wiki 知识块向量列为 `vector(1024)`，事件向量列为 `vector(256)`（LSTM 输出）。
- 检索不按模型版本过滤，换供应商后旧向量不会自动重建；混用不同模型的向量结果不可信。Wiki 可用 `POST /wiki/sync-tasks` 的 `mode=reembed` 重算；事件没有批量重编码接口。
- 换供应商按需改 `EMBEDDING_*` 或 `RERANKER_*` 一组变量并重启 `api`/`worker`；换维度还需改代码常量、迁移列、重训事件模型并重嵌入。

完整的请求/响应、错误映射与迁移影响见 [MIGRATION_CHECKLIST.md](MIGRATION_CHECKLIST.md) 第 9 节。

## 5. 预检失败的常见原因

- `EVENT_MODEL_URI 指向的本地模型路径不存在`：写成宿主路径、没以 `/models/` 开头，或 `MODEL_DIR` 未指向正确目录。
- `EMBEDDING_MODEL_NAME/RERANKER_MODEL_NAME 指向的本地模型路径不存在`：本地模式但目录不在 `MODEL_DIR` 下，或容器内路径拼错。
- `配置预检失败，请设置以下环境变量`：漏了必填项；远程模式检查 `*_API_BASE`/`*_API_KEY`，本地模式检查 `*_PROVIDER`。
- `beat` 未启动：确认 `bootstrap` 已成功退出，且 `beat-state` 卷可写；调度文件固定为 `/app/state/celerybeat-schedule`。
- 全量 `up` 停在 `bootstrap`：先看 `docker compose ... logs bootstrap`，再按上面的路径规则修正 `.env`。

## 6. 入口防护、观测与就绪探针

### 6.1 入口防护

| 变量 | 默认 | 说明 |
|---|---|---|
| `SECURITY_HEADERS_ENABLED` | `true` | 关闭后不发送安全响应头，仅用于排障 |
| `CONTENT_SECURITY_POLICY` | `default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'` | 后端只返回 JSON/文本；CSP 不影响 Next.js 前端页面 |
| `HSTS_MAX_AGE_SECONDS` | `0` | 默认不发 HSTS；生产启用 TLS 后设为 `31536000` 等值 |
| `MAX_REQUEST_BODY_BYTES` | `1048576` | 超过返回 413 `PAYLOAD_TOO_LARGE` |
| `RATE_LIMIT_ENABLED` | `true` | 总开关 |
| `RATE_LIMIT_SEARCH_PER_MINUTE` | `60` | 搜索接口，按用户 |
| `RATE_LIMIT_FEEDBACK_PER_MINUTE` | `120` | 反馈接口，按用户 |
| `RATE_LIMIT_SYNC_PER_HOUR` | `10` | Wiki 同步创建，按操作员 |
| `RATE_LIMIT_REDIS_PREFIX` | `rate_limit` | Redis 计数键前缀 |

- Redis 可用时用 Redis 固定窗口计数；Redis 连接失败自动退回进程内计数并记录 warning，不会因限流组件不可用而拒绝正常请求。
- 超限返回 429 `RATE_LIMITED` 与 `Retry-After`。健康检查与 `/metrics` 不参与限流。
- CSRF：当前认证是 `Authorization: Bearer <JWT>`，没有 Cookie 会话，因此不启用 CSRF token；若改为 Cookie 会话必须重新评估。

### 6.2 Prometheus 抓取

- 端点：`GET /metrics`（**不在** `/api/v1` 下），返回 Prometheus 文本。
- 关键指标：`http_requests_total`、`http_request_duration_seconds`、`http_request_errors_total`、`rate_limit_rejections_total`、`events_processed_total`、`outbox_dispatched_total`、`outbox_pending`、`outbox_dead_letter`、`outbox_oldest_pending_seconds`。
- 每次抓取会执行一次 Outbox 聚合查询刷新积压指标；建议抓取间隔不低于 15 秒。
- `METRICS_TOKEN` 留空表示不鉴权；设置后需带 `Authorization: Bearer <token>`。无论是否设置，都应在 Nginx/内网层限制来源，不要直接暴露到公网。
- `METRICS_ENABLED=false` 时 `/metrics` 返回 404。

Prometheus 参考配置：

```yaml
scrape_configs:
  - job_name: memory-system-api
    metrics_path: /metrics
    static_configs:
      - targets: ["api:8000"]
```

### 6.3 OpenTelemetry 链路导出

```dotenv
OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4318/v1/traces
OTEL_SERVICE_NAME=memory-system-api
OTEL_TRACES_SAMPLER_RATIO=0.1
```

- 仅当 `OTEL_EXPORTER_OTLP_ENDPOINT` 非空时启用 OTLP HTTP 导出与 FastAPI 自动插桩。
- 初始化失败（依赖缺失、endpoint 不可用）只记 warning，不阻断服务启动。

### 6.4 就绪探针

`GET /api/v1/health/ready` 在原有配置、数据库、Redis 检查之外增加 `models`：

- 本地事件模型：检查目录、`manifest.json`、权重文件与版本一致性，不加载权重。
- 远程 Embedding/Reranker：用 `READINESS_MODEL_TIMEOUT_SECONDS`（默认 2 秒）短超时探测 `*_API_BASE` 可达性。
- 结果缓存 `READINESS_MODEL_CACHE_SECONDS`（默认 30 秒），探针高频调用不会反复执行；不在启动时预热。
- Kubernetes 探针建议 `periodSeconds: 15`、`timeoutSeconds: 5`，避免缓存过期瞬间与远程探测超时叠加。
- 排障时可设 `READINESS_MODEL_CHECK_ENABLED=false` 暂时跳过模型检查；正常部署不建议关闭。
