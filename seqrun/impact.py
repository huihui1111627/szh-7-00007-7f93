"""影响推算（what-if）：操作与故障对后续批次、最终有效数据量的影响。

所有推算为确定性投影，输入来自可重放状态，输出包括：
- 当前实际已得有效数据（Gb）与预计最终有效数据（Gb）
- 各批次预计开始/结束/延误（分钟）
- 指定操作（暂停、重校准、降速）带来的延误与质量增益

时间模型：单循环节拍 = 1 / 区域最小速度因子；批次节拍数 = 计划循环数。
"""
from __future__ import annotations

from typing import Any, Dict, List

from .engine import BASELINE_Q30, SPEED_QUALITY_GAIN
from .model import FaultKind, FaultState, Run, Stage


def _q30_for(speed: float, calibration: float = 0.0,
             open_crosstalk: bool = False, is_index: bool = False) -> float:
    q = BASELINE_Q30 + SPEED_QUALITY_GAIN * (1.0 - speed)
    q -= 0.02 * abs(calibration)
    if is_index:
        q += 0.02
    if open_crosstalk:
        q -= 0.18
    return max(0.0, min(1.0, q))


def _gb_per_full_cycle(run: Run) -> float:
    return 2e-4


def projected_sample_gb(run: Run, sample, open_crosstalk: bool = False) -> float:
    region = run.regions[run.sample_region[sample.sample_id]]
    total = 0.0
    for spec in run.reads:
        q = _q30_for(region.speed_factor, region.calibration_offset,
                     open_crosstalk, spec.read_type.value == "index")
        total += sample.clusters_millions * _gb_per_full_cycle(run) * q
    return total


def data_summary(run: Run) -> Dict[str, Any]:
    """有效数据量现状 + 按当前设置跑完全部循环的投影。"""
    actual = sum(s.effective_gb for s in run.samples.values())
    projected = 0.0
    lost_sealed = 0.0
    rerun_samples = []
    for s in run.samples.values():
        xt = any(
            f.state == FaultState.OPEN and f.kind == FaultKind.CROSSTALK
            and s.sample_id in f.sample_ids for f in run.faults.values())
        full = projected_sample_gb(run, s, xt)
        remaining_cycles = max(0, run.total_cycles() - s.total_done())
        region = run.regions[run.sample_region[s.sample_id]]
        future_gb = 0.0
        for spec in run.reads:
            q = _q30_for(region.speed_factor, region.calibration_offset,
                         xt, spec.read_type.value == "index")
            future_gb += s.clusters_millions * _gb_per_full_cycle(run) * q
        future_gb *= remaining_cycles / run.total_cycles()
        if s.stage == Stage.ISOLATED:
            # 未处置隔离：只能保留已得部分
            projected += s.effective_gb
            lost_sealed += full - s.effective_gb
        else:
            projected += s.effective_gb + future_gb
        if s.rerun_done:
            rerun_samples.append(s.sample_id)
    return {
        "actual_gb": round(actual, 3),
        "projected_final_gb": round(projected, 3),
        "at_risk_gb": round(max(0.0, lost_sealed), 3),
        "rerun_samples": rerun_samples,
    }


def batch_schedule(run: Run) -> List[Dict[str, Any]]:
    """按批次屏障推算计划/预计开始结束与延误。

    - 计划：无停机、不变速时，批次按完整读段节拍顺序开工；
    - 预计：已开始批次以实际开工/完成事件为准；未开始批次在前序
      批次预计结束之后开工，并叠加自身区域累计停机时间。
    """
    cycles = run.total_cycles()
    planned_cursor = 0.0
    projected_cursor = 0.0
    rows = []
    for bid in run.batch_order:
        batch = run.batches[bid]
        speeds = [run.regions[r].speed_factor for r in batch.region_ids]
        speed = min(speeds) if speeds else 1.0
        tick = 1.0 / speed
        downtime = sum(run.regions[r].downtime_min
                       for r in batch.region_ids)
        duration = cycles * tick
        samples = run.samples_of_batch(bid)
        max_done = max(
            (s.total_done() for s in samples), default=0)
        remaining_cycles = max(0, cycles - max_done)
        planned_start = planned_cursor
        if batch.finished_min is not None:
            projected_start = batch.started_min
            projected_finish = batch.finished_min
        elif batch.started_min is not None:
            projected_start = max(projected_cursor, batch.started_min)
            projected_finish = projected_start + max(
                0, downtime - max(0, run.clock_min - projected_start)) \
                + remaining_cycles * tick
        else:
            projected_start = projected_cursor + downtime
            projected_finish = projected_start + duration
        rows.append({
            "batch_id": bid,
            "planned_start_min": round(planned_start, 1),
            "planned_finish_min": round(planned_cursor + duration, 1),
            "projected_start_min": round(projected_start, 1),
            "projected_finish_min": round(projected_finish, 1),
            "delay_min": round(downtime, 1),
            "speed_factor": round(speed, 2),
            "cycles_done": max_done,
            "cycles_total": cycles,
        })
        planned_cursor += duration
        projected_cursor = max(projected_cursor, projected_finish)
    return rows


def estimate_pause_impact(run: Run, region_id: str,
                          duration_min: float) -> Dict[str, Any]:
    """what-if：暂停某区域 duration 分钟对后续批次与数据量的影响。"""
    if region_id not in run.regions:
        raise ValueError("区域不存在: %s" % region_id)
    before_sched = batch_schedule(run)
    affected_batch = next(
        (bid for bid in run.batch_order
         if region_id in run.batches[bid].region_ids), None)
    # 投影：暂停使所属批次结束推迟，其后批次连锁顺延
    cascaded = []
    started = False
    for row in before_sched:
        if row["batch_id"] == affected_batch:
            started = True
        if started:
            cascaded.append({
                "batch_id": row["batch_id"],
                "extra_delay_min": round(duration_min, 1),
            })
    # 暂停期间没有成像 -> 不产生数据损失（只是时间损失），
    # 但若批次已开始，其投影完成时间同步后移
    summary = data_summary(run)
    return {
        "operation": "pause_region",
        "region_id": region_id,
        "duration_min": duration_min,
        "affected_batch": affected_batch,
        "cascaded_batches": cascaded,
        "run_finish_delay_min": round(duration_min, 1),
        "projected_final_gb": summary["projected_final_gb"],
        "note": "暂停不损失已得数据；结束时间后移，后续批次连锁顺延",
    }


def estimate_speed_impact(run: Run, region_id: str,
                          new_speed: float) -> Dict[str, Any]:
    """what-if：调整读取速度 -> 节拍时间与每循环 Q30 的变化。"""
    if region_id not in run.regions:
        raise ValueError("区域不存在: %s" % region_id)
    region = run.regions[region_id]
    old = region.speed_factor
    cycles = run.total_cycles()
    time_delta = cycles * (1.0 / new_speed - 1.0 / old)
    q_old = _q30_for(old, region.calibration_offset)
    q_new = _q30_for(new_speed, region.calibration_offset)
    samples = run.samples_of_region(region_id)
    gb_delta = sum(
        s.clusters_millions * _gb_per_full_cycle(run)
        * (q_new - q_old) * cycles for s in samples)
    return {
        "operation": "set_speed",
        "region_id": region_id,
        "old_speed": old,
        "new_speed": new_speed,
        "extra_minutes": round(time_delta, 1),
        "q30_per_cycle_old": round(q_old, 4),
        "q30_per_cycle_new": round(q_new, 4),
        "effective_gb_delta": round(gb_delta, 3),
        "note": "降速以时间换质量；提速反之",
    }


def estimate_recalibration_impact(run: Run, region_id: str,
                                  new_offset: float) -> Dict[str, Any]:
    """what-if：重校准后对未完成循环的 Q30 修复幅度。"""
    region = run.regions[region_id]
    samples = run.samples_of_region(region_id)
    q_old = _q30_for(region.speed_factor, region.calibration_offset)
    q_new = _q30_for(region.speed_factor, new_offset)
    recoverable_gb = 0.0
    for s in samples:
        remaining = max(0, run.total_cycles() - s.total_done())
        recoverable_gb += s.clusters_millions * _gb_per_full_cycle(run) \
            * (q_new - q_old) * remaining
    return {
        "operation": "recalibrate",
        "region_id": region_id,
        "old_offset": region.calibration_offset,
        "new_offset": new_offset,
        "q30_per_cycle_old": round(q_old, 4),
        "q30_per_cycle_new": round(q_new, 4),
        "recoverable_gb": round(recoverable_gb, 3),
    }


def full_report(run: Run) -> Dict[str, Any]:
    return {
        "run_id": run.run_id,
        "state": run.state.value,
        "clock_min": round(run.clock_min, 1),
        "data": data_summary(run),
        "schedule": batch_schedule(run),
        "open_faults": [f.fault_id for f in run.open_faults()],
    }
