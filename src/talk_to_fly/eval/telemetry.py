"""Telemetry recording utilities used during evaluation."""

from __future__ import annotations

import csv
import time
import math
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple

from dronekit import Vehicle


@dataclass
class TelemetrySample:
    t_s: float
    lat: Optional[float]
    lon: Optional[float]
    alt_m: Optional[float]
    groundspeed_mps: Optional[float]
    heading_deg: Optional[float]
    armed: Optional[bool]
    mode: Optional[str]


def _safe_float(x):
    try:
        if x is None:
            return None
        return float(x)
    except Exception:
        return None


def _safe_str(x):
    try:
        if x is None:
            return None
        return str(x)
    except Exception:
        return None


class TelemetryRecorder:
    """Polls vehicle telemetry at fixed rate and writes to CSV.

    CSV columns are stable and designed for post-hoc scoring (TTFM, completion).
    """

    def __init__(self, vehicle: Vehicle, out_csv: Path, rate_hz: float = 10.0):
        self.vehicle = vehicle
        self.out_csv = Path(out_csv)
        self.rate_hz = rate_hz
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._t0 = None

        self.out_csv.parent.mkdir(parents=True, exist_ok=True)
        with self.out_csv.open("w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["t_rel_s", "epoch_s", "lat", "lon", "alt_m", "groundspeed_mps", "heading_deg", "armed", "mode"])

    def start(self):
        self._t0 = time.time()
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._thread.join(timeout=2.0)

    def _sample(self) -> TelemetrySample:
        now = time.time()
        loc = getattr(self.vehicle, "location", None)
        grf = getattr(loc, "global_relative_frame", None) if loc else None
        lat = _safe_float(getattr(grf, "lat", None)) if grf else None
        lon = _safe_float(getattr(grf, "lon", None)) if grf else None
        alt = _safe_float(getattr(grf, "alt", None)) if grf else None

        groundspeed = _safe_float(getattr(self.vehicle, "groundspeed", None))
        heading = _safe_float(getattr(self.vehicle, "heading", None))
        armed = getattr(self.vehicle, "armed", None)
        try:
            armed = bool(armed) if armed is not None else None
        except Exception:
            armed = None
        mode = _safe_str(getattr(getattr(self.vehicle, "mode", None), "name", None))
        return TelemetrySample(
            t_s=now,
            lat=lat, lon=lon, alt_m=alt,
            groundspeed_mps=groundspeed,
            heading_deg=heading,
            armed=armed,
            mode=mode,
        )

    def _run(self):
        period = 1.0 / max(self.rate_hz, 1e-3)
        while not self._stop.is_set():
            s = self._sample()
            t0 = self._t0 or s.t_s
            with self.out_csv.open("a", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow([
                    s.t_s - t0,
                    s.t_s,
                    s.lat, s.lon, s.alt_m,
                    s.groundspeed_mps,
                    s.heading_deg,
                    s.armed,
                    s.mode,
                ])
            time.sleep(period)


def haversine_m(lat1, lon1, lat2, lon2) -> float:
    """Great-circle distance in meters."""
    R = 6371000.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def displacement_m(start: Tuple[Optional[float], Optional[float]], cur: Tuple[Optional[float], Optional[float]]) -> Optional[float]:
    slat, slon = start
    clat, clon = cur
    if slat is None or slon is None or clat is None or clon is None:
        return None
    return haversine_m(slat, slon, clat, clon)
