import asyncio
import queue

from can_opener.dbc.types import CanFrame
from can_opener.transport.ble import (
    build_monitor_control_packet,
    build_request_packet,
    parse_monitor_control_response,
    parse_monitor_data,
    parse_request_response,
)
from can_opener.transport.python_can import PythonCanTransport
from can_opener.transport.types import (
    BatchRequest,
    BatchStep,
    CanFdRequest,
    MonitorControlRequest,
    VehicleRequest,
)


def test_ble_request_packet_uses_firmware_layouts():
    request_only = build_request_packet(
        7,
        VehicleRequest(
            tx_frame=CanFrame(can_id=0x123, dlc=2, data=bytes([1, 2])),
            expect_can_response=False,
        ),
    )
    assert request_only == bytes([7, 0, 0x23, 0x01, 0, 0, 2, 1, 2, 0, 0, 0, 0, 0, 0])

    with_response = build_request_packet(
        8,
        VehicleRequest(
            tx_frame=CanFrame(can_id=0x7DF, dlc=2, data=bytes([1, 0x0C])),
            expect_can_response=True,
            response_id_start=0x7E8,
            response_id_end=0x7EF,
            timeout_ms=500,
        ),
    )
    assert len(with_response) == 25
    assert with_response[:2] == bytes([8, 0x01])
    assert with_response[6:10] == (0x7E8).to_bytes(4, "little")
    assert with_response[10:14] == (0x7EF).to_bytes(4, "little")
    assert with_response[14:16] == (500).to_bytes(2, "little")
    assert with_response[16:] == bytes([2, 1, 0x0C, 0, 0, 0, 0, 0, 0])


def test_ble_request_response_parser_follows_firmware_extra_flags_byte():
    packet = bytes([5, 0, 1]) + (0x18DAF110).to_bytes(4, "little") + bytes([3, 0x41, 0x0C, 0x2E])

    response = parse_request_response(packet)

    assert response.seq == 5
    assert response.response_can_id == 0x18DAF110
    assert response.response_can_id_extended is True
    assert response.payload == bytes([0x41, 0x0C, 0x2E])


def test_ble_monitor_packets():
    assert build_monitor_control_packet(1, MonitorControlRequest("clear")) == bytes([0x03, 1])
    assert build_monitor_control_packet(2, MonitorControlRequest("add", [0x100, 0x101])) == (
        bytes([0x01, 2, 2])
        + (0x100).to_bytes(4, "little")
        + (0x101).to_bytes(4, "little")
    )

    seq, response = parse_monitor_control_response(bytes([0x80, 2, 0, 7]))
    assert seq == 2
    assert response.status == "ok"
    assert response.current_monitor_count == 7

    snapshot = parse_monitor_data(
        bytes([0x81, 1]) + (0x123).to_bytes(4, "little") + bytes([2, 1, 2, 0, 0, 0, 0, 0, 0]),
        sequence=9,
    )
    assert snapshot.sequence == 9
    assert snapshot.frames[0].can_id == 0x123
    assert snapshot.frames[0].dlc == 2
    assert snapshot.frames[0].data[:2] == bytes([1, 2])


class FakeCanMessage:
    def __init__(
        self,
        arbitration_id: int,
        data: bytes | bytearray,
        is_extended_id: bool = False,
    ) -> None:
        self.arbitration_id = arbitration_id
        self.data = bytes(data)
        self.is_extended_id = is_extended_id
        self.dlc = len(self.data)


class FakeCanBus:
    def __init__(self) -> None:
        self.sent: list[FakeCanMessage] = []
        self.incoming: queue.Queue[FakeCanMessage] = queue.Queue()
        self.shutdown_called = False

    def send(self, message: FakeCanMessage) -> None:
        self.sent.append(message)

    def recv(self, timeout: float) -> FakeCanMessage | None:
        try:
            return self.incoming.get(timeout=timeout)
        except queue.Empty:
            return None

    def inject(self, message: FakeCanMessage) -> None:
        self.incoming.put(message)

    def shutdown(self) -> None:
        self.shutdown_called = True


async def test_python_can_transport_sends_raw_can_frames():
    bus = FakeCanBus()
    transport = PythonCanTransport(
        bus=bus,
        message_factory=FakeCanMessage,
        receive_timeout=0.01,
    )
    await transport.connect()

    await transport.send_request(
        VehicleRequest(
            tx_frame=CanFrame(
                can_id=0x35D,
                data=b"\xc1\x03\x40\x00\x00\x00\x00\x00",
                dlc=8,
            ),
            expect_can_response=False,
        )
    )

    assert len(bus.sent) == 1
    assert bus.sent[0].arbitration_id == 0x35D
    assert bus.sent[0].data == b"\xc1\x03\x40\x00\x00\x00\x00\x00"
    assert bus.sent[0].is_extended_id is False
    await transport.disconnect()


async def test_python_can_transport_waits_for_response_range():
    bus = FakeCanBus()
    transport = PythonCanTransport(
        bus=bus,
        message_factory=FakeCanMessage,
        receive_timeout=0.01,
    )
    await transport.connect()

    request = asyncio.create_task(
        transport.send_request(
            VehicleRequest(
                tx_frame=CanFrame(can_id=0x7DF, data=b"\x01\x0c", dlc=2),
                expect_can_response=True,
                response_id_start=0x7E8,
                response_id_end=0x7EF,
                timeout_ms=500,
            )
        )
    )
    await asyncio.sleep(0)
    bus.inject(FakeCanMessage(0x7E8, b"\x03\x41\x0c\x2e\x00\x00\x00\x00"))

    assert await request == b"\x03\x41\x0c\x2e\x00\x00\x00\x00"
    assert bus.sent[0].arbitration_id == 0x7DF
    assert bus.sent[0].data == b"\x01\x0c"
    await transport.disconnect()


async def test_python_can_transport_emits_monitor_snapshots():
    bus = FakeCanBus()
    transport = PythonCanTransport(
        bus=bus,
        message_factory=FakeCanMessage,
        receive_timeout=0.01,
    )
    await transport.connect()
    future: asyncio.Future[CanFrame] = asyncio.get_running_loop().create_future()
    transport.on_monitor_snapshot(lambda snapshot: future.set_result(snapshot.frames[0]))

    response = await transport.update_monitor(MonitorControlRequest("add", [0x123]))
    bus.inject(FakeCanMessage(0x456, b"\x00"))
    bus.inject(FakeCanMessage(0x123, b"\x01\x02\x03"))

    frame = await asyncio.wait_for(future, timeout=1)
    assert response.status == "ok"
    assert response.current_monitor_count == 1
    assert frame.can_id == 0x123
    assert frame.data == b"\x01\x02\x03"
    await transport.disconnect()


def test_vehicle_request_carries_bus_and_can_fd():
    classic = VehicleRequest(tx_frame=CanFrame(can_id=0x7DF, data=b"\x02\x01\x0c"), expect_can_response=True)
    assert classic.bus is None and classic.can_fd is None

    fd = VehicleRequest(
        tx_frame=CanFrame(can_id=0x7DF, data=b"\x02\x01\x0c\x00\x00\x00\x00\x00", dlc=8, bus=1),
        expect_can_response=True,
        bus=1,
        can_fd=CanFdRequest(tx_dl=64, payload=b"\x01\x0c"),
    )
    assert fd.can_fd is not None and fd.can_fd.tx_dl == 64 and fd.can_fd.payload == b"\x01\x0c"
    assert fd.tx_frame.clone().bus == 1

    batch = BatchRequest(steps=[BatchStep(request=fd, delay_ms=20)])
    assert batch.steps[0].delay_ms == 20


def test_ble_request_packet_ignores_bus_and_fd_metadata():
    packet = build_request_packet(
        1,
        VehicleRequest(
            tx_frame=CanFrame(can_id=0x123, dlc=2, data=bytes([1, 2]), bus=1),
            expect_can_response=False,
            bus=1,
        ),
    )
    assert packet == bytes([1, 0, 0x23, 0x01, 0, 0, 2, 1, 2, 0, 0, 0, 0, 0, 0])
