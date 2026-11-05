"""全仓测试共用的隔离。"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate_control_token(monkeypatch):
    """start_server / 调度权威启动会把控制面 token 写进 ``BasicConfig.control_token``：每个测试结束后还原，
    避免一个测试启用的写操作鉴权影响后面的测试。"""

    from unilabos.config.config import BasicConfig

    monkeypatch.setattr(BasicConfig, "control_token", BasicConfig.control_token)
    yield
