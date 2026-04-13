"""Mission lifecycle and runtime execution helpers."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
import time
import uuid

from talk_to_fly.core.monitor import MissionMonitor, DirectExecutionMonitor
from talk_to_fly.dsl.dsl import execute_dsl_stepwise
from talk_to_fly.llm.controller import plan_dsl, review_dsl, PlanReview
from talk_to_fly.logging.logger import log_status, log_verbose


@dataclass
class Mission:
    mission_id: str
    user_task: str
    objective: str
    created_at: float = field(default_factory=time.time)
    status: str = "pending"
    current_plan: str = ""
    current_plan_steps: List[str] = field(default_factory=list)
    current_plan_source: str = "initial"
    next_step_idx: int = 0
    completed_steps: List[str] = field(default_factory=list)
    execution_history: List[Dict[str, Any]] = field(default_factory=list)
    last_state: Dict[str, Any] = field(default_factory=dict)
    last_failure: Optional[Dict[str, Any]] = None
    replan_count: int = 0
    max_replans: int = 2
    pending_question: Optional[str] = None
    last_plan_review: Dict[str, Any] = field(default_factory=dict)
    local_recovery_count: int = 0

    @classmethod
    def create(cls, user_task: str, *, max_replans: int = 2) -> "Mission":
        return cls(
            mission_id=str(uuid.uuid4())[:8],
            user_task=user_task,
            objective=user_task,
            max_replans=max_replans,
        )

    def note_state(self, state: Dict[str, Any]) -> None:
        self.last_state = state or {}

    def set_plan(self, dsl: str, steps, *, source: str = "initial") -> None:
        self.current_plan = dsl or ""
        self.current_plan_source = source
        self.current_plan_steps = [step.rendered for step in steps]
        self.next_step_idx = 0
        self.pending_question = None
        if self.status in {"pending", "awaiting_clarification", "replanning"}:
            self.status = "executing"

    def set_unexecuted_plan(self, dsl: str, *, source: str = "initial") -> None:
        self.current_plan = dsl or ""
        self.current_plan_source = source
        self.current_plan_steps = []
        self.next_step_idx = 0

    def record_step(self, step, outcome) -> None:
        record = {
            "timestamp": time.time(),
            "command": step.rendered,
            "step_name": step.name,
            "args": list(step.args_list),
            "status": "success" if outcome.success else "failure",
            "reason": outcome.reason,
            "before_state": outcome.before_state,
            "after_state": outcome.after_state,
            "verified": getattr(outcome, "verified", False),
            "metrics": dict(getattr(outcome, "metrics", {}) or {}),
            "recovery_attempted": getattr(outcome, "recovery_attempted", False),
            "recovery_success": getattr(outcome, "recovery_success", False),
            "recovery_action": getattr(outcome, "recovery_action", None),
        }
        self.execution_history.append(record)
        self.note_state(outcome.after_state or outcome.before_state or {})

        if getattr(outcome, "recovery_success", False):
            self.local_recovery_count += 1

        if outcome.success:
            self.completed_steps.append(step.rendered)
            self.next_step_idx += 1
        else:
            self.last_failure = record

    def planner_history(self) -> List[str]:
        items: List[str] = []
        for rec in self.execution_history:
            cmd = rec.get("command", "<unknown>")
            if rec.get("status") == "success":
                if rec.get("recovery_success"):
                    items.append(f"{cmd}  # succeeded after local recovery: {rec.get('recovery_action')}")
                else:
                    items.append(cmd)
            else:
                reason = rec.get("reason", "unknown failure")
                items.append(f"{cmd}  # failed: {reason}")
        return items

    def can_replan(self) -> bool:
        return self.last_failure is not None and self.replan_count < self.max_replans

    def mission_context(self) -> str:
        remaining = self.current_plan_steps[self.next_step_idx:] if self.current_plan_steps else []
        lines = [
            f"Mission ID: {self.mission_id}",
            f"Objective: {self.objective}",
            f"Status: {self.status}",
            f"Replan attempts used: {self.replan_count}/{self.max_replans}",
            f"Local recoveries used: {self.local_recovery_count}",
            f"Completed steps: {self.completed_steps if self.completed_steps else []}",
            f"Remaining steps from current plan: {remaining if remaining else []}",
        ]
        if self.pending_question:
            lines.append(f"Pending clarification question: {self.pending_question}")
        if self.last_failure:
            lines.append(f"Last failure: {self.last_failure}")
        if self.last_plan_review:
            summary = self.last_plan_review.get("summary")
            if summary:
                lines.append(f"Last plan review summary: {summary}")
        return "\n".join(lines)

    def build_replan_request(self) -> str:
        failed_cmd = self.last_failure.get("command") if self.last_failure else "<unknown>"
        failure_reason = self.last_failure.get("reason") if self.last_failure else "unknown"
        remaining = self.current_plan_steps[self.next_step_idx:] if self.current_plan_steps else []
        return (
            f"Original mission: {self.objective}\n"
            f"Already completed successfully: {self.completed_steps if self.completed_steps else []}\n"
            f"Current plan failed while executing: {failed_cmd}\n"
            f"Failure reason: {failure_reason}\n"
            f"Local recoveries attempted so far: {self.local_recovery_count}\n"
            f"Remaining intended steps before failure: {remaining if remaining else []}\n\n"
            "From the CURRENT drone state, output only the remaining DSL plan needed to safely complete the mission. "
            "Do not repeat successful steps unless they are genuinely required for recovery. "
            "Keep the plan as short as possible and consistent with the current state and history."
        )


def mission_architecture(args) -> str:
    return str(getattr(args, "architecture", "agentic") or "agentic").strip().lower()


def mission_max_replans(args) -> int:
    if mission_architecture(args) == "one_shot":
        return 0
    return int(getattr(args, "max_replans", 2))


def _terminal_log_question(compiled_steps) -> Optional[str]:
    if not compiled_steps:
        return None
    last = compiled_steps[-1]
    if last.name not in ("l", "log"):
        return None

    if getattr(last, "args_list", None):
        text = str(last.args_list[0])
    elif getattr(last, "arg_string", None) is not None:
        text = str(last.arg_string)
    else:
        return None

    text = text.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in ('"', "'"):
        text = text[1:-1]
    return text.strip() or None


class AgenticMissionRunner:
    def __init__(self, drone, args, *, ui=None, session=None):
        self.drone = drone
        self.args = args
        self.monitor = MissionMonitor()
        self.ui = ui
        self.session = session

    def _plan(
        self,
        task_text: str,
        mission: Mission,
        *,
        plan_source: str,
        conversation_history=None,
    ) -> tuple[str, PlanReview]:
        mission.note_state(self.monitor.capture_state(self.drone))
        dsl, _timings = plan_dsl(
            task_text,
            self.drone,
            stream=False,
            show_spinner=True,
            print_plan=True,
            execution_history=mission.planner_history(),
            current_status=mission.last_state,
            mission_context=mission.mission_context(),
            planning_mode=plan_source,
            conversation_history=conversation_history,
        )
        review = review_dsl(
            task_text,
            dsl,
            self.drone,
            execution_history=mission.planner_history(),
            current_status=mission.last_state,
            mission_context=mission.mission_context(),
            planning_mode=plan_source,
            conversation_history=conversation_history,
        )
        mission.last_plan_review = review.to_dict()
        if review.summary:
            log_verbose(f"[PLAN REVIEW] {review.summary}")
        final_plan = review.final_dsl(dsl)
        if review.suggested_revision_needed and final_plan.strip() != (dsl or "").strip():
            log_status("[PLAN REVIEW] Self-critique revised the proposed DSL plan before execution.")
            log_verbose(f"[PLAN REVIEW] Revised plan:\n{final_plan}")
        return final_plan, review

    def _confirm(self, mission: Mission) -> bool:
        if not getattr(self.args, "confirm", False):
            return True
        prompt = f"\n\033[1;36mExecute {mission.current_plan_source} plan for mission {mission.mission_id}? [Y/N] \033[0m"
        return input(prompt).strip().lower() == "y"

    def _enter_clarification_state(self, mission: Mission, question: str, *, conversation=None) -> Mission:
        mission.status = "awaiting_clarification"
        mission.pending_question = question
        log_status(f"[MISSION] Mission {mission.mission_id} awaiting clarification.")
        if self.ui is not None:
            self.ui.print_mission_event(mission, heading="Mission Awaiting Clarification", tone="warn")
        if conversation is not None:
            conversation.set_pending_clarification(mission, question)
        if self.session is not None:
            self.session.record_mission(mission)
        return mission

    def run(
        self,
        task: str,
        *,
        mission: Optional[Mission] = None,
        plan_source: str = "initial",
        conversation=None,
    ) -> Mission:
        if mission is None:
            mission = Mission.create(task, max_replans=getattr(self.args, "max_replans", 2))

        current_request = task

        while True:
            dsl, review = self._plan(
                current_request,
                mission,
                plan_source=plan_source,
                conversation_history=(conversation.build_prompt_history() if conversation is not None else None),
            )

            if conversation is not None and dsl:
                conversation.add_assistant(dsl, kind="planner_output")

            if not dsl:
                mission.status = "failed"
                mission.last_failure = {
                    "command": "<planning>",
                    "reason": "Planner returned no DSL plan.",
                    "after_state": mission.last_state,
                }
                return mission

            mission.set_unexecuted_plan(dsl, source=plan_source)

            if review.requires_clarification:
                question = review.clarification_question or "Please clarify the remaining ambiguous part of the mission before execution."
                return self._enter_clarification_state(mission, question, conversation=conversation)

            try:
                preview_report = execute_dsl_stepwise(
                    dsl,
                    self.drone,
                    mission=mission,
                    monitor=self.monitor,
                    dry_run=True,
                    plan_source=plan_source,
                )
            except Exception as exc:
                mission.status = "failed"
                mission.last_failure = {
                    "command": "<compile>",
                    "reason": f"DSL compilation failed: {exc}",
                    "after_state": mission.last_state,
                }
                log_status(f"[MISSION] Planning failed: {exc}")
                if self.ui is not None:
                    self.ui.print_mission_event(mission, heading="Mission Failed", tone="bad")
                if self.session is not None:
                    self.session.record_mission(mission)
                return mission

            if self.ui is not None:
                self.ui.print_plan_preview(mission, preview_report.compiled_steps)
            if self.session is not None:
                self.session.record_plan(
                    mission,
                    compiled_steps=[step.rendered for step in preview_report.compiled_steps],
                )

            if not self._confirm(mission):
                log_status("[VERIFY] Execution cancelled.")
                mission.status = "cancelled"
                if self.ui is not None:
                    self.ui.print_mission_event(mission, heading="Mission Cancelled", tone="warn")
                if self.session is not None:
                    self.session.record_mission(mission)
                return mission

            report = execute_dsl_stepwise(
                dsl,
                self.drone,
                mission=mission,
                monitor=self.monitor,
                dry_run=False,
                plan_source=plan_source,
            )

            if report.success:
                question = _terminal_log_question(report.compiled_steps)
                if question is not None:
                    return self._enter_clarification_state(mission, question, conversation=conversation)

                mission.status = "completed"
                mission.pending_question = None
                log_status(f"[MISSION] Mission {mission.mission_id} completed successfully.")
                if self.ui is not None:
                    self.ui.print_mission_event(mission, heading="Mission Complete", tone="ok")
                if conversation is not None:
                    conversation.clear_pending_clarification()
                if self.session is not None:
                    self.session.record_mission(mission)
                return mission

            if not mission.can_replan():
                mission.status = "failed"
                log_status(f"[MISSION] Mission {mission.mission_id} failed and no replans remain.")
                if self.ui is not None:
                    self.ui.print_mission_event(mission, heading="Mission Failed", tone="bad")
                if conversation is not None:
                    conversation.clear_pending_clarification()
                if self.session is not None:
                    self.session.record_mission(mission)
                return mission

            mission.replan_count += 1
            plan_source = f"replan_{mission.replan_count}"
            log_status(f"[MISSION] Replanning attempt {mission.replan_count}/{mission.max_replans}...")
            current_request = mission.build_replan_request()
            mission.status = "replanning"
            log_verbose(f"[MISSION] Replan request:\n{current_request}")


class OneShotMissionRunner:
    supports_clarification = False
    supports_replanning = False

    def __init__(self, drone, args, *, ui=None, session=None):
        self.drone = drone
        self.args = args
        self.monitor = DirectExecutionMonitor()
        self.ui = ui
        self.session = session

    def _plan(
        self,
        task_text: str,
        mission: Mission,
        *,
        plan_source: str,
        conversation_history=None,
    ) -> tuple[str, PlanReview]:
        mission.note_state(self.monitor.capture_state(self.drone))
        dsl, _timings = plan_dsl(
            task_text,
            self.drone,
            stream=False,
            show_spinner=True,
            print_plan=True,
            execution_history=[],
            current_status={},
            mission_context=None,
            planning_mode=None,
            conversation_history=None,
        )
        mission.last_plan_review = {
            "summary": "One-shot baseline: no plan review, clarification loop, or replanning.",
        }
        return dsl, PlanReview(summary="One-shot baseline: no plan review.")

    def _confirm(self, mission: Mission) -> bool:
        if not getattr(self.args, "confirm", False):
            return True
        prompt = f"\n\033[1;36mExecute {mission.current_plan_source} plan for mission {mission.mission_id}? [Y/N] \033[0m"
        return input(prompt).strip().lower() == "y"

    def _enter_clarification_state(self, mission: Mission, question: str, *, conversation=None) -> Mission:
        mission.status = "failed"
        mission.pending_question = question
        mission.last_failure = {
            "command": "<clarification>",
            "reason": f"One-shot baseline produced a clarification request: {question}",
            "after_state": mission.last_state,
        }
        if self.ui is not None:
            self.ui.print_mission_event(mission, heading="Mission Failed", tone="bad")
        if self.session is not None:
            self.session.record_mission(mission)
        return mission

    def run(
        self,
        task: str,
        *,
        mission: Optional[Mission] = None,
        plan_source: str = "initial",
        conversation=None,
    ) -> Mission:
        if mission is None:
            mission = Mission.create(task, max_replans=0)

        dsl, _review = self._plan(
            task,
            mission,
            plan_source="one_shot",
            conversation_history=None,
        )

        if conversation is not None and dsl:
            conversation.add_assistant(dsl, kind="planner_output")

        if not dsl:
            mission.status = "failed"
            mission.last_failure = {
                "command": "<planning>",
                "reason": "Planner returned no DSL plan.",
                "after_state": mission.last_state,
            }
            return mission

        mission.set_unexecuted_plan(dsl, source="one_shot")

        try:
            preview_report = execute_dsl_stepwise(
                dsl,
                self.drone,
                mission=mission,
                monitor=self.monitor,
                dry_run=True,
                plan_source="one_shot",
            )
        except Exception as exc:
            mission.status = "failed"
            mission.last_failure = {
                "command": "<compile>",
                "reason": f"DSL compilation failed: {exc}",
                "after_state": mission.last_state,
            }
            log_status(f"[MISSION] One-shot planning failed: {exc}")
            if self.ui is not None:
                self.ui.print_mission_event(mission, heading="Mission Failed", tone="bad")
            if self.session is not None:
                self.session.record_mission(mission)
            return mission

        if self.ui is not None:
            self.ui.print_plan_preview(mission, preview_report.compiled_steps)
        if self.session is not None:
            self.session.record_plan(
                mission,
                compiled_steps=[step.rendered for step in preview_report.compiled_steps],
            )

        if not self._confirm(mission):
            log_status("[VERIFY] Execution cancelled.")
            mission.status = "cancelled"
            if self.ui is not None:
                self.ui.print_mission_event(mission, heading="Mission Cancelled", tone="warn")
            if self.session is not None:
                self.session.record_mission(mission)
            return mission

        report = execute_dsl_stepwise(
            dsl,
            self.drone,
            mission=mission,
            monitor=self.monitor,
            dry_run=False,
            plan_source="one_shot",
        )

        question = _terminal_log_question(report.compiled_steps) if report.success else None
        if question is not None:
            mission.status = "failed"
            mission.pending_question = question
            mission.last_failure = {
                "command": "<clarification>",
                "reason": f"One-shot baseline produced a clarification request: {question}",
                "after_state": mission.last_state,
            }
            log_status("[MISSION] One-shot baseline failed because the planner asked for clarification.")
            if self.ui is not None:
                self.ui.print_mission_event(mission, heading="Mission Failed", tone="bad")
            if self.session is not None:
                self.session.record_mission(mission)
            return mission

        if report.success:
            mission.status = "completed"
            mission.pending_question = None
            log_status(f"[MISSION] Mission {mission.mission_id} completed successfully.")
            if self.ui is not None:
                self.ui.print_mission_event(mission, heading="Mission Complete", tone="ok")
            if conversation is not None:
                conversation.clear_pending_clarification()
            if self.session is not None:
                self.session.record_mission(mission)
            return mission

        mission.status = "failed"
        if mission.last_failure is None:
            mission.last_failure = {
                "command": report.failed_step.rendered if report.failed_step is not None else "<execution>",
                "reason": report.failed_outcome.reason if report.failed_outcome is not None else "Execution failed.",
                "after_state": mission.last_state,
            }
        log_status(f"[MISSION] Mission {mission.mission_id} failed under one-shot execution.")
        if self.ui is not None:
            self.ui.print_mission_event(mission, heading="Mission Failed", tone="bad")
        if conversation is not None:
            conversation.clear_pending_clarification()
        if self.session is not None:
            self.session.record_mission(mission)
        return mission


def build_mission_runner(drone, args, *, ui=None, session=None):
    arch = mission_architecture(args)
    if arch == "one_shot":
        return OneShotMissionRunner(drone, args, ui=ui, session=session)
    return AgenticMissionRunner(drone, args, ui=ui, session=session)
