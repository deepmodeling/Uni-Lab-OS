"""S08 单元测试用 mock OPC UA client（不连网，模拟握手变量读写）。"""

from __future__ import annotations

from typing import Any, Sequence

from unilabos.devices.workstation.szlab_poly_studio.s08_decap import decap_s08_cap_station as _s08_module

CAP_CACHE_LENGTH = _s08_module.CAP_CACHE_LENGTH
CAP_STORAGE_SLOT_SENSORS = _s08_module.CAP_STORAGE_SLOT_SENSORS
NODE_PARAMS_WRITTEN = _s08_module.NODE_PARAMS_WRITTEN
NODE_PROCESS_COMPLETE = _s08_module.NODE_PROCESS_COMPLETE
NODE_PROCESS_SELECT = _s08_module.NODE_PROCESS_SELECT
NODE_STATION_STATUS = _s08_module.NODE_STATION_STATUS
SENSOR_CAP_STATION = _s08_module.SENSOR_CAP_STATION
_cap_cache_element_name = _s08_module._cap_cache_element_name


class PseudoSzlabS08OpcUaClient:
    """S08 单元测试用的伪 PLC/OPC UA 客户端。"""

    def __init__(
        self,
        initial_values: dict[str, Any] | None = None,
        *,
        station_ready: bool = True,
        allow_process: bool = True,
    ) -> None:
        self.values: dict[str, Any] = {
            "S08原点信号": station_ready,
            "S08允许加工": allow_process,
            "S08工艺选择": 0,
            "S08参数写入完成": False,
            "S08工艺完成": 0,
            "S082瓶盖暂存位": 0,
            NODE_STATION_STATUS: 2,
            **{node_name: True for node_name in SENSOR_CAP_STATION.values()},
            **{node_name: False for node_name in CAP_STORAGE_SLOT_SENSORS.values()},
            **(initial_values or {}),
        }
        for slot in range(1, 6):
            for index in range(CAP_CACHE_LENGTH):
                self.values.setdefault(_cap_cache_element_name(slot, index), 0)
        self.writes: list[tuple[str, Any]] = []

    def read(self, name: str) -> Any:
        if name not in self.values:
            raise KeyError(f"未找到 OPC UA 节点: {name}")
        if name == NODE_PROCESS_COMPLETE:
            if self.values.get(NODE_PARAMS_WRITTEN):
                return int(self.values.get(NODE_PROCESS_SELECT, 0))
            return int(self.values.get(NODE_PROCESS_COMPLETE, 0))
        return self.values[name]

    def wait_equal(self, name: str, expected: Any, timeout: float = 300.0, interval: float = 0.2) -> bool:
        import time

        start = time.time()
        while time.time() - start < timeout:
            if self.read(name) == expected:
                return True
            time.sleep(interval)
        return False

    def write(self, name: str, value: Any) -> None:
        self.values[name] = value
        self.writes.append((name, value))

    def get_variables(self, variable_names: list[str], use_cache: bool = False) -> dict[str, dict[str, Any]]:
        del use_cache
        return {
            name: {"success": True, "value": self.read(name), "node_id": f"pseudo:{name}"}
            for name in variable_names
        }

    def get_opc_variable_metadata(self, variable_name: str) -> tuple[str, str | None]:
        return variable_name, f"pseudo:{variable_name}" if variable_name in self.values else None

    def disconnect(self) -> None:
        return None

    def seed_slot_sample_id(self, slot: int, sample_id: Sequence[int]) -> None:
        for index, value in enumerate(sample_id):
            self.values[_cap_cache_element_name(slot, index)] = int(value)

    def set_cap_station_present(self, station_id: int, present: bool) -> None:
        self.values[SENSOR_CAP_STATION[station_id]] = present

    def set_cap_storage_slot_present(self, slot: int, present: bool) -> None:
        self.values[CAP_STORAGE_SLOT_SENSORS[slot]] = present

    def set_station_status(self, status_code: int) -> None:
        self.values[NODE_STATION_STATUS] = int(status_code)


SzlabS08CapStationPseudoPlcClient = PseudoSzlabS08OpcUaClient
