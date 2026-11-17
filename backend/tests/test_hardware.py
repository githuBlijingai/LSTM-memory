from __future__ import annotations

from types import SimpleNamespace

from app.core.hardware import (
    GPUInfo,
    HardwareReport,
    detect_hardware,
    recommend_provider,
)


def report(*, cuda: bool, vram: float = 0.0, container: bool = False) -> HardwareReport:
    gpus = (GPUInfo("Test GPU", vram, "driver"),) if cuda else ()
    return HardwareReport(
        operating_system="test",
        python_version="3.12.0",
        torch_version="2.0.0" if cuda else None,
        cuda_version="12.4" if cuda else None,
        cuda_available=cuda,
        gpus=gpus,
        in_container=container,
        host_gpu_detected=cuda,
    )


def test_provider_recommendation_covers_no_gpu_high_and_low_vram() -> None:
    no_gpu = recommend_provider(report(cuda=False))
    high = recommend_provider(report(cuda=True, vram=16))
    low = recommend_provider(report(cuda=True, vram=4))

    assert no_gpu.embedding_provider == no_gpu.reranker_provider == "remote"
    assert no_gpu.model_device == "cpu"
    assert high.embedding_provider == high.reranker_provider == "local"
    assert high.model_device == "cuda"
    assert low.embedding_provider == "remote"
    assert low.requires_explicit_local_confirmation


def test_detect_hardware_handles_missing_torch_and_container_host_gpu() -> None:
    result = detect_hardware(
        torch_module=None,
        smi_runner=lambda: [GPUInfo("Host GPU", 24, "driver")],
        in_container=True,
    )

    assert not result.cuda_available
    assert result.host_gpu_detected
    assert result.recommended_device == "cpu"
    assert any("容器内 CUDA 不可用" in warning for warning in result.warnings)
    assert result.safe_dict()["gpu_count"] == 0
    assert len(result.fingerprint) == 64


def test_detect_hardware_uses_optional_cuda_torch_api() -> None:
    properties = SimpleNamespace(name="CUDA GPU", total_memory=12 * 1024**3)
    torch = SimpleNamespace(
        __version__="2.6.0",
        version=SimpleNamespace(cuda="12.4"),
        cuda=SimpleNamespace(
            is_available=lambda: True,
            device_count=lambda: 1,
            get_device_properties=lambda index: properties,
        ),
    )

    result = detect_hardware(torch_module=torch, smi_runner=list, in_container=False)

    assert result.cuda_available
    assert result.max_vram_gb == 12
    assert result.gpus[0].name == "CUDA GPU"


def test_detect_hardware_degrades_when_cuda_probe_fails() -> None:
    torch = SimpleNamespace(
        __version__="2.6.0",
        version=SimpleNamespace(cuda="12.4"),
        cuda=SimpleNamespace(is_available=lambda: (_ for _ in ()).throw(RuntimeError("failure"))),
    )

    result = detect_hardware(torch_module=torch, smi_runner=list, in_container=False)

    assert not result.cuda_available
    assert any("CUDA 检测失败：RuntimeError" in warning for warning in result.warnings)
