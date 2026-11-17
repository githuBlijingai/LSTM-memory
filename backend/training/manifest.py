"""模型包清单：生成、写入与运行期一致性校验。

模型包由 `weights.safetensors`、`config.json`、`manifest.json` 组成，
运行期由 `app.ml.registry.load_model_package` 读取并校验。
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.core.errors import AppError
from app.ml.registry import (
    CONFIG_FILE,
    DEFAULT_WEIGHTS_FILE,
    MANIFEST_FILE,
    REQUIRED_MANIFEST_KEYS,
    load_model_package,
)

MANIFEST_SCHEMA_VERSION = 1


class PackageError(RuntimeError):
    """模型包写入失败。"""


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def git_code_version(default: str = "unknown") -> str:
    """尽力读取当前代码版本；失败时返回显式占位，不伪造提交号。"""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            check=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return default
    version = result.stdout.strip()
    return version or default


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def save_weights(state_dict: dict[str, Any], path: str | Path) -> str:
    """以 Safetensors 保存权重并返回 SHA-256；只接受张量字典。"""
    try:
        from safetensors.torch import save_file
    except ImportError as exc:  # pragma: no cover - 安装 ml 依赖后不会触发
        raise PackageError("safetensors 未安装，无法导出模型包") from exc
    tensors = {key: value.detach().contiguous().cpu() for key, value in state_dict.items()}
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    save_file(tensors, str(target))
    return sha256_file(target)


def build_manifest(
    *,
    model_version: str,
    base_embedding_dim: int,
    input_dim: int,
    hidden_dim: int,
    num_layers: int,
    weights_sha256: str,
    dataset_version: str,
    code_version: str,
    training: dict[str, Any],
    metrics: dict[str, Any],
    embedding_model_version: str | None = None,
    created_at: datetime | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not model_version:
        raise PackageError("model_version 不能为空")
    manifest: dict[str, Any] = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "model_version": model_version,
        "base_embedding_dim": base_embedding_dim,
        "input_dim": input_dim,
        "hidden_dim": hidden_dim,
        "num_layers": num_layers,
        "weights_file": DEFAULT_WEIGHTS_FILE,
        "weights_sha256": weights_sha256,
        "dataset_version": dataset_version,
        "code_version": code_version,
        "embedding_model_version": embedding_model_version,
        "training": training,
        "metrics": metrics,
        "created_at": (created_at or datetime.now(UTC)).isoformat(),
    }
    if extra:
        manifest.update(extra)
    missing = [key for key in REQUIRED_MANIFEST_KEYS if key not in manifest]
    if missing:
        raise PackageError(f"模型包清单缺少字段：{', '.join(missing)}")
    return manifest


def write_package(
    output_dir: str | Path,
    *,
    state_dict: dict[str, Any],
    manifest: dict[str, Any],
    config: dict[str, Any],
) -> Path:
    """写入不可变模型包目录，返回目录路径。"""
    root = Path(output_dir)
    root.mkdir(parents=True, exist_ok=True)
    weights_sha = save_weights(
        state_dict, root / manifest.get("weights_file", DEFAULT_WEIGHTS_FILE)
    )
    manifest = {**manifest, "weights_sha256": weights_sha}
    _write_json(root / MANIFEST_FILE, manifest)
    _write_json(root / CONFIG_FILE, config)
    return root


def verify_package(root: str | Path, *, expected_version: str | None = None) -> dict[str, Any]:
    """用运行期加载器校验模型包，返回可序列化的检查结果。"""
    package_dir = Path(root)
    manifest_path = package_dir / MANIFEST_FILE
    manifest: dict[str, Any] = {}
    if manifest_path.is_file():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except ValueError:
            manifest = {}
    version = expected_version or manifest.get("model_version")
    try:
        package = load_model_package(
            str(package_dir),
            expected_version=version or "",
            base_embedding_dim=int(manifest.get("base_embedding_dim", 0)),
            input_dim=int(manifest.get("input_dim", 0)),
            hidden_dim=int(manifest.get("hidden_dim", 0)),
            num_layers=int(manifest.get("num_layers", 0)),
        )
    except (AppError, TypeError, ValueError) as exc:
        message = getattr(exc, "message", str(exc))
        return {"ok": False, "errors": [message], "manifest": manifest}
    return {
        "ok": True,
        "errors": [],
        "model_version": package.model_version,
        "weights_sha256": package.weights_sha256,
        "manifest": manifest,
    }
