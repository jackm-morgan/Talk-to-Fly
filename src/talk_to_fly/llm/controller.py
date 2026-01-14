                   
from openai import OpenAI
from dotenv import load_dotenv
import os
import threading
import sys
import time
from talk_to_fly.logging.logger import log_trace, log_verbose, log_status
from talk_to_fly.skillset import skillset_to_prompt_json
import math
from importlib.resources import files as _res_files


                           
                           
                           
def _spinner_task(stop_event):
    """Display a spinner in the terminal while stop_event is not set."""
    spinner = "|/-\\"
    idx = 0
    while not stop_event.is_set():
        sys.stdout.write(f"\r[LLM] Thinking... {spinner[idx % len(spinner)]}")
        sys.stdout.flush()
        idx += 1
        time.sleep(0.1)
    sys.stdout.write("\r" + " " * 40 + "\r")              


                           
                         
                           
def get_minispec(task_description, drone):
    """
    Generates a MiniSpec plan for a given task using GPT-5.1.
    Displays a spinner while waiting for response.
    """
    load_dotenv()                

                               
                               
                               
        def create_prompt():
        assets = _res_files("talk_to_fly.assets")
        prompt_plan = assets.joinpath("prompt_plan.txt").read_text(encoding="utf-8")
        minispec_syntax = assets.joinpath("minispec_syntax.txt").read_text(encoding="utf-8")
        guides = assets.joinpath("guides.txt").read_text(encoding="utf-8")
        plan_examples = ""
        try:
            plan_examples = assets.joinpath("plan_examples.txt").read_text(encoding="utf-8")
        except FileNotFoundError:
            pass

        high, low = skillset_to_prompt_json(drone.skills)

        prompt = prompt_plan.format(
            high_level_skills=high,
            low_level_skills=low,
            minispec_syntax=minispec_syntax,
            guides=guides,
            plan_examples=plan_examples,
            task_description=task_description,
            execution_history=drone.hist,
            drone_status=drone.get_status()
        )
        return prompt

    apikey = os.getenv("OPENAI_API_KEY")
    if not apikey:
        raise ValueError("OPENAI_API_KEY not set in environment.")
    client = OpenAI(api_key=apikey)

    prompt = create_prompt()
    log_trace(f"[LLM API] Prompt: {prompt}")
    log_verbose("[MINISPEC] Generating MiniSpec...")

                               
                                 
                               
    stop_event = threading.Event()
    spinner_thread = threading.Thread(target=_spinner_task, args=(stop_event,), daemon=True)
    spinner_thread.start()

    try:
        response = client.chat.completions.create(
            model="gpt-5.2",
            messages=[{"role": "user", "content": prompt}]
        )
    finally:
                                     
        stop_event.set()
        spinner_thread.join()

                               
                      
                               
    minispec = response.choices[0].message.content.strip()
    log_trace(f"[LLM] GPT Response raw: {response}")
    log_verbose(f"[MINISPEC] Generated Plan: {minispec}")
    log_verbose(f"[MINISPEC] Flight Plan Ready")

                    
    print(f"\n\033[1;32mFlight Plan: {minispec}\033[0m\n")
    return minispec


