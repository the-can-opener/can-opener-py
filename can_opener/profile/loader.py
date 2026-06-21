from __future__ import annotations

import re
from typing import Any

import yaml

from can_opener.errors import VirtualVehicleError

from .types import (
    BuiltInDecoderRef,
    DbcDecoderRef,
    ExpectPattern,
    LoadedVehicleProfile,
    ProfileAction,
    ProfileByte,
    ProfileEndpoint,
    ProfileMonitorSignal,
    ProfileQuery,
    ProfileValueNormalization,
    RequestStep,
    ResponseIdRange,
    VehicleProfileSource,
)


class ProfileLoader:
    def load(self, sources: list[VehicleProfileSource]) -> list[LoadedVehicleProfile]:
        profiles = [self._load_one(source) for source in sources]
        _assert_unique_slugs(profiles)
        return profiles

    def _load_one(self, source: VehicleProfileSource) -> LoadedVehicleProfile:
        raw = yaml.safe_load(source.content)
        if not isinstance(raw, dict):
            raise VirtualVehicleError(f"Profile {source.name} must contain a YAML object")
        if raw.get("version") != 1:
            raise VirtualVehicleError(f"Profile {source.name} must declare version: 1")

        sequences = raw.get("sequences") or {}
        raw_dbc_files = ((raw.get("dbc") or {}).get("files") or [])
        declared_paths = {_read_string(file.get("path"), "dbc.files.path") for file in raw_dbc_files}
        dbc_files = (
            source.dbc_files
            if not declared_paths
            else [file for file in source.dbc_files if file.name in declared_paths]
        )

        return LoadedVehicleProfile(
            name=source.name,
            dbc_files=dbc_files,
            endpoints=[
                _normalize_endpoint(name, endpoint)
                for name, endpoint in (raw.get("endpoints") or {}).items()
            ],
            queries=[
                _normalize_query(name, query)
                for name, query in (raw.get("queries") or {}).items()
            ],
            actions=[
                _normalize_action(name, action, sequences)
                for name, action in (raw.get("actions") or {}).items()
            ],
            signals=[
                signal
                for name, raw_signal in (raw.get("signals") or {}).items()
                for signal in _normalize_signal(name, raw_signal)
            ],
            display_name=(
                _read_string(raw.get("name"), "name")
                if raw.get("name") is not None
                else None
            ),
            profile_version=(
                _read_string(raw.get("profile_version"), "profile_version")
                if raw.get("profile_version") is not None
                else None
            ),
            slug=_normalize_slug(raw),
        )


def _assert_unique_slugs(profiles: list[LoadedVehicleProfile]) -> None:
    seen: dict[str, str] = {}
    for profile in profiles:
        if profile.slug is None:
            continue
        existing = seen.get(profile.slug)
        if existing is not None:
            raise VirtualVehicleError(
                f'Profile slug "{profile.slug}" is already in use by {existing}; '
                f"cannot also use it for {profile.name}"
            )
        seen[profile.slug] = profile.name


def _normalize_slug(raw: dict[str, Any]) -> str | None:
    if raw.get("slug") is not None:
        slug = _read_string(raw.get("slug"), "slug")
        if slug != _slugify(slug):
            raise VirtualVehicleError(
                "slug must contain only lowercase letters, numbers, and hyphens"
            )
        return slug

    name = raw.get("name")
    if name is None:
        return None
    return _slugify(_read_string(name, "name"))


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    if not slug:
        raise VirtualVehicleError("slug must contain at least one letter or number")
    return slug


def _normalize_endpoint(name: str, raw: dict[str, Any]) -> ProfileEndpoint:
    response_ranges: list[ResponseIdRange] = []
    if raw.get("response_id") is not None:
        response_id = _read_number(raw.get("response_id"), f"endpoints.{name}.response_id")
        response_ranges.append(ResponseIdRange(response_id, response_id))
    for response in raw.get("response_ids") or []:
        response_ranges.append(_normalize_response_range(response, f"endpoints.{name}.response_ids"))
    return ProfileEndpoint(
        name=name,
        request_id=_read_number(raw.get("request_id"), f"endpoints.{name}.request_id"),
        response_ranges=response_ranges,
        timeout_ms=(
            _read_number(raw.get("timeout_ms"), f"endpoints.{name}.timeout_ms")
            if raw.get("timeout_ms") is not None
            else None
        ),
    )


def _normalize_response_range(raw: Any, path: str) -> ResponseIdRange:
    if isinstance(raw, (int, float, str)):
        response_id = _read_number(raw, path)
        return ResponseIdRange(response_id, response_id)
    if isinstance(raw, dict) and "range" in raw:
        range_ = raw.get("range")
        if not isinstance(range_, list) or len(range_) != 2:
            raise VirtualVehicleError(f"{path}.range must contain [start, end]")
        return ResponseIdRange(
            _read_number(range_[0], f"{path}.range[0]"),
            _read_number(range_[1], f"{path}.range[1]"),
        )
    raise VirtualVehicleError(f"{path} must be a response ID or range")


def _normalize_query(name: str, raw: dict[str, Any]) -> ProfileQuery:
    decoder = _normalize_query_decoder(name, raw)
    return ProfileQuery(
        name=name,
        endpoint=_read_string(raw.get("endpoint"), f"queries.{name}.endpoint"),
        send=bytes(_read_byte_array(raw.get("send"), f"queries.{name}.send")),
        expect=(
            _normalize_expect(raw.get("expect"), f"queries.{name}.expect")
            if raw.get("expect") is not None
            else None
        ),
        decoder=decoder,
        length=(
            _read_number(raw.get("length"), f"queries.{name}.length")
            if raw.get("length") is not None
            else None
        ),
        normalize=(
            _normalize_value_normalization(raw.get("normalize"), f"queries.{name}.normalize")
            if raw.get("normalize") is not None
            else None
        ),
    )


def _normalize_action(
    name: str, raw: dict[str, Any], sequences: dict[str, Any]
) -> ProfileAction:
    if raw.get("steps") is not None:
        steps = _expand_steps(raw.get("steps") or [], sequences, raw.get("endpoint"))
    else:
        inline_send = _normalize_action_send(raw.get("send"), f"actions.{name}.send")
        steps = [
            RequestStep(
                endpoint=(
                    _read_string(raw.get("endpoint"), f"actions.{name}.endpoint")
                    if raw.get("endpoint") is not None
                    else None
                ),
                request_id=inline_send[0],
                send=inline_send[1],
                expect=(
                    _normalize_expect(raw.get("expect"), f"actions.{name}.expect")
                    if raw.get("expect") is not None
                    else None
                ),
            )
        ]
    return ProfileAction(
        name=name,
        endpoint=(
            _read_string(raw.get("endpoint"), f"actions.{name}.endpoint")
            if raw.get("endpoint") is not None
            else None
        ),
        steps=steps,
    )


def _normalize_action_send(raw: Any, path: str) -> tuple[int | None, bytes]:
    if isinstance(raw, dict):
        return (
            _read_number(raw.get("request_id"), f"{path}.request_id"),
            bytes(_read_byte_array(raw.get("request"), f"{path}.request")),
        )
    return None, bytes(_read_byte_array(raw, path))


def _normalize_signal(name: str, raw: dict[str, Any]) -> list[ProfileMonitorSignal]:
    monitor = raw.get("monitor")
    if monitor is None:
        return []
    normalize = _read_signal_normalization(name, raw)
    return [
        ProfileMonitorSignal(
            name=name,
            message=_read_string(monitor.get("message"), f"signals.{name}.monitor.message"),
            signal=_read_string(monitor.get("signal"), f"signals.{name}.monitor.signal"),
            normalize=normalize,
        )
    ]


def _read_signal_normalization(name: str, raw: dict[str, Any]) -> ProfileValueNormalization | None:
    if raw.get("normalize") is not None and raw.get("state") is not None:
        raise VirtualVehicleError(f"signals.{name} cannot declare both normalize and state")
    value = raw.get("normalize") if raw.get("normalize") is not None else raw.get("state")
    if value is None:
        return None
    key = "normalize" if raw.get("normalize") is not None else "state"
    return _normalize_value_normalization(value, f"signals.{name}.{key}")


def _expand_steps(
    raw_steps: list[dict[str, Any]],
    sequences: dict[str, Any],
    inherited_endpoint: Any,
    seen: list[str] | None = None,
) -> list[RequestStep]:
    seen = seen or []
    steps: list[RequestStep] = []
    for step in raw_steps:
        if step.get("ref") is not None:
            ref = _read_string(step.get("ref"), "steps.ref").removeprefix("sequences.")
            if ref in seen:
                raise VirtualVehicleError(f"Circular sequence reference: {' -> '.join([*seen, ref])}")
            sequence = sequences.get(ref)
            if sequence is None:
                raise VirtualVehicleError(f"Unknown sequence reference: {ref}")
            steps.extend(
                _expand_steps(
                    sequence.get("steps") or [],
                    sequences,
                    sequence.get("endpoint", inherited_endpoint),
                    [*seen, ref],
                )
            )
            continue
        endpoint_raw = step.get("endpoint", inherited_endpoint)
        steps.append(
            RequestStep(
                endpoint=(
                    _read_string(endpoint_raw, "steps.endpoint")
                    if endpoint_raw is not None
                    else None
                ),
                send=bytes(_read_byte_array(step.get("send"), "steps.send")),
                expect=(
                    _normalize_expect(step.get("expect"), "steps.expect")
                    if step.get("expect") is not None
                    else None
                ),
            )
        )
    return steps


def _normalize_query_decoder(
    name: str, raw: dict[str, Any]
) -> DbcDecoderRef | BuiltInDecoderRef | None:
    if raw.get("dbc_mapping") is not None:
        if raw.get("decoder") is not None:
            raise VirtualVehicleError(f"queries.{name} cannot declare both dbc_mapping and decoder")
        return _normalize_dbc_mapping(raw.get("dbc_mapping"), f"queries.{name}.dbc_mapping")
    if raw.get("decoder") is None:
        return None
    return _normalize_decoder(raw.get("decoder"), f"queries.{name}.decoder")


def _normalize_dbc_mapping(raw: Any, path: str) -> DbcDecoderRef:
    if isinstance(raw, dict):
        return DbcDecoderRef(
            message=_read_string(raw.get("message"), f"{path}.message"),
            signal=_read_string(raw.get("signal"), f"{path}.signal"),
        )
    raise VirtualVehicleError(f"{path} must reference a DBC message and signal")


def _normalize_decoder(raw: Any, path: str) -> BuiltInDecoderRef:
    if raw in {"ascii", "bytes"}:
        return BuiltInDecoderRef(type=raw)
    if isinstance(raw, dict) and raw.get("type") in {"ascii", "bytes"}:
        return BuiltInDecoderRef(
            type=raw["type"],
            length=(
                _read_number(raw.get("length"), f"{path}.length")
                if raw.get("length") is not None
                else None
            ),
        )
    raise VirtualVehicleError(f"{path} must be a built-in decoder name")


def _normalize_value_normalization(raw: Any, path: str) -> ProfileValueNormalization:
    if not isinstance(raw, dict):
        raise VirtualVehicleError(f"{path} must be an object")
    enum = raw.get("enum")
    return ProfileValueNormalization(
        enum=_read_string_map(enum, f"{path}.enum") if enum is not None else None
    )


def _normalize_expect(raw: Any, path: str) -> ExpectPattern:
    if raw == "none":
        return ExpectPattern(pattern=[], exact=True)
    if isinstance(raw, list):
        return ExpectPattern(pattern=_read_pattern(raw, path), exact=False)
    if isinstance(raw, dict):
        pattern = raw.get("pattern")
        if not isinstance(pattern, list):
            raise VirtualVehicleError(f"{path}.pattern must be an array")
        return ExpectPattern(pattern=_read_pattern(pattern, f"{path}.pattern"), exact=raw.get("exact") is True)
    raise VirtualVehicleError(f"{path} must be an array, object, or none")


def _read_pattern(raw: list[Any], path: str) -> list[ProfileByte]:
    return ["*" if byte == "*" else _read_byte(byte, f"{path}[{index}]") for index, byte in enumerate(raw)]


def _read_byte_array(raw: Any, path: str) -> list[int]:
    if not isinstance(raw, list):
        raise VirtualVehicleError(f"{path} must be an array")
    return [_read_byte(byte, f"{path}[{index}]") for index, byte in enumerate(raw)]


def _read_byte(raw: Any, path: str) -> int:
    value = _read_number(raw, path)
    if not isinstance(value, int) or value < 0 or value > 0xFF:
        raise VirtualVehicleError(f"{path} must be a byte between 0 and 255")
    return value


def _read_number(raw: Any, path: str) -> int:
    if isinstance(raw, bool):
        raise VirtualVehicleError(f"{path} must be numeric")
    if isinstance(raw, (int, float)):
        return int(raw)
    if isinstance(raw, str):
        try:
            return int(raw, 16) if raw.lower().startswith("0x") else int(float(raw))
        except ValueError:
            pass
    raise VirtualVehicleError(f"{path} must be numeric")


def _read_string(raw: Any, path: str) -> str:
    if not isinstance(raw, str) or len(raw) == 0:
        raise VirtualVehicleError(f"{path} must be a non-empty string")
    return raw


def _read_string_map(raw: Any, path: str) -> dict[str, str]:
    if not isinstance(raw, dict):
        raise VirtualVehicleError(f"{path} must be an object")
    return {str(key): _read_string(value, f"{path}.{key}") for key, value in raw.items()}
