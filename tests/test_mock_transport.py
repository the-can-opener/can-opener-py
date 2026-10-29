import pytest

from can_opener.dbc.types import CanFrame
from can_opener.transport.mock import MAX_MONITOR_IDS, MockTransport
from can_opener.transport.types import (
    BatchRequest,
    BatchStep,
    ConfigureBusRequest,
    ConfigureBusResponse,
    MonitorControlRequest,
    MonitorSnapshot,
    VehicleRequest,
    transport_supports_batch,
    transport_supports_configure_bus,
)


def request(can_id: int, data: bytes, *, expect: bool = True, bus: int | None = None) -> VehicleRequest:
    return VehicleRequest(
        tx_frame=CanFrame(can_id=can_id, data=data, dlc=len(data), bus=bus),
        expect_can_response=expect,
        bus=bus,
    )


async def test_requires_connect_before_use():
    transport = MockTransport()
    with pytest.raises(ValueError, match="not connected"):
        await transport.send_request(request(0x7DF, b"\x01\x0c", expect=False))
    await transport.connect()
    assert transport.is_connected()
    await transport.disconnect()
    assert not transport.is_connected()


async def test_scripted_responses_are_keyed_by_bus_id_and_bytes():
    transport = MockTransport()
    await transport.connect()
    transport.script_response(CanFrame(can_id=0x7DF, data=b"\x01\x0c"), b"\x41\x0c\x10")
    transport.script_response(CanFrame(can_id=0x7DF, data=b"\x01\x0c", bus=1), lambda req: b"\x41\x0c\x20")

    assert await transport.send_request(request(0x7DF, b"\x01\x0c")) == b"\x41\x0c\x10"
    assert await transport.send_request(request(0x7DF, b"\x01\x0c", bus=1)) == b"\x41\x0c\x20"
    assert await transport.send_request(request(0x7DF, b"\x01\x0c", expect=False)) is None
    with pytest.raises(ValueError, match="No mock response registered for CAN0 ID 2015"):
        await transport.send_request(request(0x7DF, b"\x01\x0d"))

    transport.script_responder(lambda req: bytes([req.tx_frame.can_id & 0xFF]))
    assert await transport.send_request(request(0x7DF, b"\x01\x0d")) == b"\xdf"

    # Every send is recorded, including the one that had no scripted response.
    assert len(transport.requests) == 5
    assert transport.requests[1].bus == 1


async def test_batches_are_recorded_and_report_per_step_results():
    transport = MockTransport()
    await transport.connect()
    transport.script_response(CanFrame(can_id=0x745, data=b"\x02\x10\x81"), b"\x50\x81")

    results = await transport.send_request_batch(
        BatchRequest(
            steps=[
                BatchStep(request=request(0x745, b"\x02\x10\x81"), delay_ms=20),
                BatchStep(request=request(0x745, b"\x02\x10\xc0"), delay_ms=0),
                BatchStep(request=request(0x745, b"\x02\x3e\x00", expect=False), delay_ms=0),
            ]
        )
    )
    assert [result.ok for result in results] == [True, False, False]
    assert results[0].response == b"\x50\x81"
    assert results[1].error is not None and "No mock response" in results[1].error
    assert results[2].error == "skipped"
    assert len(transport.batches) == 1
    assert [step.delay_ms for step in transport.batches[0].steps] == [20, 0, 0]
    # Only executed steps are recorded as requests.
    assert len(transport.requests) == 2

    with pytest.raises(ValueError, match="does not match batch bus"):
        await transport.send_request_batch(
            BatchRequest(
                steps=[
                    BatchStep(request=request(0x745, b"\x01", expect=False), delay_ms=0),
                    BatchStep(request=request(0x745, b"\x01", expect=False, bus=1), delay_ms=0),
                ]
            )
        )


async def test_capability_flags_hide_optional_methods():
    assert transport_supports_batch(MockTransport())
    assert transport_supports_configure_bus(MockTransport())
    assert not transport_supports_batch(MockTransport(supports_batch=False))
    assert not transport_supports_configure_bus(MockTransport(supports_configure_bus=False))


async def test_configure_bus_records_requests_and_returns_scripted_status():
    transport = MockTransport()
    await transport.connect()
    transport.script_bus_config_response(1, ConfigureBusResponse(status="auto_detect_failed", bitrate=125000))

    ok = await transport.configure_bus(ConfigureBusRequest(bus=0, bitrate=500000, data_bitrate=2000000))
    assert (ok.status, ok.bitrate, ok.data_bitrate) == ("ok", 500000, 2000000)

    auto = await transport.configure_bus(ConfigureBusRequest(bus=1, bitrate="auto"))
    assert (auto.status, auto.bitrate) == ("auto_detect_failed", 125000)
    # Scripted responses are consumed once.
    again = await transport.configure_bus(ConfigureBusRequest(bus=1, bitrate="auto"))
    assert (again.status, again.bitrate) == ("ok", None)

    bad = await transport.configure_bus(ConfigureBusRequest(bus=300, bitrate=500000))
    assert bad.status == "error"
    assert [req.bus for req in transport.bus_config_updates] == [0, 1, 1, 300]


async def test_monitor_tables_are_per_bus_and_emit_frame_filters_by_them():
    transport = MockTransport()
    await transport.connect()
    snapshots: list[MonitorSnapshot] = []
    dispose = transport.on_monitor_snapshot(snapshots.append)

    assert (await transport.update_monitor(MonitorControlRequest("add", [0x100, 0x101]))).status == "ok"
    assert (await transport.update_monitor(MonitorControlRequest("add", [0x100], bus=1))).status == "ok"
    assert (await transport.update_monitor(MonitorControlRequest("add", [0x100]))).status == "duplicate_id"
    assert (await transport.update_monitor(MonitorControlRequest("add", [0x20000000]))).status == "invalid_can_id"
    full = await transport.update_monitor(MonitorControlRequest("add", list(range(0x200, 0x200 + MAX_MONITOR_IDS))))
    assert full.status == "monitor_full"
    assert transport.monitor_can_ids == {0x100, 0x101}

    transport.emit_frame(CanFrame(can_id=0x300, data=b"\x01"))  # not monitored
    assert snapshots == []
    transport.emit_frame(CanFrame(can_id=0x100, data=b"\x01"))
    transport.emit_frame(CanFrame(can_id=0x100, data=b"\x02", bus=1))
    assert len(snapshots) == 2
    assert [(frame.bus, bytes(frame.data)) for frame in snapshots[1].frames] == [(0, b"\x01"), (1, b"\x02")]
    assert snapshots[1].sequence == 1

    removed = await transport.update_monitor(MonitorControlRequest("remove", [0x100], bus=1))
    assert (removed.status, removed.current_monitor_count) == ("ok", 0)
    cleared = await transport.update_monitor(MonitorControlRequest("clear"))
    assert (cleared.status, cleared.current_monitor_count) == ("ok", 0)
    assert transport.monitor_can_ids == set()
    assert [update.operation for update in transport.monitor_updates][-2:] == ["remove", "clear"]

    transport.emit_snapshot([CanFrame(can_id=0x999, data=b"\xff")])
    assert snapshots[-1].frames[0].can_id == 0x999

    dispose()
    transport.emit_snapshot([CanFrame(can_id=0x999, data=b"\xff")])
    assert len(snapshots) == 3
