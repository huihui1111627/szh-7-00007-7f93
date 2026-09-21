"""运行实例的持久化门面：事件日志 + 快照 + 重启核对。"""
from __future__ import annotations

import os
from typing import Any, Dict

from .engine import Sequencer, rebuild
from .events import Journal
from .model import Run


def default_root() -> str:
    return os.path.join(os.getcwd(), ".runs")


def run_dir(root: str, run_id: str) -> str:
    return os.path.join(root, run_id)


def create(root: str, run_id: str) -> Sequencer:
    path = run_dir(root, run_id)
    journal = Journal(path)
    if journal.read_all():
        raise RuntimeError("运行已存在: %s（请使用 load）" % run_id)
    run = Run(run_id=run_id)
    return Sequencer(run, journal)


def load(root: str, run_id: str, verify: bool = True) -> Sequencer:
    """重启后加载：先校验哈希链，再从事件重放。"""
    path = run_dir(root, run_id)
    journal = Journal(path)
    if verify:
        result = journal.verify()
        if not result["ok"]:
            raise RuntimeError("日志核对失败: %s @ %s"
                               % (result["reason"], result.get("at_seq")))
    run = rebuild(run_id, journal)
    return Sequencer(run, journal)


def audit(root: str, run_id: str) -> Dict[str, Any]:
    journal = Journal(run_dir(root, run_id))
    result = journal.verify()
    if result["ok"]:
        run = rebuild(run_id, journal)
        result["run_state"] = run.state.value
        result["clock_min"] = round(run.clock_min, 3)
        result["samples"] = len(run.samples)
        result["faults"] = len(run.faults)
        result["open_faults"] = len(run.open_faults())
    return result
