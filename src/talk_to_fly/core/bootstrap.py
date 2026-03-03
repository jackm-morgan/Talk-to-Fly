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

    # ---- Voice / STT (optional) ----
    parser.add_argument("--voice", action="store_true", help="Use microphone speech input instead of typing")
    parser.add_argument("--stt-model", default="small.en", help="faster-whisper model name (e.g., tiny.en, small.en)")
    parser.add_argument("--stt-device", default="cpu", help="Whisper device: cpu or cuda")
    parser.add_argument("--stt-compute-type", default="int8", help="Compute type: int8 (CPU), float16 (GPU), etc.")
    parser.add_argument("--stt-record-s", type=float, default=5.0, help="Push-to-talk recording window length (seconds)")
    parser.add_argument("--stt-sample-rate", type=int, default=16000, help="Audio sample rate (Hz)")

    return parser.parse_args(argv)

def setup_environment(argv=None):
    args = parse_args(argv)
    set_verbose(args.verbose)

    # Initialize optional STT once; keep return signature unchanged by storing on args
    args.stt = None
    if getattr(args, "voice", False):
        try:
            from talk_to_fly.io.speech_input import SpeechConfig, SpeechRecognizer
        except Exception as e:
            log_status(
                "[INIT] Voice input requested but speech dependencies are missing.\n"
                "Install: poetry install -E voice\n"
                "System (Arch): sudo pacman -S portaudio"
            )
            raise

        cfg = SpeechConfig(
            model_name=args.stt_model,
            device=args.stt_device,
            compute_type=args.stt_compute_type,
            sample_rate=args.stt_sample_rate,
            record_s=args.stt_record_s,
        )
        args.stt = SpeechRecognizer(cfg)
        log_verbose(
            f"[INIT] Speech recognizer initialized "
            f"(model={args.stt_model}, device={args.stt_device}, compute={args.stt_compute_type}, "
            f"record_s={args.stt_record_s}, sr={args.stt_sample_rate})"
        )

    sim_status = "\033[1;32mON\033[0m" if args.simulation else "\033[1;31mOFF\033[0m"
    verify_status = "\033[1;32mON\033[0m" if args.confirm else "\033[1;31mOFF\033[0m"
    verbose_status = "\033[1;32mON\033[0m" if args.verbose else "\033[1;31mOFF\033[0m"
    voice_status = "\033[1;32mON\033[0m" if getattr(args, "voice", False) else "\033[1;31mOFF\033[0m"

    msg = (
        f"Modes: Simulation={sim_status}, "
        f"Verify={verify_status}, "
        f"Verbose={verbose_status}, "
        f"Voice={voice_status}"
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
