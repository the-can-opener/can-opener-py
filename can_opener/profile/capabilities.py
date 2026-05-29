from __future__ import annotations

from can_opener.errors import UnknownVehicleSignalError, VirtualVehicleError

from .types import (
    LoadedVehicleProfile,
    ProfileAction,
    ProfileEndpoint,
    ProfileMonitorSignal,
    ProfileQuery,
)


class CapabilityRegistry:
    def __init__(self) -> None:
        self._endpoints: dict[str, ProfileEndpoint] = {}
        self._queries: dict[str, ProfileQuery] = {}
        self._actions: dict[str, ProfileAction] = {}
        self._signals: dict[str, ProfileMonitorSignal] = {}
        self._profile_backed = False

    def load(self, profiles: list[LoadedVehicleProfile]) -> None:
        self.clear()
        self._profile_backed = len(profiles) > 0
        for profile in profiles:
            self._endpoints.update({endpoint.name: endpoint for endpoint in profile.endpoints})
            self._queries.update({query.name: query for query in profile.queries})
            self._actions.update({action.name: action for action in profile.actions})
            self._signals.update({signal.name: signal for signal in profile.signals})

    def clear(self) -> None:
        self._endpoints.clear()
        self._queries.clear()
        self._actions.clear()
        self._signals.clear()
        self._profile_backed = False

    def has_profiles(self) -> bool:
        return self._profile_backed

    def resolve_endpoint(self, name: str) -> ProfileEndpoint:
        endpoint = self._endpoints.get(name)
        if endpoint is None:
            raise VirtualVehicleError(f"Unknown profile endpoint: {name}")
        return endpoint

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

    def resolve_signal(self, name: str) -> ProfileMonitorSignal:
        signal = self._signals.get(name)
        if signal is None:
            raise UnknownVehicleSignalError(name)
        return signal
