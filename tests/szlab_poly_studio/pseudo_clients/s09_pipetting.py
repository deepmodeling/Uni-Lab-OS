"""S09 移液站单元测试用 pseudo OPC UA client。"""

from __future__ import annotations

from typing import Any


class PseudoSzlabS09OpcUaClient:
    def __init__(self, initial_values: dict[str, Any] | None = None, wait_results: dict[tuple[str, Any], bool] | None = None):
        self.values: dict[str, Any] = {
            "S09原点信号_1": True,
            "S09原点信号_2": True,
            "S09原点信号_3": True,
            "S09原点信号_4": True,
            "S09允许加工": True,
            "S09工艺选择": 0,
            "S09参数写入完成": False,
            "S09工艺完成": 0,
            "S09TIP盒工位编号": 0,
            "S09TIP编号": 0,
            "S09液体瓶编号": 0,
            "S09抽液量": 0,
            "S09放液量": 0,
            "S09天平读数稳定": True,
            "S09天平读数": 0.0,
            "S09液体瓶1剩余液量": 0.0,
            "S09液体瓶2剩余液量": 0.0,
            "S09液体瓶3剩余液量": 0.0,
            "S09液体瓶4剩余液量": 0.0,
            "S09液体瓶5剩余液量": 0.0,
            "工站状态[8]": 2,
            **(initial_values or {}),
        }
        self.reads: list[str] = []
        self.writes: list[tuple[str, Any]] = []
        self.pulses: list[str] = []
        self.wait_equal_calls: list[tuple[str, Any]] = []
        self.events: list[tuple[str, str, Any]] = []
        self.wait_results = wait_results or {}

    def read(self, name: str) -> Any:
        self.reads.append(name)
        self.events.append(("read", name, None))
        if name not in self.values:
            raise KeyError(f"未找到 OPC UA 节点: {name}")
        return self.values[name]

    def read_variable(self, name: str, use_cache: bool = False) -> Any:
        del use_cache
        return self.read(name)

    def write(self, name: str, value: Any) -> None:
        self.values[name] = value
        self.writes.append((name, value))
        self.events.append(("write", name, value))

    def write_variable(self, name: str, value: Any) -> bool:
        self.write(name, value)
        return True

    def pulse(self, name: str, value: Any = True, reset_value: Any = False, reset_delay: float = 0.0) -> None:
        del reset_delay
        self.write(name, value)
        self.write(name, reset_value)
        self.pulses.append(name)

    def wait_equal(self, name: str, expected: Any, timeout: float = 300.0, interval: float = 0.2) -> bool:
        del timeout, interval
        self.wait_equal_calls.append((name, expected))
        self.events.append(("wait_equal", name, expected))
        if (name, expected) in self.wait_results:
            return self.wait_results[(name, expected)]
        self.values[name] = expected
        return True

    def get_variables(self, variable_names: list[str], use_cache: bool = False) -> dict[str, dict[str, Any]]:
        del use_cache
        return {name: {"success": True, "value": self.read(name)} for name in variable_names}

    def get_opc_variable_metadata(self, variable_name: str) -> tuple[str, str | None]:
        return variable_name, f"ns=4;s=上位机通讯|{variable_name}"

    def disconnect(self) -> None:
        return None
