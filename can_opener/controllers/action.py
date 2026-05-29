from __future__ import annotations

import asyncio

from can_opener.dbc.controller import DbcController
from can_opener.dbc.types import ActionOptions, CanFrame, VehicleSignal
from can_opener.profile.capabilities import CapabilityRegistry
from can_opener.profile.request_builder import (
    assert_expected_response,
    build_step_frame,
    response_bounds,
)
from can_opener.profile.types import ProfileAction, ProfileEndpoint
from can_opener.transport.types import VehicleRequest, VehicleTransport


class ActionController:
    def __init__(
        self,
        dbc: DbcController,
        transport: VehicleTransport,
        capabilities: CapabilityRegistry | None = None,
    ) -> None:
        self._dbc = dbc
        self._transport = transport
        self._capabilities = capabilities
        self._scheduled: set[asyncio.Task[None]] = set()

    async def send(self, signal: VehicleSignal, opts: ActionOptions) -> None:
        frame = self._apply_mask(self._dbc.encode_signal(signal.name, opts.value), opts)
        await self._transport.send_request(
            VehicleRequest(
                signal_name=signal.name,
                tx_frame=frame,
                expect_can_response=False,
                action=opts,
            )
        )

        if opts.frequency_hz is None or opts.duration_ms is None or opts.duration_ms <= 0:
            return
        task = asyncio.create_task(self._repeat_send(signal.name, frame, opts))
        self._scheduled.add(task)
        task.add_done_callback(lambda done: self._scheduled.discard(done))

    def clear_scheduled(self) -> None:
        for task in list(self._scheduled):
            task.cancel()
        self._scheduled.clear()

    async def run(self, action: ProfileAction) -> bool | None:
        verified = False
        for step in action.steps:
            endpoint_name = step.endpoint or action.endpoint
            endpoint = self._resolve_endpoint(endpoint_name) if endpoint_name is not None else None
            if endpoint is None and step.request_id is None:
                raise ValueError(f"Action {action.name} step does not declare an endpoint")
            if endpoint is None and step.expect is not None:
                raise ValueError(f"Action {action.name} step with expect requires an endpoint")
            bounds = response_bounds(endpoint) if endpoint is not None else {}
            payload = await self._transport.send_request(
                VehicleRequest(
                    signal_name=action.name,
                    tx_frame=build_step_frame(step, endpoint) if endpoint is not None else build_step_frame(step, _direct_endpoint(step.request_id)),
                    expect_can_response=step.expect is not None,
                    response_id_start=bounds.get("response_id_start"),
                    response_id_end=bounds.get("response_id_end"),
                    timeout_ms=endpoint.timeout_ms if endpoint is not None else None,
                )
            )
            if step.expect is None:
                continue
            if payload is None:
                return False
            try:
                assert_expected_response(step.expect, payload)
            except Exception:
                return False
            verified = True
        return True if verified else None

    async def _repeat_send(self, signal_name: str, frame: CanFrame, opts: ActionOptions) -> None:
        assert opts.frequency_hz is not None and opts.duration_ms is not None
        interval = max(0.001, 1 / opts.frequency_hz)
        end_at = asyncio.get_running_loop().time() + opts.duration_ms / 1000
        try:
            while asyncio.get_running_loop().time() < end_at:
                await asyncio.sleep(interval)
                await self._transport.send_request(
                    VehicleRequest(
                        signal_name=signal_name,
                        tx_frame=frame.clone(),
                        expect_can_response=False,
                        action=opts,
                    )
                )
        except asyncio.CancelledError:
            return

    @staticmethod
    def _apply_mask(frame: CanFrame, opts: ActionOptions) -> CanFrame:
        if opts.mask is None:
            return frame
        data = bytearray(frame.data)
        value = opts.mask if isinstance(opts.value, bool) and opts.value else int(opts.value) & opts.mask
        data[0] = (data[0] & ~opts.mask) | value
        return CanFrame(can_id=frame.can_id, data=bytes(data), dlc=frame.dlc, extended=frame.extended)

    def _resolve_endpoint(self, name: str):
        if self._capabilities is None:
            raise ValueError(f"No capability registry configured for endpoint {name}")
        return self._capabilities.resolve_endpoint(name)


def _direct_endpoint(request_id: int | None):
    if request_id is None:
        raise ValueError("Direct action step requires request_id")
    return ProfileEndpoint(name="direct", request_id=request_id)
