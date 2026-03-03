from talk_to_fly.llm.controller import get_minispec
from talk_to_fly.dsl.minispec import run_minispec
from talk_to_fly.logging.logger import log_status, log_verbose, log_trace
from talk_to_fly.core.bootstrap import setup_environment
from talk_to_fly.io.speech_input import prompt_user_for_task

#def prompt_user_for_task():
#    return input("\n\033[1;36mEnter UAV task :> \033[0m").strip()

def handle_exit(drone, args):
    if drone.vehicle.armed and not args.simulation:
        choice = input("Drone is armed. Auto-land before exit? [Y/N]: ").lower()
        if choice == "y":
            log_status("[EXIT] Auto-landing...")
            drone.land()
            return True
        else:
            log_status("[CANCEL] Exit aborted.")
            return False
    return True

def main_loop(drone, args):
    while True:
        task = prompt_user_for_task(voice=args.voice, stt=args.stt)
        print("")
        log_trace(f"[TASK] :>{task}")

        if not task:
            log_verbose("[WARN] Empty task ignored.")
            continue

                                
        if task in (":land", ":l"):
            drone.land()
            continue

        if task in (":rtl", ":r"):
            drone.rtl()
            continue

        if task in (":pos", ":status"):
            log_status(drone.get_status())
            continue

                      
        if task.lower() in ("quit", "exit"):
            if handle_exit(drone, args):
                break
            else:
                continue

                          
        minispec = get_minispec(task, drone)

        if not minispec:
            log_status("[ERROR] No MiniSpec received. Skipping.")
            continue

        if args.confirm:
            execute = input("\n\033[1;36mExecute this plan? [Y/N] \033[0m").strip().lower()
            if execute != 'y':
                log_status("[VERIFY] Execution cancelled.")
                continue

                           
        try:
            run_minispec(minispec, drone)
        except Exception as e:
            log_status(f"[ERROR] MiniSpec execution failed: {e}")
                          
            continue

def main(argv=None) -> int:
    args, drone, gps_logger = setup_environment(argv)
    try:
        main_loop(drone, args)
    except KeyboardInterrupt:
        log_status("[ABORT] Ctrl-C detected. Landing...")
        try: drone.land()
        except: pass
    finally:
        gps_logger.stop()
        drone.close()
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
