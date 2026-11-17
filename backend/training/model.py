"""训练期编码器：结构与 `app.ml.event_encoder.LSTMEventEncoder` 完全一致。

权重键名必须与运行期模块一致（projection.* / lstm.*），
这样 `save_weights` 导出的 Safetensors 可以被 `load_model_package` 严格加载。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import torch
from torch import Tensor, nn


class EncoderModule(nn.Module):
    def __init__(
        self,
        *,
        base_dim: int = 1024,
        input_dim: int = 256,
        hidden_dim: int = 256,
        num_layers: int = 1,
    ) -> None:
        super().__init__()
        if min(base_dim, input_dim, hidden_dim) <= 0 or num_layers < 1:
            raise ValueError("编码器维度配置无效")
        self.base_dim = base_dim
        self.input_dim = input_dim
        self.hidden_dim = hidden_dim
        self.num_layers = num_layers
        self.projection = nn.Linear(base_dim, input_dim)
        self.lstm = nn.LSTM(
            input_size=input_dim,
            hidden_size=hidden_dim,
            num_layers=num_layers,
            batch_first=True,
        )

    def state_dict_for_export(self) -> dict[str, Any]:
        return {key: value.detach().cpu() for key, value in self.state_dict().items()}


def pad_windows(
    windows: Sequence[Sequence[Sequence[float]]],
    *,
    device: torch.device | str = "cpu",
) -> tuple[Tensor, Tensor]:
    """把变长窗口填充为 [B, L, D] 张量，并返回 [B, L] 掩码。"""
    if not windows:
        raise ValueError("windows 不能为空")
    width = len(windows[0][0])
    max_length = max(len(window) for window in windows)
    batch = torch.zeros(len(windows), max_length, width, dtype=torch.float32, device=device)
    mask = torch.zeros(len(windows), max_length, dtype=torch.float32, device=device)
    for index, window in enumerate(windows):
        if not window:
            raise ValueError("窗口不能为空")
        for position, vector in enumerate(window):
            if len(vector) != width:
                raise ValueError("窗口内向量维度必须一致")
            batch[index, position] = torch.tensor(vector, dtype=torch.float32, device=device)
            mask[index, position] = 1.0
    return batch, mask


def encode_windows(module: EncoderModule, base: Tensor, mask: Tensor) -> Tensor:
    """逐步推进 LSTM 并用掩码保留最后有效步，与运行期实现同构。"""
    projected = module.projection(base)
    batch_size = base.shape[0]
    hidden = torch.zeros(
        module.num_layers, batch_size, module.hidden_dim, dtype=base.dtype, device=base.device
    )
    cell = torch.zeros_like(hidden)
    last = torch.zeros(batch_size, module.hidden_dim, dtype=base.dtype, device=base.device)
    for position in range(base.shape[1]):
        step = projected[:, position : position + 1]
        output, (next_hidden, next_cell) = module.lstm(step, (hidden, cell))
        valid = mask[:, position].view(1, batch_size, 1)
        hidden = next_hidden * valid + hidden * (1 - valid)
        cell = next_cell * valid + cell * (1 - valid)
        last = torch.where(mask[:, position].unsqueeze(1) > 0, output[:, 0], last)
    return torch.nn.functional.normalize(last, p=2, dim=-1)


def encode_queries(module: EncoderModule, base: Tensor) -> Tensor:
    """查询适配器：零初始状态单步 LSTM。"""
    projected = module.projection(base).unsqueeze(1)
    batch_size = base.shape[0]
    hidden = torch.zeros(
        module.num_layers, batch_size, module.hidden_dim, dtype=base.dtype, device=base.device
    )
    output, _ = module.lstm(projected, (hidden, hidden.clone()))
    return torch.nn.functional.normalize(output[:, 0], p=2, dim=-1)
