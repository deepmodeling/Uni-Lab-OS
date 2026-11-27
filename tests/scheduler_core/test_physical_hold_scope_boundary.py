"""显式资源范围结束于 pickup 时，物理持有必须延续到已证明的 place。"""
from copy import deepcopy

import pytest

from unilabos.workflow.resource_lock_plan import ResourcePlanError, compile_template_resource_plan


def node(name, *resources, **kwargs):
    return {"uuid": name, "resource_defaults": list(resources), **kwargs}


def edge(source, target):
    return {"source_node_uuid": source, "target_node_uuid": target}


def step(operation, owner, site):
    return {"operation": operation, "material": "plate", "endpoint_resource": owner,
            "endpoint_site": site, "carrier_resources": ["west-arm"]}


graph = {
    "nodes": [node("01", "stack"), node("02", "center-arm", "stack"), node("03", "turntable"),
              node("04", "center-arm", "turntable"), node("05", "turntable"), node("06", "turntable"),
              node("07", "west-arm", transfer_step=step("pick", "turntable", "plate-site")),
              node("08", "west-arm", transfer_step=step("place", "g3", "g3-input")), node("09", "g3")],
    "edges": [edge("01", "02"), edge("01", "03"), edge("02", "04"), edge("03", "04"),
              edge("04", "05"), edge("05", "06"), edge("06", "07"), edge("07", "08"), edge("08", "09")],
    "resource_scopes": [{"scope_id": "handoff", "kind": "with", "resources": ["shared-handoff"],
                         "node_uuids": ["02", "03", "04", "05", "06", "07"]}],
}


def test_only_proven_held_resources_cross_exact_six_member_scope():
    plan = compile_template_resource_plan(graph)
    assert plan.scopes[0].node_uuids == ("02", "03", "04", "05", "06", "07")
    aliases = {r.resource_id: r.alias for r in plan.resources}
    for alias in ("west-arm", "g3", "g3-input"):
        held = [i for i in plan.intervals if aliases[i.resource_id] == alias and "07" in i.node_uuids]
        assert len(held) == 1 and "08" in held[0].node_uuids
    for alias in ("stack", "center-arm", "turntable", "shared-handoff"):
        intervals = [i for i in plan.intervals if aliases[i.resource_id] == alias and "07" in i.node_uuids]
        assert intervals and all("08" not in i.node_uuids and "09" not in i.node_uuids for i in intervals)


def test_unproved_physical_hold_cannot_cross_scope_boundary():
    invalid = deepcopy(graph)
    invalid["nodes"][7]["transfer_step"]["material"] = "different-plate"
    with pytest.raises(ResourcePlanError, match="配对唯一 place"):
        compile_template_resource_plan(invalid)


def test_turntable_reservation_keeps_orientation_and_independent_physical_custody():
    selected = deepcopy(graph)
    selected['resource_scopes'][0]['kind'] = 'reservation'
    selected['resource_scopes'][0]['resources'] = ['turntable']
    plan = compile_template_resource_plan(selected)
    aliases = {resource.resource_id: resource.alias for resource in plan.resources}
    intervals = {alias: [interval for interval in plan.intervals
                        if aliases[interval.resource_id] == alias]
                 for alias in ('center-arm', 'turntable', 'west-arm', 'g3-input')}
    assert all(set(i.node_uuids) <= {'02', '04'} for i in intervals['center-arm'])
    assert intervals['turntable'][0].release_node_uuid == '07'
    assert intervals['west-arm'][0].release_node_uuid == '08'
    assert intervals['g3-input'][0].release_node_uuid == '08'
    assert '08' not in plan.scopes[0].node_uuids


def test_extended_hold_shares_only_inside_original_lexical_scope(monkeypatch):
    from types import SimpleNamespace

    from unilabos.app.scheduler.service import EdgeScheduler
    from unilabos.workflow.resource_lock_plan import bind_station_resource_plan

    template = compile_template_resource_plan(graph)
    plan = bind_station_resource_plan(template, {
        r.alias: {"canonical_key": f"/devices/{r.alias}", "kind": "device"}
        for r in template.resources
    })
    held = next(i for i in plan.intervals
                if i.resource_id == next(r.resource_id for r in plan.resources if r.alias == "west-arm")
                and "07" in i.node_uuids)
    assert held.explicit_boundary and held.scope_id == "handoff"
    scheduler = EdgeScheduler()
    run = SimpleNamespace(spec=object(), resource_template_node_uuid=lambda value: value)
    scheduler._workflows["task"] = run
    monkeypatch.setattr(scheduler, "_resource_plan_for_spec", lambda spec: plan)
    scheduler._inflight["job"] = SimpleNamespace(
        job_id="job", workflow_id="task", node_id="03",
        resource_interval_ids=[held.interval_id], operator_released_keys=set(),
    )
    def candidate(node_id):
        return SimpleNamespace(id=node_id, resource_interval_ids=[held.interval_id])
    assert scheduler._shared_scope_holders("task", candidate("02")) == {
        "job": {"/devices/west-arm"}}
    assert scheduler._shared_scope_holders("task", candidate("08")) == {}
    scheduler._inflight["job"].workflow_id = "foreign-task"
    assert scheduler._shared_scope_holders("task", candidate("02")) == {}
