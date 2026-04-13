"""Console rendering helpers for mission and vehicle state."""

from __future__ import annotations

import os
import shutil
import textwrap
from typing import Any, Dict, Iterable, List, Optional


class ConsoleUI:
    """Lightweight ANSI terminal UI for Talk-to-Fly.

    Uses only the standard library so it works in the existing project
    environment without extra dependencies.
    """

    CYAN = "\033[1;36m"
    BLUE = "\033[1;34m"
    GREEN = "\033[1;32m"
    YELLOW = "\033[1;33m"
    RED = "\033[1;31m"
    MAGENTA = "\033[1;35m"
    DIM = "\033[2m"
    BOLD = "\033[1m"
    RESET = "\033[0m"

    def __init__(self, *, enabled: bool = True):
        self.enabled = bool(enabled)

    def width(self) -> int:
        try:
            return max(72, min(120, shutil.get_terminal_size((100, 30)).columns))
        except Exception:
            return 100

    def color(self, text: str, code: str) -> str:
        if not self.enabled:
            return text
        return f"{code}{text}{self.RESET}"

    def badge(self, label: str, value: str, *, tone: str = "info") -> str:
        palette = {
            "info": self.CYAN,
            "ok": self.GREEN,
            "warn": self.YELLOW,
            "bad": self.RED,
            "accent": self.MAGENTA,
            "muted": self.DIM,
        }
        code = palette.get(tone, self.CYAN)
        return self.color(f"[{label}: {value}]", code)

    def rule(self, title: Optional[str] = None, char: str = "─") -> str:
        width = self.width()
        if not title:
            return char * width
        label = f" {title.strip()} "
        side = max(2, (width - len(label)) // 2)
        text = (char * side) + label + (char * side)
        return text[:width]

    def box(self, title: str, lines: Iterable[str], *, tone: str = "info") -> str:
        width = self.width()
        palette = {
            "info": self.CYAN,
            "ok": self.GREEN,
            "warn": self.YELLOW,
            "bad": self.RED,
            "accent": self.MAGENTA,
            "muted": self.DIM,
        }
        code = palette.get(tone, self.CYAN)
        inner_width = max(30, width - 4)
        wrapped: List[str] = []
        for line in lines:
            text = "" if line is None else str(line)
            if not text:
                wrapped.append("")
                continue
            wrapped.extend(textwrap.wrap(text, width=inner_width) or [""])
        top = f"┌{'─' * (width - 2)}┐"
        bottom = f"└{'─' * (width - 2)}┘"
        title_text = f" {title.strip()} "
        if len(title_text) < width - 2:
            top = f"┌{title_text}{'─' * (width - len(title_text) - 2)}┐"
        body = [f"│ {line.ljust(inner_width)} │" for line in wrapped]
        rendered = "\n".join([top, *body, bottom])
        return self.color(rendered, code) if self.enabled else rendered

    def clear(self) -> None:
        if self.enabled:
            print("\033[2J\033[H", end="")

    def print_banner(self, args) -> None:
        sim = self.badge("SIM", "ON" if getattr(args, "simulation", False) else "OFF", tone="ok" if getattr(args, "simulation", False) else "warn")
        verify = self.badge("VERIFY", "ON" if getattr(args, "confirm", False) else "OFF", tone="ok" if getattr(args, "confirm", False) else "muted")
        voice = self.badge("VOICE", "ON" if getattr(args, "voice", False) else "OFF", tone="accent" if getattr(args, "voice", False) else "muted")
        verbose = self.badge("VERBOSE", "ON" if getattr(args, "verbose", False) else "OFF", tone="accent" if getattr(args, "verbose", False) else "muted")
        print(self.box(
            "Talk-to-Fly Control Console",
            [
                "Natural-language mission console for UAV planning, verification, and execution.",
                f"Connection: {getattr(args, 'connect', 'unknown')}",
                f"{sim}  {verify}  {voice}  {verbose}",
                "Type :help for commands. Type a plain-English task to plan and fly.",
            ],
            tone="accent",
        ))

    def print_help(self) -> None:
        print(self.box(
            "Controls",
            [
                ":help          Show this help panel",
                ":status        Show live UAV status snapshot",
                ":mission       Show current mission summary",
                ":plan          Show current plan and compiled steps",
                ":history       Show recent session commands and tasks",
                ":repeat        Re-run the last natural-language task",
                ":clear         Clear the terminal and redraw the header",
                ":land / :l     Land immediately",
                ":rtl / :r      Return to launch",
                "quit / exit    Exit the application",
            ],
            tone="info",
        ))

    def format_status_snapshot(self, snapshot: Dict[str, Any]) -> List[str]:
        position = snapshot.get("position") or {}
        velocity = snapshot.get("velocity") or {}
        return [
            f"Flight mode   : {snapshot.get('flight_mode')}",
            f"Armed         : {snapshot.get('armed')}",
            f"Simulation    : {snapshot.get('is_simulation')}",
            f"Battery       : {self._fmt(snapshot.get('battery_percent'), suffix='%')}",
            f"Heading       : {self._fmt(snapshot.get('heading_degrees'), suffix=' deg')}",
            f"Groundspeed   : {self._fmt(snapshot.get('groundspeed_mps'), suffix=' m/s')}",
            f"Position      : lat={self._fmt(position.get('lat'))}, lon={self._fmt(position.get('lon'))}, alt={self._fmt(position.get('alt_agl'), suffix=' m')}",
            f"Velocity      : vx={self._fmt(velocity.get('vx'))}, vy={self._fmt(velocity.get('vy'))}, vz={self._fmt(velocity.get('vz'))}",
            f"Command hist. : {snapshot.get('history_len')}",
        ]

    def print_status_panel(self, drone) -> None:
        snapshot = drone.get_status_dict()
        print(self.box("UAV Status", self.format_status_snapshot(snapshot), tone="info"))

    def print_dashboard(self, *, args, drone, active_mission=None, conversation=None, recent_events: Optional[List[str]] = None) -> None:
        lines: List[str] = []
        snapshot = drone.get_status_dict()
        lines.extend(self.format_status_snapshot(snapshot)[:6])
        if active_mission is None:
            lines.append("Mission       : no active mission")
        else:
            lines.append(f"Mission       : {active_mission.mission_id} ({active_mission.status})")
            lines.append(f"Plan source   : {active_mission.current_plan_source}")
            lines.append(f"Replans       : {active_mission.replan_count}/{active_mission.max_replans}")
            remaining = active_mission.current_plan_steps[active_mission.next_step_idx:]
            lines.append(f"Next step     : {remaining[0] if remaining else 'none'}")
        if conversation is not None:
            pending = getattr(conversation, "pending", None)
            lines.append(f"Clarification : {'pending' if pending is not None else 'none'}")
            lines.append(f"Turns in mem. : {len(getattr(conversation, 'turns', []))}")
        if recent_events:
            lines.append("")
            lines.append("Recent events:")
            for item in recent_events[-4:]:
                lines.append(f"- {item}")
        print(self.box("Mission Dashboard", lines, tone="accent"))

    def print_plan_preview(self, mission, compiled_steps) -> None:
        remaining = compiled_steps[mission.next_step_idx:] if getattr(mission, "next_step_idx", 0) else compiled_steps
        review = dict(getattr(mission, 'last_plan_review', {}) or {})
        lines = [
            f"Mission ID    : {mission.mission_id}",
            f"Objective     : {mission.objective}",
            f"Plan source   : {mission.current_plan_source}",
            f"Compiled steps: {len(compiled_steps)}",
            f"Replans used  : {mission.replan_count}/{mission.max_replans}",
            f"Local fixes   : {getattr(mission, 'local_recovery_count', 0)}",
        ]
        if review.get('summary'):
            lines.extend(["", f"Review        : {review.get('summary')}"])
        lines.extend([
            "",
            "DSL:",
            mission.current_plan.strip() or "<empty>",
            "",
            "Steps:",
        ])
        for idx, step in enumerate(compiled_steps, start=1):
            prefix = "->" if idx - 1 == mission.next_step_idx else "  "
            lines.append(f"{prefix} {idx:02d}. {step.rendered}")
        print(self.box("Plan Preview", lines, tone="ok"))

    def print_existing_plan(self, mission) -> None:
        lines = [
            f"Mission ID    : {mission.mission_id}",
            f"Objective     : {mission.objective}",
            f"Plan source   : {mission.current_plan_source}",
            f"Status        : {mission.status}",
            f"Replans       : {mission.replan_count}/{mission.max_replans}",
            "",
            "DSL:",
            mission.current_plan.strip() or "<empty>",
            "",
            "Steps:",
        ]
        if mission.current_plan_steps:
            for idx, step in enumerate(mission.current_plan_steps, start=1):
                prefix = "->" if idx - 1 == mission.next_step_idx else "  "
                lines.append(f"{prefix} {idx:02d}. {step}")
        else:
            lines.append("<no compiled steps>")
        print(self.box("Current Plan", lines, tone="ok"))

    def print_mission_event(self, mission, *, heading: str, tone: str = "info") -> None:
        remaining = mission.current_plan_steps[mission.next_step_idx:] if mission.current_plan_steps else []
        review = dict(getattr(mission, 'last_plan_review', {}) or {})
        lines = [
            f"Mission ID    : {mission.mission_id}",
            f"Status        : {mission.status}",
            f"Objective     : {mission.objective}",
            f"Plan source   : {mission.current_plan_source}",
            f"Completed     : {len(mission.completed_steps)} step(s)",
            f"Remaining     : {len(remaining)} step(s)",
            f"Replans       : {mission.replan_count}/{mission.max_replans}",
            f"Local fixes   : {getattr(mission, 'local_recovery_count', 0)}",
        ]
        if mission.last_failure:
            lines.append(f"Last failure  : {mission.last_failure.get('command')} | {mission.last_failure.get('reason')}")
        if review.get('summary'):
            lines.append(f"Plan review   : {review.get('summary')}")
        if mission.pending_question:
            lines.append(f"Question      : {mission.pending_question}")
        print(self.box(heading, lines, tone=tone))

    def print_history(self, entries: List[str]) -> None:
        print(self.box("Recent Session History", entries or ["No history yet."], tone="info"))

    def print_exit_summary(self, session_summary_lines: List[str]) -> None:
        print(self.box("Session Summary", session_summary_lines, tone="accent"))

    def prompt(self, *, active_mission=None, voice: bool = False) -> str:
        if active_mission is None or active_mission.status in {"completed", "failed", "cancelled"}:
            state = self.badge("READY", "NEW TASK", tone="ok")
        else:
            state = self.badge("MISSION", f"{active_mission.mission_id}:{active_mission.status}", tone="accent")
        modality = self.badge("INPUT", "VOICE" if voice else "TEXT", tone="info")
        return f"\n{state} {modality} Enter UAV task :> "

    def _fmt(self, value: Any, *, suffix: str = "") -> str:
        if value is None:
            return "n/a"
        if isinstance(value, float):
            return f"{value:.3f}{suffix}"
        return f"{value}{suffix}"


def install_readline(commands: Iterable[str], *, history_path: Optional[str] = None) -> None:
    try:
        import atexit
        import readline
    except Exception:
        return

    commands = sorted(set(str(cmd) for cmd in commands))

    def completer(text: str, state: int):
        options = [cmd for cmd in commands if cmd.startswith(text)]
        return options[state] if state < len(options) else None

    readline.set_completer(completer)
    try:
        readline.parse_and_bind("tab: complete")
    except Exception:
        pass

    if history_path:
        history_dir = os.path.dirname(history_path)
        if history_dir:
            os.makedirs(history_dir, exist_ok=True)
        try:
            readline.read_history_file(history_path)
        except FileNotFoundError:
            pass
        except Exception:
            return

        def save_history() -> None:
            try:
                readline.write_history_file(history_path)
            except Exception:
                pass

        atexit.register(save_history)
