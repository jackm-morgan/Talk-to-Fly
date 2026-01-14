                         
from talk_to_fly.logging.logger import log_verbose, log_status


def run_minispec(minispec_code, drone, vars=None):
    """
    Interpret and execute MiniSpec commands using the drone skillset.
    Fully supports:
    - LLM-generated l('...') logging strings with nested parentheses, semicolons, etc.
    - Loops N{ ... } including nested loops
    - Conditionals ?cond{ ... }
    - Variable assignments _1 = 5
    - Normal skill calls
    """

    if vars is None:
        vars = {}

    skillset = drone.skills                     

                                                            
                                        
                                                            
    def is_guided():
        mode = drone.vehicle.mode.name
        if mode == "GUIDED" or drone.is_simulation:
            return True
        log_verbose(f"[MINISPEC][BLOCKED] Cannot execute: vehicle not in GUIDED mode (current: {mode})")
        return False

                                                            
                                     
                                                            
    def execute_skill(name, arg_string):
        if name not in ("l", "log") and not is_guided():
            return

        skill = skillset.get_skill(name)
        if skill is None:
            log_verbose(f"[MiniSpec] Unknown command: {name}({arg_string}) - Skipping")
            return

        args_list = []

        if name in ("l", "log"):
                                                         
            args_list = [arg_string]
        elif arg_string and arg_string.strip():
            raw_args = [a.strip() for a in arg_string.split(",")]
            skill_args = skill.get_argument()
            if len(raw_args) != len(skill_args):
                log_verbose(f"[MiniSpec] Incorrect number of arguments for {name}. Expected {len(skill_args)}, got {len(raw_args)}")
                return
            for raw, spec in zip(raw_args, skill_args):
                if raw.startswith("_") and raw in vars:
                    args_list.append(vars[raw])
                    continue
                t = spec.arg_type
                try:
                    if t == str:
                        args_list.append(raw.strip("'\""))
                    else:
                        args_list.append(t(raw))
                except Exception as e:
                    log_verbose(f"[MiniSpec] Argument type error for {name}: {e}")
                    return

        result = skill.execute(args_list)
        if isinstance(result, str):
            run_minispec(result, drone, vars)

                                                            
                                                                 
                                                            
    def extract_l_command(code, start_idx):
        depth = 0
        for i in range(start_idx, len(code)):
            if code[i] == '(':
                depth += 1
            elif code[i] == ')':
                depth -= 1
                if depth == 0:
                    return code[start_idx + 2:i].strip(), i + 1
        return None, start_idx                         

                                                            
                                                         
                                                            
    def extract_loop(code, start_idx):
        i = start_idx
        count_str = ''
        while i < len(code) and code[i].isdigit():
            count_str += code[i]
            i += 1
        if not count_str or i >= len(code) or code[i] != '{':
            return None, None, start_idx
        loop_count = int(count_str)
        i += 1
        depth = 1
        body_start = i
        while i < len(code):
            if code[i] == '{':
                depth += 1
            elif code[i] == '}':
                depth -= 1
                if depth == 0:
                    body = code[body_start:i]
                    return loop_count, body, i + 1
            i += 1
        return None, None, start_idx                    

                                                            
                           
                                                            
    idx = 0
    while idx < len(minispec_code):
                         
        while idx < len(minispec_code) and minispec_code[idx].isspace():
            idx += 1
        if idx >= len(minispec_code):
            break

                                       
        if minispec_code[idx:idx+2] == "l(":
            content, next_idx = extract_l_command(minispec_code, idx)
            if content is not None:
                execute_skill("l", content)
                idx = next_idx
                continue
            else:
                log_verbose(f"[MiniSpec] Warning: unmatched parentheses in l() at index {idx}")
                break

                                     
        if minispec_code[idx].isdigit():
            loop_count, body, next_idx = extract_loop(minispec_code, idx)
            if loop_count is not None:
                for _ in range(loop_count):
                    run_minispec(body, drone, vars)
                idx = next_idx
                continue

                                                         
        semicolon_idx = minispec_code.find(";", idx)
        if semicolon_idx == -1:
            semicolon_idx = len(minispec_code)
        cmd = minispec_code[idx:semicolon_idx].strip()
        idx = semicolon_idx + 1
        if not cmd:
            continue

                                                
        if cmd.startswith("?") and "{" in cmd and cmd.endswith("}"):
            try:
                cond_expr = cmd[1:cmd.find("{")].strip()
                cond_body = cmd[cmd.find("{")+1:-1]
                for key, val in vars.items():
                    cond_expr = cond_expr.replace(key, str(val))
                if eval(cond_expr):
                    run_minispec(cond_body, drone, vars)
                continue
            except Exception as e:
                log_verbose(f"[MiniSpec] Conditional parsing error: {e}")
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
                log_verbose(f"[MiniSpec] Warning: invalid assignment: {cmd}")
            continue

                                   
        if "(" in cmd and cmd.endswith(")"):
            name = cmd[:cmd.find("(")]
            arg = cmd[cmd.find("(")+1:-1]
        else:
            name = cmd
            arg = None
        execute_skill(name, arg)

                                     
                    
                                         
    log_status("[MINISPEC] Commands Executed")

