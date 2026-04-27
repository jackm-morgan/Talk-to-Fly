"""Command-line interface for the evaluation toolkit."""

import argparse
from talk_to_fly.eval.runner import run_suite


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="talk_to_fly.eval", description="Talk-to-Fly evaluation runner (SITL/real).")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="Run an evaluation suite (batch episodes).")
    r.add_argument("--suite", required=True, help="Path to suite YAML/JSON.")
    r.add_argument("--connect", default="udp:127.0.0.1:14550", help="MAVLink connection string.")
    r.add_argument("--simulation", action="store_true", help="Assume simulation mode (SITL).")
    r.add_argument("--runs", type=int, default=1, help="Repeat entire suite N times.")
    r.add_argument("--shuffle", action="store_true", help="Shuffle episode order each run.")
    r.add_argument("--category", default="all", choices=["all", "simple", "compound", "complex"],
                   help="Filter by category.")
    r.add_argument("--out-dir", default="eval_runs", help="Root output directory.")
    r.add_argument("--name", default=None, help="Optional run name override (folder name suffix).")

    # LLM controls
    r.add_argument("--llm", default="live", choices=["live", "record", "replay"],
                   help="LLM mode: live (no cache), record (write cache), replay (use cache only).")
    r.add_argument("--cache", default=None, help="Path to LLM cache JSONL (default: <run>/cache/llm_cache.jsonl).")

    # Agentic / ablation controls
    r.add_argument(
        "--ablation",
        default="full",
        choices=["full", "stateless", "no_replanning", "open_loop", "one_shot"],
        help=(
            "Execution mode: full=stateful closed-loop baseline; "
            "stateless=remove mission/state/history from planner input; "
            "no_replanning=disable recovery replans; "
            "open_loop=execute planned steps without mission feedback/replanning; "
            "one_shot=single upfront plan with no runtime context, no clarification/replanning, and no execution-grounded recovery."
        ),
    )
    r.add_argument("--max-replans", type=int, default=2,
                   help="Maximum recovery replans for full/stateless modes.")

    # Timeouts / thresholds
    r.add_argument("--timeout-s", type=float, default=None, help="Override per-episode timeout seconds.")
    r.add_argument("--settle-s", type=float, default=None, help="Override per-episode settle seconds.")
    r.add_argument("--movement-speed-mps", type=float, default=0.10, help="First-motion threshold when already armed (groundspeed, m/s).")
    r.add_argument("--movement-disp-m", type=float, default=0.15, help="First-motion threshold when already armed (horizontal displacement, meters).")
    r.add_argument("--movement-alt-m", type=float, default=0.10, help="First-motion threshold when already armed (altitude change, meters).")
    r.add_argument("--movement-yaw-deg", type=float, default=5.0, help="First-motion threshold when already armed (heading change, degrees).")
    r.add_argument("--movement-hold-s", type=float, default=0.20, help="Hold time for physical-motion thresholds; arming is counted immediately if the episode starts disarmed.")

    # Verbosity
    r.add_argument("-v", "--verbose", action="store_true", help="Verbose console logging (reuses existing logger).")

    return p


def main(argv=None) -> int:
    p = build_parser()
    args = p.parse_args(argv)

    if args.cmd == "run":
        run_suite(args)
        return 0

    return 2
