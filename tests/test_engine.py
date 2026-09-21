import json
import os
import tempfile
import unittest

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
from sequencing.engine import quality_factor


def spec(rid, sample, cycles=10, clusters=1_000_000, read_len=100, per_cycle=1.0, sku="KIT-A"):
    return RegionSpec(rid, sample, cycles, clusters, read_len, per_cycle, sku)


def make_engine(path=None, regions=None, reagents=None, cycle_minutes=10.0):
    cfg = RunConfig(
        run_id="RUN-T",
        regions=regions or [spec("R1", "S1"), spec("R2", "S2")],
        reagents=reagents or [ReagentSpec("KIT-A", 100.0)],
        cycle_minutes=cycle_minutes,
    )
    return SequencingEngine(EventStore(path), cfg)


class UnifiedViewTests(unittest.TestCase):
    def test_start_and_advance_snapshot(self):
        eng = make_engine()
        eng.start_run()
        eng.advance(3)
        snap = eng.snapshot()
        r1 = next(r for r in snap["regions"] if r["region_id"] == "R1")
        self.assertEqual(r1["cycle"], "3/10")
        self.assertEqual(r1["status"], "ACTIVE")
        self.assertGreater(r1["usable_bases"], 0)
        self.assertEqual(snap["reagents"][0]["remaining_ml"], 94.0)
        self.assertAlmostEqual(snap["elapsed_min"], 30.0)

    def test_region_finishes_automatically(self):
        eng = make_engine()
        eng.start_run()
        eng.advance(10)
        r1 = eng.regions["R1"]
        self.assertEqual(r1.status.value, "FINISHED")
        self.assertEqual(r1.stage.value, "DONE")
        self.assertEqual(eng.snapshot()["eta_min"], 0.0)


class IsolationTests(unittest.TestCase):
    def test_duplicate_sample_id_isolates_only_pair(self):
        eng = make_engine(
            regions=[spec("R1", "DUP"), spec("R2", "DUP"), spec("R3", "OTHER")]
        )
        eng.start_run()
        self.assertEqual(eng.regions["R1"].status.value, "QUARANTINED")
        self.assertEqual(eng.regions["R2"].status.value, "QUARANTINED")
        self.assertEqual(eng.regions["R3"].status.value, "ACTIVE")
        self.assertEqual(eng.status.value, "RUNNING")
        eng.advance(2)
        self.assertEqual(eng.regions["R1"].cycles_done, 0)
        self.assertEqual(eng.regions["R3"].cycles_done, 2)

    def test_reagent_shortage_holds_only_starving_region(self):
        eng = make_engine(
            regions=[spec("R1", "S1", per_cycle=3.0, sku="KIT-A"),
                     spec("R2", "S2", per_cycle=1.0, sku="KIT-B")],
            reagents=[ReagentSpec("KIT-A", 11.5), ReagentSpec("KIT-B", 100.0)],
        )
        eng.start_run()
        eng.advance(3)
        result = eng.advance(1)
        self.assertTrue(result["incidents"])
        self.assertEqual(eng.regions["R1"].status.value, "HELD")
        self.assertEqual(eng.regions["R2"].status.value, "ACTIVE")
        self.assertEqual(eng.status.value, "RUNNING")

    def test_shortage_scope_covers_all_regions_sharing_starving_sku(self):
        eng = make_engine(
            regions=[spec("R1", "S1", per_cycle=3.0), spec("R2", "S2", per_cycle=3.0)],
            reagents=[ReagentSpec("KIT-A", 11.5)],
        )
        eng.start_run()
        eng.advance(3)
        eng.advance(1)
        self.assertEqual(eng.regions["R1"].status.value, "HELD")
        self.assertEqual(eng.regions["R2"].status.value, "HELD")
        inc = next(iter(eng.incidents.values()))
        self.assertEqual(inc.region_ids, ["R1", "R2"])

    def test_crosstalk_isolates_scope_only(self):
        eng = make_engine(regions=[spec("R1", "S1"), spec("R2", "S2"), spec("R3", "S3")])
        eng.start_run()
        eng.advance(4)
        eng.raise_signal_crosstalk(["R1", "R2"])
        self.assertEqual(eng.regions["R1"].status.value, "QUARANTINED")
        self.assertEqual(eng.regions["R3"].status.value, "ACTIVE")

    def test_full_run_never_auto_fails(self):
        eng = make_engine()
        eng.start_run()
        eng.raise_signal_crosstalk(["R1", "R2"])
        self.assertEqual(eng.status.value, "RUNNING")


class RecoveryTests(unittest.TestCase):
    def test_conflict_continue_after_remap(self):
        eng = make_engine(regions=[spec("R1", "DUP"), spec("R2", "DUP")])
        eng.start_run()
        inc = list(eng.incidents)[0]
        self.assertEqual(eng.recommend_recovery(inc)["recovery"], Recovery.RERUN)
        eng.remap_sample("R2", "DUP-FIX")
        rec = eng.recommend_recovery(inc)
        self.assertEqual(rec["recovery"], Recovery.CONTINUE)
        eng.decide_recovery(inc)
        eng.resolve_incident(inc)
        self.assertEqual(eng.regions["R1"].status.value, "ACTIVE")
        self.assertIsNone(eng.regions["R2"].incident_id)

    def test_remap_to_existing_sample_rejected(self):
        eng = make_engine(regions=[spec("R1", "DUP"), spec("R2", "DUP"), spec("R3", "X")])
        eng.start_run()
        with self.assertRaises(ValueError):
            eng.remap_sample("R2", "X")

    def test_crosstalk_three_buckets(self):
        # 轻微：污染 1/5 => CONTINUE
        light = make_engine(regions=[spec("R1", "S1"), spec("R2", "S2")])
        light.start_run()
        light.advance(5)
        inc = light.raise_signal_crosstalk(["R1", "R2"], {"R1": [5], "R2": [5]})
        self.assertEqual(light.recommend_recovery(inc)["recovery"], Recovery.CONTINUE)
        light.recalibrate("R1")
        light.decide_recovery(inc)
        light.resolve_incident(inc)
        self.assertEqual(light.regions["R1"].status.value, "ACTIVE")

        # 中等：污染 2/5 => PARTIAL_KEEP，干净前缀 3 周期计入有效
        mid = make_engine(regions=[spec("R1", "S1"), spec("R2", "S2")])
        mid.start_run()
        mid.advance(5)
        inc = mid.raise_signal_crosstalk(["R1", "R2"], {"R1": [4, 5], "R2": [4, 5]})
        self.assertEqual(mid.recommend_recovery(inc)["recovery"], Recovery.PARTIAL_KEEP)
        mid.decide_recovery(inc)
        mid.resolve_incident(inc)
        self.assertEqual(mid.regions["R1"].status.value, "PARTIAL")
        self.assertEqual(mid.usable_cycles(mid.regions["R1"]), 3)

        # 严重：全部污染 => RERUN，有效数据归零但原始数据留档
        heavy = make_engine(regions=[spec("R1", "S1"), spec("R2", "S2")])
        heavy.start_run()
        heavy.advance(5)
        inc = heavy.raise_signal_crosstalk(["R1", "R2"])
        self.assertEqual(heavy.recommend_recovery(inc)["recovery"], Recovery.RERUN)
        heavy.decide_recovery(inc)
        heavy.resolve_incident(inc)
        self.assertEqual(heavy.regions["R1"].status.value, "RERUN_REQUIRED")
        self.assertEqual(heavy.usable_bases(heavy.regions["R1"]), 0.0)
        self.assertGreater(heavy.regions["R1"].raw_bases, 0)

    def test_shortage_partial_without_reagent_and_continue_after_refill(self):
        eng = make_engine(
            regions=[spec("R1", "S1", per_cycle=3.0, sku="KIT-A"),
                     spec("R2", "S2", sku="KIT-B")],
            reagents=[ReagentSpec("KIT-A", 11.5), ReagentSpec("KIT-B", 100.0)],
        )
        eng.start_run()
        eng.advance(3)
        inc = eng.advance(1)["incidents"][0]
        self.assertEqual(eng.recommend_recovery(inc)["recovery"], Recovery.PARTIAL_KEEP)
        eng.replenish_reagent("KIT-A", 30.0)
        self.assertEqual(eng.recommend_recovery(inc)["recovery"], Recovery.CONTINUE)
        eng.decide_recovery(inc)
        eng.resolve_incident(inc)
        self.assertEqual(eng.regions["R1"].status.value, "ACTIVE")

    def test_decide_requires_open_incident(self):
        eng = make_engine(regions=[spec("R1", "DUP"), spec("R2", "DUP")])
        eng.start_run()
        inc = list(eng.incidents)[0]
        eng.remap_sample("R2", "DUP-FIX")
        eng.decide_recovery(inc)
        eng.resolve_incident(inc)
        with self.assertRaises(RuntimeError):
            eng.decide_recovery(inc)


class ImpactPreviewTests(unittest.TestCase):
    def test_pause_preview_delays_batch_and_eta(self):
        eng = make_engine()
        eng.start_run()
        eng.advance(2)
        p = eng.preview_action("pause", "R1", minutes=30)
        self.assertEqual(p["eta_delay_min"], 30.0)
        self.assertEqual(p["batch_delay_min"], 30.0)
        self.assertLess(p["usable_delta_conservative"], 0)
        self.assertEqual(p["usable_delta_optimistic"], 0)

    def test_slowdown_preview_extends_eta_and_keeps_bases(self):
        eng = make_engine()
        eng.start_run()
        p = eng.preview_action("slowdown", "R1", speed=0.5)
        self.assertGreater(p["eta_delay_min"], 0)
        self.assertEqual(p["usable_delta_conservative"], 0)
        self.assertGreater(p["usable_delta_optimistic"], 0)

    def test_recalibration_preview_raises_quality(self):
        eng = make_engine()
        eng.start_run()
        before = eng.regions["R1"].signal_q
        p = eng.preview_action("recalibrate", "R1")
        self.assertEqual(p["eta_delay_min"], 20.0)
        self.assertGreater(p["signal_q_after"], before)
        self.assertGreater(p["usable_delta_optimistic"], 0)
        new_q = eng.recalibrate("R1")
        self.assertAlmostEqual(new_q, p["signal_q_after"], places=3)

    def test_pause_then_resume_only_changes_target_region(self):
        eng = make_engine(regions=[spec("R1", "S1"), spec("R2", "S2"), spec("R3", "S3")])
        eng.start_run()
        eng.pause_region("R1")
        self.assertEqual(eng.regions["R1"].status.value, "PAUSED")
        self.assertEqual(eng.regions["R2"].status.value, "ACTIVE")
        eng.advance(2)
        self.assertEqual(eng.regions["R1"].cycles_done, 0)
        self.assertEqual(eng.regions["R2"].cycles_done, 2)
        eng.resume_region("R1", minutes_paused=25)
        eng.advance(1)
        self.assertEqual(eng.regions["R1"].cycles_done, 1)
        self.assertGreater(eng.regions["R1"].busy_min, eng.regions["R2"].busy_min)


class AuditTests(unittest.TestCase):
    def test_restart_replay_restores_identical_snapshot(self):
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "run.jsonl")
        eng = make_engine(path, regions=[spec("R1", "DUP"), spec("R2", "DUP"), spec("R3", "X")])
        eng.start_run()
        eng.advance(3)
        inc = list(eng.incidents)[0]
        eng.remap_sample("R2", "DUP-FIX")
        eng.decide_recovery(inc)
        eng.resolve_incident(inc)
        eng.advance(2)
        before = eng.snapshot()

        rebuilt = SequencingEngine(EventStore(path))
        self.assertEqual(rebuilt.snapshot(), before)
        self.assertGreater(len(rebuilt.store.audit_trail()), 0)

    def test_tampered_event_rejected(self):
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "run.jsonl")
        eng = make_engine(path)
        eng.start_run()
        eng.advance(2)
        with open(path, "r", encoding="utf-8") as fh:
            lines = fh.readlines()
        row = json.loads(lines[2])
        row["data"]["cycles_done"] = 99
        lines[2] = json.dumps(row) + "\n"
        tampered = os.path.join(tmp, "bad.jsonl")
        with open(tampered, "w", encoding="utf-8") as fh:
            fh.writelines(lines)
        with self.assertRaises(TamperError):
            EventStore(tampered)

    def test_append_after_restart_continues_sequence(self):
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "run.jsonl")
        eng = make_engine(path)
        eng.start_run()
        n0 = len(EventStore(path).events)
        eng2 = SequencingEngine(EventStore(path))
        eng2.advance(1)
        store = EventStore(path)
        self.assertEqual([e.seq for e in store.events[-2:]], [n0 + 1, n0 + 2])
        self.assertTrue(all(e.type == "CycleAdvanced" for e in store.events[-2:]))
        self.assertEqual(eng2.regions["R1"].cycles_done, 1)
        store.verify()


if __name__ == "__main__":
    unittest.main()
