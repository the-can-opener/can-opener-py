from pathlib import Path

from can_opener.profile.types import VehicleProfileSource

ROOT = Path(__file__).parents[1] / "examples" / "vehicles"


nissan_sentra_profile = VehicleProfileSource.from_directory(ROOT / "nissan" / "sentra")
test_vehicle_profile = VehicleProfileSource.from_directory(ROOT / "test" / "basic")
universal_pid_profile = VehicleProfileSource.from_directory(ROOT / "universal" / "pid")

nissan_sentra_dbc = nissan_sentra_profile.dbc_files[0]
test_vehicle_dbc = test_vehicle_profile.dbc_files[0]
