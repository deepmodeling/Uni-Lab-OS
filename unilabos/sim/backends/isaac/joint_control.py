from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from itertools import count
from typing import Any, Protocol


class JointControlError(RuntimeError):
    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None) -> None:
        self.code = str(code)
        self.message = str(message)
        self.details = dict(details or {})
        super().__init__(f"{self.code}: {self.message}")


@dataclass(frozen=True)
class JointSpec:
    name: str
    path: str
    joint_type: str
    drive_axis: str
    unit: str
    lower_limit: float
    upper_limit: float
    body0: str | None = None
    body1: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class PlanOptions:
    max_revolute_step_deg: float = 5.0
    max_prismatic_step_m: float = 0.02
    sim_steps_per_waypoint: int = 4
    max_waypoints: int = 240


DEFAULT_STABLE_DRIVE_SETTINGS: dict[str, dict[str, float]] = {
    "arm0_joint": {"stiffness": 180.0, "damping": 50.0, "max_force": 800.0},
    "arm1_joint": {"stiffness": 120.0, "damping": 18.0, "max_force": 1200.0},
    "arm2_joint": {"stiffness": 90.0, "damping": 14.0, "max_force": 900.0},
    "arm3_joint": {"stiffness": 45.0, "damping": 8.0, "max_force": 500.0},
    "left_joint": {"stiffness": 20.0, "damping": 4.0, "max_force": 100.0},
    "right_joint": {"stiffness": 20.0, "damping": 4.0, "max_force": 100.0},
    "gate_joint": {"stiffness": 80.0, "damping": 12.0, "max_force": 600.0},
}


@dataclass
class TrajectoryPlan:
    plan_id: str
    start: dict[str, float]
    targets: dict[str, float]
    waypoints: list[dict[str, float]]
    options: PlanOptions = field(default_factory=PlanOptions)
    checked: bool = False
    safe: bool | None = None
    code: str = "unchecked"
    message: str = "Trajectory has not been checked"
    contacts: list[dict[str, Any]] = field(default_factory=list)
    waypoint_index: int | None = None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["options"] = asdict(self.options)
        return data


@dataclass
class CollisionCheckResult:
    plan_id: str
    safe: bool
    code: str
    message: str
    waypoint_index: int | None = None
    contacts: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ExecutionResult:
    plan_id: str | None
    ok: bool
    code: str
    message: str
    waypoints_executed: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class JointControlAdapter(Protocol):
    def list_joint_specs(self) -> list[JointSpec]:
        ...

    def get_joint_positions(self) -> dict[str, float]:
        ...

    def set_joint_targets(self, targets: dict[str, float]) -> None:
        ...

    def step_simulation(self, steps: int) -> None:
        ...

    def get_disallowed_contacts(self) -> list[dict[str, Any]]:
        ...

    def capture_state(self) -> Any:
        ...

    def restore_state(self, state: Any) -> None:
        ...

    def apply_drive_settings(self, settings: dict[str, dict[str, float]]) -> dict[str, dict[str, float]]:
        ...


DEFAULT_MVPPKUSHENGKE_JOINTS: list[JointSpec] = [
    JointSpec(
        name="arm0_joint",
        path="/World/HOR_Horizon_V2_1_2508_13/joints/arm0_joint",
        joint_type="prismatic",
        drive_axis="linear",
        unit="m",
        lower_limit=0.0,
        upper_limit=0.4300000071525574,
        body0="/World/HOR_Horizon_V2_1_2508_13/arm_Link",
        body1="/World/HOR_Horizon_V2_1_2508_13/arm0_Link",
    ),
    JointSpec(
        name="arm1_joint",
        path="/World/HOR_Horizon_V2_1_2508_13/joints/arm1_joint",
        joint_type="revolute",
        drive_axis="angular",
        unit="deg",
        lower_limit=-114.591552734375,
        upper_limit=114.591552734375,
        body0="/World/HOR_Horizon_V2_1_2508_13/arm0_Link",
        body1="/World/HOR_Horizon_V2_1_2508_13/arm1_Link",
    ),
    JointSpec(
        name="arm2_joint",
        path="/World/HOR_Horizon_V2_1_2508_13/joints/arm2_joint",
        joint_type="revolute",
        drive_axis="angular",
        unit="deg",
        lower_limit=-114.591552734375,
        upper_limit=114.591552734375,
        body0="/World/HOR_Horizon_V2_1_2508_13/arm1_Link",
        body1="/World/HOR_Horizon_V2_1_2508_13/arm2_Link",
    ),
    JointSpec(
        name="arm3_joint",
        path="/World/HOR_Horizon_V2_1_2508_13/joints/arm3_joint",
        joint_type="revolute",
        drive_axis="angular",
        unit="deg",
        lower_limit=-114.591552734375,
        upper_limit=114.591552734375,
        body0="/World/HOR_Horizon_V2_1_2508_13/arm2_Link",
        body1="/World/HOR_Horizon_V2_1_2508_13/arm3_Link",
    ),
    JointSpec(
        name="left_joint",
        path="/World/HOR_Horizon_V2_1_2508_13/joints/left_joint",
        joint_type="prismatic",
        drive_axis="linear",
        unit="m",
        lower_limit=-0.02500000037252903,
        upper_limit=0.0,
        body0="/World/HOR_Horizon_V2_1_2508_13/arm3_Link",
        body1="/World/HOR_Horizon_V2_1_2508_13/left_Link",
    ),
    JointSpec(
        name="right_joint",
        path="/World/HOR_Horizon_V2_1_2508_13/joints/right_joint",
        joint_type="prismatic",
        drive_axis="linear",
        unit="m",
        lower_limit=0.0,
        upper_limit=0.02500000037252903,
        body0="/World/HOR_Horizon_V2_1_2508_13/arm3_Link",
        body1="/World/HOR_Horizon_V2_1_2508_13/right_Link",
    ),
    JointSpec(
        name="gate_joint",
        path="/World/HOR_Horizon_V2_1_2508_13/joints/gate_joint",
        joint_type="revolute",
        drive_axis="angular",
        unit="deg",
        lower_limit=0.0,
        upper_limit=89.9543685913086,
        body0="/World/HOR_Horizon_V2_1_2508_13/base_link",
        body1="/World/HOR_Horizon_V2_1_2508_13/gate_Link",
    ),
]


class JointControlService:
    def __init__(self, adapter: JointControlAdapter) -> None:
        self.adapter = adapter
        self._plan_ids = count(1)
        self._plans: dict[str, TrajectoryPlan] = {}
        self._status = "ready"
        self._last_result: dict[str, Any] | None = None
        self._stop_requested = False
        self._collision_check_enabled = True
        self._drive_tuning: dict[str, Any] = {"mode": "asset", "settings": {}}

    def list_joints(self) -> list[JointSpec]:
        return list(self.adapter.list_joint_specs())

    def get_joint_control_state(self) -> dict[str, Any]:
        positions = self.adapter.get_joint_positions()
        last_plan = None
        if self._plans:
            last_plan = next(reversed(self._plans.values())).to_dict()
        return {
            "status": self._status,
            "collision_check_enabled": self._collision_check_enabled,
            "drive_tuning": dict(self._drive_tuning),
            "joints": [spec.to_dict() for spec in self.list_joints()],
            "positions": {str(key): float(value) for key, value in positions.items()},
            "last_plan": last_plan,
            "last_result": self._last_result,
        }

    def set_collision_check_enabled(self, enabled: bool) -> dict[str, bool]:
        self._collision_check_enabled = bool(enabled)
        self._last_result = {
            "ok": True,
            "code": "collision_check_enabled" if self._collision_check_enabled else "collision_check_disabled",
            "collision_check_enabled": self._collision_check_enabled,
        }
        return {"collision_check_enabled": self._collision_check_enabled}

    def apply_stable_drive_settings(
        self,
        settings: dict[str, dict[str, float]] | None = None,
    ) -> dict[str, Any]:
        clean_settings = self._clean_drive_settings(settings or DEFAULT_STABLE_DRIVE_SETTINGS)
        applied = self.adapter.apply_drive_settings(clean_settings)
        result = {
            "ok": True,
            "code": "stable_drive_applied",
            "settings": {str(name): dict(values) for name, values in applied.items()},
        }
        self._drive_tuning = {"mode": "stable", "settings": result["settings"]}
        self._last_result = result
        return result

    def plan_joint_targets(
        self,
        targets: dict[str, float],
        options: PlanOptions | dict[str, Any] | None = None,
    ) -> TrajectoryPlan:
        plan_options = self._coerce_options(options)
        clean_targets = {str(key): float(value) for key, value in targets.items()}
        specs = {spec.name: spec for spec in self.list_joints()}
        current = self.adapter.get_joint_positions()
        for name, value in clean_targets.items():
            spec = specs.get(name)
            if spec is None:
                raise JointControlError("unknown_joint", f"Unknown joint target: {name}", {"joint": name})
            if value < spec.lower_limit or value > spec.upper_limit:
                raise JointControlError(
                    "joint_limit_exceeded",
                    f"Joint {name} target {value} outside [{spec.lower_limit}, {spec.upper_limit}]",
                    {"joint": name, "target": value, "lower_limit": spec.lower_limit, "upper_limit": spec.upper_limit},
                )

        start = {name: float(current.get(name, 0.0)) for name in clean_targets}
        waypoint_count = self._waypoint_count(start, clean_targets, specs, plan_options)
        if waypoint_count > int(plan_options.max_waypoints):
            raise JointControlError(
                "trajectory_too_long",
                f"Trajectory requires {waypoint_count} waypoints, max is {plan_options.max_waypoints}",
                {"waypoints": waypoint_count, "max_waypoints": plan_options.max_waypoints},
            )
        waypoints: list[dict[str, float]] = []
        for index in range(1, waypoint_count + 1):
            ratio = float(index) / float(waypoint_count)
            waypoints.append(
                {
                    name: float(start[name] + (clean_targets[name] - start[name]) * ratio)
                    for name in clean_targets
                }
            )
        plan_id = f"plan_{next(self._plan_ids):04d}"
        plan = TrajectoryPlan(
            plan_id=plan_id,
            start=start,
            targets=clean_targets,
            waypoints=waypoints,
            options=plan_options,
        )
        self._plans[plan_id] = plan
        self._status = "planned"
        self._last_result = {"ok": True, "code": "planned", "plan_id": plan_id}
        return plan

    def check_joint_plan(self, plan_id: str) -> CollisionCheckResult:
        plan = self._require_plan(plan_id)
        self._status = "checking"
        if not self._collision_check_enabled:
            result = CollisionCheckResult(
                plan_id=plan.plan_id,
                safe=True,
                code="collision_check_disabled",
                message="Collision check is disabled",
            )
            self._mark_checked(plan, result)
            return result

        state = self.adapter.capture_state()
        applied_any = False
        try:
            baseline_contact_keys = self._contact_keys(self.adapter.get_disallowed_contacts())
            for index, waypoint in enumerate(plan.waypoints):
                self.adapter.set_joint_targets(waypoint)
                applied_any = True
                self.adapter.step_simulation(plan.options.sim_steps_per_waypoint)
                contacts = self._new_contacts(self.adapter.get_disallowed_contacts(), baseline_contact_keys)
                if contacts:
                    result = CollisionCheckResult(
                        plan_id=plan.plan_id,
                        safe=False,
                        code="collision_detected",
                        message=f"Trajectory collides at waypoint {index}",
                        waypoint_index=index,
                        contacts=[dict(contact) for contact in contacts],
                    )
                    self._mark_checked(plan, result)
                    return result
        except JointControlError:
            raise
        except Exception as exc:
            result = CollisionCheckResult(
                plan_id=plan.plan_id,
                safe=False,
                code="collision_check_unavailable",
                message=str(exc),
            )
            self._mark_checked(plan, result)
            return result
        finally:
            if applied_any:
                self.adapter.restore_state(state)

        result = CollisionCheckResult(
            plan_id=plan.plan_id,
            safe=True,
            code="safe",
            message="Trajectory collision check passed",
        )
        self._mark_checked(plan, result)
        return result

    def execute_joint_plan(self, plan_id: str) -> ExecutionResult:
        plan = self._require_plan(plan_id)
        if self._collision_check_enabled and not plan.checked:
            raise JointControlError("plan_not_checked", f"Plan {plan_id} has not been collision checked")
        if self._collision_check_enabled and plan.code == "collision_check_disabled":
            raise JointControlError("plan_not_checked", f"Plan {plan_id} must be rechecked with collision checking enabled")
        if self._collision_check_enabled and plan.safe is not True:
            raise JointControlError("plan_not_safe", f"Plan {plan_id} is not safe", {"code": plan.code})
        self._status = "executing"
        self._stop_requested = False
        executed = 0
        baseline_contact_keys: set[tuple[str, str]] = set()
        if self._collision_check_enabled:
            try:
                baseline_contact_keys = self._contact_keys(self.adapter.get_disallowed_contacts())
            except Exception as exc:
                result = ExecutionResult(
                    plan_id=plan_id,
                    ok=False,
                    code="collision_check_unavailable",
                    message=str(exc),
                    waypoints_executed=0,
                )
                self._last_result = result.to_dict()
                self._status = "failed"
                return result
        for waypoint in plan.waypoints:
            if self._stop_requested:
                result = ExecutionResult(plan_id=plan_id, ok=False, code="stopped", message="Motion stopped", waypoints_executed=executed)
                self._last_result = result.to_dict()
                self._status = "stopped"
                return result
            self.adapter.set_joint_targets(waypoint)
            self.adapter.step_simulation(plan.options.sim_steps_per_waypoint)
            if self._collision_check_enabled:
                contacts = self._new_contacts(self.adapter.get_disallowed_contacts(), baseline_contact_keys)
                if contacts:
                    result = ExecutionResult(
                        plan_id=plan_id,
                        ok=False,
                        code="collision_detected",
                        message=f"Execution collision after waypoint {executed}",
                        waypoints_executed=executed,
                    )
                    self._last_result = result.to_dict()
                    self._status = "failed"
                    return result
            executed += 1
        result = ExecutionResult(
            plan_id=plan_id,
            ok=True,
            code="executed",
            message="Trajectory executed",
            waypoints_executed=executed,
        )
        self._last_result = result.to_dict()
        self._status = "ready"
        return result

    def stop_joint_motion(self) -> ExecutionResult:
        self._stop_requested = True
        self._status = "stopped"
        result = ExecutionResult(plan_id=None, ok=True, code="stopped", message="Stop requested")
        self._last_result = result.to_dict()
        return result

    def _mark_checked(self, plan: TrajectoryPlan, result: CollisionCheckResult) -> None:
        plan.checked = True
        plan.safe = result.safe
        plan.code = result.code
        plan.message = result.message
        plan.contacts = list(result.contacts)
        plan.waypoint_index = result.waypoint_index
        self._last_result = result.to_dict()
        self._status = "checked_safe" if result.safe else "failed"

    def _require_plan(self, plan_id: str) -> TrajectoryPlan:
        plan = self._plans.get(str(plan_id))
        if plan is None:
            raise JointControlError("unknown_plan", f"Unknown joint plan: {plan_id}", {"plan_id": str(plan_id)})
        return plan

    def _coerce_options(self, options: PlanOptions | dict[str, Any] | None) -> PlanOptions:
        if options is None:
            return PlanOptions()
        if isinstance(options, PlanOptions):
            return options
        return PlanOptions(**dict(options))

    def _clean_drive_settings(self, settings: dict[str, dict[str, float]]) -> dict[str, dict[str, float]]:
        specs = {spec.name: spec for spec in self.list_joints()}
        clean_settings: dict[str, dict[str, float]] = {}
        for name, values in settings.items():
            if str(name) not in specs:
                raise JointControlError("unknown_joint", f"Unknown joint drive setting: {name}", {"joint": str(name)})
            clean_settings[str(name)] = {
                "stiffness": float(values["stiffness"]),
                "damping": float(values["damping"]),
                "max_force": float(values["max_force"]),
            }
        return clean_settings

    def _waypoint_count(
        self,
        start: dict[str, float],
        targets: dict[str, float],
        specs: dict[str, JointSpec],
        options: PlanOptions,
    ) -> int:
        waypoint_count = 0
        for name, target in targets.items():
            delta = abs(float(target) - float(start[name]))
            if delta <= 1e-12:
                continue
            spec = specs[name]
            if spec.unit == "deg":
                max_step = max(float(options.max_revolute_step_deg), 1e-9)
            else:
                max_step = max(float(options.max_prismatic_step_m), 1e-9)
            waypoint_count = max(waypoint_count, int(math.ceil(delta / max_step)))
        return waypoint_count

    def _contact_keys(self, contacts: list[dict[str, Any]]) -> set[tuple[str, str]]:
        return {self._contact_key(contact) for contact in contacts}

    def _new_contacts(
        self,
        contacts: list[dict[str, Any]],
        baseline_contact_keys: set[tuple[str, str]],
    ) -> list[dict[str, Any]]:
        return [dict(contact) for contact in contacts if self._contact_key(contact) not in baseline_contact_keys]

    def _contact_key(self, contact: dict[str, Any]) -> tuple[str, str]:
        body0 = self._contact_endpoint(contact, ("body0", "actor0", "path0", "prim0"))
        body1 = self._contact_endpoint(contact, ("body1", "actor1", "path1", "prim1"))
        if not body0 and not body1:
            return ("", repr(sorted((str(key), str(value)) for key, value in dict(contact).items())))
        return tuple(sorted((body0, body1)))

    def _contact_endpoint(self, contact: dict[str, Any], names: tuple[str, ...]) -> str:
        for name in names:
            value = contact.get(name)
            if value:
                return str(value)
        return ""
