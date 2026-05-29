from can_opener.dbc.codec import decode_signal_value, encode_signal_value
from can_opener.dbc.types import VehicleSignal


def test_round_trips_little_endian_scaled_values():
    signal = VehicleSignal(
        name="RPM",
        protocol="frame",
        can_id=100,
        start_bit=0,
        length=16,
        byte_order="little",
        scale=0.25,
        offset=0,
    )

    frame = encode_signal_value(signal, 3000)

    assert frame.data[0] == 0xE0
    assert frame.data[1] == 0x2E
    assert decode_signal_value(frame, signal) == 3000


def test_round_trips_big_endian_values():
    signal = VehicleSignal(
        name="BIG",
        protocol="frame",
        can_id=101,
        start_bit=7,
        length=12,
        byte_order="big",
        scale=1,
        offset=0,
    )

    frame = encode_signal_value(signal, 0xABC)

    assert decode_signal_value(frame, signal) == 0xABC


def test_decodes_signed_values():
    signal = VehicleSignal(
        name="SIGNED",
        protocol="frame",
        can_id=102,
        start_bit=0,
        length=8,
        byte_order="little",
        signed=True,
        scale=1,
        offset=0,
    )

    frame = encode_signal_value(signal, -5)

    assert decode_signal_value(frame, signal) == -5
