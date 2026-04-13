"""MAVLink wrapper for DroneKit-backed vehicles.

The wrapper centralizes vehicle connection management, safety checks,
telemetry access, movement primitives, and skill registration.
"""

from dronekit import connect, VehicleMode, LocationGlobalRelative
from pymavlink import mavutil
import time
import math
import threading
from typing import Optional, Tuple, Dict, Any, List

from talk_to_fly.logging.logger import log_status, log_verbose, log_trace
from talk_to_fly.skillset import SkillSet, create_low_level_skillset, create_high_level_skillset


# ============================================================
# Timing limits and execution thresholds
# ============================================================
ARM_TIMEOUT = 20
DISARM_TIMEOUT = 20
MODE_CHANGE_TIMEOUT = 7
TAKEOFF_TIMEOUT = 45
MOVE_TIMEOUT_PER_M = 5
ROTATE_TIMEOUT_PER_360 = 120
ASCEND_TIMEOUT = 30
HOVER_REASSERT_INTERVAL = 2.0

GPS_ALT_TOLERANCE = 0.2
POSITION_TOLERANCE_M = 0.8
MOVE_CLOSE_ENOUGH_M = 1.2
MOVE_STALL_WINDOW_S = 2.5
MOVE_PROGRESS_EPS_M = 0.15
MOVE_STALL_SPEED_MPS = 0.12
MOVE_REISSUE_AFTER_S = 1.5
MOVE_MAX_REISSUES = 2
FINAL_NUDGE_START_M = 1.5
FINAL_NUDGE_SPEED_MPS = 0.35
FINAL_NUDGE_TIMEOUT_S = 4.0

MAX_ALTITUDE_M = 30.0
MAX_SPEED_MPS = 2.0
LOW_BATTERY_THRESHOLD = 10.0
HEARTBEAT_TIMEOUT = 5.0

# Rotation-control thresholds
ROTATE_MIN_SPEED_DEG_S = 8.0
ROTATE_MIN_TOL_DEG = 2.0
ROTATE_MAX_TOL_DEG = 5.0
ROTATE_SETTLE_S = 0.0 # 0.25
ROTATE_REISSUE_AFTER_S = 2.0
ROTATE_REQUIRED_CONSECUTIVE = 1 # 3


class MavlinkWrapper:
    def __init__(self, connection_str: str, simulation: bool):
        log_status(f"[INIT] Connecting to vehicle via {connection_str}...")
        self.vehicle = connect(connection_str, wait_ready=True, timeout=60)
        log_status("[INIT] Connected to vehicle!")
        self.is_simulation = simulation
        self.hist: List[Dict[str, Any]] = []

        self.original_heading: Optional[float] = None
        self.home_hov_location: Optional[LocationGlobalRelative] = None

        self._busy_lock = threading.Lock()
        self._watchdog_lock = threading.Lock()
        self._last_heartbeat_time = time.time()

        low = create_low_level_skillset(self)
        high = create_high_level_skillset(low)
        all_skills = SkillSet("both")
        all_skills.skills.update(low.skills)
        all_skills.skills.update(high.skills)
        self.skills = all_skills

        self.vehicle.add_attribute_listener("last_heartbeat", self._default_heartbeat_handler)

        self._watchdog_running = True
        self._watchdog_thread = threading.Thread(target=self._background_watchdog, daemon=True)
        self._watchdog_thread.start()

    # ============================================================
    # Watchdog and connection lifecycle
    # ============================================================
    def _update_heartbeat(self):
        with self._watchdog_lock:
            self._last_heartbeat_time = time.time()

    def _default_heartbeat_handler(self, vehicle, attr_name, value):
        self._update_heartbeat()

    def _background_watchdog(self):
        while self._watchdog_running:
            try:
                with self._watchdog_lock:
                    delta = time.time() - self._last_heartbeat_time
                if delta > HEARTBEAT_TIMEOUT and not self.is_simulation:
                    log_verbose(f"[WATCHDOG] No heartbeat for {delta:.1f}s")

                batt = self._safe_battery()
                if batt is not None and batt <= LOW_BATTERY_THRESHOLD:
                    log_verbose(f"[WATCHDOG] Low battery level: {batt}%")

                time.sleep(2.0)
            except Exception as e:
                log_verbose(f"[WATCHDOG] Exception: {e}")
                time.sleep(2.0)

    def close(self):
        log_status("[CLOSE] Stopping watchdog and closing vehicle...")
        self._watchdog_running = False

        try:
            self._watchdog_thread.join(timeout=1.0)
        except Exception:
            pass

        try:
            self.vehicle.remove_attribute_listener("last_heartbeat", self._default_heartbeat_handler)
        except Exception:
            pass

        try:
            self.vehicle.close()
        except Exception as e:
            log_verbose(f"[CLOSE] vehicle.close() exception: {e}")

        log_status("[CLOSE] Vehicle connection closed.")
        return True

    # ============================================================
    # Safe telemetry helpers
    # ============================================================
    def _safe_get_alt(self) -> Optional[float]:
        loc = getattr(self.vehicle, "location", None)
        if not loc:
            return None
        grf = getattr(loc, "global_relative_frame", None)
        if not grf:
            return None
        return getattr(grf, "alt", None)

    def _safe_get_latlonalt(self) -> Tuple[Optional[float], Optional[float], Optional[float]]:
        loc = getattr(self.vehicle, "location", None)
        if not loc:
            return (None, None, None)
        grf = getattr(loc, "global_relative_frame", None)
        if not grf:
            return (None, None, None)
        return getattr(grf, "lat", None), getattr(grf, "lon", None), getattr(grf, "alt", None)

    def _wrap360(self, deg: float) -> float:
        return float(deg) % 360.0

    def _angdiff(self, target: float, current: float) -> float:
        return (float(target) - float(current) + 180.0) % 360.0 - 180.0

    def _target_sys_comp(self) -> Tuple[int, int]:
        try:
            return int(self.vehicle._master.target_system), int(self.vehicle._master.target_component)
        except Exception:
            return 0, 0

    def _safe_get_heading(self) -> Optional[float]:
        try:
            h = getattr(self.vehicle, "heading", None)
            if h is not None:
                h = float(h)
                if 0.0 <= h < 360.0:
                    return self._wrap360(h)
        except Exception:
            pass

        try:
            att = getattr(self.vehicle, "attitude", None)
            yaw = getattr(att, "yaw", None) if att is not None else None
            if yaw is not None:
                return self._wrap360(math.degrees(float(yaw)))
        except Exception:
            pass

        return None

    def _safe_battery(self) -> Optional[float]:
        batt = getattr(self.vehicle, "battery", None)
        if not batt:
            return None
        return getattr(batt, "level", None)

    def _safe_groundspeed(self) -> Optional[float]:
        try:
            vel = getattr(self.vehicle, "velocity", None)
            if vel is not None and len(vel) >= 2 and vel[0] is not None and vel[1] is not None:
                return math.hypot(float(vel[0]), float(vel[1]))
        except Exception:
            pass

        try:
            gs = getattr(self.vehicle, "groundspeed", None)
            if gs is not None:
                return float(gs)
        except Exception:
            pass

        return None

    # ============================================================
    # History / locking helpers
    # ============================================================
    def _record(self, cmd_name: str, args: dict = None):
        self.hist.append({"time": time.time(), "cmd": cmd_name, "args": args or {}})

    def _acquire_busy(self, timeout: float = 0.0) -> bool:
        return self._busy_lock.acquire(timeout=timeout) if timeout else self._busy_lock.acquire(blocking=False)

    def _release_busy(self):
        if self._busy_lock.locked():
            self._busy_lock.release()

    # ============================================================
    # General wait helpers
    # ============================================================
    def _wait_for_mode(self, mode_name: str, timeout: float = 7.0) -> bool:
        log_verbose(f"[MODE] Requesting mode {mode_name}...")
        start = time.time()
        self.vehicle.mode = VehicleMode(mode_name)

        while getattr(self.vehicle, "mode", None) is None or self.vehicle.mode.name != mode_name:
            if time.time() - start > timeout:
                log_verbose(f"[MODE] Timeout changing to {mode_name}")
                return False
            time.sleep(0.2)

        log_status(f"[MODE] Mode is now {mode_name}.")
        return True

    def _wait_for_altitude(self, target_alt: float, timeout: float = 45.0, tolerance: float = 0.2) -> bool:
        start = time.time()
        while True:
            alt = self._safe_get_alt()
            if alt is not None and alt >= target_alt - tolerance:
                return True
            if time.time() - start > timeout:
                log_verbose("[ALT] Altitude wait timeout")
                return False
            time.sleep(0.3)

    def _wait_for_disarm(self, timeout: float = 20.0) -> bool:
        start = time.time()
        while getattr(self.vehicle, "armed", None):
            if time.time() - start > timeout:
                log_verbose("[DISARM] Timeout waiting for disarm")
                return False
            time.sleep(0.2)
        return True

    # ============================================================
    # Core state-changing commands
    # ============================================================
    def arm(self) -> bool:
        batt = self._safe_battery()
        if batt is not None and batt <= LOW_BATTERY_THRESHOLD:
            log_verbose(f"[ARM][FAIL] Battery low ({batt}%)")
            return False

        if not self._wait_for_mode("GUIDED") and not self.is_simulation:
            return False

        self.vehicle.armed = True
        start = time.time()
        while not getattr(self.vehicle, "armed", False):
            if time.time() - start > ARM_TIMEOUT:
                log_verbose("[ARM][FAIL] Timeout")
                return False
            time.sleep(0.2)

        self.original_heading = self._safe_get_heading()
        self.home_hov_location = self.vehicle.location.global_relative_frame
        self._record("arm", {"heading": self.original_heading})
        log_status("[ARM] Armed successfully")
        return True

    def disarm(self) -> bool:
        self.vehicle.armed = False
        if not self._wait_for_disarm():
            return False
        self._record("disarm")
        log_status("[DISARM] Disarmed successfully")
        return True

    # ============================================================
    # Motion primitives and internal navigation helpers
    # ============================================================
    def _send_ned_velocity(self, vx: float, vy: float, vz: float):
        """
        Send a NED velocity. Negative vz is up in this call.
        This is a fire-and-forget message; continuous motion requires repeated sends.
        """
        msg = self.vehicle.message_factory.set_position_target_local_ned_encode(
            0, 0, 0,
            mavutil.mavlink.MAV_FRAME_LOCAL_NED,
            0b0000111111000111,
            0, 0, 0,
            vx, vy, vz,
            0, 0, 0,
            0, 0
        )
        self.vehicle.send_mavlink(msg)
        self.vehicle.flush()

    def _stop_motion(self):
        """Send repeated zero velocity commands to damp residual translation."""
        for _ in range(2):
            try:
                self._send_ned_velocity(0, 0, 0)
            except Exception:
                break
            time.sleep(0.05)

    def _location_offset(self, lat0: float, lon0: float, north_m: float, east_m: float) -> LocationGlobalRelative:
        earth_radius = 6378137.0
        target_lat = lat0 + (north_m / earth_radius) * (180.0 / math.pi)
        target_lon = lon0 + (east_m / (earth_radius * math.cos(math.radians(lat0)))) * (180.0 / math.pi)
        _, _, alt0 = self._safe_get_latlonalt()
        return LocationGlobalRelative(target_lat, target_lon, alt0)

    def _distance_to_target(self, target: LocationGlobalRelative) -> Tuple[Optional[float], Optional[float], Optional[float]]:
        """
        Returns north error, east error, remaining horizontal distance (meters)
        from current position to target.
        """
        loc = getattr(self.vehicle.location, "global_relative_frame", None)
        if loc is None:
            return None, None, None

        earth_radius = 6378137.0
        d_north = (target.lat - loc.lat) * (math.pi / 180.0) * earth_radius
        d_east = (target.lon - loc.lon) * (math.pi / 180.0) * earth_radius * math.cos(math.radians(loc.lat))
        remaining = math.hypot(d_north, d_east)
        return d_north, d_east, remaining

    def _issue_simple_goto(self, target: LocationGlobalRelative, groundspeed: float) -> bool:
        try:
            self.vehicle.groundspeed = float(groundspeed)
        except Exception:
            pass

        try:
            self.vehicle.simple_goto(target, groundspeed=float(groundspeed))
            return True
        except Exception as e:
            log_verbose(f"[GOTO_INTERNAL] simple_goto exception: {e}")
            return False

    def _nudge_to_target(
        self,
        target: LocationGlobalRelative,
        max_speed: float = FINAL_NUDGE_SPEED_MPS,
        timeout: float = FINAL_NUDGE_TIMEOUT_S,
        log_prefix: str = "[MOVE]",
    ) -> bool:
        """
        Close-range correction using LOCAL_NED velocity.
        This is only used for the final short segment when simple_goto has already
        brought the vehicle close but not quite inside tolerance.
        """
        start = time.monotonic()

        while (time.monotonic() - start) < timeout:
            d_north, d_east, remaining = self._distance_to_target(target)
            if remaining is None:
                log_verbose(f"{log_prefix} Final nudge aborted: location unavailable.")
                self._stop_motion()
                return False

            if remaining <= POSITION_TOLERANCE_M:
                self._stop_motion()
                return True

            if remaining < 1e-6:
                self._stop_motion()
                return True

            speed = min(max_speed, max(0.15, 0.5 * remaining))
            vx = speed * d_north / remaining
            vy = speed * d_east / remaining

            log_verbose(
                f"{log_prefix} Final nudge: remaining={remaining:.2f} m, "
                f"vx={vx:.2f} m/s, vy={vy:.2f} m/s"
            )

            self._send_ned_velocity(vx, vy, 0)
            time.sleep(0.2)

        self._stop_motion()

        _, _, remaining = self._distance_to_target(target)
        return remaining is not None and remaining <= MOVE_CLOSE_ENOUGH_M

    def _goto_target_location(
        self,
        target: LocationGlobalRelative,
        groundspeed: float = MAX_SPEED_MPS,
        max_timeout: Optional[float] = None,
        log_prefix: str = "[GOTO]",
    ) -> bool:
        """
        Robust absolute-target movement.

        Strategy:
        - Issue simple_goto once initially.
        - Monitor actual distance-to-target.
        - If progress stalls near the target, either accept as close-enough or
          apply a short velocity nudge toward the same absolute target.
        - Reissue simple_goto to the same target if progress stalls earlier.
        - Return False on real timeout / failure instead of claiming success.
        """
        d_north, d_east, initial_distance = self._distance_to_target(target)
        if initial_distance is None:
            log_verbose(f"{log_prefix} Position unavailable before move.")
            return False

        if max_timeout is None:
            max_timeout = max(8.0, MOVE_TIMEOUT_PER_M * max(initial_distance, 0.5) + 2.0)

        if initial_distance <= POSITION_TOLERANCE_M:
            log_verbose(f"{log_prefix} Already within tolerance ({initial_distance:.2f} m).")
            return True

        log_status(
            f"{log_prefix} Moving to lat={target.lat:.6f}, lon={target.lon:.6f}, "
            f"distance={initial_distance:.2f} m"
        )

        used_velocity_only = False
        if not self._issue_simple_goto(target, groundspeed=groundspeed):
            log_verbose(f"{log_prefix} simple_goto unavailable; using velocity fallback.")
            used_velocity_only = True

        start = time.monotonic()
        progress_samples: List[Tuple[float, float]] = []
        best_remaining = initial_distance
        last_improve_t = start
        last_reissue_t = start
        reissues = 0

        if used_velocity_only:
            # With no simple_goto available, go straight to velocity guidance.
            if self._nudge_to_target(
                target,
                max_speed=min(max(0.3, groundspeed), MAX_SPEED_MPS),
                timeout=max_timeout,
                log_prefix=log_prefix,
            ):
                log_verbose(f"{log_prefix} Reached target using velocity fallback.")
                return True

            log_verbose(f"{log_prefix} Velocity fallback timed out.")
            return False

        while True:
            now = time.monotonic()
            d_north, d_east, remaining = self._distance_to_target(target)
            if remaining is None:
                log_verbose(f"{log_prefix} Location unavailable while moving.")
                self._stop_motion()
                return False

            horizontal_speed = self._safe_groundspeed()
            speed_text = f"{horizontal_speed:.2f}" if horizontal_speed is not None else "n/a"
            log_verbose(f"{log_prefix} Remaining: {remaining:.2f} m, groundspeed: {speed_text} m/s")

            if remaining <= POSITION_TOLERANCE_M:
                log_verbose(f"{log_prefix} Reached target (remaining {remaining:.2f} m).")
                self._stop_motion()
                return True

            progress_samples.append((now, remaining))
            progress_samples = [(t, r) for (t, r) in progress_samples if (now - t) <= MOVE_STALL_WINDOW_S]

            if remaining < (best_remaining - MOVE_PROGRESS_EPS_M):
                best_remaining = remaining
                last_improve_t = now

            window_progress = 0.0
            if len(progress_samples) >= 2:
                window_progress = progress_samples[0][1] - progress_samples[-1][1]

            stalled = (
                len(progress_samples) >= 2
                and window_progress < MOVE_PROGRESS_EPS_M
                and horizontal_speed is not None
                and horizontal_speed <= MOVE_STALL_SPEED_MPS
            )

            if stalled and remaining <= MOVE_CLOSE_ENOUGH_M:
                log_verbose(
                    f"{log_prefix} Close enough and stationary "
                    f"(remaining {remaining:.2f} m, speed {horizontal_speed:.2f} m/s)."
                )
                self._stop_motion()
                return True

            if stalled and remaining <= FINAL_NUDGE_START_M:
                log_verbose(f"{log_prefix} Stalled near target; attempting final nudge.")
                nudged = self._nudge_to_target(target, log_prefix=log_prefix)
                d_north, d_east, post_nudge_remaining = self._distance_to_target(target)
                if nudged and post_nudge_remaining is not None:
                    log_verbose(f"{log_prefix} Final nudge complete; remaining {post_nudge_remaining:.2f} m.")
                    self._stop_motion()
                    return True
                last_improve_t = time.monotonic()
                progress_samples.clear()

            if stalled and reissues < MOVE_MAX_REISSUES and (now - last_reissue_t) >= MOVE_REISSUE_AFTER_S:
                reissues += 1
                last_reissue_t = now
                log_verbose(f"{log_prefix} Progress stalled; reissuing simple_goto ({reissues}/{MOVE_MAX_REISSUES}).")
                self._issue_simple_goto(target, groundspeed=groundspeed)
                progress_samples.clear()

            if (now - start) > max_timeout:
                if remaining <= MOVE_CLOSE_ENOUGH_M:
                    log_verbose(f"{log_prefix} Timed out but ended close to target ({remaining:.2f} m). Accepting.")
                    self._stop_motion()
                    return True

                log_verbose(
                    f"{log_prefix} Timeout ({max_timeout:.1f}s) while moving; "
                    f"remaining {remaining:.2f} m."
                )
                self._stop_motion()
                return False

            time.sleep(0.3)

    def takeoff(self, target_altitude: float) -> bool:
        """Arms and takes off to target_altitude (relative meters)."""
        log_status(f"[TAKEOFF] Arming and taking off to {target_altitude:.1f} m")
        if not self.arm():
            return False

        if not self._wait_for_mode("GUIDED"):
            log_verbose("[TAKEOFF] Failed to set GUIDED before takeoff")
            return False

        try:
            self.vehicle.simple_takeoff(target_altitude)
            if not self._wait_for_altitude(target_altitude, timeout=TAKEOFF_TIMEOUT):
                log_verbose("[TAKEOFF] Did not reach target altitude in time")
                return False

            self.home_hov_location = self.vehicle.location.global_relative_frame
            log_status("[TAKEOFF] Reached target altitude.")
            self._record("takeoff", {"alt": target_altitude})
            return True
        except Exception as e:
            log_verbose(f"[TAKEOFF] Exception: {e}")
            return False

    def hover(self, duration: float) -> bool:
        """
        Actively hold current position for a specified duration.
        Implementation: issue one simple_goto to current location, then re-assert periodically.
        """
        acquired = self._acquire_busy(timeout=0.5)
        if not acquired:
            log_verbose("[HOVER] Busy - cannot hover now.")
            return False

        try:
            log_status(f"[HOVER] Holding position for {duration:.1f}s")
            lat, lon, alt = self._safe_get_latlonalt()
            if alt is None:
                log_verbose("[HOVER] Altitude unavailable, aborting hover.")
                return False

            target = LocationGlobalRelative(lat, lon, alt)
            start = time.time()

            try:
                self.vehicle.simple_goto(target)
            except Exception:
                self._stop_motion()

            while time.time() - start < duration:
                try:
                    self.vehicle.simple_goto(target)
                except Exception:
                    self._stop_motion()

                pos = self.vehicle.location.global_relative_frame
                log_verbose(f"[HOVER] pos: lat={pos.lat:.6f}, lon={pos.lon:.6f}, alt={pos.alt:.2f}")
                time.sleep(HOVER_REASSERT_INTERVAL)

            log_status("[HOVER] Complete.")
            self._record("hover", {"duration": duration})
            return True
        finally:
            self._release_busy()

    def ascend(self, delta_altitude: float, max_altitude: float = MAX_ALTITUDE_M, climb_rate: float = 1.0) -> bool:
        """Ascend by delta_altitude (meters), respecting max_altitude."""
        acquired = self._acquire_busy(timeout=0.5)
        if not acquired:
            log_verbose("[ASCEND] Busy - cannot ascend now.")
            return False

        try:
            current_alt = self._safe_get_alt()
            if current_alt is None:
                log_verbose("[ASCEND] Current altitude unknown.")
                return False

            target_alt = min(current_alt + delta_altitude, max_altitude)
            log_status(f"[ASCEND] Ascend from {current_alt:.2f} to {target_alt:.2f} m (rate {climb_rate} m/s)")

            start = time.time()
            timeout = ASCEND_TIMEOUT + abs(target_alt - current_alt) * 2.0
            reached = False

            while True:
                alt = self._safe_get_alt()
                if alt is not None and alt >= target_alt - GPS_ALT_TOLERANCE:
                    log_verbose("[ASCEND] Target altitude reached.")
                    reached = True
                    break

                if time.time() - start > timeout:
                    log_verbose("[ASCEND] Timeout while ascending.")
                    break

                self._send_ned_velocity(0, 0, -min(climb_rate, 3.0))
                time.sleep(0.4)

            self._stop_motion()

            if reached:
                self._record("ascend", {"target_alt": target_alt})
                return True
            return False
        finally:
            self._release_busy()

    def descend(self, delta_altitude: float, min_altitude: float = 0.5, descend_rate: float = 1.0) -> bool:
        """Descend by delta_altitude (meters), respecting min_altitude."""
        acquired = self._acquire_busy(timeout=0.5)
        if not acquired:
            log_verbose("[DESCEND] Busy - cannot descend now.")
            return False

        try:
            current_alt = self._safe_get_alt()
            if current_alt is None:
                log_verbose("[DESCEND] Current altitude unknown.")
                return False

            target_alt = max(current_alt - delta_altitude, min_altitude)
            log_status(f"[DESCEND] Descend from {current_alt:.2f} to {target_alt:.2f} m")

            start = time.time()
            timeout = ASCEND_TIMEOUT + abs(current_alt - target_alt) * 2.0
            reached = False

            while True:
                alt = self._safe_get_alt()
                if alt is not None and alt <= target_alt + GPS_ALT_TOLERANCE:
                    log_verbose("[DESCEND] Target altitude reached.")
                    reached = True
                    break

                if time.time() - start > timeout:
                    log_verbose("[DESCEND] Timeout while descending.")
                    break

                self._send_ned_velocity(0, 0, min(descend_rate, 3.0))
                time.sleep(0.4)

            self._stop_motion()

            if reached:
                self._record("descend", {"target_alt": target_alt})
                return True
            return False
        finally:
            self._release_busy()

    def _move_direction(
        self,
        forward_m: float,
        groundspeed: float = MAX_SPEED_MPS,
        max_timeout: Optional[float] = None,
        log_prefix: str = "[MOVE]",
    ) -> bool:
        """
        Move forward/right relative to current heading by computing an absolute
        target and then navigating to that absolute location robustly.
        """
        acquired = self._acquire_busy(timeout=0.5)
        if not acquired:
            log_verbose(f"{log_prefix} Busy - cannot move now.")
            return False

        try:
            lat0, lon0, alt0 = self._safe_get_latlonalt()
            if lat0 is None or lon0 is None or alt0 is None:
                log_verbose(f"{log_prefix} Position unavailable.")
                return False

            heading_deg = self._safe_get_heading() or 0.0
            heading_rad = math.radians(heading_deg)

            d_north = forward_m * math.cos(heading_rad)
            d_east = forward_m * math.sin(heading_rad)

            earth_radius = 6378137.0
            target_lat = lat0 + (d_north / earth_radius) * (180.0 / math.pi)
            target_lon = lon0 + (d_east / (earth_radius * math.cos(math.radians(lat0)))) * (180.0 / math.pi)
            target_location = LocationGlobalRelative(target_lat, target_lon, alt0)

            ok = self._goto_target_location(
                target_location,
                groundspeed=groundspeed,
                max_timeout=max_timeout,
                log_prefix=log_prefix,
            )

            if ok:
                self._record("move", {"forward_m": forward_m})
                log_status(f"{log_prefix} Movement complete.")
            else:
                log_verbose(f"{log_prefix} Movement failed.")
            return ok
        finally:
            self._release_busy()

    def move_forward(self, distance_m: float) -> bool:
        return self._move_direction(distance_m, log_prefix="[MOVE_FORWARD]")

    def move_backward(self, distance_m: float) -> bool:
        if not self.rotate(180):
            return False

        try:
            move_ok = self._move_direction(distance_m, log_prefix="[MOVE_BACKWARD]")
        finally:
            restore_ok = self.rotate(180)
            if not restore_ok:
                log_verbose("[MOVE_BACKWARD] Failed to restore original heading.")

        return move_ok and restore_ok

    def move_right(self, distance_m: float) -> bool:
        if not self.turn_cw(90):
            return False

        try:
            move_ok = self._move_direction(distance_m, log_prefix="[MOVE_RIGHT]")
        finally:
            restore_ok = self.turn_ccw(90)
            if not restore_ok:
                log_verbose("[MOVE_RIGHT] Failed to restore original heading.")

        return move_ok and restore_ok

    def move_left(self, distance_m: float) -> bool:
        if not self.turn_ccw(90):
            return False

        try:
            move_ok = self._move_direction(distance_m, log_prefix="[MOVE_LEFT]")
        finally:
            restore_ok = self.turn_cw(90)
            if not restore_ok:
                log_verbose("[MOVE_LEFT] Failed to restore original heading.")

        return move_ok and restore_ok

    # ============================================================
    # Yaw / orientation
    # ============================================================
    def rotate(self, yaw_deg: float, relative: bool = True, speed_deg_s: float = 30.0, tolerance: float = 5.0) -> bool:
        """
        Rotate to a new bearing and verify convergence using live heading feedback.

        Returns True only when the vehicle is actually near the requested bearing.
        """
        acquired = self._acquire_busy(timeout=0.5)
        if not acquired:
            log_verbose("[ROTATE] Busy - cannot rotate now.")
            return False

        try:
            deg = float(yaw_deg)
            if abs(deg) < 1e-6:
                log_verbose("[ROTATE] Zero-degree rotation requested.")
                self._record("rotate", {"deg": deg, "note": "zero"})
                return True

            start_heading = self._safe_get_heading()
            if start_heading is None:
                log_verbose("[ROTATE] Heading unavailable before command.")
                self._record("rotate", {"deg": deg, "reason": "no_heading"})
                return False

            target = self._wrap360(start_heading + deg) if relative else self._wrap360(deg)
            requested_delta = self._angdiff(target, start_heading)
            abs_delta = abs(requested_delta)

            speed = max(float(speed_deg_s), ROTATE_MIN_SPEED_DEG_S)
            tol = min(
                ROTATE_MAX_TOL_DEG,
                max(ROTATE_MIN_TOL_DEG, float(tolerance), 0.18 * abs_delta if abs_delta < 25.0 else 0.0),
            )
            timeout = max(5.0, 2.0 + ROTATE_TIMEOUT_PER_360 * (abs_delta / 360.0))
            direction = 1 if requested_delta >= 0.0 else -1
            ts, tc = self._target_sys_comp()

            try:
                self._stop_motion()
                time.sleep(0.1)
            except Exception:
                pass

            def _send_yaw_target():
                msg = self.vehicle.message_factory.command_long_encode(
                    ts,
                    tc,
                    mavutil.mavlink.MAV_CMD_CONDITION_YAW,
                    0,
                    float(target),
                    float(speed),
                    0.0,
                    0,
                    0,
                    0,
                    0,
                )
                self.vehicle.send_mavlink(msg)
                self.vehicle.flush()

            try:
                _send_yaw_target()
            except Exception as e:
                log_verbose(f"[ROTATE] Failed to send yaw command: {e}")
                self._record("rotate", {"deg": deg, "target": target, "reason": "send_failed", "e": str(e)})
                return False

            start = time.monotonic()
            consecutive = 0
            best_abs_err = abs_delta
            last_improve_t = start
            reissued = False

            while (time.monotonic() - start) < timeout:
                heading = self._safe_get_heading()
                if heading is None:
                    log_verbose("[ROTATE] Heading read unavailable yet.")
                    time.sleep(0.15)
                    continue

                err = self._angdiff(target, heading)
                abs_err = abs(err)

                if abs_err < (best_abs_err - 0.5):
                    best_abs_err = abs_err
                    last_improve_t = time.monotonic()

                log_verbose(f"[ROTATE] heading={heading:.1f}°, target={target:.1f}°, err={err:.1f}°, tol={tol:.1f}°")

                if abs_err <= tol:
                    log_status(f"[ROTATE] Reached target bearing {target:.1f}°.")
                    self._record(
                        "rotate",
                        {
                            "deg": deg,
                            "target": target,
                            "last": heading,
                            "tol": tol,
                            "timeout": timeout,
                        },
                    )
                    return True
                else:
                    consecutive = 0
                stalled = (time.monotonic() - last_improve_t) >= ROTATE_REISSUE_AFTER_S
                if stalled and not reissued:
                    try:
                        log_verbose("[ROTATE] Progress stalled; reissuing yaw command.")
                        _send_yaw_target()
                        reissued = True
                        last_improve_t = time.monotonic()
                    except Exception as e:
                        log_verbose(f"[ROTATE] Failed to reissue yaw command: {e}")

                time.sleep(0.15)

            final_heading = self._safe_get_heading()
            final_err = None if final_heading is None else self._angdiff(target, final_heading)
            log_verbose(f"[ROTATE] Timeout. target={target:.1f}°, final={final_heading}, final_err={final_err}")
            self._record(
                "rotate",
                {
                    "deg": deg,
                    "target": target,
                    "last": final_heading,
                    "tol": tol,
                    "timeout": timeout,
                    "reason": "timeout",
                },
            )
            return False
        finally:
            self._release_busy()

    def turn_cw(self, degrees: float, speed_deg_s: float = 30.0) -> bool:
        log_status(f"[TURN_CW] Turning clockwise by {degrees}°")
        return self.rotate(abs(degrees), relative=True, speed_deg_s=speed_deg_s)

    def turn_ccw(self, degrees: float, speed_deg_s: float = 30.0) -> bool:
        log_status(f"[TURN_CCW] Turning counter-clockwise by {degrees}°")
        return self.rotate(-abs(degrees), relative=True, speed_deg_s=speed_deg_s)

    def orient(self) -> bool:
        """Rotate to the original heading (heading at arming time)."""
        if self.original_heading is None:
            log_verbose("[ORIENT] Original heading unknown.")
            return False

        current = self._safe_get_heading()
        if current is None:
            log_verbose("[ORIENT] Current heading unavailable.")
            return False

        delta = (self.original_heading - current + 540) % 360 - 180
        if abs(delta) < 1.0:
            log_verbose("[ORIENT] Already oriented.")
            return True

        log_status(f"[ORIENT] Rotating to original heading: delta {delta:.1f}°")
        if delta >= 0:
            return self.turn_cw(delta)
        return self.turn_ccw(-delta)

    # ============================================================
    # Landing / RTL
    # ============================================================
    def land(self) -> bool:
        """Initiate LAND mode and wait for touchdown and disarm."""
        log_status("[LAND] Initiating landing...")

        acquired = self._acquire_busy(timeout=2.0)
        if not acquired:
            log_verbose("[LAND] Busy - cannot land right now.")
            return False

        try:
            if not self._wait_for_mode("LAND", timeout=MODE_CHANGE_TIMEOUT):
                log_verbose("[LAND] Could not switch to LAND mode.")
                return False

            start = time.time()
            land_timeout = TAKEOFF_TIMEOUT + 30.0
            while True:
                alt = self._safe_get_alt()
                if alt is not None and alt <= 0.1:
                    log_verbose("[LAND] Touchdown detected.")
                    break
                if time.time() - start > land_timeout:
                    log_verbose("[LAND] Landing timeout.")
                    break
                time.sleep(0.5)

            if not self._wait_for_disarm(timeout=DISARM_TIMEOUT + 10.0):
                log_verbose("[LAND] Vehicle did not disarm after landing.")
                return False

            log_status("[LAND] Landing complete and vehicle disarmed.")
            self._record("land", {})
            return True
        finally:
            self._release_busy()

    def rtl(self) -> bool:
        """Return-to-launch (RTL) and wait for landing & disarm."""
        log_status("[RTL] Initiating Return-To-Launch (RTL)...")
        acquired = self._acquire_busy(timeout=2.0)
        if not acquired:
            log_verbose("[RTL] Busy - cannot start RTL now.")
            return False

        try:
            if not self._wait_for_mode("RTL", timeout=MODE_CHANGE_TIMEOUT):
                log_verbose("[RTL] Could not set RTL mode.")
                return False

            start = time.time()
            rtl_timeout = TAKEOFF_TIMEOUT + 120.0
            while True:
                alt = self._safe_get_alt()
                mode = getattr(self.vehicle, "mode", None)
                if mode and getattr(mode, "name", "") != "RTL":
                    log_verbose(f"[RTL] Mode changed externally to {mode.name}; aborting monitoring.")
                    break
                if alt is not None and alt <= 0.1:
                    log_verbose("[RTL] Touchdown detected.")
                    break
                if time.time() - start > rtl_timeout:
                    log_verbose("[RTL] RTL monitoring timeout.")
                    break
                time.sleep(0.6)

            if not self._wait_for_disarm(timeout=DISARM_TIMEOUT + 10.0):
                log_verbose("[RTL] Vehicle did not disarm after RTL landing.")
                return False

            log_status("[RTL] RTL complete and vehicle disarmed.")
            self._record("rtl", {})
            return True
        finally:
            self._release_busy()

    # ============================================================
    # Higher-level movement wrappers
    # ============================================================
    def survey_area(self, length_m: float) -> bool:
        """
        Simple two-pass survey: forward length_m, move_left 3m, return.
        Do not acquire the busy lock here; sub-commands already do that.
        """
        log_status(f"[SURVEY] Starting 2-pass sweep over {length_m:.1f} m")

        if not self.move_forward(length_m):
            log_verbose("[SURVEY] Forward pass failed.")
            return False

        time.sleep(0.3)

        if not self.move_left(3.0):
            log_verbose("[SURVEY] Lateral move failed.")
            return False

        time.sleep(0.3)

        if not self.move_backward(length_m):
            log_verbose("[SURVEY] Backward pass failed.")
            return False

        log_status("[SURVEY] Sweep complete.")
        self._record("survey_area", {"length_m": length_m})
        return True

    def goto(self, north_m: float, east_m: float, up_m: float = 0.0, groundspeed: float = MAX_SPEED_MPS) -> bool:
        """Go to a relative N/E/U offset (meters) from current position using robust absolute-target guidance."""
        acquired = self._acquire_busy(timeout=0.5)
        if not acquired:
            log_verbose("[GOTO] Busy - cannot goto now.")
            return False

        try:
            lat0, lon0, alt0 = self._safe_get_latlonalt()
            if lat0 is None or lon0 is None or alt0 is None:
                log_verbose("[GOTO] Position unavailable.")
                return False

            earth_radius = 6378137.0
            target_lat = lat0 + (north_m / earth_radius) * (180.0 / math.pi)
            target_lon = lon0 + (east_m / (earth_radius * math.cos(math.radians(lat0)))) * (180.0 / math.pi)
            target_alt = max(0.5, alt0 + float(up_m))
            target = LocationGlobalRelative(target_lat, target_lon, target_alt)

            log_status(
                f"[GOTO] N={north_m:.2f}m E={east_m:.2f}m U={up_m:.2f}m -> "
                f"lat={target_lat:.6f}, lon={target_lon:.6f}, alt={target_alt:.2f}"
            )

            dist = math.hypot(north_m, east_m)
            timeout = max(8.0, MOVE_TIMEOUT_PER_M * max(dist, 0.5) + 2.0)

            ok = self._goto_target_location(
                target,
                groundspeed=groundspeed,
                max_timeout=timeout,
                log_prefix="[GOTO]",
            )
            if ok:
                self._record("goto", {"north_m": north_m, "east_m": east_m, "up_m": up_m})
            return ok
        finally:
            self._release_busy()

    # ============================================================
    # Telemetry / emergency / status
    # ============================================================
    def get_location(self):
        lat, lon, alt = self._safe_get_latlonalt()
        log_verbose(f"[GET_LOCATION] lat={lat}, lon={lon}, alt={alt}")
        return (lat, lon, alt)

    def get_heading(self):
        heading = self._safe_get_heading()
        log_verbose(f"[GET_HEADING] {heading}")
        return heading

    def emergency_land(self):
        """Immediate attempt to land now (force mode and stop other commands)."""
        log_status("[EMERGENCY] Emergency landing requested!")
        got = self._acquire_busy(timeout=0.5)
        try:
            self.vehicle.mode = VehicleMode("LAND")
            log_verbose("[EMERGENCY] LAND mode requested.")
            return True
        except Exception as e:
            log_verbose(f"[EMERGENCY] Exception requesting LAND: {e}")
            return False
        finally:
            if got:
                self._release_busy()

    def get_status_dict(self) -> Dict[str, Any]:
        """Return a structured state snapshot suitable for prompts and monitoring."""
        v = self.vehicle

        if v and v.location and v.location.global_relative_frame:
            position = {
                "lat": v.location.global_relative_frame.lat,
                "lon": v.location.global_relative_frame.lon,
                "alt_agl": v.location.global_relative_frame.alt,
            }
        else:
            position = None

        heading = self._safe_get_heading()

        velocity = None
        if v and hasattr(v, "velocity") and v.velocity is not None:
            vx, vy, vz = v.velocity
            velocity = {"vx": vx, "vy": vy, "vz": vz}

        mode = v.mode.name if v and v.mode else None
        armed = v.armed if v else None
        battery = self._safe_battery()

        return {
            "position": position,
            "heading_degrees": heading,
            "velocity": velocity,
            "battery_percent": battery,
            "flight_mode": mode,
            "armed": armed,
            "is_simulation": self.is_simulation,
            "groundspeed_mps": self._safe_groundspeed(),
            "history_len": len(self.hist),
        }

    def get_status(self):
        """Return a structured, human-readable description of the drone state."""
        snapshot = self.get_status_dict()
        status = [
            f"- Position: {snapshot['position']}",
            f"- Heading: {snapshot['heading_degrees']}",
            f"- Velocity: {snapshot['velocity']}",
            f"- Battery: {snapshot['battery_percent']}",
            f"- Flight Mode: {snapshot['flight_mode']}",
            f"- Armed: {snapshot['armed']}",
            f"- Groundspeed: {snapshot['groundspeed_mps']}",
            f"- Simulation: {snapshot['is_simulation']}",
            f"- History length: {snapshot['history_len']}",
        ]
        return "\n".join(status)
