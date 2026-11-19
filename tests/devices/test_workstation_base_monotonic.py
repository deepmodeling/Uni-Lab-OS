"""工作站基类的工作流运行时长按单调时钟计：墙钟回拨不让它变成负数。

只改写墙钟（``time.time``），单调时钟照常走。
"""

from __future__ import annotations

import time
from types import SimpleNamespace

from unilabos.devices.workstation.workstation_base import WorkstationBase


class _Workstation(WorkstationBase):
    def __init__(self) -> None:
        super().__init__(deck=None)
        self._ros_node = SimpleNamespace(device_id="ws-1")

    def _execute_workflow_impl(self, workflow_name, parameters) -> bool:
        return True


def test_workflow_runtime_ignores_backward_wall_clock_step(monkeypatch) -> None:
    station = _Workstation()
    assert station.execute_workflow("prepare", {})
    started_at = station.workflow_start_time

    real_time = time.time
    monkeypatch.setattr(time, "time", lambda: real_time() - 30.0)

    assert 0 <= station.workflow_runtime < 5
    # 墙钟开始时间照旧保留，供展示
    assert station.workflow_start_time == started_at
