from __future__ import annotations

import asyncio
import importlib
from contextlib import suppress
from dataclasses import dataclass
from typing import Any, Callable

from can_opener.dbc.types import CanFrame

from .types import (
    MonitorControlRequest,
    MonitorControlResponse,
    MonitorSnapshot,
    SnapshotCallback,
    VehicleRequest,
)

MessageFactory = Callable[..., Any]


class PythonCanRequestTimeout(TimeoutError):
    def __init__(self, req: VehicleRequest) -> None:
        start = req.response_id_start
        end = req.response_id_end if req.response_id_end is not None else start
        response_range = "any CAN ID" if start is None else f"0x{start:x}-0x{end:x}"
        super().__init__(f"Timed out waiting for CAN response on {response_range}")


@dataclass(slots=True)
class _PendingRequest:
    start: int | None
    end: int | None
    future: asyncio.Future[bytes]


class PythonCanTransport:
    def __init__(
        self,
        interface: str = "slcan",
        channel: str | None = None,
        bitrate: int | None = None,
        *,
        bus: Any | None = None,
        message_factory: MessageFactory | None = None,
        receive_timeout: float = 0.1,
    ) -> None:
        self.interface = interface
        self.channel = channel
        self.bitrate = bitrate
        self.receive_timeout = receive_timeout
        self._bus = bus
        self._owns_bus = bus is None
        self._message_factory = message_factory
        self._reader_task: asyncio.Task[None] | None = None
        self._pending: list[_PendingRequest] = []
        self._snapshot_callbacks: set[SnapshotCallback] = set()
        self._monitor_can_ids: set[int] = set()
        self._snapshot_sequence = 0
        self._connected = False

    async def connect(self) -> None:
        if self._connected:
            return

        if self._bus is None or self._message_factory is None:
            can_module = _load_python_can()
            if self._message_factory is None:
                self._message_factory = can_module.Message
            if self._bus is None:
                kwargs: dict[str, Any] = {"interface": self.interface}
                if self.channel is not None:
                    kwargs["channel"] = self.channel
                if self.bitrate is not None:
                    kwargs["bitrate"] = self.bitrate
                self._bus = can_module.Bus(**kwargs)

        self._connected = True
        self._reader_task = asyncio.create_task(self._read_loop())

    async def disconnect(self) -> None:
        if not self._connected:
            return

        self._connected = False
        if self._reader_task is not None:
            self._reader_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._reader_task
            self._reader_task = None

        self._fail_pending(ConnectionError("PythonCanTransport disconnected"))
        self._pending.clear()
        self._snapshot_callbacks.clear()
        self._monitor_can_ids.clear()

        if self._owns_bus and self._bus is not None and hasattr(self._bus, "shutdown"):
            await asyncio.to_thread(self._bus.shutdown)
        self._bus = None if self._owns_bus else self._bus

    async def send_request(self, req: VehicleRequest) -> bytes | None:
        bus = self._require_bus()
        message = self._build_message(req.tx_frame)
        pending: _PendingRequest | None = None

        if req.expect_can_response:
            future: asyncio.Future[bytes] = asyncio.get_running_loop().create_future()
            pending = _PendingRequest(
                start=req.response_id_start,
                end=req.response_id_end,
                future=future,
            )
            self._pending.append(pending)

        try:
            await asyncio.to_thread(bus.send, message)
            if pending is None:
                return None
            timeout = (req.timeout_ms if req.timeout_ms is not None else 1000) / 1000
            try:
                return await asyncio.wait_for(pending.future, timeout=timeout)
            except asyncio.TimeoutError as exc:
                raise PythonCanRequestTimeout(req) from exc
        finally:
            if pending is not None and pending in self._pending:
                self._pending.remove(pending)

    async def update_monitor(self, req: MonitorControlRequest) -> MonitorControlResponse:
        self._require_bus()
        invalid_can_id = next(
            (
                can_id
                for can_id in req.can_ids
                if not isinstance(can_id, int) or can_id < 0 or can_id > 0x1FFFFFFF
            ),
            None,
        )
        if invalid_can_id is not None:
            return MonitorControlResponse(
                status="invalid_can_id",
                current_monitor_count=len(self._monitor_can_ids),
            )

        if req.operation == "clear":
            self._monitor_can_ids.clear()
        elif req.operation == "add":
            self._monitor_can_ids.update(req.can_ids)
        elif req.operation == "remove":
            for can_id in req.can_ids:
                self._monitor_can_ids.discard(can_id)
        else:
            return MonitorControlResponse(
                status="invalid_opcode",
                current_monitor_count=len(self._monitor_can_ids),
            )

        return MonitorControlResponse(status="ok", current_monitor_count=len(self._monitor_can_ids))

    def on_monitor_snapshot(self, cb: SnapshotCallback):
        self._snapshot_callbacks.add(cb)

        def dispose() -> None:
            self._snapshot_callbacks.discard(cb)

        return dispose

    async def _read_loop(self) -> None:
        bus = self._require_bus()
        while self._connected:
            message = await asyncio.to_thread(bus.recv, self.receive_timeout)
            if message is None:
                continue
            self._handle_message(message)

    def _handle_message(self, message: Any) -> None:
        frame = _frame_from_message(message)
        pending = next((item for item in self._pending if _matches_pending(item, frame.can_id)), None)
        if pending is not None and not pending.future.done():
            pending.future.set_result(bytes(frame.data))

        if frame.can_id not in self._monitor_can_ids:
            return
        snapshot = MonitorSnapshot(sequence=self._snapshot_sequence, frames=[frame])
        self._snapshot_sequence = (self._snapshot_sequence + 1) & 0xFFFF
        for callback in list(self._snapshot_callbacks):
            callback(snapshot)

    def _build_message(self, frame: CanFrame) -> Any:
        if self._message_factory is None:
            raise ConnectionError("PythonCanTransport is not connected")
        data = _frame_payload(frame)
        return self._message_factory(
            arbitration_id=frame.can_id,
            data=data,
            is_extended_id=bool(frame.extended),
        )

    def _require_bus(self) -> Any:
        if not self._connected or self._bus is None:
            raise ConnectionError("PythonCanTransport is not connected")
        return self._bus

    def _fail_pending(self, exc: Exception) -> None:
        for pending in self._pending:
            if not pending.future.done():
                pending.future.set_exception(exc)


def _load_python_can() -> Any:
    try:
        return importlib.import_module("can")
    except ImportError as exc:
        raise ImportError("Install python-can to use PythonCanTransport") from exc


def _frame_payload(frame: CanFrame) -> bytes:
    data = bytes(frame.data)
    if frame.dlc is None:
        return data
    if len(data) >= frame.dlc:
        return data[: frame.dlc]
    return data + bytes(frame.dlc - len(data))


def _frame_from_message(message: Any) -> CanFrame:
    return CanFrame(
        can_id=message.arbitration_id,
        data=bytes(message.data),
        dlc=getattr(message, "dlc", len(message.data)),
        extended=getattr(message, "is_extended_id", False),
    )


def _matches_pending(pending: _PendingRequest, can_id: int) -> bool:
    if pending.start is None:
        return True
    end = pending.end if pending.end is not None else pending.start
    return pending.start <= can_id <= end
