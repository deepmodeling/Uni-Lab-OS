from __future__ import annotations

from typing import Any

from unilabos.registry.decorators import ActionInputHandle, DataSource, action, device, not_action, topic_config
from unilabos.devices.workstation.szlab_poly_studio.plc import SZLabPolyPLCDevice

MAGNETIC_STIRRER_SLOT_SENSORS = {
    1: "传感器状态_上位机[2].NO[10]",
    2: "传感器状态_上位机[2].NO[11]",
    3: "传感器状态_上位机[2].NO[12]",
    4: "传感器状态_上位机[2].NO[13]",
    5: "传感器状态_上位机[2].NO[14]",
    6: "传感器状态_上位机[2].NO[15]",
}

MAGNETIC_STIRRER_STATUS_LABELS = {
    1: "Idle",
    2: "Busy",
}


@device(
    id="szlab_mixer_stirrer",
    display_name="SZLab 磁搅",
    category=["heaterstirrer"],
    description="SZLab VirtualMixer 磁搅工位设备",
)
class SzlabMixerStirrerDevice:
    def __init__(
        self,
        url: str,
        username: str | None = None,
        password: str | None = None,
        csv_path: str | None = None,
        timeout: float = 300.0,
        auto_connect: bool = True,
        plc_device_id: str = "szlab_poly_plc",
        use_plc_gateway: bool = False,
        **kwargs,
    ):
        self.url = url
        self.timeout = timeout
        self.plc_device_id = plc_device_id
        self._plc_gateway = None
        client_kwargs: dict[str, Any] = {
            "url": url,
            "username": username,
            "password": password,
            "timeout": timeout,
            "auto_connect": auto_connect,
        }
        if csv_path is not None:
            client_kwargs["csv_path"] = csv_path
        self._client = None if use_plc_gateway else SZLabPolyPLCDevice(**client_kwargs)
        self._last_position = 1
        self._status = "Idle"
        self._sample_by_position: dict[int, str] = {}

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
    def _read_optional_variable(self, name: str, use_cache: bool = False) -> Any:
        try:
            return self._read_variable(name, use_cache=use_cache)
        except Exception:
            return None

    @not_action
    def _write_variable(self, name: str, value: Any) -> None:
        if getattr(self, "_plc_gateway", None) is not None:
            self._plc_gateway.write_variable(name, value)
            return
        self._client.write(name, value)

    @not_action
    def _reset_and_pulse(self, name: str) -> None:
        if getattr(self, "_plc_gateway", None) is not None:
            self._plc_gateway.write_variable(name, False)
            self._plc_gateway.write_variable(name, True)
            self._plc_gateway.write_variable(name, False)
            return
        self._client.reset_and_pulse(name)

    @not_action
    def _wait_new_cycle_done(self, name: str) -> bool:
        if getattr(self, "_plc_gateway", None) is not None and hasattr(self._plc_gateway, "wait_new_cycle_done"):
            return self._plc_gateway.wait_new_cycle_done(name, timeout=self.timeout)
        if self._client is not None and hasattr(self._client, "wait_new_cycle_done"):
            return self._client.wait_new_cycle_done(name, timeout=self.timeout)
        if self._client is not None:
            if bool(self._client.read(name)):
                if not self._client.wait_equal(name, False, timeout=self.timeout):
                    return False
            return self._client.wait_equal(name, True, timeout=self.timeout)
        return bool(self._read_variable(name, use_cache=False))

    @not_action
    def _validate_position(self, position: int) -> None:
        if position not in MAGNETIC_STIRRER_SLOT_SENSORS:
            raise ValueError("磁搅位置必须在 1-6 范围内")

    @not_action
    def _station_name(self, position: int) -> str:
        self._validate_position(position)
        return f"S04{position}"

    @not_action
    def _process_selection(self, speed: int, temperature: int) -> int:
        stirring = int(speed) > 0
        heating = int(temperature) > 0
        if stirring and heating:
            return 3
        if heating:
            return 2
        return 1

    @action(auto_prefix=True, description="读取指定磁搅位置占用状态")
    def position_occupied(self, position: int = 1) -> dict[str, Any]:
        try:
            self._validate_position(position)
            station = self._station_name(position)
            occupied = bool(self._read_variable(MAGNETIC_STIRRER_SLOT_SENSORS[position], use_cache=False))
            status_value = self._read_optional_variable(f"{station}磁搅状态", use_cache=False)
            return {
                "success": True,
                "position": position,
                "occupied": occupied,
                "ready": bool(self._read_optional_variable(f"{station}准备信号", use_cache=False)),
                "status_value": status_value,
                "status": MAGNETIC_STIRRER_STATUS_LABELS.get(status_value, "UNKNOWN"),
                "sample_id": self._sample_by_position.get(position, ""),
                "reserved": position in self._sample_by_position,
            }
        except Exception as exc:
            return {"success": False, "message": str(exc), "position": position}

    @action(auto_prefix=True, description="读取全部磁搅位置状态")
    def slot_status(self) -> dict[str, Any]:
        slots = {}
        for position in sorted(MAGNETIC_STIRRER_SLOT_SENSORS):
            station = self._station_name(position)
            occupied = bool(self._read_variable(MAGNETIC_STIRRER_SLOT_SENSORS[position], use_cache=False))
            status_value = self._read_optional_variable(f"{station}磁搅状态", use_cache=False)
            slots[str(position)] = {
                "occupied": occupied,
                "ready": bool(self._read_optional_variable(f"{station}准备信号", use_cache=False)),
                "status_value": status_value,
                "status": MAGNETIC_STIRRER_STATUS_LABELS.get(status_value, "UNKNOWN"),
                "temperature_feedback": self._read_optional_variable(
                    f"磁搅温度反馈_上位机[{position - 1}]",
                    use_cache=False,
                ),
                "sample_id": self._sample_by_position.get(position, ""),
                "reserved": position in self._sample_by_position,
            }
        return {"success": True, "slots": slots}

    @action(auto_prefix=True, description="请求一个空闲磁搅位置")
    def request_idle_position(self, sample_id: str = "", require_ready: bool = True) -> dict[str, Any]:
        for position in sorted(MAGNETIC_STIRRER_SLOT_SENSORS):
            station = self._station_name(position)
            occupied = bool(self._read_variable(MAGNETIC_STIRRER_SLOT_SENSORS[position], use_cache=False))
            ready = bool(self._read_optional_variable(f"{station}准备信号", use_cache=False))
            reserved = position in self._sample_by_position
            if not occupied and not reserved and (ready or not require_ready):
                return {"success": True, "position": position, "sample_id": sample_id}
        return {"success": False, "message": "没有空闲磁搅位置"}

    @action(auto_prefix=True, description="绑定样品到磁搅位置")
    def bind_sample_to_position(
        self,
        sample_id: str,
        position: int = 1,
        require_material: bool = False,
    ) -> dict[str, Any]:
        try:
            self._validate_position(position)
            occupied = bool(self._read_variable(MAGNETIC_STIRRER_SLOT_SENSORS[position], use_cache=False))
            if require_material and not occupied:
                return {"success": False, "message": f"磁搅位置 {position} 未检测到物料"}
            self._sample_by_position[position] = sample_id
            return {
                "success": True,
                "position": position,
                "sample_id": sample_id,
                "material_confirmed": occupied,
                "reserved": True,
            }
        except Exception as exc:
            return {"success": False, "message": str(exc), "position": position}

    @action(auto_prefix=True, description="释放样品与磁搅位置的绑定")
    def release_position(self, position: int = 1) -> dict[str, Any]:
        try:
            self._validate_position(position)
            sample_id = self._sample_by_position.pop(position, "")
            return {"success": True, "position": position, "sample_id": sample_id}
        except Exception as exc:
            return {"success": False, "message": str(exc), "position": position}

    @action(
        auto_prefix=True,
        description="运行工位上的磁搅",
        handles=[
            ActionInputHandle(
                key="stirrer_position",
                data_type="szlab_mixer_stirrer_position",
                label="磁搅位置",
                data_key="position",
                data_source=DataSource.HANDLE,
                description="磁搅工位编号，范围 1-6",
            )
        ],
    )
    def run_stirring(
        self,
        position: int = 1,
        speed: int = 300,
        temperature: int = 25,
        duration: int = 60,
        require_material: bool = True,
    ) -> dict[str, Any]:
        if position not in MAGNETIC_STIRRER_SLOT_SENSORS:
            return {"success": False, "message": "磁搅位置必须在 1-6 范围内"}
        self._status = "Running"
        self._last_position = position
        index = position - 1
        station = f"S04{position}"

        if require_material and not bool(self._read_variable(MAGNETIC_STIRRER_SLOT_SENSORS[position], use_cache=False)):
            self._status = "Error"
            return {"success": False, "message": f"磁搅位置 {position} 未检测到物料"}
        if not bool(self._read_variable(f"{station}允许加工", use_cache=False)):
            self._status = "Error"
            return {"success": False, "message": f"{station} 不允许加工"}

        self._write_variable(f"磁搅速度设置_上位机[{index}]", int(speed))
        self._write_variable(f"磁搅温度设置_上位机[{index}]", int(temperature))
        self._write_variable(f"磁搅安全温度设置_上位机[{index}]", int(max(temperature + 10, temperature)))
        self._write_variable(f"磁搅时间设置_上位机[{index}]", int(duration))
        self._write_variable(f"{station}磁搅工艺选择", self._process_selection(speed=speed, temperature=temperature))
        self._reset_and_pulse(f"{station}参数写入完成")

        if not self._wait_new_cycle_done(f"{station}加工完成"):
            self._status = "Error"
            return {"success": False, "message": f"{station} 加工完成等待超时"}
        self._status = "Idle"
        return {
            "success": True,
            "message": f"磁搅工位 {position} 执行完成",
            "data": {
                "position": position,
                "sample_id": getattr(self, "_sample_by_position", {}).get(position, ""),
                "speed": speed,
                "temperature": temperature,
                "duration": duration,
                "status_value": self._read_optional_variable(f"{station}磁搅状态", use_cache=False),
                "temperature_feedback": self._read_optional_variable(
                    f"磁搅温度反馈_上位机[{index}]",
                    use_cache=False,
                ),
            },
        }
