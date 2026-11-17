from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import structlog

from app.core.config import Settings
from app.core.errors import AppError
from app.core.logging import sanitize_url
from app.ml.embedding import EmbeddingProvider, LocalBGEProvider, RemoteEmbeddingProvider
from app.ml.event_encoder import LSTMEventEncoder
from app.ml.query_encoder import QueryAdapter
from app.ml.reranker import LocalReranker, RemoteReranker, Reranker

logger = structlog.get_logger(__name__)

MANIFEST_FILE = "manifest.json"
CONFIG_FILE = "config.json"
DEFAULT_WEIGHTS_FILE = "weights.safetensors"
REQUIRED_MANIFEST_KEYS = (
    "model_version",
    "base_embedding_dim",
    "input_dim",
    "hidden_dim",
    "num_layers",
    "weights_sha256",
)


@dataclass(frozen=True)
class ModelPackage:
    uri: str
    model_version: str
    base_embedding_dim: int
    input_dim: int
    hidden_dim: int
    num_layers: int
    weights_path: Path
    weights_sha256: str
    manifest: dict[str, Any]
    config: dict[str, Any]


@dataclass(frozen=True)
class ModelBundle:
    embedding: EmbeddingProvider
    reranker: Reranker | None
    event_encoder: LSTMEventEncoder
    query_encoder: QueryAdapter
    embedding_model_version: str
    event_model_version: str
    package: ModelPackage


def _unavailable(message: str) -> AppError:
    return AppError("MODEL_UNAVAILABLE", message, 503)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise _unavailable(f"模型包文件无法读取：{path.name}") from exc
    if not isinstance(value, dict):
        raise _unavailable(f"模型包文件格式无效：{path.name}")
    return value


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise _unavailable("模型权重文件无法读取") from exc
    return digest.hexdigest()


def _validate_dimension(manifest: dict[str, Any], key: str, expected: int, actual: object) -> None:
    if isinstance(actual, bool) or not isinstance(actual, int) or actual != expected:
        raise _unavailable(f"模型包 {key} 与配置不一致")


def load_model_package(
    uri: str,
    *,
    expected_version: str,
    base_embedding_dim: int,
    input_dim: int,
    hidden_dim: int,
    num_layers: int,
) -> ModelPackage:
    """加载并校验模型包目录，任何缺失或不一致都抛 MODEL_UNAVAILABLE。"""
    if not uri or not expected_version:
        raise _unavailable("事件模型 URI 或版本未配置")
    root = Path(uri)
    if not root.is_dir():
        raise _unavailable("事件模型目录不存在")
    manifest_path = root / MANIFEST_FILE
    if not manifest_path.is_file():
        raise _unavailable("模型包缺少 manifest.json")
    manifest = _read_json(manifest_path)
    missing = [key for key in REQUIRED_MANIFEST_KEYS if key not in manifest]
    if missing:
        raise _unavailable(f"模型包清单缺少字段：{', '.join(missing)}")
    if manifest["model_version"] != expected_version:
        raise _unavailable("模型包版本与 EVENT_MODEL_VERSION 不一致")
    _validate_dimension(
        manifest, "base_embedding_dim", base_embedding_dim, manifest["base_embedding_dim"]
    )
    _validate_dimension(manifest, "input_dim", input_dim, manifest["input_dim"])
    _validate_dimension(manifest, "hidden_dim", hidden_dim, manifest["hidden_dim"])
    _validate_dimension(manifest, "num_layers", num_layers, manifest["num_layers"])
    weights_name = manifest.get("weights_file", DEFAULT_WEIGHTS_FILE)
    if not isinstance(weights_name, str) or not weights_name:
        raise _unavailable("模型包权重文件名无效")
    weights_path = (root / weights_name).resolve()
    try:
        weights_path.relative_to(root.resolve())
    except ValueError as exc:
        raise _unavailable("模型包权重路径越界") from exc
    if not weights_path.is_file() or weights_path.stat().st_size == 0:
        raise _unavailable("模型包权重文件缺失或为空")
    expected_sha = manifest["weights_sha256"]
    if not isinstance(expected_sha, str) or len(expected_sha) != 64:
        raise _unavailable("模型包权重校验和格式无效")
    if _file_sha256(weights_path) != expected_sha:
        raise _unavailable("模型包权重校验和不匹配")
    config: dict[str, Any] = {}
    config_path = root / CONFIG_FILE
    if config_path.is_file():
        config = _read_json(config_path)
        if "model_version" in config and config["model_version"] != expected_version:
            raise _unavailable("模型包 config 与 manifest 版本不一致")
    return ModelPackage(
        uri=str(root),
        model_version=expected_version,
        base_embedding_dim=base_embedding_dim,
        input_dim=input_dim,
        hidden_dim=hidden_dim,
        num_layers=num_layers,
        weights_path=weights_path,
        weights_sha256=expected_sha,
        manifest=manifest,
        config=config,
    )


def load_model_weights(package: ModelPackage) -> dict[str, Any]:
    try:
        from safetensors.torch import load_file
    except ImportError as exc:
        raise _unavailable("safetensors 未安装，无法加载模型包") from exc
    try:
        weights = load_file(str(package.weights_path), device="cpu")
    except (OSError, ValueError, RuntimeError) as exc:
        raise _unavailable("模型包权重无法解析") from exc
    if not weights:
        raise _unavailable("模型包权重为空")
    return weights


def _build_embedding(settings: Settings) -> EmbeddingProvider:
    if settings.embedding_provider == "remote":
        missing = settings._provider_missing("embedding")
        if missing:
            raise _unavailable(f"远程 Embedding 配置缺失：{', '.join(missing)}")
        return RemoteEmbeddingProvider(settings)
    if settings.embedding_provider == "local":
        missing = settings._provider_missing("embedding")
        if missing:
            raise _unavailable(f"本地 Embedding 配置缺失：{', '.join(missing)}")
        return LocalBGEProvider(settings)
    raise _unavailable("EMBEDDING_PROVIDER 未配置")


def _build_reranker(settings: Settings) -> Reranker | None:
    if not settings.reranker_enabled:
        return None
    if settings.reranker_provider == "remote":
        missing = settings._provider_missing("reranker")
        if missing:
            raise _unavailable(f"远程 Reranker 配置缺失：{', '.join(missing)}")
        return RemoteReranker(settings)
    if settings.reranker_provider == "local":
        missing = settings._provider_missing("reranker")
        if missing:
            raise _unavailable(f"本地 Reranker 配置缺失：{', '.join(missing)}")
        return LocalReranker(settings)
    raise _unavailable("RERANKER_PROVIDER 未配置")


def build_model_bundle(settings: Settings) -> ModelBundle:
    """按 Settings 构造本地/远程 Provider 集合与事件编码器。"""
    package = load_model_package(
        settings.event_model_uri or "",
        expected_version=settings.event_model_version or "",
        base_embedding_dim=settings.base_embedding_dim,
        input_dim=settings.lstm_input_dim,
        hidden_dim=settings.lstm_hidden_dim,
        num_layers=settings.lstm_num_layers,
    )
    weights = load_model_weights(package)
    encoder = LSTMEventEncoder(
        model_version=package.model_version,
        base_dim=settings.base_embedding_dim,
        input_dim=settings.lstm_input_dim,
        hidden_dim=settings.lstm_hidden_dim,
        num_layers=settings.lstm_num_layers,
        weights=weights,
        device=settings.model_device or "cpu",
    )
    embedding = _build_embedding(settings)
    reranker = _build_reranker(settings)
    logger.info(
        "model_bundle_loaded",
        event_model_version=package.model_version,
        embedding_model_version=embedding.model_version,
        embedding_provider=settings.embedding_provider,
        reranker_provider=settings.reranker_provider if reranker else None,
        embedding_api_base=sanitize_url(settings.embedding_api_base),
        reranker_api_base=sanitize_url(settings.reranker_api_base),
    )
    return ModelBundle(
        embedding=embedding,
        reranker=reranker,
        event_encoder=encoder,
        query_encoder=QueryAdapter(encoder),
        embedding_model_version=embedding.model_version,
        event_model_version=package.model_version,
        package=package,
    )


def _cache_key(settings: Settings) -> tuple[Any, ...]:
    def secret_digest(value: Any) -> str | None:
        if value is None:
            return None
        return hashlib.sha256(value.get_secret_value().encode("utf-8")).hexdigest()

    return (
        settings.environment,
        settings.event_model_uri,
        settings.event_model_version,
        settings.base_embedding_dim,
        settings.lstm_input_dim,
        settings.lstm_hidden_dim,
        settings.lstm_num_layers,
        settings.model_device,
        settings.embedding_provider,
        settings.embedding_model_name,
        settings.embedding_model_version,
        settings.embedding_api_base,
        secret_digest(settings.embedding_api_key),
        settings.reranker_enabled,
        settings.reranker_provider,
        settings.reranker_model_name,
        settings.reranker_model_version,
        settings.reranker_api_base,
        secret_digest(settings.reranker_api_key),
    )


class ModelRegistry:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._bundle: ModelBundle | None = None

    def get_bundle(self) -> ModelBundle:
        if self._bundle is None:
            self._bundle = build_model_bundle(self.settings)
        return self._bundle


_REGISTRIES: dict[tuple[Any, ...], ModelRegistry] = {}


def get_model_registry(settings: Settings) -> ModelRegistry:
    key = _cache_key(settings)
    registry = _REGISTRIES.get(key)
    if registry is None:
        registry = ModelRegistry(settings)
        _REGISTRIES[key] = registry
    return registry
