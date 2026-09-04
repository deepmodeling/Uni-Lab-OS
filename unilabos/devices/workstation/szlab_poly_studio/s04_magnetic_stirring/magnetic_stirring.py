from __future__ import annotations

import os
import time
from typing import Any

from unilabos.registry.decorators import action, device, not_action, topic_config
from unilabos.devices.workstation.szlab_poly_studio.sensor import wait_variable_equal

from .sensors import (
    S04_PROCESS_MODES,
    S04_POSITION_RANGE,
    s04_allow_var,
    s04_done_var,
    s04_duration_var,
    s04_material_sensor_var,
    s04_opcua_node_id_map,
    s04_params_written_var,
    s04_process_var,
    s04_safe_temperature_var,
    s04_speed_var,
    s04_station_prefix,
    s04_status_var,
    s04_temperature_var,
)

DEFAULT_OPCUA_URL = os.environ.get(
    "UNILABOS_SZLAB_MIXER_OPCUA_URL",
    "opc.tcp://192.168.1.10:4840/",
)

S04_DONE_TIMEOUT_GRACE_SECONDS = 30.0


@device(
    id="szlab_mixer_stirrer",
    display_name="SZLab 磁搅",
    category=["heaterstirrer"],
    description="SZLab Poly Studio S04 磁搅工位设备",
)
class SzlabMixerMagneticStirrerDevice:
    def __init__(
        self,
        url: str = DEFAULT_OPCUA_URL,
        username: str | None = None,
        password: str | None = None,
        csv_path: str | None = "s04_magnetic_stirring/magnetic_stirring_nodes.csv",
        auto_connect: bool = True,
        plc_device_id: str = "szlab_poly_plc",
        use_plc_gateway: bool = False,
        opcua_node_id_map: dict[str, str] | None = None,
        **kwargs,
    ):
        self.url = url
        self.plc_device_id = plc_device_id
        self._plc_gateway = None
        self._status = "Idle"
        self._last_position = 0
        self._last_mode = 0
        client_kwargs: dict[str, Any] = {
            "url": url,
            "username": username,
            "password": password,
            "auto_connect": auto_connect,
        }
        if csv_path is not None:
            client_kwargs["csv_path"] = csv_path
        client_kwargs["opcua_node_id_map"] = opcua_node_id_map or s04_opcua_node_id_map()
        if use_plc_gateway:
            self._client = None
        else:
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
        if self._client is not None:
            self._client.disconnect()

    @not_action
    def get_variables(self, variable_names: list[str], use_cache: bool = False) -> dict[str, dict[str, Any]]:
        if getattr(self, "_plc_gateway", None) is not None:
            values = {}
            for name in variable_names:
                try:
                    values[name] = {"success": True, "value": self._read_variable(name, use_cache=use_cache)}
                except Exception as exc:
                    values[name] = {"success": False, "error": str(exc)}
            return values
        return self._client.get_variables(variable_names, use_cache=use_cache)

    @not_action
    def get_opc_variable_metadata(self, variable_name: str) -> tuple[str, str | None]:
        if self._client is None:
            return variable_name, None
        return self._client.get_opc_variable_metadata(variable_name)

    @not_action
    def _read_variable(self, name: str, use_cache: bool = False) -> Any:
        if getattr(self, "_plc_gateway", None) is not None:
            return self._plc_gateway.read_variable(name, use_cache=use_cache)
        return self._client.read(name)

    @not_action
    def _write_variable(self, name: str, value: Any) -> None:
        if getattr(self, "_plc_gateway", None) is not None:
            self._plc_gateway.write_variable(name, value)
            return
        self._client.write(name, value)

    @not_action
    def _validate_position(self, position: int) -> int:
        position = int(position)
        if position not in S04_POSITION_RANGE:
            raise ValueError("磁搅位置必须在 1-4 范围内")
        return position

    @not_action
    def _validate_mode(self, mode: int) -> int:
        mode = int(mode)
        if mode not in S04_PROCESS_MODES:
            raise ValueError("磁搅工艺选择必须是 1(搅拌)、2(加热)、3(搅拌+加热)")
        return mode

    @not_action
    def _wait_allow_processing(self, position: int) -> bool:
        variable = s04_allow_var(position)
        return self._wait_variable_true(variable)

    @not_action
    def _wait_done(self, position: int, *, timeout: float) -> bool:
        variable = s04_done_var(position)
        waiter = getattr(self._plc_gateway, "wait_new_cycle_done", None) if self._plc_gateway is not None else None
        if callable(waiter):
            try:
                return waiter(variable, interval=1.0, timeout=timeout)
            except TypeError:
                return waiter(variable, interval=1.0)
        wait_equal = getattr(self._plc_gateway, "wait_equal", None) if self._plc_gateway is not None else None
        wait_variable_equal = (
            getattr(self._plc_gateway, "wait_variable_equal", None) if self._plc_gateway is not None else None
        )
        if callable(wait_equal) or callable(wait_variable_equal):
            if not self._wait_variable_equal(variable, False):
                return False
            return self._wait_variable_true(variable)
        reader = self._plc_gateway if self._plc_gateway is not None else self._client
        started_at = time.monotonic()
        if self._read_bool(variable):
            if not wait_variable_equal(
                reader, variable, False, interval=1.0, timeout=timeout
            ):
                return False
        remaining = max(0.0, timeout - (time.monotonic() - started_at))
        return wait_variable_equal(
            reader, variable, True, interval=1.0, timeout=remaining
        )

    @not_action
    def _wait_idle_status(self, position: int) -> bool:
        return self._wait_variable_equal(s04_status_var(position), 1)

    @not_action
    def _read_bool(self, variable: str) -> bool:
        return bool(self._read_variable(variable, use_cache=False))

    @not_action
    def _wait_variable_true(self, variable: str) -> bool:
        waiter = getattr(self._plc_gateway, "wait_variable_true", None) if self._plc_gateway is not None else None
        if callable(waiter):
            return waiter(variable, interval=1.0)
        return self._wait_variable_equal(variable, True)

    @not_action
    def _wait_variable_equal(self, variable: str, expected: Any) -> bool:
        waiter = getattr(self._plc_gateway, "wait_equal", None) if self._plc_gateway is not None else None
        if callable(waiter):
            return waiter(variable, expected, interval=1.0)
        waiter = getattr(self._plc_gateway, "wait_variable_equal", None) if self._plc_gateway is not None else None
        if callable(waiter):
            return waiter(variable, expected, interval=1.0)
        reader = self._plc_gateway if self._plc_gateway is not None else self._client
        return wait_variable_equal(
            reader,
            variable,
            expected,
            interval=1.0,
        )

    @action(auto_prefix=True, description="执行 S04 磁搅加工")
    def run_stirring(
        self,
        position: int = 1,
        mode: int = 3,
        speed: int = 300,
        temperature: int = 25,
        duration: float = 30.0,
        safe_temperature: int = 80,
        reset: bool = False,
    ) -> dict[str, Any]:
        """
        Args:
            position[磁搅位置]: 磁搅工位编号，范围 1-4。
            mode[工艺选择]: 1=搅拌，2=加热，3=搅拌+加热。
            speed[磁搅速度]: 磁搅速度设置。
            temperature[磁搅温度]: 磁搅温度设置。
            duration[磁搅时间(s)]: 磁搅持续时间，单位秒，写入 PLC 时转换为毫秒。
            safe_temperature[安全温度]: 磁搅安全温度设置。
            reset[恢复初始值]: 为 True 时只恢复本位置 PC->PLC 参数初始值，不启动加工。
        """
        try:
            position = self._validate_position(position)
            mode = self._validate_mode(mode)
        except ValueError as exc:
            return {"success": False, "message": str(exc)}

        station = s04_station_prefix(position)
        self._status = "Running"
        if reset:
            return self._reset_pc_to_plc_defaults(position)

        material_sensor = s04_material_sensor_var(position)
        if not self._wait_variable_true(material_sensor):
            self._status = "Error"
            return {
                "success": False,
                "message": f"{station} 等待搅拌位置有料失败",
                "data": {"station": station, "sensor_variable": material_sensor},
            }

        if not self._wait_idle_status(position):
            self._status = "Error"
            return {
                "success": False,
                "message": f"{station} 磁搅状态等待空闲失败（期望 1）",
                "data": {"station": station, "expected_status": 1},
            }

        if not self._wait_allow_processing(position):
            self._status = "Error"
            return {"success": False, "message": f"{station} 允许加工等待失败", "data": {"station": station}}

        duration_ms = int(float(duration) * 1000)
        try:
            self._write_variable(s04_process_var(position), mode)
            self._write_variable(s04_speed_var(position), int(speed))
            self._write_variable(s04_temperature_var(position), int(temperature))
            self._write_variable(s04_duration_var(position), duration_ms)
            self._write_variable(s04_safe_temperature_var(position), int(safe_temperature))
            self._write_variable(s04_params_written_var(position), True)
        except Exception as exc:
            self._reset_pc_to_plc_defaults(position)
            self._status = "Error"
            return {"success": False, "message": str(exc), "data": {"station": station}}

        done = False
        wait_error: Exception | None = None
        try:
            done = self._wait_done(
                position,
                timeout=float(duration) + S04_DONE_TIMEOUT_GRACE_SECONDS,
            )
        except Exception as exc:
            wait_error = exc
        finally:
            reset_result = self._reset_pc_to_plc_defaults(position)

        if wait_error is not None:
            self._status = "Error"
            raise wait_error
        if not reset_result.get("success", False):
            return reset_result

        if not self._wait_variable_true(material_sensor):
            self._status = "Error"
            return {
                "success": False,
                "status": "verification_failed",
                "message": f"{station} 加工已完成，但搅拌位置物料在位验证失败",
                "data": {"station": station, "sensor_variable": material_sensor},
            }

        self._status = "Idle"
        self._last_position = position
        self._last_mode = mode
        return {
            "success": True,
            "message": (
                f"{station} 磁搅已达到规定时间，按正常完成处理"
                if not done
                else f"{station} 磁搅加工完成，工艺 {S04_PROCESS_MODES[mode]}"
            ),
            "data": {
                "station": station,
                "position": position,
                "mode": mode,
                "mode_label": S04_PROCESS_MODES[mode],
                "speed": int(speed),
                "temperature": int(temperature),
                "duration": float(duration),
                "duration_ms": duration_ms,
                "safe_temperature": int(safe_temperature),
                "done_variable": s04_done_var(position),
                "done_signal_received": done,
                "reset": reset_result.get("data", {}),
            },
        }

    @not_action
    def _reset_pc_to_plc_defaults(self, position: int, include_params_written: bool = True) -> dict[str, Any]:
        station = s04_station_prefix(position)
        reset_values = [
            (s04_process_var(position), 0),
            (s04_speed_var(position), 0),
            (s04_temperature_var(position), 0),
            (s04_duration_var(position), 0),
            (s04_safe_temperature_var(position), 0),
        ]
        if include_params_written:
            reset_values.append((s04_params_written_var(position), False))

        errors: list[str] = []
        for name, value in reset_values:
            try:
                self._write_variable(name, value)
            except Exception as exc:
                errors.append(f"{name}: {exc}")
        if errors:
            self._status = "Error"
            return {
                "success": False,
                "message": f"{station} 磁搅 PC->PLC 参数清理失败: {'; '.join(errors)}",
                "data": {"station": station, "reset": True},
            }
        self._status = "Idle"
        return {
            "success": True,
            "message": f"{station} 磁搅 PC->PLC 参数已恢复初始值",
            "data": {"station": station, "position": position, "reset": True},
        }
