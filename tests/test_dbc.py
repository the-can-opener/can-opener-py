import pytest

from can_opener.dbc.controller import DbcController
from can_opener.dbc.parser import DbcParser
from can_opener.dbc.types import CanFrame
from can_opener.manager import ConnectVehicleOptions, VirtualVehicleManager

from .fixtures import nissan_sentra_dbc, nissan_sentra_profile, test_vehicle_dbc
from .support import FixtureTransport


def test_dbc_parser_parses_messages_signals_attributes_and_values():
    parsed = DbcParser().parse(test_vehicle_dbc)

    assert len(parsed.messages) == 4
    first_signal = parsed.messages[0].signals[0]
    assert first_signal.name == "ENGINE_RPM"
    assert first_signal.start_bit == 0
    assert first_signal.length == 16
    assert first_signal.byte_order == "little"
    assert first_signal.scale == 0.25
    assert first_signal.unit == "rpm"
    assert any(
        attr.name == "SignalProtocol"
        and attr.scope == "SG_"
        and attr.message_id == 201
        and attr.signal_name == "VEHICLE_SPEED"
        and attr.value == "pid"
        for attr in parsed.attributes
    )
    assert parsed.value_tables[0].values[1] == "locked"


def test_nissan_dbc_decodes_shared_lights_status_frame():
    dbc = DbcController()
    dbc.load([nissan_sentra_dbc])

    low_beams = dbc.resolve("LOW_BEAMS")
    assert low_beams.protocol == "frame"
    assert low_beams.can_id == 1549
    assert low_beams.enum_values == {0: "off", 1: "on"}

    decoded = dbc.decode_frame(
        CanFrame(can_id=1549, data=bytes([0b0100_1100, 0b0010_1000, 0, 0, 0, 0, 0, 0]))
    )
    values = {item.name: item.value for item in decoded}

    assert values["LOW_BEAMS"] == "on"
    assert values["HIGH_BEAMS"] == "on"
    assert values["LEFT_SIGNAL"] == "on"
    assert values["RIGHT_SIGNAL"] == "off"
    assert values["FRONT_LEFT_DOOR_OPEN"] == "open"
    assert values["REAR_RIGHT_DOOR_OPEN"] == "open"


@pytest.mark.asyncio
async def test_nissan_profile_subscribes_standardized_monitor_signals():
    manager = VirtualVehicleManager()
    transport = FixtureTransport()
    car = await manager.connect(
        ConnectVehicleOptions(id="sentra", transport=transport, profiles=[nissan_sentra_profile])
    )

    await car.subscribe(
        [
            "SPEED",
            "RPM",
            "TPS",
            "STEERING_ANGLE",
            "BRAKE_LIGHTS",
            "LOW_BEAMS",
            "HIGH_BEAMS",
            "LEFT_SIGNAL",
            "RIGHT_SIGNAL",
        ]
    )

    assert transport.monitor_can_ids == {640, 505, 574, 2, 852, 1549}
    transport.emit_frame(CanFrame(can_id=1549, data=bytes([0b0100_1100, 0b0010_1000, 0, 0, 0, 0, 0, 0])))
    transport.emit_frame(CanFrame(can_id=640, data=bytes([0, 0, 0, 0, 0xD0, 0x07, 0, 0])))

    assert car.state.get("SPEED") == 20
    assert car.state.get("LEFT_SIGNAL") == "on"
    assert car.state.get("RIGHT_SIGNAL") == "off"
    assert car.state.get("LOW_BEAMS") == "on"
    assert car.state.get("HIGH_BEAMS") == "on"
