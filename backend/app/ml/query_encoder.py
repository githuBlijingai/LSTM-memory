from __future__ import annotations

from typing import Protocol

from app.ml.event_encoder import LSTMEventEncoder


class QueryEncoder(Protocol):
    model_version: str

    def encode(self, base_vector: list[float]) -> list[float]:
        """把 1024 维查询基础向量映射到事件空间并归一化。"""


class QueryAdapter:
    """首版复用事件编码器的投影层与 LSTM，以零初始状态执行单步编码。"""

    def __init__(self, encoder: LSTMEventEncoder) -> None:
        self.encoder = encoder

    @property
    def model_version(self) -> str:
        return self.encoder.model_version

    def encode(self, base_vector: list[float]) -> list[float]:
        return self.encoder.encode_query(base_vector)
