from __future__ import annotations

from typing import Any

from .types import ProfileValueNormalization


def apply_value_normalization(
    value: Any, normalize: ProfileValueNormalization | None
) -> Any:
    if normalize is None:
        return value
    enum_value = (normalize.enum or {}).get(str(value))
    return enum_value if enum_value is not None else value
