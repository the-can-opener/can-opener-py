from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .classifier import SignalClassifier, SignalClassifierOptions
from .codec import decode_frame_signals, decode_signal_value, encode_signal_value, is_codec_signal
from .parser import DbcParser
from .registry import SignalRegistry
from .types import CanFrame, DbcFile, DecodedSignalValue, VehicleSignal, VehicleSignalState


@dataclass(frozen=True, slots=True)
class DbcControllerOptions:
    classifier: SignalClassifierOptions | None = None


class DbcController:
    def __init__(self, options: DbcControllerOptions | None = None) -> None:
        self._parser = DbcParser()
        self._classifier = SignalClassifier((options or DbcControllerOptions()).classifier)
        self._signals = SignalRegistry()

    def load(self, files: list[DbcFile]) -> None:
        self._signals.clear()
        for file in files:
            parsed = self._parser.parse(file)
            classified = self._classifier.classify(parsed)
            self._signals.load(classified)

    def resolve(self, name: str) -> VehicleSignal:
        return self._signals.resolve(name)

    def resolve_state(self, name: str) -> VehicleSignalState:
        return self._signals.resolve_state(name)

    def all(self) -> list[VehicleSignal]:
        return self._signals.values()

    def decode_frame(self, frame: CanFrame) -> list[DecodedSignalValue]:
        return [
            DecodedSignalValue(name=signal.name, value=value, signal=signal)
            for signal, value in decode_frame_signals(
                frame, self._signals.find_by_can_id(frame.can_id)
            )
        ]

    def decode_signal(self, signal_name: str, payload: bytes | bytearray) -> Any:
        signal = self.resolve(signal_name)
        return self._decode_signal_payload(signal, payload)

    def decode_message_signal(
        self, message_name: str, signal_name: str, payload: bytes | bytearray
    ) -> Any:
        signal = self._signals.find_by_message_signal(message_name, signal_name)
        if signal is None:
            raise ValueError(f"Unknown DBC decoder: {message_name}.{signal_name}")
        return self._decode_signal_payload(signal, payload)

    def resolve_message_signal(self, message_name: str, signal_name: str) -> VehicleSignal:
        signal = self._signals.find_by_message_signal(message_name, signal_name)
        if signal is None:
            raise ValueError(f"Unknown DBC decoder: {message_name}.{signal_name}")
        return signal

    def encode_signal(self, name: str, value: Any) -> CanFrame:
        signal = self.resolve(name)
        if not is_codec_signal(signal):
            raise ValueError(f"{name} does not define bit layout metadata")
        return encode_signal_value(signal, value)

    @staticmethod
    def _decode_signal_payload(signal: VehicleSignal, payload: bytes | bytearray) -> Any:
        if not is_codec_signal(signal):
            return bytes(payload)
        return decode_signal_value(CanFrame(can_id=signal.can_id, data=payload), signal)
