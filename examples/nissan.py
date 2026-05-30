import asyncio
from pathlib import Path

from can_opener import ConnectVehicleOptions, VirtualVehicleManager
from can_opener.profile import VehicleProfileSource
from can_opener.transport import PythonCanTransport
import time

# this script activates the horn when the door of the car is opened and flashes the signals 10 times
# it is tested on a 2010 nissan versa

CAN_INTERFACE = "slcan"
CAN_CHANNEL = "/dev/tty.usbmodem209433A131331"
CAN_BITRATE = 500000
REPO_ROOT = Path(__file__).resolve().parents[1]
PROFILE_PATH = REPO_ROOT / "examples" / "vehicles" / "nissan" / "sentra"
DOOR_SIGNALS = ("FRONT_LEFT_DOOR_OPEN", "FRONT_RIGHT_DOOR_OPEN")
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
    return f"{door_status(door_open)} ({door_open})"


def any_door_open(car) -> bool | None:
    door_states = [is_door_open(car.state.get(signal)) for signal in DOOR_SIGNALS]
    if any(door_open is True for door_open in door_states):
        return True
    if any(door_open is None for door_open in door_states):
        return None
    return False


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

        for signal in DOOR_SIGNALS:
            await car.subscribe(signal)

        previous_door_open = any_door_open(car)

        while True:
            current_door_open = any_door_open(car)
            if previous_door_open is False and current_door_open is True:
                await car.action("HORN")
                time.sleep(0.1)
                for i in range(10):
                    await car.action("LEFT_SIGNAL")
                    time.sleep(0.5)
                    await car.action("RIGHT_SIGNAL")
                    time.sleep(0.5)
                await car.action("SIGNAL_OFF")
                time.sleep(0.1)
            previous_door_open = current_door_open
            await asyncio.sleep(PRINT_INTERVAL_SECONDS)
    finally:
        await manager.disconnect_all()


asyncio.run(main())