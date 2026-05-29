from __future__ import annotations

from can_opener.dbc.types import CanFrame

from .types import ExpectPattern, ProfileEndpoint, RequestStep


def build_request_frame(endpoint: ProfileEndpoint, payload: bytes | bytearray) -> CanFrame:
    data = bytearray(8)
    data[: min(len(payload), 8)] = payload[:8]
    return CanFrame(
        can_id=endpoint.request_id,
        dlc=min(len(payload), 8),
        data=bytes(data),
    )


def build_step_frame(step: RequestStep, endpoint: ProfileEndpoint) -> CanFrame:
    if step.request_id is not None:
        data = bytearray(8)
        data[: min(len(step.send), 8)] = step.send[:8]
        return CanFrame(
            can_id=step.request_id,
            dlc=min(len(step.send), 8),
            data=bytes(data),
        )
    return build_request_frame(endpoint, step.send)


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
