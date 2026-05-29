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
