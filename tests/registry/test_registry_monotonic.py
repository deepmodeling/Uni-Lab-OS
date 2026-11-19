"""注册表加载日志里的耗时按单调时钟计：墙钟回拨不让它变成负数。

只改写墙钟（``time.time``），单调时钟照常走。
"""

from __future__ import annotations

import re
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from typing import Any

from unilabos.registry import registry as registry_module

_Registry = registry_module.Registry.__wrapped__


def _jump_wall_clock(monkeypatch, offset_s: float) -> None:
    real_time = time.time
    monkeypatch.setattr(time, "time", lambda: real_time() + offset_s)


class _Logger:
    def __init__(self) -> None:
        self.infos: list[str] = []

    def info(self, message: str, *_args: Any) -> None:
        self.infos.append(message)

    def trace(self, *_args: Any, **_kwargs: Any) -> None:
        pass

    debug = warning = error = trace


def _seconds(log: _Logger, marker: str) -> float:
    (message,) = [line for line in log.infos if marker in line]
    return float(re.search(r"耗时 (-?\d+(?:\.\d+)?)s", message).group(1))


def test_type_resolution_duration_ignores_backward_wall_clock_step(monkeypatch) -> None:
    log = _Logger()
    monkeypatch.setattr(registry_module, "logger", log)
    monkeypatch.setattr(registry_module, "_ROS_TYPE_RESOLUTION_ENABLED", True)
    registry = SimpleNamespace(
        device_type_registry={"pump": {}},
        resolve_types_for_device=lambda _device_id: _jump_wall_clock(monkeypatch, -30.0),
    )

    _Registry.resolve_all_types(registry)

    assert 0 <= _seconds(log, "类型解析完成") < 5


def test_yaml_device_load_duration_ignores_backward_wall_clock_step(monkeypatch, tmp_path) -> None:
    (tmp_path / "devices").mkdir()
    (tmp_path / "devices" / "pump.yaml").write_text("{}\n", encoding="utf-8")
    log = _Logger()
    monkeypatch.setattr(registry_module, "logger", log)

    def load_single_device_file(_file, _complete_registry):
        _jump_wall_clock(monkeypatch, -30.0)
        return {}, {}, True, []

    with ThreadPoolExecutor(max_workers=1) as executor:
        registry = SimpleNamespace(
            device_type_registry={},
            _startup_executor=executor,
            _load_config_cache=lambda: {},
            _save_config_cache=lambda _cache: None,
            _load_single_device_file=load_single_device_file,
            _record_yaml_error=lambda _file, _error: None,
            _rebuild_device_runtime_data=lambda data: data,
        )
        _Registry.load_device_types(registry, tmp_path, complete_registry=True)

    assert 0 <= _seconds(log, "YAML 设备加载") < 5
