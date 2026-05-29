from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from can_opener.dbc.types import CanFrame

from .types import (
    MonitorControlRequest,
    MonitorControlResponse,
    MonitorControlStatus,
    MonitorSnapshot,
    SnapshotCallback,
    VehicleRequest,
)

CANOPENER_SERVICE_UUID = "00000180-0000-1000-8000-00805f9b34fb"
REQUEST_CHAR_UUID = "0000fef4-0000-1000-8000-00805f9b34fb"
MONITOR_CONTROL_CHAR_UUID = "0000a003-0000-1000-8000-00805f9b34fb"
MONITOR_DATA_CHAR_UUID = "0000a004-0000-1000-8000-00805f9b34fb"
DEFAULT_DEVICE_NAME = "CanOpener"

REQ_FLAG_EXPECT_CAN_RESPONSE = 0x01
REQ_FLAG_TX_CAN_ID_EXTENDED = 0x02
REQ_FLAG_RESPONSE_ID_EXTENDED = 0x04
RESP_FLAG_RESPONSE_CAN_ID_EXTENDED = 0x01

MONITOR_OPCODE_ADD = 0x01
MONITOR_OPCODE_REMOVE = 0x02
MONITOR_OPCODE_CLEAR = 0x03
MONITOR_CONFIG_RESPONSE_OPCODE = 0x80
MONITOR_DATA_OPCODE = 0x81

MONITOR_STATUS_BY_CODE: dict[int, MonitorControlStatus] = {
    0x00: "ok",
    0x01: "invalid_opcode",
    0x02: "invalid_length",
    0x03: "monitor_full",
    0x04: "duplicate_id",
    0x05: "invalid_can_id",
    0x06: "internal_error",
}

REQUEST_STATUS_BY_CODE = {
    0x00: "ok",
    0xE1: "bad_len",
    0xE2: "bad_dlc",
    0xE3: "can_tx_fail",
    0xE4: "can_rx_timeout",
    0xE5: "isotp_invalid",
    0xE6: "isotp_overflow",
    0xE7: "invalid_can_id",
}


class BleRequestError(Exception):
    def __init__(self, status_code: int, status: str) -> None:
        super().__init__(f"BLE request failed with status {status} (0x{status_code:02x})")
        self.status_code = status_code
        self.status = status


@dataclass(frozen=True, slots=True)
class _RequestResponse:
    seq: int
    status_code: int
    response_can_id: int
    response_can_id_extended: bool
    payload: bytes


class BleTransport:
    def __init__(
        self,
        address: str | None = None,
        *,
        name: str = DEFAULT_DEVICE_NAME,
        service_uuid: str = CANOPENER_SERVICE_UUID,
        request_uuid: str = REQUEST_CHAR_UUID,
        monitor_control_uuid: str = MONITOR_CONTROL_CHAR_UUID,
        monitor_data_uuid: str = MONITOR_DATA_CHAR_UUID,
        timeout: float = 10.0,
    ) -> None:
        self.address = address
        self.name = name
        self.service_uuid = service_uuid
        self.request_uuid = request_uuid
        self.monitor_control_uuid = monitor_control_uuid
        self.monitor_data_uuid = monitor_data_uuid
        self.timeout = timeout
        self._client: Any | None = None
        self._seq = 0
        self._request_waiters: dict[int, asyncio.Future[_RequestResponse]] = {}
        self._monitor_waiters: dict[int, asyncio.Future[MonitorControlResponse]] = {}
        self._snapshot_callbacks: set[SnapshotCallback] = set()
        self._snapshot_sequence = 0

    async def connect(self) -> None:
        from bleak import BleakClient, BleakScanner

        target = self.address
        if target is None:
            device = await BleakScanner.find_device_by_filter(
                lambda d, _: d.name == self.name,
                timeout=self.timeout,
            )
            if device is None:
                raise ConnectionError(f"BLE device named {self.name!r} not found")
            target = device.address

        self._client = BleakClient(target, timeout=self.timeout)
        await self._client.connect()
        await self._client.start_notify(self.request_uuid, self._handle_request_notify)
        await self._client.start_notify(self.monitor_control_uuid, self._handle_monitor_control_notify)
        await self._client.start_notify(self.monitor_data_uuid, self._handle_monitor_data_notify)

    async def disconnect(self) -> None:
        if self._client is None:
            return
        try:
            if self._client.is_connected:
                await self._client.disconnect()
        finally:
            self._client = None
            self._request_waiters.clear()
            self._monitor_waiters.clear()
            self._snapshot_callbacks.clear()

    async def send_request(self, req: VehicleRequest) -> bytes | None:
        client = self._require_client()
        seq = self._next_seq()
        packet = build_request_packet(seq, req)
        future: asyncio.Future[_RequestResponse] | None = None
        if req.expect_can_response:
            future = asyncio.get_running_loop().create_future()
            self._request_waiters[seq] = future
        await client.write_gatt_char(self.request_uuid, packet, response=True)
        if future is None:
            return None
        try:
            response = await asyncio.wait_for(future, timeout=(req.timeout_ms or int(self.timeout * 1000)) / 1000 + 1)
        finally:
            self._request_waiters.pop(seq, None)
        if response.status_code != 0:
            status = REQUEST_STATUS_BY_CODE.get(response.status_code, "unknown")
            raise BleRequestError(response.status_code, status)
        return response.payload

    async def update_monitor(self, req: MonitorControlRequest) -> MonitorControlResponse:
        client = self._require_client()
        seq = self._next_seq()
        packet = build_monitor_control_packet(seq, req)
        future: asyncio.Future[MonitorControlResponse] = asyncio.get_running_loop().create_future()
        self._monitor_waiters[seq] = future
        await client.write_gatt_char(self.monitor_control_uuid, packet, response=True)
        try:
            return await asyncio.wait_for(future, timeout=self.timeout)
        finally:
            self._monitor_waiters.pop(seq, None)

    def on_monitor_snapshot(self, cb: SnapshotCallback):
        self._snapshot_callbacks.add(cb)

        def dispose() -> None:
            self._snapshot_callbacks.discard(cb)

        return dispose

    def _handle_request_notify(self, _: Any, data: bytearray) -> None:
        response = parse_request_response(bytes(data))
        future = self._request_waiters.get(response.seq)
        if future is not None and not future.done():
            future.set_result(response)

    def _handle_monitor_control_notify(self, _: Any, data: bytearray) -> None:
        seq, response = parse_monitor_control_response(bytes(data))
        future = self._monitor_waiters.get(seq)
        if future is not None and not future.done():
            future.set_result(response)

    def _handle_monitor_data_notify(self, _: Any, data: bytearray) -> None:
        snapshot = parse_monitor_data(bytes(data), self._snapshot_sequence)
        self._snapshot_sequence = (self._snapshot_sequence + 1) & 0xFFFF
        for callback in list(self._snapshot_callbacks):
            callback(snapshot)

    def _next_seq(self) -> int:
        seq = self._seq
        self._seq = (self._seq + 1) & 0xFF
        return seq

    def _require_client(self):
        if self._client is None or not self._client.is_connected:
            raise ConnectionError("BleTransport is not connected")
        return self._client


def build_request_packet(seq: int, req: VehicleRequest) -> bytes:
    flags = 0
    if req.expect_can_response:
        flags |= REQ_FLAG_EXPECT_CAN_RESPONSE
    if req.tx_frame.extended:
        flags |= REQ_FLAG_TX_CAN_ID_EXTENDED
    response_extended = req.response_id_end is not None and req.response_id_end > 0x7FF
    if response_extended:
        flags |= REQ_FLAG_RESPONSE_ID_EXTENDED

    tx_id = req.tx_frame.can_id.to_bytes(4, "little")
    payload = _payload8(req.tx_frame.data)
    dlc = req.tx_frame.dlc if req.tx_frame.dlc is not None else min(len(req.tx_frame.data), 8)

    if req.expect_can_response:
        response_id_start = req.response_id_start if req.response_id_start is not None else 0
        response_id_end = req.response_id_end if req.response_id_end is not None else response_id_start
        timeout_ms = req.timeout_ms if req.timeout_ms is not None else 1000
        return bytes([seq, flags]) + tx_id + response_id_start.to_bytes(4, "little") + response_id_end.to_bytes(4, "little") + timeout_ms.to_bytes(2, "little") + bytes([dlc]) + payload

    return bytes([seq, flags]) + tx_id + bytes([dlc]) + payload


def build_monitor_control_packet(seq: int, req: MonitorControlRequest) -> bytes:
    if req.operation == "clear":
        return bytes([MONITOR_OPCODE_CLEAR, seq])
    opcode = MONITOR_OPCODE_ADD if req.operation == "add" else MONITOR_OPCODE_REMOVE
    body = b"".join(can_id.to_bytes(4, "little") for can_id in req.can_ids)
    return bytes([opcode, seq, len(req.can_ids)]) + body


def parse_request_response(data: bytes) -> _RequestResponse:
    if len(data) < 8:
        raise ValueError("Request response notify packet must be at least 8 bytes")
    seq = data[0]
    status_code = data[1]
    flags = data[2]
    response_can_id = int.from_bytes(data[3:7], "little")
    payload_len = data[7]
    payload = data[8 : 8 + payload_len]
    if len(payload) != payload_len:
        raise ValueError("Request response payload length does not match packet")
    return _RequestResponse(
        seq=seq,
        status_code=status_code,
        response_can_id=response_can_id,
        response_can_id_extended=bool(flags & RESP_FLAG_RESPONSE_CAN_ID_EXTENDED),
        payload=payload,
    )


def parse_monitor_control_response(data: bytes) -> tuple[int, MonitorControlResponse]:
    if len(data) != 4 or data[0] != MONITOR_CONFIG_RESPONSE_OPCODE:
        raise ValueError("Invalid monitor control response packet")
    seq = data[1]
    status = MONITOR_STATUS_BY_CODE.get(data[2], "internal_error")
    return seq, MonitorControlResponse(status=status, current_monitor_count=data[3])


def parse_monitor_data(data: bytes, sequence: int = 0) -> MonitorSnapshot:
    if len(data) < 2 or data[0] != MONITOR_DATA_OPCODE:
        raise ValueError("Invalid monitor data packet")
    frame_count = data[1]
    expected_len = 2 + frame_count * 13
    if len(data) != expected_len:
        raise ValueError("Monitor data packet length does not match frame count")
    frames: list[CanFrame] = []
    offset = 2
    for _ in range(frame_count):
        can_id = int.from_bytes(data[offset : offset + 4], "little")
        dlc = data[offset + 4]
        payload = bytes(data[offset + 5 : offset + 13])
        frames.append(CanFrame(can_id=can_id, dlc=dlc, data=payload))
        offset += 13
    return MonitorSnapshot(sequence=sequence, frames=frames)


def _payload8(data: bytes | bytearray) -> bytes:
    payload = bytearray(8)
    payload[: min(len(data), 8)] = data[:8]
    return bytes(payload)
