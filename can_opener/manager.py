from __future__ import annotations

from dataclasses import dataclass, field

from can_opener.controllers.action import ActionController
from can_opener.controllers.query import QueryController
from can_opener.controllers.subscription import SubscriptionController
from can_opener.dbc.controller import DbcController, DbcControllerOptions
from can_opener.dbc.types import DbcFile
from can_opener.errors import VehicleConnectionError
from can_opener.profile.capabilities import CapabilityRegistry
from can_opener.profile.loader import ProfileLoader
from can_opener.profile.types import VehicleProfileSource
from can_opener.transport.types import VehicleTransport
from can_opener.vehicle.state import VehicleState
from can_opener.vehicle.vehicle import VirtualVehicle, VirtualVehicleInternals


@dataclass(slots=True)
class ConnectVehicleOptions:
    id: str
    transport: VehicleTransport
    dbc_files: list[DbcFile] = field(default_factory=list)
    profiles: list[VehicleProfileSource] = field(default_factory=list)
    dbc: DbcControllerOptions | None = None


class VirtualVehicleManager:
    def __init__(self) -> None:
        self._vehicles: dict[str, VirtualVehicle] = {}

    async def connect(self, options: ConnectVehicleOptions) -> VirtualVehicle:
        if options.id in self._vehicles:
            raise VehicleConnectionError(f"Vehicle already connected: {options.id}")

        state = VehicleState()
        dbc = DbcController(options.dbc)
        capabilities = CapabilityRegistry()
        profiles = ProfileLoader().load(options.profiles)
        capabilities.load(profiles)
        dbc.load([*options.dbc_files, *[file for profile in profiles for file in profile.dbc_files]])

        subscriptions = SubscriptionController(state, dbc, options.transport)
        queries = QueryController(state, dbc, options.transport, capabilities)
        actions = ActionController(dbc, options.transport, capabilities)

        await options.transport.connect()
        dispose_frames = options.transport.on_monitor_snapshot(
            lambda snapshot: [subscriptions.handle_frame(frame) for frame in snapshot.frames]
        )

        vehicle = VirtualVehicle(
            options.id,
            state,
            dbc,
            options.transport,
            VirtualVehicleInternals(
                subscriptions=subscriptions,
                queries=queries,
                actions=actions,
                capabilities=capabilities,
                dispose_frames=dispose_frames,
            ),
        )
        self._vehicles[options.id] = vehicle
        return vehicle

    def get(self, id: str) -> VirtualVehicle | None:
        return self._vehicles.get(id)

    async def disconnect(self, id: str) -> None:
        vehicle = self._vehicles.get(id)
        if vehicle is None:
            return
        await vehicle.disconnect()
        self._vehicles.pop(id, None)

    async def disconnect_all(self) -> None:
        for id in list(self._vehicles.keys()):
            await self.disconnect(id)

    def list(self) -> list[VirtualVehicle]:
        return list(self._vehicles.values())
