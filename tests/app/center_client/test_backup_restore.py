"""通过独立运维工具恢复工作流/库存副本；只补原事件，不释放保护或重建执行。"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest

from tests.app import test_station_handoff as handoffs
from tests.app.center_client.test_center_client import INVOCATION, FakeTransport, config
from tests.app.center_client.test_event_envelope import draft
from tests.app.center_client.test_event_history import response
from tests.workflow.test_error_control import seed_job
from unilabos.app.center_client.client import CenterClient, ControlSession
from unilabos.app.center_client.durable import DurableCenterStore
from unilabos.app.center_client.event_history import EventHistoryRecovery, EventHistoryStore
from unilabos.app.scheduler.inventory.store import InventoryStore
from unilabos.persistence.recovery_mode import EvidenceOnlyError
from unilabos.workflow.store import WorkflowStore


def test_workflow_and_inventory_backup_restores_original_evidence_and_pending_custody(tmp_path):
    tool = os.environ.get("CENTER_BACKUP_TOOL")
    if not tool:
        pytest.skip("显式设置 CENTER_BACKUP_TOOL 选择独立运维工具，不读取兄弟仓私有实现")
    assert Path(tool).is_file()
    def command(*args):
        result = subprocess.run([sys.executable, tool, *args], capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)

    for setup in handoffs.handoff_inventory_fixture(tmp_path):
        ids = setup["ids"]
        handoffs.prepared(setup)
        inventory = setup["stores"]["target"]
        original_protection = inventory.query_all("SELECT * FROM station_handoff")
        original_intents = inventory.query_all("SELECT * FROM station_handoff_outbox")
        path = tmp_path / "workflow.db"
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
        assets = []
        for name, kind, body in (("cert", "public_certificate", "test public certificate"),
                                 ("config", "public_config", "tls_required=true"),
                                 ("vault", "encrypted_private_material", "age-encryption.org/v1\nsynthetic-opaque-test-data")):
            asset = tmp_path / name
            asset.write_text(body)
            assets.append({"name": name, "kind": kind, "source": str(asset)})
        asset_file = tmp_path / "assets.json"
        asset_file.write_text(json.dumps({"deployment_revision": "drill-version", "files": assets,
            "artifacts": [{"name": "runtime", "sha256": "a" * 64, "recovery_ref": "fixture://station-runtime"}]}))
        bundle, recovered = tmp_path / "backup", tmp_path / "recovered"
        saved = command("create", "--role", "station", "--directory", str(bundle), "--quiesced",
                        "--sqlite", f"workflow={path}", "--sqlite", f"inventory={inventory.path}", "--assets", str(asset_file))
        restored_report = command("restore", "--directory", str(bundle), "--expected-manifest-sha256", saved["manifest_sha256"],
                                 "--destination", str(recovered), "--reason", "核对分库及未决交接，先补原证据", "--station-python", sys.executable)
        assert restored_report["mode"]["mode"] == "evidence_only"
        restored = WorkflowStore(recovered / "db_workflow.sqlite3")
        stock = InventoryStore(str(recovered / "db_inventory.sqlite3"))
        history = EventHistoryStore(recovered / "db_workflow.sqlite3", station_uuid=ids["target"], instance_uuid=ids["target_instance"])
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
                events = [json.loads(row[0]) for row in conn.execute("SELECT event_json FROM center_boundary_outbox ORDER BY event_sequence")]
                assert events == [first, second]
                assert conn.execute("SELECT COUNT(*) FROM workflow_task").fetchone()[0] == 1
                assert conn.execute("SELECT COUNT(*) FROM workflow_node_job").fetchone()[0] == 1
            assert restored.get_task(task)["uuid"] == task and restored.get_job(job)["uuid"] == job
            with pytest.raises(EvidenceOnlyError):
                restored.create_task_with_jobs(workflow_uuid=str(uuid4()), task_uuid=str(uuid4()), run_mode="normal",
                    target_node_uuid=None, description=None, meta_data={}, plan_builder=lambda _: pytest.fail("不能重建任务"))
            assert stock.query_all("SELECT * FROM station_handoff") == original_protection
            assert command("verify", "--directory", str(bundle), "--expected-manifest-sha256", saved["manifest_sha256"])["verified"] is True
            assert inventory.query_all("SELECT * FROM station_handoff") == original_protection
            print("station-backup-drill:", json.dumps(restored_report))
        finally:
            history.close()
            stock.close()
            restored.close()
