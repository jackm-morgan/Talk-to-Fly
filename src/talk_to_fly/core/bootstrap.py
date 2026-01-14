import argparse
import logging
from talk_to_fly.uav.mavlink_wrapper import MavlinkWrapper
from talk_to_fly.logging.logger import (
    set_verbose,
    logger,
    file_handler,
    log_status,
    log_verbose,
    log_trace
)
from talk_to_fly.telemetry.gps_logger import GPSLogger
from openai import OpenAI
from dotenv import load_dotenv
import os

def ping_openai(timeout: float = 5.0) -> bool:
    """
    Simple check to see if the OpenAI API is reachable and the key is valid.
    Returns True if successful, False otherwise.
    """
    load_dotenv()
    apikey = os.getenv("OPENAI_API_KEY")
    if not apikey:
        log_verbose("[INIT] OPENAI_API_KEY not found in environment.")
        return False

    client = OpenAI(api_key=apikey)

    try:
                                                              
        log_verbose("[INIT] Checking OpenAI API connectivity...")
        client.models.list()                    
        log_status("[INIT] OpenAI API reachable!")
        return True
    except Exception as e:
        log_status(f"[INIT] OpenAI API connection failed: {e}")
        return False


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="LLM-controlled UAV")
    parser.add_argument("--connect", default="udp:127.0.0.1:14551", help="MAVLink connection string")
    parser.add_argument("-k", "--confirm", action="store_true", help="Show MiniSpec plan and ask for confirmation")
    parser.add_argument("-s", "--simulation", action="store_true", help="Run in simulation mode")
    parser.add_argument("-v", "--verbose", action="store_true", help="Print log messages to console")
    return parser.parse_args(argv)

def setup_environment(argv=None):
    args = parse_args(argv)
    set_verbose(args.verbose)

                                                          
    sim_status = "\033[1;32mON\033[0m" if args.simulation else "\033[1;31mOFF\033[0m"
    verify_status = "\033[1;32mON\033[0m" if args.confirm else "\033[1;31mOFF\033[0m"
    verbose_status = "\033[1;32mON\033[0m" if args.verbose else "\033[1;31mOFF\033[0m"
    msg = (
        f"Modes: Simulation={sim_status}, "
        f"Verify={verify_status}, "
        f"Verbose={verbose_status}"
    )

    print(f"\n{msg}\n")
    log_trace(f"[INIT] {msg}")

                                                            
    if not ping_openai():
        exit(1)

    if args.simulation:
        args.connect = "udp:127.0.0.1:14550"

                      
    drone = MavlinkWrapper(args.connect, simulation=args.simulation)
    log_verbose(f"[INIT] Drone initialized on {args.connect} (Simulation={args.simulation})")

                                                         
    gps_logger = GPSLogger(drone.vehicle)                                                        
    gps_logger.start()
    log_verbose(f"[INIT] GPS trajectory logging started: {gps_logger.filename}")

                                                             
    autopilot_logger = logging.getLogger("autopilot")
    autopilot_logger.setLevel(logging.DEBUG)
    autopilot_logger.handlers.clear()
    autopilot_logger.propagate = False

                                                       
    ap_console_warn = logging.StreamHandler()
    ap_console_warn.setLevel(logging.WARNING)
    ap_console_warn.setFormatter(file_handler.formatter)

    ap_console_info = logging.StreamHandler()
    ap_console_info.setLevel(logging.INFO)
    ap_console_info.setFormatter(file_handler.formatter)

                                   
    class AutopilotFilter(logging.Filter):
        def filter(self, record):
            record.msg = f"[AUTOPILOT] {record.msg}"
            if record.levelno == logging.INFO:
                return args.verbose
            return True

    ap_console_warn.addFilter(AutopilotFilter())
    ap_console_info.addFilter(AutopilotFilter())

                               
    autopilot_logger.addHandler(file_handler)
    autopilot_logger.addHandler(ap_console_warn)
    if args.verbose:
        autopilot_logger.addHandler(ap_console_info)

    log_verbose("[INIT] Autopilot logger hooked into uav_logger")

                                                                          
    return args, drone, gps_logger
