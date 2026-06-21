import pytest

from can_opener.dbc.codec import write_signal_value
from can_opener.dbc.types import CanFrame
from can_opener.errors import VirtualVehicleError
from can_opener.profile.loader import ProfileLoader
from can_opener.profile.types import VehicleProfileSource
from can_opener.manager import ConnectVehicleOptions, VirtualVehicleManager

from .fixtures import ROOT, test_vehicle_profile, universal_pid_profile
from .support import FixtureTransport


def request_frame(service: int, pid: int, can_id: int = 0x7DF) -> CanFrame:
    data = bytearray(8)
    data[0] = service
    data[1] = pid
    return CanFrame(can_id=can_id, dlc=2, data=bytes(data))


def test_profile_source_from_directory_reads_declared_dbc_files():
    profile = VehicleProfileSource.from_directory(ROOT / "test" / "basic")

    assert profile.name.endswith("test/basic/profile.yaml")
    assert [dbc.name for dbc in profile.dbc_files] == ["signals.dbc"]
    assert "BO_ 100 POWERTRAIN" in profile.dbc_files[0].content


def test_profile_loader_derives_slug_from_name():
    profile = ProfileLoader().load(
        [
            VehicleProfileSource(
                name="nissan/versa/profile.yaml",
                content="""
version: 1
profile_version: 1.0.0
name: Nissan Versa Vehicle Profile
""",
            )
        ]
    )[0]

    assert profile.display_name == "Nissan Versa Vehicle Profile"
    assert profile.profile_version == "1.0.0"
    assert profile.slug == "nissan-versa-vehicle-profile"


def test_profile_loader_accepts_explicit_slug():
    profile = ProfileLoader().load(
        [
            VehicleProfileSource(
                name="nissan/versa/profile.yaml",
                content="""
version: 1
name: Nissan Versa Vehicle Profile
slug: nissan-versa-2010-vehicle-profile
""",
            )
        ]
    )[0]

    assert profile.slug == "nissan-versa-2010-vehicle-profile"


def test_profile_loader_rejects_invalid_explicit_slug():
    with pytest.raises(VirtualVehicleError, match="slug must contain only lowercase"):
        ProfileLoader().load(
            [
                VehicleProfileSource(
                    name="nissan/versa/profile.yaml",
                    content="""
version: 1
name: Nissan Versa Vehicle Profile
slug: Nissan Versa Vehicle Profile
""",
                )
            ]
        )


def test_profile_loader_rejects_duplicate_slugs():
    with pytest.raises(
        VirtualVehicleError,
        match='Profile slug "nissan-versa-vehicle-profile" is already in use',
    ):
        ProfileLoader().load(
            [
                VehicleProfileSource(
                    name="nissan/versa/profile.yaml",
                    content="""
version: 1
name: Nissan Versa Vehicle Profile
""",
                ),
                VehicleProfileSource(
                    name="nissan/sentra/profile.yaml",
                    content="""
version: 1
slug: nissan-versa-vehicle-profile
name: Nissan Sentra Vehicle Profile
""",
                ),
            ]
        )


@pytest.mark.asyncio
async def test_profile_query_decodes_with_dbc_mapping():
    manager = VirtualVehicleManager()
    transport = FixtureTransport()
    car = await manager.connect(
        ConnectVehicleOptions(id="car-a", transport=transport, profiles=[universal_pid_profile])
    )
    response = car.dbc.encode_signal("RPM", 3000)
    data = bytearray(response.data)
    data[1] = 0x41
    data[2] = 0x0C
    transport.script_response(request_frame(0x01, 0x0C), data)

    assert await car.query("RPM") == 3000
    assert car.state.get("RPM") == 3000
    assert transport.requests[0].signal_name == "RPM"
    assert transport.requests[0].expect_can_response is True
    assert transport.requests[0].response_id_start == 0x7E8
    assert transport.requests[0].response_id_end == 0x7EF
    assert bytes(transport.requests[0].tx_frame.data) == bytes([0x01, 0x0C, 0, 0, 0, 0, 0, 0])


@pytest.mark.asyncio
async def test_vin_ascii_query_strips_expected_prefix():
    manager = VirtualVehicleManager()
    transport = FixtureTransport()
    car = await manager.connect(
        ConnectVehicleOptions(id="car-a", transport=transport, profiles=[universal_pid_profile])
    )
    vin = b"1HGCM82633A004352"
    transport.script_response(request_frame(0x09, 0x02), bytes([0x49, 0x02, 0x01]) + vin)

    assert await car.query("VIN") == "1HGCM82633A004352"
    assert car.state.get("VIN") == "1HGCM82633A004352"


@pytest.mark.asyncio
async def test_subscribe_decodes_incoming_frames():
    manager = VirtualVehicleManager()
    transport = FixtureTransport()
    car = await manager.connect(
        ConnectVehicleOptions(id="car-a", transport=transport, profiles=[test_vehicle_profile])
    )

    assert await car.subscribe("ENGINE_RPM") is True
    transport.emit_frame(car.dbc.encode_signal("ENGINE_RPM", 900))

    assert car.state.get("ENGINE_RPM") == 900
    assert transport.monitor_can_ids == {100}
    assert car.subscription_count() == 1

    await car.unsubscribe("ENGINE_RPM")
    assert car.subscription_count() == 0


@pytest.mark.asyncio
async def test_merges_keyword_subscriptions_that_share_can_frame():
    manager = VirtualVehicleManager()
    transport = FixtureTransport()
    car = await manager.connect(
        ConnectVehicleOptions(id="car-a", transport=transport, profiles=[test_vehicle_profile])
    )

    await car.subscribe("TURN_SIGNAL_LEFT")
    await car.subscribe("HIGH_BEAMS")

    assert transport.monitor_can_ids == {300}
    assert car.subscription_count() == 2

    frame = CanFrame(can_id=300, data=bytearray(8))
    turn_signal = car.dbc.resolve("TURN_SIGNAL_LEFT")
    high_beams = car.dbc.resolve("HIGH_BEAMS")
    data = bytearray(frame.data)
    write_signal_value(data, turn_signal, True)
    write_signal_value(data, high_beams, True)
    transport.emit_frame(CanFrame(can_id=300, data=bytes(data)))

    assert car.state.turn_signal_left == 1
    assert car.state.high_beams == 1

    await car.unsubscribe(["TURN_SIGNAL_LEFT", "HIGH_BEAMS"])
    assert transport.monitor_can_ids == set()


@pytest.mark.asyncio
async def test_sends_profile_declared_action_frames():
    manager = VirtualVehicleManager()
    transport = FixtureTransport()
    car = await manager.connect(
        ConnectVehicleOptions(id="car-a", transport=transport, profiles=[test_vehicle_profile])
    )

    await car.action("HORN")

    assert len(transport.requests) == 1
    assert transport.requests[0].signal_name == "HORN"
    assert transport.requests[0].expect_can_response is False
    assert transport.requests[0].tx_frame.data[0] == 1


@pytest.mark.asyncio
async def test_sends_direct_request_only_action_frames():
    profile = VehicleProfileSource(
        name="direct-action/profile.yaml",
        content="""
version: 1
actions:
  LEFT_SIGNAL:
    send:
      request_id: 0x123
      request: [0x01, 0x00, 0x00, 0x00]
""",
    )
    manager = VirtualVehicleManager()
    transport = FixtureTransport()
    car = await manager.connect(
        ConnectVehicleOptions(id="car-a", transport=transport, profiles=[profile])
    )

    await car.action("LEFT_SIGNAL")

    assert transport.requests[0].tx_frame.can_id == 0x123
    assert transport.requests[0].expect_can_response is False
    assert transport.requests[0].tx_frame.data == bytes([1, 0, 0, 0, 0, 0, 0, 0])
