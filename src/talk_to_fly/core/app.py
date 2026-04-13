"""Interactive application runner for Talk-to-Fly."""

from __future__ import annotations

from typing import Optional

from talk_to_fly.logging.logger import log_status, log_verbose, log_trace
from talk_to_fly.core.bootstrap import setup_environment
from talk_to_fly.core.conversation import ConversationMemory
from talk_to_fly.core.mission import build_mission_runner
from talk_to_fly.io.speech_input import prompt_user_for_task
from talk_to_fly.ui.console import ConsoleUI, install_readline
from talk_to_fly.ui.session import SessionRecorder


COMMANDS = [
    ":help",
    ":status",
    ":pos",
    ":mission",
    ":plan",
    ":history",
    ":repeat",
    ":settings",
    ":dashboard",
    ":ui",
    ":clear",
    ":land",
    ":l",
    ":rtl",
    ":r",
    "quit",
    "exit",
]


def handle_exit(drone, args):
    if drone.vehicle.armed and not args.simulation:
        choice = input("Drone is armed. Auto-land before exit? [Y/N]: ").lower()
        if choice == "y":
            log_status("[EXIT] Auto-landing...")
            drone.land()
            return True
        else:
            log_status("[CANCEL] Exit aborted.")
            return False
    return True


def _show_settings(ui: ConsoleUI, args) -> None:
    lines = [
        f"Connection     : {args.connect}",
        f"Simulation     : {args.simulation}",
        f"Confirm plans  : {args.confirm}",
        f"Verbose logs   : {args.verbose}",
        f"Voice input    : {args.voice}",
        f"Architecture   : {getattr(args, 'architecture', 'agentic')}",
        f"Max replans    : {args.max_replans}",
        f"Enhanced UI    : {not getattr(args, 'plain_ui', False)}",
    ]
    print(ui.box("Runtime Settings", lines, tone="info"))


def _show_mission(ui: ConsoleUI, active_mission) -> None:
    if active_mission is None:
        print(ui.box("Mission", ["No mission has been planned yet."], tone="muted"))
        return
    ui.print_mission_event(active_mission, heading="Current Mission", tone="accent")


def _show_plan(ui: ConsoleUI, active_mission) -> None:
    if active_mission is None or not getattr(active_mission, "current_plan", ""):
        print(ui.box("Current Plan", ["No plan available yet."], tone="muted"))
        return
    ui.print_existing_plan(active_mission)


def main_loop(drone, args, *, ui: ConsoleUI, session: SessionRecorder):
    mission_runner = build_mission_runner(drone, args, ui=ui, session=session)
    conversation = ConversationMemory()
    active_mission = None
    last_user_task: Optional[str] = None

    while True:
        task = prompt_user_for_task(
            voice=args.voice,
            stt=args.stt,
            prompt=ui.prompt(active_mission=active_mission, voice=args.voice),
        )
        print("")
        log_trace(f"[TASK] :>{task}")

        if not task:
            log_verbose("[WARN] Empty task ignored.")
            continue

        if task in COMMANDS or task.lower() in ("quit", "exit"):
            session.record_command(task)

        if task == ":help":
            ui.print_help()
            continue

        if task in (":dashboard", ":ui"):
            ui.print_dashboard(
                args=args,
                drone=drone,
                active_mission=active_mission,
                conversation=conversation,
                recent_events=session.recent_event_lines(),
            )
            continue

        if task == ":clear":
            ui.clear()
            ui.print_banner(args)
            ui.print_dashboard(
                args=args,
                drone=drone,
                active_mission=active_mission,
                conversation=conversation,
                recent_events=session.recent_event_lines(),
            )
            continue

        if task in (":pos", ":status"):
            ui.print_status_panel(drone)
            continue

        if task == ":mission":
            _show_mission(ui, active_mission)
            continue

        if task == ":plan":
            _show_plan(ui, active_mission)
            continue

        if task == ":history":
            ui.print_history(session.recent_event_lines(limit=12))
            continue

        if task == ":settings":
            _show_settings(ui, args)
            continue

        if task == ":repeat":
            if not last_user_task:
                print(ui.box("Repeat Task", ["No previous natural-language task is available yet."], tone="warn"))
                continue
            task = last_user_task
            print(ui.box("Repeat Task", [f"Re-running: {task}"], tone="info"))

        if task in (":land", ":l"):
            drone.land()
            continue

        if task in (":rtl", ":r"):
            drone.rtl()
            continue

        if task.lower() in ("quit", "exit"):
            if handle_exit(drone, args):
                break
            else:
                continue

        last_user_task = task
        session.record_task(task)
        conversation.add_user(task)

        if conversation.has_pending_clarification():
            clarification_request, pending_mission = conversation.build_clarification_request(task)
            active_mission = mission_runner.run(
                clarification_request,
                mission=pending_mission,
                plan_source="clarification",
                conversation=conversation,
            )
        else:
            active_mission = mission_runner.run(task, conversation=conversation)

        if active_mission.status == "failed" and active_mission.last_failure:
            log_status(f"[MISSION] Failed: {active_mission.last_failure.get('reason')}")
        elif active_mission.status == "cancelled":
            log_status("[MISSION] Cancelled.")

    return active_mission


def main(argv=None) -> int:
    args, drone, gps_logger = setup_environment(argv)
    ui = ConsoleUI(enabled=not getattr(args, "plain_ui", False))
    session = SessionRecorder()
    install_readline(COMMANDS, history_path="sessions/.talk_to_fly_readline_history")
    ui.clear()
    ui.print_banner(args)
    ui.print_dashboard(args=args, drone=drone, active_mission=None, conversation=None, recent_events=None)

    try:
        main_loop(drone, args, ui=ui, session=session)
    except KeyboardInterrupt:
        log_status("[ABORT] Ctrl-C detected. Landing...")
        try:
            drone.land()
        except Exception:
            pass
    finally:
        summary_lines = session.write_summary()
        ui.print_exit_summary(summary_lines)
        gps_logger.stop()
        drone.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
