from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Callable

from can_opener.controllers.action import ActionController
from can_opener.controllers.query import QueryController
from can_opener.controllers.subscription import SubscriptionController, SubscriptionRequest
from can_opener.dbc.controller import DbcController
from can_opener.dbc.types import ActionOptions, CanFrame, DbcFile, PollingOptions, QuerySubscriptionHandle, VehicleSignalState
from can_opener.errors import SignalProtocolError, VehicleConnectionError, VirtualVehicleError
from can_opener.profile.capabilities import CapabilityRegistry
from can_opener.profile.types import ProfileCanBusConfig
from can_opener.transport.types import (
    BusTiming,
    ConfigureBusRequest,
    VehicleTransport,
    transport_supports_configure_bus,
)

from .state import VehicleState


@dataclass(slots=True)
class VirtualVehicleInternals:
    subscriptions: SubscriptionController
    queries: QueryController
    actions: ActionController
    capabilities: CapabilityRegistry
    dispose_frames: Callable[[], None]
    # What the transport reported when each bus's profile timing was applied.
    bus_timing: dict[int, BusTiming] = field(default_factory=dict)


class VirtualVehicle:
    def __init__(
        self,
        id: str,
        state: VehicleState,
        dbc: DbcController,
        transport: VehicleTransport,
        internals: VirtualVehicleInternals,
    ) -> None:
        self.id = id
        self.state = state
        self.dbc = dbc
        self.transport = transport
        self._internals = internals
        self._bus_timing_lock = asyncio.Lock()

    async def query(self, signal_name: str) -> Any:
        if self._internals.capabilities.has_profiles():
            return await self._internals.queries.request_profile(
                self._internals.capabilities.resolve_query(signal_name)
            )
        raise VirtualVehicleError(f"Query {signal_name} requires a vehicle profile")

    async def subscribe(self, signal_name_or_names: str | list[str]) -> bool:
        if isinstance(signal_name_or_names, list):
            return await self._internals.subscriptions.add_many(
                [self._resolve_subscription_request(signal_name) for signal_name in signal_name_or_names]
            )
        return await self._internals.subscriptions.add_many(
            [self._resolve_subscription_request(signal_name_or_names)]
        )

    async def unsubscribe(self, signal_name_or_names: str | list[str]) -> None:
        if isinstance(signal_name_or_names, list):
            await self._internals.subscriptions.cancel_many(signal_name_or_names)
            return
        await self._internals.subscriptions.cancel(signal_name_or_names)

    def subscription_count(self) -> int:
        return self._internals.subscriptions.count()

    async def subscribe_query(
        self, signal_name: str, opts: PollingOptions | None = None
    ) -> QuerySubscriptionHandle:
        if self._internals.capabilities.has_profiles():
            return await self._internals.queries.subscribe(
                self._internals.capabilities.resolve_query(signal_name), opts
            )
        raise VirtualVehicleError(f"Query subscription {signal_name} requires a vehicle profile")

    def unsubscribe_query(self, handle: QuerySubscriptionHandle) -> None:
        self._internals.queries.cancel_handle(handle)

    async def action(self, name: str, opts: ActionOptions | None = None) -> bool | None:
        if self._internals.capabilities.has_profiles():
            if opts is not None:
                raise VirtualVehicleError(f"Profile action {name} does not accept raw frame action options")
            return await self._internals.actions.run(self._internals.capabilities.resolve_action(name))

        if opts is None:
            raise VirtualVehicleError(f"Raw DBC action {name} requires action options")
        signal = self.dbc.resolve(name)
        if signal.protocol != "frame":
            raise SignalProtocolError(name, "frame", signal.protocol)
        await self._internals.actions.send(signal, opts)
        return None

    def reload_dbc(self, files: list[DbcFile]) -> None:
        self.state.clear()
        self.dbc.load(files)
        self._internals.capabilities.clear()
        self._internals.subscriptions.cancel_all()
        self._internals.queries.cancel_all_subscriptions()
        self._internals.queries.clear_pending()
        self._internals.actions.clear_scheduled()

    async def configure_buses(self) -> dict[int, BusTiming]:
        """Applies bus timing declared by the loaded profiles.

        Buses whose requested timing has not changed since the last call are
        skipped, so a profile reload does not repeat an auto-detect. A failed
        ``auto`` detection leaves the adapter on its previous timing and is
        reported, not raised; any other failure raises
        ``VehicleConnectionError``. Transports without ``configure_bus`` are
        left on their default timing. Calls run one at a time.
        """
        async with self._bus_timing_lock:
            pending = [
                config
                for config in self._internals.capabilities.bus_configs()
                if not self._already_applied(config)
            ]
            if pending and transport_supports_configure_bus(self.transport):
                for config in pending:
                    await self._apply_bus_timing(config)
        return self.bus_timing()

    def bus_timing(self) -> dict[int, BusTiming]:
        """Timing the adapter reported per configured bus, keyed by bus number."""
        return dict(sorted(self._internals.bus_timing.items()))

    def _already_applied(self, config: ProfileCanBusConfig) -> bool:
        report = self._internals.bus_timing.get(config.bus)
        return report is not None and report.requested == config

    async def _apply_bus_timing(self, config: ProfileCanBusConfig) -> None:
        response = await self.transport.configure_bus(  # type: ignore[attr-defined]
            ConfigureBusRequest(
                bus=config.bus,
                bitrate=config.bitrate,
                data_bitrate=config.data_bitrate,
                fd=config.fd,
            )
        )
        if response.status == "error":
            detail = f": {response.message}" if response.message else ""
            raise VehicleConnectionError(
                f"Failed to configure CAN bus {config.bus}: {response.status}{detail}"
            )
        self._internals.bus_timing[config.bus] = BusTiming(
            requested=config,
            status=response.status,
            bitrate=response.bitrate,
            data_bitrate=response.data_bitrate,
        )

    def handle_frame(self, frame: CanFrame) -> None:
        self._internals.subscriptions.handle_frame(frame)

    async def disconnect(self) -> None:
        self._internals.subscriptions.cancel_all()
        self._internals.queries.cancel_all_subscriptions()
        self._internals.queries.clear_pending()
        self._internals.actions.clear_scheduled()
        self._internals.dispose_frames()
        await self.transport.disconnect()

    def _resolve_subscription_request(self, signal_name: str) -> SubscriptionRequest:
        if self._internals.capabilities.has_profiles():
            monitor = self._internals.capabilities.resolve_signal(signal_name)
            return SubscriptionRequest(
                state=VehicleSignalState(
                    name=monitor.name,
                    signal=self.dbc.resolve_message_signal(monitor.message, monitor.signal),
                ),
                normalize=monitor.normalize,
                bus=monitor.bus,
            )

        state = self.dbc.resolve_state(signal_name)
        if state.signal.protocol != "frame":
            raise SignalProtocolError(signal_name, "frame", state.signal.protocol)
        return SubscriptionRequest(state=state)
