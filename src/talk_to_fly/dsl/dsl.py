"""Utilities for compiling and executing the Talk-to-Fly flight-plan DSL."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from talk_to_fly.logging.logger import log_verbose, log_status
from talk_to_fly.skillset import HighLevelSkillItem


@dataclass
class CompiledStep:
    name: str
    arg_string: Optional[str]
    args_list: List[Any]
    rendered: str
    skill: Any


@dataclass
class StepwiseExecutionReport:
    success: bool
    compiled_steps: List[CompiledStep]
    outcomes: List[Dict[str, Any]]
    failed_step: Optional[CompiledStep] = None
    failed_outcome: Optional[Any] = None


def _resolve_args(skill, name: str, arg_string: Optional[str], vars: Dict[str, Any]) -> List[Any]:
    args_list: List[Any] = []

    if name in ("l", "log"):
        return [arg_string]

    if arg_string and arg_string.strip():
        raw_args = [a.strip() for a in arg_string.split(",")]
        skill_args = skill.get_argument()
        if len(raw_args) != len(skill_args):
            raise ValueError(
                f"Incorrect number of arguments for {name}. Expected {len(skill_args)}, got {len(raw_args)}"
            )
        for raw, spec in zip(raw_args, skill_args):
            if raw.startswith("_") and raw in vars:
                args_list.append(vars[raw])
                continue
            if spec.arg_type == str:
                args_list.append(raw.strip("'\""))
            else:
                args_list.append(spec.arg_type(raw))
    return args_list


def compile_dsl_steps(dsl_code, drone, vars=None):
    """Compile the flight-plan DSL into a flat list of executable low-level steps.

    High-level skills are expanded recursively before execution so the mission loop
    can monitor one concrete action at a time.
    """
    if vars is None:
        vars = {}

    skillset = drone.skills
    compiled_steps: List[CompiledStep] = []

    def add_compiled_step(name: str, arg_string: Optional[str]):
        skill = skillset.get_skill(name)
        if skill is None:
            raise ValueError(f"Unknown command: {name}({arg_string})")

        args_list = _resolve_args(skill, name, arg_string, vars)

        if isinstance(skill, HighLevelSkillItem):
            expanded = skill.execute(args_list)
            _compile(expanded)
            return

        rendered = f"{name}({arg_string});" if arg_string is not None else f"{name};"
        compiled_steps.append(
            CompiledStep(
                name=name,
                arg_string=arg_string,
                args_list=args_list,
                rendered=rendered,
                skill=skill,
            )
        )

    def extract_l_command(code, start_idx):
        depth = 0
        for i in range(start_idx, len(code)):
            if code[i] == "(":
                depth += 1
            elif code[i] == ")":
                depth -= 1
                if depth == 0:
                    return code[start_idx + 2 : i].strip(), i + 1
        return None, start_idx

    def extract_loop(code, start_idx):
        i = start_idx
        count_str = ""
        while i < len(code) and code[i].isdigit():
            count_str += code[i]
            i += 1
        if not count_str or i >= len(code) or code[i] != "{":
            return None, None, start_idx
        loop_count = int(count_str)
        i += 1
        depth = 1
        body_start = i
        while i < len(code):
            if code[i] == "{":
                depth += 1
            elif code[i] == "}":
                depth -= 1
                if depth == 0:
                    body = code[body_start:i]
                    return loop_count, body, i + 1
            i += 1
        return None, None, start_idx

    def _compile(code):
        idx = 0
        while idx < len(code):
            while idx < len(code) and code[idx].isspace():
                idx += 1
            if idx >= len(code):
                break

            if code[idx : idx + 2] == "l(":
                content, next_idx = extract_l_command(code, idx)
                if content is None:
                    raise ValueError(f"Unmatched parentheses in l() at index {idx}")
                add_compiled_step("l", content)
                idx = next_idx
                continue

            if code[idx].isdigit():
                loop_count, body, next_idx = extract_loop(code, idx)
                if loop_count is not None:
                    for _ in range(loop_count):
                        _compile(body)
                    idx = next_idx
                    continue

            semicolon_idx = code.find(";", idx)
            if semicolon_idx == -1:
                semicolon_idx = len(code)
            cmd = code[idx:semicolon_idx].strip()
            idx = semicolon_idx + 1
            if not cmd:
                continue

            if cmd.startswith("?") and "{" in cmd and cmd.endswith("}"):
                cond_expr = cmd[1 : cmd.find("{")].strip()
                cond_body = cmd[cmd.find("{") + 1 : -1]
                for key, val in vars.items():
                    cond_expr = cond_expr.replace(key, str(val))
                if eval(cond_expr):
                    _compile(cond_body)
                continue

            if "=" in cmd:
                parts = cmd.split("=", 1)
                if len(parts) == 2:
                    var_name, value = parts[0].strip(), parts[1].strip()
                    try:
                        vars[var_name] = float(value)
                    except ValueError:
                        vars[var_name] = value.strip("'\"")
                else:
                    raise ValueError(f"Invalid assignment: {cmd}")
                continue

            if cmd.startswith("->"):
                continue

            if "(" in cmd and cmd.endswith(")"):
                name = cmd[: cmd.find("(")]
                arg = cmd[cmd.find("(") + 1 : -1]
            else:
                name = cmd
                arg = None
            add_compiled_step(name, arg)

    _compile(dsl_code)
    return compiled_steps


def execute_dsl_stepwise(
    dsl_code,
    drone,
    mission=None,
    monitor=None,
    vars=None,
    *,
    dry_run: bool = False,
    plan_source: str = "initial",
):
    if monitor is None:
        from talk_to_fly.core.monitor import MissionMonitor

        monitor = MissionMonitor()

    compiled_steps = compile_dsl_steps(dsl_code, drone, vars=vars)
    if mission is not None:
        mission.set_plan(dsl_code, compiled_steps, source=plan_source)

    if dry_run:
        return StepwiseExecutionReport(success=True, compiled_steps=compiled_steps, outcomes=[])

    outcomes: List[Dict[str, Any]] = []
    total = len(compiled_steps)

    for idx, step in enumerate(compiled_steps, start=1):
        log_status(f"[MISSION] Step {idx}/{total}: {step.rendered}")
        before_state = monitor.capture_state(drone)

        raw_result = None
        exception = None
        try:
            raw_result = step.skill.execute(step.args_list)
        except Exception as exc:
            exception = exc

        after_state = monitor.capture_state(drone)
        outcome = monitor.evaluate(
            step,
            raw_result=raw_result,
            before_state=before_state,
            after_state=after_state,
            exception=exception,
        )

        if not outcome.success:
            recovered = monitor.attempt_local_recovery(drone, step, outcome)
            if recovered is not None:
                outcome = recovered
                if outcome.success:
                    log_status(f"[MISSION] Local recovery succeeded: {step.rendered} | {outcome.reason}")

        if mission is not None:
            mission.record_step(step, outcome)

        outcomes.append({"step": step, "outcome": outcome})
        if not outcome.success:
            log_status(f"[MISSION] Step failed: {step.rendered} | {outcome.reason}")
            return StepwiseExecutionReport(
                success=False,
                compiled_steps=compiled_steps,
                outcomes=outcomes,
                failed_step=step,
                failed_outcome=outcome,
            )

    log_status("[DSL] Commands executed")
    return StepwiseExecutionReport(success=True, compiled_steps=compiled_steps, outcomes=outcomes)


def run_dsl(dsl_code, drone, vars=None):
    """Backward-compatible one-shot executor.

    Existing evaluation code still calls this function. Internally it now uses the
    same DSL compiler used by the mission loop, but without mission
    tracking or replanning.
    """
    report = execute_dsl_stepwise(
        dsl_code,
        drone,
        mission=None,
        monitor=None,
        vars=vars,
        dry_run=False,
    )
    if not report.success:
        failed = report.failed_step.rendered if report.failed_step else "<unknown>"
        reason = report.failed_outcome.reason if report.failed_outcome else "unknown error"
        log_verbose(f"[DSL] Execution stopped at {failed}: {reason}")
