import asyncio
from pathlib import Path

from can_opener import ConnectVehicleOptions, VirtualVehicleManager
from can_opener.profile import VehicleProfileSource
from can_opener.transport import PythonCanTransport

# this script activates the horn when the door of the car is opened
# it is tested on a 2010 nissan versa

CAN_INTERFACE = "slcan"
CAN_CHANNEL = "/dev/tty.usbmodem209433A131331"
CAN_BITRATE = 500000
REPO_ROOT = Path(__file__).resolve().parents[1]
PROFILE_PATH = REPO_ROOT / "examples" / "vehicles" / "nissan" / "sentra"
DOOR_SIGNAL = "FRONT_LEFT_DOOR_OPEN"
PRINT_INTERVAL_SECONDS = 0.1


def door_status(door_open) -> str:
    if door_open is None:
        return "unknown"
    if isinstance(door_open, str):
        return door_open
    return "open" if door_open else "closed"


def is_door_open(door_open) -> bool | None:
    status = door_status(door_open)
    if status == "unknown":
        return None
    return status == "open"


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

        previous_door_open = is_door_open(car.state.get(DOOR_SIGNAL))

        while True:
            door_open = car.state.get(DOOR_SIGNAL)
            current_door_open = is_door_open(door_open)
            if previous_door_open is False and current_door_open is True:
                await car.action("HORN")
            previous_door_open = current_door_open
            await asyncio.sleep(PRINT_INTERVAL_SECONDS)
    finally:
        await manager.disconnect_all()


asyncio.run(main())