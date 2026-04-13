"""Suite-loading utilities for evaluation episode specifications."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union
import json

try:
    import yaml  # type: ignore
except Exception:  # pragma: no cover
    yaml = None


@dataclass(frozen=True)
class InitialStateSpec:
    mode: str = "ground"  # ground|airborne
    altitude_m: Optional[float] = None
    heading_deg: Optional[float] = None
    position_ne_m: Optional[Tuple[float, float]] = None
    transit_alt_m: Optional[float] = None
    settle_s: float = 1.0


@dataclass(frozen=True)
class EpisodeSpec:
    id: str
    category: str  # simple|compound|complex
    command: str
    timeout_s: float
    settle_s: float
    oracle: Optional[Dict[str, Any]] = None
    initial_state: Optional[InitialStateSpec] = None
    visible_history: List[Any] = field(default_factory=list)
    reset_visible_history: bool = False
    cleanup_mode: str = "land"  # land|none
    tags: List[str] = field(default_factory=list)


@dataclass(frozen=True)
class SuiteSpec:
    name: str
    episodes: List[EpisodeSpec]


def _load_obj(path: Union[str, Path]) -> Dict[str, Any]:
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    if p.suffix.lower() in (".json",):
        return json.loads(text)
    if yaml is None:
        raise RuntimeError("PyYAML is not available; provide a JSON suite instead.")
    return yaml.safe_load(text)


def _parse_position_ne(raw: Any) -> Optional[Tuple[float, float]]:
    if raw is None:
        return None
    if isinstance(raw, dict):
        north = raw.get("north_m", raw.get("north"))
        east = raw.get("east_m", raw.get("east"))
        if north is None or east is None:
            raise ValueError("initial_state.position_ne_m dict must contain north_m/east_m.")
        return (float(north), float(east))
    if isinstance(raw, (list, tuple)) and len(raw) == 2:
        return (float(raw[0]), float(raw[1]))
    raise ValueError("initial_state.position_ne_m must be [north_m, east_m] or {north_m, east_m}.")


def _parse_initial_state(raw: Any) -> Optional[InitialStateSpec]:
    if raw in (None, False):
        return None
    if not isinstance(raw, dict):
        raise ValueError("Episode 'initial_state' must be a mapping.")

    mode = str(raw.get("mode") or "ground").strip().lower()
    if mode not in ("ground", "airborne"):
        raise ValueError("initial_state.mode must be 'ground' or 'airborne'.")

    altitude_m = raw.get("altitude_m")
    heading_deg = raw.get("heading_deg")
    transit_alt_m = raw.get("transit_alt_m")
    settle_s = raw.get("settle_s", 1.0)

    return InitialStateSpec(
        mode=mode,
        altitude_m=(float(altitude_m) if altitude_m is not None else None),
        heading_deg=(float(heading_deg) if heading_deg is not None else None),
        position_ne_m=_parse_position_ne(raw.get("position_ne_m")),
        transit_alt_m=(float(transit_alt_m) if transit_alt_m is not None else None),
        settle_s=float(settle_s),
    )


def _parse_tags(raw: Any) -> List[str]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError("Episode 'tags' must be a list of strings.")
    return [str(x) for x in raw if str(x).strip()]


def load_suite(path: Union[str, Path]) -> SuiteSpec:
    obj = _load_obj(path)
    name = obj.get("name") or Path(path).stem
    defaults = obj.get("defaults") or {}
    timeout_s = float(defaults.get("timeout_s", 90))
    settle_s = float(defaults.get("settle_s", 2.0))
    cleanup_mode_default = str(defaults.get("cleanup_mode", "land")).strip().lower() or "land"
    if cleanup_mode_default not in ("land", "none"):
        raise ValueError("defaults.cleanup_mode must be 'land' or 'none'.")

    episodes: List[EpisodeSpec] = []
    for e in obj.get("episodes", []):
        if not isinstance(e, dict):
            continue
        eid = str(e.get("id") or "")
        if not eid:
            raise ValueError("Each episode must have an 'id'.")
        cat = str(e.get("category") or "simple").lower()
        cmd = str(e.get("command") or "").strip()
        if not cmd:
            raise ValueError(f"Episode '{eid}' missing 'command'.")

        cleanup_mode = str(e.get("cleanup_mode", cleanup_mode_default)).strip().lower() or cleanup_mode_default
        if cleanup_mode not in ("land", "none"):
            raise ValueError(f"Episode '{eid}' has invalid cleanup_mode='{cleanup_mode}'.")

        raw_visible_history = e.get("visible_history")
        if raw_visible_history is None:
            visible_history: List[Any] = []
        elif isinstance(raw_visible_history, list):
            visible_history = list(raw_visible_history)
        else:
            raise ValueError(f"Episode '{eid}' visible_history must be a list.")

        ep = EpisodeSpec(
            id=eid,
            category=cat,
            command=cmd,
            timeout_s=float(e.get("timeout_s", timeout_s)),
            settle_s=float(e.get("settle_s", settle_s)),
            oracle=e.get("oracle"),
            initial_state=_parse_initial_state(e.get("initial_state")),
            visible_history=visible_history,
            reset_visible_history=bool(e.get("reset_visible_history", False)),
            cleanup_mode=cleanup_mode,
            tags=_parse_tags(e.get("tags")),
        )
        episodes.append(ep)

    if not episodes:
        raise ValueError("Suite has no episodes.")

    return SuiteSpec(name=name, episodes=episodes)
