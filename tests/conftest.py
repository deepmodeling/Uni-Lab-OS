"""全仓测试共用的隔离。"""

from __future__ import annotations

import os

import pytest

# Windows 中文系统的默认编码是 GBK：用例起的子进程（python -c / python -m unilabos）一律跑 UTF-8 模式，
# 与 CI（PYTHONUTF8=1、PYTHONIOENCODING=utf-8）一致；测试进程自己的文本读写都显式写 encoding="utf-8"。
os.environ.setdefault("PYTHONUTF8", "1")
os.environ.setdefault("PYTHONIOENCODING", "utf-8")


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


def pytest_configure(config):
    # 起真实 unilab 进程、单个用例几十秒的端到端用例；-m "not slow" 可跳过
    config.addinivalue_line("markers", "slow: 起真实 unilab 进程的较慢用例")
