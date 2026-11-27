"""取消后分别保存真实结果和资源收尾，重试不能重跑物理动作。"""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from tests.app.center_client import test_material_outcomes as outcomes
from tests.app.center_client.test_cancellation import cancellation
from tests.app.center_client.test_center_client import INSTANCE, STATION
from tests.app.center_client.test_material_exports import Allocator, bind, complete, inventory  # noqa: F401
from tests.app.center_client.test_runtime import Center
from unilabos.app.center_client.material import MaterialBridge
from unilabos.app.center_client.material_settlement import settle_material_cancellation
from unilabos.app.scheduler.inventory.material_exports import InventoryMaterialAuthority
from unilabos.center_protocol.digest import digest


class SettlementCenter(Allocator):
    def __init__(self):
        super().__init__()
        self.settlements = []
    def settle_canceled_material(self, invocation, body, epoch):
        self.settlements.append(deepcopy(body))
        return {"station_invocation_uuid": invocation, "owner_instance_uuid": INSTANCE,
                "settlement_uuid": body["settlement_uuid"], "request_digest": digest(body),
                "resources_released": body["resources_released"]}


def test_succeeded_material_cancel_settlement_keeps_original_result_and_releases_only_original_hold(tmp_path, inventory):
    inv, backend, template = inventory
    local = backend.create_material({"resource_template_uuid": template, "name": "sample", "barcode": "SAMPLE-1"})["uuid"]
    with outcomes.material_workflow(tmp_path) as (store, _, durable, package, task, scheduler, identity):
        complete(store, task, local, inventory_store=inv)
        authority = InventoryMaterialAuthority(inv, station_uuid=STATION, instance_uuid=INSTANCE, event_epoch_uuid=durable.event_epoch_uuid)
        client = SettlementCenter()
        handoffs = bind(authority, client, local)
        bridge = MaterialBridge(client, store, authority, handoff_authority=handoffs,
            origins=lambda _: (_ for _ in ()).throw(ValueError("原冻结谱系冲突")))
        bridge.pump()
        runtime = SimpleNamespace(store=durable, materials=bridge, handoffs=SimpleNamespace(authority=handoffs), client=client)
        before_jobs = store.list_jobs(task["uuid"])
        before_result = store.get_station_workflow_result(task["uuid"])
        with pytest.raises(ValueError, match="取消"):
            settle_material_cancellation(runtime, identity, reason="核实取消", evidence_refs=["scan:source"])
        message = cancellation(Center(), package)
        durable.receive(message)
        durable.finish_command(message["command"]["command_uuid"])
        state = settle_material_cancellation(runtime, identity, reason="核实取消", evidence_refs=["scan:source"])
        assert state["resources_released"] is True and state["release_receipt"] is not None
        assert len(client.settlements) == 2
        assert client.settlements[0]["execution_status"] == "succeeded"
        assert client.settlements[0]["resources_released"] is False
        assert client.settlements[1]["resources_released"] is True
        assert store.get_task(task["uuid"])["status"] == "succeeded"
        assert store.get_station_workflow_result(task["uuid"]) == before_result
        assert store.list_jobs(task["uuid"]) == before_jobs
        assert store.db.execute("SELECT terminal_recorded FROM center_invocation_inbox WHERE invocation_uuid=?", (identity,)).fetchone()[0] == 0
        assert inv.query_one("SELECT COUNT(*) n FROM inventory_reservation WHERE workflow_id=? AND status='active'", (identity,))["n"] == 0
        assert settle_material_cancellation(runtime, identity, reason="核实取消", evidence_refs=["scan:source"]) == state
        assert len(client.settlements) == 2
