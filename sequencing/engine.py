"""测序运行聚合：推进、在线操作、影响推算、故障隔离与恢复裁决。

状态只能通过领域事件改变；从 EventStore 重放即可完整重建。
时间按区域累计（busy_min，分钟）：各区域并行，运行级时钟取最大区域耗时。
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .domain import (
    IncidentType,
    Recovery,
    RegionSpec,
    RegionStatus,
    RunConfig,
    RunStatus,
    Stage,
)
from .events import EventStore


@dataclass
class RegionState:
    spec: RegionSpec
    sample_id: str = ""
    status: RegionStatus = RegionStatus.WAITING
    stage: Stage = Stage.PENDING
    cycles_done: int = 0
    signal_q: float = 0.85
    speed: float = 1.0
    busy_min: float = 0.0
    contaminated_cycles: List[int] = field(default_factory=list)
    incident_id: Optional[str] = None
    recovery: Optional[str] = None

    @property
    def region_id(self) -> str:
        return self.spec.region_id

    @property
    def raw_bases(self) -> int:
        return self.cycles_done * self.spec.cluster_count * self.spec.read_len

    def remaining_cycles(self) -> int:
        return max(0, self.spec.total_cycles - self.cycles_done)

    def eta_minutes(self, cycle_minutes: float) -> float:
        if self.status in (RegionStatus.FINISHED, RegionStatus.RERUN_REQUIRED, RegionStatus.PARTIAL):
            return 0.0
        return self.busy_min + self.remaining_cycles() * cycle_minutes / self.speed


@dataclass
class IncidentState:
    incident_id: str
    itype: IncidentType
    region_ids: List[str]
    detail: str
    raised_seq: int
    status: str = "OPEN"
    recovery: Optional[Recovery] = None
    reason: str = ""
    decided_seq: Optional[int] = None
    resolved_seq: Optional[int] = None


def quality_factor(q: float) -> float:
    """质量 -> 可用比例：q=0.85 时约 0.77，单调有界、确定性。"""
    return q / (q + 0.25)


class SequencingEngine:
    def __init__(self, store: EventStore, config: Optional[RunConfig] = None):
        self.store = store
        self.config = config
        self.run_id = ""
        self.status: Optional[RunStatus] = None
        self.regions: Dict[str, RegionState] = {}
        self.reagents: Dict[str, float] = {}
        self.incidents: Dict[str, IncidentState] = {}
        self._inc_seq = 0
        self._rebuild()

    # ------------------------------------------------------------- 重建/应用

    def _rebuild(self) -> None:
        if self.config is None and not self.store.events:
            return
        self.regions = {}
        self.reagents = {}
        self.incidents = {}
        self._inc_seq = 0
        self.status = None
        for event in self.store.replay():
            self._apply(event)

    def _apply(self, event) -> None:
        etype, d = event.type, event.data
        if etype == "RunStarted":
            self.run_id = d["run_id"]
            self.status = RunStatus.RUNNING
            self.config = RunConfig(
                run_id=d["run_id"],
                regions=[RegionSpec(**r) for r in d["regions"]],
                reagents=d["reagents"],
                cycle_minutes=d.get("cycle_minutes", 10.0),
                calibration_minutes=d.get("calibration_minutes", 20.0),
                slow_speed=d.get("slow_speed", 0.6),
            )
            for spec in self.config.regions:
                self.regions[spec.region_id] = RegionState(spec=spec, sample_id=spec.sample_id)
            for item in d["reagents"]:
                self.reagents[item["sku"]] = item["total_ml"]
        elif etype == "RegionActivated":
            r = self.regions[d["region_id"]]
            r.status = RegionStatus.ACTIVE
            r.stage = Stage.IMAGING
        elif etype == "CycleAdvanced":
            r = self.regions[d["region_id"]]
            r.cycles_done = d["cycles_done"]
            r.busy_min = d["busy_min"]
            r.stage = Stage(d["stage"])
            for sku, used in d["reagent_used"].items():
                self.reagents[sku] -= used
        elif etype == "RegionPaused":
            self.regions[d["region_id"]].status = RegionStatus.PAUSED
        elif etype == "RegionResumed":
            r = self.regions[d["region_id"]]
            r.status = RegionStatus.ACTIVE
            r.busy_min += d.get("minutes_paused", 0.0)
        elif etype == "CalibrationStarted":
            self.regions[d["region_id"]].busy_min += d["downtime_min"]
        elif etype == "CalibrationCompleted":
            r = self.regions[d["region_id"]]
            r.signal_q = d["signal_q"]
            held = d.get("hold_status")
            r.status = RegionStatus(held) if held else RegionStatus.ACTIVE
        elif etype == "SpeedChanged":
            r = self.regions[d["region_id"]]
            r.speed = d["speed"]
            r.signal_q = d["signal_q"]
        elif etype == "IncidentRaised":
            self._inc_seq += 1
            inc = IncidentState(
                incident_id=d["incident_id"],
                itype=IncidentType(d["itype"]),
                region_ids=list(d["region_ids"]),
                detail=d.get("detail", ""),
                raised_seq=event.seq,
            )
            self.incidents[d["incident_id"]] = inc
            for rid in d["region_ids"]:
                r = self.regions[rid]
                r.incident_id = d["incident_id"]
                r.status = RegionStatus(d["status"])
            for rid, cycles in d.get("contaminated", {}).items():
                self.regions[rid].contaminated_cycles = sorted(set(cycles))
        elif etype == "IncidentScopeAdded":
            inc = self.incidents[d["incident_id"]]
            if d["region_id"] not in inc.region_ids:
                inc.region_ids.append(d["region_id"])
            r = self.regions[d["region_id"]]
            r.incident_id = d["incident_id"]
            r.status = RegionStatus(d["status"])
        elif etype == "RegionFinished":
            r = self.regions[d["region_id"]]
            r.status = RegionStatus.FINISHED
            r.stage = Stage.DONE
            r.busy_min = d["busy_min"]
        elif etype == "SampleRemapped":
            self.regions[d["region_id"]].sample_id = d["new_sample_id"]
        elif etype == "ReagentReplenished":
            self.reagents[d["sku"]] += d["added_ml"]
        elif etype == "RecoveryDecided":
            inc = self.incidents[d["incident_id"]]
            inc.recovery = Recovery(d["recovery"])
            inc.reason = d["reason"]
            inc.decided_seq = event.seq
        elif etype == "IncidentResolved":
            inc = self.incidents[d["incident_id"]]
            inc.status = "RESOLVED"
            inc.resolved_seq = event.seq
            terminal = {RegionStatus.RERUN_REQUIRED.value, RegionStatus.PARTIAL.value}
            for rid, outcome in d["outcomes"].items():
                r = self.regions[rid]
                r.status = RegionStatus(outcome)
                r.recovery = inc.recovery.value if inc.recovery else None
                r.incident_id = None
                if outcome == RegionStatus.FINISHED.value or outcome in terminal:
                    r.stage = Stage.DONE
        elif etype == "RunCompleted":
            self.status = RunStatus.COMPLETED

    # ------------------------------------------------------------------ 启动

    def start_run(self, operator: str = "system") -> None:
        if self.status is not None:
            raise RuntimeError("运行已存在，不能重复启动")
        from dataclasses import asdict

        self.store.append(
            "RunStarted",
            {
                "run_id": self.config.run_id,
                "regions": [asdict(r) for r in self.config.regions],
                "reagents": [{"sku": s.sku, "total_ml": s.total_ml} for s in self.config.reagents],
                "cycle_minutes": self.config.cycle_minutes,
                "calibration_minutes": self.config.calibration_minutes,
                "slow_speed": self.config.slow_speed,
            },
            operator,
        )
        self._rebuild()
        for rid in sorted(self.regions):
            self.store.append(
                "RegionActivated", {"region_id": rid, "stage": Stage.IMAGING.value}, operator
            )
        self._rebuild()
        self._detect_sample_conflict(operator)

    # ------------------------------------------------------------- 推进/检测

    def advance(self, cycles: int = 1, operator: str = "system") -> Dict[str, List[str]]:
        """按周期并行推进所有正常区域；自动识别试剂不足，返回新事件单。"""
        if self.status != RunStatus.RUNNING:
            raise RuntimeError("运行不在进行中")
        new_incidents: List[str] = []
        for _ in range(cycles):
            for rid in sorted(self.regions):
                r = self.regions[rid]
                if r.status != RegionStatus.ACTIVE or r.stage == Stage.DONE:
                    continue
                if r.cycles_done >= r.spec.total_cycles:
                    continue
                need = r.spec.reagent_per_cycle_ml
                if self.reagents.get(r.spec.reagent_sku, 0.0) + 1e-9 < need:
                    new_incidents.append(self._raise_shortage(rid, operator))
                    continue
                next_cycle = r.cycles_done + 1
                stage = Stage.IMAGING.value
                if next_cycle > int(r.spec.total_cycles * 0.8):
                    stage = Stage.BASECALLING.value
                if next_cycle >= r.spec.total_cycles:
                    stage = Stage.BASECALLING.value
                busy_min = r.busy_min + self.config.cycle_minutes / r.speed
                self.store.append(
                    "CycleAdvanced",
                    {
                        "region_id": rid,
                        "cycles_done": next_cycle,
                        "stage": stage,
                        "reagent_used": {r.spec.reagent_sku: need},
                        "busy_min": busy_min,
                    },
                    operator,
                )
                if next_cycle >= r.spec.total_cycles:
                    self.store.append(
                        "RegionFinished",
                        {"region_id": rid, "busy_min": busy_min},
                        operator,
                    )
            self._rebuild()
        new_incidents.extend(self._detect_sample_conflict(operator))
        return {"incidents": sorted(set(i for i in new_incidents if i))}

    def _detect_sample_conflict(self, operator: str) -> List[str]:
        seen: Dict[str, str] = {}
        raised: List[str] = []
        for rid in sorted(self.regions):
            r = self.regions[rid]
            if r.status in (RegionStatus.FINISHED, RegionStatus.RERUN_REQUIRED, RegionStatus.PARTIAL):
                continue
            other = seen.get(r.sample_id)
            if other:
                pair = {other, rid}
                if not self._open_exists(IncidentType.SAMPLE_ID_CONFLICT, pair):
                    inc = self._new_incident_id()
                    self.store.append(
                        "IncidentRaised",
                        {
                            "incident_id": inc,
                            "itype": IncidentType.SAMPLE_ID_CONFLICT.value,
                            "region_ids": sorted(pair),
                            "status": RegionStatus.QUARANTINED.value,
                            "detail": f"样本标识 {r.sample_id} 同时绑定区域 {other} 与 {rid}",
                            "contaminated": {},
                        },
                        operator,
                    )
                    self._rebuild()
                    raised.append(inc)
            else:
                seen[r.sample_id] = rid
        return raised

    def _raise_shortage(self, rid: str, operator: str) -> str:
        sku = self.regions[rid].spec.reagent_sku
        scope = {
            x.region_id
            for x in self.regions.values()
            if x.spec.reagent_sku == sku
            and x.status == RegionStatus.ACTIVE
            and self.reagents[sku] + 1e-9 < x.spec.reagent_per_cycle_ml
        }
        scope.add(rid)
        existing = self._find_open(IncidentType.REAGENT_SHORTAGE, scope)
        if existing:
            missing = scope - set(existing.region_ids)
            for new_rid in sorted(missing):
                self.store.append(
                    "IncidentScopeAdded",
                    {
                        "incident_id": existing.incident_id,
                        "region_id": new_rid,
                        "status": RegionStatus.HELD.value,
                    },
                    operator,
                )
            if missing:
                self._rebuild()
            return existing.incident_id
        inc = self._new_incident_id()
        self.store.append(
            "IncidentRaised",
            {
                "incident_id": inc,
                "itype": IncidentType.REAGENT_SHORTAGE.value,
                "region_ids": sorted(scope),
                "status": RegionStatus.HELD.value,
                "detail": f"试剂 {sku} 余量不足以下一周期成像",
                "contaminated": {},
            },
            operator,
        )
        self._rebuild()
        return inc

    # ------------------------------------------------------------- 在线操作

    def pause_region(self, region_id: str, operator: str = "lab") -> None:
        r = self._require_region(region_id)
        if r.status != RegionStatus.ACTIVE:
            raise RuntimeError(f"区域 {region_id} 当前为 {r.status.value}，无法暂停")
        self.store.append("RegionPaused", {"region_id": region_id}, operator)
        self._rebuild()

    def resume_region(self, region_id: str, minutes_paused: float = 0.0, operator: str = "lab") -> None:
        r = self._require_region(region_id)
        if r.status != RegionStatus.PAUSED:
            raise RuntimeError(f"区域 {region_id} 未处于暂停状态")
        self.store.append(
            "RegionResumed",
            {"region_id": region_id, "minutes_paused": minutes_paused},
            operator,
        )
        self._rebuild()

    def recalibrate(self, region_id: str, operator: str = "lab") -> float:
        r = self._require_region(region_id)
        if r.status not in (RegionStatus.ACTIVE, RegionStatus.QUARANTINED):
            raise RuntimeError(f"区域 {region_id} 当前为 {r.status.value}，无法校准")
        cfg = self.config
        self.store.append(
            "CalibrationStarted",
            {
                "region_id": region_id,
                "downtime_min": cfg.calibration_minutes,
                "hold_status": r.status.value,
            },
            operator,
        )
        new_q = min(1.0, r.signal_q + (1.0 - r.signal_q) * cfg.calibration_gain)
        self.store.append(
            "CalibrationCompleted",
            {
                "region_id": region_id,
                "signal_q": round(new_q, 6),
                "hold_status": r.status.value,
            },
            operator,
        )
        self._rebuild()
        return new_q

    def set_speed(self, region_id: str, speed: float, operator: str = "lab") -> None:
        r = self._require_region(region_id)
        if not 0.2 <= speed <= 1.5:
            raise ValueError("读取倍率需在 0.2–1.5 之间")
        if speed < 1.0:
            new_q = 1.0 - (1.0 - r.signal_q) * self.config.slowdown_quality_gain
        elif speed > 1.0:
            new_q = r.signal_q * 0.95
        else:
            new_q = r.signal_q
        self.store.append(
            "SpeedChanged",
            {"region_id": region_id, "speed": speed, "signal_q": round(new_q, 6)},
            operator,
        )
        self._rebuild()

    def raise_signal_crosstalk(
        self,
        region_ids: List[str],
        contaminated: Optional[Dict[str, List[int]]] = None,
        operator: str = "detector",
    ) -> str:
        scope = set(region_ids)
        for rid in scope:
            self._require_region(rid)
        existing = self._find_open(IncidentType.SIGNAL_CROSSTALK, scope)
        if existing:
            return existing.incident_id
        contaminated = contaminated or {
            rid: list(range(1, self.regions[rid].cycles_done + 1)) for rid in scope
        }
        inc = self._new_incident_id()
        self.store.append(
            "IncidentRaised",
            {
                "incident_id": inc,
                "itype": IncidentType.SIGNAL_CROSSTALK.value,
                "region_ids": sorted(scope),
                "status": RegionStatus.QUARANTINED.value,
                "detail": f"区域 {sorted(scope)} 之间检测到信号串扰",
                "contaminated": contaminated,
            },
            operator,
        )
        self._rebuild()
        return inc

    def remap_sample(self, region_id: str, new_sample_id: str, operator: str = "lab") -> None:
        r = self._require_region(region_id)
        if new_sample_id in {x.sample_id for k, x in self.regions.items() if k != region_id}:
            raise ValueError("新样本标识仍与其他区域冲突")
        self.store.append(
            "SampleRemapped",
            {"region_id": region_id, "old_sample_id": r.sample_id, "new_sample_id": new_sample_id},
            operator,
        )
        self._rebuild()

    def replenish_reagent(self, sku: str, added_ml: float, operator: str = "lab") -> None:
        if sku not in self.reagents:
            raise KeyError(f"未知试剂 {sku}")
        self.store.append("ReagentReplenished", {"sku": sku, "added_ml": added_ml}, operator)
        self._rebuild()

    # ------------------------------------------------------------- 恢复裁决

    def recommend_recovery(self, incident_id: str) -> Dict[str, Any]:
        inc = self.incidents[incident_id]
        cfg = self.config
        if inc.itype == IncidentType.SAMPLE_ID_CONFLICT:
            samples = {self.regions[rid].sample_id for rid in inc.region_ids}
            if len(samples) > 1:
                return self._rec(Recovery.CONTINUE, "样本标识已重新映射，冲突消除，可继续读取")
            return self._rec(Recovery.RERUN, "两区域仍为同一标识且未重映射，数据归属无法区分，必须重跑")
        if inc.itype == IncidentType.REAGENT_SHORTAGE:
            sku = self.regions[inc.region_ids[0]].spec.reagent_sku
            enough = all(
                self.reagents[sku] + 1e-9 >= self.regions[rid].spec.reagent_per_cycle_ml
                for rid in inc.region_ids
            )
            if enough:
                return self._rec(Recovery.CONTINUE, f"试剂 {sku} 已补足，可继续读取")
            return self._rec(Recovery.PARTIAL_KEEP, "试剂未补足，只能冻结保留已有结果，补剂后须重开运行")
        ratios = [
            len(self.regions[rid].contaminated_cycles) / max(1, self.regions[rid].cycles_done)
            for rid in inc.region_ids
        ]
        worst = max(ratios)
        if worst <= cfg.crosstalk_continue_ratio:
            return self._rec(
                Recovery.CONTINUE,
                f"污染占比 {worst:.0%} ≤ {cfg.crosstalk_continue_ratio:.0%}，校准后可继续",
            )
        if worst >= cfg.crosstalk_rerun_ratio:
            return self._rec(
                Recovery.RERUN,
                f"污染占比 {worst:.0%} ≥ {cfg.crosstalk_rerun_ratio:.0%}，有效数据不足，必须重跑",
            )
        return self._rec(
            Recovery.PARTIAL_KEEP,
            f"污染占比 {worst:.0%} 居中，切除污染段、保留污染前结果",
        )

    @staticmethod
    def _rec(recovery: Recovery, reason: str) -> Dict[str, Any]:
        return {"recovery": recovery, "reason": reason}

    def decide_recovery(
        self,
        incident_id: str,
        operator: str = "lab",
        recovery: Optional[Recovery] = None,
    ) -> Recovery:
        inc = self.incidents[incident_id]
        if inc.status != "OPEN":
            raise RuntimeError(f"事件单 {incident_id} 已处置")
        rec = self.recommend_recovery(incident_id)
        chosen = recovery or rec["recovery"]
        self.store.append(
            "RecoveryDecided",
            {
                "incident_id": incident_id,
                "recovery": chosen.value,
                "reason": rec["reason"],
                "auto_recommended": rec["recovery"].value,
            },
            operator,
        )
        self._rebuild()
        return chosen

    def resolve_incident(self, incident_id: str, operator: str = "lab") -> None:
        inc = self.incidents[incident_id]
        if inc.status != "OPEN" or inc.recovery is None:
            raise RuntimeError("事件单须先裁决（decide_recovery）再解除")
        outcomes: Dict[str, str] = {}
        for rid in inc.region_ids:
            if inc.recovery == Recovery.CONTINUE:
                outcomes[rid] = RegionStatus.ACTIVE.value
            elif inc.recovery == Recovery.RERUN:
                outcomes[rid] = RegionStatus.RERUN_REQUIRED.value
            else:
                outcomes[rid] = RegionStatus.PARTIAL.value
        self.store.append(
            "IncidentResolved", {"incident_id": incident_id, "outcomes": outcomes}, operator
        )
        self._rebuild()

    def complete_run(self, operator: str = "system") -> None:
        active = [r for r in self.regions.values() if r.status == RegionStatus.ACTIVE]
        if active:
            raise RuntimeError("仍有区域在读取，不能结束运行")
        self.store.append("RunCompleted", {"operator_note": "运行结束"}, operator)
        self._rebuild()

    # ------------------------------------------------------- 有效数据量核算

    def usable_cycles(self, r: RegionState) -> int:
        """返回计入有效产出的周期：RERUN 不计；PARTIAL/串扰只计污染前的干净前缀。"""
        if r.recovery == Recovery.RERUN.value or r.status == RegionStatus.RERUN_REQUIRED:
            return 0
        if not r.contaminated_cycles:
            return r.cycles_done
        clean = 0
        contaminated = set(r.contaminated_cycles)
        for cycle in range(1, r.cycles_done + 1):
            if cycle in contaminated:
                break
            clean = cycle
        return clean

    def usable_bases(self, r: RegionState, q_override: Optional[float] = None) -> float:
        cycles = self.usable_cycles(r)
        q = r.signal_q if q_override is None else q_override
        return cycles * r.spec.cluster_count * r.spec.read_len * quality_factor(q)

    def projected_total_usable(self) -> float:
        """最终有效数据量投影：未失败区域按全部周期 + 当前质量系数估算。"""
        total = 0.0
        for r in self.regions.values():
            if r.status == RegionStatus.RERUN_REQUIRED or r.recovery == Recovery.RERUN.value:
                continue
            total += r.spec.total_cycles * r.spec.cluster_count * r.spec.read_len * quality_factor(r.signal_q)
        return total

    # ------------------------------------------------------------- 影响推算

    def preview_action(self, action: str, region_id: str, **kwargs) -> Dict[str, Any]:
        """执行前预估：运行 ETA 延迟、后续批次延迟、有效数据量增减（保守/乐观）。"""
        r = self._require_region(region_id)
        cfg = self.config
        baseline_eta = self.run_eta_minutes()
        if action == "pause":
            minutes = float(kwargs.get("minutes", 30))
            return {
                "action": "pause",
                "region_id": region_id,
                "eta_delay_min": minutes,
                "batch_delay_min": minutes,
                "usable_delta_conservative": -int(
                    (minutes / cfg.cycle_minutes)
                    * r.spec.cluster_count
                    * r.spec.read_len
                    * quality_factor(r.signal_q)
                ),
                "usable_delta_optimistic": 0,
                "note": "暂停不消耗试剂；恢复后保守估计损失暂停窗口的潜在产出",
            }
        if action == "recalibrate":
            new_q = min(1.0, r.signal_q + (1.0 - r.signal_q) * cfg.calibration_gain)
            usable_now = self.projected_total_usable()
            saved = self._project_with_quality(region_id, new_q) - usable_now
            return {
                "action": "recalibrate",
                "region_id": region_id,
                "eta_delay_min": cfg.calibration_minutes,
                "batch_delay_min": cfg.calibration_minutes,
                "usable_delta_conservative": 0,
                "usable_delta_optimistic": int(saved),
                "signal_q_before": r.signal_q,
                "signal_q_after": round(new_q, 4),
                "note": f"校准停机 {cfg.calibration_minutes:.0f} 分钟，质量提升带来有效数据量回升",
            }
        if action == "slowdown":
            new_speed = float(kwargs.get("speed", cfg.slow_speed))
            if new_speed >= r.speed:
                raise ValueError("降低读取速度要求新倍率小于当前倍率")
            extra = r.remaining_cycles() * cfg.cycle_minutes * (1.0 / new_speed - 1.0 / r.speed)
            new_q = 1.0 - (1.0 - r.signal_q) * cfg.slowdown_quality_gain
            saved = self._project_with_quality(region_id, new_q) - self.projected_total_usable()
            return {
                "action": "slowdown",
                "region_id": region_id,
                "eta_delay_min": round(extra, 1),
                "batch_delay_min": round(extra, 1),
                "usable_delta_conservative": 0,
                "usable_delta_optimistic": int(saved),
                "speed_after": new_speed,
                "note": "放慢读取不损失周期，仅延后完成时间并小幅改善质量",
            }
        raise ValueError(f"未知操作 {action}")

    def _project_with_quality(self, region_id: str, new_q: float) -> float:
        total = 0.0
        for rid, r in self.regions.items():
            if r.status == RegionStatus.RERUN_REQUIRED or r.recovery == Recovery.RERUN.value:
                continue
            q = new_q if rid == region_id else r.signal_q
            total += r.spec.total_cycles * r.spec.cluster_count * r.spec.read_len * quality_factor(q)
        return total

    def run_eta_minutes(self) -> float:
        active = [
            r for r in self.regions.values()
            if r.status not in (RegionStatus.FINISHED, RegionStatus.RERUN_REQUIRED, RegionStatus.PARTIAL)
        ]
        if not active:
            return 0.0
        return max(r.eta_minutes(self.config.cycle_minutes) for r in active)

    # ------------------------------------------------------------- 统一视图

    def snapshot(self) -> Dict[str, Any]:
        regions_view = []
        total_raw = 0
        total_usable = 0.0
        for rid in sorted(self.regions):
            r = self.regions[rid]
            usable = self.usable_bases(r)
            total_raw += r.raw_bases
            total_usable += usable
            regions_view.append(
                {
                    "region_id": rid,
                    "sample_id": r.sample_id,
                    "stage": r.stage.value,
                    "status": r.status.value,
                    "cycle": f"{r.cycles_done}/{r.spec.total_cycles}",
                    "signal_q": round(r.signal_q, 4),
                    "speed": r.speed,
                    "raw_bases": r.raw_bases,
                    "usable_bases": int(usable),
                    "usable_cycles": self.usable_cycles(r),
                    "incident_id": r.incident_id,
                    "recovery": r.recovery,
                    "eta_min": round(r.eta_minutes(self.config.cycle_minutes), 1),
                }
            )
        return {
            "run_id": self.run_id,
            "status": self.status.value if self.status else None,
            "elapsed_min": round(max((r.busy_min for r in self.regions.values()), default=0.0), 1),
            "eta_min": round(self.run_eta_minutes(), 1),
            "raw_bases": total_raw,
            "usable_bases": int(total_usable),
            "projected_final_usable_bases": int(self.projected_total_usable()),
            "reagents": [
                {
                    "sku": sku,
                    "remaining_ml": round(vol, 3),
                    "depleted_pct": round(
                        100.0 * (1.0 - vol / self._reagent_total(sku)), 1
                    ),
                }
                for sku, vol in sorted(self.reagents.items())
            ],
            "regions": regions_view,
            "incidents": [
                {
                    "incident_id": i.incident_id,
                    "type": i.itype.value,
                    "region_ids": i.region_ids,
                    "status": i.status,
                    "recovery": i.recovery.value if i.recovery else None,
                    "reason": i.reason,
                    "detail": i.detail,
                }
                for i in sorted(self.incidents.values(), key=lambda x: x.raised_seq)
            ],
        }

    def _reagent_total(self, sku: str) -> float:
        # 由 RunStarted 之后的补充事件累计：从事件流求总量
        total = 0.0
        for event in self.store.events:
            if event.type == "RunStarted":
                total += sum(x["total_ml"] for x in event.data["reagents"] if x["sku"] == sku)
            elif event.type == "ReagentReplenished" and event.data["sku"] == sku:
                total += event.data["added_ml"]
        return total

    # ------------------------------------------------------------- 辅助方法

    def _new_incident_id(self) -> str:
        self._inc_seq += 1
        n = self._inc_seq
        return f"INC-{n:03d}"

    def _open_exists(self, itype: IncidentType, scope) -> bool:
        return self._find_open(itype, scope) is not None

    def _find_open(self, itype: IncidentType, scope) -> Optional[IncidentState]:
        scope = set(scope)
        for inc in self.incidents.values():
            if (
                inc.status == "OPEN"
                and inc.itype == itype
                and (set(inc.region_ids) & scope if itype == IncidentType.REAGENT_SHORTAGE else set(inc.region_ids) == scope)
            ):
                return inc
        return None

    def _require_region(self, region_id: str) -> RegionState:
        if region_id not in self.regions:
            raise KeyError(f"未知区域 {region_id}")
        return self.regions[region_id]
