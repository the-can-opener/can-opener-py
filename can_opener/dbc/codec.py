from __future__ import annotations

import math
from typing import Any, Iterable

from .types import CanFrame, VehicleSignal

DEFAULT_FRAME_BYTES = 8


def decode_signal_value(frame: CanFrame, signal: VehicleSignal) -> int | float | str | bytes:
    if signal.value_type == "ascii":
        return _read_signal_bytes(frame.data, signal).decode(errors="replace").rstrip("\0")

    if signal.value_type == "bytes":
        return _read_signal_bytes(frame.data, signal)

    unsigned = _read_raw(frame.data, signal)
    raw = _to_signed(unsigned, signal.length or 0) if signal.signed is True else unsigned
    enum_value = (signal.enum_values or {}).get(raw)
    if enum_value is not None:
        return enum_value

    return raw * (signal.scale if signal.scale is not None else 1) + (
        signal.offset if signal.offset is not None else 0
    )


def encode_signal_value(signal: VehicleSignal, value: Any) -> CanFrame:
    data = bytearray(DEFAULT_FRAME_BYTES)
    write_signal_value(data, signal, value)
    return CanFrame(can_id=signal.can_id, data=bytes(data))


def write_signal_value(data: bytearray, signal: VehicleSignal, value: Any) -> None:
    numeric = _normalize_numeric_value(value)
    physical = (numeric - (signal.offset if signal.offset is not None else 0)) / (
        signal.scale if signal.scale is not None else 1
    )
    raw = round(physical)
    unsigned = _from_signed(raw, signal.length or 0) if signal.signed is True else raw
    _write_raw(data, signal, unsigned)


def decode_frame_signals(
    frame: CanFrame, signals: Iterable[VehicleSignal]
) -> list[tuple[VehicleSignal, Any]]:
    decoded: list[tuple[VehicleSignal, Any]] = []
    for signal in signals:
        if not is_codec_signal(signal) or signal.can_id != frame.can_id:
            continue
        decoded.append((signal, decode_signal_value(frame, signal)))
    return decoded


def is_codec_signal(signal: VehicleSignal) -> bool:
    return isinstance(signal.start_bit, int) and isinstance(signal.length, int)


def _read_raw(data: bytes | bytearray, signal: VehicleSignal) -> int:
    _assert_signal_fits(data, signal)
    assert signal.start_bit is not None and signal.length is not None

    value = 0
    if (signal.byte_order or "little") == "little":
        for i in range(signal.length):
            bit = _read_data_bit(data, signal.start_bit + i)
            value += bit * (2**i)
        return value

    for i in range(signal.length):
        bit_position = _motorola_bit_position(signal.start_bit, i)
        bit = _read_data_bit(data, bit_position)
        value = (value << 1) | bit
    return value


def _read_signal_bytes(data: bytes | bytearray, signal: VehicleSignal) -> bytes:
    _assert_byte_signal_fits(data, signal)
    assert signal.start_bit is not None and signal.length is not None
    start_byte = signal.start_bit // 8
    length_bytes = signal.length // 8
    return bytes(data[start_byte : start_byte + length_bytes])


def _write_raw(data: bytearray, signal: VehicleSignal, value: int) -> None:
    _assert_signal_fits(data, signal)
    assert signal.start_bit is not None and signal.length is not None
    _assert_integer_range(value, signal.length)

    if (signal.byte_order or "little") == "little":
        for i in range(signal.length):
            _write_data_bit(data, signal.start_bit + i, math.floor(value / (2**i)) % 2)
        return

    for i in range(signal.length):
        bit_position = _motorola_bit_position(signal.start_bit, i)
        shift = signal.length - 1 - i
        _write_data_bit(data, bit_position, math.floor(value / (2**shift)) % 2)


def _read_data_bit(data: bytes | bytearray, bit_position: int) -> int:
    byte_index = bit_position // 8
    bit_index = bit_position % 8
    return ((data[byte_index] if byte_index < len(data) else 0) >> bit_index) & 1


def _write_data_bit(data: bytearray, bit_position: int, bit: int) -> None:
    byte_index = bit_position // 8
    bit_index = bit_position % 8
    current = data[byte_index] if byte_index < len(data) else 0
    data[byte_index] = current | (1 << bit_index) if bit == 1 else current & ~(1 << bit_index)


def _motorola_bit_position(start_bit: int, bit_index_from_msb: int) -> int:
    bit_position = start_bit
    for _ in range(bit_index_from_msb):
        bit_position = bit_position + 15 if bit_position % 8 == 0 else bit_position - 1
    return bit_position


def _to_signed(value: int, length: int) -> int:
    sign_bit = 2 ** (length - 1)
    return value if value < sign_bit else value - 2**length


def _from_signed(value: int, length: int) -> int:
    return value if value >= 0 else 2**length + value


def _normalize_numeric_value(value: Any) -> int | float:
    if isinstance(value, bool):
        return 1 if value else 0
    if not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"Signal value must be a finite number or boolean, received {value}")
    return value


def _assert_signal_fits(data: bytes | bytearray, signal: VehicleSignal) -> None:
    if signal.start_bit is None or signal.length is None:
        raise ValueError("Signal does not define bit layout metadata")
    if signal.length <= 0 or signal.length > 52:
        raise ValueError(f"Signal length must be between 1 and 52 bits, received {signal.length}")

    if signal.byte_order == "big":
        positions = [_motorola_bit_position(signal.start_bit, index) for index in range(signal.length)]
    else:
        positions = [signal.start_bit + index for index in range(signal.length)]

    max_bit = len(data) * 8 - 1
    for position in positions:
        if position < 0 or position > max_bit:
            raise ValueError(f"Signal exceeds {len(data)}-byte CAN frame bounds")


def _assert_byte_signal_fits(data: bytes | bytearray, signal: VehicleSignal) -> None:
    if signal.start_bit is None or signal.length is None:
        raise ValueError("Signal does not define bit layout metadata")
    if signal.start_bit % 8 != 0 or signal.length % 8 != 0:
        raise ValueError("Byte or ASCII signals must be byte-aligned")
    start_byte = signal.start_bit // 8
    length_bytes = signal.length // 8
    if length_bytes <= 0 or start_byte + length_bytes > len(data):
        raise ValueError(f"Signal exceeds {len(data)}-byte payload bounds")


def _assert_integer_range(value: int, length: int) -> None:
    if not isinstance(value, int):
        raise ValueError(f"Encoded raw value must be an integer, received {value}")
    if value < 0 or value >= 2**length:
        raise ValueError(f"Encoded raw value {value} does not fit in {length} bits")
