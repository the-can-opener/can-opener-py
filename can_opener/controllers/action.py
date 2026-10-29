from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Literal

from can_opener.dbc.controller import DbcController
from can_opener.dbc.types import ActionOptions, CanFrame, VehicleSignal
from can_opener.errors import BatchUnsupportedError, VirtualVehicleError
from can_opener.profile.capabilities import CapabilityRegistry
from can_opener.profile.request_builder import (
    assert_expected_response,
    build_step_frame,
    can_fd_request,
    response_bounds,
)
from can_opener.profile.types import ProfileAction, RequestStep, ResolvedProfileEndpoint
from can_opener.transport.types import (
    BatchRequest,
    BatchStep,
    BatchStepResult,
    VehicleRequest,
    VehicleTransport,
    transport_supports_batch,
)

StepVerdict = Literal["verified", "unverified", "mismatch"]


@dataclass(frozen=True, slots=True)
class _ResolvedStep:
    step: RequestStep
    request: VehicleRequest


def _group_steps_by_bus(steps: list[_ResolvedStep]) -> list[list[_ResolvedStep]]:
    """Splits steps into runs of consecutive steps that share a logical bus."""
    groups: list[list[_ResolvedStep]] = []
    current: list[_ResolvedStep] = []
    current_bus: int | None = None
    for resolved in steps:
        bus = resolved.request.bus if resolved.request.bus is not None else 0
        if current and bus != current_bus:
            groups.append(current)
            current = []
        current.append(resolved)
        current_bus = bus
    if current:
        groups.append(current)
    return groups


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
        """Runs a profile action.

        Returns ``True`` when a step's expected response was verified,
        ``False`` when a response did not match (or none arrived for a step
        that expected one), and ``None`` when the action declares no
        expectations to verify.
        """
        resolved = [self._resolve_step(action, step) for step in action.steps]
        use_batch = (
            transport_supports_batch(self._transport)
            and action.batch is not False
            and len(resolved) > 1
        )

        verified = False
        groups = _group_steps_by_bus(resolved) if use_batch else [[step] for step in resolved]
        for group_index, group in enumerate(groups):
            if use_batch and len(group) > 1:
                verdicts = await self._run_batched_group_or_fallback(action, group)
            else:
                verdicts = await self._run_sequential_group(action, group)
            for verdict in verdicts:
                if verdict == "mismatch":
                    return False
                if verdict == "verified":
                    verified = True

            # Honour the trailing gap of a group when the next group runs on
            # another bus (or another transport transaction).
            gap_ms = group[-1].step.delay_ms
            if gap_ms > 0 and group_index < len(groups) - 1:
                await asyncio.sleep(gap_ms / 1000)

        return True if verified else None

    async def _run_sequential_group(
        self, action: ProfileAction, group: list[_ResolvedStep]
    ) -> list[StepVerdict]:
        verdicts: list[StepVerdict] = []
        for index, resolved in enumerate(group):
            payload = await self._transport.send_request(resolved.request)
            verdict = self._verify_step(resolved.step, payload)
            verdicts.append(verdict)
            # Stop before transmitting further steps once one has been rejected.
            if verdict == "mismatch":
                break
            gap_ms = resolved.step.delay_ms
            if gap_ms > 0 and index < len(group) - 1:
                await asyncio.sleep(gap_ms / 1000)
        return verdicts

    async def _run_batched_group_or_fallback(
        self, action: ProfileAction, group: list[_ResolvedStep]
    ) -> list[StepVerdict]:
        # A transport may only discover at runtime that the connected device
        # lacks batch support; nothing has been transmitted then, so replay the
        # same steps one request at a time.
        try:
            return await self._run_batched_group(action, group)
        except BatchUnsupportedError:
            return await self._run_sequential_group(action, group)

    async def _run_batched_group(
        self, action: ProfileAction, group: list[_ResolvedStep]
    ) -> list[StepVerdict]:
        """Sends a same-bus group as one device transaction.

        The device stops at the first transport failure, but it cannot evaluate
        ``expect`` patterns, so a mismatching response does not prevent later
        steps in the same batch from having been transmitted. Profiles that need
        strict gating between steps should set ``batch: false``.
        """
        results: list[BatchStepResult] = await self._transport.send_request_batch(  # type: ignore[attr-defined]
            BatchRequest(
                steps=[
                    BatchStep(request=resolved.request, delay_ms=resolved.step.delay_ms)
                    for resolved in group
                ]
            )
        )
        if len(results) != len(group):
            raise VirtualVehicleError(
                f"Action {action.name} batch returned {len(results)} results for {len(group)} steps"
            )

        verdicts: list[StepVerdict] = []
        for resolved, result in zip(group, results):
            if not result.ok:
                raise VirtualVehicleError(
                    f"Action {action.name} step failed: {result.error or 'transport error'}"
                )
            verdict = self._verify_step(resolved.step, result.response)
            verdicts.append(verdict)
            if verdict == "mismatch":
                break
        return verdicts

    @staticmethod
    def _verify_step(step: RequestStep, payload: bytes | None) -> StepVerdict:
        if step.expect is None:
            return "unverified"
        if payload is None:
            return "mismatch"
        try:
            assert_expected_response(step.expect, payload)
        except ValueError:
            return "mismatch"
        return "verified"

    def _resolve_step(self, action: ProfileAction, step: RequestStep) -> _ResolvedStep:
        endpoint_name = step.endpoint or action.endpoint
        endpoint: ResolvedProfileEndpoint | None = (
            self._resolve_endpoint(endpoint_name)
            if step.request_id is None and endpoint_name is not None
            else None
        )
        if step.request_id is None and endpoint is None:
            raise ValueError(f"Action {action.name} step does not declare an endpoint or request_id")
        if endpoint is None and step.expect is not None:
            raise ValueError(f"Action {action.name} step with expect requires an endpoint")

        timeout_ms = step.timeout_ms
        if timeout_ms is None and endpoint is not None:
            timeout_ms = endpoint.timeout_ms
        if endpoint is not None:
            can_fd = endpoint.can_fd
            subject = f"Endpoint {endpoint.name}"
        else:
            can_fd = (
                self._capabilities.inline_step_can_fd(step, action.name)
                if self._capabilities is not None
                else None
            )
            subject = f"Action {action.name}"
        tx_frame = build_step_frame(step, endpoint, can_fd)
        bounds = response_bounds(endpoint) if endpoint is not None else {}
        return _ResolvedStep(
            step=step,
            request=VehicleRequest(
                signal_name=action.name,
                tx_frame=tx_frame,
                expect_can_response=step.expect is not None,
                response_id_start=bounds.get("response_id_start"),
                response_id_end=bounds.get("response_id_end"),
                timeout_ms=timeout_ms,
                bus=tx_frame.bus,
                can_fd=can_fd_request(can_fd, tx_frame, subject),
            ),
        )

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

    def _resolve_endpoint(self, name: str) -> ResolvedProfileEndpoint:
        if self._capabilities is None:
            raise ValueError(f"No capability registry configured for endpoint {name}")
        return self._capabilities.resolve_endpoint(name)
