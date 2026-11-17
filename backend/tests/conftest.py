from __future__ import annotations

import pytest

from app.core.config import Settings


@pytest.fixture(autouse=True)
def isolate_settings_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in Settings.model_fields:
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(name.upper(), raising=False)
    monkeypatch.delenv("CI", raising=False)


@pytest.fixture(autouse=True)
def reset_runtime_caches() -> None:
    from app.core.rate_limit import reset_rate_limits
    from app.services.readiness import reset_readiness_cache

    reset_rate_limits()
    reset_readiness_cache()
    yield
    reset_rate_limits()
    reset_readiness_cache()
