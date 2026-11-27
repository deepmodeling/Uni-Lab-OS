"""同一合同的车辆角色、动作及词法资源必须绑定同一实际库存设备。"""

from copy import deepcopy
from uuid import uuid4

import pytest

from unilabos.workflow.resource_lock_key import device_lock_key
from unilabos.workflow.station_contract import bind_station_contract


def fixture():
    template = str(uuid4())
    vehicles = [{"uuid": str(uuid4()), "resource_template_uuid": template} for _ in range(2)]
    names = dict(zip(["agv-1", "agv-2"], vehicles))
    names.update({v["uuid"]: v for v in vehicles})
    nodes = [
        {
            "uuid": str(uuid4()),
            "material_uuid": None,
            "meta_data": {"unilab": {"executor_binding": {"mode": "fixed", "device_id": "agv-1"}}},
        }
        for _ in range(4)
    ]
    profile = {
        "schema_version": 1,
        "role": "transport_vehicle",
        "executor_key": "executor_1",
        "resource_alias": "agv-1",
        "equivalent_devices": ["agv-1", "agv-2"],
    }
    contract = {
        "graph_snapshot": {
            "workflow": {
                "meta_data": {
                    "center_transport_allocation": profile,
                    "unilab": {
                        "resource_scopes": [
                            {"hard_boundary": True, "resources": ["agv-1"], "node_uuids": [n["uuid"] for n in nodes]}
                        ]
                    },
                }
            },
            "nodes": nodes,
        },
        "executor_requirements": [{"key": "executor_1", "resource_template_uuid": template}],
        "executor_binding_mapping": {n["uuid"]: "executor_1" for n in nodes},
    }
    binding = {
        "vehicle_id": "agv-2",
        "vehicle_resource_uuid": vehicles[1]["uuid"],
        "executor_bindings": {"transport_vehicle": vehicles[1]["uuid"]},
    }
    return contract, names.__getitem__, binding


def test_bind_all_actions_and_resource_scope_without_mutating_contract():
    contract, resolver, binding = fixture()
    original = deepcopy(contract)
    graph = bind_station_contract(contract, resolver, transport_binding=binding)
    assert contract == original
    assert {n["material_uuid"] for n in graph["nodes"]} == {binding["vehicle_resource_uuid"]}
    assert graph["resource_bindings"]["agv-1"]["canonical_key"] == device_lock_key(binding["vehicle_resource_uuid"])


def test_reject_unapproved_replacement_and_alias_mismatch():
    contract, resolver, binding = fixture()
    with pytest.raises(ValueError):
        bind_station_contract(contract, resolver, transport_binding={**binding, "vehicle_id": "foreign-car"})
    with pytest.raises(ValueError):
        bind_station_contract(contract, resolver, transport_binding={**binding, "vehicle_id": "agv-1"})
    contract["graph_snapshot"]["workflow"]["meta_data"] = {}
    with pytest.raises(ValueError):
        bind_station_contract(contract, resolver, transport_binding=binding)


@pytest.mark.parametrize("mutation", ["wrong_key", "missed_action", "missing_scope", "split_scope"])
def test_reject_partial_role_or_discontinuous_vehicle_scope(mutation):
    from unilabos.workflow.transport_profile import validate_transport_contract

    contract, _, _ = fixture()
    if mutation == "wrong_key":
        contract["executor_binding_mapping"][contract["graph_snapshot"]["nodes"][-1]["uuid"]] = "other"
    elif mutation == "missed_action":
        contract["graph_snapshot"]["nodes"][-1]["meta_data"]["unilab"]["executor_binding"]["device_id"] = "agv-2"
    elif mutation == "missing_scope":
        contract["graph_snapshot"]["workflow"]["meta_data"]["unilab"]["resource_scopes"] = []
    else:
        scopes = contract["graph_snapshot"]["workflow"]["meta_data"]["unilab"]["resource_scopes"]
        scopes[0]["node_uuids"] = scopes[0]["node_uuids"][:-1]
    with pytest.raises(ValueError):
        validate_transport_contract(contract)
