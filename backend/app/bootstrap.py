from __future__ import annotations

import getpass
import hashlib
import json
import os
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import SecretStr

from app.core.config import PROJECT_ROOT, Settings
from app.core.hardware import (
    HardwareReport,
    ProviderRecommendation,
    detect_hardware,
    recommend_provider,
)
from app.core.logging import redact_secrets
from app.services.remote_checks import RemoteCheckError, RemoteModelChecker

SECRET_ENV_NAMES = {"EMBEDDING_API_KEY", "RERANKER_API_KEY"}
MANAGED_ENV_NAMES = {
    "EMBEDDING_PROVIDER",
    "EMBEDDING_MODEL_NAME",
    "EMBEDDING_MODEL_VERSION",
    "EMBEDDING_API_BASE",
    "RERANKER_ENABLED",
    "RERANKER_PROVIDER",
    "RERANKER_MODEL_NAME",
    "RERANKER_MODEL_VERSION",
    "RERANKER_API_BASE",
    "EVENT_MODEL_URI",
    "EVENT_MODEL_VERSION",
    "MODEL_DEVICE",
    "ALLOW_CPU_MODELS",
}


class BootstrapError(RuntimeError):
    pass


def configuration_fingerprint(settings: Settings) -> tuple[str, dict[str, Any]]:
    safe = {
        "embedding_provider": settings.embedding_provider,
        "embedding_model_name": settings.embedding_model_name,
        "embedding_model_version": settings.embedding_model_version,
        "embedding_api_base": settings.embedding_api_base,
        "reranker_enabled": settings.reranker_enabled,
        "reranker_provider": settings.reranker_provider,
        "reranker_model_name": settings.reranker_model_name,
        "reranker_model_version": settings.reranker_model_version,
        "reranker_api_base": settings.reranker_api_base,
        "event_model_uri": settings.event_model_uri,
        "event_model_version": settings.event_model_version,
        "model_device": settings.model_device,
        "allow_cpu_models": settings.allow_cpu_models,
    }
    safe = redact_secrets(safe)
    encoded = json.dumps(safe, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest(), safe


def load_marker(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    required = {"completed_at", "providers", "hardware_fingerprint", "config_hash"}
    return value if isinstance(value, dict) and required <= set(value) else None


def write_marker(path: Path, settings: Settings, hardware: HardwareReport) -> None:
    config_hash, safe = configuration_fingerprint(settings)
    value = {
        "completed_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "providers": {
            "embedding": settings.embedding_provider,
            "reranker": settings.reranker_provider if settings.reranker_enabled else None,
        },
        "hardware_fingerprint": hardware.fingerprint,
        "config_hash": config_hash,
        "configuration": safe,
    }
    serialized = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)
    for secret_name in SECRET_ENV_NAMES:
        secret = os.getenv(secret_name)
        if secret:
            serialized = serialized.replace(secret, "[REDACTED]")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(serialized + "\n", encoding="utf-8")
    temporary.replace(path)


def write_nonsecret_env(path: Path, settings: Settings) -> None:
    _, safe = configuration_fingerprint(settings)
    existing: list[str] = []
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            name = line.split("=", 1)[0].strip() if "=" in line else ""
            if name not in MANAGED_ENV_NAMES | SECRET_ENV_NAMES:
                existing.append(line)
    values = {
        "EMBEDDING_PROVIDER": settings.embedding_provider,
        "EMBEDDING_MODEL_NAME": settings.embedding_model_name,
        "EMBEDDING_MODEL_VERSION": settings.embedding_model_version,
        "EMBEDDING_API_BASE": safe["embedding_api_base"],
        "RERANKER_ENABLED": str(settings.reranker_enabled).lower(),
        "RERANKER_PROVIDER": settings.reranker_provider,
        "RERANKER_MODEL_NAME": settings.reranker_model_name,
        "RERANKER_MODEL_VERSION": settings.reranker_model_version,
        "RERANKER_API_BASE": safe["reranker_api_base"],
        "EVENT_MODEL_URI": settings.event_model_uri,
        "EVENT_MODEL_VERSION": settings.event_model_version,
        "MODEL_DEVICE": settings.model_device,
        "ALLOW_CPU_MODELS": str(settings.allow_cpu_models).lower(),
    }
    lines = existing + [f"{name}={value}" for name, value in values.items() if value is not None]
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text("\n".join(lines) + "\n", encoding="utf-8")
    temporary.replace(path)


def run_bootstrap(
    *,
    settings: Settings | None = None,
    project_root: Path = PROJECT_ROOT,
    input_fn: Callable[[str], str] = input,
    getpass_fn: Callable[[str], str] = getpass.getpass,
    hardware_detector: Callable[[], HardwareReport] = detect_hardware,
    checker_factory: Callable[[Settings], RemoteModelChecker] = RemoteModelChecker,
    output: Callable[[str], None] = print,
    local_inference_check: Callable[[Settings], None] | None = None,
) -> int:
    settings = settings or Settings()
    marker_path = project_root / ".memory-system" / "bootstrap.json"
    env_path = project_root / ".env.local"
    try:
        hardware = hardware_detector()
        recommendation = recommend_provider(hardware, settings.local_model_min_vram_gb)
        output(json.dumps({"hardware": hardware.safe_dict()}, ensure_ascii=False, default=str))
        for warning in hardware.warnings:
            output(f"WARNING: {warning}")
        output(f"Provider 建议：{recommendation.reason}")

        non_interactive = settings.non_interactive or bool(os.getenv("CI"))
        current_hash, _ = configuration_fingerprint(settings)
        marker = load_marker(marker_path)
        marker_current = bool(
            marker
            and marker.get("hardware_fingerprint") == hardware.fingerprint
            and marker.get("config_hash") == current_hash
        )
        needs_wizard = not marker_current or bool(settings.missing_configuration())
        if non_interactive:
            missing = settings.missing_configuration()
            if missing:
                _print_missing(missing, output)
                return 2
        elif needs_wizard:
            if not _run_wizard(
                settings,
                hardware,
                recommendation,
                input_fn,
                getpass_fn,
                output,
            ):
                output("未完成模型配置，启动取消。")
                return 2
            write_nonsecret_env(env_path, settings)

        missing = settings.missing_configuration()
        if missing:
            _print_missing(missing, output)
            return 2
        _validate_local_files(settings)
        if (
            settings.embedding_provider == "local"
            and hardware.cuda_available
            and hardware.max_vram_gb < settings.local_model_min_vram_gb
        ):
            if local_inference_check is None:
                raise BootstrapError(
                    "低显存本地模型必须通过实际推理检查；当前未配置检查器，请改用远程服务"
                )
            local_inference_check(settings)
        checker = checker_factory(settings)
        checks = checker.check_all()
        if checks:
            output(json.dumps({"model_checks": checks}, ensure_ascii=False, default=str))
        if settings.model_device == "cpu" and settings.embedding_provider == "local":
            output("WARNING: 本地 CPU BGE/Reranker 仅限开发/测试，可能存在较高延迟")
        write_marker(marker_path, settings, hardware)
        output(f"预检完成：{marker_path}")
        return 0
    except (BootstrapError, RemoteCheckError) as exc:
        output(f"预检失败：{exc}")
        return 3
    except (EOFError, KeyboardInterrupt):
        output("预检已取消。")
        return 2


def _run_wizard(
    settings: Settings,
    hardware: HardwareReport,
    recommendation: ProviderRecommendation,
    input_fn: Callable[[str], str],
    getpass_fn: Callable[[str], str],
    output: Callable[[str], None],
) -> bool:
    if not hardware.cuda_available:
        output(
            "未检测到可用 GPU。\n本地加载 BGE Embedding 和 Reranker 可能产生较高延迟，"
            "建议配置远程模型服务。"
        )
        output(
            "[1] 配置远程 Embedding + Rerank（推荐）\n"
            "[2] 使用本地 CPU 模型（仅开发/测试）\n[3] 暂不配置，退出启动"
        )
        choice = input_fn("请选择 [1/2/3]: ").strip()
        if choice == "1":
            _configure_remote(settings, input_fn, getpass_fn)
        elif choice == "2":
            _configure_local_cpu(settings, input_fn)
        else:
            return False
    elif recommendation.requires_explicit_local_confirmation:
        output("检测到 GPU，但显存不足。建议选择远程 Embedding + Reranker。")
        output("[1] 配置远程服务（推荐）\n[2] 仍使用本地模型（需实际推理检查）\n[3] 退出")
        choice = input_fn("请选择 [1/2/3]: ").strip()
        if choice == "1":
            _configure_remote(settings, input_fn, getpass_fn)
        elif choice == "2":
            confirmation = input_fn("确认在低显存 GPU 上加载本地模型？输入 YES 继续: ")
            if confirmation != "YES":
                return False
            settings.embedding_provider = "local"
            settings.reranker_provider = "local"
            settings.model_device = "cuda"
        else:
            return False
    else:
        output("[1] 使用本地 GPU 模型（建议）\n[2] 配置远程服务\n[3] 保持当前 Provider\n[4] 退出")
        choice = input_fn("请选择 [1/2/3/4]: ").strip()
        if choice == "1":
            settings.embedding_provider = "local"
            settings.reranker_provider = "local"
            settings.model_device = "cuda"
        elif choice == "2":
            _configure_remote(settings, input_fn, getpass_fn)
        elif choice != "3":
            return False
    _prompt_event_model(settings, input_fn)
    return True


def _configure_remote(
    settings: Settings,
    input_fn: Callable[[str], str],
    getpass_fn: Callable[[str], str],
) -> None:
    settings.embedding_provider = "remote"
    settings.embedding_api_base = _prompt(
        input_fn, "Embedding Base URL", settings.embedding_api_base
    )
    settings.embedding_model_name = _prompt(
        input_fn, "Embedding Model Name", settings.embedding_model_name
    )
    settings.embedding_model_version = _prompt(
        input_fn, "Embedding Model Version", settings.embedding_model_version
    )
    settings.embedding_api_key = SecretStr(
        _prompt_secret(getpass_fn, "Embedding API Key", settings.embedding_api_key)
    )
    if settings.reranker_enabled:
        settings.reranker_provider = "remote"
        settings.reranker_api_base = _prompt(
            input_fn, "Reranker Base URL", settings.reranker_api_base
        )
        settings.reranker_model_name = _prompt(
            input_fn, "Reranker Model Name", settings.reranker_model_name
        )
        settings.reranker_api_key = SecretStr(
            _prompt_secret(getpass_fn, "Reranker API Key", settings.reranker_api_key)
        )


def _configure_local_cpu(settings: Settings, input_fn: Callable[[str], str]) -> None:
    settings.embedding_provider = "local"
    settings.reranker_provider = "local" if settings.reranker_enabled else None
    settings.model_device = "cpu"
    settings.allow_cpu_models = True
    settings.embedding_model_name = _prompt(
        input_fn, "本地 Embedding 模型目录", settings.embedding_model_name
    )
    settings.embedding_model_version = _prompt(
        input_fn, "Embedding Model Version", settings.embedding_model_version
    )
    if settings.reranker_enabled:
        settings.reranker_model_name = _prompt(
            input_fn, "本地 Reranker 模型目录", settings.reranker_model_name
        )


def _prompt_event_model(settings: Settings, input_fn: Callable[[str], str]) -> None:
    settings.event_model_uri = _prompt(input_fn, "LSTM 模型包 URI", settings.event_model_uri)
    settings.event_model_version = _prompt(
        input_fn, "LSTM Model Version", settings.event_model_version
    )


def _prompt(input_fn: Callable[[str], str], label: str, current: str | None) -> str:
    suffix = f" [{current}]" if current else ""
    value = input_fn(f"{label}{suffix}: ").strip()
    if not value and current:
        return current
    if not value:
        raise BootstrapError(f"{label} 不能为空")
    return value


def _prompt_secret(getpass_fn: Callable[[str], str], label: str, current: SecretStr | None) -> str:
    value = getpass_fn(f"{label}（输入不回显，留空保留当前环境值）: ")
    if value:
        return value
    if current and current.get_secret_value():
        return current.get_secret_value()
    raise BootstrapError(f"{label} 不能为空；请通过环境变量或 Secret Manager 提供")


def _validate_local_files(settings: Settings) -> None:
    if settings.embedding_provider == "local":
        _require_path(settings.embedding_model_name, "EMBEDDING_MODEL_NAME")
        if settings.reranker_enabled:
            _require_path(settings.reranker_model_name, "RERANKER_MODEL_NAME")
    event_uri = settings.event_model_uri or ""
    if "://" not in event_uri:
        _require_path(event_uri, "EVENT_MODEL_URI")


def _require_path(value: str | None, variable: str) -> None:
    if not value or not Path(value).exists():
        raise BootstrapError(f"{variable} 指向的本地模型路径不存在；M0 不会自动下载或安装模型")


def _print_missing(missing: list[str], output: Callable[[str], None]) -> None:
    output("配置预检失败，请设置以下环境变量或修复配置：")
    for name in missing:
        output(f" - {name}")
    output("示例：EMBEDDING_PROVIDER=remote；远程密钥仅通过环境变量或 Secret Manager 提供。")


def main() -> None:
    raise SystemExit(run_bootstrap())


if __name__ == "__main__":
    main()
