from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Literal, Protocol

from can_opener.dbc.types import ActionOptions, CanFrame, DiagnosticBinding
from can_opener.profile.types import CanBitrate, CanFdTxDl, ProfileCanBusConfig, ProfileCanBusFd

MonitorControlStatus = Literal[
    "ok",
    "invalid_opcode",
    "invalid_length",
    "monitor_full",
    "duplicate_id",
    "invalid_can_id",
    "internal_error",
    "invalid_bus",
]

ConfigureBusStatus = Literal["ok", "auto_detect_failed", "error"]


@dataclass(frozen=True, slots=True)
class MonitorControlRequest:
    operation: Literal["add", "remove", "clear"]
    can_ids: list[int] = field(default_factory=list)
    # Logical CAN controller. None means bus 0.
    bus: int | None = None


@dataclass(frozen=True, slots=True)
class MonitorControlResponse:
    status: MonitorControlStatus
    current_monitor_count: int


@dataclass(frozen=True, slots=True)
class MonitorSnapshot:
    sequence: int
    frames: list[CanFrame]


@dataclass(frozen=True, slots=True)
class CanFdRequest:
    """Send a request as CAN FD.

    ``tx_dl`` 8 sends ``tx_frame`` unchanged as one 8-byte FD frame. ``tx_dl``
    64 sends ``payload`` (the ISO-TP application payload without its PCI byte)
    in 64-byte frames with ISO 15765-2:2016 PCI.
    """

    tx_dl: CanFdTxDl
    payload: bytes | None = None


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
    # Logical CAN controller. None means bus 0.
    bus: int | None = None
    # Send as CAN FD. None means classic CAN.
    can_fd: CanFdRequest | None = None


@dataclass(frozen=True, slots=True)
class ConfigureBusRequest:
    bus: int
    bitrate: CanBitrate
    data_bitrate: int | None = None
    fd: ProfileCanBusFd | None = None


@dataclass(frozen=True, slots=True)
class ConfigureBusResponse:
    status: ConfigureBusStatus
    bitrate: int | None = None
    data_bitrate: int | None = None
    message: str | None = None


@dataclass(frozen=True, slots=True)
class BatchStep:
    request: VehicleRequest
    # Gap the device inserts after this step before transmitting the next one.
    delay_ms: int


@dataclass(frozen=True, slots=True)
class BatchRequest:
    steps: list[BatchStep]


@dataclass(frozen=True, slots=True)
class BatchStepResult:
    ok: bool
    response: bytes | None = None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class BusTiming:
    """What the transport reported after applying one bus's profile timing."""

    requested: ProfileCanBusConfig
    status: ConfigureBusStatus
    bitrate: int | None = None
    data_bitrate: int | None = None


SnapshotCallback = Callable[[MonitorSnapshot], None]
DisposeCallback = Callable[[], None]


class VehicleTransport(Protocol):
    """Device-facing contract used by the vehicle layer.

    Optional capabilities, detected at runtime with
    :func:`transport_supports_configure_bus` / :func:`transport_supports_batch`:

    - ``async def configure_bus(req: ConfigureBusRequest) -> ConfigureBusResponse``
      applies a profile's ``can.buses`` timing.
    - ``async def send_request_batch(req: BatchRequest) -> list[BatchStepResult]``
      transmits several steps back-to-back on one bus in a single device
      transaction, returning one result per step in request order.
    """

    async def connect(self) -> None: ...

    async def disconnect(self) -> None: ...

    async def send_request(self, req: VehicleRequest) -> bytes | None: ...

    async def update_monitor(self, req: MonitorControlRequest) -> MonitorControlResponse: ...

    def on_monitor_snapshot(self, cb: SnapshotCallback) -> DisposeCallback: ...


def transport_supports_batch(transport: Any) -> bool:
    return callable(getattr(transport, "send_request_batch", None))


def transport_supports_configure_bus(transport: Any) -> bool:
    return callable(getattr(transport, "configure_bus", None))
