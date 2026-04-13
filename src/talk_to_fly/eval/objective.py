"""Objective-based mission evaluation utilities."""

from __future__ import annotations

import csv
import math
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

R_EARTH_M = 6371000.0


def _safe_float(x) -> Optional[float]:
    try:
        if x is None:
            return None
        s = str(x).strip()
        if s == "" or s.lower() == "none":
            return None
        return float(s)
    except Exception:
        return None


def _latlon_to_ne_m(lat0: float, lon0: float, lat: float, lon: float) -> Tuple[float, float]:
    dlat = math.radians(lat - lat0)
    dlon = math.radians(lon - lon0)
    north = dlat * R_EARTH_M
    east = dlon * R_EARTH_M * math.cos(math.radians(lat0))
    return north, east


def _unwrap_heading_delta_deg(prev: float, cur: float) -> float:
    return (cur - prev + 180.0) % 360.0 - 180.0


def read_telemetry_csv(path: Path) -> List[Dict[str, Any]]:
    with Path(path).open("r", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def compute_telemetry_metrics(telemetry_csv: Path) -> Dict[str, Any]:
    rows = read_telemetry_csv(Path(telemetry_csv))

    lat0 = lon0 = None
    for row in rows:
        lat = _safe_float(row.get("lat"))
        lon = _safe_float(row.get("lon"))
        if lat is not None and lon is not None:
            lat0, lon0 = lat, lon
            break

    path_length_m = 0.0
    end_distance_m = None
    max_alt_m = None
    min_alt_m = None
    start_alt_m = None
    end_alt_m = None
    yaw_total_abs_deg = 0.0
    hover_low_speed_s = 0.0

    prev_ne = None
    prev_hdg = None
    prev_t = None
    prev_low_speed = False

    for row in rows:
        t = _safe_float(row.get("t_rel_s"))
        lat = _safe_float(row.get("lat"))
        lon = _safe_float(row.get("lon"))
        alt = _safe_float(row.get("alt_m"))
        gs = _safe_float(row.get("groundspeed_mps"))
        hdg = _safe_float(row.get("heading_deg"))

        if alt is not None:
            if start_alt_m is None:
                start_alt_m = alt
            end_alt_m = alt
            max_alt_m = alt if max_alt_m is None else max(max_alt_m, alt)
            min_alt_m = alt if min_alt_m is None else min(min_alt_m, alt)

        if lat0 is not None and lon0 is not None and lat is not None and lon is not None:
            ne = _latlon_to_ne_m(lat0, lon0, lat, lon)
            if prev_ne is not None:
                path_length_m += math.hypot(ne[0] - prev_ne[0], ne[1] - prev_ne[1])
            prev_ne = ne
            end_distance_m = math.hypot(ne[0], ne[1])

        if prev_hdg is not None and hdg is not None:
            yaw_total_abs_deg += abs(_unwrap_heading_delta_deg(prev_hdg, hdg))
        if hdg is not None:
            prev_hdg = hdg

        if t is not None and prev_t is not None:
            dt = max(0.0, t - prev_t)
            low_speed = (gs is not None and gs <= 0.30)
            if prev_low_speed and low_speed:
                hover_low_speed_s += dt
            prev_low_speed = low_speed
        elif gs is not None:
            prev_low_speed = (gs <= 0.30)
        if t is not None:
            prev_t = t

    alt_range_m = None
    if max_alt_m is not None and min_alt_m is not None:
        alt_range_m = max_alt_m - min_alt_m

    return {
        "path_length_m": path_length_m,
        "end_distance_m": end_distance_m,
        "max_alt_m": max_alt_m,
        "min_alt_m": min_alt_m,
        "start_alt_m": start_alt_m,
        "end_alt_m": end_alt_m,
        "alt_range_m": alt_range_m,
        "yaw_total_abs_deg": yaw_total_abs_deg,
        "hover_low_speed_s": hover_low_speed_s,
        "num_samples": len(rows),
    }


@dataclass
class PlanExpectation:
    takeoff_alt_m: Optional[float]
    hover_s: float
    move_total_m: float
    yaw_total_abs_deg: float
    expected_end_dist_m: float


def _extract_braced(code: str, brace_idx: int) -> Tuple[str, int]:
    depth = 1
    i = brace_idx + 1
    start = i
    in_quote = None
    while i < len(code):
        ch = code[i]
        if in_quote:
            if ch == in_quote:
                in_quote = None
            elif ch == "\\":
                i += 1
        else:
            if ch in ("'", '"'):
                in_quote = ch
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return code[start:i], i + 1
        i += 1
    return code[start:], len(code)


def expand_loops(code: str, max_expand: int = 20000) -> str:
    out: List[str] = []
    i = 0
    while i < len(code):
        if code[i].isdigit():
            j = i
            while j < len(code) and code[j].isdigit():
                j += 1
            if j < len(code) and code[j] == "{":
                n = int(code[i:j])
                body, next_i = _extract_braced(code, j)
                body_expanded = expand_loops(body, max_expand=max_expand)
                chunk = body_expanded * n
                if sum(len(x) for x in out) + len(chunk) > max_expand:
                    out.append(body_expanded)
                else:
                    out.append(chunk)
                i = next_i
                continue
        out.append(code[i])
        i += 1
    return "".join(out)


def parse_calls(code: str) -> List[Tuple[str, str]]:
    calls: List[Tuple[str, str]] = []
    i = 0
    while i < len(code):
        if code[i].isalpha() or code[i] == "_":
            j = i + 1
            while j < len(code) and (code[j].isalnum() or code[j] == "_"):
                j += 1
            name = code[i:j]
            k = j
            while k < len(code) and code[k].isspace():
                k += 1
            if k < len(code) and code[k] == "(":
                depth = 1
                k += 1
                start = k
                in_quote = None
                while k < len(code) and depth > 0:
                    ch = code[k]
                    if in_quote:
                        if ch == in_quote:
                            in_quote = None
                        elif ch == "\\":
                            k += 1
                    else:
                        if ch in ("'", '"'):
                            in_quote = ch
                        elif ch == "(":
                            depth += 1
                        elif ch == ")":
                            depth -= 1
                            if depth == 0:
                                break
                    k += 1
                arg = code[start:k].strip()
                if name not in ("l", "log"):
                    calls.append((name, arg))
                i = k + 1
                continue
        i += 1
    return calls


def _as_float(arg: str) -> Optional[float]:
    try:
        m = ""
        for ch in arg.strip():
            if ch.isdigit() or ch in ".-+eE":
                m += ch
            else:
                break
        return float(m) if m else None
    except Exception:
        return None


def expectation_from_dsl(dsl: str) -> PlanExpectation:
    calls = parse_calls(expand_loops(dsl))
    takeoff_alt = None
    hover_s = 0.0
    move_total_m = 0.0
    yaw_total_abs_deg = 0.0

    x = 0.0
    y = 0.0
    heading_deg = 0.0

    def move_vec(dist_m: float, rel_deg: float) -> Tuple[float, float]:
        ang = math.radians(heading_deg + rel_deg)
        return dist_m * math.cos(ang), dist_m * math.sin(ang)

    for name, arg in calls:
        if name == "tk":
            a = _as_float(arg)
            if a is not None:
                takeoff_alt = a if takeoff_alt is None else max(takeoff_alt, a)
        elif name == "hv":
            s = _as_float(arg)
            if s is not None:
                hover_s += max(0.0, s)
        elif name in ("mf", "mb", "mr", "ml"):
            d = _as_float(arg)
            if d is None:
                continue
            d = abs(d)
            move_total_m += d
            if name == "mf":
                dx, dy = move_vec(d, 0.0)
            elif name == "mb":
                dx, dy = move_vec(d, 180.0)
            elif name == "mr":
                dx, dy = move_vec(d, 90.0)
            else:
                dx, dy = move_vec(d, -90.0)
            x += dx
            y += dy
        elif name == "tcw":
            deg = _as_float(arg)
            if deg is not None:
                yaw_total_abs_deg += abs(deg)
                heading_deg -= abs(deg)
        elif name == "tccw":
            deg = _as_float(arg)
            if deg is not None:
                yaw_total_abs_deg += abs(deg)
                heading_deg += abs(deg)

    return PlanExpectation(
        takeoff_alt_m=takeoff_alt,
        hover_s=hover_s,
        move_total_m=move_total_m,
        yaw_total_abs_deg=yaw_total_abs_deg,
        expected_end_dist_m=math.hypot(x, y),
    )


def _normalise_skill_list(raw: Any) -> List[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        return [raw]
    if isinstance(raw, list):
        return [str(x) for x in raw if str(x).strip()]
    raise ValueError("Skill lists in the oracle must be a string or a list of strings.")


def _skill_counter(dsl: str) -> Counter:
    return Counter(name for name, _ in parse_calls(expand_loops(dsl)))


def evaluate_mission_objective(
    *,
    dsl: str,
    telemetry_csv: Path,
    oracle: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    telem = compute_telemetry_metrics(Path(telemetry_csv))
    exp = expectation_from_dsl(dsl) if dsl.strip() else None
    skill_counts = _skill_counter(dsl) if dsl.strip() else Counter()

    oracle_req: Dict[str, Any] = {}
    oracle_type = None
    required_skills: List[str] = []
    forbidden_skills: List[str] = []
    if isinstance(oracle, dict):
        oracle_type = str(oracle.get("type") or "").strip().lower() or None
        for key in (
            "min_alt_m",
            "max_alt_m",
            "max_alt_range_m",
            "min_path_m",
            "min_yaw_abs_deg",
            "min_hover_s",
            "min_end_dist_m",
            "max_end_dist_m",
        ):
            if key in oracle and oracle[key] is not None:
                oracle_req[key] = oracle[key]
        required_skills = _normalise_skill_list(oracle.get("require_skills"))
        forbidden_skills = _normalise_skill_list(oracle.get("forbid_skills"))

    checks: Dict[str, Any] = {}
    objective_checked = False
    objective_met: Optional[bool] = None

    if oracle_req or required_skills or forbidden_skills:
        objective_checked = True
        ok = True

        if "min_alt_m" in oracle_req:
            tgt = float(oracle_req["min_alt_m"])
            obs = telem.get("max_alt_m")
            cond = (obs is not None and float(obs) >= tgt)
            checks["alt_ok"] = cond
            checks["min_alt_m"] = tgt
            checks["max_alt_m"] = obs
            ok = ok and cond

        if "max_alt_m" in oracle_req:
            tgt = float(oracle_req["max_alt_m"])
            obs = telem.get("max_alt_m")
            cond = (obs is not None and float(obs) <= tgt)
            checks["max_alt_cap_ok"] = cond
            checks["max_alt_m_limit"] = tgt
            checks["max_alt_m"] = obs
            ok = ok and cond

        if "max_alt_range_m" in oracle_req:
            tgt = float(oracle_req["max_alt_range_m"])
            obs = telem.get("alt_range_m")
            cond = (obs is not None and float(obs) <= tgt)
            checks["alt_range_ok"] = cond
            checks["max_alt_range_m"] = tgt
            checks["alt_range_m"] = obs
            ok = ok and cond

        if "min_path_m" in oracle_req:
            tgt = float(oracle_req["min_path_m"])
            obs = float(telem.get("path_length_m") or 0.0)
            cond = obs >= tgt
            checks["path_ok"] = cond
            checks["min_path_m"] = tgt
            checks["path_length_m"] = obs
            ok = ok and cond

        if "min_yaw_abs_deg" in oracle_req:
            tgt = float(oracle_req["min_yaw_abs_deg"])
            obs = float(telem.get("yaw_total_abs_deg") or 0.0)
            cond = obs >= tgt
            checks["yaw_ok"] = cond
            checks["min_yaw_abs_deg"] = tgt
            checks["yaw_total_abs_deg"] = obs
            ok = ok and cond

        if "min_hover_s" in oracle_req:
            tgt = float(oracle_req["min_hover_s"])
            obs = float(telem.get("hover_low_speed_s") or 0.0)
            cond = obs >= tgt
            checks["hover_ok"] = cond
            checks["min_hover_s"] = tgt
            checks["hover_low_speed_s"] = obs
            ok = ok and cond

        if "min_end_dist_m" in oracle_req:
            tgt = float(oracle_req["min_end_dist_m"])
            obs = telem.get("end_distance_m")
            cond = (obs is not None and float(obs) >= tgt)
            checks["min_end_ok"] = cond
            checks["min_end_dist_m"] = tgt
            checks["end_distance_m"] = obs
            ok = ok and cond

        if "max_end_dist_m" in oracle_req:
            tgt = float(oracle_req["max_end_dist_m"])
            obs = telem.get("end_distance_m")
            cond = (obs is not None and float(obs) <= tgt)
            checks["max_end_ok"] = cond
            checks["max_end_dist_m"] = tgt
            checks["end_distance_m"] = obs
            ok = ok and cond

        if required_skills:
            missing = [skill for skill in required_skills if skill_counts.get(skill, 0) <= 0]
            cond = len(missing) == 0
            checks["require_skills_ok"] = cond
            checks["required_skills"] = required_skills
            checks["missing_required_skills"] = missing
            ok = ok and cond

        if forbidden_skills:
            present = [skill for skill in forbidden_skills if skill_counts.get(skill, 0) > 0]
            cond = len(present) == 0
            checks["forbid_skills_ok"] = cond
            checks["forbidden_skills"] = forbidden_skills
            checks["present_forbidden_skills"] = present
            ok = ok and cond

        objective_met = bool(ok)
        return {
            "objective_checked": objective_checked,
            "objective_met": objective_met,
            "oracle_type": oracle_type or "requirements",
            "oracle_requirements": oracle_req,
            "checks": checks,
            "expected": None,
            "observed": telem,
            "observed_skills": dict(skill_counts),
        }

    if exp is None:
        return {
            "objective_checked": False,
            "objective_met": None,
            "oracle_type": oracle_type,
            "checks": {"reason": "no_dsl"},
            "expected": None,
            "observed": telem,
            "observed_skills": dict(skill_counts),
        }

    ALT_FRAC = 0.80
    MOVE_FRAC = 0.60
    YAW_FRAC = 0.60
    HOVER_FRAC = 0.80

    alt_ok = True
    if exp.takeoff_alt_m is not None and exp.takeoff_alt_m > 0.5:
        objective_checked = True
        obs = telem.get("max_alt_m")
        alt_ok = (obs is not None and float(obs) >= ALT_FRAC * float(exp.takeoff_alt_m))
        checks["alt_ok"] = alt_ok

    move_ok = True
    if exp.move_total_m >= 0.8:
        objective_checked = True
        obs = float(telem.get("path_length_m") or 0.0)
        move_ok = obs >= MOVE_FRAC * float(exp.move_total_m)
        checks["move_ok"] = move_ok

    yaw_ok = True
    if exp.yaw_total_abs_deg >= 20.0:
        objective_checked = True
        obs = float(telem.get("yaw_total_abs_deg") or 0.0)
        yaw_ok = obs >= YAW_FRAC * float(exp.yaw_total_abs_deg)
        checks["yaw_ok"] = yaw_ok

    hover_ok = True
    if exp.hover_s >= 0.8:
        objective_checked = True
        obs = float(telem.get("hover_low_speed_s") or 0.0)
        hover_ok = obs >= HOVER_FRAC * float(exp.hover_s)
        checks["hover_ok"] = hover_ok

    closure_ok = True
    if exp.expected_end_dist_m <= 1.0 and exp.move_total_m >= 4.0:
        objective_checked = True
        obs = telem.get("end_distance_m")
        closure_ok = (obs is not None and float(obs) <= 2.0)
        checks["closure_ok"] = closure_ok

    objective_met = bool(alt_ok and move_ok and yaw_ok and hover_ok and closure_ok) if objective_checked else None
    return {
        "objective_checked": objective_checked,
        "objective_met": objective_met,
        "oracle_type": oracle_type,
        "checks": checks,
        "expected": {
            "takeoff_alt_m": exp.takeoff_alt_m,
            "hover_s": exp.hover_s,
            "move_total_m": exp.move_total_m,
            "yaw_total_abs_deg": exp.yaw_total_abs_deg,
            "expected_end_dist_m": exp.expected_end_dist_m,
        },
        "observed": telem,
        "observed_skills": dict(skill_counts),
    }
