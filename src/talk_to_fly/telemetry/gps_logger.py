import csv
import threading
import time
from datetime import datetime
from dronekit import Vehicle
from talk_to_fly.logging.logger import get_log_filename, log_trace

class GPSLogger:
    def __init__(self, vehicle, filename=None):
        self.vehicle: Vehicle = vehicle
        self.filename = f"{get_log_filename()[:-4]}_tra.csv"
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

                                
        with open(self.filename, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["time_s", "lat", "lon", "alt_m"])

    def start(self):
        log_trace(f"[GPS LOG] Logging to {self.filename}")
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._thread.join()

    def _run(self):
        while not self._stop.is_set():
            loc = self.vehicle.location.global_relative_frame
            if loc.lat is not None:
                with open(self.filename, "a", newline="") as f:
                    writer = csv.writer(f)
                    writer.writerow([
                        time.time(),
                        loc.lat,
                        loc.lon,
                        loc.alt
                    ])
            time.sleep(0.1)                                                   

