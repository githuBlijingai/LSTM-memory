FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY pyproject.toml /app/pyproject.toml
COPY backend /app/backend
COPY infra/pip.ini /etc/pip.conf

# 镜像分两档：
# - 基础档（默认，INSTALL_ML=false）：只装基础依赖，镜像精简；事件编码会报 MODEL_UNAVAILABLE。
# - ml 档（INSTALL_ML=true）：额外装事件编码依赖（torch/safetensors/transformers）。
#   torch 固定安装 CPU-only wheel，不引入 CUDA 运行时；全部依赖在构建期落盘，启动时不下载，可离线运行。
ARG INSTALL_ML=false
ARG TORCH_CPU_INDEX_URL=https://mirror.sjtu.edu.cn/pytorch-wheels/cpu
ARG TORCH_CPU_VERSION=2.6.0+cpu

RUN pip install . \
    && if [ "$INSTALL_ML" = "true" ]; then \
        pip install --no-deps --index-url "$TORCH_CPU_INDEX_URL" "torch==${TORCH_CPU_VERSION}" \
        && pip install ".[ml]"; \
    fi \
    && useradd --create-home --uid 10001 memory \
    && mkdir -p /app/.memory-system /app/state \
    && chown -R memory:memory /app/.memory-system /app/state

USER memory
WORKDIR /app/backend
EXPOSE 8000

CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
