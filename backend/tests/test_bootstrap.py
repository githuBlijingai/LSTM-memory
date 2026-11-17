from __future__ import annotations

import json
from pathlib import Path

from pydantic import SecretStr

from app.bootstrap import run_bootstrap
from app.core.config import Settings
from app.core.hardware import HardwareReport
from app.services.remote_checks import RemoteCheckError


def no_gpu() -> HardwareReport:
    return HardwareReport(
        operating_system="test",
        python_version="3.12.0",
        torch_version=None,
        cuda_version=None,
        cuda_available=False,
        gpus=(),
        in_container=False,
        host_gpu_detected=False,
    )


def complete_settings(non_interactive: bool = True) -> Settings:
    return Settings(
        _env_file=None,
        bootstrap_non_interactive=non_interactive,
        database_url="postgresql+psycopg://memory:memory@localhost/memory",
        redis_url="redis://localhost:6379/0",
        embedding_provider="remote",
        embedding_model_name="bge-m3",
        embedding_model_version="v1",
        embedding_api_base="https://models.example.test/v1",
        embedding_api_key=SecretStr("test-only-secret"),
        reranker_enabled=False,
        event_model_uri="s3://models/event",
        event_model_version="v1",
    )


def test_non_interactive_missing_configuration_fails_clearly(tmp_path: Path) -> None:
    output: list[str] = []

    result = run_bootstrap(
        settings=Settings(_env_file=None, bootstrap_non_interactive=True),
        project_root=tmp_path,
        hardware_detector=no_gpu,
        output=output.append,
    )

    assert result != 0
    assert not (tmp_path / ".memory-system" / "bootstrap.json").exists()
    text = "\n".join(output)
    assert "DATABASE_URL" in text
    assert "EMBEDDING_PROVIDER" in text
    assert "建议" in text


def test_non_interactive_valid_configuration_writes_only_safe_marker(tmp_path: Path) -> None:
    class Checker:
        def __init__(self, settings: Settings) -> None:
            self.settings = settings

        def check_all(self) -> dict:
            return {"embedding": {"dimension": 1024}}

    settings = complete_settings()
    output: list[str] = []

    result = run_bootstrap(
        settings=settings,
        project_root=tmp_path,
        hardware_detector=no_gpu,
        checker_factory=Checker,
        output=output.append,
    )

    marker_path = tmp_path / ".memory-system" / "bootstrap.json"
    assert result == 0
    marker_text = marker_path.read_text(encoding="utf-8")
    marker = json.loads(marker_text)
    assert marker["providers"] == {"embedding": "remote", "reranker": None}
    assert marker["configuration"]["embedding_model_name"] == "bge-m3"
    assert "embedding_api_key" not in marker_text
    assert "reranker_api_key" not in marker_text


def test_remote_precheck_failure_is_nonzero_and_does_not_write_marker(tmp_path: Path) -> None:
    class Checker:
        def __init__(self, settings: Settings) -> None:
            pass

        def check_all(self) -> dict:
            raise RemoteCheckError("NETWORK_ERROR", "连接失败")

    output: list[str] = []
    result = run_bootstrap(
        settings=complete_settings(),
        project_root=tmp_path,
        hardware_detector=no_gpu,
        checker_factory=Checker,
        output=output.append,
    )

    assert result == 3
    assert "连接失败" in output[-1]
    assert not (tmp_path / ".memory-system" / "bootstrap.json").exists()
