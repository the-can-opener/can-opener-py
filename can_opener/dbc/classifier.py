from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Mapping

from .types import (
    DiagnosticBinding,
    DiagnosticRequest,
    DiagnosticResponse,
    DiagnosticTransport,
    RawDbc,
    RawDbcAttribute,
    RawDbcSignal,
    SignalProtocol,
    SignalValueType,
    VehicleSignal,
)


@dataclass(frozen=True, slots=True)
class SignalClassifierAttributeMap:
    protocol: str = "SignalProtocol"
    pid: str = "Pid"
    request_can_id: str = "RequestCanId"
    response_can_id: str = "ResponseCanId"
    diagnostic_service_id: str = "DiagnosticServiceId"
    uds_service_id: str = "UdsServiceId"
    uds_did: str = "UdsDid"
    uds_payload: str = "UdsPayload"
    diagnostic_transport: str = "DiagnosticTransport"
    response_length: str = "ResponseLength"
    value_type: str = "SignalValueType"


@dataclass(frozen=True, slots=True)
class SignalClassifierOptions:
    attribute_map: Mapping[str, str] | None = None
    default_protocol: SignalProtocol = "frame"


class SignalClassifier:
    def __init__(self, options: SignalClassifierOptions | None = None) -> None:
        options = options or SignalClassifierOptions()
        defaults = SignalClassifierAttributeMap()
        values = asdict(defaults) | dict(options.attribute_map or {})
        self.attribute_map = SignalClassifierAttributeMap(**values)
        self.default_protocol = options.default_protocol

    def classify(self, raw: RawDbc) -> list[VehicleSignal]:
        signals: list[VehicleSignal] = []
        for message in raw.messages:
            for raw_signal in message.signals:
                attributes = [
                    attribute
                    for attribute in raw.attributes
                    if attribute.scope == "SG_"
                    and attribute.message_id == message.id
                    and attribute.signal_name == raw_signal.name
                ]
                signals.append(self._classify_signal(raw, raw_signal, attributes))
        return signals

    def _classify_signal(
        self, raw: RawDbc, raw_signal: RawDbcSignal, attributes: list[RawDbcAttribute]
    ) -> VehicleSignal:
        protocol = self._read_signal_protocol(attributes)
        pid = self._read_number_attribute(attributes, self.attribute_map.pid)
        request_can_id = self._read_number_attribute(attributes, self.attribute_map.request_can_id)
        response_can_id = self._read_number_attribute(attributes, self.attribute_map.response_can_id)
        service_id = self._read_number_attribute(
            attributes, self.attribute_map.diagnostic_service_id
        )
        if service_id is None:
            service_id = self._read_number_attribute(attributes, self.attribute_map.uds_service_id)
        did = self._read_number_attribute(attributes, self.attribute_map.uds_did)
        payload = self._read_payload_attribute(attributes, self.attribute_map.uds_payload)
        diagnostic_transport = self._read_diagnostic_transport(attributes)
        response_length = self._read_number_attribute(attributes, self.attribute_map.response_length)
        value_type = self._read_signal_value_type(attributes)
        enum_values = next(
            (
                table.values
                for table in raw.value_tables
                if table.message_id == raw_signal.message_id
                and table.signal_name == raw_signal.name
            ),
            None,
        )
        message_name = next(
            (message.name for message in raw.messages if message.id == raw_signal.message_id), None
        )

        diagnostic = None
        if protocol == "pid":
            diagnostic = self._build_diagnostic_binding(
                raw_signal,
                pid=pid,
                request_can_id=request_can_id,
                response_can_id=response_can_id,
                service_id=service_id,
                did=did,
                payload=payload,
                diagnostic_transport=diagnostic_transport,
                response_length=response_length,
            )

        return VehicleSignal(
            name=raw_signal.name,
            protocol=protocol,
            can_id=response_can_id if response_can_id is not None else raw_signal.message_id,
            message_name=message_name,
            start_bit=raw_signal.start_bit,
            length=raw_signal.length,
            byte_order=raw_signal.byte_order,
            signed=raw_signal.signed,
            scale=raw_signal.scale,
            offset=raw_signal.offset,
            unit=raw_signal.unit,
            value_type=value_type,
            enum_values=enum_values,
            diagnostic=diagnostic,
        )

    def _read_signal_protocol(self, attributes: list[RawDbcAttribute]) -> SignalProtocol:
        value = self._read_attribute(attributes, self.attribute_map.protocol)
        if value is None:
            return self.default_protocol
        if value in {"frame", "pid", "query"}:
            return value  # type: ignore[return-value]
        raise ValueError(f"Unsupported signal protocol: {value}")

    def _build_diagnostic_binding(
        self,
        raw_signal: RawDbcSignal,
        *,
        pid: int | None,
        request_can_id: int | None,
        response_can_id: int | None,
        service_id: int | None,
        did: int | None,
        payload: bytes | None,
        diagnostic_transport: DiagnosticTransport | None,
        response_length: int | None,
    ) -> DiagnosticBinding:
        if payload is None and pid is None and did is None:
            raise ValueError(
                f"PID signal {raw_signal.name} must define Pid, UdsDid, or UdsPayload metadata"
            )

        request_service_id = service_id if service_id is not None else (0x22 if did is not None else 0x01)
        return DiagnosticBinding(
            request=DiagnosticRequest(
                can_id=request_can_id if request_can_id is not None else raw_signal.message_id,
                service_id=request_service_id,
                pid=pid,
                did=did,
                payload=payload,
            ),
            response=DiagnosticResponse(
                can_id=response_can_id if response_can_id is not None else raw_signal.message_id,
                service_id=request_service_id | 0x40,
                pid=pid,
                did=did,
            ),
            transport=diagnostic_transport,
            response_length=response_length,
        )

    def _read_diagnostic_transport(
        self, attributes: list[RawDbcAttribute]
    ) -> DiagnosticTransport | None:
        value = self._read_attribute(attributes, self.attribute_map.diagnostic_transport)
        if value is None:
            return None
        if value in {"single", "isotp"}:
            return value  # type: ignore[return-value]
        raise ValueError(f"Unsupported diagnostic transport: {value}")

    def _read_signal_value_type(self, attributes: list[RawDbcAttribute]) -> SignalValueType | None:
        value = self._read_attribute(attributes, self.attribute_map.value_type)
        if value is None:
            return None
        if value in {"number", "ascii", "bytes"}:
            return value  # type: ignore[return-value]
        raise ValueError(f"Unsupported signal value type: {value}")

    def _read_number_attribute(self, attributes: list[RawDbcAttribute], name: str) -> int | None:
        value = self._read_attribute(attributes, name)
        if value is None:
            return None
        if not isinstance(value, (int, float)):
            raise ValueError(f"Attribute {name} must be numeric")
        return int(value)

    def _read_payload_attribute(self, attributes: list[RawDbcAttribute], name: str) -> bytes | None:
        value = self._read_attribute(attributes, name)
        if value is None:
            return None
        if isinstance(value, (int, float)):
            return bytes([int(value) & 0xFF])
        if not isinstance(value, str):
            raise ValueError(f"Attribute {name} must be a hex string or number")
        normalized = value.removeprefix("0x").removeprefix("0X")
        normalized = "".join(normalized.split())
        if len(normalized) % 2 != 0:
            raise ValueError(f"Attribute {name} must be an even-length hex string")
        try:
            return bytes.fromhex(normalized)
        except ValueError as exc:
            raise ValueError(f"Attribute {name} must be an even-length hex string") from exc

    @staticmethod
    def _read_attribute(attributes: list[RawDbcAttribute], name: str):
        return next((attribute.value for attribute in attributes if attribute.name == name), None)


def classify_signals(
    raw: RawDbc, options: SignalClassifierOptions | None = None
) -> list[VehicleSignal]:
    return SignalClassifier(options).classify(raw)
