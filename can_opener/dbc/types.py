from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

SignalProtocol = Literal["frame", "query", "pid"]
SignalValueType = Literal["number", "ascii", "bytes"]
DiagnosticTransport = Literal["single", "isotp"]
ByteOrder = Literal["little", "big"]
AttributeValue = str | int | float | bool


@dataclass(slots=True)
class DiagnosticRequest:
    can_id: int
    service_id: int | None = None
    pid: int | None = None
    did: int | None = None
    payload: bytes | None = None


@dataclass(slots=True)
class DiagnosticResponse:
    can_id: int | None = None
    service_id: int | None = None
    pid: int | None = None
    did: int | None = None


@dataclass(slots=True)
class DiagnosticBinding:
    request: DiagnosticRequest
    response: DiagnosticResponse
    transport: DiagnosticTransport | None = None
    response_length: int | None = None


@dataclass(slots=True)
class VehicleSignal:
    name: str
    protocol: SignalProtocol
    can_id: int
    message_name: str | None = None
    start_bit: int | None = None
    length: int | None = None
    byte_order: ByteOrder | None = None
    signed: bool | None = None
    scale: float | None = None
    offset: float | None = None
    unit: str | None = None
    value_type: SignalValueType | None = None
    enum_values: dict[int, str] | None = None
    diagnostic: DiagnosticBinding | None = None


@dataclass(slots=True)
class VehicleSignalState:
    name: str
    signal: VehicleSignal
    enum_value: int | None = None


@dataclass(slots=True)
class CanFrame:
    can_id: int
    data: bytes | bytearray
    dlc: int | None = None
    extended: bool | None = None

    def clone(self) -> "CanFrame":
        return CanFrame(
            can_id=self.can_id,
            data=bytes(self.data),
            dlc=self.dlc,
            extended=self.extended,
        )


@dataclass(slots=True)
class DbcFile:
    name: str
    content: str


@dataclass(slots=True)
class RawDbcSignal:
    name: str
    message_id: int
    start_bit: int
    length: int
    byte_order: ByteOrder
    signed: bool
    scale: float
    offset: float
    minimum: float | None = None
    maximum: float | None = None
    unit: str | None = None
    receivers: list[str] = field(default_factory=list)


@dataclass(slots=True)
class RawDbcMessage:
    id: int
    name: str
    size: int
    transmitter: str
    signals: list[RawDbcSignal] = field(default_factory=list)


@dataclass(slots=True)
class RawDbcAttributeDefinition:
    name: str
    type: Literal["STRING", "INT", "FLOAT", "ENUM", "HEX"]
    scope: Literal["SG_", "BO_", "BU_", "EV_"] | None = None
    enum_values: list[str] | None = None


@dataclass(slots=True)
class RawDbcAttribute:
    name: str
    value: AttributeValue
    scope: Literal["SG_", "BO_", "BU_", "EV_"] | None = None
    message_id: int | None = None
    signal_name: str | None = None


@dataclass(slots=True)
class RawDbcValueTable:
    message_id: int
    signal_name: str
    values: dict[int, str]


@dataclass(slots=True)
class RawDbc:
    file_name: str
    messages: list[RawDbcMessage]
    attribute_definitions: list[RawDbcAttributeDefinition]
    attributes: list[RawDbcAttribute]
    value_tables: list[RawDbcValueTable]


@dataclass(slots=True)
class DecodedSignalValue:
    name: str
    value: Any
    signal: VehicleSignal


@dataclass(frozen=True, slots=True)
class PollingOptions:
    frequency_hz: float | None = None
    duration_ms: int | None = None


@dataclass(frozen=True, slots=True)
class QuerySubscriptionHandle:
    id: str
    signal_name: str


@dataclass(frozen=True, slots=True)
class ActionOptions:
    value: Any
    mask: int | None = None
    frequency_hz: float | None = None
    duration_ms: int | None = None
