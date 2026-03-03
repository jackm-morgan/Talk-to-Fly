                    
"""
Improved Mavlink wrapper for DroneKit-backed drones.

Features:
- Safety watchdogs (heartbeat, battery)
- Command interlock (self._busy_lock)
- Timeouts on all blocking operations
- Structured history of commands for debugging
- Defensive telemetry getters
- Cleaner hover, movement, rotation, ascend/descend implementations
- Graceful shutdown helpers
"""

from dronekit import connect, VehicleMode, LocationGlobalRelative
from pymavlink import mavutil
import time
import math
import threading
from typing import Optional, Tuple

from talk_to_fly.logging.logger import log_status, log_verbose, log_trace
from talk_to_fly.skillset import SkillSet, create_low_level_skillset, create_high_level_skillset

                               
ARM_TIMEOUT = 20
DISARM_TIMEOUT = 20
MODE_CHANGE_TIMEOUT = 7
TAKEOFF_TIMEOUT = 45
MOVE_TIMEOUT_PER_M = 5
ROTATE_TIMEOUT_PER_360 = 30
ASCEND_TIMEOUT = 30
HOVER_REASSERT_INTERVAL = 2.0
GPS_ALT_TOLERANCE = 0.2
POSITION_TOLERANCE_M = 0.6
MAX_ALTITUDE_M = 30.0
MAX_SPEED_MPS = 2.0
LOW_BATTERY_THRESHOLD = 20.0
HEARTBEAT_TIMEOUT = 5.0
                                                          

class MavlinkWrapper:
    def __init__(self, connection_str: str, simulation: bool):
        log_status(f"[INIT] Connecting to vehicle via {connection_str}...")
        self.vehicle = connect(connection_str, wait_ready=True, timeout=60)
        log_status("[INIT] Connected to vehicle!")
        self.is_simulation = simulation
        self.hist=[]

               
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

                                                         
        self.vehicle.add_attribute_listener('last_heartbeat', self._default_heartbeat_handler)

                         
        self._watchdog_running = True
        self._watchdog_thread = threading.Thread(target=self._background_watchdog, daemon=True)
        self._watchdog_thread.start()

                               
                          
                               
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
            self.vehicle.remove_attribute_listener('last_heartbeat', self._default_heartbeat_handler)
        except Exception:
            pass
        try:
            self.vehicle.close()
        except Exception as e:
            log_verbose(f"[CLOSE] vehicle.close() exception: {e}")
        log_status("[CLOSE] Vehicle connection closed.")
        return True

                               
                       
                               
    def _safe_get_alt(self) -> Optional[float]:
        loc = getattr(self.vehicle, "location", None)
        if not loc: return None
        grf = getattr(loc, "global_relative_frame", None)
        if not grf: return None
        return getattr(grf, "alt", None)

    def _safe_get_latlonalt(self) -> Tuple[Optional[float], Optional[float], Optional[float]]:
        loc = getattr(self.vehicle, "location", None)
        if not loc: return (None, None, None)
        grf = getattr(loc, "global_relative_frame", None)
        if not grf: return (None, None, None)
        return getattr(grf, "lat", None), getattr(grf, "lon", None), getattr(grf, "alt", None)

    def _safe_get_heading(self) -> Optional[float]:
        return getattr(self.vehicle, "heading", None)

    def _safe_battery(self) -> Optional[float]:
        batt = getattr(self.vehicle, "battery", None)
        if not batt: return None
        return getattr(batt, "level", None)

                               
                     
                               
    def _record(self, cmd_name: str, args: dict = None):
        self.hist.append({"time": time.time(), "cmd": cmd_name, "args": args or {}})

                               
                       
                               
    def _acquire_busy(self, timeout: float = 0.0) -> bool:
        return self._busy_lock.acquire(timeout=timeout) if timeout else self._busy_lock.acquire(blocking=False)

    def _release_busy(self):
        if self._busy_lock.locked():
            self._busy_lock.release()

                               
                               
                               
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

                               
                             
                               
    def _send_ned_velocity(self, vx: float, vy: float, vz: float):
        """
        Send a NED velocity. Negative vz is up in this call.
        This is a fire-and-forget message (send once); continuous motion requires repeated sends.
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

    def _stop_motion(self):
        """Send zero velocity to stop movement."""
        self._send_ned_velocity(0, 0, 0)

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
            while True:
                alt = self._safe_get_alt()
                if alt is not None and alt >= target_alt - GPS_ALT_TOLERANCE:
                    log_verbose("[ASCEND] Target altitude reached.")
                    break
                if time.time() - start > timeout:
                    log_verbose("[ASCEND] Timeout while ascending.")
                    break
                                                                        
                self._send_ned_velocity(0, 0, -min(climb_rate, 3.0))
                time.sleep(0.4)
            self._stop_motion()
            self._record("ascend", {"target_alt": target_alt})
            return True
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
            while True:
                alt = self._safe_get_alt()
                if alt is not None and alt <= target_alt + GPS_ALT_TOLERANCE:
                    log_verbose("[DESCEND] Target altitude reached.")
                    break
                if time.time() - start > timeout:
                    log_verbose("[DESCEND] Timeout while descending.")
                    break
                                         
                self._send_ned_velocity(0, 0, min(descend_rate, 3.0))
                time.sleep(0.4)
            self._stop_motion()
            self._record("descend", {"target_alt": target_alt})
            return True
        finally:
            self._release_busy()

                               
                                            
                               
    def _move_direction(self, forward_m: float, groundspeed: float = MAX_SPEED_MPS, max_timeout: Optional[float] = None, log_prefix: str = "[MOVE]") -> bool:
        """
        Move forward/right relative to current heading using simple_goto target computed from offsets.
        Uses simple_goto + timeout. Preserves heading as best-effort by not issuing yaw changes.
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

            dNorth = forward_m * math.cos(heading_rad)
            dEast  = forward_m * math.sin(heading_rad)

            earth_radius = 6378137.0
            target_lat = lat0 + (dNorth / earth_radius) * (180.0 / math.pi)
            target_lon = lon0 + (dEast / (earth_radius * math.cos(math.pi * lat0 / 180.0))) * (180.0 / math.pi)

            target_location = LocationGlobalRelative(target_lat, target_lon, alt0)
            log_status(f"{log_prefix} Moving fwd={forward_m:.2f} m -> lat={target_lat:.6f}, lon={target_lon:.6f}")

                                     
            distance = math.sqrt(dNorth**2 + dEast**2)
            if max_timeout is None:
                max_timeout = max(8.0, MOVE_TIMEOUT_PER_M * distance)

                                           
            try:
                self.vehicle.groundspeed = float(groundspeed)
            except Exception:
                pass

                                           
            try:
                self.vehicle.simple_goto(target_location, groundspeed=float(groundspeed))
            except Exception:
                log_verbose(f"{log_prefix} simple_goto failed; trying velocity fallback")
                                                                                    
                                                        
                if distance > 0:
                    vx = (dNorth / distance) * groundspeed
                    vy = (dEast  / distance) * groundspeed
                else:
                    vx = vy = 0
                start = time.time()
                while time.time() - start < max_timeout:
                    self._send_ned_velocity(vx, vy, 0)
                    time.sleep(0.5)
            else:
                                                           
                start = time.time()
                while True:
                                                          
                    loc = self.vehicle.location.global_relative_frame
                    if loc is None:
                        log_verbose(f"{log_prefix} location unavailable while moving.")
                        break
                    dlat = (target_location.lat - loc.lat) * (math.pi / 180.0) * earth_radius
                    dlon = (target_location.lon - loc.lon) * (math.pi / 180.0) * earth_radius * math.cos(math.radians(loc.lat))
                    remaining = math.sqrt(dlat**2 + dlon**2)

                    log_verbose(f"{log_prefix} Remaining: {remaining:.2f} m, groundspeed: {getattr(self.vehicle, 'groundspeed', 0.0):.2f} m/s")
                    if remaining <= POSITION_TOLERANCE_M:
                        log_verbose(f"{log_prefix} Reached target (remaining {remaining:.2f} m).")
                        break
                    if time.time() - start > max_timeout:
                        log_verbose(f"{log_prefix} Timeout ({max_timeout:.1f}s) while moving; remaining {remaining:.2f} m.")
                        break
                    time.sleep(0.4)

                                                      
            self._stop_motion()
            self._record("move", {"forward_m": forward_m})
            log_status(f"{log_prefix} Movement complete.")
            return True
        finally:
            self._release_busy()

    def move_forward(self, distance_m: float) -> bool:
        return self._move_direction(distance_m, log_prefix="[MOVE_FORWARD]")

    def move_backward(self, distance_m: float) -> bool:
        self.rotate(180)
        self._move_direction(distance_m, log_prefix="[MOVE_BACKWARD]")
        self.rotate(180)
        return True

    def move_right(self, distance_m: float) -> bool:
        self.turn_cw(90)
        self._move_direction(distance_m, log_prefix="[MOVE_RIGHT]")
        self.turn_ccw(90)
        return True

    def move_left(self, distance_m: float) -> bool:
        self.turn_ccw(90)
        self._move_direction(distance_m, log_prefix="[MOVE_LEFT]")
        self.turn_cw(90)
        return True

                               
              
                               
    def rotate(self, yaw_deg: float, relative: bool = True, speed_deg_s: float = 30.0, tolerance: float = 5.0) -> bool:
        """
        Rotate by yaw_deg degrees (relative if relative=True). Uses MAV_CMD_CONDITION_YAW.
        Adaptive timeout is used: proportional to degrees rotated.
        """
        acquired = self._acquire_busy(timeout=0.5)
        if not acquired:
            log_verbose("[ROTATE] Busy - cannot rotate now.")
            return False

        try:
                                         
            deg = float(yaw_deg)
            if deg == 0:
                log_verbose("[ROTATE] Zero-degree rotation requested.")
                return True

                                                                                                                  
            is_positive = deg >= 0
            abs_deg = abs(deg)
            timeout = max(ROTATE_TIMEOUT_PER_360 * (abs_deg / 360.0), 5.0)

                                                                                                           
            msg = self.vehicle.message_factory.command_long_encode(
                0, 0,
                mavutil.mavlink.MAV_CMD_CONDITION_YAW,
                0,
                abs_deg,
                float(speed_deg_s),
                1 if is_positive else -1,
                1 if relative else 0,
                0, 0, 0
            )
            self.vehicle.send_mavlink(msg)
            self.vehicle.flush()

                                                                                                                 
            start = time.time()
            while True:
                heading = self._safe_get_heading()
                if heading is None:
                    log_verbose("[ROTATE] Heading read unavailable yet.")
                else:
                                                                                                              
                                                                                  
                                                                                                                       
                    pass
                if time.time() - start > timeout:
                    log_verbose("[ROTATE] Rotation timeout reached.")
                    break
                time.sleep(0.15)

            log_status(f"[ROTATE] Rotation command issued ({deg}°).")
            self._record("rotate", {"deg": deg})
            return True
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
        else:
            return self.turn_ccw(-delta)

                               
                
                               
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

                               
                                 
                               
    def survey_area(self, length_m: float) -> bool:
        """
        Simple two-pass survey: forward length_m, move_left 3m, return.
        Wraps movement calls and appropriately uses timeouts and interlocks.
        """
        log_status(f"[SURVEY] Starting 2-pass sweep over {length_m:.1f} m")
        acquired = self._acquire_busy(timeout=0.5)
        if not acquired:
            log_verbose("[SURVEY] Busy - cannot survey now.")
            return False

        try:
            if not self.move_forward(length_m):
                log_verbose("[SURVEY] forward pass failed.")
                return False
            time.sleep(0.3)
            if not self.move_left(3.0):
                log_verbose("[SURVEY] lateral move failed.")
                return False
            time.sleep(0.3)
                                                                
            if not self.move_backward(length_m):
                log_verbose("[SURVEY] backward pass failed.")
                return False
            log_status("[SURVEY] Sweep complete.")
            self._record("survey_area", {"length_m": length_m})
            return True
        finally:
            self._release_busy()


    def goto(self, north_m: float, east_m: float, up_m: float = 0.0, groundspeed: float = MAX_SPEED_MPS) -> bool:
        """Go to a relative N/E/U offset (meters) from current position using simple_goto.

        north_m, east_m are in the local NED frame (positive north/east).
        up_m is a *delta* altitude in meters (positive up) applied to current relative altitude.
        """
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
            target_lon = lon0 + (east_m  / (earth_radius * math.cos(math.radians(lat0)))) * (180.0 / math.pi)
            target_alt = max(0.5, alt0 + float(up_m))
            target = LocationGlobalRelative(target_lat, target_lon, target_alt)
            try:
                self.vehicle.groundspeed = float(groundspeed)
            except Exception:
                pass
            log_status(f"[GOTO] N={north_m:.2f}m E={east_m:.2f}m U={up_m:.2f}m -> lat={target_lat:.6f}, lon={target_lon:.6f}, alt={target_alt:.2f}")
            self.vehicle.simple_goto(target, groundspeed=float(groundspeed))
                                                                                      
            dist = math.hypot(north_m, east_m)
            timeout = max(8.0, MOVE_TIMEOUT_PER_M * dist)
            start = time.time()
            while time.time() - start < timeout:
                loc = self.vehicle.location.global_relative_frame
                if loc is None:
                    break
                dlat = (target.lat - loc.lat) * (math.pi / 180.0) * earth_radius
                dlon = (target.lon - loc.lon) * (math.pi / 180.0) * earth_radius * math.cos(math.radians(loc.lat))
                rem = math.hypot(dlat, dlon)
                if rem <= POSITION_TOLERANCE_M:
                    break
                time.sleep(0.5)
            self._stop_motion()
            self._record("goto", {"north_m": north_m, "east_m": east_m, "up_m": up_m})
            return True
        finally:
            self._release_busy()

                               
               
                               
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


    def get_status(self):
        """Return a structured, human-readable description of the drone state."""
        v = self.vehicle
        if v and v.location and v.location.global_relative_frame:
            pos = {
                "lat": v.location.global_relative_frame.lat,
                "lon": v.location.global_relative_frame.lon,
                "alt_agl": v.location.global_relative_frame.alt
            }
        else:
            pos = None
        heading = None
        if v and v.attitude:
            import math
            heading = math.degrees(v.attitude.yaw)

        vel = None
        if v and hasattr(v, "velocity") and v.velocity is not None:
            vx, vy, vz = v.velocity
            vel = {"vx": vx, "vy": vy, "vz": vz}
        batt = None
        if v and v.battery:
            batt = v.battery.level
        mode = v.mode.name if v and v.mode else None
        armed = v.armed if v else None
        status = [
            f"- Position: {pos}",
            f"- Heading: {heading}",
            f"- Velocity: {vel}",
            f"- Battery: {batt}",
            f"- Flight Mode: {mode}",
            f"- Armed: {armed}",
        ]
        return "\n".join(status)
