# can-opener-py

`can-opener-py` is a Python port of `can-opener-js`: a signal-first virtual
vehicle library that turns names like `RPM`, `SPEED`, `HORN`, and
`LEFT_SIGNAL` into DBC-aware queries, actions, subscriptions, and state.

It keeps the same separation of concerns as the TypeScript library:

- DBC files define CAN frame bit layout, scaling, byte order, units, and enums.
- YAML vehicle profiles define executable capabilities: endpoints, requests,
  actions, monitor subscriptions, sequences, and DBC mappings.
- Transports send raw CAN requests and monitor frames. The package includes a
  real Bleak-based `BleTransport` for the CanOpener firmware and a
  python-can-based `PythonCanTransport` for wired CAN adapters.

## Install

```sh
python -m pip install -e ".[dev]"
```

Runtime dependencies are `pyyaml`, `bleak`, and `python-can`. Tests use
`pytest` and `pytest-asyncio`.

## Quick Start

```python
import asyncio

from can_opener import ConnectVehicleOptions, VirtualVehicleManager
from can_opener.transport import BleTransport
from can_opener.profile import VehicleProfileSource


async def main():
    profile = VehicleProfileSource.from_folder("vehicles/my-car")

    manager = VirtualVehicleManager()
    car = await manager.connect(
        ConnectVehicleOptions(
            id="car-a",
            transport=BleTransport(),
            profiles=[profile],
        )
    )

    await car.subscribe("RPM")
    speed = await car.query("SPEED")
    await car.action("HORN")

    print(speed)
    print(car.state.rpm)


asyncio.run(main())
```

## BLE Transport

`BleTransport` implements the ESP32 firmware GATT interface with Bleak.
Defaults match the firmware:

- Device name: `CanOpener`
- Service UUID: `00000180-0000-1000-8000-00805f9b34fb`
- Request characteristic: `0000fef4-0000-1000-8000-00805f9b34fb`
- Monitor Control characteristic: `0000a003-0000-1000-8000-00805f9b34fb`
- Monitor Data characteristic: `0000a004-0000-1000-8000-00805f9b34fb`

```python
from can_opener import ConnectVehicleOptions, VirtualVehicleManager
from can_opener.transport import BleTransport

transport = BleTransport()  # scans for device name "CanOpener"
# or: BleTransport(address="AA:BB:CC:DD:EE:FF")

manager = VirtualVehicleManager()
car = await manager.connect(
    ConnectVehicleOptions(id="garage-car", transport=transport, profiles=[profile])
)
```

The firmware handles CAN TX/RX, monitor snapshots, and ISO-TP reassembly. The
Python layer sends request packets, validates responses, decodes DBC payloads,
and updates vehicle state.

## Wired CAN Transport

`PythonCanTransport` uses `python-can` directly, with no BLE firmware protocol.
It sends the same `VehicleRequest` CAN frames that profiles and DBC-backed
actions already produce, and it reads the bus for query responses and monitored
subscription frames.

```python
from can_opener import ConnectVehicleOptions, VirtualVehicleManager
from can_opener.transport import PythonCanTransport

transport = PythonCanTransport(
    interface="slcan",
    channel="/dev/tty.usbmodem209433A131331",
    bitrate=500000,
)

manager = VirtualVehicleManager()
car = await manager.connect(
    ConnectVehicleOptions(id="wired-car", transport=transport, profiles=[profile])
)

await car.action("HIGHBEAM")
```

Direct raw-frame profile actions work too. For example, a profile action step
with `request_id: 0x745` and `send: [0x02, 0x10, 0x81, 0, 0, 0, 0, 0]` sends
the same frame as:

```python
bus.send(can.Message(arbitration_id=0x745, data=b"\x02\x10\x81\x00\x00\x00\x00\x00", is_extended_id=False))
```

## Profiles And DBC

Profiles are loaded at runtime:

```yaml
version: 1

dbc:
  files:
    - path: signals.dbc

endpoints:
  obd:
    request_id: 0x7DF
    response_ids:
      - range: [0x7E8, 0x7EF]
    timeout_ms: 500

queries:
  SPEED:
    endpoint: obd
    send: [0x01, 0x0D]
    expect: [0x41, 0x0D]
    dbc_mapping:
      message: OBD_Response_7E8
      signal: SPEED
```

DBC owns physical decoding:

```dbc
BO_ 2024 OBD_Response_7E8: 8 ECU
 SG_ RESPONSE_SERVICE : 8|8@1+ (1,0) [0|255] "" Vector__XXX
 SG_ PID M : 16|8@1+ (1,0) [0|255] "" Vector__XXX
 SG_ SPEED m13 : 24|8@1+ (1,0) [0|255] "km/h" Vector__XXX
```

## API Overview

`VirtualVehicleManager` connects isolated vehicles:

```python
car = await manager.connect(ConnectVehicleOptions(...))
manager.get("car-a")
manager.list()
await manager.disconnect("car-a")
await manager.disconnect_all()
```

`VirtualVehicle` exposes the main operations:

```python
await car.subscribe(["RPM", "LEFT_SIGNAL"])
await car.unsubscribe("RPM")
car.subscription_count()

rpm = await car.query("RPM")
handle = await car.subscribe_query("SPEED")
car.unsubscribe_query(handle)

await car.action("HORN")
car.state.get("RPM")
car.state.rpm
car.state.snapshot()
```

## Development

```sh
python -m pytest
```

The test suite covers the DBC parser/codec/controller, profile query/action and
subscription flows, Nissan Sentra fixture integration, and BLE packet
builders/parsers.