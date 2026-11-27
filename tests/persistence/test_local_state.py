"""公共状态接口的持久事务、迁移和文件身份合同。"""

import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from unilabos.runtime.state import open_local_state


def test_restart_readonly_and_atomic_rollback(tmp_path):
    path = tmp_path / "state.db"
    state = open_local_state(path, namespace="test:device")
    with state.transaction():
        state.put("world", "sample", {"volume": 10})
        state.append("evidence", {"effect": "accepted"})
    with pytest.raises(ValueError):
        with state.transaction():
            state.put("world", "sample", {"volume": 20})
            state.append("evidence", {"bad": float("nan")})
    state.close()
    reader = open_local_state(path, namespace="test:device", readonly=True)
    assert reader.get("world", "sample") == {"volume": 10}
    assert reader.items("evidence") == {"1": {"effect": "accepted"}}
    with pytest.raises(PermissionError):
        reader.put("world", "sample", {})
    reader.close()


def test_concurrent_owners_serialize_read_modify_write(tmp_path):
    path = tmp_path / "state.db"
    state = open_local_state(path, namespace="test")
    def increment(_):
        worker = open_local_state(path, namespace="test")
        try:
            with worker.transaction():
                worker.put("world", "counter", worker.get("world", "counter", 0) + 1)
                worker.append("events", "increment")
        finally:
            worker.close()
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(increment, range(40)))
    assert state.get("world", "counter") == 40
    assert len(state.items("events")) == 40
    state.close()


def test_migration_is_atomic_and_only_once(tmp_path):
    path = tmp_path / "state.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE old_state (key TEXT,value INTEGER)")
        db.execute("INSERT INTO old_state VALUES ('counter',7)")
    attempts = []
    def migrate(state, legacy):
        assert legacy.tables == {"old_state"}
        for row in legacy.rows("old_state", ("key", "value")):
            state.put("world", row["key"], row["value"])
        attempts.append(True)
        if len(attempts) == 1:
            raise ValueError("interrupted")
    with pytest.raises(ValueError, match="interrupted"):
        open_local_state(path, namespace="test", initialize=migrate)
    state = open_local_state(path, namespace="test", initialize=migrate)
    state.put("world", "counter", 8)
    state.close()
    state = open_local_state(path, namespace="test", initialize=migrate)
    assert state.get("world", "counter") == 8
    assert len(attempts) == 2
    state.close()


def test_missing_replaced_or_foreign_store_cannot_reset_history(tmp_path):
    path = tmp_path / "state.db"
    state = open_local_state(path, namespace="test")
    state.put("world", "counter", 7)
    with pytest.raises(ValueError, match="命名空间"):
        open_local_state(path, namespace="other")
    path.unlink()
    with pytest.raises(sqlite3.OperationalError):
        state.get("world", "counter")
    assert not path.exists()
    replacement = open_local_state(path, namespace="test")
    with pytest.raises(ValueError, match="替换"):
        state.get("world", "counter")
    replacement.close()
    state.close()


def test_nested_failure_rolls_back_savepoint(tmp_path):
    state = open_local_state(tmp_path / "state.db", namespace="test")
    with state.transaction():
        state.put("world", "counter", 7)
        with pytest.raises(RuntimeError):
            with state.transaction():
                state.put("world", "counter", 99)
                raise RuntimeError()
        assert state.get("world", "counter") == 7
    state.close()
