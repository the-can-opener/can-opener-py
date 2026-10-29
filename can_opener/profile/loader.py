from __future__ import annotations

import re
from typing import Any

import yaml

from can_opener.errors import VirtualVehicleError

from .types import (
    BuiltInDecoderRef,
    CanBitrate,
    CanFdTxDl,
    DbcDecoderRef,
    ExpectPattern,
    LoadedVehicleProfile,
    ProfileAction,
    ProfileAppliesTo,
    ProfileByte,
    ProfileCanBusConfig,
    ProfileCanBusFd,
    ProfileCanFdConfig,
    ProfileEndpoint,
    ProfileEndpointFd,
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

        actions: list[ProfileAction] = []
        for name, action in (raw.get("actions") or {}).items():
            normalized = _normalize_action(name, action, sequences)
            if normalized is not None:
                actions.append(normalized)

        return LoadedVehicleProfile(
            name=source.name,
            dbc_files=dbc_files,
            can_buses=_normalize_can_buses((raw.get("can") or {}).get("buses")),
            endpoints=[
                _normalize_endpoint(name, endpoint)
                for name, endpoint in (raw.get("endpoints") or {}).items()
            ],
            queries=[
                _normalize_query(name, query)
                for name, query in (raw.get("queries") or {}).items()
            ],
            actions=actions,
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
            applies_to=(
                _normalize_applies_to(raw.get("applies_to"), "applies_to")
                if raw.get("applies_to") is not None
                else None
            ),
        )


def merge_profile_can_bus_configs(
    profiles: list[LoadedVehicleProfile],
) -> list[ProfileCanBusConfig]:
    """Merges ``can.buses`` across profiles, sorted by bus.

    Raises when two profiles declare the same bus with differing fields.
    """
    merged: dict[int, ProfileCanBusConfig] = {}
    for profile in profiles:
        for config in profile.can_buses:
            existing = merged.get(config.bus)
            if existing is None:
                merged[config.bus] = config
            elif existing != config:
                raise VirtualVehicleError(
                    f"Conflicting CAN bus {config.bus} settings across loaded profiles"
                )
    return [merged[bus] for bus in sorted(merged)]


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


def _normalize_applies_to(raw: Any, path: str) -> ProfileAppliesTo:
    if not isinstance(raw, dict):
        raise VirtualVehicleError(f"{path} must be an object")
    return ProfileAppliesTo(
        manufacturers=_read_string_list(raw.get("manufacturers"), f"{path}.manufacturers"),
        models=_read_string_list(raw.get("models"), f"{path}.models"),
        years=_read_years(raw.get("years"), f"{path}.years"),
        trims=_read_string_list(raw.get("trims"), f"{path}.trims"),
        engines=_read_string_list(raw.get("engines"), f"{path}.engines"),
        markets=_read_string_list(raw.get("markets"), f"{path}.markets"),
    )


def _read_years(raw: Any, path: str) -> list[int | tuple[int, int]]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise VirtualVehicleError(f"{path} must be an array")
    years: list[int | tuple[int, int]] = []
    for index, entry in enumerate(raw):
        entry_path = f"{path}[{index}]"
        if isinstance(entry, dict) and "range" in entry:
            range_ = entry.get("range")
            if not isinstance(range_, list) or len(range_) != 2:
                raise VirtualVehicleError(f"{entry_path}.range must contain [start, end]")
            start = _read_number(range_[0], f"{entry_path}.range[0]")
            end = _read_number(range_[1], f"{entry_path}.range[1]")
            if end < start:
                raise VirtualVehicleError(f"{entry_path}.range end must not precede start")
            years.append((start, end))
            continue
        years.append(_read_number(entry, entry_path))
    return years


def _normalize_can_buses(raw: Any) -> list[ProfileCanBusConfig]:
    if raw is None:
        return []
    if not isinstance(raw, dict):
        raise VirtualVehicleError("can.buses must be an object keyed by logical bus")
    configs: list[ProfileCanBusConfig] = []
    for bus_key, config in raw.items():
        path = f"can.buses.{bus_key}"
        bus = _read_bus(bus_key, path)
        if not isinstance(config, dict):
            raise VirtualVehicleError(f"{path} must be an object")
        configs.append(
            ProfileCanBusConfig(
                bus=bus,
                bitrate=_read_can_bitrate(config.get("bitrate"), f"{path}.bitrate"),
                data_bitrate=(
                    _read_positive_uint32(config.get("data_bitrate"), f"{path}.data_bitrate")
                    if config.get("data_bitrate") is not None
                    else None
                ),
                fd=(
                    _read_can_bus_fd(config.get("fd"), f"{path}.fd")
                    if config.get("fd") is not None
                    else None
                ),
            )
        )
    configs.sort(key=lambda config: config.bus)
    if len({config.bus for config in configs}) != len(configs):
        raise VirtualVehicleError("can.buses declares the same logical bus more than once")
    return configs


def _read_can_bus_fd(raw: Any, path: str) -> ProfileCanBusFd | None:
    if isinstance(raw, bool):
        return ProfileCanBusFd(isotp_tx_dl=8) if raw else None
    if not isinstance(raw, dict):
        raise VirtualVehicleError(f"{path} must be a boolean or an object")
    isotp = raw.get("isotp")
    if isotp is None:
        return ProfileCanBusFd(isotp_tx_dl=8)
    if not isinstance(isotp, dict):
        raise VirtualVehicleError(f"{path}.isotp must be an object")
    return ProfileCanBusFd(isotp_tx_dl=_read_tx_dl(isotp.get("tx_dl"), f"{path}.isotp.tx_dl"))


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
        bus=(
            _read_bus(raw.get("bus"), f"endpoints.{name}.bus")
            if raw.get("bus") is not None
            else None
        ),
        extended=(
            _read_boolean(raw.get("extended"), f"endpoints.{name}.extended")
            if raw.get("extended") is not None
            else None
        ),
        fd=(
            _read_endpoint_fd(raw.get("fd"), f"endpoints.{name}.fd")
            if raw.get("fd") is not None
            else None
        ),
    )


def _read_endpoint_fd(raw: Any, path: str) -> ProfileEndpointFd:
    if isinstance(raw, bool):
        return raw
    if not isinstance(raw, dict):
        raise VirtualVehicleError(f"{path} must be a boolean or an object")
    return ProfileCanFdConfig(tx_dl=_read_tx_dl(raw.get("tx_dl"), f"{path}.tx_dl"))


def _read_tx_dl(raw: Any, path: str) -> CanFdTxDl:
    tx_dl = _read_number(raw, path)
    if tx_dl == 8:
        return 8
    if tx_dl == 64:
        return 64
    raise VirtualVehicleError(f"{path} must be 8 or 64")


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
        timeout_ms=(
            _read_number(raw.get("timeout_ms"), f"queries.{name}.timeout_ms")
            if raw.get("timeout_ms") is not None
            else None
        ),
    )


def _normalize_action(
    name: str, raw: dict[str, Any], sequences: dict[str, Any]
) -> ProfileAction | None:
    endpoint = (
        _read_string(raw.get("endpoint"), f"actions.{name}.endpoint")
        if raw.get("endpoint") is not None
        else None
    )
    if raw.get("steps") is not None:
        steps = _expand_steps(raw.get("steps") or [], sequences, endpoint)
    elif raw.get("send") is not None:
        steps = [
            _read_send_step(
                raw.get("send"),
                f"actions.{name}.send",
                endpoint,
                expect=(
                    _normalize_expect(raw.get("expect"), f"actions.{name}.expect")
                    if raw.get("expect") is not None
                    else None
                ),
            )
        ]
    else:
        # Placeholder actions without send bytes or steps are skipped.
        return None

    if not steps:
        return None

    return ProfileAction(
        name=name,
        endpoint=endpoint,
        steps=steps,
        batch=(
            _read_boolean(raw.get("batch"), f"actions.{name}.batch")
            if raw.get("batch") is not None
            else None
        ),
    )


def _read_send_step(
    raw: Any,
    path: str,
    endpoint: str | None,
    *,
    expect: ExpectPattern | None = None,
    timeout_ms: int | None = None,
    delay_ms: int | None = None,
) -> RequestStep:
    extra: dict[str, Any] = {"expect": expect, "timeout_ms": timeout_ms}
    if delay_ms is not None:
        extra["delay_ms"] = delay_ms

    if isinstance(raw, list):
        return RequestStep(
            endpoint=endpoint,
            send=bytes(_read_byte_array(raw, path)),
            **extra,
        )

    if isinstance(raw, dict) and raw.get("request_id") is not None and raw.get("request") is not None:
        return RequestStep(
            request_id=_read_number(raw.get("request_id"), f"{path}.request_id"),
            bus=(
                _read_bus(raw.get("bus"), f"{path}.bus")
                if raw.get("bus") is not None
                else None
            ),
            fd=(
                _read_boolean(raw.get("fd"), f"{path}.fd")
                if raw.get("fd") is not None
                else None
            ),
            send=bytes(_read_byte_array(raw.get("request"), f"{path}.request")),
            **extra,
        )

    raise VirtualVehicleError(f"{path} must be a byte array or {{ request_id, request }} object")


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
            bus=(
                _read_bus(monitor.get("bus"), f"signals.{name}.monitor.bus")
                if monitor.get("bus") is not None
                else None
            ),
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
    inherited_endpoint: str | None,
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
            # A sequence's own endpoint overrides the inherited action endpoint.
            sequence_endpoint = (
                _read_string(sequence.get("endpoint"), f"sequences.{ref}.endpoint")
                if sequence.get("endpoint") is not None
                else inherited_endpoint
            )
            steps.extend(
                _expand_steps(sequence.get("steps") or [], sequences, sequence_endpoint, [*seen, ref])
            )
            continue

        step_endpoint = (
            _read_string(step.get("endpoint"), "steps.endpoint")
            if step.get("endpoint") is not None
            else inherited_endpoint
        )
        steps.append(
            _read_send_step(
                step.get("send"),
                "steps.send",
                step_endpoint,
                expect=(
                    _normalize_expect(step.get("expect"), "steps.expect")
                    if step.get("expect") is not None
                    else None
                ),
                timeout_ms=(
                    _read_duration_ms(step.get("timeout_ms"), "steps.timeout_ms")
                    if step.get("timeout_ms") is not None
                    else None
                ),
                delay_ms=(
                    _read_duration_ms(step.get("delay_ms"), "steps.delay_ms")
                    if step.get("delay_ms") is not None
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


def _read_bus(raw: Any, path: str) -> int:
    value = _read_number(raw, path)
    if value < 0 or value > 0xFF:
        raise VirtualVehicleError(f"{path} must be an integer between 0 and 255")
    return value


def _read_can_bitrate(raw: Any, path: str) -> CanBitrate:
    if raw == "auto":
        return "auto"
    return _read_positive_uint32(raw, path)


def _read_duration_ms(raw: Any, path: str) -> int:
    value = _read_number(raw, path)
    if value < 0 or value > 0xFFFF:
        raise VirtualVehicleError(f"{path} must be an integer between 0 and 65535 milliseconds")
    return value


def _read_positive_uint32(raw: Any, path: str) -> int:
    value = _read_number(raw, path)
    if value <= 0 or value > 0xFFFFFFFF:
        raise VirtualVehicleError(f"{path} must be a positive 32-bit integer")
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


def _read_boolean(raw: Any, path: str) -> bool:
    if isinstance(raw, bool):
        return raw
    raise VirtualVehicleError(f"{path} must be a boolean")


def _read_string(raw: Any, path: str) -> str:
    if not isinstance(raw, str) or len(raw) == 0:
        raise VirtualVehicleError(f"{path} must be a non-empty string")
    return raw


def _read_string_list(raw: Any, path: str) -> list[str]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise VirtualVehicleError(f"{path} must be an array")
    return [_read_string(str(value) if isinstance(value, (int, float)) else value, f"{path}[{index}]") for index, value in enumerate(raw)]


def _read_string_map(raw: Any, path: str) -> dict[str, str]:
    if not isinstance(raw, dict):
        raise VirtualVehicleError(f"{path} must be an object")
    return {str(key): _read_string(value, f"{path}.{key}") for key, value in raw.items()}
