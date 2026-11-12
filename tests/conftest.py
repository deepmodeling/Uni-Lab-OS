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


@pytest.fixture(autouse=True)
def _release_database_root_locks():
    """组合根在进程内独占数据库根目录、持有到进程退出；测试进程里每个用例结束后释放本用例取的锁，
    临时目录才能被清理（Windows 不能删除仍被打开的锁文件），后面的用例也不会被前面的锁挡住。"""

    from unilabos.server.database import root_lock

    before = set(root_lock.held_database_roots())
    yield
    for root in root_lock.held_database_roots():
        if root not in before:
            root_lock.release_database_root(root)
