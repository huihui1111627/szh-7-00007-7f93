"""只追加事件存储：JSONL 持久化、SHA-256 哈希链、重放与篡改校验。"""

import hashlib
import json
import os
from dataclasses import dataclass, asdict
from typing import Any, Dict, List, Optional


class TamperError(RuntimeError):
    """事件日志哈希校验失败（被篡改、缺序或损坏）。"""


@dataclass
class Event:
    seq: int
    ts: str
    type: str
    data: Dict[str, Any]
    operator: str = "system"
    prev_hash: str = ""
    hash: str = ""

    def to_line(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, sort_keys=True)


def _digest(prev_hash: str, ts: str, etype: str, data: Dict[str, Any], operator: str) -> str:
    body = json.dumps(
        {"prev": prev_hash, "ts": ts, "type": etype, "data": data, "operator": operator},
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


class EventStore:
    def __init__(self, path: Optional[str] = None, clock=None):
        self.path = path
        self._clock = clock
        self.events: List[Event] = []
        if path and os.path.exists(path):
            self._load()

    @property
    def next_seq(self) -> int:
        return len(self.events) + 1

    def _now(self) -> str:
        return self._clock() if self._clock else _iso_now()

    def append(self, etype: str, data: Dict[str, Any], operator: str = "system") -> Event:
        prev_hash = self.events[-1].hash if self.events else ""
        ts = self._now()
        digest = _digest(prev_hash, ts, etype, data, operator)
        event = Event(self.next_seq, ts, etype, data, operator, prev_hash, digest)
        self.events.append(event)
        if self.path:
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(event.to_line() + "\n")
        return event

    def _load(self) -> None:
        with open(self.path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    self.events.append(Event(**json.loads(line)))
        self.verify()

    def replay(self) -> List[Event]:
        self.verify()
        return list(self.events)

    def verify(self) -> None:
        prev = ""
        for event in self.events:
            expected = _digest(prev, event.ts, event.type, event.data, event.operator)
            if event.prev_hash != prev or event.hash != expected:
                raise TamperError(f"事件 {event.seq} 哈希链校验失败")
            prev = event.hash

    def audit_trail(self) -> List[Dict[str, Any]]:
        return [
            {"seq": e.seq, "ts": e.ts, "type": e.type, "operator": e.operator, "data": e.data}
            for e in self.events
        ]


def _iso_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()
