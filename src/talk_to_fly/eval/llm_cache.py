"""Cache helpers for storing and replaying planner outputs during evaluation."""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional


@dataclass
class CachedPlan:
    key: str
    task: str
    dsl: str
    meta: Dict[str, Any]


def compute_key(task: str, category: Optional[str] = None, suite: Optional[str] = None, episode_id: Optional[str] = None) -> str:
    s = json.dumps(
        {"task": task, "category": category, "suite": suite, "episode_id": episode_id},
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


class LLMCache:
    """Very simple JSONL cache keyed by (task, category, suite, episode_id)."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._index: Dict[str, CachedPlan] = {}
        if self.path.exists():
            for line in self.path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    cp = CachedPlan(
                        key=obj["key"],
                        task=obj.get("task", ""),
                        dsl=obj.get("dsl", ""),
                        meta=obj.get("meta", {}) or {},
                    )
                    self._index[cp.key] = cp
                except Exception:
                    continue

    def get(self, key: str) -> Optional[CachedPlan]:
        return self._index.get(key)

    def put(self, key: str, task: str, dsl: str, meta: Dict[str, Any]):
        cp = CachedPlan(key=key, task=task, dsl=dsl, meta=meta)
        self._index[key] = cp
        rec = {
            "key": key,
            "task": task,
            "dsl": dsl,
            "meta": {**meta, "cached_at_epoch_s": time.time()},
        }
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
