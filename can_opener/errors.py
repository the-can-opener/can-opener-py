class VirtualVehicleError(Exception):
    """Base exception for can-opener-py errors."""


class UnknownVehicleSignalError(VirtualVehicleError):
    def __init__(self, signal_name: str) -> None:
        super().__init__(f"Unknown vehicle signal: {signal_name}")


class SignalProtocolError(VirtualVehicleError):
    def __init__(self, signal_name: str, expected: str, actual: str) -> None:
        super().__init__(
            f"{signal_name} is not a {expected} signal (actual protocol: {actual})"
        )


class VehicleConnectionError(VirtualVehicleError):
    pass


class BatchUnsupportedError(VirtualVehicleError):
    """Raised by a transport whose connected device turns out not to support batches.

    Nothing has been transmitted when it is raised, so the caller replays the
    steps one request at a time.
    """
