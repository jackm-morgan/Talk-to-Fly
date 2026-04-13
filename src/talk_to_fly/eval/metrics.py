"""Evaluation metrics and movement-detection utilities."""


from __future__ import annotations

import time
import threading
from dataclasses import dataclass
from typing import Optional, Tuple

from dronekit import Vehicle

from talk_to_fly.eval.telemetry import displacement_m


@dataclass
class MovementResult:
    ttfm_ms: Optional[float]
    method: str  # groundspeed|displacement|altitude|yaw|none


class MovementDetector:
    """Detect the first physical UAV action after task submission.

    This is intentionally broader than horizontal movement:
    - horizontal motion (groundspeed / horizontal displacement)
    - vertical motion (altitude change)
    - yaw motion (heading change)

    It does NOT trigger on arm/mode changes alone. Those delays still count because
    the timer starts before planning/execution, but they are not themselves the endpoint.
    """

    def __init__(
        self,
        vehicle: Vehicle,
        start_latlon: Tuple[Optional[float], Optional[float]],
        start_alt_m: Optional[float],
        start_heading_deg: Optional[float],
        speed_thresh_mps: float = 0.10,
        disp_thresh_m: float = 0.15,
        alt_thresh_m: float = 0.10,
        yaw_thresh_deg: float = 5.0,
        hold_s: float = 0.20,
        poll_hz: float = 10.0,
    ):
        self.vehicle = vehicle
        self.start_latlon = start_latlon
        self.start_alt_m = start_alt_m
        self.start_heading_deg = start_heading_deg
        self.speed_thresh_mps = speed_thresh_mps
        self.disp_thresh_m = disp_thresh_m
        self.alt_thresh_m = alt_thresh_m
        self.yaw_thresh_deg = yaw_thresh_deg
        self.hold_s = hold_s
        self.poll_hz = poll_hz

        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._t0: Optional[float] = None
        self._ttfm_s: Optional[float] = None
        self._method: str = "none"

    def start(self, start_time_s: Optional[float] = None):
        self._t0 = float(start_time_s) if start_time_s is not None else time.time()
        self._thread.start()

    def stop(self) -> MovementResult:
        self._stop.set()
        self._thread.join(timeout=2.0)
        if self._ttfm_s is None:
            return MovementResult(ttfm_ms=None, method="none")
        return MovementResult(ttfm_ms=1000.0 * self._ttfm_s, method=self._method)

    @staticmethod
    def _unwrap_heading_delta_deg(start_deg: float, cur_deg: float) -> float:
        return abs((cur_deg - start_deg + 180.0) % 360.0 - 180.0)

    def _run(self):
        period = 1.0 / max(self.poll_hz, 1e-3)
        speed_hold = 0.0
        disp_hold = 0.0
        alt_hold = 0.0
        yaw_hold = 0.0

        while not self._stop.is_set() and self._ttfm_s is None:
            now = time.time()
            t0 = self._t0 or now
            dt = period

            # Horizontal speed
            gs = getattr(self.vehicle, "groundspeed", None)
            try:
                gs = float(gs) if gs is not None else None
            except Exception:
                gs = None
            speed_hold = speed_hold + dt if (gs is not None and gs > self.speed_thresh_mps) else 0.0

            # Horizontal displacement
            loc = getattr(self.vehicle, "location", None)
            grf = getattr(loc, "global_relative_frame", None) if loc else None
            lat = getattr(grf, "lat", None) if grf else None
            lon = getattr(grf, "lon", None) if grf else None
            alt = getattr(grf, "alt", None) if grf else None
            try:
                lat = float(lat) if lat is not None else None
                lon = float(lon) if lon is not None else None
                alt = float(alt) if alt is not None else None
            except Exception:
                lat, lon, alt = None, None, None

            disp = displacement_m(self.start_latlon, (lat, lon))
            disp_hold = disp_hold + dt if (disp is not None and disp > self.disp_thresh_m) else 0.0

            # Vertical motion
            alt_delta = None
            if self.start_alt_m is not None and alt is not None:
                alt_delta = abs(alt - self.start_alt_m)
            alt_hold = alt_hold + dt if (alt_delta is not None and alt_delta > self.alt_thresh_m) else 0.0

            # Yaw motion
            heading = getattr(self.vehicle, "heading", None)
            try:
                heading = float(heading) if heading is not None else None
            except Exception:
                heading = None
            yaw_delta = None
            if self.start_heading_deg is not None and heading is not None:
                yaw_delta = self._unwrap_heading_delta_deg(self.start_heading_deg, heading)
            yaw_hold = yaw_hold + dt if (yaw_delta is not None and yaw_delta > self.yaw_thresh_deg) else 0.0

            if alt_hold >= self.hold_s:
                self._ttfm_s = now - t0
                self._method = "altitude"
                break
            if yaw_hold >= self.hold_s:
                self._ttfm_s = now - t0
                self._method = "yaw"
                break
            if speed_hold >= self.hold_s:
                self._ttfm_s = now - t0
                self._method = "groundspeed"
                break
            if disp_hold >= self.hold_s:
                self._ttfm_s = now - t0
                self._method = "displacement"
                break

            time.sleep(period)
