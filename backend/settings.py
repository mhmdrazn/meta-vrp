import os
from dataclasses import dataclass
from pathlib import Path

# Resolusi absolut relatif terhadap folder backend/, supaya tidak bergantung
# pada current working directory (penting untuk deployment serverless spt Vercel).
_BACKEND_DIR = Path(__file__).resolve().parent
_DEFAULT_DATA_DIR = _BACKEND_DIR / "data"


@dataclass
class Settings:
    # === data paths ===
    DATA_NODES_PATH: str = os.getenv(
        "DATA_NODES_PATH", str(_DEFAULT_DATA_DIR / "nodes.csv")
    )
    DATA_MATRIX_PATH: str = os.getenv(
        "DATA_MATRIX_PATH", str(_DEFAULT_DATA_DIR / "time_matrix.csv")
    )

    # === fixed operational params ===
    DEPOT_ID: str = "0"
    VEHICLE_CAPACITY_LITERS: float = 5000.0
    ALLOW_REFILL: bool = True
    REFILL_SERVICE_MIN: float = 12.0

    # === objective & penalties ===
    LAMBDA_USE_MIN: float = 24.0  # penalti aktivasi kendaraan (menit ekv.)
    TIME_LIMIT_SEC: float = 30.0  # total waktu solver (construct+ALNS+improve)

    # === ALNS master switch & tuning ===
    USE_ALNS: bool = True  # aktifkan / matikan ALNS
    # proporsi waktu total utk ALNS (sisanya improve)
    ALNS_TIME_FRAC: float = 0.9
    ALNS_LAMBDA_CAPACITY: float = 0.0  # penalti overload kapasitas (0 = off)

    # SA / acceptance params
    ALNS_SEED: int = 42
    ALNS_INIT_TEMP: float = 2500.0
    ALNS_COOLING_RATE: float = 0.997
    ALNS_MIN_TEMP: float = 1e-3

    # destroy / repair operators
    ALNS_K_REMOVE_MIN: int = 4
    ALNS_K_REMOVE_MAX: int = 12
    ALNS_SCORE_UPDATE_PERIOD: int = 30

    # tabu settings
    ALNS_TABU_TENURE: int = 30
    ALNS_USE_TABU_ON_REMOVED: bool = True

    # optional repair strategy
    ALNS_USE_CONSTRUCT_AS_REPAIR: bool = (
        False  # True = pakai greedy_construct utk repair
    )

    IMPROVE_MAX_NO_IMPROVE: int = 10000


settings = Settings()
