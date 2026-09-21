"""核心行为测试：不依赖第三方框架，python -m unittest 即可。"""
import os
import shutil
import tempfile
import unittest

from seqrun import store
from seqrun.engine import EngineError
from seqrun.impact import (data_summary, estimate_pause_impact,
                           estimate_speed_impact)
from seqrun.model import FaultKind, RecoveryAction, Stage
from seqrun.recovery import recommend


READS = [
    {"name": "R1", "read_type": "R1", "cycles": 10},
    {"name": "R2", "read_type": "R2", "cycles": 10},
]


class Case(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="seqrun-test-")
        self.eng = store.create(self.root, "R1")
        e = self.eng
        e.create_run(READS, {"reagent1": 100.0, "reagent2": 50.0})
        e.add_batch("B1")
        e.add_region("RG-A", "A", 1, 1, 4, "B1")
        e.add_region("RG-B", "B", 1, 5, 8, "B1")
        e.add_batch("B2")
        e.add_region("RG-C", "C", 2, 1, 4, "B2")
        e.load_sample("S1", "RG-A", 1, 1, 10, 20, 10.0, ["BC-1"])
        e.load_sample("S2", "RG-A", 1, 2, 30, 20, 10.0, ["BC-2"])
        e.load_sample("S3", "RG-B", 1, 5, 10, 40, 10.0, ["BC-3"])
        e.load_sample("S4", "RG-C", 2, 1, 10, 20, 10.0, ["BC-4"])
        e.start()

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def _tick(self, minutes=10, r1=9.0, r2=4.0):
        self.eng.advance(minutes,
                         reagent_usage_ml={"reagent1": r1, "reagent2": r2})

    # ---- 1. 看板基础数据随成像推进 ----
    def test_progress_and_dashboard(self):
        self._tick(5)
        run = self.eng.run
        s1 = run.samples["S1"]
        self.assertEqual(s1.current_cycle, 5)
        self.assertTrue(s1.qualities)
        self.assertEqual(s1.effective_gb, 0.0)  # R1 未满 10 循环尚未提交
        text = self.eng and __import__("seqrun.dashboard", fromlist=["render_text"]).render_text(run)
        self.assertIn("S1", text)
        self.assertIn("试剂", text)

    # ---- 2. 暂停只影响指定区域，其他区域继续 ----
    def test_pause_isolation_by_region(self):
        self._tick(5)
        self.eng.pause_region("RG-A", duration_min=10, reason="t")
        self._tick(10)
        s1 = self.eng.run.samples["S1"]
        s3 = self.eng.run.samples["S3"]
        self.assertEqual(s1.current_cycle, 5)   # A 区冻结
        self.assertEqual(s1.total_committed(), 0)
        # B 区继续：S3 已跑完 R1（10/10 提交）进入 R2，总进度领先
        self.assertGreater(s3.total_committed(), 0)
        self.eng.resume_region("RG-A")

    # ---- 3. what-if 暂停：后续批次连锁延误但不损失数据 ----
    def test_whatif_pause(self):
        result = estimate_pause_impact(self.eng.run, "RG-A", 20)
        self.assertEqual(result["run_finish_delay_min"], 20)
        self.assertIn("B1", [b["batch_id"] for b in result["cascaded_batches"]])
        self.assertIn("B2", [b["batch_id"] for b in result["cascaded_batches"]])

    # ---- 4. what-if 降速：时间增加、质量提升、Gb 增加 ----
    def test_whatif_speed(self):
        result = estimate_speed_impact(self.eng.run, "RG-A", 0.5)
        self.assertGreater(result["extra_minutes"], 0)
        self.assertGreater(result["q30_per_cycle_new"],
                           result["q30_per_cycle_old"])
        self.assertGreater(result["effective_gb_delta"], 0)

    # ---- 5. 串扰：隔离范围仅限受影响样本，运行不失败 ----
    def test_crosstalk_continue_read(self):
        self._tick(9)
        self.eng.report_crosstalk("RG-A", ["S1", "S2"], first_cycle=8)
        run = self.eng.run
        self.assertEqual(run.samples["S1"].stage, Stage.ISOLATED)
        self.assertNotEqual(run.samples["S3"].stage, Stage.ISOLATED)
        self.assertNotEqual(run.samples["S4"].stage, Stage.ISOLATED)
        # R1 尚未提交（10/10 边界：10 循环刚好提交？只有跑完读段才提交）
        self.eng.resolve_fault("xt-1", RecoveryAction.CONTINUE_READ.value)
        self.assertNotEqual(run.samples["S1"].stage, Stage.ISOLATED)

    # ---- 6. 串扰污染已提交 -> 必须 rerun ----
    def test_crosstalk_must_rerun(self):
        # 跑完 R1（10 循环，提交），再申报从第 9 循环开始的串扰
        self._tick(10)
        self.assertEqual(self.eng.run.samples["S1"].committed_cycles["R1"], 10)
        self.eng.report_crosstalk("RG-A", ["S1"], first_cycle=7)
        with self.assertRaises(EngineError):
            self.eng.resolve_fault("xt-1",
                                   RecoveryAction.CONTINUE_READ.value)
        self.eng.resolve_fault("xt-1", RecoveryAction.RERUN.value)
        s1 = self.eng.run.samples["S1"]
        self.assertEqual(s1.total_committed(), 0)
        self.assertEqual(s1.stage, Stage.IMAGING)
        self.assertTrue(s1.rerun_done)

    # ---- 7. 标识冲突：remap 后继续，或 keep_existing 封存 ----
    def test_id_conflict_remap(self):
        self._tick(6)
        self.eng.report_id_conflict("S4", "S1", message="dup")
        with self.assertRaises(EngineError):
            self.eng.resolve_fault("idc-1", RecoveryAction.RERUN.value)
        self.eng.resolve_fault("idc-1", RecoveryAction.REMAP.value,
                               new_barcode="BC-4X")
        s4 = self.eng.run.samples["S4"]
        self.assertEqual(s4.barcodes, ["BC-4X"])
        self.assertNotEqual(s4.stage, Stage.ISOLATED)

    # ---- 8. 试剂不足自动隔离，补液后继续 ----
    def test_reagent_low_refill_continue(self):
        self._tick(10)
        # 试剂1 容量100，已耗9，再让剩余跌破阈值
        self.eng.advance(5, reagent_usage_ml={"reagent1": 90.0,
                                              "reagent2": 2.0})
        low = [f for f in self.eng.run.faults.values()
               if f.kind == FaultKind.REAGENT_LOW]
        self.assertTrue(low)
        # 受影响仅是当前批次 B1 的未完成样本
        fault = low[0]
        self.assertNotIn("S4", fault.sample_ids)
        self.eng.resolve_fault(fault.fault_id,
                               RecoveryAction.CONTINUE_READ.value,
                               refill_ml=50.0)
        self.assertEqual(self.eng.run.reagents["reagent1"].remaining_ml,
                         50.0)

    # ---- 9. 只能保留已有结果：封存样本，其余继续，运行可完成 ----
    def test_keep_existing_seals_partial(self):
        self._tick(10)
        self.eng.report_id_conflict("S2", "S1", message="dup")
        before_gb = self.eng.run.samples["S2"].effective_gb
        self.eng.resolve_fault("idc-1",
                               RecoveryAction.KEEP_EXISTING.value)
        s2 = self.eng.run.samples["S2"]
        self.assertEqual(s2.stage, Stage.COMPLETE)
        self.assertAlmostEqual(s2.effective_gb, before_gb, places=6)

    # ---- 10. 整次运行能跑完并统计 Gb ----
    def test_run_completes(self):
        # 用量足够小，避免触发试剂不足
        self._tick(200, r1=0.1, r2=0.05)
        run = self.eng.run
        self.assertTrue(all(s.stage == Stage.COMPLETE
                            for s in run.samples.values()))
        summary = data_summary(run)
        self.assertGreater(summary["actual_gb"], 0)

    # ---- 11. 重启：哈希链校验 + 重放状态一致 ----
    def test_restart_replay_and_verify(self):
        self._tick(12)
        self.eng.pause_region("RG-A", duration_min=5)
        self.eng.resume_region("RG-A")
        self.eng.report_crosstalk("RG-A", ["S1"], first_cycle=10)
        audit = store.audit(self.root, "R1")
        self.assertTrue(audit["ok"])
        reloaded = store.load(self.root, "R1")
        self.assertEqual(reloaded.run.samples["S1"].current_cycle,
                         self.eng.run.samples["S1"].current_cycle)
        self.assertEqual(len(reloaded.run.faults), len(self.eng.run.faults))

    # ---- 12. 篡改事件日志 -> 校验失败 ----
    def test_tamper_detected(self):
        self._tick(6)
        log = os.path.join(self.root, "R1", "events.log")
        with open(log, "a", encoding="utf-8") as fh:
            fh.write('{"tampered":true}\n')
        audit = store.audit(self.root, "R1")
        self.assertFalse(audit["ok"])

    # ---- 13. what-if 重校准：偏移归零带来可回收 Gb ----
    def test_whatif_recalibrate(self):
        from seqrun.impact import estimate_recalibration_impact
        self.eng.recalibrate("RG-A", 2.0)
        self._tick(5, r1=0.1, r2=0.05)
        result = estimate_recalibration_impact(self.eng.run, "RG-A", 0.0)
        self.assertGreater(result["q30_per_cycle_new"],
                           result["q30_per_cycle_old"])
        self.assertGreater(result["recoverable_gb"], 0)

    # ---- 14. 恢复建议：未提交串扰 continue；已提交超窗口 rerun ----
    def test_recommend_paths(self):
        self._tick(9, r1=0.1, r2=0.05)
        self.eng.report_crosstalk("RG-A", ["S1"], first_cycle=8)
        rec = recommend(self.eng.run, self.eng.run.faults["xt-1"])
        self.assertEqual(rec["action"], RecoveryAction.CONTINUE_READ.value)
        self.eng.resolve_fault("xt-1",
                               RecoveryAction.CONTINUE_READ.value)
        # 跑完 R1 后再发生后期串扰 -> rerun
        self._tick(2, r1=0.1, r2=0.05)
        self.eng.report_crosstalk("RG-A", ["S1"], first_cycle=7)
        rec2 = recommend(self.eng.run, self.eng.run.faults["xt-2"])
        self.assertEqual(rec2["action"], RecoveryAction.RERUN.value)


if __name__ == "__main__":
    unittest.main()
