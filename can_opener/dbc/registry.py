from __future__ import annotations

from can_opener.errors import UnknownVehicleSignalError

from .types import VehicleSignal, VehicleSignalState


class SignalRegistry:
    def __init__(self) -> None:
        self._signals: dict[str, VehicleSignal] = {}
        self._enum_states: dict[str, VehicleSignalState] = {}

    def clear(self) -> None:
        self._signals.clear()
        self._enum_states.clear()

    def set(self, signal: VehicleSignal) -> None:
        self._signals[signal.name] = signal
        self._set_enum_states(signal)

    def load(self, signals: list[VehicleSignal]) -> None:
        for signal in signals:
            self.set(signal)

    def resolve(self, name: str) -> VehicleSignal:
        signal = self._signals.get(name)
        if signal is None:
            raise UnknownVehicleSignalError(name)
        return signal

    def resolve_state(self, name: str) -> VehicleSignalState:
        signal = self._signals.get(name)
        if signal is not None:
            return VehicleSignalState(name=signal.name, signal=signal)
        enum_state = self._enum_states.get(name)
        if enum_state is None:
            raise UnknownVehicleSignalError(name)
        return enum_state

    def find_by_can_id(self, can_id: int) -> list[VehicleSignal]:
        return [signal for signal in self._signals.values() if signal.can_id == can_id]

    def find_by_message_signal(self, message_name: str, signal_name: str) -> VehicleSignal | None:
        return next(
            (
                signal
                for signal in self._signals.values()
                if signal.message_name == message_name and signal.name == signal_name
            ),
            None,
        )

    def values(self) -> list[VehicleSignal]:
        return list(self._signals.values())

    def _set_enum_states(self, signal: VehicleSignal) -> None:
        if signal.enum_values is None:
            return
        for value, label in signal.enum_values.items():
            self._enum_states[label] = VehicleSignalState(
                name=label,
                signal=signal,
                enum_value=int(value),
            )
