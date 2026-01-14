from typing import Callable, Any, List, Dict, Optional, Union
import re

                             
                
                             

class SkillArg:
    def __init__(self, name: str, arg_type: type, units: str = None):
        self.name = name
        self.arg_type = arg_type
        self.units = units

                             
                
                             

class SkillItem:
    abbr_dict: Dict[str, str] = {}                                    

    def get_name(self) -> str:
        raise NotImplementedError

    def get_skill_description(self) -> str:
        raise NotImplementedError

    def get_argument(self) -> List[SkillArg]:
        raise NotImplementedError

    def execute(self, arg_list: List[Union[int, float, str]]):
        raise NotImplementedError

                             
                 
                             

class LowLevelSkillItem(SkillItem):
    def __init__(self, skill_name: str, skill_callable: Callable,
                 skill_description: str = "", args: List[SkillArg] = []):
        self.skill_name = skill_name                                               
        self.skill_callable = skill_callable
        self.skill_description = skill_description
        self.args = args
        self.abbr = skill_name                                               
        SkillItem.abbr_dict[self.abbr] = skill_name

    def get_name(self) -> str:
        return self.skill_name

    def get_skill_description(self) -> str:
        return self.skill_description

    def get_argument(self) -> List[SkillArg]:
        return self.args

    def execute(self, arg_list: List[Union[int, float, str]]):
        if callable(self.skill_callable):
            return self.skill_callable(*arg_list)
        else:
            raise ValueError(f"'{self.skill_callable}' is not callable.")

    def __repr__(self):
        return f"name:{self.skill_name}, args:{self.args}, description:{self.skill_description}"

                             
                  
                             

class HighLevelSkillItem(SkillItem):
    def __init__(self, skill_name: str, definition: str, skill_description: str = ""):
        self.skill_name = skill_name
        self.definition = definition                                
        self.skill_description = skill_description
        self.low_level_skillset: Optional[SkillSet] = None
        self.args: List[SkillArg] = []

    def set_skillset(self, low_level_skillset: 'SkillSet', high_level_skillset: 'SkillSet'):
        self.low_level_skillset = low_level_skillset
        self.high_level_skillset = high_level_skillset
        self.args = self.generate_argument_list()

    def generate_argument_list(self) -> List[SkillArg]:
        skill_calls = re.findall(r'(\w+)\(([^)]*)\)', self.definition)
        arg_types = {}
        for skill_name, args in skill_calls:
            args = [a.strip() for a in args.split(',')]
            skill = self.low_level_skillset.get_skill(skill_name) \
                    or self.high_level_skillset.get_skill(skill_name)
            if skill is None:
                raise ValueError(f"Skill '{skill_name}' not found.")
            function_args = skill.get_argument()
            for i, arg in enumerate(args):
                if arg.startswith('$') and arg not in arg_types:
                    arg_types[arg] = function_args[i]
                             
        arg_types = dict(sorted(arg_types.items()))
        return [arg for arg in arg_types.values()]

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
        definition = self.definition
        for i, val in enumerate(arg_list):
            definition = definition.replace(f"${i+1}", str(val))
        return definition

    def __repr__(self):
        return f"name:{self.skill_name}, definition:{self.definition}, args:{self.args}, description:{self.skill_description}"

                             
          
                             

class SkillSet:
    def __init__(self, level: str = "low", lower_level_skillset: Optional['SkillSet'] = None):
        self.skills: Dict[str, SkillItem] = {}
        self.level = level
        self.lower_level_skillset = lower_level_skillset

    def get_skill(self, skill_name: str) -> Optional[SkillItem]:
        skill = self.skills.get(skill_name)
        if skill is None and skill_name in SkillItem.abbr_dict:
            skill = self.skills.get(SkillItem.abbr_dict[skill_name])
        return skill

    def add_skill(self, skill_item: SkillItem):
        if skill_item.get_name() in self.skills:
            raise ValueError(f"Skill '{skill_item.get_name()}' already exists.")
        if self.level == "high" and isinstance(skill_item, HighLevelSkillItem):
            if self.lower_level_skillset is not None:
                skill_item.set_skillset(self.lower_level_skillset, self)
            else:
                raise ValueError("Low-level skillset not set for high-level skill.")
        self.skills[skill_item.get_name()] = skill_item

    def remove_skill(self, skill_name: str):
        if skill_name not in self.skills:
            raise ValueError(f"No skill '{skill_name}' found.")
        del self.skills[skill_name]

    def __repr__(self):
        return "\n".join([str(s) for s in self.skills.values()])

                             
                                   
                             


def create_low_level_skillset(drone) -> SkillSet:
    skillset = SkillSet(level="low")
    
    skillset.add_skill(LowLevelSkillItem(
        "a", drone.arm, "Arm motors"
    ))
    skillset.add_skill(LowLevelSkillItem(
        "d", drone.disarm, "Disarm motors"
    ))
    skillset.add_skill(LowLevelSkillItem(
        "tk", drone.takeoff, "Arm motors and takeoff", 
        [SkillArg("altitude", float, "meters")]
    ))
    skillset.add_skill(LowLevelSkillItem(
        "ld", drone.land, "Land drone and disarm motors"
    ))
    skillset.add_skill(LowLevelSkillItem(
        "rtl", drone.rtl, "Return to launch"
    ))
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
    skillset.add_skill(LowLevelSkillItem(
        "o", drone.orient, "Rotate to face original heading"
    ))
    skillset.add_skill(LowLevelSkillItem(
        "hv", drone.hover, "Hover", 
        [SkillArg("seconds", float, "seconds")]
    ))
    skillset.add_skill(LowLevelSkillItem(
        "go", lambda arg: drone.goto(*[float(a) for a in arg.split(",")]), 
        "Go to coordinates", 
        [SkillArg("coords", str, "x,y,z in meters")]
    ))
    skillset.add_skill(LowLevelSkillItem(
        "l", lambda msg: print(f"[MINISPEC LOG] {msg}"), 
        "Log message", 
        [SkillArg("msg", str, "string message")]
    ))

    return skillset
                             
                                    
                             

def create_high_level_skillset(low_level_skillset: SkillSet) -> SkillSet:
    high_level = SkillSet(level="high", lower_level_skillset=low_level_skillset)
    high_level.add_skill(HighLevelSkillItem("scan", "True", "Scan for object"))
    return high_level


def skillset_to_prompt_json(skillset: 'SkillSet'):
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
                    "units": getattr(arg, "units", None)
                }
                for arg in skill.get_argument()
            ]
        }

                                                         
        if hasattr(skill, "definition"):
            info["definition"] = skill.definition
            high.append(info)
        else:
            low.append(info)

    return high, low
