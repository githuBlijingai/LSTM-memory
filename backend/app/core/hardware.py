from __future__ import annotations

import hashlib
import importlib
import json
import os
import platform
import subprocess
import sys
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class GPUInfo:
    name: str
    total_vram_gb: float
    driver: str | None = None


@dataclass(frozen=True)
class HardwareReport:
    operating_system: str
    python_version: str
    torch_version: str | None
    cuda_version: str | None
    cuda_available: bool
    gpus: tuple[GPUInfo, ...]
    in_container: bool
    host_gpu_detected: bool
    warnings: tuple[str, ...] = field(default_factory=tuple)

    @property
    def gpu_count(self) -> int:
        return len(self.gpus)

    @property
    def max_vram_gb(self) -> float:
        return max((gpu.total_vram_gb for gpu in self.gpus), default=0.0)

    @property
    def recommended_device(self) -> str:
        return "cuda" if self.cuda_available and self.gpus else "cpu"

    @property
    def fingerprint(self) -> str:
        value = {
            "gpus": [asdict(gpu) for gpu in self.gpus],
            "in_container": self.in_container,
            "cuda_version": self.cuda_version,
        }
        return hashlib.sha256(
            json.dumps(value, sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest()

    def safe_dict(self) -> dict[str, Any]:
        return {
            "operating_system": self.operating_system,
            "python_version": self.python_version,
            "torch_version": self.torch_version,
            "cuda_version": self.cuda_version,
            "cuda_available": self.cuda_available,
            "gpu_count": self.gpu_count,
            "gpus": [asdict(gpu) for gpu in self.gpus],
            "in_container": self.in_container,
            "host_gpu_detected": self.host_gpu_detected,
            "recommended_device": self.recommended_device,
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True)
class ProviderRecommendation:
    embedding_provider: str
    reranker_provider: str
    model_device: str
    reason: str
    requires_explicit_local_confirmation: bool = False


def recommend_provider(report: HardwareReport, min_vram_gb: float = 8.0) -> ProviderRecommendation:
    if report.cuda_available and report.gpus and report.max_vram_gb >= min_vram_gb:
        return ProviderRecommendation("local", "local", "cuda", "GPU 显存满足本地模型建议")
    if report.cuda_available and report.gpus:
        return ProviderRecommendation(
            "remote",
            "remote",
            "cuda",
            f"GPU 显存低于 {min_vram_gb:g} GiB，建议远程 Embedding + Reranker",
            requires_explicit_local_confirmation=True,
        )
    return ProviderRecommendation(
        "remote", "remote", "cpu", "未检测到可用 GPU，建议远程 Embedding + Reranker"
    )


_UNSET = object()


def detect_hardware(
    *,
    torch_module: Any = _UNSET,
    smi_runner: Callable[[], list[GPUInfo]] | None = None,
    in_container: bool | None = None,
) -> HardwareReport:
    warnings: list[str] = []
    torch = torch_module
    if torch is _UNSET:
        try:
            torch = importlib.import_module("torch")
        except (ImportError, OSError) as exc:
            torch = None
            warnings.append(f"PyTorch 可选导入失败：{type(exc).__name__}")
    smi_gpus = (smi_runner or query_nvidia_smi)()
    container = in_container if in_container is not None else _running_in_container()
    gpus: list[GPUInfo] = []
    cuda_available = False
    torch_version = None
    cuda_version = None
    if torch is not None:
        torch_version = str(getattr(torch, "__version__", "unknown"))
        cuda_version = str(getattr(getattr(torch, "version", None), "cuda", None) or "") or None
        try:
            cuda = torch.cuda
            cuda_available = bool(cuda.is_available())
            if cuda_available:
                for index in range(int(cuda.device_count())):
                    properties = cuda.get_device_properties(index)
                    driver = smi_gpus[index].driver if index < len(smi_gpus) else None
                    gpus.append(
                        GPUInfo(
                            name=str(properties.name),
                            total_vram_gb=round(float(properties.total_memory) / 1024**3, 2),
                            driver=driver,
                        )
                    )
        except (AttributeError, RuntimeError, OSError) as exc:
            warnings.append(f"CUDA 检测失败：{type(exc).__name__}")
            cuda_available = False
            gpus = []
    if container and smi_gpus and not cuda_available:
        warnings.append(
            "宿主机 nvidia-smi 可见 GPU，但容器内 CUDA 不可用；请检查 NVIDIA Container Toolkit、"
            "容器 GPU 参数及 CUDA 版 PyTorch"
        )
    if not cuda_available:
        warnings.append(
            "未检测到可用 GPU；LSTM 事件模型可使用 CPU，本地 BGE/Reranker 需显式开发选项"
        )
    return HardwareReport(
        operating_system=platform.platform(),
        python_version=sys.version.split()[0],
        torch_version=torch_version,
        cuda_version=cuda_version,
        cuda_available=cuda_available,
        gpus=tuple(gpus),
        in_container=container,
        host_gpu_detected=bool(smi_gpus),
        warnings=tuple(warnings),
    )


def query_nvidia_smi() -> list[GPUInfo]:
    try:
        result = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=name,memory.total,driver_version",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if result.returncode != 0:
        return []
    gpus = []
    for line in result.stdout.splitlines():
        fields = [item.strip() for item in line.split(",")]
        if len(fields) != 3:
            continue
        try:
            gpus.append(GPUInfo(fields[0], round(float(fields[1]) / 1024, 2), fields[2]))
        except ValueError:
            continue
    return gpus


def _running_in_container() -> bool:
    return Path("/.dockerenv").exists() or os.getenv("KUBERNETES_SERVICE_HOST") is not None
