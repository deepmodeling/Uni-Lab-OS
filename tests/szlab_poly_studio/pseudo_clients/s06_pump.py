"""S06 注射泵单元测试用 pseudo OPC UA client。"""

from __future__ import annotations

import time
from typing import Any


class PseudoSzlabMixerOpcUaClient:
    """S06 单元测试用的伪 PLC/OPC UA 客户端。"""

    def __init__(self, initial_values: dict[str, Any] | None = None):
        self.values: dict[str, Any] = {
            "S06准备信号": True,
            "S06允许加工": True,
            "S06工艺选择": 0,
            "S06_1号溶液添加量": 0,
            "S06_2号溶液添加量": 0,
            "S06参数写入完成": False,
            "S06加工完成": False,
            "传感器状态_上位机[3].NO[1]": True,
            **(initial_values or {}),
        }
        self.writes: list[tuple[str, Any]] = []
        self.events: list[tuple[Any, ...]] = []
        self.pulses: list[str] = []
        self.wait_equal_calls: list[tuple[str, Any]] = []
        self.force_done_wait_failure = False

    def read(self, name: str) -> Any:
        self.events.append(("read", name))
        if name not in self.values:
            raise KeyError(f"未找到 OPC UA 节点: {name}")
        return self.values[name]

    def read_variable(self, name: str, use_cache: bool = False) -> Any:
        del use_cache
        return self.read(name)

    def write(self, name: str, value: Any) -> None:
        self.events.append(("write", name, value))
        self.values[name] = value
        self.writes.append((name, value))

    def pulse(self, name: str, value: Any = True, reset_value: Any = False, reset_delay: float = 0.0) -> None:
        self.write(name, value)
        if reset_delay:
            time.sleep(reset_delay)
        self.write(name, reset_value)
        self.pulses.append(name)

    def wait_equal(self, name: str, expected: Any, interval: float = 0.2) -> bool:
        del interval
        self.events.append(("wait", name, expected))
        self.wait_equal_calls.append((name, expected))
        return self.values.get(name) == expected

    def wait_sensor_conditions(
        self,
        conditions: dict[str, bool],
        interval: float = 0.2,
        context: str | None = None,
    ) -> tuple[bool, dict[str, Any]]:
        del interval, context
        self.events.append(("wait_sensor_conditions", dict(conditions)))
        values = {name: self.read(name) for name in conditions}
        return all(values[name] == expected for name, expected in conditions.items()), values

    def wait_new_cycle_done(self, name: str, interval: float = 0.2) -> bool:
        del interval
        self.events.append(("wait_new_cycle_done", name))
        if self.force_done_wait_failure:
            return False
        if bool(self.read(name)):
            self.wait_equal_calls.append((name, False))
            self.values[name] = False
        self.wait_equal_calls.append((name, True))
        self.values[name] = True
        return True

    def get_variables(self, variable_names: list[str], use_cache: bool = False) -> dict[str, dict[str, Any]]:
        del use_cache
        return {name: {"success": True, "value": self.read(name)} for name in variable_names}

    def get_opc_variable_metadata(self, variable_name: str) -> tuple[str, str | None]:
        return variable_name, f"ns=2;s={variable_name}"

    def check_variable_accessible(self, variable_name: str) -> tuple[bool, str | None]:
        if variable_name not in self.values:
            return False, "配置中未找到该变量"
        return True, f"ns=2;s={variable_name}"

    def disconnect(self) -> None:
        return None
