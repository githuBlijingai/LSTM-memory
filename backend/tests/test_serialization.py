from __future__ import annotations

import pytest

from app.core.errors import AppError
from app.ml.serialization import compute_checksum, deserialize_state, serialize_state


def test_checksum_is_deterministic_and_sensitive() -> None:
    first = compute_checksum(b"h", b"c")
    assert first == compute_checksum(b"h", b"c")
    assert first != compute_checksum(b"h", b"changed")
    assert len(first) == 64


def test_safetensors_roundtrip_and_checksum_validation() -> None:
    torch = pytest.importorskip("torch")
    pytest.importorskip("safetensors")

    h_tensor = torch.tensor([[0.1, -0.2, 0.3, 0.4]], dtype=torch.float32)
    c_tensor = torch.tensor([[0.5, 0.6, -0.7, 0.8]], dtype=torch.float32)
    serialized = serialize_state(h_tensor, c_tensor)

    assert serialized.shape == [1, 4]
    assert serialized.dtype == "float32"
    assert serialized.checksum == compute_checksum(serialized.h_bytes, serialized.c_bytes)

    restored_h, restored_c = deserialize_state(serialized)
    assert torch.allclose(restored_h, h_tensor)
    assert torch.allclose(restored_c, c_tensor)

    with pytest.raises(AppError) as error:
        deserialize_state(serialized, expected_checksum="0" * 64)
    assert error.value.code == "SNAPSHOT_INVALID"

    with pytest.raises(AppError) as shape_error:
        deserialize_state(serialized, expected_shape=[2, 2])
    assert shape_error.value.code == "SNAPSHOT_INVALID"
