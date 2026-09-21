"""测序运行的领域模型（纯数据，全部状态可由事件重建）。

结构：Run -> Batch -> Region -> Sample（批次->区域->样本）

注意：兼容 Python 3.9，不使用 3.10+ 语法。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional, Tuple


class Stage(str, Enum):
    """样本所处处理阶段。"""

    LOADING = "loading"              # 进样/上样
    CLUSTERING = "clustering"        # 簇生成
    IMAGING = "imaging"              # 成像（逐循环）
    BASE_CALLING = "base_calling"    # 碱基判读
    COMPLETE = "complete"            # 已完成
    ISOLATED = "isolated"            # 被故障隔离（保留已有结果，等待处置）


class ReadType(str, Enum):
    FORWARD = "R1"
    REVERSE = "R2"
    INDEX = "index"


class RunState(str, Enum):
    SETUP = "setup"
    RUNNING = "running"
    PAUSED = "paused"          # 仅表示存在被暂停区域（运行整体仍在推进）
    COMPLETED = "completed"


class FaultKind(str, Enum):
    ID_CONFLICT = "id_conflict"     # 样本标识冲突
    CROSSTALK = "crosstalk"         # 信号串扰
    REAGENT_LOW = "reagent_low"     # 试剂不足


class RecoveryAction(str, Enum):
    """三种恢复路径 + 冲突处置专用路径。"""

    CONTINUE_READ = "continue_read"   # 可继续读取（补救后无需重跑）
    RERUN = "rerun"                   # 必须重跑受影响单元
    KEEP_EXISTING = "keep_existing"   # 只能保留已有结果
    REMAP = "remap"                   # 重新指定样本标识（冲突解除后继续）


class FaultState(str, Enum):
    OPEN = "open"
    RESOLVED = "resolved"


@dataclass
class ReadSpec:
    name: str
    read_type: ReadType
    cycles: int


@dataclass
class Position:
    """样本在仪器中的物理/逻辑位置。"""

    lane: int
    tile: int
    x: int
    y: int

    def as_tuple(self) -> Tuple[int, int, int, int]:
        return (self.lane, self.tile, self.x, self.y)


@dataclass
class CycleQuality:
    read: str
    cycle: int
    q30_fraction: float          # 该循环 Q30 比例 0~1
    raw_intensity: float         # 归一化信号强度
    crosstalk: bool = False      # 是否受串扰污染
    counted: bool = False        # 该循环有效碱基是否已计入


@dataclass
class Fault:
    fault_id: str
    kind: FaultKind
    scope: str                   # "sample" | "region"
    sample_ids: List[str]
    region_id: Optional[str]
    batch_id: str
    cycle: Optional[int]
    message: str
    state: FaultState = FaultState.OPEN
    recovered_by: Optional[RecoveryAction] = None
    detected_at_min: float = 0.0
    resolved_at_min: Optional[float] = None
    extra: Dict[str, object] = field(default_factory=dict)
    resolution_note: str = ""


@dataclass
class Sample:
    sample_id: str
    position: Position
    stage: Stage = Stage.LOADING
    clusters_millions: float = 0.0
    # 当前正在成像的读段与循环（1 基）
    current_read: int = 0
    current_cycle: int = 0
    # 已提交（已完成 base call 并计入有效数据）的循环数，按读段
    committed_cycles: Dict[str, int] = field(default_factory=dict)
    # 已成像但尚未提交的循环数（在读段之间为 0）
    imaged_uncommitted: int = 0
    qualities: List[CycleQuality] = field(default_factory=list)
    paused: bool = False
    isolated: bool = False
    rerun_done: bool = False
    # 有效碱基数（Gb），由提交循环累积
    effective_gb: float = 0.0
    barcodes: List[str] = field(default_factory=list)

    def total_committed(self) -> int:
        return sum(self.committed_cycles.values())

    def total_done(self) -> int:
        """已成像循环数（含尚未提交的当前读段循环）。"""
        return self.total_committed() + self.imaged_uncommitted

    def avg_q30(self) -> float:
        if not self.qualities:
            return 0.0
        return sum(q.q30_fraction for q in self.qualities) / len(self.qualities)

    def read_progress(self, reads: List[ReadSpec]) -> Tuple[int, int]:
        done = sum(self.committed_cycles.values())
        total = sum(r.cycles for r in reads)
        return done, total


@dataclass
class Region:
    region_id: str
    name: str
    lane: int
    tile_range: Tuple[int, int]
    sample_ids: List[str] = field(default_factory=list)
    calibration_offset: float = 0.0   # 成像校准微调（0=出厂基准）
    speed_factor: float = 1.0         # 读取速度因子（<1 为降速，质量提升）
    paused: bool = False
    # 已发生停机时长（分钟）
    downtime_min: float = 0.0
    # 降速时的节拍累计器：累计 speed_factor，每满 1.0 出一个循环
    tick_accumulator: float = 0.0


@dataclass
class Batch:
    batch_id: str
    region_ids: List[str] = field(default_factory=list)
    # 计划开始/结束由调度推算；实际开始在首个样本进入成像时记录
    started_min: Optional[float] = None
    finished_min: Optional[float] = None


@dataclass
class ReagentState:
    name: str
    capacity_ml: float
    remaining_ml: float
    low_alert_latched: bool = False

    @property
    def fraction(self) -> float:
        if self.capacity_ml <= 0:
            return 0.0
        return max(0.0, self.remaining_ml / self.capacity_ml)


@dataclass
class Run:
    run_id: str
    reads: List[ReadSpec] = field(default_factory=list)
    samples: Dict[str, Sample] = field(default_factory=dict)
    regions: Dict[str, Region] = field(default_factory=dict)
    batches: Dict[str, Batch] = field(default_factory=dict)
    batch_order: List[str] = field(default_factory=list)
    reagents: Dict[str, ReagentState] = field(default_factory=dict)
    faults: Dict[str, Fault] = field(default_factory=dict)
    state: RunState = RunState.SETUP
    clock_min: float = 0.0
    event_seq: int = 0
    # 样本 -> 批次/区域 的归属索引
    sample_region: Dict[str, str] = field(default_factory=dict)
    sample_batch: Dict[str, str] = field(default_factory=dict)

    # ---- 便捷查询 ----
    def total_cycles(self) -> int:
        return sum(r.cycles for r in self.reads)

    def samples_of_region(self, region_id: str) -> List[Sample]:
        return [self.samples[sid] for sid in self.regions[region_id].sample_ids]

    def samples_of_batch(self, batch_id: str) -> List[Sample]:
        out = []
        for rid in self.batches[batch_id].region_ids:
            out.extend(self.samples_of_region(rid))
        return out

    def region_of(self, sample_id: str) -> Region:
        return self.regions[self.sample_region[sample_id]]

    def batch_of(self, sample_id: str) -> Batch:
        return self.batches[self.sample_batch[sample_id]]

    def open_faults(self) -> List[Fault]:
        return [f for f in self.faults.values() if f.state == FaultState.OPEN]

    def active_samples(self) -> List[Sample]:
        """尚未结束（未完成、未隔离、未暂停）的样本。"""
        out = []
        for s in self.samples.values():
            if s.stage in (Stage.COMPLETE, Stage.ISOLATED):
                continue
            if s.paused:
                continue
            out.append(s)
        return out

    def pending_samples(self) -> List[Sample]:
        """除已完成外的所有样本（含暂停/隔离）。"""
        return [s for s in self.samples.values() if s.stage != Stage.COMPLETE]

    def gb_per_cycle(self) -> float:
        """每个成像循环每百万簇对应的 Gb 数。"""
        return 2e-4

    def projected_sample_gb(self, sample: Sample) -> float:
        """样本按全部循环跑完、当前平均质量下的预计有效 Gb。"""
        q = sample.avg_q30() or 0.85
        return sample.clusters_millions * self.total_cycles() * self.gb_per_cycle() * q

    def effective_gb_total(self) -> float:
        return sum(s.effective_gb for s in self.samples.values())

    def projected_effective_gb_total(self) -> float:
        return sum(self.projected_sample_gb(s) for s in self.samples.values())

    def overall_progress(self) -> Tuple[int, int]:
        done = total = 0
        for s in self.samples.values():
            d, t = s.read_progress(self.reads)
            done += d
            total += t
        return done, total

    def next_read_cycle(self, sample: Sample) -> Optional[Tuple[ReadSpec, int]]:
        """根据已提交循环 + 已成像未提交循环推断下一个（读段, 循环）。"""
        for idx, spec in enumerate(self.reads):
            committed = sample.committed_cycles.get(spec.name, 0)
            in_read = committed + (sample.imaged_uncommitted if idx == _current_read_index(sample, self.reads) else 0)
            if in_read < spec.cycles:
                return spec, in_read + 1
        return None


def _current_read_index(sample: "Sample", reads: List[ReadSpec]) -> int:
    name = sample.current_read
    for i, spec in enumerate(reads):
        if spec.name == name:
            return i
    return 0
