from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from typing import Any

from unilabos.devices.workstation.szlab_poly_studio.sensor import wait_sensor_conditions
from unilabos.registry.decorators import action, device, not_action, topic_config

from .sensors import (
    S09_ASPIRATE_BALANCE_READINGS_VAR,
    S09_ALLOW_PROCESS_VAR,
    S09_ASPIRATE_VOLUME_VAR,
    S09_BALANCE_READING_VAR,
    S09_BALANCE_STABLE_VAR,
    S09_DISPENSE_VOLUME_VAR,
    S09_DISPENSE_BALANCE_READINGS_VAR,
    S09_DENSITY_COUNT_VAR,
    S09_HOME_LABELS,
    S09_HOME_SIGNALS,
    S09_LIQUID_BOTTLE_VAR,
    S09_PARAM_WRITTEN_VAR,
    S09_PROCESS_DONE_VAR,
    S09_PROCESS_LABELS,
    S09_PROCESS_SELECT_VAR,
    S09_STATION_STATUS_VAR,
    S09_STATION_SENSORS,
    S09_TIP_BOX_VAR,
    S09_TIP_BOX_SENSORS,
    S09_TIP_VAR,
    s09_opcua_node_id_map,
    s09_density_balance_vars,
    s09_remaining_volume_var,
    s09_remaining_volume_vars,
    validate_home_position,
    validate_density_count,
    validate_liquid_bottle,
    validate_process,
    validate_station,
    validate_tip,
    validate_tip_box,
)
from .tip_reuse_state import ReusableTipStateStore

DEFAULT_OPCUA_URL = os.environ.get(
    "UNILABOS_SZLAB_MIXER_OPCUA_URL",
    "opc.tcp://192.168.1.10:4840/",
)
DEFAULT_TIP_REUSE_STATE_PATH = Path(
    os.environ.get(
        "UNILABOS_S09_TIP_REUSE_STATE_PATH",
        str(
            Path(__file__).resolve().parents[5]
            / "workflow_artifacts"
            / "s09_tip_reuse_state.json"
        ),
    )
)
S09_VOLUME_RAW_MAX = 50000
S09_VOLUME_UL_MAX = 5000.0


@device(
    id="szlab_mixer_pipetting_station",
    display_name="SZLab 移液站",
    category=["liquid_handler"],
    description="SZLab Poly Studio S09 移液/加液工位设备",
)
class SzlabMixerPipettingStationDevice:
    def __init__(
        self,
        url: str = DEFAULT_OPCUA_URL,
        username: str | None = None,
        password: str | None = None,
        csv_path: str | None = "szlab_plc_0628_addnodeid.csv",
        auto_connect: bool = True,
        plc_device_id: str = "szlab_poly_plc",
        use_plc_gateway: bool = False,
        opcua_client: Any | None = None,
        opcua_node_id_map: dict[str, str] | None = None,
        tip_reuse_state_path: str = str(DEFAULT_TIP_REUSE_STATE_PATH),
        tip_max_use_count: int = 20,
        **kwargs,
    ):
        self.url = url
        self.plc_device_id = plc_device_id
        self._plc_gateway = None
        self._status = "Idle"
        self._bindings: dict[int, str] = {}
        self._last_process: dict[str, Any] = {}
        self._tip_reuse_state = ReusableTipStateStore(
            tip_reuse_state_path,
            max_use_count=tip_max_use_count,
        )
        self._tip_reuse_execution_lock = threading.RLock()

        if use_plc_gateway:
            self._client = opcua_client
            return
        if opcua_client is not None:
            self._client = opcua_client
            return

        client_kwargs: dict[str, Any] = {
            "url": url,
            "username": username,
            "password": password,
            "auto_connect": auto_connect,
            "opcua_node_id_map": opcua_node_id_map or s09_opcua_node_id_map(),
        }
        if csv_path is not None:
            client_kwargs["csv_path"] = csv_path
        from unilabos.devices.workstation.szlab_poly_studio.plc import SZLabPolyPLCDevice

        self._client = SZLabPolyPLCDevice(**client_kwargs)

    @not_action
    def set_plc_gateway(self, plc_gateway) -> None:
        self._plc_gateway = plc_gateway

    @property
    @topic_config()
    def status(self) -> str:
        return self._status

    @not_action
    def disconnect(self) -> None:
        if self._client is not None and hasattr(self._client, "disconnect"):
            self._client.disconnect()

    @not_action
    def _target(self):
        return self._plc_gateway if self._plc_gateway is not None else self._client

    @not_action
    def get_variables(self, variable_names: list[str], use_cache: bool = False) -> dict[str, Any]:
        target = self._target()
        if hasattr(target, "get_variables"):
            return target.get_variables(variable_names, use_cache=use_cache)
        values: dict[str, Any] = {}
        for name in variable_names:
            try:
                values[name] = {"success": True, "value": self._read_variable(name, use_cache=use_cache)}
            except Exception as exc:
                values[name] = {"success": False, "error": str(exc)}
        return values

    @not_action
    def get_opc_variable_metadata(self, variable_name: str) -> tuple[str, str | None]:
        target = self._target()
        if hasattr(target, "get_opc_variable_metadata"):
            return target.get_opc_variable_metadata(variable_name)
        return variable_name, None

    @not_action
    def _read_variable(self, name: str, use_cache: bool = False) -> Any:
        target = self._target()
        if hasattr(target, "read_variable"):
            return target.read_variable(name, use_cache=use_cache)
        return target.read(name)

    @not_action
    def _write_variable(self, name: str, value: Any) -> None:
        target = self._target()
        if hasattr(target, "write_variable"):
            target.write_variable(name, value)
            return
        target.write(name, value)

    @not_action
    def _pulse_variable(self, name: str, value: Any = True, reset_value: Any = False, reset_delay: float = 0.1) -> None:
        target = self._target()
        if hasattr(target, "pulse"):
            target.pulse(name, value=value, reset_value=reset_value, reset_delay=reset_delay)
            return
        self._write_variable(name, value)
        if reset_delay:
            time.sleep(reset_delay)
        self._write_variable(name, reset_value)

    @not_action
    def _wait_equal(self, name: str, expected: Any, interval: float = 0.2) -> bool:
        target = self._target()
        if hasattr(target, "wait_equal"):
            return target.wait_equal(name, expected, interval=interval)
        if hasattr(target, "wait_variable_equal"):
            return target.wait_variable_equal(name, expected, interval=interval)
        while True:
            abort_check = getattr(target, "_mixing_wait_should_abort", None)
            if callable(abort_check) and abort_check():
                return False
            if self._read_variable(name, use_cache=False) == expected:
                return True
            time.sleep(interval)

    @not_action
    def _wait_process_done(self, process: int) -> bool:
        expected = int(process)
        try:
            current = self._read_variable(S09_PROCESS_DONE_VAR, use_cache=False)
        except Exception:
            current = None
        if current == expected and not self._wait_equal(S09_PROCESS_DONE_VAR, 0):
            return False
        return self._wait_equal(S09_PROCESS_DONE_VAR, expected)

    @not_action
    def _wait_allow_process(self) -> bool:
        return self._wait_equal(S09_ALLOW_PROCESS_VAR, True)

    @not_action
    def _material_conditions_for_process(
        self,
        process: int,
        *,
        tip_box_index: int,
        liquid_bottle_index: int,
        station: int,
    ) -> dict[str, bool]:
        if process in {5, 6}:
            return {S09_TIP_BOX_SENSORS[validate_tip_box(tip_box_index)]: True}
        if process == 7:
            return {S09_STATION_SENSORS[validate_liquid_bottle(liquid_bottle_index)]: True}
        return {}

    @not_action
    def _wait_material_conditions(
        self,
        conditions: dict[str, bool],
        *,
        phase: str,
    ) -> dict[str, Any]:
        target = self._target()
        phase_labels = {
            "pre": "前置",
            "post": "后置",
            "workflow_pre": "流程前置",
        }
        context = f"S09 加液{phase_labels.get(phase, phase)}传感器检查"
        waiter = getattr(target, "wait_sensor_conditions", None)
        if callable(waiter):
            success, values = waiter(
                conditions,
                interval=0.2,
                context=context,
            )
        else:
            success, values = wait_sensor_conditions(
                target,
                conditions,
                interval=0.2,
                context=context,
            )
        return {
            "success": bool(success),
            "phase": phase,
            "conditions": conditions,
            "values": values,
            "mismatches": {
                name: {"expected": expected, "actual": values.get(name)}
                for name, expected in conditions.items()
                if values.get(name) != expected
            },
        }

    @not_action
    def _append_log(
        self,
        logs: list[dict[str, Any]],
        message: str,
        detail: dict[str, Any] | None = None,
    ) -> None:
        logs.append({"message": message, "detail": detail or {}})

    @not_action
    def _clear_process_params(self, process: int) -> dict[str, Any]:
        writes: dict[str, Any] = {}
        errors: dict[str, str] = {}
        clear_values = [
            (S09_PARAM_WRITTEN_VAR, False),
            (S09_PROCESS_SELECT_VAR, 0),
            (S09_TIP_BOX_VAR, 0),
            (S09_TIP_VAR, 0),
            (S09_LIQUID_BOTTLE_VAR, 0),
            (S09_ASPIRATE_VOLUME_VAR, 0),
            (S09_DISPENSE_VOLUME_VAR, 0),
        ]
        if int(process) == 9:
            clear_values.append((S09_DENSITY_COUNT_VAR, 0))
        for name, value in clear_values:
            try:
                self._write_variable(name, value)
                writes[name] = value
            except Exception as exc:
                errors[name] = str(exc)
        return {"success": not errors, "written_variables": writes, "errors": errors}

    @not_action
    def _volume_to_raw(self, volume: int | float, volume_unit: str = "raw") -> int:
        unit = str(volume_unit).strip().lower()
        value = float(volume)
        if unit in {"raw", "int", "int16", "plc", "0.1ul", "0.1µl"}:
            raw = int(round(value))
        elif unit in {"ul", "µl", "μl", "microliter", "microliters"}:
            raw = int(round(value * 10))
        elif unit in {"ml", "milliliter", "milliliters"}:
            raw = int(round(value * 1000 * 10))
        else:
            raise ValueError("S09 体积单位必须是 raw、uL 或 mL")
        if raw < 0:
            raise ValueError("S09 抽液量/放液量不能为负数")
        return raw

    @not_action
    def _raw_volume_to_ul(self, raw_volume: int) -> float:
        return int(raw_volume) / 10.0

    @not_action
    def _raw_volume_to_ml(self, raw_volume: int) -> float:
        return int(raw_volume) / 10000.0

    @not_action
    def _read_remaining_volume(self, bottle: int) -> float:
        bottle = validate_liquid_bottle(bottle)
        return float(self._read_variable(s09_remaining_volume_var(bottle), use_cache=False))

    @not_action
    def _ensure_sufficient_remaining_volume(
        self,
        bottle: int,
        raw_volume: int,
    ) -> dict[str, Any]:
        bottle = validate_liquid_bottle(bottle)
        current = self._read_remaining_volume(bottle)
        needed_ml = self._raw_volume_to_ml(raw_volume)
        if current + 1e-9 < needed_ml:
            raise ValueError(
                f"S09 液体瓶 {bottle} 剩余液量不足：当前 {current:g} mL，需要 {needed_ml:g} mL"
            )
        return {
            "bottle": bottle,
            "variable": s09_remaining_volume_var(bottle),
            "remaining_volume": current,
            "required_volume_ml": needed_ml,
        }

    @not_action
    def _deduct_remaining_volume(self, bottle: int, raw_volume: int) -> dict[str, Any]:
        bottle = validate_liquid_bottle(bottle)
        variable = s09_remaining_volume_var(bottle)
        current = self._read_remaining_volume(bottle)
        deduct_ml = self._raw_volume_to_ml(raw_volume)
        new_volume = round(current - deduct_ml, 6)
        if new_volume < 0:
            raise ValueError(
                f"S09 液体瓶 {bottle} 剩余液量不足：当前 {current:g} mL，需要 {deduct_ml:g} mL"
            )
        self._write_variable(variable, new_volume)
        return {
            "bottle": bottle,
            "variable": variable,
            "previous_remaining_volume": current,
            "deducted_volume_ml": deduct_ml,
            "remaining_volume": new_volume,
        }

    @not_action
    def _write_configured_remaining_volumes(
        self,
        remaining_volumes: dict[int, float | None],
    ) -> dict[str, float]:
        written: dict[str, float] = {}
        for bottle, remaining_volume in remaining_volumes.items():
            if remaining_volume is None:
                continue
            bottle = validate_liquid_bottle(bottle)
            value = float(remaining_volume)
            if value < 0:
                raise ValueError("S09 液体瓶剩余液量不能为负数")
            variable = s09_remaining_volume_var(bottle)
            self._write_variable(variable, value)
            written[variable] = value
        return written

    @not_action
    def _split_raw_volume(self, raw_volume: int) -> list[int]:
        raw_volume = int(raw_volume)
        chunks: list[int] = []
        remaining = raw_volume
        while remaining > 0:
            chunk = min(remaining, S09_VOLUME_RAW_MAX)
            chunks.append(chunk)
            remaining -= chunk
        return chunks or [0]

    @not_action
    def _validate_volumes(
        self,
        process: int,
        aspirate_volume: int | float,
        dispense_volume: int | float,
        volume_unit: str = "raw",
    ) -> tuple[int, int]:
        aspirate_volume = self._volume_to_raw(aspirate_volume, volume_unit)
        dispense_volume = self._volume_to_raw(dispense_volume, volume_unit)
        if process in {7, 9} and aspirate_volume <= 0:
            raise ValueError("S09 抽液量必须大于 0")
        if process == 8 and dispense_volume <= 0:
            raise ValueError("S09 放液量必须大于 0")
        if process in {7, 9} and aspirate_volume > S09_VOLUME_RAW_MAX:
            raise ValueError("S09 单次抽液量不能超过 5000 uL；业务加液请使用 add_liquid 自动拆分")
        if process == 8 and dispense_volume > S09_VOLUME_RAW_MAX:
            raise ValueError("S09 单次放液量不能超过 5000 uL；业务加液请使用 add_liquid 自动拆分")
        return aspirate_volume, dispense_volume

    @not_action
    def _validate_process_params(
        self,
        process: int,
        tip_box_index: int,
        tip_index: int,
        liquid_bottle_index: int,
        station: int,
        aspirate_volume: int | float,
        dispense_volume: int | float,
        volume_unit: str = "raw",
    ) -> tuple[int, int, int, int, int, int, int]:
        process = validate_process(process)
        if process in {5, 6, 7, 8, 9}:
            tip_box_index = validate_tip_box(tip_box_index)
            tip_index = validate_tip(tip_index)
        else:
            tip_box_index = int(tip_box_index)
            tip_index = int(tip_index)
        if process == 7:
            liquid_bottle_index = validate_liquid_bottle(liquid_bottle_index)
        else:
            liquid_bottle_index = int(liquid_bottle_index)
        if process == 8:
            station = validate_station(station)
        else:
            station = int(station)
        aspirate_volume, dispense_volume = self._validate_volumes(
            process,
            aspirate_volume,
            dispense_volume,
            volume_unit=volume_unit,
        )
        return process, tip_box_index, tip_index, liquid_bottle_index, station, aspirate_volume, dispense_volume

    @action(auto_prefix=True, description="读取 S09 指定安全位原点信号")
    def check_home_position(self, home_position: int = 1) -> dict[str, Any]:
        try:
            home_position = validate_home_position(home_position)
            variable = S09_HOME_SIGNALS[home_position]
            is_home = bool(self._read_variable(variable, use_cache=False))
        except ValueError as exc:
            return {"success": False, "message": str(exc)}
        except Exception as exc:
            return {"success": False, "message": str(exc), "data": {"home_position": home_position}}
        return {
            "success": is_home,
            "message": f"S09 安全位{home_position} {'已到位' if is_home else '未到位'}",
            "data": {
                "home_position": home_position,
                "label": S09_HOME_LABELS[home_position],
                "variable": variable,
                "value": is_home,
            },
        }

    @action(auto_prefix=True, description="读取 S09 四个安全位原点信号")
    def read_home_positions(self) -> dict[str, Any]:
        values: dict[int, dict[str, Any]] = {}
        errors: dict[int, str] = {}
        for home_position, variable in S09_HOME_SIGNALS.items():
            try:
                value = bool(self._read_variable(variable, use_cache=False))
                values[home_position] = {
                    "home_position": home_position,
                    "label": S09_HOME_LABELS[home_position],
                    "variable": variable,
                    "value": value,
                }
            except Exception as exc:
                errors[home_position] = str(exc)
        return {
            "success": not errors,
            "message": "S09 四个安全位原点信号读取完成" if not errors else "S09 原点信号读取失败",
            "data": {"home_positions": values, "errors": errors},
        }

    @not_action
    def go_to_safe_position(self, home_position: int = 1, require_allow: bool = True) -> dict[str, Any]:
        try:
            home_position = validate_home_position(home_position)
        except ValueError as exc:
            return {"success": False, "message": str(exc)}
        result = self.run_process(process=home_position, require_allow=require_allow)
        if not result.get("success", False):
            return result
        logs = list(result.get("logs") or [])
        self._append_log(
            logs,
            f"等待机械臂到达 S09 安全位{home_position}",
            {"home_position": home_position, "home_signal": S09_HOME_SIGNALS[home_position]},
        )
        home_signal = S09_HOME_SIGNALS[home_position]
        try:
            is_home = self._wait_equal(home_signal, True)
        except Exception as exc:
            return {"success": False, "message": str(exc), "process": result, "logs": logs}
        home = {
            "success": bool(is_home),
            "message": f"S09 安全位{home_position}已确认" if is_home else f"S09 安全位{home_position}等待失败",
            "data": {
                "home_position": home_position,
                "label": S09_HOME_LABELS[home_position],
                "variable": home_signal,
                "value": is_home,
            },
        }
        self._append_log(
            logs,
            f"S09 安全位{home_position}原点信号读取完成",
            {"home_position": home_position, "result": home.get("data")},
        )
        return {**home, "process": result, "logs": logs}

    @action(auto_prefix=True, description="确认 S09 唯一加液工位空闲")
    def prepare_liquid_station(self) -> dict[str, Any]:
        try:
            is_idle = self._wait_equal(S09_STATION_STATUS_VAR, 2)
            station_status = int(self._read_variable(S09_STATION_STATUS_VAR, use_cache=False))
        except Exception as exc:
            return {"success": False, "message": str(exc)}
        return {
            "success": is_idle,
            "message": "S09 唯一加液工位空闲" if is_idle else f"S09 唯一加液工位非空闲，当前状态 {station_status}",
            "data": {"station_status": station_status, "station": 1},
        }

    @action(auto_prefix=True, description="读取 S09 允许加工（允许参数写入）信号")
    def read_allow_process(self) -> dict[str, Any]:
        try:
            allowed = bool(self._read_variable(S09_ALLOW_PROCESS_VAR, use_cache=False))
        except Exception as exc:
            return {"success": False, "message": str(exc)}
        return {
            "success": True,
            "message": "S09 允许加工信号读取完成",
            "data": {
                "allowed": allowed,
                "variable": S09_ALLOW_PROCESS_VAR,
            },
        }

    @action(auto_prefix=True, description="绑定样品到 S09 加液工位（占位）")
    def bind_sample_to_station(self, sample_id: str = "") -> dict[str, Any]:
        return {
            "success": True,
            "message": "S09 样品绑定逻辑暂未启用",
            "data": {"sample_id": sample_id, "enabled": False},
        }

    @action(auto_prefix=True, description="释放 S09 加液工位绑定（占位）")
    def release_station(self) -> dict[str, Any]:
        return {
            "success": True,
            "message": "S09 样品解绑逻辑暂未启用",
            "data": {"enabled": False},
        }

    @action(auto_prefix=True, description="执行 S09 单个 PLC 工艺")
    def run_process(
        self,
        process: int = 5,
        tip_box_index: int = 1,
        tip_index: int = 1,
        liquid_bottle_index: int = 1,
        station: int = 1,
        aspirate_volume: int = 0,
        dispense_volume: int = 0,
        volume_unit: str = "raw",
        require_allow: bool = False,
        skip_level_check: bool = False,
        reset_delay: float = 0.1,
        read_balance_after_done: bool | None = None,
        density_measurement_count: int = 1,
    ) -> dict[str, Any]:
        logs: list[dict[str, Any]] = []
        try:
            density_measurement_count = validate_density_count(density_measurement_count)
            (
                process,
                tip_box_index,
                tip_index,
                liquid_bottle_index,
                station,
                aspirate_volume,
                dispense_volume,
            ) = self._validate_process_params(
                process,
                tip_box_index,
                tip_index,
                liquid_bottle_index,
                station,
                aspirate_volume,
                dispense_volume,
                volume_unit,
            )
        except ValueError as exc:
            return {"success": False, "message": str(exc)}

        material_conditions = self._material_conditions_for_process(
            process,
            tip_box_index=tip_box_index,
            liquid_bottle_index=liquid_bottle_index,
            station=station,
        )
        try:
            sensor_precheck = self._wait_material_conditions(material_conditions, phase="pre")
        except Exception as exc:
            return {
                "success": False,
                "message": f"S09 工艺 {process} 前置物料传感器读取失败: {exc}",
                "logs": logs,
            }
        if not sensor_precheck["success"]:
            return {
                "success": False,
                "message": f"S09 工艺 {process} 等待所需物料在位失败",
                "status": "rejected",
                "sensor_precheck": sensor_precheck,
                "logs": logs,
            }

        if require_allow:
            try:
                self._append_log(
                    logs,
                    "等待 S09 允许加工信号",
                    {"variable": S09_ALLOW_PROCESS_VAR, "expected": True},
                )
                if not self._wait_allow_process():
                    return {"success": False, "message": "等待 S09 允许加工失败", "logs": logs}
            except Exception as exc:
                return {"success": False, "message": str(exc), "logs": logs}

        if process == 7 and aspirate_volume > 0 and not skip_level_check:
            try:
                level_check = self._ensure_sufficient_remaining_volume(liquid_bottle_index, aspirate_volume)
                self._append_log(
                    logs,
                    f"S09 液体瓶 {liquid_bottle_index} 剩余液量校验通过",
                    level_check,
                )
            except ValueError as exc:
                return {"success": False, "message": str(exc), "logs": logs}
            except Exception as exc:
                return {"success": False, "message": str(exc), "logs": logs}

        self._status = "Running"
        process_params = {
            S09_TIP_BOX_VAR: int(tip_box_index),
            S09_TIP_VAR: int(tip_index),
            S09_LIQUID_BOTTLE_VAR: int(liquid_bottle_index),
            S09_ASPIRATE_VOLUME_VAR: int(aspirate_volume),
            S09_DISPENSE_VOLUME_VAR: int(dispense_volume),
            S09_PROCESS_SELECT_VAR: int(process),
        }
        if process == 9:
            process_params[S09_DENSITY_COUNT_VAR] = density_measurement_count
        try:
            self._append_log(
                logs,
                f"S09 工艺 {process} 参数写入开始：{S09_PROCESS_LABELS[process]}",
                {"process": process, "process_label": S09_PROCESS_LABELS[process], "params": process_params},
            )
            for variable, value in process_params.items():
                self._write_variable(variable, value)
            self._append_log(
                logs,
                f"S09 工艺 {process} 参数写入完成",
                {"process": process, "written_variables": process_params},
            )
            self._write_variable(S09_PARAM_WRITTEN_VAR, True)
            self._append_log(
                logs,
                "S09 参数写入完成信号已置位，将保持至工艺结束",
                {"variable": S09_PARAM_WRITTEN_VAR, "value": True, "reset_delay": reset_delay},
            )
        except Exception as exc:
            self._status = "Error"
            return {"success": False, "message": str(exc), "data": {"process": process}, "logs": logs}

        data: dict[str, Any] = {
            "process": process,
            "process_label": S09_PROCESS_LABELS[process],
            "tip_box_index": tip_box_index,
            "tip_index": tip_index,
            "liquid_bottle_index": liquid_bottle_index,
            "station": station,
            "aspirate_volume": aspirate_volume,
            "dispense_volume": dispense_volume,
            "volume_unit": "raw",
            "aspirate_volume_ul": self._raw_volume_to_ul(aspirate_volume),
            "dispense_volume_ul": self._raw_volume_to_ul(dispense_volume),
            "density_measurement_count": density_measurement_count,
            "sensor_precheck": sensor_precheck,
            "logs": logs,
        }
        try:
            self._append_log(
                logs,
                f"等待 S09 工艺 {process} 完成",
                {"variable": S09_PROCESS_DONE_VAR, "expected": process},
            )
            if not self._wait_process_done(process):
                self._status = "Error"
                return {"success": False, "message": f"S09 工艺 {process} 完成等待失败", "data": data, "logs": logs}
            self._append_log(
                logs,
                f"S09 工艺 {process} 完成信号已确认",
                {"variable": S09_PROCESS_DONE_VAR, "expected": process},
            )

            try:
                sensor_postcheck = self._wait_material_conditions(material_conditions, phase="post")
            except Exception as exc:
                self._status = "Error"
                return {
                    "success": False,
                    "status": "verification_failed",
                    "message": f"S09 工艺 {process} 已完成，但物料传感器读取失败: {exc}",
                    "data": data,
                    "logs": logs,
                }
            data["sensor_postcheck"] = sensor_postcheck
            if not sensor_postcheck["success"]:
                self._status = "Error"
                return {
                    "success": False,
                    "status": "verification_failed",
                    "message": f"S09 工艺 {process} 已完成，但所需物料在位验证失败",
                    "data": data,
                    "logs": logs,
                }
            data["process_completed"] = True

            if process == 7 and aspirate_volume > 0:
                try:
                    remaining_update = self._deduct_remaining_volume(liquid_bottle_index, aspirate_volume)
                    data["remaining_volume_update"] = remaining_update
                    self._append_log(
                        logs,
                        (
                            f"S09 液体瓶 {liquid_bottle_index} 剩余液量已更新："
                            f"{remaining_update['previous_remaining_volume']:g} -> "
                            f"{remaining_update['remaining_volume']:g} mL"
                        ),
                        remaining_update,
                    )
                except Exception as exc:
                    self._status = "Error"
                    return {"success": False, "message": str(exc), "data": data, "logs": logs}

            should_read_balance = (
                process == 9
                if read_balance_after_done is None
                else bool(read_balance_after_done)
            )
            if should_read_balance:
                self._append_log(
                    logs,
                    "等待 S09 天平读数稳定",
                    {"process": process, "variable": S09_BALANCE_STABLE_VAR, "expected": True},
                )
                balance = self.read_balance(require_stable=True)
                if not balance.get("success", False):
                    self._status = "Error"
                    return {
                        "success": False,
                        "message": balance.get("message", "S09 天平读数读取失败"),
                        "data": data,
                        "logs": logs,
                    }
                data["balance"] = balance["data"]
                data["balance_reading"] = balance["data"]["balance_reading"]
                self._append_log(logs, "S09 天平读数读取完成", balance["data"])
        finally:
            self._append_log(logs, "S09 工艺参数清零开始")
            clear_result = self._clear_process_params(process)
            data["clear_process_params"] = clear_result
            self._append_log(logs, "S09 工艺参数清零完成", clear_result)
            if not clear_result["success"] and self._status != "Error":
                self._status = "Error"
                return {
                    "success": False,
                    "status": "cleanup_failed",
                    "message": "S09 工艺参数清零失败",
                    "data": data,
                    "logs": logs,
                }
        self._status = "Idle"
        self._last_process = data
        return {
            "success": True,
            "message": f"S09 工艺 {process} 完成：{S09_PROCESS_LABELS[process]}",
            "data": data,
            "logs": logs,
        }

    @action(auto_prefix=True, description="执行 S09 单次业务加液流程")
    def add_liquid(
        self,
        take_tip_box_index: int = 1,
        release_tip_box_index: int = 2,
        tip_index: int = 1,
        liquid_bottle_index: int = 1,
        station: int = 1,
        aspirate_volume: int = 1,
        dispense_volume: int = 1,
        volume_unit: str = "raw",
        skip_level_check: bool = False,
        S09液体瓶1剩余液量: float | None = None,
        S09液体瓶2剩余液量: float | None = None,
        S09液体瓶3剩余液量: float | None = None,
        S09液体瓶4剩余液量: float | None = None,
        S09液体瓶5剩余液量: float | None = None,
    ) -> dict[str, Any]:
        steps: list[dict[str, Any]] = []
        logs: list[dict[str, Any]] = []
        try:
            aspirate_raw = self._volume_to_raw(aspirate_volume, volume_unit)
            dispense_raw = self._volume_to_raw(dispense_volume, volume_unit)
        except ValueError as exc:
            return {"success": False, "message": str(exc)}
        if aspirate_raw <= 0 or dispense_raw <= 0:
            return {"success": False, "message": "S09 加液量必须大于 0"}
        if aspirate_raw != dispense_raw and max(aspirate_raw, dispense_raw) > S09_VOLUME_RAW_MAX:
            return {"success": False, "message": "S09 自动拆分加液时要求抽液量和放液量一致"}

        try:
            take_tip_box_index = validate_tip_box(take_tip_box_index)
            release_tip_box_index = validate_tip_box(release_tip_box_index)
            workflow_sensor_conditions = {
                S09_TIP_BOX_SENSORS[take_tip_box_index]: True,
                S09_TIP_BOX_SENSORS[release_tip_box_index]: True,
                S09_STATION_SENSORS[validate_liquid_bottle(liquid_bottle_index)]: True,
            }
            workflow_sensor_precheck = self._wait_material_conditions(
                workflow_sensor_conditions,
                phase="workflow_pre",
            )
        except (KeyError, ValueError) as exc:
            return {"success": False, "message": str(exc)}
        except Exception as exc:
            return {"success": False, "message": f"S09 加液流程物料传感器读取失败: {exc}"}
        if not workflow_sensor_precheck["success"]:
            return {
                "success": False,
                "status": "rejected",
                "message": "S09 加液流程等待 TIP盒和液体瓶物料在位失败",
                "sensor_precheck": workflow_sensor_precheck,
            }

        if aspirate_raw == dispense_raw:
            transfer_chunks = [(chunk, chunk) for chunk in self._split_raw_volume(aspirate_raw)]
        else:
            transfer_chunks = [(aspirate_raw, dispense_raw)]

        try:
            configured_remaining_volumes = self._write_configured_remaining_volumes(
                {
                    1: S09液体瓶1剩余液量,
                    2: S09液体瓶2剩余液量,
                    3: S09液体瓶3剩余液量,
                    4: S09液体瓶4剩余液量,
                    5: S09液体瓶5剩余液量,
                }
            )
        except ValueError as exc:
            return {"success": False, "message": str(exc)}
        except Exception as exc:
            return {"success": False, "message": str(exc)}

        plan: list[tuple[int, str, int, int, int, bool]] = [
            (5, f"从 TIP盒{take_tip_box_index} 取 TIP", take_tip_box_index, 0, 0, False)
        ]
        for chunk_index, (aspirate_chunk, dispense_chunk) in enumerate(transfer_chunks):
            is_last_chunk = chunk_index == len(transfer_chunks) - 1
            plan.extend(
                [
                    (7, "液体瓶取液", take_tip_box_index, aspirate_chunk, 0, False),
                    (8, "烧杯放液", take_tip_box_index, 0, dispense_chunk, is_last_chunk),
                ]
            )
        plan.append((6, f"向 TIP盒{release_tip_box_index} 放 TIP", release_tip_box_index, 0, 0, False))

        for (
            process,
            step_name,
            process_tip_box_index,
            aspirate_chunk,
            dispense_chunk,
            read_balance_after_done,
        ) in plan:
            result = self.run_process(
                process=process,
                tip_box_index=process_tip_box_index,
                tip_index=tip_index,
                liquid_bottle_index=liquid_bottle_index,
                station=station,
                aspirate_volume=aspirate_chunk,
                dispense_volume=dispense_chunk,
                volume_unit="raw",
                require_allow=process in {5, 6, 7, 8},
                skip_level_check=skip_level_check,
                read_balance_after_done=read_balance_after_done,
            )
            steps.append({"step": step_name, **result})
            logs.extend(result.get("logs") or [])
            if not result.get("success", False):
                return {
                    "success": False,
                    "message": result.get("message", step_name),
                    "steps": steps,
                    "logs": logs,
                }
        return {
            "success": True,
            "message": "S09 单次加液完成",
            "data": {
                "take_tip_box_index": take_tip_box_index,
                "release_tip_box_index": release_tip_box_index,
                "tip_index": tip_index,
                "liquid_bottle_index": liquid_bottle_index,
                "station": station,
                "aspirate_volume": aspirate_raw,
                "dispense_volume": dispense_raw,
                "volume_unit": "raw",
                "aspirate_volume_ul": self._raw_volume_to_ul(aspirate_raw),
                "dispense_volume_ul": self._raw_volume_to_ul(dispense_raw),
                "configured_remaining_volumes": configured_remaining_volumes,
                "sensor_precheck": workflow_sensor_precheck,
                "split_count": len(transfer_chunks),
                "transfer_chunks": [
                    {
                        "aspirate_volume": aspirate_chunk,
                        "dispense_volume": dispense_chunk,
                        "aspirate_volume_ul": self._raw_volume_to_ul(aspirate_chunk),
                        "dispense_volume_ul": self._raw_volume_to_ul(dispense_chunk),
                    }
                    for aspirate_chunk, dispense_chunk in transfer_chunks
                ],
            },
            "steps": steps,
            "logs": logs,
        }

    @action(auto_prefix=True, description="按配置复用或一次性使用 S09 加液 TIP")
    def add_liquid_with_reusable_tip(
        self,
        liquid_station_index: int = 1,
        solvent_batch_id: str = "",
        volume: int | float = 1,
        volume_unit: str = "raw",
        skip_level_check: bool = False,
        reuse_tip: bool = True,
        liquid_count: int = 1,
        liquid_additions: list[dict[str, Any]] | None = None,
        initialize_tip_inventory: bool = False,
        initial_used_tip_count: int = 0,
    ) -> dict[str, Any]:
        if initialize_tip_inventory:
            initialized = self.initialize_reusable_tip_inventory(
                reset=True,
                used_tip_count=initial_used_tip_count,
            )
            if not initialized.get("success"):
                return initialized
        if liquid_additions:
            if liquid_count != len(liquid_additions):
                return {"success": False, "message": "liquid_count 与 liquid_additions 数量不一致"}
            addition_results: list[dict[str, Any]] = []
            for index, addition in enumerate(liquid_additions, start=1):
                try:
                    result = self.add_liquid_with_reusable_tip(
                        liquid_station_index=int(addition["liquid_station_index"]),
                        solvent_batch_id=str(addition["solvent_batch_id"]),
                        volume=float(addition["volume"]),
                        volume_unit=volume_unit,
                        skip_level_check=skip_level_check,
                        reuse_tip=bool(addition["reuse_tip"]),
                    )
                except (KeyError, TypeError, ValueError) as exc:
                    return {
                        "success": False,
                        "message": f"第 {index} 种液体参数无效：{exc}",
                        "liquid_results": addition_results,
                    }
                addition_results.append(result)
                if not result.get("success"):
                    return {
                        "success": False,
                        "message": f"第 {index} 种液体加液失败：{result.get('message') or '未知错误'}",
                        "liquid_results": addition_results,
                    }
            final_data = dict(addition_results[-1].get("data") or {})
            final_data["liquid_count"] = len(addition_results)
            final_data["liquid_results"] = addition_results
            return {
                **addition_results[-1],
                "message": f"S09 已完成 {len(addition_results)} 次加液",
                "data": final_data,
            }
        try:
            liquid_station_index = validate_liquid_bottle(liquid_station_index)
            solvent_batch_id = str(solvent_batch_id).strip()
            if not solvent_batch_id:
                raise ValueError("S09 加液必须提供明确的 solvent_batch_id")
            raw_volume = self._volume_to_raw(volume, volume_unit)
            if raw_volume <= 0:
                raise ValueError("S09 加液量必须大于 0")
            transfer_chunks = self._split_raw_volume(raw_volume)
            required_cycles = len(transfer_chunks)
        except (TypeError, ValueError) as exc:
            return {"success": False, "message": str(exc)}

        # 同一批次在不同液体工位可能对应不同溶剂瓶，必须分别绑定 TIP。
        solvent_key = f"S09-STATION-{liquid_station_index}:BATCH:{solvent_batch_id}"
        with self._tip_reuse_execution_lock:
            try:
                if reuse_tip:
                    tip = self._tip_reuse_state.prepare_tip(
                        solvent_key,
                        required_cycles=required_cycles,
                        liquid_station_index=liquid_station_index,
                    )
                else:
                    tip = self._tip_reuse_state.prepare_single_use_tip()
            except Exception as exc:
                return {"success": False, "message": str(exc)}

            liquid_tip_tracking = {
                "solvent_key": solvent_key,
                "solvent_batch_id": solvent_batch_id,
                "liquid_station_index": liquid_station_index,
                "reuse_tip": reuse_tip,
                "single_use": not reuse_tip,
                "tip_index": int(tip["tip_index"]),
                "take_tip_box_index": int(tip["current_box"]),
                "release_tip_box_index": 2,
                "required_cycles": required_cycles,
            }
            conditions = {
                S09_TIP_BOX_SENSORS[liquid_tip_tracking["take_tip_box_index"]]: True,
                S09_TIP_BOX_SENSORS[liquid_tip_tracking["release_tip_box_index"]]: True,
                S09_STATION_SENSORS[liquid_station_index]: True,
            }
            try:
                precheck = self._wait_material_conditions(conditions, phase="workflow_pre")
            except Exception as exc:
                if not reuse_tip:
                    try:
                        self._tip_reuse_state.release_single_use_tip_reservation(
                            liquid_tip_tracking["tip_index"]
                        )
                    except Exception as tracking_exc:
                        liquid_tip_tracking["tracking_error"] = str(tracking_exc)
                return {
                    "success": False,
                    "message": f"S09 加液物料传感器读取失败: {exc}",
                    "tip_reuse": liquid_tip_tracking,
                }
            if not precheck["success"]:
                if not reuse_tip:
                    try:
                        self._tip_reuse_state.release_single_use_tip_reservation(
                            liquid_tip_tracking["tip_index"]
                        )
                    except Exception as tracking_exc:
                        liquid_tip_tracking["tracking_error"] = str(tracking_exc)
                return {
                    "success": False,
                    "status": "rejected",
                    "message": "S09 加液等待 TIP盒和液体瓶物料在位失败",
                    "sensor_precheck": precheck,
                    "tip_reuse": liquid_tip_tracking,
                }

            steps: list[dict[str, Any]] = []
            logs: list[dict[str, Any]] = []
            plan: list[tuple[int, int, int, int, str, bool]] = [
                (5, liquid_tip_tracking["take_tip_box_index"], 0, 0, "取加液 TIP", False),
            ]
            for chunk in transfer_chunks:
                plan.extend(
                    [
                        (7, liquid_tip_tracking["take_tip_box_index"], chunk, 0, "液体工位取液", False),
                        (8, liquid_tip_tracking["take_tip_box_index"], 0, chunk, "烧杯加液", False),
                    ]
                )
            plan.append((6, liquid_tip_tracking["release_tip_box_index"], 0, 0, "放回加液 TIP", False))
            for process, tip_box, aspirate, dispense, step_name, read_balance in plan:
                result = self.run_process(
                    process=process,
                    tip_box_index=tip_box,
                    tip_index=liquid_tip_tracking["tip_index"],
                    liquid_bottle_index=(liquid_station_index if process == 7 else 0),
                    station=1,
                    aspirate_volume=aspirate,
                    dispense_volume=dispense,
                    volume_unit="raw",
                    require_allow=True,
                    skip_level_check=skip_level_check,
                    read_balance_after_done=read_balance,
                )
                steps.append({"step": step_name, **result})
                logs.extend(result.get("logs") or [])
                if not result.get("success", False):
                    take_tip_succeeded = any(
                        (step.get("success", False) or (step.get("data") or {}).get("process_completed", False))
                        and (step.get("data") or {}).get("process") == 5
                        for step in steps
                    )
                    release_tip_succeeded = any(
                        (step.get("success", False) or (step.get("data") or {}).get("process_completed", False))
                        and (step.get("data") or {}).get("process") == 6
                        for step in steps
                    )
                    if not reuse_tip:
                        try:
                            if not take_tip_succeeded:
                                tracked_tip = (
                                    self._tip_reuse_state.release_single_use_tip_reservation(
                                        liquid_tip_tracking["tip_index"]
                                    )
                                )
                            elif release_tip_succeeded:
                                tracked_tip = self._tip_reuse_state.consume_single_use_tip(
                                    liquid_tip_tracking["tip_index"]
                                )
                            else:
                                tracked_tip = self._tip_reuse_state.mark_single_use_tip_unknown(
                                    liquid_tip_tracking["tip_index"]
                                )
                            liquid_tip_tracking["status"] = tracked_tip["status"]
                        except Exception as tracking_exc:
                            liquid_tip_tracking["tracking_error"] = str(tracking_exc)
                    elif take_tip_succeeded and not release_tip_succeeded:
                        try:
                            unknown_tip = self._tip_reuse_state.mark_active_tip_unknown(
                                solvent_key
                            )
                            liquid_tip_tracking["status"] = unknown_tip["status"]
                        except Exception as tracking_exc:
                            liquid_tip_tracking["tracking_error"] = str(tracking_exc)
                    return {
                        "success": False,
                        "message": result.get("message", step_name),
                        "steps": steps,
                        "logs": logs,
                        "tip_reuse": liquid_tip_tracking,
                    }

            try:
                if reuse_tip:
                    updated_tip = self._tip_reuse_state.record_tip_use(
                        solvent_key,
                        cycles=required_cycles,
                    )
                else:
                    updated_tip = self._tip_reuse_state.consume_single_use_tip(
                        liquid_tip_tracking["tip_index"]
                    )
            except Exception as exc:
                try:
                    if reuse_tip:
                        self._tip_reuse_state.mark_active_tip_unknown(solvent_key)
                    else:
                        self._tip_reuse_state.mark_single_use_tip_unknown(
                            liquid_tip_tracking["tip_index"]
                        )
                except Exception:
                    pass
                return {
                    "success": False,
                    "message": f"S09 加液已完成，但 TIP 状态更新失败: {exc}",
                    "steps": steps,
                    "logs": logs,
                    "tip_reuse": liquid_tip_tracking,
                }

            tip_reuse = {
                **liquid_tip_tracking,
                "status": updated_tip["status"],
                "current_box": updated_tip["current_box"],
                "use_count": updated_tip["use_count"],
            }
            if reuse_tip:
                tip_reuse["max_use_count"] = self._tip_reuse_state.max_use_count

            return {
                "success": True,
                "message": "S09 加液完成",
                "data": {
                    "liquid_station_index": liquid_station_index,
                    "solvent_batch_id": solvent_batch_id,
                    "volume": raw_volume,
                    "volume_ul": self._raw_volume_to_ul(raw_volume),
                    "volume_unit": "raw",
                    "sensor_precheck": precheck,
                    "tip_reuse": tip_reuse,
                },
                "steps": steps,
                "logs": logs,
            }

    @action(auto_prefix=True, description="执行 S09 独立密度测量并自动管理一次性 TIP")
    def measure_density(
        self,
        density_volume: int | float = 1,
        density_measurement_count: int = 1,
        volume_unit: str = "raw",
    ) -> dict[str, Any]:
        """在烧杯完成前序处理后独立测量密度，不执行加液。"""
        try:
            density_raw_volume = self._volume_to_raw(density_volume, volume_unit)
            if density_raw_volume <= 0:
                raise ValueError("S09 测密度体积必须大于 0")
            if density_raw_volume > S09_VOLUME_RAW_MAX:
                raise ValueError("S09 单次测密度体积不能超过 5000 uL")
            density_measurement_count = validate_density_count(density_measurement_count)
        except (TypeError, ValueError) as exc:
            return {"success": False, "message": str(exc)}

        with self._tip_reuse_execution_lock:
            try:
                density_tip = self._tip_reuse_state.prepare_single_use_tip()
            except Exception as exc:
                return {"success": False, "message": str(exc)}

            density_tip_tracking = {
                "tip_index": int(density_tip["tip_index"]),
                "take_tip_box_index": 1,
                "release_tip_box_index": 2,
                "single_use": True,
                "density_measurement_count": density_measurement_count,
            }
            steps: list[dict[str, Any]] = []
            logs: list[dict[str, Any]] = []
            plan = [
                (5, 1, 0, 0, "取一次性测密度 TIP"),
                (9, 1, density_raw_volume, 0, "烧杯测密度抽排液"),
                (6, 2, 0, 0, "废弃测密度 TIP"),
            ]
            for process, tip_box, aspirate, dispense, step_name in plan:
                result = self.run_process(
                    process=process,
                    tip_box_index=tip_box,
                    tip_index=density_tip_tracking["tip_index"],
                    liquid_bottle_index=0,
                    station=1,
                    aspirate_volume=aspirate,
                    dispense_volume=dispense,
                    volume_unit="raw",
                    require_allow=True,
                    skip_level_check=True,
                    read_balance_after_done=False,
                    density_measurement_count=density_measurement_count,
                )
                steps.append({"step": step_name, **result})
                logs.extend(result.get("logs") or [])
                if not result.get("success", False):
                    try:
                        unknown_tip = self._tip_reuse_state.mark_single_use_tip_unknown(
                            density_tip_tracking["tip_index"]
                        )
                        density_tip_tracking["status"] = unknown_tip["status"]
                    except Exception as tracking_exc:
                        density_tip_tracking["tracking_error"] = str(tracking_exc)
                    return {
                        "success": False,
                        "message": result.get("message", step_name),
                        "steps": steps,
                        "logs": logs,
                        "density_tip": density_tip_tracking,
                    }

            try:
                consumed_tip = self._tip_reuse_state.consume_single_use_tip(
                    density_tip_tracking["tip_index"]
                )
                density_tip_tracking.update(
                    {
                        "status": consumed_tip["status"],
                        "current_box": consumed_tip["current_box"],
                        "use_count": consumed_tip["use_count"],
                    }
                )
                aspirate_readings = [
                    float(self._read_variable(name, use_cache=False))
                    for name in s09_density_balance_vars(S09_ASPIRATE_BALANCE_READINGS_VAR)[
                        :density_measurement_count
                    ]
                ]
                dispense_readings = [
                    float(self._read_variable(name, use_cache=False))
                    for name in s09_density_balance_vars(S09_DISPENSE_BALANCE_READINGS_VAR)[
                        :density_measurement_count
                    ]
                ]
                density_volume_ml = self._raw_volume_to_ml(density_raw_volume)
                net_masses = [*aspirate_readings, *dispense_readings]
                densities = [abs(mass) / density_volume_ml for mass in net_masses]
                density = sum(densities) / len(densities)
            except Exception as exc:
                return {
                    "success": False,
                    "message": f"S09 密度结果无效: {exc}",
                    "steps": steps,
                    "logs": logs,
                    "density_tip": density_tip_tracking,
                }

            data = {
                "net_mass": net_masses[0],
                "net_masses": net_masses,
                "absolute_mass": abs(net_masses[0]),
                "mass_unit": "g",
                "density_volume": density_raw_volume,
                "density_volume_ml": density_volume_ml,
                "density_measurement_count": density_measurement_count,
                "aspirate_balance_readings": aspirate_readings,
                "dispense_balance_readings": dispense_readings,
                "aspirate_densities": densities[:density_measurement_count],
                "dispense_densities": densities[density_measurement_count:],
                "densities": densities,
                "volume_unit": "raw",
                "density": density,
                "density_unit": "g/mL",
                "balance_stable": True,
                "density_tip": density_tip_tracking,
            }
            return {
                "success": True,
                "message": "S09 密度测量完成",
                "display_message": (
                    f"S09 密度结果：{density:.6g} g/mL（抽液、放液各测 "
                    f"{density_measurement_count} 次，共 {len(densities)} 个结果）"
                ),
                "data": data,
                "steps": steps,
                "logs": logs,
            }

    @action(auto_prefix=True, description="执行 S09 烧杯加液：取 TIP、液体瓶取液、烧杯放液、放 TIP")
    def add_liquid_to_beaker(
        self,
        take_tip_box_index: int = 1,
        release_tip_box_index: int = 2,
        tip_index: int = 1,
        liquid_bottle_index: int = 1,
        station: int = 1,
        aspirate_volume: int = 1,
        dispense_volume: int = 1,
        volume_unit: str = "raw",
        skip_level_check: bool = False,
        S09液体瓶1剩余液量: float | None = None,
        S09液体瓶2剩余液量: float | None = None,
        S09液体瓶3剩余液量: float | None = None,
        S09液体瓶4剩余液量: float | None = None,
        S09液体瓶5剩余液量: float | None = None,
    ) -> dict[str, Any]:
        result = self.add_liquid(
            take_tip_box_index=take_tip_box_index,
            release_tip_box_index=release_tip_box_index,
            tip_index=tip_index,
            liquid_bottle_index=liquid_bottle_index,
            station=station,
            aspirate_volume=aspirate_volume,
            dispense_volume=dispense_volume,
            volume_unit=volume_unit,
            skip_level_check=skip_level_check,
            S09液体瓶1剩余液量=S09液体瓶1剩余液量,
            S09液体瓶2剩余液量=S09液体瓶2剩余液量,
            S09液体瓶3剩余液量=S09液体瓶3剩余液量,
            S09液体瓶4剩余液量=S09液体瓶4剩余液量,
            S09液体瓶5剩余液量=S09液体瓶5剩余液量,
        )
        if result.get("success", False):
            data = dict(result.get("data") or {})
            data["process_sequence"] = [
                step["data"]["process"]
                for step in result.get("steps", [])
                if isinstance(step.get("data"), dict) and "process" in step["data"]
            ]
            return {**result, "message": "S09 烧杯加液完成", "data": data}
        return result

    @action(auto_prefix=True, description="执行 S09 多步加液工作流")
    def run_liquid_workflow(
        self,
        liquid_steps: list[dict[str, Any]] | None = None,
        sample_id: str = "",
        release_after: bool = True,
    ) -> dict[str, Any]:
        liquid_steps = liquid_steps or []
        if not liquid_steps:
            return {"success": False, "message": "liquid_steps 不能为空"}
        prepared = self.prepare_liquid_station()
        if not prepared.get("success", False):
            return prepared
        self.bind_sample_to_station(sample_id=sample_id)
        steps: list[dict[str, Any]] = []
        try:
            for index, item in enumerate(liquid_steps, start=1):
                result = self.add_liquid(**item)
                steps.append({"index": index, **result})
                if not result.get("success", False):
                    return {"success": False, "message": result.get("message", f"第 {index} 步加液失败"), "steps": steps}
            return {
                "success": True,
                "message": f"S09 多步加液完成，共 {len(liquid_steps)} 步",
                "data": {"sample_id": sample_id},
                "steps": steps,
            }
        finally:
            if release_after:
                self.release_station()

    @action(auto_prefix=True, description="写入 S09 单个液体瓶剩余液量")
    def set_liquid_bottle_remaining_volume(
        self,
        bottle: int = 1,
        remaining_volume: float = 100.0,
    ) -> dict[str, Any]:
        try:
            bottle = validate_liquid_bottle(bottle)
            remaining_volume = float(remaining_volume)
            if remaining_volume < 0:
                raise ValueError("S09 液体瓶剩余液量不能为负数")
            variable = s09_remaining_volume_var(bottle)
            self._write_variable(variable, remaining_volume)
        except ValueError as exc:
            return {"success": False, "message": str(exc)}
        except Exception as exc:
            return {"success": False, "message": str(exc), "data": {"bottle": bottle}}
        return {
            "success": True,
            "message": f"S09 液体瓶 {bottle} 剩余液量已写入 {remaining_volume}",
            "data": {"bottle": bottle, "remaining_volume": remaining_volume, "variable": variable},
        }

    @action(auto_prefix=True, description="初始化 S09 1-5 号液体瓶剩余液量")
    def initialize_liquid_bottle_remaining_volumes(self, remaining_volume: float = 100.0) -> dict[str, Any]:
        writes: list[dict[str, Any]] = []
        for bottle in range(1, 6):
            result = self.set_liquid_bottle_remaining_volume(bottle=bottle, remaining_volume=remaining_volume)
            writes.append(result)
            if not result.get("success", False):
                return {"success": False, "message": result.get("message", "写入剩余液量失败"), "data": {"writes": writes}}
        return {
            "success": True,
            "message": f"S09 1-5 号液体瓶剩余液量已初始化为 {float(remaining_volume)}",
            "data": {"remaining_volume": float(remaining_volume), "writes": writes},
        }

    @action(auto_prefix=True, description="读取 S09 天平读数")
    def read_balance(self, require_stable: bool = True) -> dict[str, Any]:
        try:
            if require_stable:
                stable = self._wait_equal(S09_BALANCE_STABLE_VAR, True)
            else:
                stable = bool(self._read_variable(S09_BALANCE_STABLE_VAR, use_cache=False))
            if not stable:
                return {
                    "success": False,
                    "message": "等待 S09 天平读数稳定失败",
                    "data": {"stable": stable},
                }
            reading = self._read_variable(S09_BALANCE_READING_VAR, use_cache=False)
        except Exception as exc:
            return {"success": False, "message": str(exc)}
        return {
            "success": True,
            "message": "S09 天平读数读取完成",
            "data": {"balance_reading": reading, "stable": stable},
        }

    @action(auto_prefix=True, description="初始化 S09 可复用 TIP 库存（确认盒1满、盒2空后调用）")
    def initialize_reusable_tip_inventory(
        self,
        reset: bool = False,
        used_tip_count: int = 0,
        known_bindings: dict[str, int] | None = None,
    ) -> dict[str, Any]:
        try:
            state = self._tip_reuse_state.initialize(
                reset=reset,
                used_tip_count=used_tip_count,
                known_bindings=known_bindings,
            )
        except Exception as exc:
            return {"success": False, "message": str(exc)}
        return {
            "success": True,
            "message": f"S09 可复用 TIP 库存已初始化，共 {state['tip_count']} 个 TIP",
            "data": {
                "initialized": state["initialized"],
                "tip_count": state["tip_count"],
                "max_use_count": state["max_use_count"],
                "used_tip_count": sum(
                    tip["status"] != "unused" for tip in state["tips"].values()
                ),
                "known_bindings": dict(known_bindings or {}),
                "state_path": str(self._tip_reuse_state.state_path),
            },
        }

    @action(auto_prefix=True, description="读取 S09 可复用 TIP 库存与溶剂绑定")
    def get_reusable_tip_status(self) -> dict[str, Any]:
        state = self._tip_reuse_state.snapshot()
        return {
            "success": True,
            "message": "S09 可复用 TIP 状态读取完成",
            "data": {
                **state,
                "state_path": str(self._tip_reuse_state.state_path),
            },
        }

    @action(auto_prefix=True, description="读取 S09 移液站状态")
    def get_pipetting_status(self) -> dict[str, Any]:
        variable_names = [
            S09_PROCESS_DONE_VAR,
            S09_STATION_STATUS_VAR,
            S09_BALANCE_STABLE_VAR,
            S09_BALANCE_READING_VAR,
            S09_DENSITY_COUNT_VAR,
            *s09_density_balance_vars(S09_ASPIRATE_BALANCE_READINGS_VAR),
            *s09_density_balance_vars(S09_DISPENSE_BALANCE_READINGS_VAR),
            *s09_remaining_volume_vars(),
        ]
        values = self.get_variables(variable_names, use_cache=False)
        return {
            "success": True,
            "message": "S09 状态读取完成",
            "data": {
                "variables": values,
                "bindings": dict(self._bindings),
                "last_process": dict(self._last_process),
                "reusable_tip_state": self._tip_reuse_state.snapshot(),
            },
        }
