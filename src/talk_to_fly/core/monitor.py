"""Mission monitoring and state-verification utilities."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple
import math


@dataclass
class StepOutcome:
    success: bool
    reason: str
    raw_result: Any = None
    before_state: Optional[Dict[str, Any]] = None
    after_state: Optional[Dict[str, Any]] = None
    exception: Optional[str] = None
    verified: bool = False
    metrics: Dict[str, Any] = field(default_factory=dict)
    recovery_attempted: bool = False
    recovery_success: bool = False
    recovery_action: Optional[str] = None


class MissionMonitor:
    """Mission monitor with state-verified postconditions and bounded local recovery.

    The monitor no longer trusts wrapper return values alone. For supported skills it
    checks whether the drone's resulting state actually matches the intended effect.
    If a step is close to success, it attempts one bounded local correction before the
    higher-level mission loop escalates to a full replan.
    """

    POS_TOLERANCE_M = 1.2
    ALT_TOLERANCE_M = 0.6
    HEADING_TOLERANCE_DEG = 8.0
    MAX_LOCAL_POSITION_CORRECTION_M = 2.5
    MAX_LOCAL_ALT_CORRECTION_M = 1.8
    MAX_LOCAL_HEADING_CORRECTION_DEG = 25.0
    MAX_HOVER_DRIFT_M = 1.5

    def capture_state(self, drone) -> Dict[str, Any]:
        getter = getattr(drone, "get_status_dict", None)
        if callable(getter):
            try:
                return getter()
            except Exception:
                pass
        return {"status_text": str(drone.get_status())}

    def evaluate(
        self,
        step,
        *,
        raw_result: Any,
        before_state: Dict[str, Any],
        after_state: Dict[str, Any],
        exception: Optional[BaseException] = None,
    ) -> StepOutcome:
        if exception is not None:
            return StepOutcome(
                success=False,
                reason=f"Exception during execution: {exception}",
                raw_result=raw_result,
                before_state=before_state,
                after_state=after_state,
                exception=repr(exception),
                verified=False,
            )

        if step.name in ("l", "log"):
            return StepOutcome(
                success=True,
                reason="Log step executed.",
                raw_result=raw_result,
                before_state=before_state,
                after_state=after_state,
                verified=True,
            )

        verifier = getattr(self, f"_verify_{step.name}", None)
        if callable(verifier):
            outcome = verifier(step, raw_result=raw_result, before_state=before_state, after_state=after_state)
            outcome.raw_result = raw_result
            outcome.before_state = before_state
            outcome.after_state = after_state
            return outcome

        return self._generic_outcome(raw_result=raw_result, before_state=before_state, after_state=after_state)

    def attempt_local_recovery(self, drone, step, outcome: StepOutcome) -> Optional[StepOutcome]:
        if outcome.success:
            return None

        recoverer = getattr(self, f"_recover_{step.name}", None)
        if not callable(recoverer):
            return None

        try:
            recovery_note = recoverer(drone, step, outcome)
        except Exception as exc:
            return StepOutcome(
                success=False,
                reason=f"{outcome.reason} Local recovery failed with exception: {exc}",
                raw_result=outcome.raw_result,
                before_state=outcome.before_state,
                after_state=self.capture_state(drone),
                verified=False,
                metrics={**dict(outcome.metrics or {}), "pre_recovery_reason": outcome.reason},
                recovery_attempted=True,
                recovery_success=False,
                recovery_action=f"exception:{exc}",
            )

        if not recovery_note:
            return None

        final_after = self.capture_state(drone)
        reevaluated = self.evaluate(
            step,
            raw_result=True,
            before_state=outcome.before_state or {},
            after_state=final_after,
            exception=None,
        )
        reevaluated.metrics = {
            **dict(outcome.metrics or {}),
            **dict(reevaluated.metrics or {}),
            "pre_recovery_reason": outcome.reason,
        }
        reevaluated.recovery_attempted = True
        reevaluated.recovery_success = bool(reevaluated.success)
        reevaluated.recovery_action = recovery_note
        if reevaluated.success:
            reevaluated.reason = f"{reevaluated.reason} Local recovery succeeded: {recovery_note}"
        else:
            reevaluated.reason = f"{reevaluated.reason} Local recovery attempted but postcondition still not met: {recovery_note}"
        return reevaluated


class DirectExecutionMonitor(MissionMonitor):
    """Minimal execution monitor for one-shot planner baselines.

    This monitor deliberately avoids postcondition verification and local recovery.
    It models the common one-shot architecture where the planner emits a full plan
    once and execution trusts wrapper-level success/failure without an execution-
    grounded feedback loop.
    """

    def evaluate(
        self,
        step,
        *,
        raw_result: Any,
        before_state: Dict[str, Any],
        after_state: Dict[str, Any],
        exception: Optional[BaseException] = None,
    ) -> StepOutcome:
        if exception is not None:
            return StepOutcome(
                success=False,
                reason=f"Exception during execution: {exception}",
                raw_result=raw_result,
                before_state=before_state,
                after_state=after_state,
                exception=repr(exception),
                verified=False,
            )
        return self._generic_outcome(
            raw_result=raw_result,
            before_state=before_state,
            after_state=after_state,
        )

    def attempt_local_recovery(self, drone, step, outcome: StepOutcome) -> Optional[StepOutcome]:
        return None

    # ------------------------------------------------------------------
    # Generic and utility helpers
    # ------------------------------------------------------------------
    def _generic_outcome(self, *, raw_result: Any, before_state: Dict[str, Any], after_state: Dict[str, Any]) -> StepOutcome:
        if isinstance(raw_result, bool):
            success = raw_result
        elif raw_result is None:
            success = True
        else:
            success = bool(raw_result)

        reason = "Command completed successfully." if success else f"Command returned unsuccessful result: {raw_result!r}"
        return StepOutcome(
            success=success,
            reason=reason,
            raw_result=raw_result,
            before_state=before_state,
            after_state=after_state,
            verified=False,
        )

    def _extract_pos(self, state: Optional[Dict[str, Any]]) -> Optional[Tuple[float, float, float]]:
        if not state:
            return None
        pos = state.get("position") or {}
        lat = pos.get("lat")
        lon = pos.get("lon")
        alt = pos.get("alt_agl")
        if lat is None or lon is None or alt is None:
            return None
        try:
            return float(lat), float(lon), float(alt)
        except Exception:
            return None

    def _extract_alt(self, state: Optional[Dict[str, Any]]) -> Optional[float]:
        pos = self._extract_pos(state)
        if pos is None:
            return None
        return float(pos[2])

    def _extract_heading(self, state: Optional[Dict[str, Any]]) -> Optional[float]:
        if not state:
            return None
        heading = state.get("heading_degrees")
        if heading is None:
            return None
        try:
            return self._wrap360(float(heading))
        except Exception:
            return None

    def _extract_armed(self, state: Optional[Dict[str, Any]]) -> Optional[bool]:
        if not state:
            return None
        armed = state.get("armed")
        return None if armed is None else bool(armed)

    def _wrap360(self, deg: float) -> float:
        return float(deg) % 360.0

    def _angdiff(self, target: float, current: float) -> float:
        return (float(target) - float(current) + 180.0) % 360.0 - 180.0

    def _displacement_neu(self, before_state: Dict[str, Any], after_state: Dict[str, Any]) -> Optional[Tuple[float, float, float]]:
        before = self._extract_pos(before_state)
        after = self._extract_pos(after_state)
        if before is None or after is None:
            return None
        lat0, lon0, alt0 = before
        lat1, lon1, alt1 = after
        earth_radius = 6378137.0
        d_north = math.radians(lat1 - lat0) * earth_radius
        d_east = math.radians(lon1 - lon0) * earth_radius * math.cos(math.radians((lat0 + lat1) / 2.0))
        d_up = alt1 - alt0
        return d_north, d_east, d_up

    def _movement_vector(self, step, before_state: Dict[str, Any]) -> Optional[Tuple[float, float, float]]:
        heading = self._extract_heading(before_state)
        if step.name == "go":
            try:
                raw = step.args_list[0] if step.args_list else "0,0,0"
                if isinstance(raw, str):
                    parts = [float(x.strip()) for x in raw.split(",")]
                else:
                    parts = [float(x) for x in raw]
                while len(parts) < 3:
                    parts.append(0.0)
                return parts[0], parts[1], parts[2]
            except Exception:
                return None

        if not step.args_list:
            return None
        try:
            distance = float(step.args_list[0])
        except Exception:
            return None

        if step.name in ("mf", "mb", "mr", "ml") and heading is not None:
            hdg = math.radians(heading)
            if step.name == "mf":
                return distance * math.cos(hdg), distance * math.sin(hdg), 0.0
            if step.name == "mb":
                return -distance * math.cos(hdg), -distance * math.sin(hdg), 0.0
            if step.name == "mr":
                side = hdg + math.pi / 2.0
                return distance * math.cos(side), distance * math.sin(side), 0.0
            if step.name == "ml":
                side = hdg - math.pi / 2.0
                return distance * math.cos(side), distance * math.sin(side), 0.0

        if step.name == "mu":
            return 0.0, 0.0, distance
        if step.name == "md":
            return 0.0, 0.0, -distance
        return None

    def _position_outcome(self, *, success: bool, reason: str, metrics: Dict[str, Any]) -> StepOutcome:
        return StepOutcome(success=success, reason=reason, verified=True, metrics=metrics)

    # ------------------------------------------------------------------
    # Skill-specific verifiers
    # ------------------------------------------------------------------
    def _verify_a(self, step, *, raw_result: Any, before_state: Dict[str, Any], after_state: Dict[str, Any]) -> StepOutcome:
        armed = self._extract_armed(after_state)
        success = bool(armed)
        return StepOutcome(success=success, reason=("Vehicle armed." if success else "Vehicle is still disarmed after arm command."), verified=True)

    def _verify_d(self, step, *, raw_result: Any, before_state: Dict[str, Any], after_state: Dict[str, Any]) -> StepOutcome:
        armed = self._extract_armed(after_state)
        success = armed is False
        return StepOutcome(success=success, reason=("Vehicle disarmed." if success else "Vehicle is still armed after disarm command."), verified=True)

    def _verify_tk(self, step, *, raw_result: Any, before_state: Dict[str, Any], after_state: Dict[str, Any]) -> StepOutcome:
        target_alt = float(step.args_list[0]) if step.args_list else None
        after_alt = self._extract_alt(after_state)
        armed = self._extract_armed(after_state)
        tol = max(self.ALT_TOLERANCE_M, 0.15 * abs(target_alt or 0.0))
        success = (armed is True) and (after_alt is not None) and (target_alt is not None) and (after_alt >= target_alt - tol)
        reason = (
            f"Takeoff verified at {after_alt:.2f} m."
            if success and after_alt is not None
            else f"Takeoff postcondition not met. Target {target_alt} m, observed {after_alt} m, armed={armed}."
        )
        return StepOutcome(success=success, reason=reason, verified=True, metrics={"target_alt_m": target_alt, "observed_alt_m": after_alt, "armed": armed, "alt_tol_m": tol})

    def _verify_mu(self, step, *, raw_result: Any, before_state: Dict[str, Any], after_state: Dict[str, Any]) -> StepOutcome:
        before_alt = self._extract_alt(before_state)
        after_alt = self._extract_alt(after_state)
        delta = float(step.args_list[0]) if step.args_list else 0.0
        target_alt = None if before_alt is None else before_alt + delta
        tol = max(self.ALT_TOLERANCE_M, 0.15 * abs(delta))
        success = target_alt is not None and after_alt is not None and after_alt >= target_alt - tol
        reason = (
            f"Climb verified at {after_alt:.2f} m."
            if success and after_alt is not None
            else f"Ascend postcondition not met. Expected >= {target_alt} m, observed {after_alt} m."
        )
        return StepOutcome(success=success, reason=reason, verified=True, metrics={"target_alt_m": target_alt, "observed_alt_m": after_alt, "alt_tol_m": tol})

    def _verify_md(self, step, *, raw_result: Any, before_state: Dict[str, Any], after_state: Dict[str, Any]) -> StepOutcome:
        before_alt = self._extract_alt(before_state)
        after_alt = self._extract_alt(after_state)
        delta = float(step.args_list[0]) if step.args_list else 0.0
        target_alt = None if before_alt is None else before_alt - delta
        tol = max(self.ALT_TOLERANCE_M, 0.15 * abs(delta))
        success = target_alt is not None and after_alt is not None and after_alt <= target_alt + tol
        reason = (
            f"Descent verified at {after_alt:.2f} m."
            if success and after_alt is not None
            else f"Descend postcondition not met. Expected <= {target_alt} m, observed {after_alt} m."
        )
        return StepOutcome(success=success, reason=reason, verified=True, metrics={"target_alt_m": target_alt, "observed_alt_m": after_alt, "alt_tol_m": tol})

    def _verify_heading_turn(self, step, before_state: Dict[str, Any], after_state: Dict[str, Any], requested_delta: Optional[float] = None) -> StepOutcome:
        before_heading = self._extract_heading(before_state)
        after_heading = self._extract_heading(after_state)
        if before_heading is None or after_heading is None:
            return StepOutcome(success=False, reason="Heading unavailable for postcondition check.", verified=True)
        if requested_delta is None:
            if step.name == "tcw":
                requested_delta = abs(float(step.args_list[0]))
            elif step.name == "tccw":
                requested_delta = -abs(float(step.args_list[0]))
            else:
                return StepOutcome(success=bool(step.args_list), reason="Heading step lacked requested delta.", verified=True)
        target = self._wrap360(before_heading + requested_delta)
        err = abs(self._angdiff(target, after_heading))
        tol = max(self.HEADING_TOLERANCE_DEG, 0.12 * abs(requested_delta))
        success = err <= tol
        reason = (
            f"Heading verified at {after_heading:.1f}° (target {target:.1f}°)."
            if success
            else f"Heading postcondition not met. Target {target:.1f}°, observed {after_heading:.1f}°, error {err:.1f}°.")
        return StepOutcome(success=success, reason=reason, verified=True, metrics={"before_heading_deg": before_heading, "after_heading_deg": after_heading, "target_heading_deg": target, "heading_error_deg": err, "heading_tol_deg": tol})

    def _verify_tcw(self, step, *, raw_result: Any, before_state: Dict[str, Any], after_state: Dict[str, Any]) -> StepOutcome:
        return self._verify_heading_turn(step, before_state, after_state)

    def _verify_tccw(self, step, *, raw_result: Any, before_state: Dict[str, Any], after_state: Dict[str, Any]) -> StepOutcome:
        return self._verify_heading_turn(step, before_state, after_state)

    def _verify_o(self, step, *, raw_result: Any, before_state: Dict[str, Any], after_state: Dict[str, Any]) -> StepOutcome:
        return StepOutcome(success=bool(raw_result), reason=("Orientation command completed." if raw_result else "Orientation command did not complete."), verified=False)

    def _verify_hv(self, step, *, raw_result: Any, before_state: Dict[str, Any], after_state: Dict[str, Any]) -> StepOutcome:
        disp = self._displacement_neu(before_state, after_state)
        if disp is None:
            return self._generic_outcome(raw_result=raw_result, before_state=before_state, after_state=after_state)
        drift = math.hypot(disp[0], disp[1])
        success = drift <= self.MAX_HOVER_DRIFT_M and (bool(raw_result) if raw_result is not None else True)
        reason = (
            f"Hover verified with {drift:.2f} m drift."
            if success
            else f"Hover drift too large: {drift:.2f} m."
        )
        return StepOutcome(success=success, reason=reason, verified=True, metrics={"hover_drift_m": drift, "hover_drift_tol_m": self.MAX_HOVER_DRIFT_M})

    def _verify_position_step(self, step, *, before_state: Dict[str, Any], after_state: Dict[str, Any]) -> StepOutcome:
        expected = self._movement_vector(step, before_state)
        actual = self._displacement_neu(before_state, after_state)
        if expected is None or actual is None:
            return StepOutcome(success=False, reason="Position unavailable for postcondition check.", verified=True)

        err_n = expected[0] - actual[0]
        err_e = expected[1] - actual[1]
        err_u = expected[2] - actual[2]
        horiz_err = math.hypot(err_n, err_e)
        alt_err = abs(err_u)
        commanded_horiz = math.hypot(expected[0], expected[1])
        tol_xy = max(self.POS_TOLERANCE_M, 0.2 * commanded_horiz)
        tol_z = max(self.ALT_TOLERANCE_M, 0.2 * abs(expected[2]))
        success = horiz_err <= tol_xy and alt_err <= tol_z
        reason = (
            f"Movement verified. Horizontal residual {horiz_err:.2f} m, vertical residual {alt_err:.2f} m."
            if success
            else f"Movement postcondition not met. Horizontal residual {horiz_err:.2f} m, vertical residual {alt_err:.2f} m."
        )
        return self._position_outcome(
            success=success,
            reason=reason,
            metrics={
                "expected_north_m": expected[0],
                "expected_east_m": expected[1],
                "expected_up_m": expected[2],
                "actual_north_m": actual[0],
                "actual_east_m": actual[1],
                "actual_up_m": actual[2],
                "residual_north_m": err_n,
                "residual_east_m": err_e,
                "residual_up_m": err_u,
                "horizontal_residual_m": horiz_err,
                "vertical_residual_m": alt_err,
                "horizontal_tol_m": tol_xy,
                "vertical_tol_m": tol_z,
            },
        )

    def _verify_mf(self, step, *, raw_result: Any, before_state: Dict[str, Any], after_state: Dict[str, Any]) -> StepOutcome:
        return self._verify_position_step(step, before_state=before_state, after_state=after_state)

    def _verify_mb(self, step, *, raw_result: Any, before_state: Dict[str, Any], after_state: Dict[str, Any]) -> StepOutcome:
        return self._verify_position_step(step, before_state=before_state, after_state=after_state)

    def _verify_mr(self, step, *, raw_result: Any, before_state: Dict[str, Any], after_state: Dict[str, Any]) -> StepOutcome:
        return self._verify_position_step(step, before_state=before_state, after_state=after_state)

    def _verify_ml(self, step, *, raw_result: Any, before_state: Dict[str, Any], after_state: Dict[str, Any]) -> StepOutcome:
        return self._verify_position_step(step, before_state=before_state, after_state=after_state)

    def _verify_go(self, step, *, raw_result: Any, before_state: Dict[str, Any], after_state: Dict[str, Any]) -> StepOutcome:
        return self._verify_position_step(step, before_state=before_state, after_state=after_state)

    def _verify_ld(self, step, *, raw_result: Any, before_state: Dict[str, Any], after_state: Dict[str, Any]) -> StepOutcome:
        armed = self._extract_armed(after_state)
        alt = self._extract_alt(after_state)
        success = (armed is False) or (alt is not None and alt <= 0.25)
        reason = (
            "Landing verified."
            if success
            else f"Landing postcondition not met. armed={armed}, alt={alt}."
        )
        return StepOutcome(success=success, reason=reason, verified=True, metrics={"armed": armed, "observed_alt_m": alt})

    def _verify_rtl(self, step, *, raw_result: Any, before_state: Dict[str, Any], after_state: Dict[str, Any]) -> StepOutcome:
        return self._verify_ld(step, raw_result=raw_result, before_state=before_state, after_state=after_state)

    # ------------------------------------------------------------------
    # Local recovery handlers
    # ------------------------------------------------------------------
    def _recover_tk(self, drone, step, outcome: StepOutcome) -> Optional[str]:
        target_alt = outcome.metrics.get("target_alt_m")
        observed_alt = outcome.metrics.get("observed_alt_m")
        if target_alt is None or observed_alt is None:
            return None
        residual = float(target_alt) - float(observed_alt)
        if residual <= 0 or residual > self.MAX_LOCAL_ALT_CORRECTION_M:
            return None
        if drone.ascend(float(residual)):
            return f"ascend({residual:.2f}m)"
        return None

    def _recover_mu(self, drone, step, outcome: StepOutcome) -> Optional[str]:
        target_alt = outcome.metrics.get("target_alt_m")
        observed_alt = outcome.metrics.get("observed_alt_m")
        if target_alt is None or observed_alt is None:
            return None
        residual = float(target_alt) - float(observed_alt)
        if residual <= 0 or residual > self.MAX_LOCAL_ALT_CORRECTION_M:
            return None
        if drone.ascend(float(residual)):
            return f"ascend({residual:.2f}m)"
        return None

    def _recover_md(self, drone, step, outcome: StepOutcome) -> Optional[str]:
        target_alt = outcome.metrics.get("target_alt_m")
        observed_alt = outcome.metrics.get("observed_alt_m")
        if target_alt is None or observed_alt is None:
            return None
        residual = float(observed_alt) - float(target_alt)
        if residual <= 0 or residual > self.MAX_LOCAL_ALT_CORRECTION_M:
            return None
        if drone.descend(float(residual)):
            return f"descend({residual:.2f}m)"
        return None

    def _recover_tcw(self, drone, step, outcome: StepOutcome) -> Optional[str]:
        err = outcome.metrics.get("heading_error_deg")
        after = outcome.metrics.get("after_heading_deg")
        target = outcome.metrics.get("target_heading_deg")
        if err is None or after is None or target is None:
            return None
        correction = self._angdiff(float(target), float(after))
        if abs(correction) > self.MAX_LOCAL_HEADING_CORRECTION_DEG or abs(correction) < 1.0:
            return None
        if drone.rotate(float(correction), relative=True):
            return f"rotate({correction:.1f}deg)"
        return None

    def _recover_tccw(self, drone, step, outcome: StepOutcome) -> Optional[str]:
        return self._recover_tcw(drone, step, outcome)

    def _recover_position_step(self, drone, step, outcome: StepOutcome) -> Optional[str]:
        dn = outcome.metrics.get("residual_north_m")
        de = outcome.metrics.get("residual_east_m")
        du = outcome.metrics.get("residual_up_m")
        if dn is None or de is None or du is None:
            return None
        horiz = math.hypot(float(dn), float(de))
        if horiz > self.MAX_LOCAL_POSITION_CORRECTION_M or abs(float(du)) > self.MAX_LOCAL_ALT_CORRECTION_M:
            return None
        if horiz < 0.2 and abs(float(du)) < 0.2:
            return None
        if drone.goto(float(dn), float(de), float(du)):
            return f"goto(n={dn:.2f}, e={de:.2f}, u={du:.2f})"
        return None

    def _recover_mf(self, drone, step, outcome: StepOutcome) -> Optional[str]:
        return self._recover_position_step(drone, step, outcome)

    def _recover_mb(self, drone, step, outcome: StepOutcome) -> Optional[str]:
        return self._recover_position_step(drone, step, outcome)

    def _recover_mr(self, drone, step, outcome: StepOutcome) -> Optional[str]:
        return self._recover_position_step(drone, step, outcome)

    def _recover_ml(self, drone, step, outcome: StepOutcome) -> Optional[str]:
        return self._recover_position_step(drone, step, outcome)

    def _recover_go(self, drone, step, outcome: StepOutcome) -> Optional[str]:
        return self._recover_position_step(drone, step, outcome)

    def _recover_ld(self, drone, step, outcome: StepOutcome) -> Optional[str]:
        armed = outcome.metrics.get("armed")
        alt = outcome.metrics.get("observed_alt_m")
        if armed is False or (alt is not None and alt <= 0.25):
            return None
        if drone.land():
            return "land() retry"
        return None

    def _recover_rtl(self, drone, step, outcome: StepOutcome) -> Optional[str]:
        armed = outcome.metrics.get("armed")
        alt = outcome.metrics.get("observed_alt_m")
        if armed is False or (alt is not None and alt <= 0.25):
            return None
        if drone.rtl():
            return "rtl() retry"
        return None
