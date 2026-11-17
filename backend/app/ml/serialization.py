from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

from app.core.errors import AppError


@dataclass(frozen=True)
class SerializedState:
    h_bytes: bytes
    c_bytes: bytes
    dtype: str
    shape: list[int]
    checksum: str


def compute_checksum(h_bytes: bytes, c_bytes: bytes) -> str:
    digest = hashlib.sha256()
    digest.update(h_bytes)
    digest.update(c_bytes)
    return digest.hexdigest()


def verify_serialized_state(
    h_bytes: bytes,
    c_bytes: bytes,
    *,
    checksum: str,
    shape: list[int] | None = None,
    dtype: str | None = None,
) -> None:
    if compute_checksum(h_bytes, c_bytes) != checksum:
        raise AppError("SNAPSHOT_INVALID", "状态快照校验和不匹配", 500)
    if shape is not None and any(dimension <= 0 for dimension in shape):
        raise AppError("SNAPSHOT_INVALID", "状态快照形状无效", 500)
    if dtype is not None and not dtype:
        raise AppError("SNAPSHOT_INVALID", "状态快照数据类型无效", 500)


def _require_torch() -> Any:
    try:
        import torch
    except ImportError as exc:
        raise AppError("MODEL_UNAVAILABLE", "PyTorch 未安装，无法处理状态张量", 503) from exc
    return torch


def _require_safetensors() -> Any:
    try:
        from safetensors import torch as safetensors_torch
    except ImportError as exc:
        raise AppError("MODEL_UNAVAILABLE", "safetensors 未安装，无法序列化状态", 503) from exc
    return safetensors_torch


def serialize_state(h_tensor: Any, c_tensor: Any) -> SerializedState:
    """仅使用 safetensors 序列化 (h, c)，禁止 pickle。"""
    torch = _require_torch()
    safetensors_torch = _require_safetensors()
    if not isinstance(h_tensor, torch.Tensor) or not isinstance(c_tensor, torch.Tensor):
        raise AppError("STATE_INVALID", "状态必须是张量", 500)
    if tuple(h_tensor.shape) != tuple(c_tensor.shape):
        raise AppError("STATE_INVALID", "h 与 c 形状必须一致", 500)
    if h_tensor.dtype != c_tensor.dtype:
        raise AppError("STATE_INVALID", "h 与 c 数据类型必须一致", 500)
    if not h_tensor.is_floating_point():
        raise AppError("STATE_INVALID", "状态张量必须是浮点类型", 500)
    h_cpu = h_tensor.detach().to("cpu").contiguous()
    c_cpu = c_tensor.detach().to("cpu").contiguous()
    h_bytes = safetensors_torch.save({"h": h_cpu})
    c_bytes = safetensors_torch.save({"c": c_cpu})
    return SerializedState(
        h_bytes=h_bytes,
        c_bytes=c_bytes,
        dtype=str(h_cpu.dtype).replace("torch.", ""),
        shape=list(h_cpu.shape),
        checksum=compute_checksum(h_bytes, c_bytes),
    )


def deserialize_state(
    serialized: SerializedState,
    *,
    expected_checksum: str | None = None,
    expected_shape: list[int] | None = None,
    expected_dtype: str | None = None,
) -> tuple[Any, Any]:
    """反序列化前强制校验校验和、形状与 dtype。"""
    verify_serialized_state(
        serialized.h_bytes,
        serialized.c_bytes,
        checksum=expected_checksum or serialized.checksum,
        shape=expected_shape or serialized.shape,
        dtype=expected_dtype or serialized.dtype,
    )
    if expected_dtype and serialized.dtype != expected_dtype:
        raise AppError("SNAPSHOT_INVALID", "状态快照数据类型不匹配", 500)
    if expected_shape and serialized.shape != expected_shape:
        raise AppError("SNAPSHOT_INVALID", "状态快照形状不匹配", 500)
    torch = _require_torch()
    safetensors_torch = _require_safetensors()
    try:
        h_tensor = safetensors_torch.load(serialized.h_bytes)["h"]
        c_tensor = safetensors_torch.load(serialized.c_bytes)["c"]
    except (ValueError, KeyError, RuntimeError) as exc:
        raise AppError("SNAPSHOT_INVALID", "状态快照无法解析", 500) from exc
    if tuple(h_tensor.shape) != tuple(c_tensor.shape):
        raise AppError("SNAPSHOT_INVALID", "状态快照形状不一致", 500)
    if expected_shape and list(h_tensor.shape) != expected_shape:
        raise AppError("SNAPSHOT_INVALID", "状态快照形状不匹配", 500)
    if expected_dtype and str(h_tensor.dtype).replace("torch.", "") != expected_dtype:
        raise AppError("SNAPSHOT_INVALID", "状态快照数据类型不匹配", 500)
    if h_tensor.dtype != c_tensor.dtype or not h_tensor.is_floating_point():
        raise AppError("SNAPSHOT_INVALID", "状态快照数据类型无效", 500)
    if not torch.isfinite(h_tensor).all() or not torch.isfinite(c_tensor).all():
        raise AppError("SNAPSHOT_INVALID", "状态快照包含非有限值", 500)
    return h_tensor, c_tensor
