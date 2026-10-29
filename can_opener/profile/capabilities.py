from __future__ import annotations

from dataclasses import replace

from can_opener.errors import UnknownVehicleSignalError, VirtualVehicleError

from .loader import merge_profile_can_bus_configs
from .types import (
    LoadedVehicleProfile,
    ProfileAction,
    ProfileCanBusConfig,
    ProfileCanFdConfig,
    ProfileEndpoint,
    ProfileMonitorSignal,
    ProfileQuery,
    RequestStep,
    ResolvedProfileEndpoint,
)


class CapabilityRegistry:
    def __init__(self) -> None:
        self._endpoints: dict[str, ResolvedProfileEndpoint] = {}
        self._queries: dict[str, ProfileQuery] = {}
        self._actions: dict[str, ProfileAction] = {}
        self._signals: dict[str, ProfileMonitorSignal] = {}
        self._buses: dict[int, ProfileCanBusConfig] = {}
        self._profile_backed = False

    def load(self, profiles: list[LoadedVehicleProfile]) -> None:
        """Loads profiles in order; later profiles override earlier names.

        Raises when profiles disagree on a bus, or when anything sends CAN FD
        on a bus no profile declares ``fd``.
        """
        buses = merge_profile_can_bus_configs(profiles)
        self.clear()
        self._profile_backed = len(profiles) > 0
        self._buses = {config.bus: config for config in buses}

        endpoints: dict[str, ProfileEndpoint] = {}
        for profile in profiles:
            endpoints.update({endpoint.name: endpoint for endpoint in profile.endpoints})
            self._queries.update({query.name: query for query in profile.queries})
            self._actions.update({action.name: action for action in profile.actions})
            self._signals.update({signal.name: signal for signal in profile.signals})

        self._endpoints = {
            name: self._resolve_endpoint_fd(endpoint) for name, endpoint in endpoints.items()
        }
        for action in self._actions.values():
            for step in action.steps:
                if step.request_id is not None:
                    self.inline_step_can_fd(step, action.name)

    def clear(self) -> None:
        self._endpoints.clear()
        self._queries.clear()
        self._actions.clear()
        self._signals.clear()
        self._buses.clear()
        self._profile_backed = False

    def has_profiles(self) -> bool:
        return self._profile_backed

    def bus_configs(self) -> list[ProfileCanBusConfig]:
        """Bus settings merged across the loaded profiles, sorted by bus."""
        return [self._buses[bus] for bus in sorted(self._buses)]

    def resolve_endpoint(self, name: str) -> ResolvedProfileEndpoint:
        endpoint = self._endpoints.get(name)
        if endpoint is None:
            raise VirtualVehicleError(f"Unknown profile endpoint: {name}")
        return endpoint

    def inline_step_can_fd(self, step: RequestStep, action_name: str) -> ProfileCanFdConfig | None:
        """CAN FD setting for an inline ``request_id`` step.

        Its own ``fd``, else the bus default. The step's bytes are sent as one
        frame, so FD uses ``tx_dl`` 8.
        """
        bus = step.bus if step.bus is not None else 0
        bus_config = self._buses.get(bus)
        bus_fd = bus_config.fd if bus_config is not None else None
        wants_fd = step.fd if step.fd is not None else bus_fd is not None
        if not wants_fd:
            return None
        if bus_fd is None:
            raise _fd_on_classic_bus_error(f"Action {action_name}", bus)
        return ProfileCanFdConfig(tx_dl=8)

    def resolve_query(self, name: str) -> ProfileQuery:
        query = self._queries.get(name)
        if query is None:
            raise UnknownVehicleSignalError(name)
        return query

    def resolve_action(self, name: str) -> ProfileAction:
        action = self._actions.get(name)
        if action is None:
            raise VirtualVehicleError(f"Unknown vehicle action: {name}")
        return action

    def has_action(self, name: str) -> bool:
        return name in self._actions

    def resolve_signal(self, name: str) -> ProfileMonitorSignal:
        signal = self._signals.get(name)
        if signal is None:
            raise UnknownVehicleSignalError(name)
        return signal

    def has_signal(self, name: str) -> bool:
        return name in self._signals

    def _resolve_endpoint_fd(self, endpoint: ProfileEndpoint) -> ResolvedProfileEndpoint:
        bus = endpoint.bus if endpoint.bus is not None else 0
        bus_config = self._buses.get(bus)
        bus_fd = bus_config.fd if bus_config is not None else None
        fd = endpoint.fd if endpoint.fd is not None else bus_fd is not None
        resolved = ResolvedProfileEndpoint(
            name=endpoint.name,
            request_id=endpoint.request_id,
            response_ranges=endpoint.response_ranges,
            timeout_ms=endpoint.timeout_ms,
            bus=endpoint.bus,
            extended=endpoint.extended,
            fd=endpoint.fd,
        )
        if fd is False:
            return resolved
        if bus_fd is None:
            raise _fd_on_classic_bus_error(f"Endpoint {endpoint.name}", bus)
        can_fd = ProfileCanFdConfig(tx_dl=bus_fd.isotp_tx_dl) if fd is True else fd
        return replace(resolved, can_fd=can_fd)


def _fd_on_classic_bus_error(subject: str, bus: int) -> VirtualVehicleError:
    return VirtualVehicleError(
        f"{subject} sends CAN FD on bus {bus}, but no loaded profile declares can.buses.{bus}.fd"
    )
