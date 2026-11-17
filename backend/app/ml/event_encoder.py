from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Protocol

from app.core.errors import AppError
from app.ml.serialization import SerializedState, serialize_state


@dataclass(frozen=True)
class EncodedEvent:
    event_vector: list[float]
    state: SerializedState


class EventEncoder(Protocol):
    model_version: str

    def encode(
        self,
        base_vectors: list[list[float]],
        *,
        mask: list[float] | None = None,
        init_state: tuple[Any, Any] | None = None,
    ) -> EncodedEvent:
        """编码事件窗口，返回当前事件向量与推进后的 (h,c)。"""


def _require_torch() -> Any:
    try:
        import torch
    except ImportError as exc:
        raise AppError("MODEL_UNAVAILABLE", "PyTorch 未安装，无法加载事件编码器", 503) from exc
    return torch


def _l2_normalize(values: Any) -> Any:
    torch = _require_torch()
    return torch.nn.functional.normalize(values, p=2, dim=-1)


class LSTMEventEncoder:
    """投影 1024→256 + 单层 LSTM 的事件编码器。

    权重必须由模型包显式加载，禁止在生产路径使用随机初始化。
    """

    def __init__(
        self,
        *,
        model_version: str,
        base_dim: int = 1024,
        input_dim: int = 256,
        hidden_dim: int = 256,
        num_layers: int = 1,
        weights: dict[str, Any] | None = None,
        allow_untrained: bool = False,
        device: str = "cpu",
    ) -> None:
        if not model_version:
            raise AppError("MODEL_UNAVAILABLE", "事件模型版本不能为空", 503)
        if min(base_dim, input_dim, hidden_dim) <= 0 or num_layers < 1:
            raise AppError("MODEL_UNAVAILABLE", "事件编码器维度配置无效", 503)
        if weights is None and not allow_untrained:
            raise AppError("MODEL_UNAVAILABLE", "事件编码器缺少已训练权重", 503)
        self.model_version = model_version
        self.base_dim = base_dim
        self.input_dim = input_dim
        self.hidden_dim = hidden_dim
        self.num_layers = num_layers
        self.device = device
        self._torch = _require_torch()

        torch = self._torch
        nn = torch.nn

        class _EncoderModule(nn.Module):
            def __init__(self) -> None:
                super().__init__()
                self.projection = nn.Linear(base_dim, input_dim)
                self.lstm = nn.LSTM(
                    input_size=input_dim,
                    hidden_size=hidden_dim,
                    num_layers=num_layers,
                    batch_first=True,
                )

        module = _EncoderModule()
        if weights is not None:
            try:
                module.load_state_dict(weights, strict=True)
            except (RuntimeError, ValueError) as exc:
                raise AppError("MODEL_UNAVAILABLE", "事件编码器权重与结构不匹配", 503) from exc
        module.to(device)
        module.eval()
        self._module = module

    def state_dict(self) -> dict[str, Any]:
        return {key: value.detach().cpu() for key, value in self._module.state_dict().items()}

    def _initial_state(self, init_state: tuple[Any, Any] | None) -> tuple[Any, Any]:
        torch = self._torch
        if init_state is None:
            zeros = torch.zeros(
                self.num_layers, 1, self.hidden_dim, dtype=torch.float32, device=self.device
            )
            return zeros, zeros.clone()
        h_state, c_state = init_state
        expected = (self.num_layers, self.hidden_dim)
        prepared = []
        for name, tensor in (("h", h_state), ("c", c_state)):
            shape = tuple(tensor.shape)
            if shape not in {expected, (self.num_layers, 1, self.hidden_dim)}:
                raise AppError("STATE_INVALID", f"初始状态 {name} 形状必须为 {expected}", 500)
            if not self._torch.isfinite(tensor).all():
                raise AppError("STATE_INVALID", "初始状态包含非有限值", 500)
            value = tensor.to(self.device)
            prepared.append(value if value.dim() == 3 else value.unsqueeze(1))
        return prepared[0], prepared[1]

    def _prepare_inputs(
        self,
        base_vectors: list[list[float]],
        mask: list[float] | None,
    ) -> tuple[Any, Any]:
        if not base_vectors:
            raise ValueError("base_vectors must not be empty")
        length = len(base_vectors)
        if any(len(vector) != self.base_dim for vector in base_vectors):
            raise ValueError(f"base vector dimension must be {self.base_dim}")
        torch = self._torch
        tensor = torch.tensor(base_vectors, dtype=torch.float32, device=self.device)
        if not torch.isfinite(tensor).all():
            raise ValueError("base vectors contain non-finite values")
        if mask is None:
            mask_tensor = torch.ones(length, dtype=torch.float32, device=self.device)
        else:
            if len(mask) != length:
                raise ValueError("mask length must match base_vectors")
            if any(value not in (0, 0.0, 1, 1.0) for value in mask):
                raise ValueError("mask values must be 0 or 1")
            mask_tensor = torch.tensor(mask, dtype=torch.float32, device=self.device)
        if float(mask_tensor.sum().item()) == 0:
            raise ValueError("at least one window position must be valid")
        return tensor, mask_tensor

    def encode(
        self,
        base_vectors: list[list[float]],
        *,
        mask: list[float] | None = None,
        init_state: tuple[Any, Any] | None = None,
    ) -> EncodedEvent:
        tensor, mask_tensor = self._prepare_inputs(base_vectors, mask)
        torch = self._torch
        projected = self._module.projection(tensor)
        h_state, c_state = self._initial_state(init_state)
        last_valid_output = None
        with torch.no_grad():
            for index in range(projected.shape[0]):
                step = projected[index : index + 1].unsqueeze(0)
                output, (next_h, next_c) = self._module.lstm(step, (h_state, c_state))
                valid = mask_tensor[index]
                h_state = next_h * valid + h_state * (1 - valid)
                c_state = next_c * valid + c_state * (1 - valid)
                if float(valid.item()) > 0:
                    last_valid_output = output[:, 0, :]
            if last_valid_output is None:
                raise ValueError("window contains no valid event")
            event_vector = _l2_normalize(last_valid_output.squeeze(0))
        if not math.isclose(
            float(torch.linalg.vector_norm(event_vector).item()), 1.0, rel_tol=1e-5
        ):
            raise AppError("STATE_INVALID", "事件向量 L2 归一化失败", 500)
        serialized = serialize_state(h_state.squeeze(1), c_state.squeeze(1))
        return EncodedEvent(event_vector=event_vector.tolist(), state=serialized)

    def encode_query(self, base_vector: list[float]) -> list[float]:
        """查询适配器：零初始状态单步 LSTM。"""
        if len(base_vector) != self.base_dim:
            raise ValueError(f"query vector dimension must be {self.base_dim}")
        torch = self._torch
        tensor = torch.tensor([base_vector], dtype=torch.float32, device=self.device)
        zeros = torch.zeros(
            self.num_layers, 1, self.hidden_dim, dtype=torch.float32, device=self.device
        )
        with torch.no_grad():
            projected = self._module.projection(tensor).unsqueeze(0)
            output, _ = self._module.lstm(projected, (zeros, zeros.clone()))
            vector = _l2_normalize(output[0, 0])
        return vector.tolist()


def require_torch() -> Any:
    """供训练脚本复用的依赖检查。"""
    return _require_torch()
