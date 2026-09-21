"""领域枚举与配置值对象。"""

from dataclasses import dataclass
from enum import Enum


class RunStatus(str, Enum):
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class Stage(str, Enum):
    PENDING = "PENDING"
    IMAGING = "IMAGING"
    BASECALLING = "BASECALLING"
    DONE = "DONE"


class RegionStatus(str, Enum):
    WAITING = "WAITING"
    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"
    QUARANTINED = "QUARANTINED"
    HELD = "HELD"
    RERUN_REQUIRED = "RERUN_REQUIRED"
    PARTIAL = "PARTIAL"
    FINISHED = "FINISHED"


class IncidentType(str, Enum):
    SAMPLE_ID_CONFLICT = "SAMPLE_ID_CONFLICT"
    SIGNAL_CROSSTALK = "SIGNAL_CROSSTALK"
    REAGENT_SHORTAGE = "REAGENT_SHORTAGE"


class Recovery(str, Enum):
    CONTINUE = "CONTINUE"
    RERUN = "RERUN"
    PARTIAL_KEEP = "PARTIAL_KEEP"


@dataclass(frozen=True)
class RegionSpec:
    region_id: str
    sample_id: str
    total_cycles: int
    cluster_count: int
    read_len: int
    reagent_per_cycle_ml: float
    reagent_sku: str = "KIT-A"


@dataclass(frozen=True)
class ReagentSpec:
    sku: str
    total_ml: float


@dataclass
class RunConfig:
    run_id: str
    regions: list
    reagents: list
    cycle_minutes: float = 10.0
    calibration_minutes: float = 20.0
    slow_speed: float = 0.6
    calibration_gain: float = 0.4
    slowdown_quality_gain: float = 0.6
    crosstalk_continue_ratio: float = 0.2
    crosstalk_rerun_ratio: float = 0.6
