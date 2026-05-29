from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from can_opener.dbc.controller import DbcController
from can_opener.dbc.types import PollingOptions, QuerySubscriptionHandle
from can_opener.profile.capabilities import CapabilityRegistry
from can_opener.profile.normalize import apply_value_normalization
from can_opener.profile.request_builder import (
    assert_expected_response,
    build_request_frame,
    response_bounds,
    strip_expected_prefix,
)
from can_opener.profile.types import ProfileQuery, QueryDecoder
from can_opener.transport.types import VehicleRequest, VehicleTransport
from can_opener.vehicle.state import VehicleState

DEFAULT_QUERY_SUBSCRIPTION_FREQUENCY_HZ = 1.0


@dataclass(slots=True)
class _ActiveQuerySubscription:
    handle: QuerySubscriptionHandle
    query: ProfileQuery
    task: asyncio.Task[None]
    in_flight: bool = False


class QueryController:
    def __init__(
        self,
        state: VehicleState,
        dbc: DbcController,
        transport: VehicleTransport,
        capabilities: CapabilityRegistry | None = None,
    ) -> None:
        self._state = state
        self._dbc = dbc
        self._transport = transport
        self._capabilities = capabilities
        self._pending: set[asyncio.Task[Any]] = set()
        self._active_by_signal: dict[str, _ActiveQuerySubscription] = {}
        self._next_subscription_id = 1

    async def request_profile(self, query: ProfileQuery) -> Any:
        endpoint = self._resolve_endpoint(query.endpoint)
        frame = build_request_frame(endpoint, query.send)
        kwargs = response_bounds(endpoint)
        req = VehicleRequest(
            signal_name=query.name,
            tx_frame=frame,
            expect_can_response=True,
            response_id_start=kwargs.get("response_id_start"),
            response_id_end=kwargs.get("response_id_end"),
            timeout_ms=endpoint.timeout_ms,
        )
        task = asyncio.create_task(self._transport.send_request(req))
        self._pending.add(task)
        try:
            payload = await task
        finally:
            self._pending.discard(task)

        if payload is None:
            raise ValueError(f"No response payload returned for query {query.name}")
        assert_expected_response(query.expect, payload)
        value = self._decode_profile_query(query, payload)
        self._state.update(query.name, value)
        return value

    async def subscribe(
        self, query: ProfileQuery, opts: PollingOptions | None = None
    ) -> QuerySubscriptionHandle:
        opts = opts or PollingOptions()
        self.cancel(query.name)
        frequency_hz = opts.frequency_hz or DEFAULT_QUERY_SUBSCRIPTION_FREQUENCY_HZ
        if frequency_hz <= 0:
            raise ValueError(f"Query subscription frequency must be greater than 0, received {frequency_hz}")

        handle = QuerySubscriptionHandle(id=str(self._next_subscription_id), signal_name=query.name)
        self._next_subscription_id += 1
        active = _ActiveQuerySubscription(
            handle=handle,
            query=query,
            task=asyncio.create_task(self._poll_loop(query.name, frequency_hz, opts.duration_ms)),
        )
        self._active_by_signal[query.name] = active
        await self._poll(active)
        return handle

    def clear_pending(self) -> None:
        for task in list(self._pending):
            task.cancel()
        self._pending.clear()

    def cancel(self, signal_name: str) -> None:
        active = self._active_by_signal.pop(signal_name, None)
        if active is not None:
            active.task.cancel()

    def cancel_handle(self, handle: QuerySubscriptionHandle) -> None:
        active = self._active_by_signal.get(handle.signal_name)
        if active is None or active.handle.id != handle.id:
            return
        self.cancel(handle.signal_name)

    def cancel_all_subscriptions(self) -> None:
        for signal_name in list(self._active_by_signal.keys()):
            self.cancel(signal_name)

    async def _poll_loop(self, signal_name: str, frequency_hz: float, duration_ms: int | None) -> None:
        started = asyncio.get_running_loop().time()
        interval = 1 / frequency_hz
        try:
            while True:
                if duration_ms is not None and (
                    asyncio.get_running_loop().time() - started
                ) * 1000 >= duration_ms:
                    self.cancel(signal_name)
                    return
                await asyncio.sleep(interval)
                active = self._active_by_signal.get(signal_name)
                if active is not None:
                    await self._poll(active)
        except asyncio.CancelledError:
            return

    async def _poll(self, active: _ActiveQuerySubscription) -> None:
        if active.in_flight:
            return
        active.in_flight = True
        try:
            await self.request_profile(active.query)
        except Exception:
            pass
        finally:
            active.in_flight = False

    def _resolve_endpoint(self, name: str):
        if self._capabilities is None:
            raise ValueError(f"No capability registry configured for endpoint {name}")
        return self._capabilities.resolve_endpoint(name)

    def _decode_profile_query(self, query: ProfileQuery, payload: bytes) -> Any:
        if query.decoder is None:
            return apply_value_normalization(strip_expected_prefix(query.expect, payload), query.normalize)
        decoder_payload = payload if query.decoder.type == "dbc" else strip_expected_prefix(query.expect, payload)
        return apply_value_normalization(
            _decode_profile_payload(self._dbc, query.decoder, decoder_payload, query.length),
            query.normalize,
        )


def _decode_profile_payload(
    dbc: DbcController, decoder: QueryDecoder, payload: bytes, query_length: int | None
) -> Any:
    if decoder.type == "dbc":
        return dbc.decode_message_signal(decoder.message, decoder.signal, payload)
    if decoder.type == "ascii":
        length = decoder.length if decoder.length is not None else query_length
        bytes_ = payload if length is None else payload[:length]
        return bytes_.decode(errors="replace").rstrip("\0")
    if decoder.type == "bytes":
        return payload if decoder.length is None else payload[: decoder.length]
    raise AssertionError(f"Unhandled decoder: {decoder}")
