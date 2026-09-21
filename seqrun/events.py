"""事件定义、序列化与事件日志（哈希链 + 快照）。

每次状态变更都追加一条不可变事件；事件间以 SHA-256 构成哈希链，
任何篡改都会在重启重放时被发现。
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from typing import Any, Dict, List


@dataclass
class Event:
    seq: int
    at_min: float
    kind: str
    data: Dict[str, Any]
    prev_hash: str = ""
    hash: str = ""

    def canonical(self) -> bytes:
        payload = {
            "seq": self.seq,
            "at_min": round(self.at_min, 6),
            "kind": self.kind,
            "data": self.data,
            "prev_hash": self.prev_hash,
        }
        return json.dumps(payload, sort_keys=True, ensure_ascii=False,
                          separators=(",", ":")).encode("utf-8")

    def compute_hash(self) -> str:
        return hashlib.sha256(self.canonical()).hexdigest()

    def to_json(self) -> Dict[str, Any]:
        return {
            "seq": self.seq,
            "at_min": self.at_min,
            "kind": self.kind,
            "data": self.data,
            "prev_hash": self.prev_hash,
            "hash": self.hash,
        }

    @staticmethod
    def from_json(obj: Dict[str, Any]) -> "Event":
        return Event(
            seq=obj["seq"],
            at_min=obj["at_min"],
            kind=obj["kind"],
            data=obj["data"],
            prev_hash=obj.get("prev_hash", ""),
            hash=obj.get("hash", ""),
        )


class JournalCorruption(RuntimeError):
    """事件日志内容损坏或无法解析。"""


class Journal:
    """追加写事件日志；每行一条 JSON（JSONL）。"""

    def __init__(self, run_dir: str):
        self.run_dir = run_dir
        os.makedirs(run_dir, exist_ok=True)
        self.path = os.path.join(run_dir, "events.log")

    def append(self, event: Event) -> None:
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(event.to_json(), ensure_ascii=False) + "\n")
            fh.flush()
            os.fsync(fh.fileno())

    def read_all(self) -> List[Event]:
        if not os.path.exists(self.path):
            return []
        events = []
        with open(self.path, "r", encoding="utf-8") as fh:
            for lineno, line in enumerate(fh, 1):
                line = line.strip()
                if line:
                    try:
                        obj = json.loads(line)
                        events.append(Event.from_json(obj))
                    except (ValueError, KeyError) as exc:
                        raise JournalCorruption(
                            "第 %d 行无法解析: %s" % (lineno, exc))
        return events

    def verify(self) -> Dict[str, Any]:
        """重放并校验哈希链，返回核对结果。"""
        try:
            events = self.read_all()
        except JournalCorruption as exc:
            return {"ok": False, "reason": "malformed-line",
                    "at_seq": str(exc)}
        prev = ""
        seqs = []
        for ev in events:
            expected = ev.compute_hash()
            if ev.prev_hash != prev:
                return {"ok": False, "reason": "broken-chain", "at_seq": ev.seq}
            if ev.hash != expected:
                return {"ok": False, "reason": "hash-mismatch", "at_seq": ev.seq}
            prev = ev.hash
            seqs.append(ev.seq)
        if seqs != list(range(1, len(seqs) + 1)):
            return {"ok": False, "reason": "seq-gap", "at_seq": seqs}
        return {"ok": True, "events": len(events),
                "first_seq": seqs[0] if seqs else None,
                "last_seq": seqs[-1] if seqs else None,
                "last_hash": prev}
