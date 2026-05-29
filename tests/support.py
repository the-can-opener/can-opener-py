from __future__ import annotations

from can_opener.dbc.types import CanFrame
from can_opener.transport.types import (
    MonitorControlRequest,
    MonitorControlResponse,
    MonitorSnapshot,
    SnapshotCallback,
    VehicleRequest,
)

MAX_MONITOR_IDS = 16


class FixtureTransport:
    def __init__(self) -> None:
        self.monitor_can_ids: set[int] = set()
        self.requests: list[VehicleRequest] = []
        self.monitor_updates: list[MonitorControlRequest] = []
        self._callbacks: set[SnapshotCallback] = set()
        self._responses: dict[str, bytes] = {}
        self._monitor_frames: dict[int, CanFrame] = {}
        self._connected = False
        self._snapshot_sequence = 0

    async def connect(self) -> None:
        self._connected = True

    async def disconnect(self) -> None:
        self._connected = False
        self._callbacks.clear()
        self.monitor_can_ids.clear()
        self._monitor_frames.clear()

    async def send_request(self, req: VehicleRequest) -> bytes | None:
        self._assert_connected()
        self.requests.append(_clone_request(req))
        if not req.expect_can_response:
            return None
        response = self._responses.get(_frame_key(req.tx_frame))
        if response is None:
            raise ValueError(f"No test response registered for CAN ID {req.tx_frame.can_id}")
        return bytes(response)

    async def update_monitor(self, req: MonitorControlRequest) -> MonitorControlResponse:
        self._assert_connected()
        self.monitor_updates.append(MonitorControlRequest(req.operation, list(req.can_ids)))

        if req.operation == "clear":
            self.monitor_can_ids.clear()
            self._monitor_frames.clear()
            return self._monitor_response("ok")

        invalid_can_id = next(
            (can_id for can_id in req.can_ids if not isinstance(can_id, int) or can_id < 0 or can_id > 0x1FFFFFFF),
            None,
        )
        if invalid_can_id is not None:
            return self._monitor_response("invalid_can_id")

        if req.operation == "remove":
            for can_id in req.can_ids:
                self.monitor_can_ids.discard(can_id)
                self._monitor_frames.pop(can_id, None)
            return self._monitor_response("ok")

        unique_can_ids = set(req.can_ids)
        if len(unique_can_ids) != len(req.can_ids) or any(can_id in self.monitor_can_ids for can_id in req.can_ids):
            return self._monitor_response("duplicate_id")

        if len(self.monitor_can_ids) + len(unique_can_ids) > MAX_MONITOR_IDS:
            return self._monitor_response("monitor_full")

        self.monitor_can_ids.update(unique_can_ids)
        return self._monitor_response("ok")

    def on_monitor_snapshot(self, cb: SnapshotCallback):
        self._callbacks.add(cb)

        def dispose() -> None:
            self._callbacks.discard(cb)

        return dispose

    def script_response(self, frame: CanFrame, payload: bytes | bytearray) -> None:
        self._responses[_frame_key(frame)] = bytes(payload)

    def emit_frame(self, frame: CanFrame) -> None:
        if frame.can_id not in self.monitor_can_ids:
            return
        self._monitor_frames[frame.can_id] = frame.clone()
        snapshot = MonitorSnapshot(
            sequence=self._snapshot_sequence,
            frames=[stored.clone() for stored in self._monitor_frames.values()],
        )
        self._snapshot_sequence = (self._snapshot_sequence + 1) & 0xFFFF
        for callback in list(self._callbacks):
            callback(snapshot)

    def _assert_connected(self) -> None:
        if not self._connected:
            raise ValueError("FixtureTransport is not connected")

    def _monitor_response(self, status) -> MonitorControlResponse:
        return MonitorControlResponse(status=status, current_monitor_count=len(self.monitor_can_ids))


def _clone_request(req: VehicleRequest) -> VehicleRequest:
    return VehicleRequest(
        signal_name=req.signal_name,
        tx_frame=req.tx_frame.clone(),
        expect_can_response=req.expect_can_response,
        notify_tx_status=req.notify_tx_status,
        response_id_start=req.response_id_start,
        response_id_end=req.response_id_end,
        timeout_ms=req.timeout_ms,
        diagnostic=req.diagnostic,
        action=req.action,
    )


def _frame_key(frame: CanFrame) -> str:
    return f"{frame.can_id}:{bytes(frame.data).hex()}"
