"""Bridge server used by external clients such as Mission Planner."""

from __future__ import annotations

import argparse
import json
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urlparse

from talk_to_fly.core.bootstrap import setup_environment
from talk_to_fly.core.conversation import ConversationMemory
from talk_to_fly.core.mission import build_mission_runner, mission_max_replans, Mission, _terminal_log_question
from talk_to_fly.dsl.dsl import execute_dsl_stepwise
from talk_to_fly.logging.logger import log_status, log_verbose
from talk_to_fly.ui.session import SessionRecorder


@dataclass
class BridgeEvent:
    seq: int
    timestamp: float
    level: str
    message: str
    mission_id: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "Seq": self.seq,
            "Timestamp": self.timestamp,
            "Level": self.level,
            "Message": self.message,
            "MissionId": self.mission_id,
        }


class MissionBridgeService:
    def __init__(self, args, drone, gps_logger):
        self.args = args
        self.drone = drone
        self.gps_logger = gps_logger
        self.conversation = ConversationMemory()
        self.session = SessionRecorder()
        self.runner = build_mission_runner(drone, args, ui=None, session=None)
        self.active_mission: Optional[Mission] = None
        self._events: List[BridgeEvent] = []
        self._event_seq = 0
        self._worker: Optional[threading.Thread] = None
        self._lock = threading.RLock()
        self._approval_required = bool(getattr(args, "confirm", False))
        self._shutdown_requested = False
        self._last_error: Optional[str] = None
        self._emit("info", "Mission Planner bridge initialised.")

    def _emit(self, level: str, message: str, mission: Optional[Mission] = None) -> None:
        with self._lock:
            self._event_seq += 1
            evt = BridgeEvent(
                seq=self._event_seq,
                timestamp=time.time(),
                level=level,
                message=message,
                mission_id=getattr(mission, "mission_id", None),
            )
            self._events.append(evt)
            if len(self._events) > 200:
                self._events = self._events[-200:]
        if level == "error":
            self._last_error = message
            log_status(f"[BRIDGE] {message}")
        else:
            log_verbose(f"[BRIDGE] {message}")

    def _worker_alive(self) -> bool:
        return self._worker is not None and self._worker.is_alive()

    def _require_not_busy(self) -> None:
        if self._worker_alive():
            raise RuntimeError("A mission is currently executing. Abort or wait for it to finish first.")

    def _serialize_drone(self) -> Dict[str, Any]:
        status = self.drone.get_status_dict()
        position = status.get("position") or {}
        velocity = status.get("velocity") or {}
        return {
            "FlightMode": status.get("flight_mode"),
            "Armed": status.get("armed"),
            "Simulation": status.get("is_simulation"),
            "BatteryPercent": status.get("battery_percent"),
            "HeadingDegrees": status.get("heading_degrees"),
            "GroundspeedMps": status.get("groundspeed_mps"),
            "PositionLat": position.get("lat"),
            "PositionLon": position.get("lon"),
            "PositionAlt": position.get("alt_agl"),
            "VelocityX": velocity.get("vx"),
            "VelocityY": velocity.get("vy"),
            "VelocityZ": velocity.get("vz"),
            "HistoryLength": status.get("history_len"),
        }

    def _serialize_mission(self, mission: Optional[Mission]) -> Optional[Dict[str, Any]]:
        if mission is None:
            return None
        last_failure_reason = None
        if mission.last_failure:
            last_failure_reason = mission.last_failure.get("reason")
        review_summary = None
        if mission.last_plan_review:
            review_summary = mission.last_plan_review.get("summary")
        return {
            "MissionId": mission.mission_id,
            "UserTask": mission.user_task,
            "Objective": mission.objective,
            "Status": mission.status,
            "CurrentPlan": mission.current_plan,
            "CurrentPlanSteps": list(mission.current_plan_steps),
            "CurrentPlanSource": mission.current_plan_source,
            "NextStepIdx": mission.next_step_idx,
            "CompletedSteps": list(mission.completed_steps),
            "PendingQuestion": mission.pending_question,
            "ReplanCount": mission.replan_count,
            "MaxReplans": mission.max_replans,
            "LocalRecoveryCount": mission.local_recovery_count,
            "ReviewSummary": review_summary,
            "LastFailureReason": last_failure_reason,
            "ExecutionHistoryCount": len(mission.execution_history),
            "LastState": dict(mission.last_state or {}),
        }

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            mission = self.active_mission
            events = [evt.to_dict() for evt in self._events[-40:]]
            pending = self.conversation.pending
            return {
                "Bridge": {
                    "ApprovalRequired": self._approval_required,
                    "WorkerAlive": self._worker_alive(),
                    "LastError": self._last_error,
                    "Connect": getattr(self.args, "connect", None),
                    "Simulation": bool(getattr(self.args, "simulation", False)),
                    "Verbose": bool(getattr(self.args, "verbose", False)),
                    "Confirm": bool(getattr(self.args, "confirm", False)),
                    "Timestamp": time.time(),
                },
                "Mission": self._serialize_mission(mission),
                "Conversation": {
                    "PendingClarification": pending is not None,
                    "PendingQuestion": pending.question if pending is not None else None,
                    "TurnCount": len(self.conversation.turns),
                },
                "Drone": self._serialize_drone(),
                "Events": events,
            }

    def ping(self) -> Dict[str, Any]:
        return self.snapshot()

    def submit_task(self, task_text: str) -> Dict[str, Any]:
        task_text = (task_text or "").strip()
        if not task_text:
            raise RuntimeError("Task text is empty.")

        with self._lock:
            self._require_not_busy()
            self.session.record_task(task_text)
            self.conversation.add_user(task_text)
            if self.conversation.has_pending_clarification():
                clarification_request, pending_mission = self.conversation.build_clarification_request(task_text)
                self.active_mission = pending_mission
                self._emit("info", f"Received clarification answer: {task_text}", mission=pending_mission)
                self._plan_until_pause(
                    clarification_request,
                    mission=pending_mission,
                    plan_source="clarification",
                    clear_pending_clarification_on_success=True,
                )
            else:
                self.active_mission = Mission.create(task_text, max_replans=mission_max_replans(self.args))
                self._emit("info", f"Received task: {task_text}", mission=self.active_mission)
                self._plan_until_pause(task_text, mission=self.active_mission, plan_source="initial")
            return self.snapshot()

    def submit_clarification(self, answer_text: str) -> Dict[str, Any]:
        return self.submit_task(answer_text)

    def approve_plan(self) -> Dict[str, Any]:
        with self._lock:
            mission = self.active_mission
            if mission is None:
                raise RuntimeError("No mission is active.")
            if mission.status != "awaiting_approval":
                raise RuntimeError(f"Mission is not awaiting approval. Current status: {mission.status}")
            self._require_not_busy()
            mission.status = "executing"
            self._emit("info", f"Executing approved plan ({mission.current_plan_source}).", mission=mission)
            worker = threading.Thread(target=self._execute_current_plan_worker, name=f"ttf-mission-{mission.mission_id}", daemon=True)
            self._worker = worker
            worker.start()
            return self.snapshot()

    def cancel_preview(self) -> Dict[str, Any]:
        with self._lock:
            mission = self.active_mission
            if mission is None:
                raise RuntimeError("No mission is active.")
            if mission.status not in {"awaiting_approval", "awaiting_clarification"}:
                raise RuntimeError(f"Mission cannot be cancelled in status {mission.status}.")
            mission.status = "cancelled"
            self.conversation.clear_pending_clarification()
            self._emit("warn", "Mission cancelled before execution.", mission=mission)
            return self.snapshot()

    def abort_mission(self) -> Dict[str, Any]:
        with self._lock:
            mission = self.active_mission
            if mission is None:
                raise RuntimeError("No mission is active.")
            self._emit("warn", "Abort requested. Calling land() on the vehicle.", mission=mission)
            try:
                self.drone.land()
            except Exception as exc:
                mission.status = "failed"
                mission.last_failure = {"reason": f"Abort land() failed: {exc}"}
                self._emit("error", f"Abort land() failed: {exc}", mission=mission)
                return self.snapshot()
            mission.status = "cancelled"
            self._emit("warn", "Abort issued. Vehicle instructed to land.", mission=mission)
            return self.snapshot()

    def _plan_until_pause(
        self,
        task_text: str,
        *,
        mission: Mission,
        plan_source: str,
        clear_pending_clarification_on_success: bool = False,
    ) -> None:
        self._emit("info", f"Planning started ({plan_source}).", mission=mission)
        dsl, review = self.runner._plan(
            task_text,
            mission,
            plan_source=plan_source,
            conversation_history=self.conversation.build_prompt_history(),
        )

        if not dsl.strip():
            mission.status = "failed"
            mission.last_failure = {
                "command": "<planner>",
                "reason": "Planner returned no DSL plan.",
                "after_state": mission.last_state,
            }
            self._emit("error", "Planner returned no DSL plan.", mission=mission)
            return

        self.conversation.add_assistant(dsl, kind="planner_output")
        mission.set_unexecuted_plan(dsl, source=plan_source)

        if getattr(self.runner, "supports_clarification", True) and review.requires_clarification:
            question = review.clarification_question or "Please clarify the ambiguous part of the mission."
            self.runner._enter_clarification_state(mission, question, conversation=self.conversation)
            self._emit("warn", f"Clarification required: {question}", mission=mission)
            return

        try:
            preview_report = execute_dsl_stepwise(
                dsl,
                self.drone,
                mission=mission,
                monitor=self.runner.monitor,
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
            self._emit("error", f"DSL compilation failed: {exc}", mission=mission)
            return

        if clear_pending_clarification_on_success:
            self.conversation.clear_pending_clarification()

        self.session.record_plan(mission, compiled_steps=[step.rendered for step in preview_report.compiled_steps])

        if self._approval_required:
            mission.status = "awaiting_approval"
            self._emit("info", f"Plan ready for approval with {len(preview_report.compiled_steps)} compiled step(s).", mission=mission)
            return

        mission.status = "executing"
        self._emit("info", "Plan auto-approved because confirm mode is disabled.", mission=mission)
        self._execute_current_plan_worker()

    def _execute_current_plan_worker(self) -> None:
        mission = self.active_mission
        if mission is None:
            return
        try:
            report = execute_dsl_stepwise(
                mission.current_plan,
                self.drone,
                mission=mission,
                monitor=self.runner.monitor,
                dry_run=False,
                plan_source=mission.current_plan_source,
            )

            if report.success:
                question = _terminal_log_question(report.compiled_steps)
                if question is not None:
                    if getattr(self.runner, "supports_clarification", True):
                        self.runner._enter_clarification_state(mission, question, conversation=self.conversation)
                        self._emit("warn", f"Mission requested follow-up clarification: {question}", mission=mission)
                    else:
                        mission.status = "failed"
                        mission.pending_question = question
                        mission.last_failure = {
                            "command": "<clarification>",
                            "reason": f"One-shot baseline produced a clarification request: {question}",
                            "after_state": mission.last_state,
                        }
                        self.session.record_mission(mission)
                        self._emit("error", f"One-shot baseline failed because the planner asked for clarification: {question}", mission=mission)
                    return

                mission.status = "completed"
                mission.pending_question = None
                self.conversation.clear_pending_clarification()
                self.session.record_mission(mission)
                self._emit("info", "Mission completed successfully.", mission=mission)
                return

            if not getattr(self.runner, "supports_replanning", True) or not mission.can_replan():
                mission.status = "failed"
                self.conversation.clear_pending_clarification()
                self.session.record_mission(mission)
                reason = mission.last_failure.get("reason") if mission.last_failure else "unknown failure"
                self._emit("error", f"Mission failed and no replans remain: {reason}", mission=mission)
                return

            mission.replan_count += 1
            mission.status = "replanning"
            replan_source = f"replan_{mission.replan_count}"
            self._emit(
                "warn",
                f"Mission step failed. Building {replan_source} of {mission.max_replans}.",
                mission=mission,
            )
            current_request = mission.build_replan_request()
            self._plan_until_pause(current_request, mission=mission, plan_source=replan_source)
        except Exception as exc:
            mission.status = "failed"
            mission.last_failure = {"reason": str(exc)}
            self._emit("error", f"Execution worker crashed: {exc}", mission=mission)
        finally:
            with self._lock:
                self._worker = None

    def shutdown(self) -> None:
        if self._shutdown_requested:
            return
        self._shutdown_requested = True
        self._emit("info", "Bridge shutting down.")
        try:
            summary_lines = self.session.write_summary()
            for line in summary_lines:
                self._emit("info", line)
        except Exception:
            pass
        try:
            self.gps_logger.stop()
        except Exception:
            pass
        try:
            self.drone.close()
        except Exception:
            pass


class BridgeRequestHandler(BaseHTTPRequestHandler):
    server_version = "TalkToFlyBridge/0.1"

    @property
    def bridge(self) -> MissionBridgeService:
        return self.server.bridge_service  # type: ignore[attr-defined]

    def _send_json(self, code: int, payload: Dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        try:
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, OSError) as exc:
            log_verbose(f"[BRIDGE HTTP] Client disconnected before response could be sent: {exc}")

    def _read_form(self) -> Dict[str, str]:
        length = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(length).decode("utf-8") if length > 0 else ""
        form = parse_qs(raw, keep_blank_values=True)
        return {key: values[-1] if values else "" for key, values in form.items()}

    def _success(self, snapshot: Optional[Dict[str, Any]] = None, **extra: Any) -> None:
        payload = {"Ok": True, "Error": None, "Snapshot": snapshot if snapshot is not None else self.bridge.snapshot()}
        payload.update(extra)
        self._send_json(200, payload)

    def _failure(self, code: int, message: str) -> None:
        self._send_json(code, {"Ok": False, "Error": message, "Snapshot": self.bridge.snapshot()})

    def log_message(self, format: str, *args) -> None:
        log_verbose("[BRIDGE HTTP] " + (format % args))

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path in {"/", "/ping"}:
            self._success(self.bridge.ping())
            return
        if parsed.path == "/status":
            self._success(self.bridge.snapshot())
            return
        self._failure(404, f"Unknown endpoint: {parsed.path}")

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        form = self._read_form()
        try:
            if parsed.path == "/task":
                self._success(self.bridge.submit_task(form.get("Task", "")))
                return
            if parsed.path == "/clarification":
                self._success(self.bridge.submit_clarification(form.get("Answer", "")))
                return
            if parsed.path == "/approve":
                self._success(self.bridge.approve_plan())
                return
            if parsed.path == "/cancel":
                self._success(self.bridge.cancel_preview())
                return
            if parsed.path == "/abort":
                self._success(self.bridge.abort_mission())
                return
            self._failure(404, f"Unknown endpoint: {parsed.path}")
        except RuntimeError as exc:
            self._failure(409, str(exc))
        except Exception as exc:
            self._failure(500, str(exc))


class BridgeHTTPServer(ThreadingHTTPServer):
    def __init__(self, server_address, RequestHandlerClass, bridge_service: MissionBridgeService):
        super().__init__(server_address, RequestHandlerClass)
        self.bridge_service = bridge_service


def parse_bridge_args(argv: Optional[List[str]] = None) -> tuple[argparse.Namespace, List[str]]:
    parser = argparse.ArgumentParser(description="Mission Planner bridge for Talk-to-Fly", add_help=True)
    parser.add_argument("--bridge-host", default="127.0.0.1", help="Host interface to bind")
    parser.add_argument("--bridge-port", type=int, default=8765, help="TCP port for the bridge server")
    parser.add_argument("--bridge-no-banner", action="store_true", help="Reserved for future use")
    return parser.parse_known_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    bridge_args, passthrough = parse_bridge_args(argv)
    args, drone, gps_logger = setup_environment(passthrough)
    service = MissionBridgeService(args=args, drone=drone, gps_logger=gps_logger)
    server = BridgeHTTPServer((bridge_args.bridge_host, bridge_args.bridge_port), BridgeRequestHandler, service)
    host, port = server.server_address
    log_status(f"[BRIDGE] Listening on http://{host}:{port}")
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        log_status("[BRIDGE] Keyboard interrupt received.")
    finally:
        server.shutdown()
        server.server_close()
        service.shutdown()
    return 0
