from __future__ import annotations

import os
import time
from typing import Any

from unilabos.registry.decorators import ActionInputHandle, DataSource, action, device, not_action
from unilabos.devices.workstation.szlab_poly_studio.sensor import wait_sensor_conditions
from unilabos.devices.workstation.szlab_poly_studio.s12_robot.robot_tasks import (
    ROBOT_HOME_VARIABLE,
    ROBOT_TASK_COMPLETE_VARIABLE,
    ROBOT_TASK_NUMBER_VARIABLE,
    ROBOT_WRITE_ALLOWED_VARIABLE,
    ROBOT_WRITE_DONE_VARIABLE,
    S05_MATERIAL_SENSOR,
)
from unilabos.devices.workstation.szlab_poly_studio.s12_robot.robot_S01 import SzlabRobotS01Mixin
from unilabos.devices.workstation.szlab_poly_studio.s12_robot.robot_S02 import SzlabRobotS02Mixin
from unilabos.devices.workstation.szlab_poly_studio.s12_robot.robot_S03 import SzlabRobotS03Mixin
from unilabos.devices.workstation.szlab_poly_studio.s12_robot.robot_S04 import SzlabRobotS04Mixin
from unilabos.devices.workstation.szlab_poly_studio.s12_robot.robot_S05 import SzlabRobotS05Mixin
from unilabos.devices.workstation.szlab_poly_studio.s12_robot.robot_S06 import SzlabRobotS06Mixin
from unilabos.devices.workstation.szlab_poly_studio.s12_robot.robot_S07 import SzlabRobotS07Mixin
from unilabos.devices.workstation.szlab_poly_studio.s12_robot.robot_S08 import SzlabRobotS08Mixin
from unilabos.devices.workstation.szlab_poly_studio.s12_robot.robot_S09 import SzlabRobotS09Mixin
from unilabos.devices.workstation.szlab_poly_studio.s12_robot.robot_S10 import SzlabRobotS10Mixin
from unilabos.devices.workstation.szlab_poly_studio.s12_robot.robot_S11 import SzlabRobotS11Mixin


GRIPPER_ORIGIN_VARIABLE = "Robot_夹爪.原点位"
GRIPPER_STATUS_VARIABLE = "Robot_夹爪.夹爪状态"
GRIPPER_POSITION_VARIABLES = {
    "tip_box": "Robot_夹爪.TIP盒位",
    "beaker": "Robot_夹爪.烧杯位",
    "sample_vial_250ml": "Robot_夹爪.样品瓶250ml位",
    "sample_vial_500ml": "Robot_夹爪.样品瓶500ml位",
    "liquid_reagent_100ml": "Robot_夹爪.液体试剂瓶100ml位",
    "solid_powder": "Robot_夹爪.固体粉末位",
}

# S05 是单工位，错误放料会直接造成叠料；即使在调试模式也不能绕过该门禁。
_NON_SKIPPABLE_ROBOT_SENSOR_VARIABLES = frozenset({S05_MATERIAL_SENSOR})


@device(
    id="szlab_mixer_robot",
    display_name="SZLab Mixer 机器人任务",
    category=["robotic_arm"],
    description="SZLab Mixer 机器人任务设备，负责向 PLC 下发 S01-S11 取放料任务号",
)
class SzlabMixerRobotDevice(
    SzlabRobotS01Mixin,
    SzlabRobotS02Mixin,
    SzlabRobotS03Mixin,
    SzlabRobotS04Mixin,
    SzlabRobotS05Mixin,
    SzlabRobotS06Mixin,
    SzlabRobotS07Mixin,
    SzlabRobotS08Mixin,
    SzlabRobotS09Mixin,
    SzlabRobotS10Mixin,
    SzlabRobotS11Mixin,
):
    def __init__(
        self,
        plc_device_id: str = "szlab_poly_plc",
        poll_interval: float = 1.0,
        write_done_hold_seconds: float = 0.0,
        enable_gripper_check: bool = False,
        *args,
        **kwargs,
    ):
        self.plc_device_id = plc_device_id
        self.poll_interval = float(poll_interval)
        self.write_done_hold_seconds = float(write_done_hold_seconds)
        self.enable_gripper_check = bool(enable_gripper_check)
        self._plc_gateway = None
        self._last_task: dict[str, Any] = {}

    @not_action
    def set_plc_gateway(self, plc_gateway) -> None:
        self._plc_gateway = plc_gateway

    @not_action
    def _write_variable(self, name: str, value: Any) -> None:
        if self._plc_gateway is None:
            raise RuntimeError("机器人任务需要注入 szlab_poly_plc 网关")
        self._plc_gateway.write_variable(name, value)

    @not_action
    def _read_variable(self, name: str, use_cache: bool = False) -> Any:
        if self._plc_gateway is None:
            raise RuntimeError("机器人任务需要注入 szlab_poly_plc 网关")
        return self._plc_gateway.read_variable(name, use_cache=use_cache)

    @not_action
    def _plc_alarm_active(self) -> bool:
        checker = getattr(self._plc_gateway, "_mixing_wait_should_abort", None)
        return bool(checker()) if callable(checker) else False

    @not_action
    def _wait_variable_equal(
        self,
        name: str,
        expected: Any,
        interval: float | None = None,
    ) -> bool:
        waiter = getattr(self._plc_gateway, "wait_variable_equal", None) if self._plc_gateway is not None else None
        if callable(waiter):
            return bool(waiter(name, expected, interval=interval or self.poll_interval))

        poll_interval = self.poll_interval if interval is None else interval
        while True:
            if self._plc_alarm_active():
                return False
            if self._read_variable(name, use_cache=False) == expected:
                return True
            time.sleep(poll_interval)

    @not_action
    def _wait_variable_truthy(
        self,
        name: str,
        interval: float | None = None,
    ) -> tuple[bool, Any]:
        poll_interval = self.poll_interval if interval is None else interval
        waiter = getattr(self._plc_gateway, "wait_variable_equal", None) if self._plc_gateway is not None else None
        if callable(waiter):
            success = bool(waiter(name, True, interval=poll_interval))
            return success, True if success else None

        last_value = None
        while True:
            if self._plc_alarm_active():
                return False, last_value
            last_value = self._read_variable(name, use_cache=False)
            if bool(last_value):
                return True, last_value
            time.sleep(poll_interval)

    @not_action
    def _ensure_sensor_gate(self, sensor_variable: str, expected: bool, message: str) -> dict[str, Any] | None:
        if (
            os.environ.get("SKIP_SENSOR_PRECHECK") == "1"
            and sensor_variable not in _NON_SKIPPABLE_ROBOT_SENSOR_VARIABLES
        ):
            return None
        if not sensor_variable:
            return {"success": False, "message": "缺少精确传感器变量，不能执行机器人取放料动作"}
        if self._should_skip_robot_precheck_variable(sensor_variable):
            return None
        actual = bool(self._read_variable(sensor_variable, use_cache=False))
        if actual == expected:
            return None
        return {
            "success": False,
            "message": message,
            "sensor_variable": sensor_variable,
            "expected": expected,
            "actual": actual,
        }

    @not_action
    def _wait_sensor_conditions(
        self,
        conditions: dict[str, bool],
        *,
        phase: str,
    ) -> dict[str, Any]:
        if (
            os.environ.get("SKIP_SENSOR_PRECHECK") == "1"
            and not _NON_SKIPPABLE_ROBOT_SENSOR_VARIABLES.intersection(conditions)
        ):
            return {
                "success": True,
                "phase": phase,
                "skipped": True,
                "message": "已跳过机器人传感器检查",
                "conditions": conditions,
                "values": {},
            }

        active_conditions = {
            name: expected
            for name, expected in conditions.items()
            if not self._should_skip_robot_precheck_variable(name)
        }
        if not active_conditions:
            return {
                "success": True,
                "phase": phase,
                "skipped": True,
                "message": "配置中的传感器均已跳过",
                "conditions": conditions,
                "values": {},
            }
        if self._plc_gateway is None:
            raise RuntimeError("机器人任务需要注入 szlab_poly_plc 网关")

        waiter = getattr(self._plc_gateway, "wait_sensor_conditions", None)
        context = f"机器人{'前置' if phase == 'pre' else '后置'}传感器检查"
        if callable(waiter):
            success, values = waiter(
                active_conditions,
                interval=self.poll_interval,
                context=context,
            )
        else:
            success, values = wait_sensor_conditions(
                self._plc_gateway,
                active_conditions,
                interval=self.poll_interval,
                context=context,
            )
        mismatches = {
            name: {"expected": expected, "actual": values.get(name)}
            for name, expected in active_conditions.items()
            if values.get(name) != expected
        }
        return {
            "success": bool(success),
            "phase": phase,
            "conditions": active_conditions,
            "values": values,
            "mismatches": mismatches,
        }

    @not_action
    def _robot_sensor_requirements(
        self,
        task: str,
        station: str,
        data: dict[str, Any],
    ) -> tuple[dict[str, bool], dict[str, bool]]:
        if station == "S01":
            return {}, {}
        if station == "S072":
            return {}, {}

        custom_preconditions = data.get("pre_sensor_conditions")
        custom_postconditions = data.get("post_sensor_conditions")
        if custom_preconditions is not None or custom_postconditions is not None:
            return dict(custom_preconditions or {}), dict(custom_postconditions or {})

        if task == "place":
            target = str(data.get("target_sensor_variable") or "")
            if not target:
                raise RuntimeError(f"{station} 放料动作缺少目标位传感器")
            return {target: False}, {target: True}
        if task == "pick":
            source = str(data.get("source_sensor_variable") or "")
            if not source:
                raise RuntimeError(f"{station} 取料动作缺少来源位传感器")
            return {source: True}, {source: False}
        return {}, {}

    @not_action
    def _gripper_position_variable(self, task: str, station: str, data: dict[str, Any]) -> str:
        if station == "S08" and task == "pour":
            # 全流程中 S08 倒料时机械臂始终夹持烧杯；product_type 表示样品瓶规格。
            return GRIPPER_POSITION_VARIABLES["beaker"]
        if station == "S01":
            product_map = {
                1: "tip_box",
                2: "beaker",
                3: "sample_vial_250ml",
                4: "sample_vial_500ml",
                5: "liquid_reagent_100ml",
                6: "solid_powder",
            }
        elif station == "S02":
            return GRIPPER_POSITION_VARIABLES["tip_box"]
        elif station in {"S03", "S11"}:
            product_map = {1: "beaker", 2: "sample_vial_250ml", 3: "sample_vial_500ml"}
        elif station in {"S04", "S05", "S06"}:
            return GRIPPER_POSITION_VARIABLES["beaker"]
        elif station == "S071":
            return GRIPPER_POSITION_VARIABLES["solid_powder"]
        elif station == "S072":
            product_map = {1: "solid_powder", 2: "beaker"}
        elif station == "S08":
            product_map = {1: "sample_vial_250ml", 2: "sample_vial_500ml", 3: "liquid_reagent_100ml"}
        elif station == "S09":
            product_map = {1: "tip_box", 2: "liquid_reagent_100ml", 3: "beaker", 4: "beaker"}
        elif station == "S10":
            return GRIPPER_POSITION_VARIABLES["liquid_reagent_100ml"]
        else:
            raise ValueError(f"未配置 {station} {task} 的夹爪位置")

        product_type = int(data.get("product_type", 0))
        if product_type not in product_map:
            raise ValueError(f"{station} 夹爪检查不支持 product_type={product_type}")
        return GRIPPER_POSITION_VARIABLES[product_map[product_type]]

    @not_action
    def _gripper_requirements(
        self,
        task: str,
        station: str,
        data: dict[str, Any],
    ) -> tuple[tuple[str, tuple[int, ...]] | None, tuple[str, tuple[int, ...]] | None]:
        if not self.enable_gripper_check:
            return None, None
        product_position = self._gripper_position_variable(task, station, data)
        empty = (GRIPPER_ORIGIN_VARIABLE, (1,))
        holding = (product_position, (1, 2))
        if task == "pick":
            return empty, holding
        if task == "place":
            return holding, empty
        if task == "pour":
            return holding, holding
        return None, None

    @not_action
    def _wait_gripper_requirement(
        self,
        requirement: tuple[str, tuple[int, ...]],
        *,
        phase: str,
    ) -> dict[str, Any]:
        position_variable, allowed_statuses = requirement
        conditions: dict[str, Any] = {
            position_variable: True,
            GRIPPER_STATUS_VARIABLE: {"one_of": list(allowed_statuses)},
        }
        if os.environ.get("SKIP_SENSOR_PRECHECK") == "1":
            return {
                "success": True,
                "phase": phase,
                "skipped": True,
                "message": "已跳过机器人夹爪检查",
                "conditions": conditions,
                "values": {},
            }

        skip_position = self._should_skip_robot_precheck_variable(position_variable)
        skip_status = self._should_skip_robot_precheck_variable(GRIPPER_STATUS_VARIABLE)
        if skip_position and skip_status:
            return {
                "success": True,
                "phase": phase,
                "skipped": True,
                "message": "配置中的夹爪信号均已跳过",
                "conditions": conditions,
                "values": {},
            }

        values: dict[str, Any] = {}
        while True:
            if self._plc_alarm_active():
                return {
                    "success": False,
                    "phase": phase,
                    "message": "PLC 报警已中止机器人夹爪等待",
                    "conditions": conditions,
                    "values": values,
                }
            values = {}
            status_value = None
            if not skip_status:
                status_value = int(self._read_variable(GRIPPER_STATUS_VARIABLE, use_cache=False))
                values[GRIPPER_STATUS_VARIABLE] = status_value
                if status_value == 3:
                    return {
                        "success": False,
                        "phase": phase,
                        "message": "夹爪状态为 3，检测到物料掉落",
                        "conditions": conditions,
                        "values": values,
                        "mismatches": {
                            GRIPPER_STATUS_VARIABLE: {
                                "expected": list(allowed_statuses),
                                "actual": status_value,
                            }
                        },
                    }
                if status_value not in {0, 1, 2}:
                    return {
                        "success": False,
                        "phase": phase,
                        "message": f"未知夹爪状态: {status_value}",
                        "conditions": conditions,
                        "values": values,
                    }

            position_value = True
            if not skip_position:
                position_value = bool(self._read_variable(position_variable, use_cache=False))
                values[position_variable] = position_value
            status_matches = skip_status or status_value in allowed_statuses
            if position_value and status_matches:
                return {
                    "success": True,
                    "phase": phase,
                    "conditions": conditions,
                    "values": values,
                    "mismatches": {},
                }
            time.sleep(self.poll_interval)

    @not_action
    def _slot_number(self, position: str | int) -> int:
        if isinstance(position, int):
            return position
        row_text, col_text = str(position).split("-", maxsplit=1)
        row = int(row_text)
        col = int(col_text)
        if row < 1 or col < 1:
            raise ValueError(f"位置编号必须从 1 开始: {position}")
        return (row - 1) * 6 + col

    @not_action
    def _should_skip_robot_precheck_variable(self, variable_name: str) -> bool:
        if variable_name in _NON_SKIPPABLE_ROBOT_SENSOR_VARIABLES:
            return False
        raw_variables = os.environ.get("SKIP_ROBOT_PRECHECK_VARIABLES", "")
        skipped_variables = {
            item.strip()
            for item in raw_variables.replace(";", ",").split(",")
            if item.strip()
        }
        return variable_name in skipped_variables

    @not_action
    def _run_robot_handshake_precheck(self, target_station: str) -> dict[str, Any]:
        if os.environ.get("SKIP_ROBOT_HANDSHAKE_CHECK") == "1":
            return {
                "target_station": target_station,
                "skipped": True,
                "message": "已跳过 Robot_Home 和 Robot_任务允许写入前置检查",
            }

        status: dict[str, Any] = {
            "target_station": target_station,
            ROBOT_HOME_VARIABLE: None,
            ROBOT_WRITE_ALLOWED_VARIABLE: None,
        }
        if self._should_skip_robot_precheck_variable(ROBOT_HOME_VARIABLE):
            status[ROBOT_HOME_VARIABLE] = "skipped"
        else:
            home_ready, home_value = self._wait_variable_truthy(
                ROBOT_HOME_VARIABLE,
                interval=self.poll_interval,
            )
            status[ROBOT_HOME_VARIABLE] = home_value
            if not home_ready:
                raise RuntimeError(f"等待 {ROBOT_HOME_VARIABLE} 为 True 失败")

        allowed, allowed_value = self._wait_variable_truthy(
            ROBOT_WRITE_ALLOWED_VARIABLE,
            interval=self.poll_interval,
        )
        status[ROBOT_WRITE_ALLOWED_VARIABLE] = allowed_value
        if not allowed:
            raise RuntimeError(f"等待 {ROBOT_WRITE_ALLOWED_VARIABLE} 为 True 失败")
        return status

    @not_action
    def _wait_robot_task_complete(self, task_number: int) -> tuple[bool, str, Any]:
        if os.environ.get("SKIP_ROBOT_HANDSHAKE_CHECK") == "1":
            return True, "已跳过 Robot_任务完成等待", None
        expected = int(task_number)
        success = self._wait_variable_equal(
            ROBOT_TASK_COMPLETE_VARIABLE,
            expected,
            interval=self.poll_interval,
        )
        if success:
            return True, f"{ROBOT_TASK_COMPLETE_VARIABLE} == {expected}", expected
        actual = None
        try:
            actual = self._read_variable(ROBOT_TASK_COMPLETE_VARIABLE, use_cache=False)
        except Exception:
            actual = None
        return False, f"等待 {ROBOT_TASK_COMPLETE_VARIABLE} == {expected} 失败", actual

    @not_action
    def _reset_pc_to_plc_variables(
        self,
        reset_variables: dict[str, Any],
        verify: bool = False,
        max_attempts: int = 3,
    ) -> dict[str, Any]:
        reset_writes: dict[str, Any] = {}
        readback: dict[str, Any] = {}
        errors: dict[str, str] = {}
        attempts = max(1, int(max_attempts))
        for attempt in range(1, attempts + 1):
            errors = {}
            for name, value in reset_variables.items():
                try:
                    self._write_variable(name, value)
                    reset_writes[name] = value
                except Exception as exc:
                    errors[name] = str(exc)
            if errors or not verify:
                break
            readback = {}
            mismatches: dict[str, str] = {}
            for name, expected in reset_variables.items():
                try:
                    actual = self._read_variable(name, use_cache=False)
                    readback[name] = actual
                    if actual != expected:
                        mismatches[name] = f"期望 {expected!r}，实际 {actual!r}"
                except Exception as exc:
                    mismatches[name] = str(exc)
            errors = mismatches
            if not errors:
                break
            if attempt < attempts:
                time.sleep(min(self.poll_interval, 0.2))
        return {
            "success": not errors,
            "written_variables": reset_writes,
            "readback": readback,
            "errors": errors,
        }

    @not_action
    def _ensure_written_variables_nonzero(self, written_variables: dict[str, Any]) -> dict[str, Any]:
        names = [name for name in written_variables if name != ROBOT_WRITE_DONE_VARIABLE]
        readback: dict[str, Any] = {name: None for name in names}
        while True:
            if self._plc_alarm_active():
                raise RuntimeError("PLC 报警已中止机器人任务参数写入确认")
            zero_variables = {}
            for name in names:
                value = self._read_variable(name, use_cache=False)
                readback[name] = value
                if not bool(value):
                    zero_variables[name] = value
            if not zero_variables:
                return readback
            time.sleep(min(self.poll_interval, 0.2))

    @not_action
    def _submit_robot_task(
        self,
        task: str,
        station: str,
        task_number: int,
        variables: dict[str, Any] | None = None,
        reset_variables: dict[str, Any] | None = None,
        precheck=None,
        verify_reset: bool = False,
        **data: Any,
    ) -> dict[str, Any]:
        reset_variables = reset_variables or {ROBOT_TASK_NUMBER_VARIABLE: 0}
        try:
            pre_sensor_conditions, post_sensor_conditions = self._robot_sensor_requirements(task, station, data)
            pre_gripper_requirement, post_gripper_requirement = self._gripper_requirements(task, station, data)
        except Exception as exc:
            result = {
                "success": False,
                "message": str(exc),
                "task": task,
                "station": station,
                "task_number": int(task_number),
                "status": "rejected",
                **data,
            }
            self._last_task = result
            return result

        sensor_precheck = None
        if pre_sensor_conditions:
            try:
                sensor_precheck = self._wait_sensor_conditions(pre_sensor_conditions, phase="pre")
            except Exception as exc:
                result = {
                    "success": False,
                    "message": f"机器人动作前传感器读取失败: {exc}",
                    "task": task,
                    "station": station,
                    "task_number": int(task_number),
                    "status": "rejected",
                    **data,
                }
                self._last_task = result
                return result
            if not sensor_precheck["success"]:
                result = {
                    "success": False,
                    "message": f"{station} {task} 前置传感器状态等待失败",
                    "task": task,
                    "station": station,
                    "task_number": int(task_number),
                    "status": "rejected",
                    "sensor_precheck": sensor_precheck,
                    **data,
                }
                self._last_task = result
                return result
        elif precheck is not None:
            precheck_result = precheck()
            if precheck_result is not None:
                self._last_task = {**precheck_result, "status": "rejected"}
                return precheck_result

        gripper_precheck = None
        if pre_gripper_requirement is not None:
            try:
                gripper_precheck = self._wait_gripper_requirement(pre_gripper_requirement, phase="pre")
            except Exception as exc:
                gripper_precheck = {
                    "success": False,
                    "phase": "pre",
                    "message": str(exc),
                    "values": {},
                }
            if not gripper_precheck["success"]:
                result = {
                    "success": False,
                    "message": gripper_precheck.get("message") or f"{station} {task} 夹爪前置检查失败",
                    "task": task,
                    "station": station,
                    "task_number": int(task_number),
                    "status": "rejected",
                    "sensor_precheck": sensor_precheck,
                    "gripper_precheck": gripper_precheck,
                    **data,
                }
                self._last_task = result
                return result

        try:
            handshake_precheck = self._run_robot_handshake_precheck(station)
        except Exception as exc:
            message = str(exc)
            self._last_task = {
                "task": task,
                "station": station,
                "task_number": int(task_number),
                "status": "rejected",
                "handshake_message": message,
                **data,
            }
            return {"success": False, "message": message, **self._last_task}

        written_variables: dict[str, Any] = {}
        try:
            for name, value in (variables or {}).items():
                int_value = int(value)
                self._write_variable(name, int_value)
                written_variables[name] = int_value
            self._write_variable(ROBOT_TASK_NUMBER_VARIABLE, int(task_number))
            written_variables[ROBOT_TASK_NUMBER_VARIABLE] = int(task_number)
            self._write_variable(ROBOT_WRITE_DONE_VARIABLE, False)
            written_variables[ROBOT_WRITE_DONE_VARIABLE] = False
            write_readback = self._ensure_written_variables_nonzero(written_variables)
            self._write_variable(ROBOT_WRITE_DONE_VARIABLE, True)
            written_variables[ROBOT_WRITE_DONE_VARIABLE] = True
            if self.write_done_hold_seconds > 0:
                time.sleep(self.write_done_hold_seconds)
        except Exception as exc:
            rollback_variables = {
                ROBOT_WRITE_DONE_VARIABLE: False,
                **{
                    name: reset_variables[name]
                    for name in written_variables
                    if name in reset_variables
                },
            }
            reset_result = self._reset_pc_to_plc_variables(
                rollback_variables,
                verify=verify_reset,
            )
            self._last_task = {
                "task": task,
                "station": station,
                "task_number": int(task_number),
                "status": "write_failed",
                "written_variables": written_variables,
                "handshake_precheck": handshake_precheck,
                "reset": reset_result,
                **data,
            }
            return {"success": False, "message": str(exc), **self._last_task}

        complete_success, complete_message, complete_value = self._wait_robot_task_complete(task_number)
        if os.environ.get("SKIP_RESET_AFTER_RUN") == "1":
            try:
                self._write_variable(ROBOT_WRITE_DONE_VARIABLE, False)
            except Exception:
                pass
            reset_result = {
                "success": True,
                "written_variables": {ROBOT_WRITE_DONE_VARIABLE: False},
                "errors": {},
                "skipped": True,
                "message": "已跳过任务完成后的参数复位，仅复位 Robot_任务写入完成",
            }
        else:
            reset_result = self._reset_pc_to_plc_variables(
                {ROBOT_WRITE_DONE_VARIABLE: False, **reset_variables},
                verify=verify_reset,
            )
        status = "completed" if complete_success and reset_result["success"] else "failed"

        self._last_task = {
            "task": task,
            "station": station,
            "task_number": int(task_number),
            "status": status,
            "written_variables": written_variables,
            "write_readback": write_readback,
            "completion_variable": ROBOT_TASK_COMPLETE_VARIABLE,
            "completion_value": complete_value,
            "completion_message": complete_message,
            "handshake_precheck": handshake_precheck,
            "sensor_precheck": sensor_precheck,
            "gripper_precheck": gripper_precheck,
            "reset": reset_result,
            **data,
        }
        if not complete_success:
            return {
                "success": False,
                "message": complete_message,
                **self._last_task,
            }
        if not reset_result["success"]:
            return {
                "success": False,
                "message": "机器人任务已完成，但 PC->PLC 变量复位失败",
                **self._last_task,
            }

        if post_gripper_requirement is not None:
            try:
                gripper_postcheck = self._wait_gripper_requirement(post_gripper_requirement, phase="post")
            except Exception as exc:
                gripper_postcheck = {
                    "success": False,
                    "phase": "post",
                    "message": str(exc),
                    "values": {},
                }
            self._last_task["gripper_postcheck"] = gripper_postcheck
            if not gripper_postcheck["success"]:
                self._last_task["status"] = "verification_failed"
                return {
                    "success": False,
                    "message": gripper_postcheck.get("message") or "机器人任务已完成，但夹爪后置检查失败",
                    **self._last_task,
                }
        sensor_postcheck = None
        if post_sensor_conditions:
            try:
                sensor_postcheck = self._wait_sensor_conditions(post_sensor_conditions, phase="post")
            except Exception as exc:
                sensor_postcheck = {
                    "success": False,
                    "phase": "post",
                    "message": str(exc),
                    "conditions": post_sensor_conditions,
                    "values": {},
                }
            self._last_task["sensor_postcheck"] = sensor_postcheck
            if not sensor_postcheck["success"]:
                self._last_task["status"] = "verification_failed"
                return {
                    "success": False,
                    "message": "机器人任务已完成，但现场传感器状态验证失败；禁止自动重试",
                    **self._last_task,
                }
        return {
            "success": True,
            "message": f"机器人任务已完成: {station} {task}",
            **self._last_task,
        }

    @action(auto_prefix=True, description="S01 取料")
    def submit_pick_from_s01(
        self,
        product_type: int = 1,
        position: int = 1,
    ) -> dict[str, Any]:
        try:
            return self._run_s01_pick(product_type, position)
        except Exception as exc:
            return {"success": False, "message": str(exc), "task": "pick", "station": "S01", "position": position}

    @action(auto_prefix=True, description="S02 放 TIP")
    def submit_place_to_s02(self, position: int = 1) -> dict[str, Any]:
        try:
            return self._run_s02_place(position)
        except Exception as exc:
            return {"success": False, "message": str(exc), "task": "place", "station": "S02", "position": position}

    @action(auto_prefix=True, description="S02 取 TIP")
    def submit_pick_from_s02(self, position: int = 1) -> dict[str, Any]:
        try:
            return self._run_s02_pick(position)
        except Exception as exc:
            return {"success": False, "message": str(exc), "task": "pick", "station": "S02", "position": position}

    @action(auto_prefix=True, description="S03 放容器")
    def submit_place_to_s03(self, product_type: int = 1, position: str = "1-1") -> dict[str, Any]:
        try:
            return self._run_s03_place(product_type, position)
        except Exception as exc:
            return {"success": False, "message": str(exc), "task": "place", "station": "S03", "position": position}

    @action(auto_prefix=True, description="S03 取容器")
    def submit_pick_from_s03(self, product_type: int = 1, position: str = "1-1") -> dict[str, Any]:
        try:
            return self._run_s03_pick(product_type, position)
        except Exception as exc:
            return {"success": False, "message": str(exc), "task": "pick", "station": "S03", "position": position}

    @action(auto_prefix=True, description="S04 放料")
    def submit_place_to_s04(self, position: int = 1, sample_id: str = "") -> dict[str, Any]:
        try:
            return self._run_s04_place(position=position, sample_id=sample_id)
        except Exception as exc:
            return {
                "success": False,
                "message": str(exc),
                "task": "place",
                "station": "S04",
                "position": position,
                "sample_id": sample_id,
            }

    @action(auto_prefix=True, description="S04 取料")
    def submit_pick_from_s04(self, position: int = 1) -> dict[str, Any]:
        try:
            return self._run_s04_pick(position=position)
        except Exception as exc:
            return {"success": False, "message": str(exc), "task": "pick", "station": "S04", "position": position}

    @action(auto_prefix=True, description="S05 放料")
    def submit_place_to_s05(self, sample_id: str = "") -> dict[str, Any]:
        try:
            return self._run_s05_place(sample_id=sample_id)
        except Exception as exc:
            return {
                "success": False,
                "message": str(exc),
                "task": "place",
                "station": "S05",
                "sample_id": sample_id,
            }

    @action(auto_prefix=True, description="S05 取料")
    def submit_pick_from_s05(self, sample_id: str = "") -> dict[str, Any]:
        try:
            return self._run_s05_pick(sample_id=sample_id)
        except Exception as exc:
            return {"success": False, "message": str(exc), "task": "pick", "station": "S05", "sample_id": sample_id}

    @action(auto_prefix=True, description="S06 放料")
    def submit_place_to_s06(self) -> dict[str, Any]:
        try:
            return self._run_s06_place()
        except Exception as exc:
            return {"success": False, "message": str(exc), "task": "place", "station": "S06"}

    @action(auto_prefix=True, description="S06 取料")
    def submit_pick_from_s06(self) -> dict[str, Any]:
        try:
            return self._run_s06_pick()
        except Exception as exc:
            return {"success": False, "message": str(exc), "task": "pick", "station": "S06"}

    @action(auto_prefix=True, description="S071 放粉罐")
    def submit_place_to_s071(self, position: str = "1-1") -> dict[str, Any]:
        try:
            return self._run_s071_place(position)
        except Exception as exc:
            return {"success": False, "message": str(exc), "task": "place", "station": "S071", "position": position}

    @action(auto_prefix=True, description="S071 取粉罐")
    def submit_pick_from_s071(self, position: str = "1-1") -> dict[str, Any]:
        try:
            return self._run_s071_pick(position)
        except Exception as exc:
            return {"success": False, "message": str(exc), "task": "pick", "station": "S071", "position": position}

    @action(auto_prefix=True, description="并行执行 S071 取粉罐与 S07 旋转到上料位")
    def submit_pick_from_s071_and_rotate_to_feed(
        self,
        position: str = "1-1",
        load_position: int = 1,
    ) -> dict[str, Any]:
        try:
            return self._run_s071_pick_and_rotate_to_feed(position, load_position)
        except Exception as exc:
            return {
                "success": False,
                "message": str(exc),
                "status": "rejected",
                "position": position,
                "load_position": load_position,
            }

    @action(auto_prefix=True, description="S072 放料")
    def submit_place_to_s072(self, product_type: int = 1, position: int = 1) -> dict[str, Any]:
        try:
            return self._run_s072_place(product_type, position)
        except Exception as exc:
            return {"success": False, "message": str(exc), "task": "place", "station": "S072", "position": position}

    @action(auto_prefix=True, description="S072 取料")
    def submit_pick_from_s072(self, product_type: int = 1, position: int = 1) -> dict[str, Any]:
        try:
            return self._run_s072_pick(product_type, position)
        except Exception as exc:
            return {"success": False, "message": str(exc), "task": "pick", "station": "S072", "position": position}

    @action(auto_prefix=True, description="S08 放瓶")
    def submit_place_to_s08(
        self,
        product_type: int = 1,
        position: int = 1,
    ) -> dict[str, Any]:
        try:
            return self._run_s08_place(product_type, position)
        except Exception as exc:
            return {"success": False, "message": str(exc), "task": "place", "station": "S08", "position": position}

    @action(auto_prefix=True, description="S08 取瓶")
    def submit_pick_from_s08(
        self,
        product_type: int = 1,
        position: int = 1,
    ) -> dict[str, Any]:
        try:
            return self._run_s08_pick(product_type, position)
        except Exception as exc:
            return {"success": False, "message": str(exc), "task": "pick", "station": "S08", "position": position}

    @action(auto_prefix=True, description="S08 倒料")
    def submit_pour_from_s08(self, product_type: int = 1) -> dict[str, Any]:
        try:
            return self._run_s08_pour(product_type)
        except Exception as exc:
            return {"success": False, "message": str(exc), "task": "pour", "station": "S08", "product_type": product_type}

    @action(
        auto_prefix=True,
        description="S09 放料",
        handles=[
            ActionInputHandle(
                key="product_type",
                data_type="szlab_s09_product_type",
                label="S09取放料产品",
                data_key="product_type",
                data_source=DataSource.HANDLE,
                description="S09取放料产品：1=TIP盒，2=液体试剂瓶，3=烧杯，4=测密度烧杯",
            ),
            ActionInputHandle(
                key="position",
                data_type="szlab_s09_position",
                label="S09取放料编号",
                data_key="position",
                data_source=DataSource.HANDLE,
                description="S09取放料编号：TIP盒 1-2，液体试剂瓶 1-5，烧杯 1",
            ),
        ],
    )
    def submit_place_to_s09(self, product_type: int = 1, position: int = 1) -> dict[str, Any]:
        try:
            return self._run_s09_place(product_type, position)
        except Exception as exc:
            return {"success": False, "message": str(exc), "task": "place", "station": "S09", "position": position}

    @action(
        auto_prefix=True,
        description="S09 取料",
        handles=[
            ActionInputHandle(
                key="product_type",
                data_type="szlab_s09_product_type",
                label="S09取放料产品",
                data_key="product_type",
                data_source=DataSource.HANDLE,
                description="S09取放料产品：1=TIP盒，2=液体试剂瓶，3=烧杯，4=测密度烧杯",
            ),
            ActionInputHandle(
                key="position",
                data_type="szlab_s09_position",
                label="S09取放料编号",
                data_key="position",
                data_source=DataSource.HANDLE,
                description="S09取放料编号：TIP盒 1-2，液体试剂瓶 1-5，烧杯 1",
            ),
        ],
    )
    def submit_pick_from_s09(self, product_type: int = 1, position: int = 1) -> dict[str, Any]:
        try:
            return self._run_s09_pick(product_type, position)
        except Exception as exc:
            return {"success": False, "message": str(exc), "task": "pick", "station": "S09", "position": position}

    @action(auto_prefix=True, description="S10 放试剂瓶")
    def submit_place_to_s10(self, position: int = 1) -> dict[str, Any]:
        try:
            return self._run_s10_place(position)
        except Exception as exc:
            return {"success": False, "message": str(exc), "task": "place", "station": "S10", "position": position}

    @action(auto_prefix=True, description="S10 取试剂瓶")
    def submit_pick_from_s10(self, position: int = 1) -> dict[str, Any]:
        try:
            return self._run_s10_pick(position)
        except Exception as exc:
            return {"success": False, "message": str(exc), "task": "pick", "station": "S10", "position": position}

    @action(auto_prefix=True, description="S11 放成品")
    def submit_place_to_s11(self, product_type: int = 1, position: str = "1-1") -> dict[str, Any]:
        try:
            return self._run_s11_place(product_type, position)
        except Exception as exc:
            return {"success": False, "message": str(exc), "task": "place", "station": "S11", "position": position}

    @action(auto_prefix=True, description="S11 取成品")
    def submit_pick_from_s11(self, product_type: int = 1, position: str = "1-1") -> dict[str, Any]:
        try:
            return self._run_s11_pick(product_type, position)
        except Exception as exc:
            return {"success": False, "message": str(exc), "task": "pick", "station": "S11", "position": position}

    @action(auto_prefix=True, description="读取最近一次机器人任务提交记录")
    def last_submitted_task(self) -> dict[str, Any]:
        return {"success": True, "task": self._last_task}
