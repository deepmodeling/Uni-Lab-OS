"""真实PG完整备份恢复：原Task/Job、事件缺口和未决交接保护保持不变。"""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
from dataclasses import replace
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID, uuid4

import pytest

from tests.app import test_station_handoff as handoffs
from tests.app.center_client.test_center_client import INVOCATION, FakeTransport, config
from tests.app.center_client.test_event_envelope import draft
from tests.app.center_client.test_event_history import response
from tests.persistence.test_station_handoff import handoff_stations  # noqa: F401
from tests.workflow.test_error_control import seed_job
from unilabos.app.center_client.client import CenterClient, ControlSession
from unilabos.app.center_client.durable import DurableCenterStore
from unilabos.app.center_client.event_history import EventHistoryRecovery, EventHistoryStore
from unilabos.app.scheduler.inventory.store import InventoryStore
from unilabos.persistence.config import DatabaseSettings, use_database_settings
from unilabos.persistence.recovery_mode import EvidenceOnlyError, evidence_only
from unilabos.workflow.store import StoreConflict, WorkflowStore


def test_postgres_backup_preserves_work_and_custody(handoff_stations, database_mode, tmp_path):
    if database_mode != "postgresql":
        pytest.skip("SQLite恢复演练由app/center_client/test_backup_restore.py覆盖")
    tool = os.environ.get("CENTER_BACKUP_TOOL")
    if not tool:
        pytest.skip("需要显式CENTER_BACKUP_TOOL")
    import psycopg
    from psycopg import sql

    setup = handoff_stations
    ids, inventory = setup["ids"], setup["stores"]["target"]
    settings = setup["database_settings"]["target"]
    handoffs.prepared(setup)
    original_protection = inventory.query_all("SELECT * FROM station_handoff")
    original_intents = inventory.query_all("SELECT * FROM station_handoff_outbox")
    assert original_protection and original_intents
    path = tmp_path / "workflow.db"
    with use_database_settings(settings):
        workflow = WorkflowStore(path)
        task, job = seed_job(workflow)
        source = DurableCenterStore(path, station_uuid=ids["target"], instance_uuid=ids["target_instance"])
        with source._db.transaction():
            source._db.execute("UPDATE center_runtime_state SET event_epoch_uuid=?", (ids["target_epoch"],))
        source.event_epoch_uuid = ids["target_epoch"]
        source.save_session(ControlSession(str(uuid4()), str(uuid4()), 1, ids["target"], ids["target_instance"]))
        first = source.record_event(INVOCATION, terminal=False, body=draft())
        second = source.record_event(INVOCATION, terminal=True, body=draft(terminal=True))
        with source._db.transaction():
            source._db.execute("DELETE FROM center_boundary_outbox WHERE event_uuid=?", (second["event_uuid"],))
        source.close()
        workflow.close()
    inventory.close()
    assets = []
    for name, kind, body in (("cert", "public_certificate", "test public certificate"),
                            ("config", "public_config", "tls_required=true"),
                            ("vault", "encrypted_private_material", "age-encryption.org/v1\nsynthetic-opaque-test-data")):
        asset = tmp_path / name
        asset.write_text(body)
        assets.append({"name": name, "kind": kind, "source": str(asset)})
    asset_file = tmp_path / "assets.json"
    asset_file.write_text(json.dumps({"deployment_revision": "postgres-drill", "files": assets,
        "artifacts": [{"name": "runtime", "sha256": "a" * 64, "recovery_ref": "fixture://station-runtime"}]}))
    child_directory = tmp_path / "backup-child-private"
    environment = os.environ | {"UNILAB_BACKUP_SOURCE_URL": settings.database_url, "TMPDIR": str(child_directory)}
    backup_processes_stopped = True

    def command(*args):
        nonlocal backup_processes_stopped
        child_directory.mkdir(mode=0o700)
        # 内部20秒超时先清理service文件；外层180秒覆盖恢复的多步子进程和异常展开。
        timeout_args = ["--timeout", "20"] if args[0] in {"create", "restore"} else []
        child = subprocess.Popen([sys.executable, tool, *args, *timeout_args], env=environment,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
        backup_processes_stopped = False
        try:
            stdout, stderr = child.communicate(timeout=180)
            assert child.returncode == 0, stderr
            return json.loads(stdout)
        finally:
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            child.wait(timeout=10)
            backup_processes_stopped = True
            # 强杀无法执行子进程finally时，由拥有该精确私有目录的父进程回收凭据。
            shutil.rmtree(child_directory)

    bundle, recovered = tmp_path / "backup", tmp_path / "recovered"
    saved = command("create", "--role", "station", "--directory", str(bundle), "--quiesced",
                    "--pg-url-env", "UNILAB_BACKUP_SOURCE_URL", "--assets", str(asset_file))
    try:
        report = command("restore", "--directory", str(bundle), "--expected-manifest-sha256", saved["manifest_sha256"],
            "--destination", str(recovered), "--reason", "真实PG灾备核对，先补原证据", "--station-python", sys.executable,
            "--pg-admin-url-env", "UNILABOS_TEST_POSTGRES_URL")
        assert report["mode"]["mode"] == "evidence_only"
        name = json.loads((recovered / "postgres-target.json").read_text())["database"]
        restored_settings = DatabaseSettings("postgresql", urlunsplit(urlsplit(settings.database_url)._replace(path="/" + name)))
        with use_database_settings(restored_settings):
            restored = WorkflowStore(path)
            stock = InventoryStore(str(tmp_path / "inventory.db"))
            history = EventHistoryStore(path, station_uuid=ids["target"], instance_uuid=ids["target_instance"])
            try:
                assert restored.evidence_only()
                assert stock.query_all("SELECT * FROM station_handoff") == original_protection
                assert stock.query_all("SELECT * FROM station_handoff_outbox") == original_intents
                transport = FakeTransport([response(first, prefix=2), response(second, prefix=2)])
                client = CenterClient(replace(config(tmp_path), station_uuid=ids["target"], instance_uuid=ids["target_instance"]), transport)
                recovery = EventHistoryRecovery(client, history)
                assert recovery.recover(1)["state"] == "recovered"
                assert recovery.recover(2)["state"] == "recovered"
                with restored.read() as conn:
                    assert [json.loads(row[0]) for row in conn.execute("SELECT event_json FROM center_boundary_outbox ORDER BY event_sequence")] == [first, second]
                    assert conn.execute("SELECT COUNT(*) FROM workflow_task").fetchone()[0] == 1
                    assert conn.execute("SELECT COUNT(*) FROM workflow_node_job").fetchone()[0] == 1
                assert restored.get_task(task)["uuid"] == task and restored.get_job(job)["uuid"] == job
                with pytest.raises(EvidenceOnlyError):
                    restored.create_task_with_jobs(workflow_uuid=str(uuid4()), task_uuid=str(uuid4()), run_mode="normal",
                        target_node_uuid=None, description=None, meta_data={}, plan_builder=lambda _: pytest.fail("不能重建任务"))
                assert stock.query_all("SELECT * FROM station_handoff") == original_protection
            finally:
                history.close()
                stock.close()
                restored.close()
        with use_database_settings(settings):
            original = InventoryStore(str(tmp_path / "inventory.db"))
            try:
                assert original.query_all("SELECT * FROM station_handoff") == original_protection
                assert original.query_all("SELECT * FROM station_handoff_outbox") == original_intents
                # 原库仍有刻意制造的事件缺口；恢复只修复隔离副本，原库启动仍应拒绝。
                with pytest.raises(StoreConflict, match="缺口"):
                    WorkflowStore(path)
                with psycopg.connect(settings.database_url) as db:
                    assert [json.loads(row[0]) for row in db.execute("SELECT event_json FROM center_boundary_outbox ORDER BY event_sequence")] == [first]
                assert not evidence_only(original.db)
            finally:
                original.close()
        assert command("verify", "--directory", str(bundle), "--expected-manifest-sha256", saved["manifest_sha256"])["verified"]
        print("postgres-station-backup:", json.dumps(report))
    finally:
        metadata = recovered / "postgres-target.json"
        if metadata.exists() and backup_processes_stopped:
            name = json.loads(metadata.read_text())["database"]
            assert name.startswith("unilab_recovery_") and UUID(name.removeprefix("unilab_recovery_")).hex == name.removeprefix("unilab_recovery_")
            with psycopg.connect(os.environ["UNILABOS_TEST_POSTGRES_URL"], autocommit=True) as db:
                db.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))
