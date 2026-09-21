"""恢复路径策略：根据故障类型与数据状态判定三种路径。

路径：
- CONTINUE_READ  可继续读取（在线补偿/补液/改标识后无需重跑）
- RERUN          必须重跑受影响样本/区域
- KEEP_EXISTING  只能保留已有结果（封存部分数据）

判定只依赖可重放的运行状态，重启后结论一致。
"""
from __future__ import annotations

from typing import Dict, List

from .engine import CROSSTALK_FIX_WINDOW
from .model import Fault, FaultKind, RecoveryAction, Run, Stage


def recommend(run: Run, fault: Fault) -> Dict[str, object]:
    """返回 {action, reason, affected_samples, rerun_from_cycle}。"""
    if fault.kind == FaultKind.ID_CONFLICT:
        return _recommend_id_conflict(run, fault)
    if fault.kind == FaultKind.CROSSTALK:
        return _recommend_crosstalk(run, fault)
    return _recommend_reagent(run, fault)


def _recommend_id_conflict(run: Run, fault: Fault) -> Dict[str, object]:
    # 标识冲突本身不损坏信号：优先重新指定标识后继续；
    # 无法确认归属时封存已有结果，不影响同区域其他样本。
    return {
        "action": RecoveryAction.REMAP.value,
        "alternatives": [RecoveryAction.KEEP_EXISTING.value],
        "reason": "信号数据未受损，重新分配唯一标识即可继续读取",
        "affected_samples": list(fault.sample_ids),
    }


def _recommend_crosstalk(run: Run, fault: Fault) -> Dict[str, object]:
    first_cycle = int(fault.extra.get("first_cycle", 1))
    sample = run.samples[fault.sample_ids[0]]
    read_name = sample.current_read or run.reads[0].name
    committed = sample.committed_cycles.get(read_name, 0)
    done_in_read = committed + (
        sample.imaged_uncommitted if sample.current_read == read_name else 0)
    polluted_committed = max(0, committed - (first_cycle - 1))
    polluted_done = max(0, done_in_read - (first_cycle - 1))
    if polluted_done <= CROSSTALK_FIX_WINDOW and polluted_committed == 0:
        return {
            "action": RecoveryAction.CONTINUE_READ.value,
            "alternatives": [RecoveryAction.RERUN.value],
            "reason": "污染循环尚未提交（或在最近 %d 个在线补偿窗口内），"
                      "重校准后从第 %d 循环重成像即可"
                      % (CROSSTALK_FIX_WINDOW, first_cycle),
            "affected_samples": list(fault.sample_ids),
            "rerun_from_cycle": first_cycle,
        }
    return {
        "action": RecoveryAction.RERUN.value,
        "alternatives": [RecoveryAction.KEEP_EXISTING.value],
        "reason": "已有 %d 个受污染循环完成碱基判读并提交，超出在线补偿窗口，"
                  "受影响样本必须重跑" % polluted_committed,
        "affected_samples": list(fault.sample_ids),
        "rerun_from_cycle": first_cycle,
    }


def _recommend_reagent(run: Run, fault: Fault) -> Dict[str, object]:
    # 有补货渠道 -> 补液继续；否则封存未完成样本的已读部分
    unfinished = [sid for sid in fault.sample_ids
                  if run.samples[sid].stage in (Stage.IMAGING,
                                                Stage.BASE_CALLING,
                                                Stage.ISOLATED)]
    has_partial = any(run.samples[sid].total_done() > 0
                      for sid in fault.sample_ids)
    return {
        "action": RecoveryAction.CONTINUE_READ.value,
        "alternatives": [RecoveryAction.KEEP_EXISTING.value],
        "reason": "补充试剂后可继续未完成样本的读取"
                  + ("；若无补货则封存已有 %d 个样本的部分结果"
                     % len(fault.sample_ids) if has_partial else ""),
        "affected_samples": list(fault.sample_ids),
        "unfinished": unfinished,
    }


def summarize_recovery_paths(run: Run) -> List[Dict[str, object]]:
    """对当前所有未处置故障给出建议，供看板展示。"""
    out = []
    for fault in run.faults.values():
        if fault.state.value == "open":
            item = {"fault_id": fault.fault_id,
                    "kind": fault.kind.value,
                    "message": fault.message}
            item.update(recommend(run, fault))
            out.append(item)
    return out
