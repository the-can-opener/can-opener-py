from __future__ import annotations

from dataclasses import replace
from typing import Callable

from can_opener.dbc.types import CanFrame

from .types import (
    BatchRequest,
    BatchStepResult,
    ConfigureBusRequest,
    ConfigureBusResponse,
    DisposeCallback,
    MonitorControlRequest,
    MonitorControlResponse,
    MonitorControlStatus,
    MonitorSnapshot,
    SnapshotCallback,
    VehicleRequest,
)

MAX_MONITOR_IDS = 16

# A scripted response: fixed bytes, or a callable deriving them from the request.
ScriptedResponse = bytes | bytearray | Callable[[VehicleRequest], "bytes | None"]
Responder = Callable[[VehicleRequest], "bytes | None"]


class MockTransport:
    """In-memory ``VehicleTransport`` for tests.

    Responses are scripted per transmitted frame (bus, CAN ID and data bytes)
    with :meth:`script_response`, or with a catch-all callable via
    :meth:`script_responder`. Every request is recorded in :attr:`requests`;
    batches also land in :attr:`batches`. Monitor frames can be injected with
    :meth:`emit_frame` (filtered by the monitored IDs) or pushed directly to
    snapshot callbacks with :meth:`emit_snapshot`.
    """

    def __init__(self, *, supports_batch: bool = True, supports_configure_bus: bool = True) -> None:
        # Aggregate compatibility view of monitored IDs across all buses.
        self.monitor_can_ids: set[int] = set()
        # Every step sent, whether individually or as part of a batch.
        self.requests: list[VehicleRequest] = []
        self.batches: list[BatchRequest] = []
        self.monitor_updates: list[MonitorControlRequest] = []
        self.bus_config_updates: list[ConfigureBusRequest] = []

        self._bus_config_responses: dict[int, ConfigureBusResponse] = {}
        self._callbacks: set[SnapshotCallback] = set()
        self._responses: dict[tuple[int, int, str], ScriptedResponse] = {}
        self._responder: Responder | None = None
        self._monitor_can_ids_by_bus: dict[int, set[int]] = {}
        self._monitor_frames: dict[tuple[int, int], CanFrame] = {}
        self._connected = False
        self._snapshot_sequence = 0

        # Optional capabilities are detected with ``callable(getattr(...))``, so
        # shadow the methods with ``None`` when the mock should lack them.
        if not supports_batch:
            self.send_request_batch = None  # type: ignore[method-assign]
        if not supports_configure_bus:
            self.configure_bus = None  # type: ignore[method-assign]

    # -- lifecycle -----------------------------------------------------------

    async def connect(self) -> None:
        self._connected = True

    async def disconnect(self) -> None:
        self._connected = False
        self._callbacks.clear()
        self.monitor_can_ids.clear()
        self._monitor_can_ids_by_bus.clear()
        self._monitor_frames.clear()

    def is_connected(self) -> bool:
        return self._connected

    # -- bus configuration ---------------------------------------------------

    def script_bus_config_response(self, bus: int, response: ConfigureBusResponse) -> None:
        """Makes the next ``configure_bus`` for ``bus`` return ``response``."""
        self._bus_config_responses[bus] = response

    async def configure_bus(self, req: ConfigureBusRequest) -> ConfigureBusResponse:
        self._assert_connected()
        self.bus_config_updates.append(req)
        scripted = self._bus_config_responses.pop(req.bus, None)
        if scripted is not None:
            return scripted
        if not _is_valid_bus(req.bus):
            return ConfigureBusResponse(status="error", message=f"invalid bus {req.bus}")
        if (req.bitrate != "auto" and not _is_valid_bitrate(req.bitrate)) or (
            req.data_bitrate is not None and not _is_valid_bitrate(req.data_bitrate)
        ):
            return ConfigureBusResponse(status="error", message="invalid bitrate")
        return ConfigureBusResponse(
            status="ok",
            bitrate=req.bitrate if isinstance(req.bitrate, int) else None,
            data_bitrate=req.data_bitrate,
        )

    # -- requests ------------------------------------------------------------

    def script_response(self, frame: CanFrame, response: ScriptedResponse) -> None:
        """Registers the response for requests whose tx frame matches ``frame``."""
        self._responses[_frame_key(frame)] = response if callable(response) else bytes(response)

    def script_responder(self, responder: Responder | None) -> None:
        """Registers a catch-all responder used when no frame-keyed response matches."""
        self._responder = responder

    def clear_responses(self) -> None:
        self._responses.clear()
        self._responder = None

    async def send_request(self, req: VehicleRequest) -> bytes | None:
        self._assert_connected()
        self.requests.append(_clone_request(req))
        if not req.expect_can_response:
            return None

        response = self._responses.get(_request_key(req))
        if response is None and self._responder is not None:
            return self._responder(req)
        if response is None:
            raise ValueError(
                f"No mock response registered for CAN{_normalize_bus(req.bus)} ID {req.tx_frame.can_id}"
            )
        if callable(response):
            return response(req)
        return bytes(response)

    async def send_request_batch(self, req: BatchRequest) -> list[BatchStepResult]:
        """Mirrors the device's batch semantics on top of ``send_request``.

        Steps run in order on one bus; a failing step maps to ``ok=False`` and
        later steps are still reported (as failures) so the result count
        matches the request.
        """
        self._assert_connected()
        if not req.steps:
            raise ValueError("Batch request must contain at least one step")
        bus = _normalize_bus(req.steps[0].request.bus)
        for step in req.steps:
            if _normalize_bus(step.request.bus) != bus:
                raise ValueError(
                    f"Batch step on CAN{_normalize_bus(step.request.bus)} does not match batch bus CAN{bus}"
                )
        self.batches.append(
            BatchRequest(steps=[replace(step, request=_clone_request(step.request)) for step in req.steps])
        )

        results: list[BatchStepResult] = []
        aborted = False
        for step in req.steps:
            if aborted:
                results.append(BatchStepResult(ok=False, error="skipped"))
                continue
            try:
                payload = await self.send_request(step.request)
            except Exception as error:  # noqa: BLE001 - surfaced as a step result
                results.append(BatchStepResult(ok=False, error=str(error)))
                aborted = True
                continue
            results.append(BatchStepResult(ok=True, response=payload))
        return results

    # -- monitoring ----------------------------------------------------------

    async def update_monitor(self, req: MonitorControlRequest) -> MonitorControlResponse:
        self._assert_connected()
        self.monitor_updates.append(MonitorControlRequest(req.operation, list(req.can_ids), req.bus))

        bus = _normalize_bus(req.bus)
        if not _is_valid_bus(bus):
            return self._monitor_response("invalid_bus", bus)

        bus_can_ids = self._monitor_can_ids_by_bus.setdefault(bus, set())
        if req.operation == "clear":
            bus_can_ids.clear()
            self._clear_frames_for_bus(bus)
            self._refresh_aggregate_monitor_can_ids()
            return self._monitor_response("ok", bus)

        if any(not _is_valid_can_id(can_id) for can_id in req.can_ids):
            return self._monitor_response("invalid_can_id", bus)

        if req.operation == "remove":
            for can_id in req.can_ids:
                bus_can_ids.discard(can_id)
                self._monitor_frames.pop((bus, can_id), None)
            self._refresh_aggregate_monitor_can_ids()
            return self._monitor_response("ok", bus)

        unique_can_ids = set(req.can_ids)
        if len(unique_can_ids) != len(req.can_ids) or any(can_id in bus_can_ids for can_id in req.can_ids):
            return self._monitor_response("duplicate_id", bus)

        if len(bus_can_ids) + len(unique_can_ids) > MAX_MONITOR_IDS:
            return self._monitor_response("monitor_full", bus)

        bus_can_ids.update(unique_can_ids)
        self._refresh_aggregate_monitor_can_ids()
        return self._monitor_response("ok", bus)

    def on_monitor_snapshot(self, cb: SnapshotCallback) -> DisposeCallback:
        self._callbacks.add(cb)

        def dispose() -> None:
            self._callbacks.discard(cb)

        return dispose

    def emit_frame(self, frame: CanFrame) -> None:
        """Records ``frame`` if its ID is monitored on its bus and emits a snapshot of all monitored frames."""
        bus = _normalize_bus(frame.bus)
        if frame.can_id not in self._monitor_can_ids_by_bus.get(bus, set()):
            return
        stored = frame.clone()
        stored.bus = bus
        self._monitor_frames[(bus, frame.can_id)] = stored
        self.emit_snapshot([stored.clone() for stored in self._monitor_frames.values()])

    def emit_snapshot(self, frames: list[CanFrame]) -> None:
        """Delivers ``frames`` to every monitor callback as one snapshot, unfiltered."""
        snapshot = MonitorSnapshot(sequence=self._snapshot_sequence, frames=list(frames))
        self._snapshot_sequence = (self._snapshot_sequence + 1) & 0xFFFF
        for callback in list(self._callbacks):
            callback(snapshot)

    # -- internals -----------------------------------------------------------

    def _assert_connected(self) -> None:
        if not self._connected:
            raise ValueError("MockTransport is not connected")

    def _clear_frames_for_bus(self, bus: int) -> None:
        for key in [key for key in self._monitor_frames if key[0] == bus]:
            self._monitor_frames.pop(key, None)

    def _refresh_aggregate_monitor_can_ids(self) -> None:
        self.monitor_can_ids.clear()
        for ids in self._monitor_can_ids_by_bus.values():
            self.monitor_can_ids.update(ids)

    def _monitor_response(self, status: MonitorControlStatus, bus: int) -> MonitorControlResponse:
        return MonitorControlResponse(
            status=status,
            current_monitor_count=len(self._monitor_can_ids_by_bus.get(bus, set())),
        )


def _clone_request(req: VehicleRequest) -> VehicleRequest:
    return replace(req, tx_frame=req.tx_frame.clone())


def _frame_key(frame: CanFrame) -> tuple[int, int, str]:
    return (_normalize_bus(frame.bus), frame.can_id, bytes(frame.data).hex())


def _request_key(req: VehicleRequest) -> tuple[int, int, str]:
    bus = req.bus if req.bus is not None else req.tx_frame.bus
    return (_normalize_bus(bus), req.tx_frame.can_id, bytes(req.tx_frame.data).hex())


def _normalize_bus(bus: int | None) -> int:
    return 0 if bus is None else bus


def _is_valid_bus(bus: int) -> bool:
    return isinstance(bus, int) and 0 <= bus <= 0xFF


def _is_valid_bitrate(bitrate: int) -> bool:
    return isinstance(bitrate, int) and 0 < bitrate <= 0xFFFFFFFF


def _is_valid_can_id(can_id: int) -> bool:
    return isinstance(can_id, int) and 0 <= can_id <= 0x1FFFFFFF
