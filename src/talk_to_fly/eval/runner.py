"""Evaluation runner for executing suites and recording results."""

from __future__ import annotations
import csv
import json
import math
import random
import threading
import time
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from talk_to_fly.eval.suite import EpisodeSpec, InitialStateSpec, load_suite
from talk_to_fly.eval.telemetry import TelemetryRecorder
from talk_to_fly.eval.metrics import MovementDetector
from talk_to_fly.eval.objective import evaluate_mission_objective
from talk_to_fly.eval.llm_cache import LLMCache, compute_key
from talk_to_fly.llm.controller import plan_dsl
from talk_to_fly.dsl.dsl import execute_dsl_stepwise
from talk_to_fly.uav.mavlink_wrapper import MavlinkWrapper
from talk_to_fly.logging.logger import log_status, set_verbose
from talk_to_fly.core.mission import Mission, _terminal_log_question
from talk_to_fly.core.monitor import MissionMonitor, DirectExecutionMonitor

R_EARTH_M = 6371000.0


def _now_stamp() -> str:
    return time.strftime("%Y-%m-%d_%H%M%S", time.localtime())


def _mkdir_run(out_root: Path, suite_name: str, name_override: Optional[str]) -> Path:
    out_root.mkdir(parents=True, exist_ok=True)
    suffix = name_override or suite_name
    run_dir = out_root / f"{_now_stamp()}_{suffix}"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "episodes").mkdir(exist_ok=True)
    (run_dir / "cache").mkdir(exist_ok=True)
    return run_dir


def _safe_latlon(vehicle) -> Tuple[Optional[float], Optional[float]]:
    loc = getattr(vehicle, "location", None)
    grf = getattr(loc, "global_relative_frame", None) if loc else None
    lat = getattr(grf, "lat", None) if grf else None
    lon = getattr(grf, "lon", None) if grf else None
    try:
        lat = float(lat) if lat is not None else None
        lon = float(lon) if lon is not None else None
    except Exception:
        lat, lon = None, None
    return lat, lon


def _safe_alt(vehicle) -> Optional[float]:
    loc = getattr(vehicle, "location", None)
    grf = getattr(loc, "global_relative_frame", None) if loc else None
    alt = getattr(grf, "alt", None) if grf else None
    try:
        return float(alt) if alt is not None else None
    except Exception:
        return None


def _safe_heading(vehicle) -> Optional[float]:
    hdg = getattr(vehicle, "heading", None)
    try:
        return float(hdg) if hdg is not None else None
    except Exception:
        return None


def _validate_dsl_unknowns(drone: MavlinkWrapper, dsl: str) -> List[str]:
    import re
    names = re.findall(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\(", dsl)
    unknown = []
    for n in names:
        if n in ("l", "log"):
            continue
        if drone.skills.get_skill(n) is None:
            unknown.append(n)
    return sorted(set(unknown))


def _wait_terminal(drone: MavlinkWrapper, timeout_s: float = 20.0) -> bool:
    """Return True if disarmed OR (low altitude and near-ground) within timeout."""
    t0 = time.time()
    low_alt_hold = 0.0
    poll = 0.2
    while time.time() - t0 < timeout_s:
        armed = getattr(drone.vehicle, "armed", None)
        try:
            armed = bool(armed) if armed is not None else None
        except Exception:
            armed = None
        alt = _safe_alt(drone.vehicle)
        if armed is False:
            return True
        if alt is not None and alt < 0.3:
            low_alt_hold += poll
            if low_alt_hold >= 2.0:
                return True
        else:
            low_alt_hold = 0.0
        time.sleep(poll)
    return False


def _wrap360(deg: float) -> float:
    return deg % 360.0


def _angdiff_deg(target: float, current: float) -> float:
    return (target - current + 180.0) % 360.0 - 180.0


def _latlon_to_ne_m(lat0: float, lon0: float, lat: float, lon: float) -> Tuple[float, float]:
    dlat = math.radians(lat - lat0)
    dlon = math.radians(lon - lon0)
    north = dlat * R_EARTH_M
    east = dlon * R_EARTH_M * math.cos(math.radians(lat0))
    return north, east


def _ne_to_latlon(lat0: float, lon0: float, north_m: float, east_m: float) -> Tuple[float, float]:
    lat = lat0 + math.degrees(north_m / R_EARTH_M)
    lon = lon0 + math.degrees(east_m / (R_EARTH_M * math.cos(math.radians(lat0))))
    return lat, lon


def _wait_for_valid_origin(vehicle, timeout_s: float = 15.0) -> Optional[Tuple[float, float]]:
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        latlon = _safe_latlon(vehicle)
        if latlon[0] is not None and latlon[1] is not None:
            return latlon
        time.sleep(0.2)
    return None


def _copy_history(history: List[Any]) -> List[Any]:
    return deepcopy(history)


def _normalise_visible_history(seed_items: List[Any]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for item in seed_items:
        if isinstance(item, dict):
            out.append(deepcopy(item))
        elif isinstance(item, str):
            out.append({"source": "seed", "text": item})
        else:
            out.append({"source": "seed", "value": repr(item)})
    return out


def _build_episode_visible_history(ep: EpisodeSpec, carried_history: List[Any]) -> List[Any]:
    base: List[Any] = [] if ep.reset_visible_history else _copy_history(carried_history)
    if ep.visible_history:
        base.extend(_normalise_visible_history(ep.visible_history))
    return base


def _run_without_history_side_effects(drone: MavlinkWrapper, fn):
    saved = _copy_history(drone.hist)
    try:
        return fn()
    finally:
        drone.hist = saved


def _ensure_hidden_landed(drone: MavlinkWrapper) -> bool:
    def _land_if_needed() -> bool:
        armed = bool(getattr(drone.vehicle, "armed", False))
        alt = _safe_alt(drone.vehicle)
        if not armed and (alt is None or alt < 0.3):
            return True
        return bool(drone.land())
    ok = _run_without_history_side_effects(drone, _land_if_needed)
    if ok:
        return bool(_wait_terminal(drone, timeout_s=25.0))
    return False


def _maybe_rotate_hidden(drone: MavlinkWrapper, heading_deg: Optional[float]) -> bool:
    if heading_deg is None:
        return True

    def _rotate() -> bool:
        current = _safe_heading(drone.vehicle)
        if current is None:
            return False
        delta = _angdiff_deg(_wrap360(float(heading_deg)), _wrap360(current))
        if abs(delta) <= 3.0:
            return True
        return bool(drone.rotate(delta, relative=True))

    return bool(_run_without_history_side_effects(drone, _rotate))


def _goto_target_from_origin(
    drone: MavlinkWrapper,
    run_origin_latlon: Tuple[float, float],
    target_ne_m: Optional[Tuple[float, float]],
) -> bool:
    if target_ne_m is None:
        return True
    origin_lat, origin_lon = run_origin_latlon
    cur_lat, cur_lon = _safe_latlon(drone.vehicle)
    if cur_lat is None or cur_lon is None:
        return False
    tgt_lat, tgt_lon = _ne_to_latlon(origin_lat, origin_lon, float(target_ne_m[0]), float(target_ne_m[1]))
    cur_n, cur_e = _latlon_to_ne_m(cur_lat, cur_lon, tgt_lat, tgt_lon)

    def _goto() -> bool:
        return bool(drone.goto(cur_n, cur_e, 0.0))

    return bool(_run_without_history_side_effects(drone, _goto))


def _adjust_altitude_hidden(drone: MavlinkWrapper, target_alt_m: Optional[float]) -> bool:
    if target_alt_m is None:
        return True
    current_alt = _safe_alt(drone.vehicle)
    if current_alt is None:
        return False
    delta = float(target_alt_m) - float(current_alt)
    if abs(delta) <= 0.25:
        return True

    def _do() -> bool:
        if delta > 0.0:
            return bool(drone.ascend(delta))
        return bool(drone.descend(abs(delta), min_altitude=max(0.5, target_alt_m - 0.3)))

    return bool(_run_without_history_side_effects(drone, _do))


def _apply_initial_state(
    *,
    drone: MavlinkWrapper,
    initial_state: Optional[InitialStateSpec],
    run_origin_latlon: Optional[Tuple[float, float]],
    desired_visible_history: List[Any],
) -> Dict[str, Any]:
    drone.hist = _copy_history(desired_visible_history)
    if initial_state is None:
        return {
            "applied": False,
            "mode": None,
            "position_ne_m": None,
            "altitude_m": _safe_alt(drone.vehicle),
            "heading_deg": _safe_heading(drone.vehicle),
        }

    if not _ensure_hidden_landed(drone):
        raise RuntimeError("Failed to establish a landed baseline before episode setup.")

    mode = initial_state.mode
    target_alt = float(initial_state.altitude_m) if initial_state.altitude_m is not None else None
    target_heading = float(initial_state.heading_deg) if initial_state.heading_deg is not None else None
    target_ne = initial_state.position_ne_m
    transit_alt = initial_state.transit_alt_m

    # Only require airborne hidden setup if we genuinely need to be airborne
    # or need to reposition. Do NOT hidden-takeoff just to enforce heading
    # for a ground-start episode.
    needs_airborne_setup = (
        mode == "airborne"
        or target_ne is not None
    )

    if needs_airborne_setup:
        if transit_alt is None:
            if target_alt is not None:
                transit_alt = max(2.0, float(target_alt))
            else:
                transit_alt = 2.0

        def _takeoff() -> bool:
            return bool(drone.takeoff(float(transit_alt)))

        if not _run_without_history_side_effects(drone, _takeoff):
            raise RuntimeError("Failed hidden takeoff during episode setup.")

        if target_ne is not None:
            if run_origin_latlon is None:
                raise RuntimeError("Episode initial_state.position_ne_m requires a valid run origin / GPS fix.")
            if not _goto_target_from_origin(drone, run_origin_latlon, target_ne):
                raise RuntimeError("Failed hidden reposition during episode setup.")

        if mode == "airborne":
            if target_alt is not None and not _adjust_altitude_hidden(drone, target_alt):
                raise RuntimeError("Failed hidden altitude adjustment during airborne setup.")
            if not _maybe_rotate_hidden(drone, target_heading):
                raise RuntimeError("Failed hidden heading adjustment during airborne setup.")
        else:
            # Ground starts: do not enforce heading.
            # If we repositioned in the air, just land back down.
            if not _ensure_hidden_landed(drone):
                raise RuntimeError("Failed hidden landing during ground-state setup.")

    # Restore only the planner-visible history.
    drone.hist = _copy_history(desired_visible_history)

    settle_s = max(0.0, float(initial_state.settle_s))
    if settle_s > 0:
        time.sleep(settle_s)

    return {
        "applied": True,
        "mode": mode,
        "position_ne_m": list(target_ne) if target_ne is not None else None,
        "target_altitude_m": target_alt,
        "target_heading_deg": target_heading,
        "transit_alt_m": transit_alt,
        "actual_altitude_m": _safe_alt(drone.vehicle),
        "actual_heading_deg": _safe_heading(drone.vehicle),
        "actual_latlon": list(_safe_latlon(drone.vehicle)),
    }

def _cleanup_after_episode(drone: MavlinkWrapper, cleanup_mode: str, desired_visible_history: List[Any]) -> bool:
    drone.hist = _copy_history(desired_visible_history)
    if cleanup_mode == "none":
        return True
    return bool(_ensure_hidden_landed(drone))

def _ablation_mode(args) -> str:
    return str(getattr(args, "ablation", "full") or "full").strip().lower()


def _planner_context_enabled(args) -> bool:
    return _ablation_mode(args) not in ("stateless", "one_shot")


def _replanning_enabled(args) -> bool:
    return _ablation_mode(args) not in ("no_replanning", "open_loop", "one_shot")


def _open_loop_enabled(args) -> bool:
    return _ablation_mode(args) == "open_loop"


def _one_shot_enabled(args) -> bool:
    return _ablation_mode(args) == "one_shot"


def _compute_llm_key_for_plan(*, task_text: str, suite_name: str, episode_id: str, category: str, ablation: str, plan_source: str) -> str:
    return compute_key(
        task_text,
        category=category,
        suite=f"{suite_name}|{ablation}|{plan_source}",
        episode_id=f"{episode_id}|{plan_source}|{ablation}",
    )


def _plan_with_cache(
    *,
    args,
    llm_cache: LLMCache,
    suite_name: str,
    ep: EpisodeSpec,
    task_text: str,
    drone: MavlinkWrapper,
    mission: Mission,
    plan_source: str,
) -> Tuple[str, Dict[str, Optional[float]]]:
    use_context = _planner_context_enabled(args)
    llm_key = _compute_llm_key_for_plan(
        task_text=task_text,
        suite_name=suite_name,
        episode_id=ep.id,
        category=ep.category,
        ablation=_ablation_mode(args),
        plan_source=plan_source,
    )

    if args.llm == "replay":
        cached = llm_cache.get(llm_key)
        if cached is None:
            raise RuntimeError(f"LLM cache miss for episode '{ep.id}' plan_source='{plan_source}' (replay mode)")
        return cached.dsl, {
            "ttft_ms": None,
            "cached_ttft_ms": cached.meta.get("ttft_ms"),
            "plan_total_ms": 0.0,
            "cached_plan_total_ms": cached.meta.get("plan_total_ms"),
        }

    t_submit = time.time()
    dsl, timings = plan_dsl(
        task_text,
        drone,
        stream=True,
        show_spinner=False,
        print_plan=False,
        submitted_at_s=t_submit,
        execution_history=(mission.planner_history() if use_context else []),
        current_status=(mission.last_state if use_context else {}),
        mission_context=(mission.mission_context() if use_context else None),
        planning_mode=(plan_source if use_context else None),
        conversation_history=[],
    )

    if args.llm == "record":
        llm_cache.put(llm_key, task_text, dsl, {
            "ttft_ms": timings.ttft_ms,
            "plan_total_ms": timings.total_ms,
            "suite": suite_name,
            "episode_id": ep.id,
            "category": ep.category,
            "plan_source": plan_source,
            "ablation": _ablation_mode(args),
        })

    return dsl, {
        "ttft_ms": timings.ttft_ms,
        "cached_ttft_ms": None,
        "plan_total_ms": timings.total_ms,
        "cached_plan_total_ms": None,
    }


def _execute_agentic_episode(
    *,
    args,
    drone: MavlinkWrapper,
    suite_name: str,
    ep: EpisodeSpec,
    llm_cache: LLMCache,
) -> Dict[str, Any]:
    mission = Mission.create(ep.command, max_replans=(0 if _one_shot_enabled(args) else int(getattr(args, "max_replans", 2))))
    monitor = DirectExecutionMonitor() if _one_shot_enabled(args) else MissionMonitor()
    mission.note_state(monitor.capture_state(drone))

    current_request = ep.command
    plan_source = "initial"

    generated_plans: List[Dict[str, Any]] = []
    unknown_skills: List[str] = []
    plan_valid = True
    ttft_ms: Optional[float] = None
    cached_ttft_ms: Optional[float] = None
    plan_total_ms: float = 0.0
    cached_plan_total_ms: float = 0.0

    while True:
        dsl, timing = _plan_with_cache(
            args=args,
            llm_cache=llm_cache,
            suite_name=suite_name,
            ep=ep,
            task_text=current_request,
            drone=drone,
            mission=mission,
            plan_source=plan_source,
        )

        if ttft_ms is None and timing.get("ttft_ms") is not None:
            ttft_ms = float(timing["ttft_ms"])
        if cached_ttft_ms is None and timing.get("cached_ttft_ms") is not None:
            cached_ttft_ms = float(timing["cached_ttft_ms"])
        if timing.get("plan_total_ms") is not None:
            plan_total_ms += float(timing["plan_total_ms"])
        if timing.get("cached_plan_total_ms") is not None:
            cached_plan_total_ms += float(timing["cached_plan_total_ms"])

        current_unknowns = _validate_dsl_unknowns(drone, dsl)
        unknown_skills.extend(current_unknowns)
        if not dsl.strip() or current_unknowns:
            plan_valid = False

        generated_plans.append({
            "source": plan_source,
            "request": current_request,
            "dsl": dsl,
            "unknown_skills": current_unknowns,
        })

        if not dsl.strip():
            mission.status = "failed"
            mission.last_failure = {
                "command": "<planning>",
                "reason": "Planner returned no DSL plan.",
                "after_state": mission.last_state,
            }
            return {
                "mission": mission,
                "dsl": dsl,
                "generated_plans": generated_plans,
                "ttft_ms": ttft_ms,
                "cached_ttft_ms": cached_ttft_ms,
                "plan_total_ms": plan_total_ms,
                "cached_plan_total_ms": cached_plan_total_ms,
                "plan_valid": False,
                "unknown_skills": sorted(set(unknown_skills)),
                "failure_stage": "planning",
                "error": "Planner returned no DSL plan.",
            }

        if current_unknowns:
            mission.status = "failed"
            mission.last_failure = {
                "command": "<planning>",
                "reason": f"Unknown skills in DSL plan: {sorted(set(current_unknowns))}",
                "after_state": mission.last_state,
            }
            return {
                "mission": mission,
                "dsl": dsl,
                "generated_plans": generated_plans,
                "ttft_ms": ttft_ms,
                "cached_ttft_ms": cached_ttft_ms,
                "plan_total_ms": plan_total_ms,
                "cached_plan_total_ms": cached_plan_total_ms,
                "plan_valid": False,
                "unknown_skills": sorted(set(unknown_skills)),
                "failure_stage": "planning",
                "error": mission.last_failure["reason"],
            }

        if _open_loop_enabled(args):
            execute_dsl_stepwise(
                dsl,
                drone,
                mission=mission,
                monitor=monitor,
                dry_run=True,
                plan_source=plan_source,
            )
            report = execute_dsl_stepwise(
                dsl,
                drone,
                mission=None,
                monitor=monitor,
                dry_run=False,
                plan_source=plan_source,
            )
            mission.note_state(monitor.capture_state(drone))
            if report.success:
                mission.status = "completed"
                failure_stage = ""
                err = None
            else:
                mission.status = "failed"
                failure_stage = "execution"
                err = report.failed_outcome.reason if report.failed_outcome is not None else "Open-loop execution failed."
                mission.last_failure = {
                    "command": report.failed_step.rendered if report.failed_step is not None else "<execution>",
                    "reason": err,
                    "after_state": mission.last_state,
                }
            return {
                "mission": mission,
                "dsl": dsl,
                "generated_plans": generated_plans,
                "ttft_ms": ttft_ms,
                "cached_ttft_ms": cached_ttft_ms,
                "plan_total_ms": plan_total_ms,
                "cached_plan_total_ms": cached_plan_total_ms if cached_plan_total_ms > 0 else None,
                "plan_valid": plan_valid,
                "unknown_skills": sorted(set(unknown_skills)),
                "failure_stage": failure_stage,
                "error": err,
            }

        report = execute_dsl_stepwise(
            dsl,
            drone,
            mission=mission,
            monitor=monitor,
            dry_run=False,
            plan_source=plan_source,
        )

        if report.success:
            question = _terminal_log_question(report.compiled_steps)
            if question is not None:
                mission.status = "failed"
                mission.pending_question = question
                mission.last_failure = {
                    "command": "<clarification>",
                    "reason": f"Planner requested clarification: {question}",
                    "after_state": mission.last_state,
                }
                return {
                    "mission": mission,
                    "dsl": dsl,
                    "generated_plans": generated_plans,
                    "ttft_ms": ttft_ms,
                    "cached_ttft_ms": cached_ttft_ms,
                    "plan_total_ms": plan_total_ms,
                    "cached_plan_total_ms": cached_plan_total_ms if cached_plan_total_ms > 0 else None,
                    "plan_valid": plan_valid,
                    "unknown_skills": sorted(set(unknown_skills)),
                    "failure_stage": "clarification",
                    "error": mission.last_failure["reason"],
                }

            mission.status = "completed"
            return {
                "mission": mission,
                "dsl": dsl,
                "generated_plans": generated_plans,
                "ttft_ms": ttft_ms,
                "cached_ttft_ms": cached_ttft_ms,
                "plan_total_ms": plan_total_ms,
                "cached_plan_total_ms": cached_plan_total_ms if cached_plan_total_ms > 0 else None,
                "plan_valid": plan_valid,
                "unknown_skills": sorted(set(unknown_skills)),
                "failure_stage": "",
                "error": None,
            }

        if not _replanning_enabled(args) or not mission.can_replan():
            mission.status = "failed"
            err = mission.last_failure.get("reason") if mission.last_failure else "Execution failed."
            return {
                "mission": mission,
                "dsl": dsl,
                "generated_plans": generated_plans,
                "ttft_ms": ttft_ms,
                "cached_ttft_ms": cached_ttft_ms,
                "plan_total_ms": plan_total_ms,
                "cached_plan_total_ms": cached_plan_total_ms if cached_plan_total_ms > 0 else None,
                "plan_valid": plan_valid,
                "unknown_skills": sorted(set(unknown_skills)),
                "failure_stage": "execution",
                "error": err,
            }

        mission.replan_count += 1
        plan_source = f"replan_{mission.replan_count}"
        current_request = mission.build_replan_request()
        mission.status = "replanning"


def run_suite(args) -> None:
    set_verbose(bool(args.verbose))
    suite = load_suite(args.suite)
    run_dir = _mkdir_run(Path(args.out_dir), suite.name, args.name)
    cache_path = Path(args.cache) if args.cache else (run_dir / "cache" / "llm_cache.jsonl")
    llm_cache = LLMCache(cache_path)

    results_csv = run_dir / "results.csv"
    results_jsonl = run_dir / "results.jsonl"

    with results_csv.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([
            "overall_idx", "overall_total",
            "run_idx", "run_total",
            "episode_idx", "episode_total",
            "episode_id", "category",
            "success", "execution_success",
            "objective_checked", "objective_met",
            "failure_stage",
            "ttft_ms", "cached_ttft_ms",
            "ttfm_ms",
            "plan_total_ms", "cached_plan_total_ms",
            "episode_time_s",
            "plan_valid", "unknown_skills",
            "cleanup_mode", "history_len", "setup_applied",
        ])

    drone = MavlinkWrapper(args.connect, simulation=bool(args.simulation))
    log_status(f"[EVAL] Connected to vehicle on {args.connect} (simulation={args.simulation})")

    run_origin = _wait_for_valid_origin(drone.vehicle, timeout_s=15.0)

    (run_dir / "config.json").write_text(json.dumps({
        "suite": suite.name,
        "suite_path": str(Path(args.suite).resolve()),
        "connect": args.connect,
        "simulation": bool(args.simulation),
        "runs": int(args.runs),
        "shuffle": bool(args.shuffle),
        "category": args.category,
        "llm_mode": args.llm,
        "cache": str(cache_path),
        "ablation": _ablation_mode(args),
        "max_replans": int(getattr(args, "max_replans", 2)),
        "run_origin_latlon": list(run_origin) if run_origin else None,
        "movement": {
            "speed_thresh_mps": args.movement_speed_mps,
            "disp_thresh_m": args.movement_disp_m,
            "alt_thresh_m": args.movement_alt_m,
            "yaw_thresh_deg": args.movement_yaw_deg,
            "hold_s": args.movement_hold_s,
        },
        "overrides": {
            "timeout_s": args.timeout_s,
            "settle_s": args.settle_s,
        }
    }, indent=2), encoding="utf-8")

    history_state: Dict[str, Any] = {
        "visible_history": [],
        "run_origin_latlon": run_origin,
    }

    try:
        episodes = suite.episodes
        if args.category != "all":
            episodes = [e for e in episodes if e.category == args.category]
        if not episodes:
            raise ValueError(f"No episodes match category='{args.category}'")

        run_total = int(args.runs)
        for run_idx0 in range(run_total):
            eps = list(episodes)
            if args.shuffle:
                random.shuffle(eps)

            episode_total = len(eps)
            overall_total = run_total * episode_total
            run_idx = run_idx0 + 1

            for episode_idx, ep in enumerate(eps, start=1):
                overall_idx = (run_idx - 1) * episode_total + episode_idx
                _run_episode(
                    args=args,
                    drone=drone,
                    suite_name=suite.name,
                    run_dir=run_dir,
                    overall_idx=overall_idx,
                    overall_total=overall_total,
                    run_idx=run_idx,
                    run_total=run_total,
                    episode_idx=episode_idx,
                    episode_total=episode_total,
                    ep=ep,
                    llm_cache=llm_cache,
                    results_csv=results_csv,
                    results_jsonl=results_jsonl,
                    history_state=history_state,
                )
    finally:
        try:
            drone.close()
        except Exception:
            pass


def _run_episode(
    *,
    args,
    drone: MavlinkWrapper,
    suite_name: str,
    run_dir: Path,
    overall_idx: int,
    overall_total: int,
    run_idx: int,
    run_total: int,
    episode_idx: int,
    episode_total: int,
    ep: EpisodeSpec,
    llm_cache: LLMCache,
    results_csv: Path,
    results_jsonl: Path,
    history_state: Dict[str, Any],
) -> None:
    ep_folder = run_dir / "episodes" / f"run_{run_idx:03d}_ep_{episode_idx:04d}_{ep.id}"
    ep_folder.mkdir(parents=True, exist_ok=True)

    timeout_s = float(args.timeout_s) if args.timeout_s is not None else float(ep.timeout_s)
    settle_s = float(args.settle_s) if args.settle_s is not None else float(ep.settle_s)

    desired_visible_history = _build_episode_visible_history(ep, history_state.get("visible_history", []))
    setup_info = _apply_initial_state(
        drone=drone,
        initial_state=ep.initial_state,
        run_origin_latlon=history_state.get("run_origin_latlon"),
        desired_visible_history=desired_visible_history,
    )

    if settle_s > 0:
        time.sleep(settle_s)

    telemetry = TelemetryRecorder(drone.vehicle, ep_folder / "telemetry.csv", rate_hz=10.0)
    telemetry.start()

    llm_key = _compute_llm_key_for_plan(
        task_text=ep.command,
        suite_name=suite_name,
        episode_id=ep.id,
        category=ep.category,
        ablation=_ablation_mode(args),
        plan_source="initial",
    )

    dsl = ""
    ttft_ms: Optional[float] = None
    cached_ttft_ms: Optional[float] = None
    plan_total_ms: Optional[float] = None
    cached_plan_total_ms: Optional[float] = None
    plan_valid = False
    unknown_skills: List[str] = []
    failure_stage = ""
    err_msg: Optional[str] = None

    t_task_submit = time.time()
    start_latlon = _safe_latlon(drone.vehicle)
    start_alt = _safe_alt(drone.vehicle)
    start_heading = _safe_heading(drone.vehicle)

    mover = MovementDetector(
        drone.vehicle,
        start_latlon=start_latlon,
        start_alt_m=start_alt,
        start_heading_deg=start_heading,
        speed_thresh_mps=float(args.movement_speed_mps),
        disp_thresh_m=float(args.movement_disp_m),
        alt_thresh_m=float(args.movement_alt_m),
        yaw_thresh_deg=float(args.movement_yaw_deg),
        hold_s=float(args.movement_hold_s),
        poll_hz=10.0,
    )

    t_episode_start = t_task_submit
    mover.start(start_time_s=t_task_submit)

    mission_run: Dict[str, Any] = {}

    def _exec_mission():
        mission_run.update(
            _execute_agentic_episode(
                args=args,
                drone=drone,
                suite_name=suite_name,
                ep=ep,
                llm_cache=llm_cache,
            )
        )

    exec_thread = threading.Thread(target=_exec_mission, daemon=True)
    exec_thread.start()
    exec_thread.join(timeout=timeout_s)

    if exec_thread.is_alive():
        failure_stage = "timeout"
        err_msg = f"Episode timed out after {timeout_s:.1f}s"
    else:
        dsl = str(mission_run.get("dsl") or "")
        ttft_ms = mission_run.get("ttft_ms")
        cached_ttft_ms = mission_run.get("cached_ttft_ms")
        plan_total_ms = mission_run.get("plan_total_ms")
        cached_plan_total_ms = mission_run.get("cached_plan_total_ms")
        plan_valid = bool(mission_run.get("plan_valid", False))
        unknown_skills = list(mission_run.get("unknown_skills") or [])
        failure_stage = str(mission_run.get("failure_stage") or "")
        err_msg = mission_run.get("error")

    generated_plans = mission_run.get("generated_plans") or []
    if generated_plans:
        plan_dump = []
        for idx, plan_entry in enumerate(generated_plans, start=1):
            plan_dump.append(f"# Plan {idx}: {plan_entry.get('source', 'unknown')}")
            plan_dump.append(str(plan_entry.get("dsl") or ""))
            plan_dump.append("")
        (ep_folder / "dsl.txt").write_text("\n".join(plan_dump).rstrip() + "\n", encoding="utf-8")
    else:
        (ep_folder / "dsl.txt").write_text(dsl, encoding="utf-8")

    post_exec_visible_history = _copy_history(drone.hist)
    history_state["visible_history"] = _copy_history(post_exec_visible_history)

    cleanup_ok = True
    try:
        cleanup_ok = _cleanup_after_episode(drone, ep.cleanup_mode, post_exec_visible_history)
        drone.hist = _copy_history(post_exec_visible_history)
    except Exception:
        cleanup_ok = False

    terminal_ok = True
    if ep.cleanup_mode == "land":
        terminal_ok = bool(cleanup_ok) and _wait_terminal(drone, timeout_s=20.0)

    t_episode_end = time.time()
    episode_time_s = t_episode_end - t_episode_start

    move_res = mover.stop()
    telemetry.stop()

    execution_success = False
    if failure_stage == "":
        execution_success = bool(plan_valid)
        if ep.cleanup_mode == "land":
            execution_success = execution_success and terminal_ok
        if not execution_success:
            failure_stage = "execution" if not terminal_ok else "validation"

    objective = evaluate_mission_objective(
        dsl=dsl,
        telemetry_csv=ep_folder / "telemetry.csv",
        oracle=ep.oracle,
    )
    objective_checked = bool(objective.get("objective_checked"))
    objective_met = objective.get("objective_met")

    if objective_checked:
        success = bool(execution_success) and (objective_met is True)
        if execution_success and not success and failure_stage == "":
            failure_stage = "objective"
    else:
        success = bool(execution_success)

    episode_meta: Dict[str, Any] = {
        "suite": suite_name,
        "overall_idx": overall_idx,
        "overall_total": overall_total,
        "run_idx": run_idx,
        "run_total": run_total,
        "episode_idx": episode_idx,
        "episode_total": episode_total,
        "episode_id": ep.id,
        "category": ep.category,
        "command": ep.command,
        "timeout_s": timeout_s,
        "settle_s": settle_s,
        "cleanup_mode": ep.cleanup_mode,
        "tags": ep.tags,
        "initial_state": None if ep.initial_state is None else {
            "mode": ep.initial_state.mode,
            "altitude_m": ep.initial_state.altitude_m,
            "heading_deg": ep.initial_state.heading_deg,
            "position_ne_m": list(ep.initial_state.position_ne_m) if ep.initial_state.position_ne_m is not None else None,
            "transit_alt_m": ep.initial_state.transit_alt_m,
            "settle_s": ep.initial_state.settle_s,
        },
        "setup": setup_info,
        "visible_history_seed": desired_visible_history,
        "visible_history_after_execution": post_exec_visible_history,
        "llm_mode": args.llm,
        "llm_key": llm_key,
        "ablation": _ablation_mode(args),
        "generated_plans": generated_plans,
        "dsl_present": bool(dsl.strip()),
        "plan_valid": plan_valid,
        "unknown_skills": unknown_skills,
        "success": success,
        "execution_success": execution_success,
        "objective": objective,
        "failure_stage": failure_stage or ("" if success else "unknown"),
        "error": err_msg,
        "metrics": {
            "task_submit_epoch_s": t_task_submit,
            "ttft_ms": ttft_ms,
            "cached_ttft_ms": cached_ttft_ms,
            "ttfm_ms": move_res.ttfm_ms,
            "ttfm_method": move_res.method,
            "plan_total_ms": plan_total_ms,
            "cached_plan_total_ms": cached_plan_total_ms,
            "episode_time_s": episode_time_s,
        },
        "terminal_ok": terminal_ok,
    }

    (ep_folder / "episode.json").write_text(json.dumps(episode_meta, indent=2), encoding="utf-8")

    with results_csv.open("a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([
            overall_idx, overall_total,
            run_idx, run_total,
            episode_idx, episode_total,
            ep.id, ep.category,
            success, execution_success,
            objective_checked, objective_met,
            failure_stage,
            ttft_ms, cached_ttft_ms,
            move_res.ttfm_ms,
            plan_total_ms, cached_plan_total_ms,
            episode_time_s,
            plan_valid, ";".join(unknown_skills),
            ep.cleanup_mode, len(post_exec_visible_history), setup_info.get("applied", False),
        ])

    with results_jsonl.open("a", encoding="utf-8") as f:
        f.write(json.dumps(episode_meta, ensure_ascii=False) + "\n")

    log_status(
        f"[EVAL] overall={overall_idx}/{overall_total} | run={run_idx}/{run_total} | ep={episode_idx}/{episode_total} | "
        f"{ep.id} | cat={ep.category} | success={success} | exec={execution_success} | obj={objective_met} | "
        f"ttft_ms={ttft_ms} | cached_ttft_ms={cached_ttft_ms} | "
        f"ttfm_ms={move_res.ttfm_ms} | ttfm_method={move_res.method} | "
        f"time_s={episode_time_s:.1f} | stage={failure_stage} | cleanup={ep.cleanup_mode}"
    )
