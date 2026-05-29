from __future__ import annotations

import re
from typing import Any, Callable

StateCallback = Callable[[], None]


class VehicleState:
    def __init__(self) -> None:
        self._data: dict[str, Any] = {}
        self._callbacks: set[StateCallback] = set()

    def update(self, signal: str, value: Any) -> None:
        self._data[signal] = value
        for callback in list(self._callbacks):
            callback()

    def get(self, signal: str) -> Any:
        return self._data.get(signal)

    def clear(self) -> None:
        if not self._data:
            return
        self._data.clear()
        for callback in list(self._callbacks):
            callback()

    def subscribe(self, callback: StateCallback) -> Callable[[], None]:
        self._callbacks.add(callback)

        def dispose() -> None:
            self._callbacks.discard(callback)

        return dispose

    def snapshot(self) -> dict[str, Any]:
        return dict(self._data)

    def __getattr__(self, name: str) -> Any:
        value = self.get(name)
        if value is not None:
            return value
        value = self.get(_to_upper_snake_case(name))
        if value is not None:
            return value
        raise AttributeError(name)


def _to_upper_snake_case(value: str) -> str:
    value = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", value)
    value = re.sub(r"[-\s]+", "_", value)
    return value.upper()
