#!/usr/bin/env python3
"""端到端示例：正常推进 → 三类故障隔离 → 三种恢复路径 → 操作影响预估 → 重启审计。

运行：python3 demo.py
"""

import json
import os
import tempfile

from sequencing import (
    EventStore,
    IncidentType,
    Recovery,
    RegionSpec,
    ReagentSpec,
    RunConfig,
    SequencingEngine,
    TamperError,
)


def build_config() -> RunConfig:
    return RunConfig(
        run_id="RUN-20260922-01",
        regions=[
            RegionSpec("L1", "SAM-1001", total_cycles=20, cluster_count=5_000_000,
                       read_len=150, reagent_per_cycle_ml=2.0, reagent_sku="KIT-A"),
            RegionSpec("L2", "SAM-1002", total_cycles=20, cluster_count=5_000_000,
                       read_len=150, reagent_per_cycle_ml=2.0, reagent_sku="KIT-A"),
            RegionSpec("L3", "SAM-1003", total_cycles=20, cluster_count=4_000_000,
                       read_len=150, reagent_per_cycle_ml=1.5, reagent_sku="KIT-B"),
            RegionSpec("L4", "SAM-1003", total_cycles=20, cluster_count=4_000_000,
                       read_len=150, reagent_per_cycle_ml=1.5, reagent_sku="KIT-B"),
        ],
        reagents=[ReagentSpec("KIT-A", 120.0), ReagentSpec("KIT-B", 30.0)],
        cycle_minutes=10.0,
    )


def show(view, title):
    print(f"\n===== {title} =====")
    for r in view["regions"]:
        print(
            f"  {r['region_id']} 样本={r['sample_id']:<9} 阶段={r['stage']:<11} "
            f"状态={r['status']:<16} 周期={r['cycle']:<6} Q={r['signal_q']:<5} "
            f"速率={r['speed']:<3} 有效={r['usable_bases']:>13,}"
        )
    print("  试剂:", {x["sku"]: f"剩{x['remaining_ml']}ml({x['depleted_pct']}%已耗)"
                        for x in view["reagents"]})
    print(f"  运行状态={view['status']} 已耗时={view['elapsed_min']}分 "
          f"ETA={view['eta_min']}分 有效合计={view['usable_bases']:,} "
          f"最终投影={view['projected_final_usable_bases']:,}")
    for inc in view["incidents"]:
        print(f"  事件单 {inc['incident_id']} [{inc['type']}] 范围={inc['region_ids']} "
              f"状态={inc['status']} 裁决={inc['recovery']} 理由={inc['reason']}")


def handle(engine, incident_id, label):
    rec = engine.recommend_recovery(incident_id)
    print(f"\n-- {label}: 系统建议 {rec['recovery'].value} — {rec['reason']}")
    engine.decide_recovery(incident_id)
    engine.resolve_incident(incident_id)


def main() -> None:
    tmp = tempfile.mkdtemp(prefix="seqrun-")
    journal = os.path.join(tmp, "run.jsonl")

    store = EventStore(journal)
    engine = SequencingEngine(store, build_config())
    engine.start_run(operator="zhang.lab")

    # 启动即检测到 L3/L4 样本标识冲突，二者被隔离，L1/L2 正常
    snap = engine.snapshot()
    show(snap, "启动后：L3/L4 标识冲突被隔离，L1/L2 继续")
    conflict = next(i for i in snap["incidents"]
                    if i["type"] == IncidentType.SAMPLE_ID_CONFLICT.value)["incident_id"]

    # 操作影响预估（不改变状态）
    preview = engine.preview_action("pause", "L1", minutes=30)
    print("\n-- 暂停 L1 30 分钟的预估：", json.dumps(preview, ensure_ascii=False))
    preview = engine.preview_action("slowdown", "L2", speed=0.6)
    print("-- 降低 L2 速率至 0.6 的预估：", json.dumps(preview, ensure_ascii=False))

    # 正常推进 6 个周期；KIT-B 仅 30ml，L3/L4 又被隔离不耗剂，KIT-A 充足
    engine.advance(6)
    show(engine.snapshot(), "推进 6 个周期后")

    # 路径一：标识冲突 → 重映射 → CONTINUE
    engine.remap_sample("L4", "SAM-1004")
    handle(engine, conflict, "标识冲突")
    engine.advance(2)
    show(engine.snapshot(), "冲突解除后继续读取（CONTINUE）")

    # 路径二：信号串扰（L3/L4 已有 2 个周期，全部污染 => 100% => RERUN）
    cross = engine.raise_signal_crosstalk(["L3", "L4"])
    handle(engine, cross, "信号串扰（污染占比高）")
    show(engine.snapshot(), "信号串扰：L3/L4 标记必须重跑（RERUN）")

    # 推进到第 18 周期时发生串扰，检测器定位到最近 6 个周期（13–18）被污染
    engine.advance(10)
    cross2 = engine.raise_signal_crosstalk(
        ["L1", "L2"],
        contaminated={"L1": list(range(13, 19)), "L2": list(range(13, 19))},
    )
    rec = engine.recommend_recovery(cross2)
    print(f"\n-- 中等范围串扰：系统建议 {rec['recovery'].value} — {rec['reason']}")
    engine.decide_recovery(cross2)
    engine.resolve_incident(cross2)
    show(engine.snapshot(), "中等范围串扰：切除污染段、保留污染前结果（PARTIAL_KEEP）")
    engine.complete_run()

    # 路径三：试剂不足 —— 用一个缺剂的新运行演示 CONTINUE
    shortage_demo(tmp)

    # 重启：丢弃内存聚合，仅靠日志重建并校验哈希链
    store2 = EventStore(journal)
    engine2 = SequencingEngine(store2)
    show(engine2.snapshot(), "进程重启后从事件日志重建的状态")
    assert engine2.snapshot()["usable_bases"] == engine.snapshot()["usable_bases"]
    print(f"\n审计链路共 {len(store2.audit_trail())} 个事件，哈希链校验通过")

    # 篡改检测
    path = journal
    with open(path, "r", encoding="utf-8") as fh:
        lines = fh.readlines()
    import json as _json
    row = _json.loads(lines[3])
    row["data"]["region_id"] = "LX"
    lines[3] = _json.dumps(row, ensure_ascii=False) + "\n"
    tampered = os.path.join(tmp, "tampered.jsonl")
    with open(tampered, "w", encoding="utf-8") as fh:
        fh.writelines(lines)
    try:
        EventStore(tampered)
    except TamperError as exc:
        print(f"篡改日志被拒绝：{exc}")


def shortage_demo(tmp) -> None:
    journal = os.path.join(tmp, "shortage.jsonl")
    cfg = RunConfig(
        run_id="RUN-SHORTAGE",
        regions=[
            RegionSpec("A1", "SAM-A", total_cycles=10, cluster_count=1_000_000,
                       read_len=100, reagent_per_cycle_ml=3.0, reagent_sku="KIT-X"),
            RegionSpec("A2", "SAM-B", total_cycles=10, cluster_count=1_000_000,
                       read_len=100, reagent_per_cycle_ml=3.0, reagent_sku="KIT-Y"),
        ],
        reagents=[ReagentSpec("KIT-X", 11.5), ReagentSpec("KIT-Y", 100.0)],
    )
    engine = SequencingEngine(EventStore(journal), cfg)
    engine.start_run()
    engine.advance(3)  # KIT-X 11.5ml 支撑 3 个周期（剩 2.5ml）
    result = engine.advance(1)  # 第 4 周期断供，A1 被隔离
    show(engine.snapshot(), "试剂不足：A1 隔离（HELD），A2 不受影响")
    inc = result["incidents"][0]
    print(f"\n-- 缺剂未补：{engine.recommend_recovery(inc)['recovery'].value}")
    engine.replenish_reagent("KIT-X", 24.0)
    rec = engine.recommend_recovery(inc)
    print(f"-- 补剂后：系统建议 {rec['recovery'].value} — {rec['reason']}")
    engine.decide_recovery(inc)
    engine.resolve_incident(inc)
    engine.advance(7)
    engine.complete_run()
    show(engine.snapshot(), "补剂解除后 A1 继续读完（CONTINUE）")


if __name__ == "__main__":
    main()
