"""部署库位预留与原生取放使用同一库存身份，且不升级为整机锁。"""

from copy import deepcopy

import pytest

from tests.workflow.test_f08_single_node_execution_plan import HTTP_NODE_A_UUID as A
from tests.workflow.test_f08_single_node_execution_plan import _http_node
from tests.workflow.test_inventory_resource_plan_boundary import (
    _FixedInventoryResources,
    _split_transfer_execution_plan,
)
from unilabos.app.scheduler.inventory.station_resource import StationSiteTarget
from unilabos.workflow.execution_plan import ExecutionPlanBuilder, ExecutionPlanBuildError
from unilabos.workflow.inventory_resource_plan import bind_inventory_resource_plan
from unilabos.workflow.resource_lock_key import parse_site_resource_alias
from unilabos.workflow.resource_lock_plan import (
    compile_template_resource_plan,
    deserialize_resource_plan,
    serialize_resource_plan,
)

ALIAS = "site:workdeck/position_15"
OWNER = "00000000-0000-4000-8000-000000000305"
SITE = "00000000-0000-4000-8000-000000000306"
KEY = f"material/{OWNER}/site/{SITE}/exclusive"


class _Inventory(_FixedInventoryResources):
    def __init__(self):
        super().__init__("/devices/00000000-0000-4000-8000-000000000302")

    def resolve_reservation_site(self, resource_id: str, site_name: str) -> StationSiteTarget:
        assert (resource_id, site_name) == ("workdeck", "position_15")
        return StationSiteTarget(SITE, site_name, OWNER)


def test_site_only_reservation_defers_then_binds_without_device_lock():
    graph = {"nodes": [_http_node(A)], "edges": [], "resource_scopes": [{
        "scope_id": "receive", "kind": "reservation", "resources": [ALIAS], "node_uuids": [A],
    }]}
    before = deepcopy(graph)
    plan, jobs = ExecutionPlanBuilder().build(graph, run_mode="normal", target_node_uuid=None)
    assert plan["inventory_resource_binding"] == "pending"
    with pytest.raises(ExecutionPlanBuildError, match="绑定"):
        ExecutionPlanBuilder._resource_plan(graph=graph, planned_nodes=plan["nodes"],
            planned_edges=[], allow_deferred_inventory=False)
    bound = bind_inventory_resource_plan({"execution_plan": plan, "workflow_snapshot": graph}, jobs, _Inventory())
    resource_plan = deserialize_resource_plan(bound["resource_plan"])
    assert resource_plan.binding_state == "bound"
    assert {resource.canonical_key for resource in resource_plan.resources} == {KEY}
    assert graph == before


def _reserved_transfer():
    plan, snapshot = _split_transfer_execution_plan()
    snapshot["resource_scopes"] = [{
        "scope_id": "receive", "kind": "reservation", "resources": [ALIAS],
        "node_uuids": ["pick", "place"],
    }]
    plan["resource_plan"] = serialize_resource_plan(compile_template_resource_plan({
        "nodes": [{"uuid": node["uuid"]} for node in plan["nodes"]],
        "edges": plan["edges"], "resource_scopes": snapshot["resource_scopes"],
    }))
    return plan, snapshot


def test_site_reservation_merges_with_native_destination_hold():
    plan, snapshot = _reserved_transfer()
    bound = bind_inventory_resource_plan({"execution_plan": plan, "workflow_snapshot": snapshot}, [], _Inventory())
    resource_plan = deserialize_resource_plan(bound["resource_plan"])
    resource, = [resource for resource in resource_plan.resources if resource.canonical_key == KEY]
    reservation = next(scope for scope in resource_plan.scopes if scope.scope_id == "receive")
    assert reservation.resource_ids == (resource.resource_id,)
    intervals = [interval for interval in resource_plan.intervals if interval.resource_id == resource.resource_id]
    assert len(intervals) == 1
    assert intervals[0].acquire_node_uuid == "pick"
    assert intervals[0].release_node_uuid == "place"


@pytest.mark.parametrize("location", ["graph", "workflow"])
def test_site_reservation_rejects_frozen_binding_to_another_site(location):
    plan, snapshot = _reserved_transfer()
    bindings = snapshot["resource_bindings"] if location == "graph" else snapshot.setdefault(
        "workflow", {}).setdefault("meta_data", {}).setdefault("unilab", {}).setdefault("resource_bindings", {})
    bindings[ALIAS] = {"canonical_key": f"material/{OWNER}/site/{OWNER}/exclusive", "kind": "material_site"}
    with pytest.raises(ExecutionPlanBuildError, match="冻结身份不一致"):
        bind_inventory_resource_plan({"execution_plan": plan, "workflow_snapshot": snapshot}, [], _Inventory())


@pytest.mark.parametrize("alias", ["site:", "site:workdeck", "site:/slot", "site:deck/", "site:deck/slot/extra", "site: deck/slot"])
def test_malformed_site_reservation_is_rejected(alias):
    with pytest.raises(ValueError, match="库位资源别名"):
        parse_site_resource_alias(alias)


def test_reservation_site_uses_deployment_identity_not_runtime_uuid():
    from tests.app.test_f05_local_resource_graph_bootstrap import (
        FIRST_SITE_UUID,
        MOUNT_MATERIAL_UUID,
        _bootstrap,
        _ResourceTree,
    )
    from unilabos.app.scheduler.inventory.station_resource import SqliteStationResourceInventory, StationResourceError
    from unilabos.app.scheduler.inventory.store import InventoryStore

    store = InventoryStore(":memory:")
    try:
        _bootstrap(store, _ResourceTree())
        inventory = SqliteStationResourceInventory(store, settle_material_transfer=lambda command: {})
        site = inventory.resolve_reservation_site("m2b_mount", "Slot 1")
        assert (site.owner_material_uuid, site.uuid) == (MOUNT_MATERIAL_UUID, FIRST_SITE_UUID)
        with pytest.raises(StationResourceError):
            inventory.resolve_reservation_site("missing-deployment", "Slot 1")
        with pytest.raises(StationResourceError):
            inventory.resolve_reservation_site("m2b_mount", "missing-slot")
    finally:
        store.close()


def test_legacy_named_site_resource_keeps_its_lock():
    from unilabos.workflow.resource_lock_key import named_resource_lock_key

    alias = "site:legacy"
    graph = {"nodes": [_http_node(A)], "edges": [], "resource_scopes": [{
        "scope_id": "named", "kind": "with", "resources": [alias], "node_uuids": [A],
    }]}
    plan, jobs = ExecutionPlanBuilder().build(graph, run_mode="normal", target_node_uuid=None)
    assert "inventory_resource_binding" not in plan
    assert bind_inventory_resource_plan({"execution_plan": plan}, jobs, None) == plan
    resource, = deserialize_resource_plan(plan["resource_plan"]).resources
    assert resource.canonical_key == named_resource_lock_key(alias)


def test_shared_lexical_alias_does_not_substitute_for_a_reserved_site():
    from unilabos.workflow.resource_lock_key import named_resource_lock_key
    from unilabos.workflow.resource_lock_plan import ResourcePlanError, bind_station_resource_plan

    scopes = [
        {"scope_id": kind, "kind": kind, "resources": [ALIAS], "node_uuids": [A]}
        for kind in ("with", "reservation")
    ]
    template = compile_template_resource_plan({"nodes": [{"uuid": A}], "resource_scopes": scopes})
    with pytest.raises(ResourcePlanError, match="未绑定"):
        bind_station_resource_plan(template, {})
    with pytest.raises(ResourcePlanError, match="库存库位"):
        bind_station_resource_plan(template, {ALIAS: {"canonical_key": named_resource_lock_key(ALIAS)}})
