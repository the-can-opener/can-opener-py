from __future__ import annotations

from can_opener.dbc.types import CanFrame
from can_opener.transport.types import CanFdRequest

from .types import ExpectPattern, ProfileCanFdConfig, ProfileEndpoint, RequestStep


def build_request_frame(
    endpoint: ProfileEndpoint,
    payload: bytes | bytearray,
    can_fd: ProfileCanFdConfig | None = None,
) -> CanFrame:
    """Builds the 8-byte request frame for ``endpoint``.

    ``can_fd`` defaults to the endpoint's resolved setting when it is a
    ``ResolvedProfileEndpoint``. CAN FD frames are sent with all eight bytes,
    zero-padded.
    """
    if can_fd is None:
        can_fd = getattr(endpoint, "can_fd", None)
    data = bytearray(8)
    data[: min(len(payload), 8)] = payload[:8]
    return CanFrame(
        can_id=endpoint.request_id,
        dlc=8 if can_fd is not None else min(len(payload), 8),
        data=bytes(data),
        extended=endpoint.extended,
        bus=endpoint.bus,
    )


def build_step_frame(
    step: RequestStep,
    endpoint: ProfileEndpoint | None = None,
    can_fd: ProfileCanFdConfig | None = None,
) -> CanFrame:
    """``can_fd`` is the endpoint's setting for endpoint-backed steps, or the inline step's resolved setting."""
    request_id = step.request_id if step.request_id is not None else (
        endpoint.request_id if endpoint is not None else None
    )
    if request_id is None:
        raise ValueError("Request step does not declare a request ID")
    if can_fd is None and endpoint is not None:
        can_fd = getattr(endpoint, "can_fd", None)
    bus = endpoint.bus if endpoint is not None and endpoint.bus is not None else step.bus
    return build_request_frame(
        ProfileEndpoint(
            name=endpoint.name if endpoint is not None else "direct",
            request_id=request_id,
            extended=endpoint.extended if endpoint is not None else None,
            bus=bus,
        ),
        step.send,
        can_fd,
    )


def can_fd_request(
    can_fd: ProfileCanFdConfig | None, frame: CanFrame, subject: str
) -> CanFdRequest | None:
    """The ``can_fd`` field for a request built from ``frame``.

    With ``tx_dl`` 8 the frame is sent as-is. With ``tx_dl`` 64 the transport
    repacks the payload, so the frame must be an ISO-TP single frame.
    """
    if can_fd is None:
        return None
    if can_fd.tx_dl == 8:
        return CanFdRequest(tx_dl=8)
    if can_fd.tx_dl == 64:
        data = bytes(frame.data)
        pci = data[0] if data else 0
        length = pci & 0x0F
        frame_length = frame.dlc if frame.dlc is not None else len(data)
        if (pci & 0xF0) != 0 or length == 0 or length > frame_length - 1:
            raise ValueError(
                f"{subject} uses CAN FD tx_dl 64, which requires send bytes that start "
                "with an ISO-TP single-frame PCI byte"
            )
        return CanFdRequest(tx_dl=64, payload=data[1 : 1 + length])
    raise AssertionError(f"Unsupported CAN FD tx_dl {can_fd.tx_dl}")


def response_bounds(endpoint: ProfileEndpoint) -> dict[str, int]:
    if not endpoint.response_ranges:
        return {}
    return {
        "response_id_start": min(r.start for r in endpoint.response_ranges),
        "response_id_end": max(r.end for r in endpoint.response_ranges),
    }


def assert_expected_response(expect: ExpectPattern | None, payload: bytes | bytearray | None) -> None:
    if expect is None:
        return
    if payload is None or not _matches_expected_response(expect, bytes(payload)):
        raise ValueError(f"Response payload did not match expected pattern {_format_pattern(expect)}")


def strip_expected_prefix(
    expect: ExpectPattern | None, payload: bytes | bytearray
) -> bytes:
    data = bytes(payload)
    if expect is None or expect.exact or len(expect.pattern) == 0:
        return data
    offset = _expected_offset(expect, data)
    return data if offset is None else data[offset + len(expect.pattern) :]


def _matches_expected_response(expect: ExpectPattern, payload: bytes) -> bool:
    return _expected_offset(expect, payload) is not None


def _expected_offset(expect: ExpectPattern, payload: bytes) -> int | None:
    if expect.exact and len(payload) != len(expect.pattern):
        return None
    if len(payload) < len(expect.pattern):
        return None
    if _matches_at(expect, payload, 0):
        return 0
    if not expect.exact and len(payload) > len(expect.pattern) and _matches_at(expect, payload, 1):
        return 1
    return None


def _matches_at(expect: ExpectPattern, payload: bytes, offset: int) -> bool:
    if len(payload) < offset + len(expect.pattern):
        return False
    return all(
        byte == "*" or payload[offset + index] == byte
        for index, byte in enumerate(expect.pattern)
    )


def _format_pattern(expect: ExpectPattern) -> str:
    bytes_ = ["*" if byte == "*" else f"0x{byte:02x}" for byte in expect.pattern]
    suffix = " exactly" if expect.exact else ""
    return f"[{', '.join(bytes_)}]{suffix}"
