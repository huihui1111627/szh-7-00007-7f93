"""测序运行引擎：命令 -> 事件 -> 状态；时钟推进与故障检测。

设计要点：
- 所有状态变更只允许通过事件；重启后由事件重放精确重建。
- 运行级命令（暂停区域/重校准/降速/处置故障）立即生效并留痕。
- 故障按 sample/region 范围隔离，运行本身不会因局部故障整体失败。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from .events import Event, Journal
from .model import (
    Batch, CycleQuality, Fault, FaultKind, FaultState, Position, ReadSpec,
    ReadType, ReagentState, RecoveryAction, Region, Run, RunState, Sample,
    Stage,
)


# ---- 仪器常量（不随事件变化的工程参数） ----
GB_PER_CYCLE_PER_MCLUSTER = 2e-4
BASELINE_Q30 = 0.92
SPEED_QUALITY_GAIN = 0.06     # 降速到 0.5x 时的 Q30 提升
CROSSTALK_PENALTY = 0.18      # 未处置串扰对当循环 Q30 的拖累
LOW_REAGENT_FRACTION = 0.08   # 剩余比例低于该值即触发试剂不足
CROSSTALK_FIX_WINDOW = 2      # 最近 2 个未提交循环内可在线补偿


class EngineError(RuntimeError):
    """命令不合法或状态不允许（不产生事件）。"""


class Sequencer:
    def __init__(self, run: Run, journal: Journal):
        self.run = run
        self.journal = journal
        events = journal.read_all()
        self._last_hash = events[-1].hash if events else ""

    # ---------- 基础设施 ----------
    def _emit(self, kind: str, data: Dict[str, Any]) -> Event:
        seq = self.run.event_seq + 1
        prev = self._last_hash
        event = Event(seq=seq, at_min=self.run.clock_min, kind=kind,
                      data=data, prev_hash=prev)
        event.hash = event.compute_hash()
        apply_event(self.run, event)
        self.journal.append(event)
        self._last_hash = event.hash
        return event

    def now(self) -> float:
        return self.run.clock_min

    # ---------- 配置类命令 ----------
    def create_run(self, reads: List[Dict[str, Any]],
                   reagents: Dict[str, float]) -> Event:
        if self.run.event_seq > 0:
            raise EngineError("运行已存在，不能重复创建")
        read_specs = [
            ReadSpec(name=r["name"],
                     read_type=ReadType(r.get("read_type", "R1")),
                     cycles=int(r["cycles"]))
            for r in reads
        ]
        return self._emit("RunCreated", {
            "reads": [{"name": r.name, "read_type": r.read_type.value,
                       "cycles": r.cycles} for r in read_specs],
            "reagents": {k: float(v) for k, v in reagents.items()},
        })

    def add_batch(self, batch_id: str) -> Event:
        if batch_id in self.run.batches:
            raise EngineError("批次已存在: %s" % batch_id)
        return self._emit("BatchAdded", {"batch_id": batch_id})

    def add_region(self, region_id: str, name: str, lane: int,
                   tile_from: int, tile_to: int, batch_id: str) -> Event:
        if region_id in self.run.regions:
            raise EngineError("区域已存在: %s" % region_id)
        if batch_id not in self.run.batches:
            raise EngineError("批次不存在: %s" % batch_id)
        return self._emit("RegionAdded", {
            "region_id": region_id, "name": name, "lane": lane,
            "tile_from": tile_from, "tile_to": tile_to,
            "batch_id": batch_id,
        })

    def load_sample(self, sample_id: str, region_id: str, lane: int,
                    tile: int, x: int, y: int,
                    clusters_millions: float,
                    barcodes: Optional[List[str]] = None) -> Event:
        if sample_id in self.run.samples:
            raise EngineError("样本已存在: %s" % sample_id)
        if region_id not in self.run.regions:
            raise EngineError("区域不存在: %s" % region_id)
        barcodes = barcodes or []
        duplicate = [s for s in self.run.samples.values()
                     if sample_id in s.barcodes and set(barcodes) & set(s.barcodes)]
        return self._emit("SampleLoaded", {
            "sample_id": sample_id, "region_id": region_id,
            "position": {"lane": lane, "tile": tile, "x": x, "y": y},
            "clusters_millions": float(clusters_millions),
            "barcodes": list(barcodes),
            "barcode_duplicate_of": duplicate[0].sample_id if duplicate else None,
        })

    def start(self) -> Event:
        if self.run.state != RunState.SETUP:
            raise EngineError("仅 setup 状态可启动")
        return self._emit("RunStarted", {})

    # ---------- 运行期操作命令（实验人员） ----------
    def pause_region(self, region_id: str, duration_min: float = 0.0,
                     reason: str = "") -> Event:
        region = self._require_region(region_id)
        if region.paused:
            raise EngineError("区域已暂停: %s" % region_id)
        return self._emit("RegionPaused", {
            "region_id": region_id, "duration_min": float(duration_min),
            "reason": reason,
        })

    def resume_region(self, region_id: str) -> Event:
        region = self._require_region(region_id)
        if not region.paused:
            raise EngineError("区域未暂停: %s" % region_id)
        return self._emit("RegionResumed", {"region_id": region_id})

    def recalibrate(self, region_id: str, new_offset: float,
                    reason: str = "") -> Event:
        self._require_region(region_id)
        return self._emit("CalibrationAdjusted", {
            "region_id": region_id, "new_offset": float(new_offset),
            "reason": reason,
        })

    def set_speed(self, region_id: str, speed_factor: float,
                  reason: str = "") -> Event:
        self._require_region(region_id)
        if not 0.25 <= speed_factor <= 1.5:
            raise EngineError("速度因子须在 0.25~1.5 之间")
        return self._emit("SpeedChanged", {
            "region_id": region_id, "speed_factor": float(speed_factor),
            "reason": reason,
        })

    def _require_region(self, region_id: str) -> Region:
        if region_id not in self.run.regions:
            raise EngineError("区域不存在: %s" % region_id)
        return self.run.regions[region_id]

    # ---------- 故障申报（自动检测或人工上报） ----------
    def report_id_conflict(self, sample_id: str, duplicate_id: str,
                           message: str = "") -> Event:
        sample = self._require_sample(sample_id)
        dup = self._require_sample(duplicate_id)
        region_id = self.run.sample_region[sample_id]
        return self._emit("FaultDetected", {
            "fault_id": self._new_fault_id("idc"),
            "kind": FaultKind.ID_CONFLICT.value,
            "scope": "sample",
            "sample_ids": [sample.sample_id, dup.sample_id],
            "region_id": region_id,
            "batch_id": self.run.sample_batch[sample_id],
            "cycle": sample.current_cycle or None,
            "message": message or "样本标识冲突: %s <-> %s" % (sample_id, duplicate_id),
            "extra": {"duplicate_id": duplicate_id},
        })

    def report_crosstalk(self, region_id: str, sample_ids: List[str],
                         first_cycle: int, message: str = "") -> Event:
        self._require_region(region_id)
        for sid in sample_ids:
            self._require_sample(sid)
        batch_id = next(b for b in self.run.batch_order
                        if region_id in self.run.batches[b].region_ids)
        return self._emit("FaultDetected", {
            "fault_id": self._new_fault_id("xt"),
            "kind": FaultKind.CROSSTALK.value,
            "scope": "sample",
            "sample_ids": list(sample_ids),
            "region_id": region_id,
            "batch_id": batch_id,
            "cycle": int(first_cycle),
            "message": message or "区域 %s 发生信号串扰" % region_id,
            "extra": {"first_cycle": int(first_cycle)},
        })

    def _report_reagent_low(self, reagent: str, region_id: str,
                            sample_ids: List[str]) -> Event:
        batch_id = next(b for b in self.run.batch_order
                        if region_id in self.run.batches[b].region_ids)
        return self._emit("FaultDetected", {
            "fault_id": self._new_fault_id("rg"),
            "kind": FaultKind.REAGENT_LOW.value,
            "scope": "region",
            "sample_ids": list(sample_ids),
            "region_id": region_id,
            "batch_id": batch_id,
            "cycle": None,
            "message": "试剂 %s 剩余不足" % reagent,
            "extra": {"reagent": reagent},
        })

    def resolve_fault(self, fault_id: str, action: str,
                      new_barcode: Optional[str] = None,
                      refill_ml: Optional[float] = None,
                      note: str = "") -> Event:
        if fault_id not in self.run.faults:
            raise EngineError("故障不存在: %s" % fault_id)
        fault = self.run.faults[fault_id]
        if fault.state != FaultState.OPEN:
            raise EngineError("故障已处置: %s" % fault_id)
        act = RecoveryAction(action)
        data = {
            "fault_id": fault_id, "action": act.value,
            "new_barcode": new_barcode, "refill_ml": refill_ml, "note": note,
        }
        if fault.kind == FaultKind.ID_CONFLICT:
            if act not in (RecoveryAction.REMAP, RecoveryAction.KEEP_EXISTING):
                raise EngineError("标识冲突只支持 remap / keep_existing")
            if act == RecoveryAction.REMAP and not new_barcode:
                raise EngineError("remap 必须提供新的 new_barcode")
        elif fault.kind == FaultKind.CROSSTALK:
            if act not in (RecoveryAction.CONTINUE_READ, RecoveryAction.RERUN):
                raise EngineError("串扰只支持 continue_read / rerun")
            if act == RecoveryAction.CONTINUE_READ:
                first = int(fault.extra.get("first_cycle", 1))
                sample = self.run.samples[fault.sample_ids[0]]
                # first_cycle 是“当前读段内”的循环序号；只统计该读段
                read_name = sample.current_read or self.run.reads[0].name
                committed = sample.committed_cycles.get(read_name, 0)
                polluted_committed = max(0, committed - (first - 1))
                if polluted_committed > CROSSTALK_FIX_WINDOW:
                    raise EngineError(
                        "受污染循环已提交且超出在线补偿窗口，必须 rerun")
        elif fault.kind == FaultKind.REAGENT_LOW:
            if act not in (RecoveryAction.CONTINUE_READ,
                           RecoveryAction.KEEP_EXISTING):
                raise EngineError("试剂不足只支持 continue_read / keep_existing")
            if act == RecoveryAction.CONTINUE_READ and not refill_ml:
                raise EngineError("continue_read 必须补充试剂 refill_ml")
        events = [self._emit("FaultResolved", data)]
        # 恢复路径对应的配套事件（同一原子操作内全部留痕）
        if fault.kind == FaultKind.ID_CONFLICT:
            target = fault.sample_ids[0]
            if act == RecoveryAction.REMAP:
                events.append(self._emit("SampleBarcodeRemapped", {
                    "sample_id": target, "new_barcode": new_barcode,
                }))
            else:
                events.append(self._emit("SampleSealed",
                                         {"sample_id": target}))
        elif fault.kind == FaultKind.CROSSTALK:
            first_cycle = int(fault.extra.get("first_cycle", 1))
            if act == RecoveryAction.RERUN:
                for sid in fault.sample_ids:
                    events.append(self._emit("SampleRerunReset", {
                        "sample_id": sid}))
            else:
                for sid in fault.sample_ids:
                    events.append(self._emit("SampleReprocessed", {
                        "sample_id": sid, "from_cycle": first_cycle}))
        elif fault.kind == FaultKind.REAGENT_LOW:
            if act == RecoveryAction.CONTINUE_READ:
                reagent = fault.extra.get("reagent")
                events.append(self._emit("ReagentRefilled", {
                    "reagent": reagent, "ml": float(refill_ml)}))
                for sid in fault.sample_ids:
                    sample = self.run.samples[sid]
                    if sample.stage == Stage.ISOLATED:
                        events.append(self._emit("SampleResumedFromIsolation", {
                            "sample_id": sid}))
            else:
                for sid in fault.sample_ids:
                    events.append(self._emit("SampleSealed",
                                             {"sample_id": sid}))
        return events

    def _require_sample(self, sample_id: str) -> Sample:
        if sample_id not in self.run.samples:
            raise EngineError("样本不存在: %s" % sample_id)
        return self.run.samples[sample_id]

    def _new_fault_id(self, prefix: str) -> str:
        n = sum(1 for f in self.run.faults.values()
                if f.fault_id.startswith(prefix)) + 1
        return "%s-%d" % (prefix, n)

    # ---------- 时钟与成像推进 ----------
    def advance(self, minutes: float,
                reagent_usage_ml: Optional[Dict[str, float]] = None
                ) -> List[Event]:
        """推进仪器时间。

        时间模型（确定性仿真）：
        - 1 个仪器节拍 = 1 分钟 = 活跃样本完成 1 个成像循环；
        - 区域速度因子 v 决定“有效推进数” = round(v)（0.5x 时两个节拍
          才完成一个循环，由区域内部累计器实现，状态随事件持久化）；
        - 暂停区域在暂停期间节拍照常流逝，但不产生成像事件；
        - 每个节拍按 reagent_usage_ml 指定的固定量扣减试剂。
        """
        # PAUSED 仅表示存在被暂停区域：其他区域继续成像，不整体停机
        if self.run.state not in (RunState.RUNNING, RunState.PAUSED):
            raise EngineError("运行未在进行中")
        events = []
        ticks = int(round(minutes))
        for _ in range(ticks):
            batch = self._earliest_unfinished_batch()
            if batch is None and self._is_all_complete():
                break
            events.append(self._emit("ClockAdvanced", {"minutes": 1.0}))
            if batch is not None:
                events.extend(
                    self._one_cycle_tick(batch, reagent_usage_ml))
                self._complete_finished_batches(events)
        if self._is_all_complete():
            events.append(self._emit("RunCompleted", {}))
        return events

    def _complete_finished_batches(self, events) -> None:
        for bid in self.run.batch_order:
            batch = self.run.batches[bid]
            if batch.finished_min is not None:
                continue
            done = all(
                s.stage == Stage.COMPLETE
                or (s.stage == Stage.ISOLATED
                    and not _has_open_fault(self.run, s.sample_id))
                for s in self.run.samples_of_batch(bid))
            if done and batch.region_ids:
                events.append(self._emit("BatchCompleted",
                                         {"batch_id": bid}))

    def _earliest_unfinished_batch(self) -> Optional[Batch]:
        for bid in self.run.batch_order:
            batch = self.run.batches[bid]
            if batch.finished_min is not None:
                continue
            pending = False
            for s in self.run.samples_of_batch(bid):
                if s.stage == Stage.COMPLETE:
                    continue
                if s.stage == Stage.ISOLATED and not _has_open_fault(
                        self.run, s.sample_id):
                    continue
                pending = True  # 暂停/隔离样本使批次保持未完成（屏障）
                if s.paused:
                    continue
            if pending:
                return batch
        return None

    def _one_cycle_tick(self, batch: Batch,
                        reagent_usage_ml: Optional[Dict[str, float]]) -> List[Event]:
        cycle_events = []
        for region_id in batch.region_ids:
            region = self.run.regions[region_id]
            if region.paused:
                continue
            region.tick_accumulator += region.speed_factor
            if region.tick_accumulator < 1.0 - 1e-9:
                continue
            region.tick_accumulator -= 1.0
            for sample in self.run.samples_of_region(region_id):
                if sample.stage in (Stage.COMPLETE, Stage.ISOLATED):
                    continue
                next_rc = self.run.next_read_cycle(sample)
                if next_rc is None:
                    continue
                spec, cycle_no = next_rc
                quality = self._measure_quality(sample, region, spec, cycle_no)
                cycle_events.append(self._emit("CycleCompleted", {
                    "sample_id": sample.sample_id,
                    "read": spec.name, "cycle": cycle_no,
                    "q30_fraction": round(quality, 6),
                    "raw_intensity": round(
                        0.8 + 0.2 * region.speed_factor, 4),
                    "crosstalk": self._under_open_crosstalk(sample.sample_id),
                }))
                if cycle_no == spec.cycles:
                    cycle_events.append(self._emit("BaseCallCommitted", {
                        "sample_id": sample.sample_id, "read": spec.name,
                        "cycles": spec.cycles,
                    }))
        # 试剂消耗（每个节拍）
        for name, ml in (reagent_usage_ml or {}).items():
            if name in self.run.reagents and ml > 0:
                cycle_events.append(self._emit("ReagentConsumed", {
                    "reagent": name, "ml": float(ml),
                }))
        # 自动检测试剂不足：隔离当前批次未完成样本
        for name, reagent in self.run.reagents.items():
            low = reagent.remaining_ml <= 0 or \
                reagent.fraction < LOW_REAGENT_FRACTION
            if low and not reagent.low_alert_latched:
                affected = [s.sample_id for s in self.run.samples_of_batch(batch.batch_id)
                            if s.stage != Stage.COMPLETE]
                if affected:
                    for rid in batch.region_ids:
                        in_region = [sid for sid in affected
                                     if self.run.sample_region[sid] == rid]
                        if in_region:
                            cycle_events.append(
                                self._report_reagent_low(name, rid, in_region))
        return cycle_events

    def _under_open_crosstalk(self, sample_id: str) -> bool:
        return any(
            f.state == FaultState.OPEN and f.kind == FaultKind.CROSSTALK
            and sample_id in f.sample_ids
            for f in self.run.faults.values())

    def _measure_quality(self, sample: Sample, region: Region,
                         spec: ReadSpec, cycle_no: int) -> float:
        # 降速 -> 更稳的信号；校准偏移过大 -> 略降；串扰 -> 显著下降
        q = BASELINE_Q30 + SPEED_QUALITY_GAIN * (1.0 - region.speed_factor)
        q -= 0.02 * abs(region.calibration_offset)
        if self._under_open_crosstalk(sample.sample_id):
            q -= CROSSTALK_PENALTY
        # 索引读段略高
        if spec.read_type == ReadType.INDEX:
            q += 0.02
        return max(0.0, min(1.0, q))

    def _is_all_complete(self) -> bool:
        for s in self.run.samples.values():
            if s.stage == Stage.COMPLETE:
                continue
            if s.stage == Stage.ISOLATED and not _has_open_fault(
                    self.run, s.sample_id):
                continue
            return False
        return True


def _has_open_fault(run: Run, sample_id: str) -> bool:
    return any(
        f.state == FaultState.OPEN and sample_id in f.sample_ids
        for f in run.faults.values())


# ================= 事件 -> 状态（reducer，重启重放用） =================
def _gb_for_cycle(sample: Sample, q30: float) -> float:
    return sample.clusters_millions * GB_PER_CYCLE_PER_MCLUSTER * max(0.0, q30)


def apply_event(run: Run, event: Event) -> None:
    """纯函数式状态迁移；不得在此之外修改运行状态。"""
    run.event_seq = event.seq
    run.clock_min = event.at_min
    d = event.data
    kind = event.kind

    if kind == "RunCreated":
        run.reads = [
            ReadSpec(name=r["name"], read_type=ReadType(r["read_type"]),
                     cycles=int(r["cycles"]))
            for r in d["reads"]
        ]
        for name, cap in d["reagents"].items():
            run.reagents[name] = ReagentState(
                name=name, capacity_ml=float(cap), remaining_ml=float(cap))

    elif kind == "BatchAdded":
        run.batches[d["batch_id"]] = Batch(batch_id=d["batch_id"])
        run.batch_order.append(d["batch_id"])

    elif kind == "RegionAdded":
        region = Region(
            region_id=d["region_id"], name=d["name"], lane=d["lane"],
            tile_range=(d["tile_from"], d["tile_to"]))
        run.regions[region.region_id] = region
        run.batches[d["batch_id"]].region_ids.append(region.region_id)

    elif kind == "SampleLoaded":
        pos = d["position"]
        sample = Sample(
            sample_id=d["sample_id"],
            position=Position(pos["lane"], pos["tile"], pos["x"], pos["y"]),
            clusters_millions=d["clusters_millions"],
            barcodes=list(d.get("barcodes", [])),
            committed_cycles={r.name: 0 for r in run.reads},
        )
        run.samples[sample.sample_id] = sample
        region = run.regions[d["region_id"]]
        region.sample_ids.append(sample.sample_id)
        run.sample_region[sample.sample_id] = region.region_id
        for bid, batch in run.batches.items():
            if region.region_id in batch.region_ids:
                run.sample_batch[sample.sample_id] = bid
                break

    elif kind == "RunStarted":
        run.state = RunState.RUNNING

    elif kind == "RegionPaused":
        region = run.regions[d["region_id"]]
        region.paused = True
        for sid in region.sample_ids:
            run.samples[sid].paused = True
        region.downtime_min += float(d.get("duration_min", 0.0))
        run.state = RunState.PAUSED

    elif kind == "RegionResumed":
        region = run.regions[d["region_id"]]
        region.paused = False
        region.tick_accumulator = 0.0
        for sid in region.sample_ids:
            run.samples[sid].paused = False
        if not any(r.paused for r in run.regions.values()):
            run.state = RunState.RUNNING

    elif kind == "CalibrationAdjusted":
        run.regions[d["region_id"]].calibration_offset = float(d["new_offset"])

    elif kind == "SpeedChanged":
        region = run.regions[d["region_id"]]
        region.speed_factor = float(d["speed_factor"])
        region.tick_accumulator = 0.0

    elif kind == "CycleCompleted":
        sample = run.samples[d["sample_id"]]
        first_read = run.reads[0].name
        if sample.stage in (Stage.LOADING, Stage.CLUSTERING) \
                and not (d["read"] == first_read and int(d["cycle"]) >= 2):
            # 第一个成像循环同时标志簇生成完成
            if d["read"] == first_read and int(d["cycle"]) == 1:
                sample.stage = Stage.IMAGING
            else:
                sample.stage = Stage.CLUSTERING
        else:
            sample.stage = Stage.IMAGING
        sample.current_read = d["read"]
        sample.current_cycle = int(d["cycle"])
        sample.imaged_uncommitted += 1
        q = CycleQuality(
            read=d["read"], cycle=int(d["cycle"]),
            q30_fraction=float(d["q30_fraction"]),
            raw_intensity=float(d["raw_intensity"]),
            crosstalk=bool(d.get("crosstalk", False)))
        sample.qualities.append(q)
        batch = run.batch_of(sample.sample_id)
        if batch.started_min is None:
            batch.started_min = event.at_min

    elif kind == "ClusteringCompleted":
        run.samples[d["sample_id"]].stage = Stage.IMAGING

    elif kind == "BaseCallCommitted":
        sample = run.samples[d["sample_id"]]
        read_name = d["read"]
        n = int(d["cycles"])
        for q in sample.qualities:
            if q.read == read_name and not getattr(q, "counted", False):
                sample.effective_gb += _gb_for_cycle(sample, q.q30_fraction)
                q.counted = True
        sample.committed_cycles[read_name] = n
        sample.imaged_uncommitted = 0
        # 推进阶段
        if sample.total_done() >= run.total_cycles():
            sample.stage = Stage.COMPLETE
        else:
            sample.stage = Stage.BASE_CALLING

    elif kind == "ReagentConsumed":
        reagent = run.reagents[d["reagent"]]
        reagent.remaining_ml = max(0.0, reagent.remaining_ml - float(d["ml"]))

    elif kind == "FaultDetected":
        if d["kind"] == FaultKind.REAGENT_LOW.value:
            reagent_name = d.get("extra", {}).get("reagent")
            if reagent_name in run.reagents:
                run.reagents[reagent_name].low_alert_latched = True
        fault = Fault(
            fault_id=d["fault_id"], kind=FaultKind(d["kind"]),
            scope=d["scope"], sample_ids=list(d["sample_ids"]),
            region_id=d.get("region_id"), batch_id=d["batch_id"],
            cycle=d.get("cycle"), message=d.get("message", ""),
            detected_at_min=event.at_min, extra=dict(d.get("extra", {})))
        run.faults[fault.fault_id] = fault
        for sid in fault.sample_ids:
            sample = run.samples[sid]
            if sample.stage != Stage.COMPLETE:
                sample.stage = Stage.ISOLATED
                sample.isolated = True

    elif kind == "FaultResolved":
        _apply_recovery(run, d, event.at_min)

    elif kind == "SampleReprocessed":
        sample = run.samples[d["sample_id"]]
        from_cycle = int(d["from_cycle"])
        # 从当前读段的首个污染循环起重成像：丢弃该读段 >= from_cycle 的
        # 全部质量记录（含污染成像），已提交的早期循环保持有效
        current_read = sample.current_read or run.reads[0].name
        kept = [q for q in sample.qualities
                if not (q.read == current_read and q.cycle >= from_cycle)]
        committed = sample.committed_cycles.get(current_read, 0)
        if committed >= from_cycle:
            sample.committed_cycles[current_read] = from_cycle - 1
        removed_imaged = sum(
            1 for q in sample.qualities
            if q.read == current_read and q.cycle >= from_cycle)
        sample.qualities = kept
        sample.effective_gb = sum(
            _gb_for_cycle(sample, q.q30_fraction)
            for q in kept if not getattr(q, "counted", False))
        # 重新计算有效 Gb（被丢弃循环可能已计入）
        for q in kept:
            q.counted = False
        sample.effective_gb = sum(
            _gb_for_cycle(sample, q.q30_fraction) for q in kept)
        sample.imaged_uncommitted = max(
            0, sample.imaged_uncommitted - removed_imaged)
        sample.stage = Stage.IMAGING
        sample.isolated = False
        sample.current_cycle = from_cycle - 1
        sample.current_read = ""

    elif kind == "SampleRerunReset":
        sample = run.samples[d["sample_id"]]
        sample.committed_cycles = {r.name: 0 for r in run.reads}
        sample.qualities = []
        sample.effective_gb = 0.0
        sample.imaged_uncommitted = 0
        sample.current_cycle = 0
        sample.current_read = ""
        sample.stage = Stage.IMAGING
        sample.isolated = False
        sample.rerun_done = True

    elif kind == "SampleSealed":
        sample = run.samples[d["sample_id"]]
        # 只能保留已有结果：作为“部分完成”封存，不再参与读取
        sample.isolated = False
        sample.stage = Stage.COMPLETE if sample.qualities else Stage.ISOLATED

    elif kind == "SampleResumedFromIsolation":
        sample = run.samples[d["sample_id"]]
        sample.isolated = False
        sample.stage = Stage.BASE_CALLING if sample.qualities else Stage.IMAGING

    elif kind == "SampleBarcodeRemapped":
        sample = run.samples[d["sample_id"]]
        sample.barcodes = [d["new_barcode"]]
        sample.isolated = False
        if sample.total_done() >= run.total_cycles():
            sample.stage = Stage.COMPLETE
        elif sample.qualities:
            sample.stage = Stage.BASE_CALLING
        else:
            sample.stage = Stage.IMAGING

    elif kind == "ReagentRefilled":
        reagent = run.reagents[d["reagent"]]
        reagent.remaining_ml = min(
            reagent.capacity_ml, reagent.remaining_ml + float(d["ml"]))
        reagent.low_alert_latched = reagent.fraction < LOW_REAGENT_FRACTION

    elif kind == "ClockAdvanced":
        run.clock_min = event.at_min + float(d.get("minutes", 0.0))
        return

    elif kind == "RunCompleted":
        run.state = RunState.COMPLETED

    elif kind == "BatchCompleted":
        run.batches[d["batch_id"]].finished_min = event.at_min

    else:
        raise EngineError("未知事件类型: %s" % kind)


def _apply_recovery(run: Run, d: Dict[str, Any], at_min: float) -> None:
    fault = run.faults[d["fault_id"]]
    action = RecoveryAction(d["action"])
    fault.state = FaultState.RESOLVED
    fault.recovered_by = action
    fault.resolved_at_min = at_min
    fault.resolution_note = d.get("note", "")
    # 样本复位/解封/补液/改标识均由同一原子操作内的配套事件完成


def rebuild(run_id: str, journal: Journal) -> Run:
    """从空 Run 重放全部事件，重启后恢复状态。"""
    run = Run(run_id=run_id)
    # 先从事件流找到 RunCreated
    events = journal.read_all()
    for event in events:
        apply_event(run, event)
    return run
