import asyncio
from pathlib import Path

from can_opener import ConnectVehicleOptions, VirtualVehicleManager
from can_opener.profile import VehicleProfileSource
from can_opener.transport import PythonCanTransport
import time
CAN_INTERFACE = "slcan"
CAN_CHANNEL = "/dev/tty.usbmodem209433A131331"
CAN_BITRATE = 500000
REPO_ROOT = Path(__file__).resolve().parents[1]
PROFILE_PATH = REPO_ROOT / "examples" / "vehicles" / "nissan" / "sentra"
DOOR_SIGNAL = "FRONT_LEFT_DOOR_OPEN"
PRINT_INTERVAL_SECONDS = 1.0


def door_status(door_open) -> str:
    if door_open is None:
        return "unknown"
    if isinstance(door_open, str):
        return door_open
    return "open" if door_open else "closed"


def door_status_line(door_open) -> str:
    return f"{DOOR_SIGNAL}: {door_status(door_open)} ({door_open})"


async def main():
    profile = VehicleProfileSource.from_directory(PROFILE_PATH)

    manager = VirtualVehicleManager()
    try:
        car = await manager.connect(
            ConnectVehicleOptions(
                id="car-a",
                transport=PythonCanTransport(
                    interface=CAN_INTERFACE,
                    channel=CAN_CHANNEL,
                    bitrate=CAN_BITRATE,
                ),
                profiles=[profile],
            )
        )

        await car.subscribe(DOOR_SIGNAL)

        await car.action("HORN")
        time.sleep(1)
        await car.action("HIGH_BEAM")

        while True:
            door_open = car.state.get(DOOR_SIGNAL)
            print(door_status_line(door_open), flush=True)
            await asyncio.sleep(PRINT_INTERVAL_SECONDS)
    finally:
        await manager.disconnect_all()


asyncio.run(main())