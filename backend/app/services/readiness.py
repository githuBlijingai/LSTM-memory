from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import httpx
import structlog

from app.core.config import Settings

logger = structlog.get_logger(__name__)

_CACHE_LOCK = threading.Lock()
_CACHE: dict[tuple, tuple[float, ModelCheckResult]] = {}


def reset_readiness_cache() -> None:
    with _CACHE_LOCK:
        _CACHE.clear()


@dataclass(frozen=True)
class ModelCheckResult:
    ready: bool
    checks: dict[str, bool]
    error: str | None = None


def _cache_key(settings: Settings) -> tuple:
    return (
        settings.event_model_uri,
        settings.event_model_version,
        settings.embedding_provider,
        settings.embedding_model_name,
        settings.embedding_api_base,
        settings.reranker_enabled,
        settings.reranker_provider,
        settings.reranker_model_name,
        settings.reranker_api_base,
        settings.readiness_model_cache_seconds,
        settings.readiness_model_timeout_seconds,
    )


def _local_event_model_ready(settings: Settings) -> bool:
    uri = settings.event_model_uri
    version = settings.event_model_version
    if not uri or not version:
        return False
    root = Path(uri)
    if not root.is_dir():
        return False
    manifest_path = root / "manifest.json"
    if not manifest_path.is_file():
        return False
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(manifest, dict) or manifest.get("model_version") != version:
        return False
    weights_name = manifest.get("weights_file", "weights.safetensors")
    if not isinstance(weights_name, str) or not weights_name:
        return False
    weights = (root / weights_name).resolve()
    try:
        weights.relative_to(root.resolve())
    except ValueError:
        return False
    return weights.is_file() and weights.stat().st_size > 0


def _remote_reachable(api_base: str | None, timeout: float) -> bool:
    if not api_base:
        return False
    try:
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            client.get(api_base)
    except httpx.HTTPError:
        return False
    return True


def _provider_ready(settings: Settings, prefix: str, timeout: float) -> bool:
    provider = getattr(settings, f"{prefix}_provider")
    model_name = getattr(settings, f"{prefix}_model_name")
    if not provider or not model_name:
        return False
    if provider == "remote":
        return _remote_reachable(getattr(settings, f"{prefix}_api_base"), timeout)
    path = Path(model_name)
    if path.exists():
        return path.is_dir() or path.is_file()
    # 本地模型名可能是 HuggingFace 仓库名，配置完整即视为可加载。
    return True


class ModelReadinessChecker:
    """模型可用性检查；结果按 TTL 缓存，避免探针高频执行重活。"""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def check(self) -> ModelCheckResult:
        if not self.settings.readiness_model_check_enabled:
            return ModelCheckResult(ready=True, checks={})

        key = _cache_key(self.settings)
        now = time.monotonic()
        with _CACHE_LOCK:
            cached = _CACHE.get(key)
            if cached and cached[0] > now:
                return cached[1]

        result = self._run()
        with _CACHE_LOCK:
            _CACHE[key] = (
                now + self.settings.readiness_model_cache_seconds,
                result,
            )
        return result

    def _run(self) -> ModelCheckResult:
        timeout = self.settings.readiness_model_timeout_seconds
        try:
            checks = {
                "event_model": _local_event_model_ready(self.settings),
                "embedding": _provider_ready(self.settings, "embedding", timeout),
                "reranker": (
                    _provider_ready(self.settings, "reranker", timeout)
                    if self.settings.reranker_enabled
                    else True
                ),
            }
        except Exception as exc:  # noqa: BLE001 - 探针不能抛出未处理异常
            logger.warning("model_readiness_check_failed", error_type=type(exc).__name__)
            return ModelCheckResult(ready=False, checks={}, error="模型检查异常")
        return ModelCheckResult(ready=all(checks.values()), checks=checks)
