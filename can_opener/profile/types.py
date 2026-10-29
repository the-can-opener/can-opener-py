from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml

from can_opener.dbc.types import DbcFile
from can_opener.errors import VirtualVehicleError

ProfileByte = int | Literal["*"]

# ISO-TP frame size for a CAN FD request: 8 keeps the classic PCI in an FD
# frame, 64 uses the ISO 15765-2:2016 escape form.
CanFdTxDl = Literal[8, 64]
CanBitrate = int | Literal["auto"]

# Used when a step omits ``delay_ms``.
DEFAULT_STEP_DELAY_MS = 20


@dataclass(slots=True)
class VehicleProfileSource:
    name: str
    content: str
    dbc_files: list[DbcFile] = field(default_factory=list)

    @classmethod
    def from_directory(
        cls,
        directory: str | Path,
        profile_file: str = "profile.yaml",
    ) -> "VehicleProfileSource":
        directory_path = Path(directory)
        profile_path = directory_path / profile_file
        content = profile_path.read_text()
        raw = yaml.safe_load(content)
        if not isinstance(raw, dict):
            raise VirtualVehicleError(f"Profile {profile_path} must contain a YAML object")

        dbc_files = [
            DbcFile(path, (directory_path / path).read_text())
            for path in _read_dbc_paths(raw, profile_path)
        ]
        return cls(name=profile_path.as_posix(), content=content, dbc_files=dbc_files)

    @classmethod
    def from_folder(
        cls,
        folder: str | Path,
        profile_file: str = "profile.yaml",
    ) -> "VehicleProfileSource":
        return cls.from_directory(folder, profile_file)


@dataclass(frozen=True, slots=True)
class ProfileAppliesTo:
    """Catalog metadata describing the vehicles a profile targets.

    Missing (empty) fields act as wildcards. ``years`` entries are either a
    single model year or an inclusive ``(start, end)`` range.
    """

    manufacturers: list[str] = field(default_factory=list)
    models: list[str] = field(default_factory=list)
    years: list[int | tuple[int, int]] = field(default_factory=list)
    trims: list[str] = field(default_factory=list)
    engines: list[str] = field(default_factory=list)
    markets: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class ProfileCanBusFd:
    """ISO-TP frame size for endpoints on this bus that do not set their own."""

    isotp_tx_dl: CanFdTxDl = 8


@dataclass(frozen=True, slots=True)
class ProfileCanBusConfig:
    """Timing and default frame format for one logical CAN controller."""

    bus: int
    bitrate: CanBitrate
    data_bitrate: int | None = None
    fd: ProfileCanBusFd | None = None


@dataclass(frozen=True, slots=True)
class ProfileCanFdConfig:
    tx_dl: CanFdTxDl


# ``True``: CAN FD at the bus's ISO-TP frame size. ``ProfileCanFdConfig``: CAN FD
# at that size. ``False``: classic CAN.
ProfileEndpointFd = bool | ProfileCanFdConfig


@dataclass(frozen=True, slots=True)
class ResponseIdRange:
    start: int
    end: int


@dataclass(frozen=True, slots=True)
class ProfileEndpoint:
    name: str
    request_id: int
    response_ranges: list[ResponseIdRange] = field(default_factory=list)
    timeout_ms: int | None = None
    bus: int | None = None
    extended: bool | None = None
    fd: ProfileEndpointFd | None = None


@dataclass(frozen=True, slots=True)
class ResolvedProfileEndpoint(ProfileEndpoint):
    """An endpoint with its bus default applied, as ``CapabilityRegistry`` returns it."""

    can_fd: ProfileCanFdConfig | None = None


@dataclass(frozen=True, slots=True)
class ExpectPattern:
    pattern: list[ProfileByte]
    exact: bool


@dataclass(frozen=True, slots=True)
class DbcDecoderRef:
    message: str
    signal: str
    type: Literal["dbc"] = "dbc"


@dataclass(frozen=True, slots=True)
class BuiltInDecoderRef:
    type: Literal["ascii", "bytes"]
    length: int | None = None


QueryDecoder = DbcDecoderRef | BuiltInDecoderRef


@dataclass(frozen=True, slots=True)
class ProfileValueNormalization:
    enum: dict[str, str] | None = None


@dataclass(frozen=True, slots=True)
class RequestStep:
    send: bytes
    endpoint: str | None = None
    request_id: int | None = None
    expect: ExpectPattern | None = None
    # Inline request_id steps only; endpoint-backed steps inherit endpoint.bus.
    bus: int | None = None
    # Inline request_id steps only: send as CAN FD (True) or classic (False).
    # None: the bus default. Endpoint-backed steps use the endpoint's setting.
    fd: bool | None = None
    # Overrides the endpoint response timeout for this step.
    timeout_ms: int | None = None
    # Gap inserted after this step before the next one is transmitted.
    delay_ms: int = DEFAULT_STEP_DELAY_MS


@dataclass(frozen=True, slots=True)
class ProfileQuery:
    name: str
    endpoint: str
    send: bytes
    expect: ExpectPattern | None = None
    decoder: QueryDecoder | None = None
    length: int | None = None
    normalize: ProfileValueNormalization | None = None
    timeout_ms: int | None = None


@dataclass(frozen=True, slots=True)
class ProfileAction:
    name: str
    endpoint: str | None = None
    steps: list[RequestStep] = field(default_factory=list)
    # When False, multi-step actions are sent one transport transaction at a
    # time even if the transport can batch them.
    batch: bool | None = None


@dataclass(frozen=True, slots=True)
class ProfileMonitorSignal:
    name: str
    message: str
    signal: str
    normalize: ProfileValueNormalization | None = None
    bus: int | None = None


@dataclass(frozen=True, slots=True)
class LoadedVehicleProfile:
    name: str
    dbc_files: list[DbcFile]
    endpoints: list[ProfileEndpoint]
    queries: list[ProfileQuery]
    actions: list[ProfileAction]
    signals: list[ProfileMonitorSignal]
    display_name: str | None = None
    profile_version: str | None = None
    slug: str | None = None
    applies_to: ProfileAppliesTo | None = None
    can_buses: list[ProfileCanBusConfig] = field(default_factory=list)


def _read_dbc_paths(raw: dict[str, Any], profile_path: Path) -> list[str]:
    raw_files = ((raw.get("dbc") or {}).get("files") or [])
    paths: list[str] = []
    for index, file in enumerate(raw_files):
        path = file.get("path") if isinstance(file, dict) else None
        if not isinstance(path, str) or not path:
            raise VirtualVehicleError(
                f"{profile_path}: dbc.files[{index}].path must be a non-empty string"
            )
        paths.append(path)
    return paths
