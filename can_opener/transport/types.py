from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Literal, Protocol

from can_opener.dbc.types import ActionOptions, CanFrame, DiagnosticBinding

MonitorControlStatus = Literal[
    "ok",
    "invalid_opcode",
    "invalid_length",
    "monitor_full",
    "duplicate_id",
    "invalid_can_id",
    "internal_error",
]


@dataclass(frozen=True, slots=True)
class MonitorControlRequest:
    operation: Literal["add", "remove", "clear"]
    can_ids: list[int] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class MonitorControlResponse:
    status: MonitorControlStatus
    current_monitor_count: int


@dataclass(frozen=True, slots=True)
class MonitorSnapshot:
    sequence: int
    frames: list[CanFrame]


@dataclass(frozen=True, slots=True)
class VehicleRequest:
    tx_frame: CanFrame
    expect_can_response: bool
    signal_name: str | None = None
    notify_tx_status: bool | None = None
    response_id_start: int | None = None
    response_id_end: int | None = None
    timeout_ms: int | None = None
    diagnostic: DiagnosticBinding | None = None
    action: ActionOptions | None = None


SnapshotCallback = Callable[[MonitorSnapshot], None]
DisposeCallback = Callable[[], None]


class VehicleTransport(Protocol):
    async def connect(self) -> None: ...

    async def disconnect(self) -> None: ...

    async def send_request(self, req: VehicleRequest) -> bytes | None: ...

    async def update_monitor(self, req: MonitorControlRequest) -> MonitorControlResponse: ...

    def on_monitor_snapshot(self, cb: SnapshotCallback) -> DisposeCallback: ...
