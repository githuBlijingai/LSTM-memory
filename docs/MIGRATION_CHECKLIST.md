# 新机器从零部署清单（迁移用）

目标：把当前这套 `memory-system` 原样迁到另一台机器，并在目标机完成可验证的启动。本文以**当前仓库现状**为准，不写“自动下载模型”“一键就绪”这类理想化流程。

先记住三条现状：

- **模型文件不在仓库里**，`MODEL_DIR` 指向的目录必须由你单独拷过去。
- **`.env` 不在仓库里**（`.gitignore` 已忽略），只有 `.env.example`，必须按目标机重新填写。
- **事件模型目前只支持本地目录**：`EVENT_MODEL_URI` 写远程 URL 能通过预检，但运行期仍按本地目录加载，必然失败。

---

## 0. 迁移前先清点

| 资产 | 当前在哪 | 是否随源码走 | 缺了会怎样 |
|---|---|---|---|
| 源码 | `memory-system/` 全目录 | 是（整目录拷贝） | 无法启动 |
| `.env` | 仓库根，被忽略，不存在 | **否** | `bootstrap` 直接失败 |
| 模型根目录 | 由 `MODEL_DIR` 指定，默认 `memory-system/models`（相对 `infra/`） | **否** | `bootstrap` 报路径不存在 |
| 事件模型包 | `MODEL_DIR/event-model/` | **否** | 事件编码与搜索不可用 |
| Embedding 模型 | `MODEL_DIR/bge-large-zh-v1.5/` 或远程服务 | **否** | 事件编码与搜索不可用 |
| Reranker 模型 | `MODEL_DIR/bge-reranker-v2-m3/` 或远程服务 | **否**（可关闭） | `bootstrap` 失败，或检索质量下降 |
| 评估数据集 | `evaluation/data/eval_dataset.json`（约 85 KB） | 是 | 无法复跑离线评估 |
| 训练数据 | `artifacts/training.json`，由数据库导出后生成 | **否** | 无法重训事件模型 |
| 数据库数据 | Compose 卷 `postgres-data` | **否** | 新机器从空库开始 |
| 前端构建产物/镜像 | `memory-system-web` 镜像或本机 `frontend/.next` | **否** | 目标机离线时无法构建 |

---

## 1. 必须带走的资产

### 1.1 源码

整个 `memory-system/` 目录拷走即可，包含 `backend/`、`frontend/`、`infra/`、`scripts/`、`evaluation/`、`docs/`、`pyproject.toml`、`package.json`、`.env.example`。

注意：

- 当前工作目录**没有 `.git`**，不要假设目标机能 `git clone` 或 `git pull`；按目录整体复制。
- `.venv/`、`.venv312/`、`frontend/node_modules/`、`frontend/.next/` 属于本机产物，可不带；目标机重新装依赖或直接用镜像。
- `.memory-system/`（bootstrap 标记）与 `artifacts/` 不在源码里，按需单独带。

### 1.2 模型目录（必须外部提供）

宿主目录建议保持下面结构，目录名与变量必须对得上：

```text
D:\models
├── bge-large-zh-v1.5\          # Embedding，本地模式必需
│   ├── config.json
│   ├── tokenizer.json / tokenizer_config.json / vocab.txt（或 sentencepiece）
│   └── model.safetensors（或 pytorch_model.bin）
├── bge-reranker-v2-m3\         # Reranker，开启时必需
└── event-model\                # LSTM 事件模型包，必需
    ├── manifest.json
    ├── weights.safetensors
    ├── config.json
    └── metrics.json（可选，训练产物）
```

事件模型包的硬性要求（运行期会逐项校验，任一项不满足就报 `MODEL_UNAVAILABLE`）：

| 检查项 | 要求 |
|---|---|
| `manifest.json` | 必须存在，且包含 `model_version`、`base_embedding_dim`、`input_dim`、`hidden_dim`、`num_layers`、`weights_sha256` |
| `model_version` | 必须与 `.env` 的 `EVENT_MODEL_VERSION` **完全一致** |
| 维度 | 默认必须匹配 `BASE_EMBEDDING_DIM=1024`、`LSTM_INPUT_DIM=256`、`LSTM_HIDDEN_DIM=256`、`LSTM_NUM_LAYERS=1`；换模型（如 512 维的 bge-small）必须同步改这些变量并重训模型包 |
| 权重文件 | `weights_file`（缺省 `weights.safetensors`）必须存在、非空，且 SHA-256 与清单一致 |
| `config.json` | 可选；存在时其 `model_version` 必须与清单一致 |

Embedding / Reranker 是标准 HuggingFace 目录，代码用 `AutoTokenizer.from_pretrained` 与 `AutoModel*` 直接读取，目录不完整会在首次编码时报 `MODEL_UNAVAILABLE`。

**明确一点**：仓库里没有 `bge-large-zh-v1.5`、没有 reranker、也没有 `event-model`。本清单只做迁移，不负责生成这些文件。

#### 1.2.1 下载 Embedding / Reranker 模型

两个模型都可以直接从 HuggingFace 或 ModelScope 取。**目录名必须与 `.env` 变量对得上**：

| 模型 | 目标目录 | 对应变量 |
|---|---|---|
| `BAAI/bge-large-zh-v1.5` | `D:\models\bge-large-zh-v1.5` | `EMBEDDING_MODEL_NAME=/models/bge-large-zh-v1.5` |
| `BAAI/bge-reranker-v2-m3` | `D:\models\bge-reranker-v2-m3` | `RERANKER_MODEL_NAME=/models/bge-reranker-v2-m3` |

HuggingFace（国内建议先设镜像）：

```powershell
.\.venv312\Scripts\python.exe -m pip install -U huggingface_hub
$env:HF_ENDPOINT = "https://hf-mirror.com"

# 新版 CLI 是 hf；旧版把 hf 换成 huggingface-cli，参数相同
hf download BAAI/bge-large-zh-v1.5 --local-dir D:/models/bge-large-zh-v1.5
hf download BAAI/bge-reranker-v2-m3 --local-dir D:/models/bge-reranker-v2-m3
```

ModelScope（国内网络通常更快）：

```powershell
.\.venv312\Scripts\python.exe -m pip install -U modelscope

modelscope download --model BAAI/bge-large-zh-v1.5 --local_dir D:/models/bge-large-zh-v1.5
modelscope download --model BAAI/bge-reranker-v2-m3 --local_dir D:/models/bge-reranker-v2-m3
```

注意：

- **不要用默认缓存目录**（`~/.cache/huggingface`）。缓存是 blobs + 符号链接结构，容器里只挂载 `MODEL_DIR`，直接指缓存目录会加载失败；一律用 `--local-dir` / `--local_dir` 落到模型目录。
- 不要下成 `bge-small-zh-v1.5`。默认 `BASE_EMBEDDING_DIM=1024` 对应 large；small 是 512 维，换它必须同时改维度并重训事件模型包。
- `bge-large-zh-v1.5` 约 1.3 GB，`bge-reranker-v2-m3` 约 2.3 GB（以实际下载为准），留足磁盘。

下载完先在本机验证目录完整：

```powershell
Get-ChildItem D:\models\bge-large-zh-v1.5 | Select-Object Name, Length
Get-ChildItem D:\models\bge-reranker-v2-m3 | Select-Object Name, Length
```

至少要看到 `config.json`、`model.safetensors`（或 `pytorch_model.bin`）、`tokenizer.json`/`tokenizer_config.json`/`vocab.txt`。再用容器确认挂载路径可见：

```powershell
docker compose --env-file .env -f infra/docker-compose.yml run --rm --no-deps --entrypoint ls api /models/bge-large-zh-v1.5
```

最后跑一次预检，`bootstrap` 的路径检查通过即说明目录名与容器内路径都对了：

```powershell
docker compose --env-file .env -f infra/docker-compose.yml run --rm bootstrap
```

**目标机没网时**：在一台有网的机器上按上面命令下载到 `D:\models`，然后把整个 `bge-large-zh-v1.5` 与 `bge-reranker-v2-m3` 目录拷到目标机的 `MODEL_DIR`。模型是普通文件，不需要目标机装 `huggingface_hub` 或 `modelscope`，也不需要联网。

### 1.3 评估数据集

`evaluation/data/eval_dataset.json` 已在仓库里，随源码带走即可。`evaluation/results/` 下的 `baseline.json`、`REPORT.md` 是产物，可不带；需要时执行：

```powershell
.\evaluation\run_all.ps1
```

重复生成的数据集是确定性的（UUIDv5），逐字节一致。

### 1.4 训练数据

`artifacts/training.json` **不在仓库里**，需要从已有数据库导出：

```powershell
.\.venv312\Scripts\python.exe -m training.export --output artifacts/training.json
```

导出口径：仅 `ACTIVE` 事件与计入熟悉度的 helpful 反馈；删除/隐私擦除事件被排除。没有历史数据的新机器导不出有效训练集，只能从旧机器把 `artifacts/training.json` 一起带走。

重训与评估：

```powershell
.\.venv312\Scripts\python.exe -m training.train --dataset artifacts\training.json --output-dir artifacts\event-model
.\.venv312\Scripts\python.exe -m training.evaluate --dataset artifacts\training.json --model-dir artifacts\event-model
```

训练产物目录可直接作为 `MODEL_DIR/event-model`，但 `EVENT_MODEL_VERSION` 要改成训练输出的 `model_version`。

### 1.5 可选：现有业务数据

要把旧机器的记忆数据一起迁走时，二选一：

```powershell
# 方式一：逻辑备份（推荐，跨机器/跨版本安全）
docker exec memory-system-db-1 pg_dump -U memory -d memory -Fc -f /tmp/memory.dump
docker cp memory-system-db-1:/tmp/memory.dump .\memory.dump
# 目标机
docker cp .\memory.dump memory-system-db-1:/tmp/memory.dump
docker exec memory-system-db-1 pg_restore -U memory -d memory --clean --if-exists /tmp/memory.dump
```

```powershell
# 方式二：直接复制卷目录（要求目标机 Docker 卷同名且版本一致）
docker run --rm -v memory-system_postgres-data:/data -v ${PWD}:/backup alpine tar czf /backup/postgres-data.tgz -C /data .
```

Redis 只存放限流计数与 Celery 队列，不需要迁移。

### 1.6 密钥

`EMBEDDING_API_KEY`、`RERANKER_API_KEY` 只通过环境变量或 Secret Manager 注入，不写进镜像、不写进仓库；迁移时单独传递，不要塞进代码包。

---

## 2. 模型目录结构与挂载

- `.env` 中的 `MODEL_DIR` 是**宿主路径**，Compose 将其只读挂载到 `bootstrap`/`api`/`worker` 容器的 `/models`。
- Compose 未设置 `MODEL_DIR` 时默认 `../models`，相对于 `infra/docker-compose.yml`，即 `memory-system/models`。
- Windows 上用正斜杠，避免反斜杠转义问题：

```dotenv
MODEL_DIR=D:/models
EMBEDDING_MODEL_NAME=/models/bge-large-zh-v1.5
RERANKER_MODEL_NAME=/models/bge-reranker-v2-m3
EVENT_MODEL_URI=/models/event-model
```

规则：

1. **所有模型路径变量写容器内路径**，必须以 `/models/` 开头；写 `D:\models\...` 在容器里不存在。
2. 预检分两步：先判空（`EVENT_MODEL_URI`、`EVENT_MODEL_VERSION`、Embedding 配置等），再判路径（本地目录是否存在）。路径检查只在 `bootstrap` 跑一次。
3. `api`/`worker` 启动时**不重复**预检；路径写错通常要等到第一次编码/搜索才暴露。
4. `EVENT_MODEL_URI` 含 `://` 时预检会跳过路径检查，但运行期 `load_model_package()` 仍按本地目录读取，所以**远程 URL 实际不可用**。
5. 容器以只读方式挂载，训练产物要落在宿主目录，不要试图在容器内写 `/models`。

---

## 3. `.env` 必填项

从模板复制后逐项核对：

```powershell
Copy-Item .env.example .env
```

### 3.1 两种部署方式都要有的项

| 变量 | 说明 | 为空/错误的结果 |
|---|---|---|
| `ENVIRONMENT` | `development` / `test` / `production` | 默认 `development` |
| `POSTGRES_PASSWORD` | Compose 数据库密码 | 用默认值 `change-me`，生产必须改 |
| `POSTGRES_PORT` | 宿主端口，本机建议 `55432` | 与已有 PostgreSQL 冲突时 db 起不来 |
| `REDIS_PORT` | 宿主端口，本机建议 `56379` | 与已有 Redis 冲突时 redis 起不来 |
| `MODEL_DIR` | 宿主模型根目录 | 挂载为空目录，预检报路径不存在 |
| `INSTALL_ML` | Compose 默认 `true`（ml 档） | `false` 时事件编码报 `MODEL_UNAVAILABLE` |
| `EVENT_MODEL_URI` | 容器内路径，如 `/models/event-model` | **bootstrap 失败** |
| `EVENT_MODEL_VERSION` | 必须等于模型包 `model_version` | **bootstrap 失败**；写错则加载时报版本不一致 |
| `EMBEDDING_PROVIDER` | `local` 或 `remote` | **bootstrap 失败** |
| `EMBEDDING_MODEL_NAME` | 本地目录或远程模型名 | **bootstrap 失败** |
| `EMBEDDING_MODEL_VERSION` | 向量版本标识 | **bootstrap 失败**（本地/远程都要求） |
| `RERANKER_ENABLED` | 建议按实际情况显式给值 | 默认 `true`，没 reranker 时会连带要求其配置 |
| `MODEL_DEVICE` | 本地模型用 `cpu` 或 `cuda` | 本地 CPU 需 `ALLOW_CPU_MODELS=true` |
| `DEV_AUTH_ENABLED` | 本地无 OIDC 时设 `true` | 否则所有业务接口 401 |

补充：

- **Compose 会覆盖 `DATABASE_URL` / `REDIS_URL`**：容器内固定连 `db:5432`、`redis:6379`。`.env` 里的这两个值只对“在宿主直接跑后端”有用（此时要写 `127.0.0.1:55432` / `127.0.0.1:56379`）。
- `ENVIRONMENT=production` 时 `DEV_AUTH_ENABLED=true` 会在加载配置时直接报错，进程起不来；生产必须配 OIDC 三项。

### 3.2 本地模型模式

```dotenv
MODEL_DIR=D:/models
EMBEDDING_PROVIDER=local
EMBEDDING_MODEL_NAME=/models/bge-large-zh-v1.5
EMBEDDING_MODEL_VERSION=bge-large-zh-v1.5-v1
RERANKER_ENABLED=true
RERANKER_PROVIDER=local
RERANKER_MODEL_NAME=/models/bge-reranker-v2-m3
EVENT_MODEL_URI=/models/event-model
EVENT_MODEL_VERSION=lstm-v1
MODEL_DEVICE=cpu
ALLOW_CPU_MODELS=true
```

- 本地 + `MODEL_DEVICE=cpu` + `ALLOW_CPU_MODELS=false` 会**直接进 bootstrap 缺失清单**；CPU 仅限开发/测试。
- 生产环境禁止本地 CPU 模型（`ENVIRONMENT=production` 时直接拒绝）。
- 没有 reranker 就把 `RERANKER_ENABLED=false`，否则必须提供 `RERANKER_PROVIDER` 与 `RERANKER_MODEL_NAME`。

### 3.3 远程 Embedding / Reranker 模式

```dotenv
EMBEDDING_PROVIDER=remote
EMBEDDING_MODEL_NAME=BAAI/bge-large-zh-v1.5
EMBEDDING_MODEL_VERSION=bge-large-zh-v1.5-v1
EMBEDDING_API_BASE=http://host.docker.internal:8080/v1
EMBEDDING_API_KEY=***
RERANKER_ENABLED=true
RERANKER_PROVIDER=remote
RERANKER_MODEL_NAME=BAAI/bge-reranker-v2-m3
RERANKER_MODEL_VERSION=bge-reranker-v2-m3-v1
RERANKER_API_BASE=http://host.docker.internal:8081/v1
RERANKER_API_KEY=***
EVENT_MODEL_URI=/models/event-model
EVENT_MODEL_VERSION=lstm-v1
```

- 容器内不能用 `127.0.0.1` 指宿主，用 `host.docker.internal` 或同一网络的服务名。
- 远程模式只替换 Embedding/Reranker；**事件模型仍然必须是本地模型包**。
- 远程模式同样要求 `EMBEDDING_MODEL_VERSION`；`RERANKER_MODEL_VERSION` 非强制但建议填。

### 3.4 判定“空格会炸”的最短清单

以下任意一项为空，`python -m app.bootstrap` 会以退出码 2 结束，并打印 `配置预检失败，请设置以下环境变量或修复配置`：

- `EVENT_MODEL_URI`
- `EVENT_MODEL_VERSION`
- `EMBEDDING_PROVIDER`
- `EMBEDDING_MODEL_NAME`
- `EMBEDDING_MODEL_VERSION`
- 远程模式下：`EMBEDDING_API_BASE`、`EMBEDDING_API_KEY`
- `RERANKER_ENABLED=true` 时：`RERANKER_PROVIDER`、`RERANKER_MODEL_NAME`；远程另需 `RERANKER_API_BASE`、`RERANKER_API_KEY`

路径存在但版本不对、权重缺失等情况，`bootstrap` 通过、**运行期**报 `MODEL_UNAVAILABLE`。

---

## 4. 镜像：两档与离线迁移

### 4.1 两档镜像

| 档位 | build arg | 内容 | 本机实测体积 | 事件编码 |
|---|---|---|---|---|
| 基础档 | `INSTALL_ML=false` | 仅基础依赖 | 325 MB | 不可用，报 `MODEL_UNAVAILABLE` |
| ml 档 | `INSTALL_ML=true` | 额外装 CPU-only `torch` / `safetensors` / `transformers` | 1.73 GB | 可用 |

Compose 默认 `INSTALL_ML=true`，即 ml 档。torch 固定 CPU wheel，不带 CUDA 运行时。

手动构建：

```powershell
# 基础档
docker build -f backend/Dockerfile -t memory-system-backend:base .

# ml 档（CPU-only）
docker build -f backend/Dockerfile --build-arg INSTALL_ML=true -t memory-system-backend:ml .
```

注意：Compose 不使用上面这两个手动标签。`docker compose build` 会为每个服务生成独立镜像名：

```text
memory-system-bootstrap
memory-system-api
memory-system-worker
memory-system-beat
memory-system-web
```

四个后端镜像来自同一 Dockerfile、共享大部分层；离线迁移时按这些名字保存。

### 4.2 在线构建

```powershell
docker compose --env-file .env -f infra/docker-compose.yml build
docker compose --env-file .env -f infra/docker-compose.yml up -d
```

### 4.3 离线迁移（目标机网络差时唯一可行）

后端构建要访问 PyPI 与 torch wheel 源，前端构建要 `npm ci`，目标机没网时**不要重建**，改为搬运镜像。

源机：

```powershell
docker compose --env-file .env -f infra/docker-compose.yml build
docker save -o memory-system-images.tar `
  memory-system-bootstrap:latest `
  memory-system-api:latest `
  memory-system-worker:latest `
  memory-system-beat:latest `
  memory-system-web:latest `
  pgvector/pgvector:pg16 `
  redis:7-alpine `
  nginx:1.27-alpine
```

目标机：

```powershell
docker load -i memory-system-images.tar
docker images | Select-String "memory-system|pgvector|redis|nginx"
```

然后正常 `up`，**不要加 `--build`**：

```powershell
docker compose --env-file .env -f infra/docker-compose.yml up -d
```

补充：

- ml 镜像本体 1.73 GB，`docker save` 产物通常与之相当或更大；四个后端镜像共享层，但 tar 包仍会明显超过 2 GB，预留足够磁盘。
- 目标机若执行带 `--build` 的 `up`，会因拉不到依赖而失败。
- 基础依赖镜像 `pgvector/pgvector:pg16`（621 MB）、`redis:7-alpine`（58.5 MB）、`nginx:1.27-alpine`（74.5 MB）也要一起带走。
- 前端镜像必须用源机构建好的产物；目标机重建需要 npm 源。

---

## 5. 端口与启动顺序

| 服务 | 宿主端口 | 容器端口 | 备注 |
|---|---|---|---|
| db | `POSTGRES_PORT`，本机 55432 | 5432 | 本机已有 PostgreSQL 时不要用 5432 |
| redis | `REDIS_PORT`，本机 56379 | 6379 | 本机 6379 被占用时改 |
| bootstrap | 无 | 无 | 一次性预检，必须退出码 0 |
| api | 8000（Compose 固定） | 8000 | 依赖 bootstrap 成功 |
| worker | 无 | 无 | Celery worker |
| beat | 无 | 无 | 每秒派发一次 Outbox |
| web | 3000（Compose 固定） | 3000 | 前端 |
| nginx | 8080 | 80 | 反代 web |
| 前端 3100 | 3100 | 3000 | 需额外指定，见 5.2 |

### 5.1 启动顺序

```powershell
# 1) 数据面
docker compose --env-file .env -f infra/docker-compose.yml up -d db redis

# 2) 单独跑预检（失败时先修 .env，不要急着 up）
docker compose --env-file .env -f infra/docker-compose.yml run --rm bootstrap

# 3) 应用面：up 仍会再执行一次 bootstrap，它成功退出后才启动 api/worker/beat
docker compose --env-file .env -f infra/docker-compose.yml up -d
docker compose --env-file .env -f infra/docker-compose.yml ps -a

# 4) 前端（按需）
docker compose --env-file .env -f infra/docker-compose.yml up -d web nginx
```

`api`、`worker`、`beat` 都带 `depends_on: bootstrap: service_completed_successfully`；预检失败时它们不会启动，这是设计行为，不是编排 bug。

`beat` 的调度文件固定为 `/app/state/celerybeat-schedule`，挂载 `beat-state` 卷；该卷不可写会导致 beat 启动失败。

### 5.2 前端跑在 3100

Compose 的 `web` 固定映射 `3000:3000`，不能通过 `.env` 改。两种做法：

```powershell
# 方式一：本机开发模式（推荐本地验证，Dev Auth 生效）
cd frontend
$env:BACKEND_URL="http://127.0.0.1:8000"
$env:DEV_AUTH_ENABLED="true"
$env:DEV_AUTH_SUBJECT="dev-admin"
$env:DEV_AUTH_ROLES="admin"
npm run dev -- --port 3100
```

```powershell
# 方式二：复用已加载的 web 镜像，映射到 3100
docker run -d --name memory-system-web-3100 `
  --network memory-system_default `
  -p 3100:3000 `
  -e BACKEND_URL=http://api:8000 `
  memory-system-web:latest
```

注意：`memory-system-web` 是生产构建（`NODE_ENV=production`），前端 Dev Auth 在该模式下**不生效**，没有真实 OIDC 时页面会 401。本地无 OIDC 时请用方式一。

前端必要环境变量：

| 变量 | 作用 | 缺失结果 |
|---|---|---|
| `BACKEND_URL` | Next.js 服务端转发后端 | 页面返回 503 `BACKEND_URL_MISSING` |
| `DEV_AUTH_ENABLED` | 开发登录开关 | 默认关闭 |
| `DEV_AUTH_SUBJECT` / `DEV_AUTH_ROLES` | 开发身份与角色 | 默认 `dev-user` / `viewer` |
| `NEXT_PUBLIC_APP_ENV` | 页面环境标识 | 不影响启动 |

---

## 6. 启动后验证清单

以下命令按 PowerShell 写，Dev Auth 用 admin 身份。

```powershell
$Base = "http://127.0.0.1:8000"
$Admin = @{ Authorization = "Dev admin"; "X-Dev-Roles" = "admin" }
```

### 6.1 存活与就绪

```powershell
Invoke-RestMethod "$Base/api/v1/health/live"

$ready = Invoke-RestMethod "$Base/api/v1/health/ready"
$ready.data.checks
$ready.data.model_checks
$ready.data.missing_configuration
```

期望：

- `/health/live` 返回 `data.status = "ok"`。
- `/health/ready` 返回 HTTP 200，`checks.configuration/database/redis/models` 全为 `true`；`model_checks` 至少包含本地事件模型与远程 Embedding/Reranker 的结果。
- 任一为 `false` 时接口返回 503，响应体里带 `missing_configuration` 或 `model_check_error`。

### 6.2 写入事件，确认 Outbox 被 beat 自动消化

```powershell
$body = @{
  schema_version   = 1
  subject          = "alice"
  entity_type      = "topic"
  event_type       = "meeting"
  event_time       = (Get-Date).ToUniversalTime().ToString("o")
  title            = "发布计划评审"
  content          = "决定 12 月 1 日发布 1.0，先灰度 10% 观察两天。"
  importance       = 0.8
  visibility       = "private"
} | ConvertTo-Json

$created = Invoke-RestMethod -Method Post `
  -Uri "$Base/api/v1/memory/events" `
  -Headers ($Admin + @{ "Idempotency-Key" = [guid]::NewGuid().ToString() }) `
  -ContentType "application/json" `
  -Body $body

$created.data    # 期望 status=pending，拿到 event_id 与 job_id
```

接口返回 **202**，事件初始状态 `pending`。`beat` 每秒执行一次 `app.tasks.outbox.dispatch_outbox`，worker 消费后转 `active` 并完成编码：

```powershell
$eventId = $created.data.event_id
1..20 | ForEach-Object {
  $detail = Invoke-RestMethod "$Base/api/v1/memory/events/$eventId" -Headers $Admin
  "{0} status={1} seq={2} model={3}" -f $_, $detail.data.status, $detail.data.sequence_no, $detail.data.event_model_version
  if ($detail.data.status -eq "active") { break }
  Start-Sleep -Seconds 2
}
```

期望最终 `status=active`、`sequence_no` 有值、`event_model_version` 等于 `.env` 的 `EVENT_MODEL_VERSION`。若一直 `pending`，看第 7 节“beat 不调度”；若变 `failed`，看 `/outbox/dead-letters` 的 `last_error`。

### 6.3 搜索命中刚写入的事件

```powershell
$searchBody = @{
  query           = "发布计划 灰度"
  memory_type     = "event"
  top_n           = 10
  enable_familiarity = $true
} | ConvertTo-Json

$result = Invoke-RestMethod -Method Post `
  -Uri "$Base/api/v1/memory/search" `
  -Headers $Admin `
  -ContentType "application/json" `
  -Body $searchBody

$result.data.intent
$result.data.results | Select-Object memory_type, title, final_score
```

期望 `results` 中包含刚写入的事件，`memory_type=event`，`final_score` 不为 0。搜不到时先确认 6.2 已经 `active`，再确认模型与向量维度一致。

### 6.4 其余主要接口逐个打一次

```powershell
Invoke-RestMethod "$Base/api/v1/dashboard/summary" -Headers $Admin
Invoke-RestMethod "$Base/api/v1/memory/events?limit=5" -Headers $Admin
Invoke-RestMethod "$Base/api/v1/entities?limit=5" -Headers $Admin

# 取列表里的 entity_id 再打详情
$entityId = (Invoke-RestMethod "$Base/api/v1/entities?limit=1" -Headers $Admin).data[0].entity_id
Invoke-RestMethod "$Base/api/v1/entities/$entityId" -Headers $Admin
Invoke-RestMethod "$Base/api/v1/entities/$entityId/state" -Headers $Admin

Invoke-RestMethod "$Base/api/v1/wiki/articles?limit=5" -Headers $Admin
Invoke-RestMethod "$Base/api/v1/wiki/sync-tasks?limit=5" -Headers $Admin
```

期望全部 HTTP 200，`data` 是数组或对象，不是错误结构；Wiki 列表为空也正常（空库时 `data = []`）。Wiki 详情需要先有词条，取列表第一项的 `article_id` 再请求 `/wiki/articles/{article_id}`。

### 6.5 指标与死信

```powershell
# Prometheus 文本
(Invoke-WebRequest "$Base/metrics").Content | Select-String "http_requests_total|events_processed_total|outbox_pending|outbox_dead_letter"

# 死信列表（admin）
Invoke-RestMethod "$Base/api/v1/outbox/dead-letters?limit=20" -Headers $Admin
```

期望：

- `http_requests_total` 能看到刚才的请求；
- `events_processed_total` 的 `succeeded` 计数增加；
- `outbox_pending` 回落，`outbox_dead_letter` 为 0；
- 死信列表返回 200（空列表也是 `data = []`），不是 404/403。

### 6.6 前端页面

打开 `http://127.0.0.1:3100`，逐个确认：

| 页面 | 期望 |
|---|---|
| `/dashboard` | 概览数字与 6.4 的 `/dashboard/summary` 一致 |
| `/memories/events` | 列表出现 6.2 写入的事件 |
| `/memories/events/{id}` | 详情能显示标题、内容、状态 `active` |
| `/entities`、`/entities/{id}` | 有实体与状态信息 |
| `/search` | 搜“发布计划 灰度”能返回结果 |
| `/wiki`、`/wiki/{articleId}` | 有数据时能打开；空库时显示空态而非报错 |
| `/jobs`、`/jobs/wiki-sync` | 能看到 6.2 产生的任务记录 |

浏览器 Network 里前端请求应走 `/api/v1/*` 同源转发，响应 200；出现 503 `BACKEND_URL_MISSING` 说明前端没拿到 `BACKEND_URL`。

---

## 7. 常见失败与处置

### 7.1 bootstrap 校验失败（退出码 2）

特征：日志末尾 `配置预检失败，请设置以下环境变量或修复配置`，逐项列出变量名。

处置：

1. 按第 3.4 节补齐变量；注意 Compose 固定 `BOOTSTRAP_NON_INTERACTIVE=true`，不会进入交互向导。
2. 改完 `.env` 后重新执行 `run --rm bootstrap`，不要直接 `up`。
3. 退出码 2 时 `api`/`worker`/`beat` 不会启动，这是预期行为。

### 7.2 bootstrap 路径检查失败（退出码 3）

特征：`EVENT_MODEL_URI 指向的本地模型路径不存在；M0 不会自动下载或安装模型`，或 `EMBEDDING_MODEL_NAME/RERANKER_MODEL_NAME 指向的本地模型路径不存在`。

处置：

1. 确认 `MODEL_DIR` 是宿主真实路径，且该目录下有对应子目录。
2. 确认变量写的是**容器内路径**（`/models/...`），不是 `D:\models\...`。
3. 用 `docker compose ... run --rm bootstrap ls /models` 验证挂载内容。
4. `EVENT_MODEL_URI` 写远程 URL 只能绕过这一步，运行期仍会失败，不要这么做。

### 7.3 `MODEL_UNAVAILABLE`

不同触发点的特征与处置：

| 报错信息 | 触发点 | 处置 |
|---|---|---|
| `事件模型目录不存在` | 首次编码/搜索 | 检查 `MODEL_DIR` 挂载与 `EVENT_MODEL_URI` |
| `模型包缺少 manifest.json` | 首次编码/搜索 | 放入完整模型包，不要只拷权重 |
| `模型包版本与 EVENT_MODEL_VERSION 不一致` | 首次编码/搜索 | 改 `.env` 或模型包，使两者完全一致 |
| `模型包 base_embedding_dim 与配置不一致` | 首次编码/搜索 | 维度与 `BASE_EMBEDDING_DIM` 对齐；换 Embedding 模型必须重训 |
| `模型包权重校验和不匹配` | 首次编码/搜索 | 权重复制损坏或被替换，重新拷贝 |
| `safetensors 未安装` | 首次编码/搜索 | 用了基础档镜像，改 `INSTALL_ML=true` 重建或改用 ml 镜像 |
| `本地 Embedding 模型加载失败` | 首次编码/搜索 | HF 目录不完整（缺 config/tokenizer/权重） |
| `远程 Embedding 调用失败` | 首次编码/搜索 | 检查 `EMBEDDING_API_BASE` 在容器内可达、密钥有效 |

排查入口：`GET /api/v1/outbox/dead-letters` 的 `last_error`，以及 `docker compose logs worker`。

### 7.4 beat 不调度

特征：事件一直 `pending`，`outbox_pending` 不下降，worker 日志没有 `dispatch_outbox`。

处置：

1. `docker compose ps` 确认 `beat` 在运行；它依赖 `bootstrap` 成功退出。
2. `docker compose logs beat` 看是否有调度文件不可写错误；调度文件固定 `/app/state/celerybeat-schedule`，`beat-state` 卷必须可写。
3. 确认 `redis` 健康；beat 与 worker 都通过 Redis broker 通信。
4. 手动触发一次验证：`docker compose exec worker python -m celery -A app.worker.celery_app call app.tasks.outbox.dispatch_outbox`。
5. beat 起不来时不要只重启 worker，先修 beat。

### 7.5 镜像源太慢或构建失败

特征：`docker compose build` 卡在 pip/npm 下载，或 torch wheel 超时。

处置：

1. 后端构建已固定 CPU wheel 源，可用 `.env` 的 `TORCH_CPU_INDEX_URL` 覆盖（默认 `https://mirror.sjtu.edu.cn/pytorch-wheels/cpu`）。
2. pip 主源见 `infra/pip.ini`（清华主源 + 阿里云备源）；构建慢时优先换网络，而不是反复重试。
3. 目标机无网时走第 4.3 节离线镜像，不要重建。
4. 前端 `npm ci` 慢时同理，直接用源机构建好的 `memory-system-web` 镜像。

### 7.6 端口被占用

特征：`db`/`redis`/`api` 起不来，日志 `port is already allocated`。

处置：改 `.env` 的 `POSTGRES_PORT`、`REDIS_PORT`。`api`（8000）与 `web`（3000）在 Compose 里是固定映射，冲突时停掉占用进程，或不用 Compose 的 `web`、改在本机起 3100。

### 7.7 前端 401 / 503

| 现象 | 原因 | 处置 |
|---|---|---|
| 页面 503 `BACKEND_URL_MISSING` | 前端没配置后端地址 | 设置 `BACKEND_URL` 后重启前端 |
| 接口 401 | Dev Auth 未启用或生产构建 | 本机用 `npm run dev` + `DEV_AUTH_ENABLED=true`；生产配 OIDC |
| 接口 429 | 触发限流（搜索 60/分、反馈 120/分、同步 10/时） | 等 `Retry-After`；联调可临时 `RATE_LIMIT_ENABLED=false` |
| 接口 413 | 请求体超过 1 MiB | 调整 `MAX_REQUEST_BODY_BYTES` 或缩小请求体 |

---

## 8. 迁移完成判定

- [ ] `docker compose ps` 中 db、redis、api、worker、beat 均为 `running`，bootstrap 为 `exited (0)`。
- [ ] `/api/v1/health/ready` 返回 200，`checks` 四项全 `true`，`model_checks` 无 error。
- [ ] 写入事件返回 202，轮询后 `status=active`，`event_model_version` 与配置一致。
- [ ] 同一事件能被 `/api/v1/memory/search` 检索到。
- [ ] 概览、事件列表、实体详情、搜索、Wiki 列表/详情、同步任务均返回 200。
- [ ] `/metrics` 能看到本轮请求与 `events_processed_total` 增长。
- [ ] `/api/v1/outbox/dead-letters` 可访问且为空或错误可解释。
- [ ] 前端 3100 的主要页面能取到真实数据。

---

## 9. 第三方 Embedding / Reranker 对接（按代码现状）

先给结论：**可以换别家的 Embedding / Reranker，但有明确前提**——对方必须提供下面这两种 HTTP 接口形态之一，且向量维度必须是 1024（或同步改配置并重训事件模型）。代码没有做“任意厂商 SDK 适配”，也没有自动迁移旧向量。

实现位置：[embedding.py](file:///d:/记忆库/memory-system/backend/app/ml/embedding.py) 的 `RemoteEmbeddingProvider`、[reranker.py](file:///d:/记忆库/memory-system/backend/app/ml/reranker.py) 的 `RemoteReranker`。

### 9.1 远程 Embedding：OpenAI 兼容 `/embeddings`

**不是自定义协议**，就是 OpenAI Embeddings 的请求/响应形状：

```http
POST {EMBEDDING_API_BASE}/embeddings
Authorization: Bearer {EMBEDDING_API_KEY}
Content-Type: application/json
```

```json
{
  "model": "BAAI/bge-large-zh-v1.5",
  "input": ["第一条文本", "第二条文本"],
  "encoding_format": "float"
}
```

- `input` 永远是**字符串数组**，即使只有一条；批量编码时会一次传多条。
- 期望响应：

```json
{
  "model": "BAAI/bge-large-zh-v1.5",
  "data": [
    {"index": 0, "embedding": [0.01, -0.02, "..."]},
    {"index": 1, "embedding": [0.03, 0.04, "..."]}
  ]
}
```

- `data` 数量必须与输入条数一致；带 `index` 时按 `index` 排序，不带则按返回顺序。
- `model` 字段可省略；一旦返回，必须与 `EMBEDDING_MODEL_NAME` 完全一致。
- 每个向量必须是有限数值、非零向量、长度等于 `BASE_EMBEDDING_DIM`（默认 1024）。代码会再做一次 L2 归一化，远端不归一化也能用。
- 鉴权只支持 `Authorization: Bearer <key>`；不支持自定义 header、AK/SK 签名、query 参数带密钥。
- Base URL 必须是 `http(s)`，生产环境必须 `https`；不能带用户名密码、query 或 fragment。
- 超时由 `MODEL_HTTP_TIMEOUT_SECONDS` 控制（默认 30 秒/次），不跟随重定向。
- 重试：最多 3 次。网络错误/超时、HTTP 429、HTTP 5xx 会重试，退避 0.25s → 0.5s；401/403 立即失败；其他 4xx 立即失败。

错误映射：

| 情况 | 返回给调用方 |
|---|---|
| 401 / 403 | 502 `MODEL_AUTH_FAILED` |
| 429 / 5xx 且 3 次都失败 | 503 `MODEL_RATE_LIMITED` |
| 连接失败 / 超时 3 次 | 503 `MODEL_NETWORK_ERROR` |
| 其他 4xx | 502 `MODEL_ERROR` |
| 响应不是 JSON、条数不符、维度不符、含 NaN/0 向量 | 502 `EMBEDDING_INVALID` |
| 配置不完整 | 503 `MODEL_UNAVAILABLE` |

就绪探针会真实请求一次 `/embeddings`（只发 1 条文本），并校验返回维度。**这里校验的是写死的 1024**（`remote_checks.EMBEDDING_DIMENSION`），不读 `BASE_EMBEDDING_DIM`；结果是缓存 30 秒。

### 9.2 维度校验与已有数据

- **每次响应**都会校验维度，不是只在启动时校验。维度不等于 `BASE_EMBEDDING_DIM` 会直接 502，不会写入数据库。
- 存储维度是固定的：Wiki 知识块 `wiki_chunk.embedding` 为 `vector(1024)`；事件向量 `event_memory.embedding` 为 `vector(256)`（LSTM 输出，不是基础向量）。
- `BASE_EMBEDDING_DIM` 默认 1024。**当前实现实际只支持 1024 维**：运行期维度校验读配置，但就绪探针的维度常量写死为 1024，改成 768/512 后 `/health/ready` 会一直报维度不符。确实要换维度时，仅改环境变量不够：
  1. 改代码里的 `EMBEDDING_DIMENSION` 与相关默认值；
  2. 事件模型包的 `base_embedding_dim` 必须与之一致，否则加载时报 `MODEL_UNAVAILABLE`；
  3. 事件模型需要重训；
  4. Wiki 的向量列是 1024 维，需要迁移表结构；
  5. 已有 Wiki 块要重嵌入，已有事件向量也要重算。
- **换供应商后已有数据不会自动重建**。检索侧没有按模型版本过滤：`_recall_events` / `_recall_wiki` 只要求 `embedding is not null`，然后直接算 cosine。不同模型产生的向量落在不同语义空间，混在一起排序结果不可信。
- 可用的重建手段：
  - Wiki：`POST /api/v1/wiki/sync-tasks`，`mode=reembed`，按当前模型批量重算知识块向量。
  - 事件：**没有批量重编码接口**。`entities/{id}/rebuild` 只重算实体状态，不重算事件向量。换 Embedding 后历史事件需要重新写入/纠偏，或另写脚本按 `event_pipeline` 的逻辑重算。
- 因此：**同维度、同模型语义的小版本升级**风险较低；真正换供应商/换模型时，按“新建库 + 重新灌数据”处理最稳。

### 9.3 远程 Reranker：`POST /rerank`

```http
POST {RERANKER_API_BASE}/rerank
Authorization: Bearer {RERANKER_API_KEY}
Content-Type: application/json
```

```json
{
  "model": "BAAI/bge-reranker-v2-m3",
  "query": "上次会议决定了什么",
  "documents": ["候选文档 1", "候选文档 2"]
}
```

响应接受三种形状：

```json
{"results": [{"index": 0, "relevance_score": 0.92}, {"index": 1, "relevance_score": 0.31}]}
{"scores": [0.92, 0.31]}
[0.92, 0.31]
```

- 分数数量必须与 `documents` 数量一致；带 `results` 时按 `index` 排序，取 `relevance_score`，没有则取 `score`。
- 分数都在 `[0,1]` 时按概率使用；出现越界值则视为 logit，统一做一次 sigmoid 归一化。
- `model` 字段可省略；返回时必须与 `RERANKER_MODEL_NAME` 一致。
- 鉴权、Base URL 校验、超时与重试行为与 Embedding 完全一致；错误码为 `RERANK_INVALID` / `MODEL_AUTH_FAILED` / `MODEL_RATE_LIMITED` / `MODEL_NETWORK_ERROR` / `MODEL_ERROR`。
- 就绪探针会发 2 条文档并校验返回 2 个分数。

`RERANKER_ENABLED=false` 时：

- `ModelBundle.reranker = None`，检索直接跳过精排，**不会报错**；
- 排序只用向量相似度 + 重要度/熟悉度/意图权重融合；
- 搜索结果的 `score_breakdown` 里不会出现 `rerank_score`，前端需要容忍该字段缺失；
- `RERANKER_ENABLED=false` 时预检不再要求 Reranker 相关变量；`RERANKER_PROVIDER`、`RERANKER_MODEL_NAME` 等即使保留也不会被使用。

### 9.4 不能远程替代的部分

- **事件模型必须是本地模型包**。`EVENT_MODEL_URI` 只支持本地目录，加载逻辑会读 `manifest.json`、校验版本/维度/SHA-256、用 `safetensors` 加载权重。远程 Embedding 只替换基础编码器，不提供事件编码能力。
- 查询编码器（`QueryAdapter`）复用同一个本地事件模型；没有远程“query encode”接口。
- 实体状态、序列号、衰减与熟悉度计算都在本地完成，不依赖外部服务。
- 没有 OpenAI 之外的厂商专用适配层（如 Bedrock、Vertex、DashScope 原生 SDK）；只要对方提供上述 HTTP 形态即可，否则需要自己加适配代码。

### 9.5 换供应商的最小改动清单

只换 Embedding（维度仍为 1024）：

```dotenv
EMBEDDING_PROVIDER=remote
EMBEDDING_MODEL_NAME=<服务端模型名>
EMBEDDING_MODEL_VERSION=<新的版本标识，建议带供应商前缀>
EMBEDDING_API_BASE=https://<供应商域名>/v1
EMBEDDING_API_KEY=<密钥>
```

只换 Reranker：

```dotenv
RERANKER_ENABLED=true
RERANKER_PROVIDER=remote
RERANKER_MODEL_NAME=<服务端模型名>
RERANKER_MODEL_VERSION=<新的版本标识>
RERANKER_API_BASE=https://<供应商域名>
RERANKER_API_KEY=<密钥>
```

换维度（例如 1024 → 768）：**当前不能只靠环境变量完成**。除设置 `BASE_EMBEDDING_DIM=768` 外，还要改 `remote_checks.EMBEDDING_DIMENSION` 常量、重训事件模型包并改 `EVENT_MODEL_VERSION`、迁移 `wiki_chunk.embedding` 列、重嵌入 Wiki 与历史事件。只改环境变量会让 `/health/ready` 持续报维度不符，或写库失败。

改完后要重启 `api` 与 `worker`（`beat` 不需要模型配置），再按第 6 节重新验证 `/health/ready`、写事件与搜索。模型 Bundle 有进程内缓存，不重启不会生效。
