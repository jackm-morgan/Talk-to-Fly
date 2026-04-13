"""Session serialization helpers for console and bridge clients."""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class SessionRecorder:
    root_dir: str = "sessions"
    started_at: float = field(default_factory=time.time)
    events: List[Dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        os.makedirs(self.root_dir, exist_ok=True)
        stamp = time.strftime("%Y%m%d_%H%M%S", time.localtime(self.started_at))
        self.session_id = f"session_{stamp}"
        self.jsonl_path = os.path.join(self.root_dir, f"{self.session_id}.jsonl")
        self.summary_path = os.path.join(self.root_dir, f"{self.session_id}_summary.txt")

    def record(self, kind: str, **payload: Any) -> None:
        event = {
            "timestamp": time.time(),
            "kind": kind,
            **payload,
        }
        self.events.append(event)
        with open(self.jsonl_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")

    def record_command(self, command: str) -> None:
        self.record("command", command=command)

    def record_task(self, task: str) -> None:
        self.record("task", task=task)

    def record_plan(self, mission, *, compiled_steps: Optional[List[str]] = None) -> None:
        review = dict(getattr(mission, "last_plan_review", {}) or {})
        self.record(
            "plan",
            mission_id=getattr(mission, "mission_id", None),
            objective=getattr(mission, "objective", None),
            plan_source=getattr(mission, "current_plan_source", None),
            dsl=getattr(mission, "current_plan", None),
            steps=compiled_steps if compiled_steps is not None else list(getattr(mission, "current_plan_steps", [])),
            replan_count=getattr(mission, "replan_count", None),
            plan_review=review,
        )

    def record_mission(self, mission) -> None:
        self.record(
            "mission",
            mission_id=getattr(mission, "mission_id", None),
            status=getattr(mission, "status", None),
            objective=getattr(mission, "objective", None),
            completed_steps=list(getattr(mission, "completed_steps", [])),
            remaining_steps=list(getattr(mission, "current_plan_steps", [])[getattr(mission, "next_step_idx", 0):]),
            last_failure=getattr(mission, "last_failure", None),
            replan_count=getattr(mission, "replan_count", None),
            local_recovery_count=getattr(mission, "local_recovery_count", None),
            plan_review=getattr(mission, "last_plan_review", None),
        )

    def recent_event_lines(self, limit: int = 8) -> List[str]:
        lines: List[str] = []
        for event in self.events[-limit:]:
            kind = event.get("kind")
            if kind == "task":
                lines.append(f"Task: {event.get('task')}")
            elif kind == "command":
                lines.append(f"Command: {event.get('command')}")
            elif kind == "plan":
                review = event.get('plan_review') or {}
                suffix = ' | clarify' if review.get('requires_clarification') else ''
                lines.append(f"Plan: {event.get('mission_id')} [{event.get('plan_source')}] {len(event.get('steps') or [])} step(s){suffix}")
            elif kind == "mission":
                rec = event.get('local_recovery_count')
                extra = f" | local fixes={rec}" if rec else ''
                lines.append(f"Mission: {event.get('mission_id')} -> {event.get('status')}{extra}")
            else:
                lines.append(f"{kind}: recorded")
        return lines

    def build_summary_lines(self) -> List[str]:
        tasks = [e for e in self.events if e.get("kind") == "task"]
        missions = [e for e in self.events if e.get("kind") == "mission"]
        commands = [e for e in self.events if e.get("kind") == "command"]
        completed = sum(1 for e in missions if e.get("status") == "completed")
        failed = sum(1 for e in missions if e.get("status") == "failed")
        cancelled = sum(1 for e in missions if e.get("status") == "cancelled")
        clarifications = sum(1 for e in missions if e.get("status") == "awaiting_clarification")
        local_recoveries = sum(int(e.get('local_recovery_count') or 0) for e in missions)
        lines = [
            f"Session ID      : {self.session_id}",
            f"Started         : {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(self.started_at))}",
            f"Natural tasks   : {len(tasks)}",
            f"Manual commands : {len(commands)}",
            f"Missions done   : {completed}",
            f"Missions failed : {failed}",
            f"Missions cancel : {cancelled}",
            f"Need clarify    : {clarifications}",
            f"Local fixes     : {local_recoveries}",
            f"Event log       : {self.jsonl_path}",
            f"Summary file    : {self.summary_path}",
        ]
        recent = self.recent_event_lines(limit=6)
        if recent:
            lines.append("")
            lines.append("Recent events:")
            lines.extend(f"- {line}" for line in recent)
        return lines

    def write_summary(self) -> List[str]:
        lines = self.build_summary_lines()
        with open(self.summary_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        return lines
