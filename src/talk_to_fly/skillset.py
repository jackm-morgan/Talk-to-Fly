"""Skill and skillset definitions used by Talk-to-Fly.

This module defines:
- Low-level skills backed by the drone wrapper.
- High-level skills represented as reusable DSL macros.
- Prompt-serialization helpers for exposing the skill library to the planner.
"""

from __future__ import annotations

from typing import Callable, List, Dict, Optional, Union
import re
import math


# =============================================================================
# Core Data Structures
# =============================================================================

class SkillArg:
    """Represents a single argument to a skill."""
    def __init__(self, name: str, arg_type: type, units: str = None):
        self.name = name
        self.arg_type = arg_type
        self.units = units

    def __repr__(self) -> str:
        u = f", units={self.units}" if self.units else ""
        return f"SkillArg(name={self.name}, type={self.arg_type.__name__}{u})"


class SkillItem:
    """
    Abstract base class for a skill.

    Note:
      abbr_dict lets you register short aliases that map to canonical names.
      (Useful when a descriptive skill name should also accept a shorter DSL token such as "sq".)
    """
    abbr_dict: Dict[str, str] = {}

    def get_name(self) -> str:
        raise NotImplementedError

    def get_skill_description(self) -> str:
        raise NotImplementedError

    def get_argument(self) -> List[SkillArg]:
        raise NotImplementedError

    def execute(self, arg_list: List[Union[int, float, str]]):
        raise NotImplementedError


# =============================================================================
# Low-Level Skills
# =============================================================================

class LowLevelSkillItem(SkillItem):
    """
    Low-level skill: directly calls a Python callable on the drone wrapper.
    """
    def __init__(
        self,
        skill_name: str,
        skill_callable: Callable,
        skill_description: str = "",
        args: Optional[List[SkillArg]] = None,
        abbr: Optional[str] = None,
    ):
        self.skill_name = skill_name
        self.skill_callable = skill_callable
        self.skill_description = skill_description
        self.args = list(args) if args else []

        # Register an abbreviation token if provided (defaults to the name).
        self.abbr = abbr or skill_name
        SkillItem.abbr_dict[self.abbr] = skill_name

    def get_name(self) -> str:
        return self.skill_name

    def get_skill_description(self) -> str:
        return self.skill_description

    def get_argument(self) -> List[SkillArg]:
        return self.args

    def execute(self, arg_list: List[Union[int, float, str]]):
        if not callable(self.skill_callable):
            raise ValueError(f"'{self.skill_callable}' is not callable.")
        return self.skill_callable(*arg_list)

    def __repr__(self) -> str:
        return f"name:{self.skill_name}, args:{self.args}, description:{self.skill_description}"


# =============================================================================
# High-Level Skills (DSL macros)
# =============================================================================

HighLevelDef = Union[str, Callable[..., str]]


class HighLevelSkillItem(SkillItem):
    """
    High-level skill: expands into a DSL string.

    Two forms:
      1) Template string definition:
         definition = "4{mf($1);tcw(90)};"
         Args are auto-inferred from the referenced skills (mf, tcw, ...).

      2) Callable definition:
         definition = lambda radius: "36{mf(...);tcw(...)};"
         Args must be provided explicitly (because there is no template to parse).
    """

    def __init__(
        self,
        skill_name: str,
        definition: HighLevelDef,
        skill_description: str = "",
        args: Optional[List[SkillArg]] = None,
        prompt_definition: Optional[str] = None,
        abbr: Optional[str] = None,
    ):
        self.skill_name = skill_name
        self.definition = definition
        self.skill_description = skill_description

        self.low_level_skillset: Optional[SkillSet] = None
        self.high_level_skillset: Optional[SkillSet] = None

        # If args not supplied and definition is a template string, we infer later.
        self.args: List[SkillArg] = list(args) if args else []

        # Used when serializing planner prompts, especially for callable expansions.
        self.prompt_definition = prompt_definition

        # Optional abbreviation token.
        self.abbr = abbr
        if self.abbr:
            SkillItem.abbr_dict[self.abbr] = skill_name

    def set_skillset(self, low_level_skillset: "SkillSet", high_level_skillset: "SkillSet"):
        self.low_level_skillset = low_level_skillset
        self.high_level_skillset = high_level_skillset

        # Auto-infer arguments only for string templates, and only if args weren’t provided.
        if isinstance(self.definition, str) and not self.args:
            self.args = self.generate_argument_list()

    def generate_argument_list(self) -> List[SkillArg]:
        """
        Parse a template definition and infer $1, $2, ... argument types by inspecting
        referenced skills' argument lists.

        Example:
          "4{mf($1);tcw(90)};"  -> $1 comes from mf(distance: meters)
        """
        if self.low_level_skillset is None or self.high_level_skillset is None:
            raise ValueError("Skillsets not set; cannot infer arguments.")

        # Finds "mf($1)" and "tcw(90)" etc inside the template.
        skill_calls = re.findall(r"(\w+)\(([^)]*)\)", self.definition)

        # Map "$1" -> SkillArg(...) (preserve first-seen type/units).
        arg_types: Dict[str, SkillArg] = {}

        for called_skill_name, raw_args in skill_calls:
            # Split raw arg string by commas, ignoring whitespace.
            args = [a.strip() for a in raw_args.split(",")] if raw_args.strip() else []

            skill = (
                self.low_level_skillset.get_skill(called_skill_name)
                or self.high_level_skillset.get_skill(called_skill_name)
            )
            if skill is None:
                raise ValueError(f"Skill '{called_skill_name}' not found (referenced in '{self.skill_name}').")

            function_args = skill.get_argument()
            for i, arg in enumerate(args):
                if arg.startswith("$") and arg not in arg_types:
                    if i >= len(function_args):
                        raise ValueError(
                            f"Arg index {i} out of range for '{called_skill_name}' "
                            f"while inferring args for '{self.skill_name}'."
                        )
                    arg_types[arg] = function_args[i]

        # Sort by $1, $2, ... (lexicographic works for $1..$9; safe enough here)
        arg_types = dict(sorted(arg_types.items(), key=lambda kv: kv[0]))
        return [a for a in arg_types.values()]

    def get_name(self) -> str:
        return self.skill_name

    def get_skill_description(self) -> str:
        return self.skill_description

    def get_argument(self) -> List[SkillArg]:
        return self.args

    def execute(self, arg_list: List[Union[int, float, str]]):
        if self.low_level_skillset is None:
            raise ValueError("Low-level skillset not set.")
        if len(arg_list) != len(self.args):
            raise ValueError(f"Expected {len(self.args)} arguments, got {len(arg_list)}.")

        # Callable expansion
        if callable(self.definition):
            return self.definition(*arg_list)

        # Template expansion
        definition = self.definition
        for i, val in enumerate(arg_list):
            definition = definition.replace(f"${i+1}", str(val))
        return definition

    def __repr__(self) -> str:
        d = self.definition if isinstance(self.definition, str) else "<callable>"
        return f"name:{self.skill_name}, definition:{d}, args:{self.args}, description:{self.skill_description}"


# =============================================================================
# SkillSet Container
# =============================================================================

class SkillSet:
    def __init__(self, level: str = "low", lower_level_skillset: Optional["SkillSet"] = None):
        self.skills: Dict[str, SkillItem] = {}
        self.level = level
        self.lower_level_skillset = lower_level_skillset

    def get_skill(self, skill_name: str) -> Optional[SkillItem]:
        # Direct lookup
        skill = self.skills.get(skill_name)
        if skill is not None:
            return skill

        # Abbreviation lookup
        if skill_name in SkillItem.abbr_dict:
            return self.skills.get(SkillItem.abbr_dict[skill_name])

        return None

    def add_skill(self, skill_item: SkillItem):
        if skill_item.get_name() in self.skills:
            raise ValueError(f"Skill '{skill_item.get_name()}' already exists.")

        # High-level skills need access to both skillsets for argument inference.
        if self.level == "high" and isinstance(skill_item, HighLevelSkillItem):
            if self.lower_level_skillset is None:
                raise ValueError("Low-level skillset not set for high-level skillset.")
            skill_item.set_skillset(self.lower_level_skillset, self)

        self.skills[skill_item.get_name()] = skill_item

    def remove_skill(self, skill_name: str):
        if skill_name not in self.skills:
            raise ValueError(f"No skill '{skill_name}' found.")
        del self.skills[skill_name]

    def __repr__(self):
        return "\n".join([str(s) for s in self.skills.values()])


# =============================================================================
# Factory: Low-Level SkillSet
# =============================================================================

def create_low_level_skillset(drone) -> SkillSet:
    """
    Create low-level skills backed by the drone wrapper.
    These are the primitives the DSL ultimately compiles into.
    """
    skillset = SkillSet(level="low")

    skillset.add_skill(LowLevelSkillItem("a", drone.arm, "Arm motors"))
    skillset.add_skill(LowLevelSkillItem("d", drone.disarm, "Disarm motors"))

    skillset.add_skill(LowLevelSkillItem(
        "tk", drone.takeoff, "Arm motors and takeoff",
        [SkillArg("altitude", float, "meters")]
    ))
    skillset.add_skill(LowLevelSkillItem("ld", drone.land, "Land drone and disarm motors"))
    skillset.add_skill(LowLevelSkillItem("rtl", drone.rtl, "Return to launch"))

    skillset.add_skill(LowLevelSkillItem(
        "mf", drone.move_forward, "Move forward",
        [SkillArg("distance", float, "meters")]
    ))
    skillset.add_skill(LowLevelSkillItem(
        "mr", drone.move_right, "Move right",
        [SkillArg("distance", float, "meters")]
    ))
    skillset.add_skill(LowLevelSkillItem(
        "ml", drone.move_left, "Move left",
        [SkillArg("distance", float, "meters")]
    ))
    skillset.add_skill(LowLevelSkillItem(
        "mb", drone.move_backward, "Move backward",
        [SkillArg("distance", float, "meters")]
    ))
    skillset.add_skill(LowLevelSkillItem(
        "mu", drone.ascend, "Ascend",
        [SkillArg("distance", float, "meters")]
    ))
    skillset.add_skill(LowLevelSkillItem(
        "md", drone.descend, "Descend",
        [SkillArg("distance", float, "meters")]
    ))

    skillset.add_skill(LowLevelSkillItem(
        "tcw", drone.turn_cw, "Turn clockwise",
        [SkillArg("deg", float, "degrees")]
    ))
    skillset.add_skill(LowLevelSkillItem(
        "tccw", drone.turn_ccw, "Turn counter-clockwise",
        [SkillArg("deg", float, "degrees")]
    ))

    skillset.add_skill(LowLevelSkillItem("o", drone.orient, "Rotate to face original heading"))

    skillset.add_skill(LowLevelSkillItem(
        "hv", drone.hover, "Hover",
        [SkillArg("seconds", float, "seconds")]
    ))

    skillset.add_skill(LowLevelSkillItem(
        "go",
        lambda arg: drone.goto(*[float(a) for a in arg.split(",")]),
        "Go to coordinates",
        [SkillArg("coords", str, "x,y,z in meters")]
    ))

    skillset.add_skill(LowLevelSkillItem(
        "l",
        lambda msg: print(f"[DSL LOG] {msg}"),
        "Log message",
        [SkillArg("msg", str, "string message")]
    ))

    return skillset


# =============================================================================
# Factory: High-Level SkillSet (macros)
# =============================================================================

# Default polygon resolution for circle/orbit approximations.
DEFAULT_CIRCLE_STEPS = 10


def _circle_polygon(radius_m: float, steps: int, clockwise: bool) -> str:
    """
    Return a DSL loop approximating a circle by a regular polygon.

    Produces:
      steps{mf(step_len);tcw(turn_deg)};  (or tccw)

    Where:
      step_len = (2*pi*radius)/steps
      turn_deg = 360/steps
    """
    steps_i = int(steps)
    if steps_i < 3:
        raise ValueError("Circle/orbit steps must be >= 3.")

    step_len = (2.0 * math.pi * float(radius_m)) / steps_i
    turn_deg = 360.0 / steps_i
    turn_cmd = "tcw" if clockwise else "tccw"

    # Keep formatting stable (avoid scientific notation).
    return "{}{{mf({:.3f});{}({:.3f})}};".format(steps_i, step_len, turn_cmd, turn_deg)


def create_high_level_skillset(low_level_skillset: SkillSet) -> SkillSet:
    """
    Create high-level skills as DSL macros.

    These compile down into the low-level primitives.
    """
    high_level = SkillSet(level="high", lower_level_skillset=low_level_skillset)

    # Example placeholder skill you had previously
    high_level.add_skill(HighLevelSkillItem("scan", "True", "Scan for object"))

    # --- Square macros ---
    high_level.add_skill(HighLevelSkillItem(
        "sq",
        "4{mf($1);tcw(90)};",
        "Fly a square (CW) with side length $1 meters"
    ))
    high_level.add_skill(HighLevelSkillItem(
        "sqccw",
        "4{mf($1);tccw(90)};",
        "Fly a square (CCW) with side length $1 meters"
    ))

    # --- Circle/orbit macros (radius-based, callable expansions) ---
    # circle(radius): fixed resolution using DEFAULT_CIRCLE_STEPS
    high_level.add_skill(HighLevelSkillItem(
        "crc",
        lambda radius: _circle_polygon(radius, DEFAULT_CIRCLE_STEPS, clockwise=True),
        "Approximate a circle (CW) with radius $1 meters",
        args=[SkillArg("radius", float, "meters")],
        prompt_definition=(
            f"Expands to {DEFAULT_CIRCLE_STEPS}{{mf(step);tcw({360/DEFAULT_CIRCLE_STEPS:.3f})}}; "
            f"where step = (2*pi*radius)/{DEFAULT_CIRCLE_STEPS}"
        )
    ))
    high_level.add_skill(HighLevelSkillItem(
        "crcccw",
        lambda radius: _circle_polygon(radius, DEFAULT_CIRCLE_STEPS, clockwise=False),
        "Approximate a circle (CCW) with radius $1 meters",
        args=[SkillArg("radius", float, "meters")],
        prompt_definition=(
            f"Expands to {DEFAULT_CIRCLE_STEPS}{{mf(step);tccw({360/DEFAULT_CIRCLE_STEPS:.3f})}}; "
            f"where step = (2*pi*radius)/{DEFAULT_CIRCLE_STEPS}"
        )
    ))

    # orbit(radius): same as circle with your current primitive set
    high_level.add_skill(HighLevelSkillItem(
        "orbit",
        lambda radius: _circle_polygon(radius, DEFAULT_CIRCLE_STEPS, clockwise=True),
        "Orbit (CW) with radius $1 meters (polygon approximation)",
        args=[SkillArg("radius", float, "meters")],
        prompt_definition=(
            f"Same as circle: {DEFAULT_CIRCLE_STEPS}{{mf(step);tcw({360/DEFAULT_CIRCLE_STEPS:.3f})}}; "
            f"step = (2*pi*radius)/{DEFAULT_CIRCLE_STEPS}"
        )
    ))
    high_level.add_skill(HighLevelSkillItem(
        "orbitccw",
        lambda radius: _circle_polygon(radius, DEFAULT_CIRCLE_STEPS, clockwise=False),
        "Orbit (CCW) with radius $1 meters (polygon approximation)",
        args=[SkillArg("radius", float, "meters")],
        prompt_definition=(
            f"Same as circle: {DEFAULT_CIRCLE_STEPS}{{mf(step);tccw({360/DEFAULT_CIRCLE_STEPS:.3f})}}; "
            f"step = (2*pi*radius)/{DEFAULT_CIRCLE_STEPS}"
        )
    ))

    # Optional: variable-resolution circle/orbit (radius + steps)
    high_level.add_skill(HighLevelSkillItem(
        "circlen",
        lambda radius, steps: _circle_polygon(radius, steps, clockwise=True),
        "Approximate a circle (CW) with radius $1 meters using $2 segments",
        args=[SkillArg("radius", float, "meters"), SkillArg("steps", int, "count")],
        prompt_definition="Expands to N{mf((2*pi*radius)/N);tcw(360/N)}; with N=$2"
    ))
    high_level.add_skill(HighLevelSkillItem(
        "orbitn",
        lambda radius, steps: _circle_polygon(radius, steps, clockwise=True),
        "Orbit (CW) with radius $1 meters using $2 segments",
        args=[SkillArg("radius", float, "meters"), SkillArg("steps", int, "count")],
        prompt_definition="Expands to N{mf((2*pi*radius)/N);tcw(360/N)}; with N=$2"
    ))

    return high_level


# =============================================================================
# Prompt Serialization Helper
# =============================================================================

def skillset_to_prompt_json(skillset: SkillSet):
    """
    Convert skills to two JSON-serializable lists: (high, low).

    For high-level skills:
      - If definition is a template string, include it.
      - If definition is callable, include `prompt_definition` if available,
        otherwise a short placeholder.

    This is intended for building the LLM prompt.
    """
    high = []
    low = []

    for skill in skillset.skills.values():
        info = {
            "name": skill.get_name(),
            "description": skill.get_skill_description(),
            "args": [
                {
                    "name": arg.name,
                    "type": arg.arg_type.__name__,
                    "units": getattr(arg, "units", None),
                }
                for arg in skill.get_argument()
            ],
        }

        if isinstance(skill, HighLevelSkillItem):
            if isinstance(skill.definition, str):
                info["definition"] = skill.definition
            else:
                info["definition"] = skill.prompt_definition or "<callable expansion>"
            high.append(info)
        else:
            low.append(info)

    return high, low
