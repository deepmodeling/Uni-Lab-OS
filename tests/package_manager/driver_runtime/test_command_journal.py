"""通用命令日志跨实例去重、命名空间、旧库迁移和重启恢复。"""

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace

import pytest

from unilabos.runtime.commands import (
    CommandConflictError,
    CommandSequenceError,
    CommandState,
    LegacyCommandTables,
    open_command_journal,
)


@dataclass(frozen=True)
class PumpRequest:
    command_id: str = "dispense-1"
    source: str = "workflow"
    source_boot_id: str = "epoch-a"
    monotonic_sequence: int = 1
    volume: int = 10

    def to_dict(self):
        return dict(self.__dict__)

    def fingerprint(self):
        return json.dumps(self.to_dict(), sort_keys=True)


def test_replay_conflict_epoch_fencing_and_namespace_isolation(tmp_path):
    path = tmp_path / "commands.sqlite3"
    first = open_command_journal(path, namespace="pump-1")
    request = PumpRequest()
    assert first.accept(request, "boot-1")[0] is True
    first.update(request.command_id, CommandState.SUCCEEDED, "boot-1", "完成", {"volume": 10})
    restarted = open_command_journal(path, namespace="pump-1")
    assert restarted.mark_previous_boot_inflight_unknown("boot-2") == 0
    created, prior = restarted.accept(request, "boot-2")
    assert created is False
    assert prior.output == {"volume": 10}
    with pytest.raises(CommandConflictError):
        restarted.accept(replace(request, volume=20), "boot-2")
    with pytest.raises(CommandSequenceError):
        restarted.accept(replace(request, command_id="dispense-2"), "boot-2")
    restarted.accept(replace(request, command_id="dispense-2", source_boot_id="epoch-b"), "boot-2")
    with pytest.raises(CommandSequenceError):
        restarted.accept(replace(request, command_id="dispense-3", monotonic_sequence=2), "boot-2")
    isolated = open_command_journal(path, namespace="pump-2")
    assert isolated.get(request.command_id) is None
    assert isolated.accept(request, "boot-3")[0] is True


def test_separate_connections_accept_only_once_and_preserve_unknown_on_restart(tmp_path):
    path = tmp_path / "commands.sqlite3"
    journals = [open_command_journal(path, namespace="pump") for _ in range(4)]
    request = PumpRequest()
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda journal: journal.accept(request, "boot-a")[0], journals))
    assert results.count(True) == 1
    journals[0].update(request.command_id, CommandState.RUNNING, "boot-a", "运动中")
    restarted = open_command_journal(path, namespace="pump")
    assert restarted.mark_previous_boot_inflight_unknown("boot-b") == 1
    assert restarted.unknown_command_ids() == ["dispense-1"]
    assert restarted.accept(request, "boot-b")[0] is False
    assert restarted.get(request.command_id).state is CommandState.UNKNOWN


def legacy_database(path):
    request = PumpRequest()
    with sqlite3.connect(path) as c:
        c.executescript("""
            CREATE TABLE old_commands(command_id TEXT PRIMARY KEY, request_hash TEXT, request_json TEXT,
                state TEXT, boot_id TEXT, message TEXT, output_json TEXT, created_at REAL, updated_at REAL);
            CREATE TABLE old_epochs(source TEXT,source_boot_id TEXT,last_sequence INTEGER,active INTEGER);
        """)
        c.execute("INSERT INTO old_commands VALUES (?,?,?,?,?,?,?,?,?)", (
            request.command_id, request.fingerprint(), json.dumps(request.to_dict()),
            "RUNNING", "old-boot", "moving", '{"observed":false}', 10.0, 11.0,
        ))
        c.execute("INSERT INTO old_epochs VALUES (?,?,?,1)", (request.source, request.source_boot_id, 1))
    return request


def test_legacy_import_preserves_fingerprint_and_never_resurrects_after_reset(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    request = legacy_database(path)
    tables = LegacyCommandTables("old_commands", "old_epochs")
    journal = open_command_journal(path, namespace="pump", legacy_tables=tables)
    record = journal.get(request.command_id)
    assert record.request_hash == request.fingerprint()
    assert record.output == {"observed": False}
    assert journal.mark_previous_boot_inflight_unknown("new-boot") == 1
    with pytest.raises(CommandSequenceError):
        journal.accept(replace(request, command_id="dispense-2"), "new-boot")
    assert open_command_journal(path, namespace="pump", legacy_tables=tables).has_unknown()
    with pytest.raises(ValueError, match="认领"):
        open_command_journal(path, namespace="another-pump", legacy_tables=tables)
    journal.reset()
    assert open_command_journal(path, namespace="pump", legacy_tables=tables).get(request.command_id) is None


def test_incomplete_legacy_import_fails_without_hiding_old_facts(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    legacy_database(path)
    with sqlite3.connect(path) as c:
        c.execute("DROP TABLE old_epochs")
    with pytest.raises(ValueError, match="不完整"):
        open_command_journal(path, namespace="pump", legacy_tables=LegacyCommandTables("old_commands", "old_epochs"))
    assert open_command_journal(path, namespace="pump").get("dispense-1") is None
    with sqlite3.connect(path) as c:
        assert c.execute("SELECT state FROM old_commands").fetchone()[0] == "RUNNING"
