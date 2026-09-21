"""命令行：操作测序运行、查看看板、what-if 推算、重启核对、端到端演示。

示例：
  python -m seqrun.cli demo --root .runs
  python -m seqrun.cli status RUN-DEMO --root .runs
  python -m seqrun.cli verify RUN-DEMO --root .runs
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import List

from . import store
from .dashboard import render_html, render_text
from .engine import EngineError
from .impact import (estimate_pause_impact, estimate_recalibration_impact,
                     estimate_speed_impact, full_report)
from .model import RecoveryAction


def main(argv: List[str] = None) -> int:
    parser = argparse.ArgumentParser(prog="seqrun")
    parser.add_argument("--root", default=store.default_root(),
                        help="运行实例根目录（也可在子命令后指定）")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_demo = sub.add_parser("demo")
    p_demo.add_argument("--root", default=None)
    p_status = sub.add_parser("status")
    p_status.add_argument("run_id")
    p_status.add_argument("--root", default=None)
    p_html = sub.add_parser("html")
    p_html.add_argument("run_id")
    p_html.add_argument("-o", "--output", default=None)
    p_html.add_argument("--root", default=None)
    p_json = sub.add_parser("json")
    p_json.add_argument("run_id")
    p_json.add_argument("--root", default=None)
    p_verify = sub.add_parser("verify")
    p_verify.add_argument("run_id")
    p_verify.add_argument("--root", default=None)

    p_pause = sub.add_parser("pause")
    p_pause.add_argument("run_id")
    p_pause.add_argument("region_id")
    p_pause.add_argument("--minutes", type=float, default=0.0)
    p_pause.add_argument("--root", default=None)
    p_resume = sub.add_parser("resume")
    p_resume.add_argument("run_id")
    p_resume.add_argument("region_id")
    p_resume.add_argument("--root", default=None)
    p_recal = sub.add_parser("recalibrate")
    p_recal.add_argument("run_id")
    p_recal.add_argument("region_id")
    p_recal.add_argument("offset", type=float)
    p_recal.add_argument("--root", default=None)
    p_speed = sub.add_parser("speed")
    p_speed.add_argument("run_id")
    p_speed.add_argument("region_id")
    p_speed.add_argument("factor", type=float)
    p_speed.add_argument("--root", default=None)
    p_tick = sub.add_parser("tick")
    p_tick.add_argument("run_id")
    p_tick.add_argument("minutes", type=float)
    p_tick.add_argument("--root", default=None)

    p_whatif = sub.add_parser("whatif")
    p_whatif.add_argument("run_id")
    p_whatif.add_argument("kind",
                          choices=["pause", "speed", "recalibrate"])
    p_whatif.add_argument("region_id")
    p_whatif.add_argument("value", type=float)
    p_whatif.add_argument("--root", default=None)

    p_resolve = sub.add_parser("resolve")
    p_resolve.add_argument("run_id")
    p_resolve.add_argument("fault_id")
    p_resolve.add_argument("action",
                           choices=[a.value for a in RecoveryAction])
    p_resolve.add_argument("--new-barcode", default=None)
    p_resolve.add_argument("--refill-ml", type=float, default=None)
    p_resolve.add_argument("--root", default=None)

    args = parser.parse_args(argv)
    if getattr(args, "root", None) is None:
        args.root = store.default_root()
    try:
        if args.cmd == "demo":
            return run_demo(args.root)
        if args.cmd == "status":
            eng = store.load(args.root, args.run_id)
            print(render_text(eng.run))
            return 0
        if args.cmd == "json":
            eng = store.load(args.root, args.run_id)
            print(json.dumps(full_report(eng.run), ensure_ascii=False,
                             indent=2))
            return 0
        if args.cmd == "html":
            eng = store.load(args.root, args.run_id)
            page = render_html(eng.run)
            out = args.output or os.path.join(
                store.run_dir(args.root, args.run_id), "dashboard.html")
            with open(out, "w", encoding="utf-8") as fh:
                fh.write(page)
            print("HTML 看板已导出: %s" % out)
            return 0
        if args.cmd == "verify":
            print(json.dumps(store.audit(args.root, args.run_id),
                             ensure_ascii=False, indent=2))
            return 0

        eng = store.load(args.root, args.run_id)
        if args.cmd == "pause":
            eng.pause_region(args.region_id, args.minutes,
                             reason="CLI 操作")
        elif args.cmd == "resume":
            eng.resume_region(args.region_id)
        elif args.cmd == "recalibrate":
            eng.recalibrate(args.region_id, args.offset, reason="CLI 操作")
        elif args.cmd == "speed":
            eng.set_speed(args.region_id, args.factor, reason="CLI 操作")
        elif args.cmd == "tick":
            eng.advance(args.minutes,
                        reagent_usage_ml={
                            "reagent1": args.minutes * 0.9,
                            "reagent2": args.minutes * 0.4})
        elif args.cmd == "whatif":
            if args.kind == "pause":
                result = estimate_pause_impact(
                    eng.run, args.region_id, args.value)
            elif args.kind == "speed":
                result = estimate_speed_impact(
                    eng.run, args.region_id, args.value)
            else:
                result = estimate_recalibration_impact(
                    eng.run, args.region_id, args.value)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        elif args.cmd == "resolve":
            eng.resolve_fault(args.fault_id, args.action,
                              new_barcode=args.new_barcode,
                              refill_ml=args.refill_ml)
        print(render_text(eng.run))
        return 0
    except EngineError as exc:
        print("操作被拒绝: %s" % exc, file=sys.stderr)
        return 2
    except RuntimeError as exc:
        print("错误: %s" % exc, file=sys.stderr)
        return 1


def run_demo(root: str) -> int:
    """端到端演示：覆盖看板、三类故障、三种恢复、what-if、重启核对。"""
    run_id = "RUN-DEMO"
    path = store.run_dir(root, run_id)
    if os.path.exists(os.path.join(path, "events.log")):
        import shutil
        shutil.rmtree(path)
    eng = store.create(root, run_id)

    # 1. 配置：R1 26 循环 + index 8 + R2 26；两个批次各两个区域
    eng.create_run(
        reads=[{"name": "R1", "read_type": "R1", "cycles": 26},
               {"name": "I7", "read_type": "index", "cycles": 8},
               {"name": "R2", "read_type": "R2", "cycles": 26}],
        reagents={"reagent1": 60.0, "reagent2": 30.0})
    eng.add_batch("B1")
    eng.add_region("RG-A", "A区", lane=1, tile_from=1, tile_to=4,
                   batch_id="B1")
    eng.add_region("RG-B", "B区", lane=1, tile_from=5, tile_to=8,
                   batch_id="B1")
    eng.add_batch("B2")
    eng.add_region("RG-C", "C区", lane=2, tile_from=1, tile_to=4,
                   batch_id="B2")
    eng.load_sample("S1", "RG-A", 1, 1, 10, 20, 10.0, barcodes=["BC-1"])
    eng.load_sample("S2", "RG-A", 1, 2, 30, 20, 12.0, barcodes=["BC-2"])
    eng.load_sample("S3", "RG-B", 1, 5, 10, 40, 11.0, barcodes=["BC-3"])
    eng.load_sample("S4", "RG-C", 2, 1, 10, 20, 10.0, barcodes=["BC-4"])
    eng.start()

    print("#1 初始看板")
    print(render_text(eng.run))

    # 2. 推进 10 个循环（10 min，1x 速度；用量为每分钟毫升数）
    eng.advance(10, reagent_usage_ml={"reagent1": 0.9, "reagent2": 0.4})
    print("\n#2 推进 10 个成像循环")
    print(render_text(eng.run))

    # 3. what-if：暂停 A 区 15 分钟
    print("\n#3 what-if：暂停 A 区 15 分钟的影响（不实际执行）")
    print(json.dumps(estimate_pause_impact(eng.run, "RG-A", 15),
                     ensure_ascii=False, indent=2))

    # 4. 实际暂停 A 区 15 分钟，其他区域继续
    eng.pause_region("RG-A", duration_min=15, reason="人工复核 S1")
    eng.advance(15, reagent_usage_ml={"reagent1": 0.8, "reagent2": 0.3})
    eng.resume_region("RG-A")
    print("\n#4 A 区实际暂停 15 分钟后恢复（S1/S2 落后于 S3）")
    print(render_text(eng.run))

    # 5. 信号串扰：A 区 S1/S2，污染从第 12 循环开始；先重校准再处置
    eng.advance(6, reagent_usage_ml={"reagent1": 0.5, "reagent2": 0.2})
    eng.report_crosstalk("RG-A", ["S1", "S2"], first_cycle=12,
                         message="A区 lane1 相邻 tile 信号串扰")
    eng.recalibrate("RG-A", 0.1, reason="串扰后重校准成像通道")
    print("\n#5 串扰被检测并按样本隔离（B1 的 S3、B2 的 S4 不受影响）")
    print(render_text(eng.run))
    # 污染循环尚未提交（R1 跑到 16/26），在线补偿 -> continue_read
    eng.resolve_fault("xt-1", RecoveryAction.CONTINUE_READ.value,
                      note="重校准后从第12循环重成像")
    print("\n#6 串扰处置：路径=continue_read，S1/S2 从第12循环重成像")
    print(render_text(eng.run))

    # 7. 标识冲突：S4 条码与新上样冲突 -> remap 继续
    eng.report_id_conflict("S4", "S1",
                           message="S4 条码与 S1 在索引读出时冲突")
    eng.resolve_fault("idc-1", RecoveryAction.REMAP.value,
                      new_barcode="BC-4B", note="实验室核对后重新指定")
    print("\n#7 标识冲突处置：路径=remap，S4 改码后继续读取")

    # 8. 降速 0.5x 的 what-if + 实际执行（B 区，换取质量）
    print("\n#8 what-if：B 区降速到 0.5x")
    print(json.dumps(estimate_speed_impact(eng.run, "RG-B", 0.5),
                     ensure_ascii=False, indent=2))
    eng.set_speed("RG-B", 0.5, reason="S3 信号波动，降速提质")

    # 9. 人为加大消耗使试剂1见底 -> 自动检测、区域隔离 -> 补液继续
    #    10 min 内用掉 55 ml（每分钟 5.5 ml），reagent2 保持低位
    eng.advance(10, reagent_usage_ml={"reagent1": 5.5, "reagent2": 0.1})
    print("\n#9 试剂1不足：当前批次未完成样本被隔离（已完成样本不受影响）")
    print(render_text(eng.run))
    open_rg = [f.fault_id for f in eng.run.open_faults()
               if f.kind.value == "reagent_low"]
    if open_rg:
        for fid in open_rg:
            eng.resolve_fault(fid, RecoveryAction.CONTINUE_READ.value,
                              refill_ml=60.0, note="更换试剂瓶后继续")
        print("\n#10 试剂处置：路径=continue_read（补液后解隔离继续）")

    # 10. 跑完剩余循环（恢复正常低消耗）
    eng.advance(200, reagent_usage_ml={"reagent1": 0.2, "reagent2": 0.1})
    print("\n#11 全部样本处理完毕")
    print(render_text(eng.run))

    # 11. 导出 HTML
    out = os.path.join(path, "dashboard.html")
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(render_html(eng.run))
    print("\nHTML 看板: %s" % out)

    # 12. 重启核对：重新加载并重放事件
    print("\n#12 重启后核对（哈希链校验 + 事件重放）")
    print(json.dumps(store.audit(root, run_id), ensure_ascii=False, indent=2))
    reloaded = store.load(root, run_id)
    print("重放状态: state=%s clock=%.1f 有效数据=%.3f Gb"
          % (reloaded.run.state.value, reloaded.run.clock_min,
             reloaded.run.effective_gb_total()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
