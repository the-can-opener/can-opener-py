from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from can_opener.dbc.controller import DbcController
from can_opener.dbc.types import CanFrame, VehicleSignal, VehicleSignalState
from can_opener.profile.normalize import apply_value_normalization
from can_opener.profile.types import ProfileValueNormalization
from can_opener.transport.types import MonitorControlRequest, VehicleTransport
from can_opener.vehicle.state import VehicleState


@dataclass(slots=True)
class SubscriptionRequest:
    state: VehicleSignalState
    normalize: ProfileValueNormalization | None = None


@dataclass(slots=True)
class _ActiveSubscription:
    state: VehicleSignalState
    normalize: ProfileValueNormalization | None = None


class SubscriptionController:
    def __init__(self, state: VehicleState, dbc: DbcController, transport: VehicleTransport) -> None:
        self._state = state
        self._dbc = dbc
        self._transport = transport
        self._active_by_signal: dict[str, _ActiveSubscription] = {}
        self._active_by_can_id: dict[int, dict[str, _ActiveSubscription]] = {}
        self._monitored_can_ids: set[int] = set()

    async def add(self, signal: VehicleSignal) -> bool:
        return await self.add_many([SubscriptionRequest(VehicleSignalState(signal.name, signal))])

    async def add_many(self, requests: list[SubscriptionRequest]) -> bool:
        affected_can_ids: set[int] = set()
        for request in requests:
            self._remove_active(request.state.name, affected_can_ids)
        for request in requests:
            active = _ActiveSubscription(state=request.state, normalize=request.normalize)
            self._active_by_signal[request.state.name] = active
            self._active_by_can_id.setdefault(request.state.signal.can_id, {})[request.state.name] = active
            affected_can_ids.add(request.state.signal.can_id)
        await self._sync_transport_subscriptions(affected_can_ids)
        return True

    def handle_frame(self, frame: CanFrame) -> None:
        active_signals = self._active_by_can_id.get(frame.can_id)
        if active_signals is None:
            return
        decoded = self._dbc.decode_frame(frame)
        for decoded_value in decoded:
            for active in active_signals.values():
                if active.state.signal.name != decoded_value.signal.name:
                    continue
                state_value = _decode_subscription_state_value(active.state, decoded_value.value)
                self._state.update(
                    active.state.name,
                    apply_value_normalization(state_value, active.normalize),
                )

    async def cancel(self, signal_name: str) -> None:
        await self.cancel_many([signal_name])

    async def cancel_many(self, signal_names: list[str]) -> None:
        affected_can_ids: set[int] = set()
        for signal_name in signal_names:
            self._remove_active(signal_name, affected_can_ids)
        await self._sync_transport_subscriptions(affected_can_ids)

    def count(self) -> int:
        return len(self._active_by_signal)

    def cancel_all(self) -> None:
        self._active_by_signal.clear()
        self._active_by_can_id.clear()
        self._monitored_can_ids.clear()
        # Fire-and-forget mirrors the JS implementation used during teardown.
        import asyncio

        try:
            asyncio.create_task(self._transport.update_monitor(MonitorControlRequest("clear")))
        except RuntimeError:
            pass

    def _remove_active(self, signal_name: str, affected_can_ids: set[int]) -> None:
        active = self._active_by_signal.pop(signal_name, None)
        if active is None:
            return
        frame_subscriptions = self._active_by_can_id.get(active.state.signal.can_id)
        if frame_subscriptions is not None:
            frame_subscriptions.pop(signal_name, None)
            if not frame_subscriptions:
                self._active_by_can_id.pop(active.state.signal.can_id, None)
        affected_can_ids.add(active.state.signal.can_id)

    async def _sync_transport_subscriptions(self, can_ids: set[int]) -> None:
        for can_id in can_ids:
            await self._sync_transport_subscription(can_id)

    async def _sync_transport_subscription(self, can_id: int) -> None:
        frame_subscriptions = self._active_by_can_id.get(can_id)
        if not frame_subscriptions:
            if can_id in self._monitored_can_ids:
                await self._apply_monitor_update(MonitorControlRequest("remove", [can_id]))
                self._monitored_can_ids.discard(can_id)
            return
        if can_id not in self._monitored_can_ids:
            await self._apply_monitor_update(MonitorControlRequest("add", [can_id]))
            self._monitored_can_ids.add(can_id)

    async def _apply_monitor_update(self, req: MonitorControlRequest) -> None:
        response = await self._transport.update_monitor(req)
        if response.status != "ok":
            raise ValueError(f"Monitor {req.operation} failed with status {response.status}")


def _decode_subscription_state_value(state: VehicleSignalState, value: Any) -> Any:
    if state.enum_value is None:
        return value
    if isinstance(value, str):
        return 1 if value == state.name else 0
    return 1 if value == state.enum_value else 0
