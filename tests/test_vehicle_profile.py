import asyncio

import pytest

from can_opener.dbc.codec import write_signal_value
from can_opener.dbc.types import CanFrame
from can_opener.errors import VehicleConnectionError, VirtualVehicleError
from can_opener.profile.capabilities import CapabilityRegistry
from can_opener.profile.loader import ProfileLoader, merge_profile_can_bus_configs
from can_opener.profile.request_builder import build_request_frame, can_fd_request
from can_opener.profile.types import (
    ProfileCanBusConfig,
    ProfileCanBusFd,
    ProfileCanFdConfig,
    VehicleProfileSource,
)
from can_opener.manager import ConnectVehicleOptions, VirtualVehicleManager
from can_opener.transport.types import ConfigureBusResponse

from .fixtures import ROOT, nissan_sentra_profile, test_vehicle_profile, universal_pid_profile
from .support import FixtureTransport


def load_profile(content: str, name: str = "inline/profile.yaml"):
    return ProfileLoader().load([VehicleProfileSource(name=name, content=content)])[0]


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


@pytest.mark.parametrize("folder", ["nissan/sentra", "test/basic", "universal/pid"])
def test_example_profiles_load(folder):
    source = VehicleProfileSource.from_directory(ROOT / folder)
    profile = ProfileLoader().load([source])[0]
    assert profile.name.endswith(f"{folder}/profile.yaml")
    assert profile.endpoints
    registry = CapabilityRegistry()
    registry.load([profile])
    assert registry.has_profiles()


def test_example_profile_applies_to_is_parsed():
    profile = ProfileLoader().load([test_vehicle_profile])[0]
    assert profile.applies_to is not None
    assert profile.applies_to.manufacturers == ["Test"]
    assert profile.applies_to.models == ["Basic"]
    assert profile.applies_to.years == []

    empty = ProfileLoader().load([universal_pid_profile])[0]
    assert empty.applies_to is not None
    assert empty.applies_to.manufacturers == []

    sentra = ProfileLoader().load([nissan_sentra_profile])[0]
    assert sentra.endpoints


def test_applies_to_parses_years_and_ranges():
    profile = load_profile(
        """
version: 1
applies_to:
  manufacturers: [Nissan]
  models: [Sentra, Versa]
  years:
    - 2011
    - range: [2012, 2015]
  trims: [S, SV]
  engines: [2.0L]
  markets: [NA]
"""
    )
    assert profile.applies_to is not None
    assert profile.applies_to.years == [2011, (2012, 2015)]
    assert profile.applies_to.trims == ["S", "SV"]
    assert profile.applies_to.engines == ["2.0L"]
    assert profile.applies_to.markets == ["NA"]


def test_loads_can_buses_with_auto_bitrate_and_fd_variants():
    profile = load_profile(
        """
version: 1
can:
  buses:
    1:
      bitrate: auto
      data_bitrate: 2000000
      fd: true
    0:
      bitrate: 500000
      fd:
        isotp:
          tx_dl: 64
    2:
      bitrate: 125000
      fd: false
"""
    )
    assert profile.can_buses == [
        ProfileCanBusConfig(bus=0, bitrate=500000, fd=ProfileCanBusFd(isotp_tx_dl=64)),
        ProfileCanBusConfig(bus=1, bitrate="auto", data_bitrate=2000000, fd=ProfileCanBusFd(isotp_tx_dl=8)),
        ProfileCanBusConfig(bus=2, bitrate=125000),
    ]


@pytest.mark.parametrize(
    "buses, message",
    [
        ("300: { bitrate: 500000 }", "can.buses.300 must be an integer between 0 and 255"),
        ("0: { bitrate: 0 }", "can.buses.0.bitrate must be a positive 32-bit integer"),
        ("0: { bitrate: 500000, fd: { isotp: { tx_dl: 12 } } }", "can.buses.0.fd.isotp.tx_dl must be 8 or 64"),
        ("0: { bitrate: 500000, fd: 7 }", "can.buses.0.fd must be a boolean or an object"),
    ],
)
def test_rejects_invalid_can_bus_settings(buses, message):
    with pytest.raises(VirtualVehicleError, match=message):
        load_profile(f"version: 1\ncan:\n  buses:\n    {buses}\n")


def test_loads_endpoint_bus_extended_fd_and_step_timing():
    profile = load_profile(
        """
version: 1
can:
  buses:
    0:
      bitrate: 500000
      fd:
        isotp:
          tx_dl: 64
endpoints:
  obd:
    bus: 0
    request_id: 0x18DB33F1
    response_ids:
      - range: [0x18DAF100, 0x18DAF1FF]
    extended: true
    timeout_ms: 500
  bcm:
    request_id: 0x7A0
    response_id: 0x7A8
    fd: { tx_dl: 8 }
  legacy:
    request_id: 0x7B0
    response_id: 0x7B8
    fd: false
sequences:
  prep:
    endpoint: bcm
    steps:
      - send: [0x02, 0x10, 0x81, 0, 0, 0, 0, 0]
        expect: [0x50, 0x81]
        timeout_ms: 250
        delay_ms: 50
queries:
  RPM:
    endpoint: obd
    send: [0x02, 0x01, 0x0C]
    expect: [0x41, 0x0C]
    timeout_ms: 750
actions:
  HORN:
    endpoint: legacy
    batch: false
    steps:
      - ref: sequences.prep
      - send: [0x04, 0x30, 0x30, 0x00, 0x01, 0, 0, 0]
        expect: [0x70, 0x30, 0x01]
  LEFT_SIGNAL:
    send:
      bus: 1
      request_id: 0x123
      fd: false
      request: [0x01]
signals:
  DOOR:
    monitor:
      bus: 1
      message: BODY
      signal: DOOR
    state:
      enum:
        inactive: closed
"""
    )
    obd, bcm, legacy = profile.endpoints
    assert (obd.bus, obd.extended, obd.fd) == (0, True, None)
    assert bcm.fd == ProfileCanFdConfig(tx_dl=8)
    assert legacy.fd is False

    assert profile.queries[0].timeout_ms == 750

    horn = profile.actions[0]
    assert horn.batch is False
    # The sequence's own endpoint overrides the action endpoint.
    assert horn.steps[0].endpoint == "bcm"
    assert horn.steps[0].timeout_ms == 250
    assert horn.steps[0].delay_ms == 50
    assert horn.steps[1].endpoint == "legacy"
    assert horn.steps[1].delay_ms == 20

    left = profile.actions[1]
    assert left.batch is None
    assert (left.steps[0].bus, left.steps[0].fd, left.steps[0].request_id) == (1, False, 0x123)

    assert profile.signals[0].bus == 1
    assert profile.signals[0].normalize is not None
    assert profile.signals[0].normalize.enum == {"inactive": "closed"}

    registry = CapabilityRegistry()
    registry.load([profile])
    assert registry.resolve_endpoint("obd").can_fd == ProfileCanFdConfig(tx_dl=64)
    assert registry.resolve_endpoint("bcm").can_fd == ProfileCanFdConfig(tx_dl=8)
    assert registry.resolve_endpoint("legacy").can_fd is None
    assert registry.resolve_endpoint("obd").extended is True


def test_rejects_out_of_range_step_timing():
    with pytest.raises(VirtualVehicleError, match="steps.delay_ms must be an integer between 0 and 65535"):
        load_profile(
            """
version: 1
endpoints:
  body: { request_id: 0x745, response_id: 0x765 }
actions:
  HORN:
    endpoint: body
    steps:
      - send: [0x01]
        delay_ms: 70000
"""
        )


def test_skips_placeholder_actions_without_send_or_steps():
    profile = load_profile(
        """
version: 1
endpoints:
  body: { request_id: 0x745, response_id: 0x765 }
actions:
  TODO: {}
  HORN:
    endpoint: body
    send: [0x01]
"""
    )
    assert [action.name for action in profile.actions] == ["HORN"]


def test_fd_on_undeclared_bus_is_a_load_error():
    endpoint_fd = load_profile(
        """
version: 1
endpoints:
  obd:
    request_id: 0x7DF
    response_id: 0x7E8
    fd: true
"""
    )
    with pytest.raises(VirtualVehicleError, match="Endpoint obd sends CAN FD on bus 0, but no loaded profile declares can.buses.0.fd"):
        CapabilityRegistry().load([endpoint_fd])

    inline_fd = load_profile(
        """
version: 1
can:
  buses:
    1:
      bitrate: 500000
actions:
  CHIME:
    send:
      bus: 1
      request_id: 0x3F1
      fd: true
      request: [0x02, 0x10]
"""
    )
    with pytest.raises(VirtualVehicleError, match="Action CHIME sends CAN FD on bus 1"):
        CapabilityRegistry().load([inline_fd])


def test_bus_declared_by_one_profile_applies_to_endpoints_of_another():
    buses = load_profile(
        """
version: 1
can:
  buses:
    0:
      bitrate: 500000
      data_bitrate: 2000000
      fd: true
""",
        name="buses/profile.yaml",
    )
    shared = load_profile(
        """
version: 1
endpoints:
  obd:
    request_id: 0x7DF
    response_id: 0x7E8
actions:
  CHIME:
    send:
      request_id: 0x3F1
      request: [0x02, 0x10]
  CLASSIC:
    send:
      request_id: 0x3F2
      fd: false
      request: [0x02, 0x10]
""",
        name="shared/profile.yaml",
    )
    registry = CapabilityRegistry()
    registry.load([shared, buses])
    assert registry.resolve_endpoint("obd").can_fd == ProfileCanFdConfig(tx_dl=8)
    assert registry.inline_step_can_fd(registry.resolve_action("CHIME").steps[0], "CHIME") == ProfileCanFdConfig(tx_dl=8)
    assert registry.inline_step_can_fd(registry.resolve_action("CLASSIC").steps[0], "CLASSIC") is None
    assert registry.bus_configs() == [
        ProfileCanBusConfig(bus=0, bitrate=500000, data_bitrate=2000000, fd=ProfileCanBusFd(isotp_tx_dl=8))
    ]


def test_merge_profile_can_bus_configs_rejects_conflicts():
    a = load_profile("version: 1\ncan:\n  buses:\n    0: { bitrate: 500000 }\n", name="a/profile.yaml")
    b = load_profile("version: 1\ncan:\n  buses:\n    1: { bitrate: 125000 }\n", name="b/profile.yaml")
    c = load_profile("version: 1\ncan:\n  buses:\n    0: { bitrate: 250000 }\n", name="c/profile.yaml")

    assert [config.bus for config in merge_profile_can_bus_configs([b, a])] == [0, 1]
    assert merge_profile_can_bus_configs([a, a]) == a.can_buses
    with pytest.raises(VirtualVehicleError, match="Conflicting CAN bus 0 settings"):
        merge_profile_can_bus_configs([a, c])


FD_BUS_PROFILE = """
version: 1
can:
  buses:
    0:
      bitrate: 500000
      data_bitrate: 2000000
      fd:
        isotp:
          tx_dl: 64
    1:
      bitrate: auto
endpoints:
  obd:
    request_id: 0x7DF
    response_ids:
      - range: [0x7E8, 0x7EF]
    timeout_ms: 500
  body:
    bus: 1
    request_id: 0x745
    response_id: 0x765
    extended: false
queries:
  RPM:
    endpoint: obd
    send: [0x02, 0x01, 0x0C, 0, 0, 0, 0, 0]
    expect: [0x41, 0x0C]
    decoder: bytes
actions:
  WAKE_BODY:
    endpoint: body
    send: [0x02, 0x10, 0x81, 0, 0, 0, 0, 0]
"""


async def test_connect_applies_bus_timing_and_routes_requests_by_bus():
    manager = VirtualVehicleManager()
    transport = FixtureTransport()
    transport.script_bus_config_response(1, ConfigureBusResponse(status="ok", bitrate=250000))
    car = await manager.connect(
        ConnectVehicleOptions(
            id="car-a",
            transport=transport,
            profiles=[VehicleProfileSource(name="fd/profile.yaml", content=FD_BUS_PROFILE)],
        )
    )

    assert [(req.bus, req.bitrate, req.data_bitrate) for req in transport.bus_config_updates] == [
        (0, 500000, 2000000),
        (1, "auto", None),
    ]
    assert transport.bus_config_updates[0].fd == ProfileCanBusFd(isotp_tx_dl=64)
    timing = car.bus_timing()
    assert set(timing) == {0, 1}
    assert (timing[0].status, timing[0].bitrate, timing[0].data_bitrate) == ("ok", 500000, 2000000)
    assert (timing[1].status, timing[1].bitrate) == ("ok", 250000)

    # Re-applying unchanged timing does not send it again.
    await car.configure_buses()
    assert len(transport.bus_config_updates) == 2

    transport.script_response(
        CanFrame(can_id=0x7DF, data=bytes([0x02, 0x01, 0x0C, 0, 0, 0, 0, 0])),
        bytes([0x41, 0x0C, 0x1A, 0xF8]),
    )
    assert await car.query("RPM") == bytes([0x1A, 0xF8])
    rpm = transport.requests[0]
    assert rpm.bus is None  # endpoint omits bus -> bus 0
    assert rpm.tx_frame.dlc == 8
    assert rpm.can_fd is not None
    assert rpm.can_fd.tx_dl == 64
    assert rpm.can_fd.payload == bytes([0x01, 0x0C])

    await car.action("WAKE_BODY")
    body = transport.requests[1]
    assert body.bus == 1
    assert body.tx_frame.bus == 1
    assert body.tx_frame.extended is False
    assert body.can_fd is None


async def test_auto_detect_failure_is_tolerated_but_errors_fail_connect():
    manager = VirtualVehicleManager()
    transport = FixtureTransport()
    transport.script_bus_config_response(1, ConfigureBusResponse(status="auto_detect_failed", bitrate=125000))
    car = await manager.connect(
        ConnectVehicleOptions(
            id="car-a",
            transport=transport,
            profiles=[VehicleProfileSource(name="fd/profile.yaml", content=FD_BUS_PROFILE)],
        )
    )
    assert car.bus_timing()[1].status == "auto_detect_failed"
    assert car.bus_timing()[1].bitrate == 125000

    failing = FixtureTransport()
    failing.script_bus_config_response(0, ConfigureBusResponse(status="error", message="unsupported"))
    with pytest.raises(VehicleConnectionError, match="Failed to configure CAN bus 0: error: unsupported"):
        await VirtualVehicleManager().connect(
            ConnectVehicleOptions(
                id="car-b",
                transport=failing,
                profiles=[VehicleProfileSource(name="fd/profile.yaml", content=FD_BUS_PROFILE)],
            )
        )
    assert failing.is_connected() is False


async def test_transport_without_configure_bus_is_left_on_default_timing():
    transport = FixtureTransport(supports_configure_bus=False)
    car = await VirtualVehicleManager().connect(
        ConnectVehicleOptions(
            id="car-a",
            transport=transport,
            profiles=[VehicleProfileSource(name="fd/profile.yaml", content=FD_BUS_PROFILE)],
        )
    )
    assert car.bus_timing() == {}
    assert transport.bus_config_updates == []


def test_tx_dl_64_requires_single_frame_pci():
    profile = load_profile(
        """
version: 1
can:
  buses:
    0:
      bitrate: 500000
      fd:
        isotp:
          tx_dl: 64
endpoints:
  obd:
    request_id: 0x7DF
    response_id: 0x7E8
queries:
  RAW:
    endpoint: obd
    send: [0x41, 0x0C]
"""
    )
    registry = CapabilityRegistry()
    registry.load([profile])
    endpoint = registry.resolve_endpoint("obd")
    frame = build_request_frame(endpoint, profile.queries[0].send)
    with pytest.raises(ValueError, match="ISO-TP single-frame PCI"):
        can_fd_request(endpoint.can_fd, frame, "Endpoint obd")


MULTI_STEP_PROFILE = """
version: 1
endpoints:
  body:
    request_id: 0x745
    response_id: 0x765
    timeout_ms: 500
  engine:
    bus: 1
    request_id: 0x7E0
    response_id: 0x7E8
sequences:
  prep:
    steps:
      - send: [0x02, 0x10, 0x81, 0, 0, 0, 0, 0]
        expect: [0x50, 0x81]
        delay_ms: 30
      - send: [0x02, 0x10, 0xC0, 0, 0, 0, 0, 0]
        expect: [0x50, 0xC0]
actions:
  HORN:
    endpoint: body
    steps:
      - ref: sequences.prep
      - send: [0x04, 0x30, 0x30, 0x00, 0x01, 0, 0, 0]
        expect: [0x70, 0x30, 0x01]
  HORN_SEQUENTIAL:
    endpoint: body
    batch: false
    steps:
      - ref: sequences.prep
      - send: [0x04, 0x30, 0x30, 0x00, 0x01, 0, 0, 0]
        expect: [0x70, 0x30, 0x01]
  TWO_BUSES:
    endpoint: body
    steps:
      - send: [0x02, 0x10, 0x81, 0, 0, 0, 0, 0]
        delay_ms: 0
      - send: [0x02, 0x10, 0xC0, 0, 0, 0, 0, 0]
        delay_ms: 0
      - endpoint: engine
        send: [0x02, 0x3E, 0x00, 0, 0, 0, 0, 0]
        delay_ms: 0
      - endpoint: engine
        send: [0x02, 0x3E, 0x00, 0, 0, 0, 0, 0]
        delay_ms: 0
"""

SESSION_81 = bytes([0x02, 0x10, 0x81, 0, 0, 0, 0, 0])
SESSION_C0 = bytes([0x02, 0x10, 0xC0, 0, 0, 0, 0, 0])
HORN_ON = bytes([0x04, 0x30, 0x30, 0x00, 0x01, 0, 0, 0])


def script_horn_responses(transport: FixtureTransport) -> None:
    transport.script_response(CanFrame(can_id=0x745, data=SESSION_81), bytes([0x50, 0x81]))
    transport.script_response(CanFrame(can_id=0x745, data=SESSION_C0), bytes([0x50, 0xC0]))
    transport.script_response(CanFrame(can_id=0x745, data=HORN_ON), bytes([0x70, 0x30, 0x01]))


async def connect_multi_step(transport: FixtureTransport):
    return await VirtualVehicleManager().connect(
        ConnectVehicleOptions(
            id="car-a",
            transport=transport,
            profiles=[VehicleProfileSource(name="multi/profile.yaml", content=MULTI_STEP_PROFILE)],
        )
    )


async def test_multi_step_action_is_sent_as_one_batch_with_step_delays():
    transport = FixtureTransport()
    script_horn_responses(transport)
    car = await connect_multi_step(transport)

    assert await car.action("HORN") is True
    assert len(transport.batches) == 1
    batch = transport.batches[0]
    assert [bytes(step.request.tx_frame.data) for step in batch.steps] == [SESSION_81, SESSION_C0, HORN_ON]
    assert [step.delay_ms for step in batch.steps] == [30, 20, 20]
    assert all(step.request.timeout_ms == 500 for step in batch.steps)
    assert all(step.request.response_id_start == 0x765 for step in batch.steps)
    assert [req.signal_name for req in transport.requests] == ["HORN"] * 3


async def test_batch_false_sends_steps_one_at_a_time_and_honours_delay_ms():
    transport = FixtureTransport()
    script_horn_responses(transport)
    car = await connect_multi_step(transport)

    loop = asyncio.get_running_loop()
    started = loop.time()
    assert await car.action("HORN_SEQUENTIAL") is True
    elapsed_ms = (loop.time() - started) * 1000
    assert transport.batches == []
    assert len(transport.requests) == 3
    # 30 ms after the first step + 20 ms after the second (default delay).
    assert elapsed_ms >= 45


async def test_transport_without_batch_support_falls_back_to_sequential_requests():
    transport = FixtureTransport(supports_batch=False)
    script_horn_responses(transport)
    car = await connect_multi_step(transport)

    assert await car.action("HORN") is True
    assert transport.batches == []
    assert len(transport.requests) == 3


async def test_mismatched_batched_response_reports_failure():
    transport = FixtureTransport()
    script_horn_responses(transport)
    transport.script_response(CanFrame(can_id=0x745, data=SESSION_C0), bytes([0x7F, 0x10, 0x12]))
    car = await connect_multi_step(transport)

    assert await car.action("HORN") is False


async def test_steps_are_grouped_into_consecutive_same_bus_batches():
    transport = FixtureTransport()
    car = await connect_multi_step(transport)

    assert await car.action("TWO_BUSES") is None
    assert [[step.request.bus for step in batch.steps] for batch in transport.batches] == [[None, None], [1, 1]]
    assert [req.tx_frame.can_id for req in transport.requests] == [0x745, 0x745, 0x7E0, 0x7E0]
