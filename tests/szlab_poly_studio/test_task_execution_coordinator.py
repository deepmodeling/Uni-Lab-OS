import threading
import time
import tempfile
from copy import deepcopy
from dataclasses import replace
from pathlib import Path

import pytest

from scripts import run_workflow_local
from scripts.run_workflow_local import (
    RuntimeConfig,
    RuntimeDeviceFactoryConfig,
    RuntimeOpcSnapshotConfig,
    WorkflowNode,
    route_node_device,
)
from scripts.task_execution_coordinator import (
    TaskApiConflict,
    TaskDispatchPreflightCode,
    TaskExecutionCoordinator,
    _atomic_start_signal_conditions,
    _atomic_task_start_trigger_satisfied,
    _find_free_s04_position,
    _resolve_sample_s04_position,
    _temporary_s08_trigger_satisfied,
    _temporary_s09_trigger_satisfied,
    deterministic_execution_id,
    validate_task_dispatch_preflight,
    workflow_nodes_from_payload,
)
from scripts.workflow_ui import ActionSpec, DEFAULT_PRESET, build_graph_workflow
from unilabos.devices.workstation.szlab_poly_studio.s09_pipetting_station.sensors import (
    S09_TIP_BOX_SENSORS,
)


WORKFLOW_PATH = "task-flow.json"


def _node(node_id: str, method: str, device_name: str = "device") -> WorkflowNode:
    return WorkflowNode(
        uuid=node_id,
        name=f"auto-{method}",
        device_name=device_name,
        param={},
    )


class FakeTriggerPlc:
    def __init__(self, values: dict[str, object]):
        self.values = dict(values)
        self.reads: list[str] = []

    def read_variable(self, name: str, use_cache: bool = True):
        assert use_cache is True
        self.reads.append(name)
        if name not in self.values:
            raise RuntimeError(f"缺少测试变量: {name}")
        return self.values[name]


ATOMIC_START_NODES = [
    WorkflowNode(
        uuid="w01_pick_beaker_s03",
        name="S03 取烧杯",
        device_name="szlab_mixer_robot",
        method="submit_pick_from_s03",
        param={"product_type": 1, "position": "1-1"},
        legacy_route_compatible=False,
    ),
    WorkflowNode(
        uuid="w01_dose_powder_s07",
        name="S07 注粉",
        device_name="szlab_s07_solid_addition",
        method="dose_powder",
        param={"coarse_position": 1, "fine_position": 1},
        legacy_route_compatible=False,
    ),
    WorkflowNode(
        uuid="w02_pick_beaker_s072",
        name="S072 取烧杯",
        device_name="szlab_mixer_robot",
        method="submit_pick_from_s072",
        param={"product_type": 2, "position": 1},
        legacy_route_compatible=False,
    ),
    WorkflowNode(
        uuid="w02_add_solvent_s06",
        name="S06 泵加液",
        device_name="szlab_s06_pump",
        method="run_solvent_addition",
        param={"volume_pump_1": 10, "volume_pump_2": 10},
        legacy_route_compatible=False,
    ),
    WorkflowNode(
        uuid="w03_pick_beaker_s06",
        name="S06 取烧杯",
        device_name="szlab_mixer_robot",
        method="submit_pick_from_s06",
        param={},
        legacy_route_compatible=False,
    ),
    WorkflowNode(
        uuid="w03_add_liquid_s09",
        name="S09 烧杯加液",
        device_name="szlab_mixer_pipetting_station",
        method="add_liquid_with_reusable_tip",
        param={
            "liquid_station_index": 1,
            "solvent_batch_id": "solvent-batch-001",
            "volume": 5000,
            "volume_unit": "raw",
            "S09液体瓶1剩余液量": 10.0,
        },
        legacy_route_compatible=False,
    ),
    WorkflowNode(
        uuid="w04_pick_beaker_s09",
        name="S09 取烧杯",
        device_name="szlab_mixer_robot",
        method="submit_pick_from_s09",
        param={"product_type": 3, "position": 1},
        legacy_route_compatible=False,
    ),
    WorkflowNode(
        uuid="w04_run_stirring_s04",
        name="S04 磁搅",
        device_name="szlab_s04_magnetic_stirring",
        method="run_stirring",
        param={"position": 1},
        legacy_route_compatible=False,
    ),
    WorkflowNode(
        uuid="w06_pick_beaker_s04",
        name="S04 取烧杯",
        device_name="szlab_mixer_robot",
        method="submit_pick_from_s04",
        param={"position": 1},
        legacy_route_compatible=False,
    ),
    WorkflowNode(
        uuid="w05_pick_sample_vial_s03",
        name="S03 取样品瓶",
        device_name="szlab_mixer_robot",
        method="submit_pick_from_s03",
        param={"product_type": 2, "position": "1-1"},
        legacy_route_compatible=False,
    ),
]


def test_sample_vial_start_requires_source_vial_and_empty_s08_target():
    node = next(
        item for item in ATOMIC_START_NODES
        if item.uuid == "w05_pick_sample_vial_s03"
    )

    conditions = _atomic_start_signal_conditions(node)

    assert conditions == {
        "传感器状态_上位机[1].NO[8]": True,
        "传感器状态_上位机[0].NO[6]": False,
        "传感器状态_上位机[3].NO[14]": False,
    }


def test_sample_vial_start_waits_when_s08_target_is_physically_occupied():
    node = next(
        item for item in ATOMIC_START_NODES
        if item.uuid == "w05_pick_sample_vial_s03"
    )
    conditions = _atomic_start_signal_conditions(node)
    conditions["传感器状态_上位机[3].NO[14]"] = True

    assert not _atomic_task_start_trigger_satisfied(
        {"task_instances": []},
        node=node,
        devices={"szlab_poly_plc": FakeTriggerPlc(conditions)},
    )


def test_s08_inbound_reservation_blocks_another_sample_before_place():
    workspace = {
        "templates": [
            {
                "id": "inbound",
                "node_ids": [
                    "w05_pick_sample_vial_s03",
                    "w05_place_sample_vial_s08",
                ],
            }
        ],
        "task_instances": [
            {
                "id": "sample-a-inbound",
                "sample_id": "sample-a",
                "template_id": "inbound",
                "execution_state": {
                    "records": [
                        {
                            "node_id": "w05_pick_sample_vial_s03",
                            "status": "succeeded",
                            "finished_at": 10,
                        }
                    ]
                },
            }
        ],
    }

    assert not _temporary_s08_trigger_satisfied(
        workspace,
        instance_id="sample-b-inbound",
        sample_id="sample-b",
        node_id="w05_pick_sample_vial_s03",
    )
    assert _temporary_s08_trigger_satisfied(
        workspace,
        instance_id="sample-a-inbound",
        sample_id="sample-a",
        node_id="w05_place_sample_vial_s08",
    )


def test_s08_occupied_station_only_allows_owner_until_pick_succeeds():
    workspace = {
        "templates": [
            {
                "id": "inbound",
                "node_ids": [
                    "w05_pick_sample_vial_s03",
                    "w05_place_sample_vial_s08",
                ],
            },
            {
                "id": "outbound",
                "node_ids": ["w07_pick_sample_vial_s08"],
            },
        ],
        "task_instances": [
            {
                "id": "sample-a-inbound",
                "sample_id": "sample-a",
                "template_id": "inbound",
                "execution_state": {
                    "records": [
                        {
                            "node_id": "w05_pick_sample_vial_s03",
                            "status": "succeeded",
                            "finished_at": 10,
                        },
                        {
                            "node_id": "w05_place_sample_vial_s08",
                            "status": "succeeded",
                            "finished_at": 20,
                        },
                    ]
                },
            }
        ],
    }

    assert _temporary_s08_trigger_satisfied(
        workspace,
        instance_id="sample-a-open",
        sample_id="sample-a",
        node_id="w05_open_sample_vial_s08",
    )
    assert not _temporary_s08_trigger_satisfied(
        workspace,
        instance_id="sample-b-open",
        sample_id="sample-b",
        node_id="w05_open_sample_vial_s08",
    )
    assert _temporary_s08_trigger_satisfied(
        workspace,
        instance_id="sample-a-pour",
        sample_id="sample-a",
        node_id="w06_pick_beaker_s09_after_density",
    )
    assert not _temporary_s08_trigger_satisfied(
        workspace,
        instance_id="sample-b-pour",
        sample_id="sample-b",
        node_id="w06_pick_beaker_s09_after_density",
    )
    assert not _temporary_s08_trigger_satisfied(
        workspace,
        instance_id="sample-b-inbound",
        sample_id="sample-b",
        node_id="w05_pick_sample_vial_s03",
    )

    workspace["task_instances"].append(
        {
            "id": "sample-a-outbound",
            "sample_id": "sample-a",
            "template_id": "outbound",
            "execution_state": {
                "records": [
                    {
                        "node_id": "w07_pick_sample_vial_s08",
                        "status": "succeeded",
                        "finished_at": 30,
                    }
                ]
            },
        }
    )

    assert _temporary_s08_trigger_satisfied(
        workspace,
        instance_id="sample-b-inbound",
        sample_id="sample-b",
        node_id="w05_pick_sample_vial_s03",
    )


def test_s09_density_return_reserves_empty_beaker_station_until_place():
    node_ids = [
        "w03_pick_beaker_s06",
        "w03_place_beaker_s09",
        "w03_add_liquid_s09",
        "w04_pick_beaker_s09",
        "w06_pick_beaker_s05_for_density",
        "w05_place_beaker_s09_for_density",
        "w05_measure_density_s09",
        "w06_pick_beaker_s09_after_density",
    ]
    workspace = {
        "templates": [{"id": "main", "node_ids": node_ids}],
        "task_instances": [{
            "id": "sample-a",
            "sample_id": "sample-a",
            "template_id": "main",
            "execution_state": {"records": [
                {"node_id": "w03_pick_beaker_s06", "status": "succeeded", "finished_at": 10},
                {"node_id": "w03_place_beaker_s09", "status": "succeeded", "finished_at": 20},
                {"node_id": "w04_pick_beaker_s09", "status": "succeeded", "finished_at": 30},
                {
                    "node_id": "w06_pick_beaker_s05_for_density",
                    "status": "succeeded",
                    "finished_at": 40,
                },
            ]},
        }],
    }

    assert not _temporary_s09_trigger_satisfied(
        workspace,
        instance_id="sample-b",
        node_id="w03_pick_beaker_s06",
    )
    assert _temporary_s09_trigger_satisfied(
        workspace,
        instance_id="sample-a",
        node_id="w05_place_beaker_s09_for_density",
    )

    workspace["task_instances"][0]["execution_state"]["records"].append(
        {
            "node_id": "w05_place_beaker_s09_for_density",
            "status": "succeeded",
            "finished_at": 50,
        }
    )
    assert _temporary_s09_trigger_satisfied(
        workspace,
        instance_id="sample-a",
        node_id="w05_measure_density_s09",
    )
    assert not _temporary_s09_trigger_satisfied(
        workspace,
        instance_id="sample-b",
        node_id="w03_pick_beaker_s06",
    )

    workspace["task_instances"][0]["execution_state"]["records"].append(
        {
            "node_id": "w06_pick_beaker_s09_after_density",
            "status": "succeeded",
            "finished_at": 60,
        }
    )
    assert _temporary_s09_trigger_satisfied(
        workspace,
        instance_id="sample-b",
        node_id="w03_pick_beaker_s06",
    )


def test_same_sample_s04_process_and_pick_inherit_latest_place_position():
    nodes = {
        "w04_place_beaker_s04": WorkflowNode(
            uuid="w04_place_beaker_s04",
            name="S04 放烧杯",
            device_name="szlab_mixer_robot",
            method="submit_place_to_s04",
            param={"position": 1},
        ),
    }
    templates = {
        "place": {"id": "place", "node_ids": ["w04_place_beaker_s04"]},
        "stir": {"id": "stir", "node_ids": ["w04_run_stirring_s04"]},
    }
    workspace = {
        "task_instances": [
            {
                "id": "sample-a-place",
                "sample_id": "sample-a",
                "template_id": "place",
                "order": 6,
                "payload": {"node_parameters": {"w04_place_beaker_s04": {"position": 4}}},
            },
            {
                "id": "sample-b-place",
                "sample_id": "sample-b",
                "template_id": "place",
                "order": 6,
                "payload": {"node_parameters": {"w04_place_beaker_s04": {"position": 2}}},
            },
        ],
    }
    current = {
        "id": "sample-a-stir",
        "sample_id": "sample-a",
        "template_id": "stir",
        "order": 7,
    }

    assert _resolve_sample_s04_position(
        workspace,
        instance=current,
        templates=templates,
        nodes_by_id=nodes,
    ) == 4


def test_same_sample_s04_position_prefers_runtime_selected_place_result():
    nodes = {
        "w04_place_beaker_s04": WorkflowNode(
            uuid="w04_place_beaker_s04",
            name="S04 放烧杯",
            device_name="szlab_mixer_robot",
            method="submit_place_to_s04",
            param={"position": 2},
        ),
    }
    templates = {
        "place": {"id": "place", "node_ids": ["w04_place_beaker_s04"]},
        "stir": {"id": "stir", "node_ids": ["w04_run_stirring_s04"]},
    }
    workspace = {
        "task_instances": [{
            "id": "sample-a-place",
            "sample_id": "sample-a",
            "template_id": "place",
            "order": 6,
            "execution_state": {
                "records": [{
                    "node_id": "w04_place_beaker_s04",
                    "status": "succeeded",
                    "result": {"success": True, "position": 1},
                }],
            },
        }],
    }

    assert _resolve_sample_s04_position(
        workspace,
        instance={"sample_id": "sample-a", "order": 7},
        templates=templates,
        nodes_by_id=nodes,
    ) == 1


def test_same_sample_s04_position_reads_wrapped_runtime_place_result():
    nodes = {
        "w04_place_beaker_s04": WorkflowNode(
            uuid="w04_place_beaker_s04",
            name="S04 放烧杯",
            device_name="szlab_mixer_robot",
            method="submit_place_to_s04",
            param={"position": 1},
        ),
    }
    templates = {
        "place": {"id": "place", "node_ids": ["w04_place_beaker_s04"]},
        "stir": {"id": "stir", "node_ids": ["w04_run_stirring_s04"]},
    }
    workspace = {
        "task_instances": [{
            "id": "sample-a-place",
            "sample_id": "sample-a",
            "template_id": "place",
            "order": 6,
            "execution_state": {
                "records": [{
                    "node_id": "w04_place_beaker_s04",
                    "status": "succeeded",
                    "result": [{
                        "uuid": "w04_place_beaker_s04",
                        "param": {"position": 2},
                        "result": {"success": True, "position": 2},
                    }],
                }],
            },
        }],
    }

    assert _resolve_sample_s04_position(
        workspace,
        instance={"sample_id": "sample-a", "order": 7},
        templates=templates,
        nodes_by_id=nodes,
    ) == 2


@pytest.mark.parametrize("node", ATOMIC_START_NODES, ids=lambda node: node.uuid)
def test_atomic_tasks_require_all_start_signals(node):
    conditions = _atomic_start_signal_conditions(node)
    plc_values = dict(conditions)
    expected_reads = set(conditions)
    if node.uuid == "w03_pick_beaker_s06":
        plc_values.update({
            "传感器状态_上位机[2].NO[10]": False,
            "S041准备信号": True,
        })
        expected_reads.update({
            "传感器状态_上位机[2].NO[10]",
            "S041准备信号",
        })
    plc = FakeTriggerPlc(plc_values)

    assert _atomic_task_start_trigger_satisfied(
        {"task_instances": []},
        node=node,
        devices={"szlab_poly_plc": plc},
    )
    assert set(plc.reads) == expected_reads

    first_name = next(iter(conditions))
    blocked_values = dict(conditions)
    blocked_values[first_name] = not bool(conditions[first_name])
    assert not _atomic_task_start_trigger_satisfied(
        {"task_instances": []},
        node=node,
        devices={"szlab_poly_plc": FakeTriggerPlc(blocked_values)},
    )


def test_s04_to_s05_waits_while_s05_is_occupied():
    node = next(
        item for item in ATOMIC_START_NODES
        if item.uuid == "w06_pick_beaker_s04"
    )
    conditions = _atomic_start_signal_conditions(node)

    assert conditions == {
        "传感器状态_上位机[2].NO[10]": True,
        "传感器状态_上位机[3].NO[0]": False,
        "S05准备信号": True,
    }
    conditions["传感器状态_上位机[3].NO[0]"] = True
    assert not _atomic_task_start_trigger_satisfied(
        {"task_instances": []},
        node=node,
        devices={"szlab_poly_plc": FakeTriggerPlc(conditions)},
    )


@pytest.mark.parametrize(
    "node_id",
    [
        "w03_pick_beaker_s06",
        "w03_add_liquid_s09",
        "w04_pick_beaker_s09",
    ],
)
def test_s09_atomic_start_does_not_require_robot_home_signal(node_id):
    node = next(item for item in ATOMIC_START_NODES if item.uuid == node_id)

    conditions = _atomic_start_signal_conditions(node)

    assert not any(name.startswith("S09原点信号_") for name in conditions)


def test_s09_process_requires_selected_liquid_station_sensor():
    node = next(
        item
        for item in ATOMIC_START_NODES
        if item.uuid == "w03_add_liquid_s09"
    )
    station_sensor = "传感器状态_上位机[4].NO[9]"
    node = replace(node, param={**node.param, "liquid_station_index": 3})

    conditions = _atomic_start_signal_conditions(node)

    assert conditions[station_sensor] is True
    assert not any(name in S09_TIP_BOX_SENSORS.values() for name in conditions)


def test_s09_start_gate_uses_new_volume_parameter_for_remaining_liquid():
    node = next(
        item
        for item in ATOMIC_START_NODES
        if item.uuid == "w03_add_liquid_s09"
    )
    conditions = _atomic_start_signal_conditions(node)
    insufficient = replace(
        node,
        param={**node.param, "volume": 5000, "S09液体瓶1剩余液量": 0.4},
    )
    sufficient = replace(insufficient, param={**insufficient.param, "volume": 4000})

    assert not _atomic_task_start_trigger_satisfied(
        {"task_instances": []},
        node=insufficient,
        devices={"szlab_poly_plc": FakeTriggerPlc(conditions)},
    )
    assert _atomic_task_start_trigger_satisfied(
        {"task_instances": []},
        node=sufficient,
        devices={"szlab_poly_plc": FakeTriggerPlc(conditions)},
    )


def test_s09_to_s04_start_uses_next_place_node_target_position():
    node = next(
        item
        for item in ATOMIC_START_NODES
        if item.uuid == "w04_pick_beaker_s09"
    )
    next_node = WorkflowNode(
        uuid="w04_place_beaker_s04",
        name="S04 放烧杯",
        device_name="szlab_mixer_robot",
        method="submit_place_to_s04",
        param={"position": 2, "sample_id": "sample-002"},
        legacy_route_compatible=False,
    )

    conditions = _atomic_start_signal_conditions(
        node,
        next_node=next_node,
    )

    assert conditions == {
        "传感器状态_上位机[2].NO[11]": False,
        "S042准备信号": True,
    }


def test_find_free_s04_position_continues_after_one_position_read_error():
    plc = FakeTriggerPlc({
        "传感器状态_上位机[2].NO[11]": False,
        "S042准备信号": True,
    })

    assert _find_free_s04_position({"szlab_poly_plc": plc}) == 2
    assert plc.reads == [
        "传感器状态_上位机[2].NO[10]",
        "传感器状态_上位机[2].NO[11]",
        "S042准备信号",
    ]


@pytest.mark.parametrize(
    ("node_id", "active_node_id"),
    [
        ("w01_pick_beaker_s03", "w01_dose_powder_s07"),
        ("w02_pick_beaker_s072", "w02_add_solvent_s06"),
        ("w03_pick_beaker_s06", "w03_add_liquid_s09"),
        ("w04_pick_beaker_s09", "w03_add_liquid_s09"),
    ],
)
def test_atomic_transport_waits_while_target_station_process_is_active(
    node_id, active_node_id
):
    node = next(item for item in ATOMIC_START_NODES if item.uuid == node_id)
    conditions = _atomic_start_signal_conditions(node)
    workspace = {
        "task_instances": [
            {
                "execution_state": {
                    "active_execution_id": "execution-active",
                    "active_node_id": active_node_id,
                }
            }
        ]
    }

    assert not _atomic_task_start_trigger_satisfied(
        workspace,
        node=node,
        devices={"szlab_poly_plc": FakeTriggerPlc(conditions)},
    )


def test_s06_to_s09_waits_until_at_least_one_s04_position_is_available():
    node = next(
        item for item in ATOMIC_START_NODES
        if item.uuid == "w03_pick_beaker_s06"
    )
    values = _atomic_start_signal_conditions(node)
    for position in range(1, 5):
        values[f"传感器状态_上位机[2].NO[{position + 9}]"] = True
        values[f"S04{position}准备信号"] = True

    assert not _atomic_task_start_trigger_satisfied(
        {"task_instances": []},
        node=node,
        devices={"szlab_poly_plc": FakeTriggerPlc(values)},
    )

    values["传感器状态_上位机[2].NO[12]"] = False
    assert _atomic_task_start_trigger_satisfied(
        {"task_instances": []},
        node=node,
        devices={"szlab_poly_plc": FakeTriggerPlc(values)},
    )


def test_s09_to_s04_transport_can_start_while_another_s04_position_is_stirring():
    node = next(
        item
        for item in ATOMIC_START_NODES
        if item.uuid == "w04_pick_beaker_s09"
    )
    next_node = WorkflowNode(
        uuid="w04_place_beaker_s04",
        name="S04 放烧杯",
        device_name="szlab_mixer_robot",
        method="submit_place_to_s04",
        param={"position": 2, "sample_id": "sample-002"},
        legacy_route_compatible=False,
    )
    workspace = {
        "task_instances": [
            {
                "execution_state": {
                    "active_execution_id": "execution-active",
                    "active_node_id": "w04_run_stirring_s04",
                }
            }
        ]
    }
    plc = FakeTriggerPlc({
        "传感器状态_上位机[2].NO[11]": False,
        "S042准备信号": True,
    })

    assert _atomic_task_start_trigger_satisfied(
        workspace,
        node=node,
        next_node=next_node,
        devices={"szlab_poly_plc": plc},
    )


def _workspace_response(
    *,
    version: int = 7,
    instances: list[dict] | None = None,
    node_ids: list[str] | None = None,
    paused: bool = False,
    pause_reason=None,
) -> dict:
    ids = node_ids or ["node_001_pick_from_s03"]
    return {
        "version": version,
        "workspace": {
            "scheduler_paused": paused,
            "pause_reason": pause_reason,
            "templates": [{"id": "template-1", "node_ids": ids}],
            "task_instances": instances
            or [
                {
                    "id": "instance-1",
                    "template_id": "template-1",
                    "status": "running",
                    "payload": {},
                    "execution_state": {
                        "cursor": 0,
                        "records": [],
                        "active_execution_id": None,
                        "active_node_id": None,
                    },
                }
            ],
            "dynamic_resource_leases": [],
        },
    }


class FakeTaskClient:
    def __init__(self, response: dict):
        self.response = response
        self.claims = []
        self.succeeded = []
        self.failed = []
        self.claim_error = None

    def get_workspace(self, *, workflow_path):
        assert workflow_path == WORKFLOW_PATH
        return deepcopy(self.response)

    def claim_action(self, **payload):
        if self.claim_error is not None:
            raise self.claim_error
        self.claims.append(payload)
        self.response["version"] += 1
        return deepcopy(self.response)

    def succeed_action(self, **payload):
        self.succeeded.append(payload)
        self.response["version"] += 1
        return deepcopy(self.response)

    def fail_action(self, **payload):
        self.failed.append(payload)
        self.response["version"] += 1
        return deepcopy(self.response)


class FakePLC:
    def __init__(self, *, home=True, write_allowed=True, connected=True):
        self.client = object() if connected else None
        self.home = home
        self.write_allowed = write_allowed
        self.reads = []

    def get_variables(self, names, use_cache=True):
        self.reads.append((names, use_cache))
        return {
            "Robot_Home": {"success": True, "value": self.home},
            "Robot_任务允许写入": {
                "success": True,
                "value": self.write_allowed,
            },
        }


class FakeActionDevice:
    def __getattr__(self, _name):
        return lambda **_kwargs: {"success": True}


def _devices(plc=None):
    return {
        "plc": plc or FakePLC(),
        "device": FakeActionDevice(),
        "robot-a": FakeActionDevice(),
        "robot-b": FakeActionDevice(),
    }


def _coordinator(client, runner, devices=None):
    current_devices = devices or _devices()
    return TaskExecutionCoordinator(
        task_client=client,
        node_runner=runner,
        device_provider=lambda: current_devices,
        max_workers=4,
    )


def test_execution_id_is_deterministic_and_cursor_sensitive():
    first = deterministic_execution_id("instance-1", 2, "node-3")
    assert first == deterministic_execution_id("instance-1", 2, "node-3")
    assert first != deterministic_execution_id("instance-1", 3, "node-3")
    assert first != deterministic_execution_id("instance-2", 2, "node-3")


def test_dispatch_preflight_error_codes_are_stable():
    assert {item.value for item in TaskDispatchPreflightCode} == {
        "task_dispatch_scope_empty",
        "workspace_recovery_required",
        "task_template_missing",
        "task_template_empty",
        "task_node_missing",
        "task_node_not_executable",
        "task_node_invalid",
        "task_device_missing",
        "task_action_unsupported",
    }


def test_dispatch_preflight_accepts_matching_active_task():
    workspace = _workspace_response()["workspace"]

    result = validate_task_dispatch_preflight(
        workspace,
        [_node("node_001_pick_from_s03", "submit_pick_from_s03")],
        _devices(),
    )

    assert result.valid is True
    assert result.as_dict() == {
        "valid": True,
        "errors": [],
        "warnings": [],
    }


def test_dispatch_preflight_reports_task_node_missing_before_dispatch():
    workspace = _workspace_response(node_ids=["task-action"])["workspace"]
    workspace["templates"][0]["name"] = "样品处理"

    result = validate_task_dispatch_preflight(
        workspace,
        [_node("standalone-action", "run")],
        _devices(),
    )

    assert result.valid is False
    assert [item.code for item in result.errors] == [
        TaskDispatchPreflightCode.NODE_MISSING
    ]
    assert result.errors[0].template_id == "template-1"
    assert result.errors[0].template_name == "样品处理"
    assert result.errors[0].node_id == "task-action"
    assert result.errors[0].instance_ids == ("instance-1",)
    assert result.errors[0].as_dict()["category"] == "dispatch_preflight"
    assert result.errors[0].as_dict()["phase"] == "preflight"


def test_dispatch_preflight_checks_scheduled_template_without_active_instance():
    workspace = _workspace_response()["workspace"]
    workspace["scheduled_template_ids"] = ["template-1"]
    workspace["task_instances"] = []

    result = validate_task_dispatch_preflight(
        workspace,
        [_node("node_001_pick_from_s03", "submit_pick_from_s03")],
        _devices(),
    )

    assert result.valid is True


def test_dispatch_preflight_ignores_inactive_historical_template():
    workspace = _workspace_response()["workspace"]
    workspace["templates"].append({
        "id": "historical-template",
        "name": "历史模板",
        "node_ids": ["deleted-node"],
    })

    result = validate_task_dispatch_preflight(
        workspace,
        [_node("node_001_pick_from_s03", "submit_pick_from_s03")],
        _devices(),
    )

    assert result.valid is True


@pytest.mark.parametrize(
    ("node", "devices", "expected_code"),
    [
        (
            replace(_node("node_001_pick_from_s03", "run"), disabled=True),
            {"device": FakeActionDevice()},
            TaskDispatchPreflightCode.NODE_NOT_EXECUTABLE,
        ),
        (
            WorkflowNode(
                uuid="node_001_pick_from_s03",
                name="invalid",
                device_name="device",
                param={},
                legacy_route_compatible=False,
            ),
            {"device": FakeActionDevice()},
            TaskDispatchPreflightCode.NODE_INVALID,
        ),
        (
            _node("node_001_pick_from_s03", "run", "missing-device"),
            {},
            TaskDispatchPreflightCode.DEVICE_MISSING,
        ),
        (
            _node("node_001_pick_from_s03", "run"),
            {"device": object()},
            TaskDispatchPreflightCode.ACTION_UNSUPPORTED,
        ),
    ],
)
def test_dispatch_preflight_separates_action_readiness_errors(
    node, devices, expected_code
):
    result = validate_task_dispatch_preflight(
        _workspace_response()["workspace"],
        [node],
        devices,
    )

    assert [item.code for item in result.errors] == [expected_code]


def test_dispatch_preflight_rejects_empty_scope_and_failed_pause():
    workspace = _workspace_response()["workspace"]
    workspace["task_instances"] = [{
        "id": "completed-instance",
        "template_id": "template-1",
        "status": "completed",
    }]
    workspace["pause_reason"] = {
        "code": "action_failed",
        "message": "首动作失败",
    }

    result = validate_task_dispatch_preflight(workspace, [], {})

    assert [item.code for item in result.errors] == [
        TaskDispatchPreflightCode.RECOVERY_REQUIRED,
        TaskDispatchPreflightCode.EMPTY_SCOPE,
    ]


def test_claim_submits_action_once_and_tick_returns_without_waiting():
    client = FakeTaskClient(_workspace_response())
    started = threading.Event()
    release = threading.Event()
    calls = []

    def runner(node, devices, _action_callable):
        calls.append((node.uuid, devices))
        started.set()
        release.wait(timeout=2)
        return [{"success": True}]

    coordinator = _coordinator(client, runner)
    started_at = time.monotonic()
    first = coordinator.cycle(
        workflow_path=WORKFLOW_PATH,
        workflow_nodes=[_node("node_001_pick_from_s03", "submit_pick_from_s03")],
    )
    elapsed = time.monotonic() - started_at
    assert started.wait(timeout=1)
    second = coordinator.cycle(
        workflow_path=WORKFLOW_PATH,
        workflow_nodes=[_node("node_001_pick_from_s03", "submit_pick_from_s03")],
    )

    assert elapsed < 0.2
    assert first["claimed"] == 1
    assert second["claimed"] == 0
    assert len(client.claims) == 1
    assert len(calls) == 1
    release.set()
    coordinator.shutdown()


def test_action_runner_receives_complete_log_context():
    response = _workspace_response()
    response["workspace"]["task_instances"][0]["sample_id"] = "Sample A"
    client = FakeTaskClient(response)
    received_context = {}
    finished = threading.Event()

    def runner(node, _devices, _action_callable, context):
        received_context.update(context)
        finished.set()
        return [{"success": True}]

    coordinator = _coordinator(client, runner)
    coordinator.cycle(
        workflow_path=WORKFLOW_PATH,
        workflow_nodes=[_node("node_001_pick_from_s03", "submit_pick_from_s03")],
    )

    assert finished.wait(timeout=1)
    assert received_context["workflow_path"] == WORKFLOW_PATH
    assert received_context["instance_id"] == "instance-1"
    assert received_context["sample_id"] == "Sample A"
    assert received_context["template_id"] == "template-1"
    assert received_context["node_id"] == "node_001_pick_from_s03"
    assert received_context["device_id"] == "device"
    assert received_context["action_name"] == "submit_pick_from_s03"
    assert received_context["execution_id"].startswith("task-action-")
    coordinator.shutdown()


def test_s04_stirring_dispatches_different_positions_concurrently():
    instances = []
    for instance_id, position in (("sample-a-stir", 1), ("sample-b-stir", 2)):
        instances.append({
            "id": instance_id,
            "template_id": "template-1",
            "status": "running",
            "payload": {
                "node_parameters": {
                    "w04_run_stirring_s04": {"position": position},
                }
            },
            "execution_state": {
                "cursor": 0,
                "records": [],
                "active_execution_id": None,
                "active_node_id": None,
            },
        })
    client = FakeTaskClient(_workspace_response(
        instances=instances,
        node_ids=["w04_run_stirring_s04"],
    ))
    release = threading.Event()
    started_positions = []

    def runner(node, _devices, _action_callable):
        started_positions.append(node.param["position"])
        release.wait(timeout=2)
        return [{"success": True}]

    coordinator = _coordinator(
        client,
        runner,
        devices={"szlab_s04_magnetic_stirring": FakeActionDevice()},
    )
    result = coordinator.cycle(
        workflow_path=WORKFLOW_PATH,
        workflow_nodes=[WorkflowNode(
            uuid="w04_run_stirring_s04",
            name="S04 magnetic stirring",
            device_name="szlab_s04_magnetic_stirring",
            method="run_stirring",
            param={"position": 1},
        )],
    )

    assert result["claimed"] == 2
    deadline = time.monotonic() + 1
    while len(started_positions) < 2 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert sorted(started_positions) == [1, 2]
    release.set()
    coordinator.shutdown()


def test_instance_parameter_overrides_are_passed_to_the_action_node():
    instance = _workspace_response()["workspace"]["task_instances"][0]
    instance["payload"] = {
        "node_parameters": {
            "node_001_pick_from_s03": {"volume_ml": 12.5},
        }
    }
    client = FakeTaskClient(_workspace_response(instances=[instance]))
    started = threading.Event()
    seen_params = {}

    def runner(node, _devices, _action_callable):
        seen_params.update(node.param)
        started.set()
        return [{"success": True}]

    coordinator = _coordinator(client, runner)
    coordinator.cycle(
        workflow_path=WORKFLOW_PATH,
        workflow_nodes=[
            WorkflowNode(
                uuid="node_001_pick_from_s03",
                name="auto-submit_pick_from_s03",
                device_name="device",
                param={"volume_ml": 5, "target": "S03"},
            )
        ],
    )

    assert started.wait(timeout=1)
    assert seen_params == {"volume_ml": 12.5, "target": "S03"}
    coordinator.shutdown()


def test_completed_future_is_reported_with_json_safe_summary_and_empty_release():
    client = FakeTaskClient(
        _workspace_response(node_ids=["node_003_dose_powder"])
    )
    coordinator = _coordinator(
        client,
        lambda _node, _devices, _action_callable: [
            {"success": True, "value": object(), "items": (1, 2)}
        ],
    )
    nodes = [_node("node_003_dose_powder", "dose_powder")]

    coordinator.cycle(workflow_path=WORKFLOW_PATH, workflow_nodes=nodes)
    result = _harvest(coordinator, nodes)

    assert result["completed"] == 1
    assert client.succeeded[0]["release_resources"] == []
    assert client.succeeded[0]["result"][0]["success"] is True
    assert isinstance(client.succeeded[0]["result"][0]["value"], str)
    assert client.succeeded[0]["result"][0]["items"] == [1, 2]
    assert client.failed == []


def test_harvest_only_reports_done_future_without_dispatch_read_or_claim():
    class CountingTaskClient(FakeTaskClient):
        def __init__(self, response):
            super().__init__(response)
            self.workspace_reads = 0

        def get_workspace(self, *, workflow_path):
            self.workspace_reads += 1
            return super().get_workspace(workflow_path=workflow_path)

    client = CountingTaskClient(
        _workspace_response(node_ids=["node_003_dose_powder"], paused=False)
    )
    runner_calls = []
    started = threading.Event()
    release = threading.Event()

    def runner(node, _devices, _action_callable):
        runner_calls.append(node.uuid)
        started.set()
        release.wait(timeout=1)
        return [{"success": True}]

    coordinator = _coordinator(client, runner)
    nodes = [_node("node_003_dose_powder", "dose_powder")]
    first = coordinator.cycle(
        workflow_path=WORKFLOW_PATH,
        workflow_nodes=nodes,
    )
    assert first["claimed"] == 1
    assert started.wait(timeout=1)
    in_flight = next(iter(coordinator._in_flight.values()))
    release.set()
    in_flight.future.result(timeout=1)

    result = coordinator.cycle(
        workflow_path=WORKFLOW_PATH,
        workflow_nodes=nodes,
        harvest_only=True,
    )

    assert result["completed"] == 1
    assert result["claimed"] == 0
    assert result["in_flight"] == 0
    assert len(client.claims) == 1
    assert client.workspace_reads == 2
    assert runner_calls == ["node_003_dose_powder"]
    coordinator.shutdown()


def test_succeed_transport_failure_retries_without_failing_or_rerunning():
    class SucceedOnceUnavailableClient(FakeTaskClient):
        def __init__(self, response):
            super().__init__(response)
            self.succeed_attempts = 0

        def succeed_action(self, **payload):
            self.succeed_attempts += 1
            if self.succeed_attempts == 1:
                raise TimeoutError("scheduler timeout")
            return super().succeed_action(**payload)

    client = SucceedOnceUnavailableClient(_workspace_response())
    plc = FakePLC()
    calls = []

    def runner(_node, _devices, _action_callable):
        calls.append(1)
        return [{"success": True, "items": (1, 2)}]

    coordinator = _coordinator(client, runner, devices=_devices(plc))
    nodes = [_node("node_001_pick_from_s03", "submit_pick_from_s03")]
    coordinator.cycle(workflow_path=WORKFLOW_PATH, workflow_nodes=nodes)
    first_report = _wait_for(
        coordinator,
        nodes,
        lambda: client.succeed_attempts == 1,
    )
    second_report = coordinator.cycle(
        workflow_path=WORKFLOW_PATH,
        workflow_nodes=nodes,
    )

    assert first_report["completed"] == 0
    assert first_report["failed"] == 0
    assert first_report["in_flight"] == 1
    assert [item["code"] for item in first_report["diagnostics"]] == [
        "terminal_report_failed"
    ]
    assert first_report["diagnostics"][0]["phase"] == "reporting"
    assert second_report["completed"] == 1
    assert client.succeed_attempts == 2
    assert client.succeeded[0]["result"] == [
        {"success": True, "items": [1, 2]}
    ]
    assert client.failed == []
    assert calls == [1]
    assert plc.reads == []


def test_submit_failure_is_reported_synchronously_and_pauses():
    class RaisingExecutor:
        def submit(self, *_args, **_kwargs):
            raise RuntimeError("executor unavailable")

    client = FakeTaskClient(_workspace_response())
    coordinator = _coordinator(client, lambda *_: None)
    coordinator._executor.shutdown()
    coordinator._executor = RaisingExecutor()
    nodes = [_node("node_001_pick_from_s03", "submit_pick_from_s03")]

    result = coordinator.cycle(
        workflow_path=WORKFLOW_PATH,
        workflow_nodes=nodes,
    )

    assert result["claimed"] == 1
    assert result["failed"] == 1
    assert result["in_flight"] == 0
    assert [item["code"] for item in result["diagnostics"]] == [
        "action_dispatch_failed"
    ]
    assert result["diagnostics"][0]["phase"] == "dispatching"
    assert result["diagnostics"][0]["execution_id"].startswith(
        "task-action-"
    )
    assert client.failed[0]["error"] == {
        "code": "action_dispatch_failed",
        "message": "executor unavailable",
    }


def test_submit_failure_conflict_is_retried_by_harvest_only():
    class RaisingExecutor:
        def submit(self, *_args, **_kwargs):
            raise RuntimeError("executor unavailable")

    class ConflictOnceClient(FakeTaskClient):
        def __init__(self, response):
            super().__init__(response)
            self.fail_attempts = 0

        def fail_action(self, **payload):
            self.fail_attempts += 1
            if self.fail_attempts == 1:
                raise TaskApiConflict("version_conflict", "stale")
            return super().fail_action(**payload)

    client = ConflictOnceClient(_workspace_response())
    coordinator = _coordinator(client, lambda *_: None)
    coordinator._executor.shutdown()
    coordinator._executor = RaisingExecutor()
    nodes = [_node("node_001_pick_from_s03", "submit_pick_from_s03")]

    first = coordinator.cycle(
        workflow_path=WORKFLOW_PATH,
        workflow_nodes=nodes,
    )
    harvested = coordinator.cycle(
        workflow_path=WORKFLOW_PATH,
        workflow_nodes=[],
        harvest_only=True,
    )

    assert first["claimed"] == 1
    assert first["failed"] == 0
    assert harvested["failed"] == 1
    assert client.fail_attempts == 2
    assert client.failed[0]["error"]["code"] == "action_dispatch_failed"


def test_pending_dispatch_failure_keeps_harvest_active_until_report_succeeds():
    class RaisingExecutor:
        def submit(self, *_args, **_kwargs):
            raise RuntimeError("executor unavailable")

        def shutdown(self, **_kwargs):
            return None

    class FailThreeTimesClient(FakeTaskClient):
        def __init__(self, response):
            super().__init__(response)
            self.fail_attempts = 0

        def fail_action(self, **payload):
            self.fail_attempts += 1
            if self.fail_attempts <= 3:
                raise TaskApiConflict("version_conflict", "stale")
            return super().fail_action(**payload)

    client = FailThreeTimesClient(_workspace_response())
    action_calls = []
    coordinator = _coordinator(
        client,
        lambda *_: action_calls.append(1),
    )
    coordinator._executor.shutdown()
    coordinator._executor = RaisingExecutor()
    nodes = [_node("node_001_pick_from_s03", "submit_pick_from_s03")]

    first = coordinator.cycle(
        workflow_path=WORKFLOW_PATH,
        workflow_nodes=nodes,
    )
    retry_one = coordinator.cycle(
        workflow_path=WORKFLOW_PATH,
        workflow_nodes=[],
        harvest_only=True,
    )
    retry_two = coordinator.cycle(
        workflow_path=WORKFLOW_PATH,
        workflow_nodes=[],
        harvest_only=True,
    )
    recovered = coordinator.cycle(
        workflow_path=WORKFLOW_PATH,
        workflow_nodes=[],
        harvest_only=True,
    )

    for pending in (first, retry_one, retry_two):
        assert pending["success"] is True
        assert pending["active"] == 1
        assert pending["in_flight"] == 1
        assert pending["failed"] == 0
    assert recovered["success"] is True
    assert recovered["active"] == 0
    assert recovered["in_flight"] == 0
    assert recovered["failed"] == 1
    assert action_calls == []
    assert len(client.claims) == 1
    assert client.fail_attempts == 4
    assert coordinator._pending_terminal_reports == {}


def test_shutdown_reports_unresolved_pending_dispatch_failure():
    class RaisingExecutor:
        def submit(self, *_args, **_kwargs):
            raise RuntimeError("executor unavailable")

        def shutdown(self, **_kwargs):
            return None

    class OfflineFailClient(FakeTaskClient):
        def __init__(self, response):
            super().__init__(response)
            self.fail_attempts = 0

        def fail_action(self, **_payload):
            self.fail_attempts += 1
            raise ConnectionError("scheduler offline")

    client = OfflineFailClient(_workspace_response())
    action_calls = []
    coordinator = _coordinator(
        client,
        lambda *_: action_calls.append(1),
    )
    coordinator._executor.shutdown()
    coordinator._executor = RaisingExecutor()
    nodes = [_node("node_001_pick_from_s03", "submit_pick_from_s03")]

    first = coordinator.cycle(
        workflow_path=WORKFLOW_PATH,
        workflow_nodes=nodes,
    )
    shutdown_result = coordinator.shutdown()

    assert first["in_flight"] == 1
    assert shutdown_result["success"] is False
    assert shutdown_result["in_flight"] == 1
    assert shutdown_result["failed"] == 0
    assert "终态上报暂未完成" in shutdown_result["message"]
    assert client.fail_attempts == 2
    assert action_calls == []
    assert len(coordinator._pending_terminal_reports) == 1


def test_action_false_result_reports_failure_without_retry():
    client = FakeTaskClient(
        _workspace_response(node_ids=["node_003_dose_powder"])
    )
    calls = []

    def runner(_node, _devices, _action_callable):
        calls.append(1)
        return [{"success": False, "message": "dose failed"}]

    coordinator = _coordinator(client, runner)
    nodes = [_node("node_003_dose_powder", "dose_powder")]
    coordinator.cycle(workflow_path=WORKFLOW_PATH, workflow_nodes=nodes)
    result = _harvest(coordinator, nodes)

    assert result["failed"] == 1
    assert client.failed[0]["error"]["code"] == "action_failed"
    second = coordinator.cycle(
        workflow_path=WORKFLOW_PATH,
        workflow_nodes=nodes,
    )
    time.sleep(0.01)
    assert second["claimed"] == 0
    assert calls == [1]
    assert client.succeeded == []


@pytest.mark.parametrize(
    "action_result",
    [
        False,
        (False, "tuple failed"),
        [False, "list failed"],
        {"success": False, "message": "dict failed"},
        [{"success": False, "message": "list dict failed"}],
    ],
)
def test_action_return_contract_reports_strict_false_as_failure(action_result):
    client = FakeTaskClient(
        _workspace_response(node_ids=["node_003_dose_powder"])
    )
    coordinator = _coordinator(client, lambda *_: action_result)
    nodes = [_node("node_003_dose_powder", "dose_powder")]

    coordinator.cycle(workflow_path=WORKFLOW_PATH, workflow_nodes=nodes)
    result = _harvest(coordinator, nodes)

    assert result["failed"] == 1
    assert client.failed[0]["error"]["code"] == "action_failed"
    assert client.succeeded == []


@pytest.mark.parametrize(
    "action_result",
    [True, None, 0, (True, "ok"), [True, "ok"], {"value": "ok"}],
)
def test_action_return_contract_accepts_non_false_results(action_result):
    client = FakeTaskClient(
        _workspace_response(node_ids=["node_003_dose_powder"])
    )
    coordinator = _coordinator(client, lambda *_: action_result)
    nodes = [_node("node_003_dose_powder", "dose_powder")]

    coordinator.cycle(workflow_path=WORKFLOW_PATH, workflow_nodes=nodes)
    result = _harvest(coordinator, nodes)

    assert result["completed"] == 1
    assert client.succeeded
    assert client.failed == []


def test_runner_exception_reports_failure_without_retry():
    client = FakeTaskClient(
        _workspace_response(node_ids=["node_003_dose_powder"])
    )
    calls = []

    def runner(_node, _devices, _action_callable):
        calls.append(1)
        raise RuntimeError("pump exploded")

    coordinator = _coordinator(client, runner)
    nodes = [_node("node_003_dose_powder", "dose_powder")]
    coordinator.cycle(workflow_path=WORKFLOW_PATH, workflow_nodes=nodes)
    result = _harvest(coordinator, nodes)
    second = coordinator.cycle(
        workflow_path=WORKFLOW_PATH,
        workflow_nodes=nodes,
    )
    time.sleep(0.01)

    assert result["failed"] == 1
    assert client.failed[0]["error"] == {
        "code": "action_failed",
        "message": "pump exploded",
    }
    assert second["claimed"] == 0
    assert calls == [1]


@pytest.mark.parametrize("code", ["version_conflict"])
def test_claim_conflicts_are_normal_waits(code):
    client = FakeTaskClient(_workspace_response())
    client.claim_error = TaskApiConflict(code, "busy")
    calls = []
    coordinator = _coordinator(client, lambda *_: calls.append(1))

    result = coordinator.cycle(
        workflow_path=WORKFLOW_PATH,
        workflow_nodes=[_node("node_001_pick_from_s03", "submit_pick_from_s03")],
    )

    assert result["claimed"] == 0
    assert result["failed"] == 0
    assert calls == []
    assert client.failed == []


def test_resource_leased_claim_conflict_is_not_a_coordinator_wait():
    client = FakeTaskClient(_workspace_response())
    client.claim_error = TaskApiConflict("resource_leased", "obsolete")
    coordinator = _coordinator(client, lambda *_: None)

    with pytest.raises(TaskApiConflict, match="obsolete"):
        coordinator.cycle(
            workflow_path=WORKFLOW_PATH,
            workflow_nodes=[
                _node("node_001_pick_from_s03", "submit_pick_from_s03")
            ],
        )


def test_paused_workspace_does_not_claim():
    response = _workspace_response(
        paused=True,
        pause_reason={
            "code": "plc_alarm",
            "message": "S08 工站报警",
            "instance_id": "instance-1",
            "node_id": "node_001_pick_from_s03",
            "detail": {"status": 0},
        },
    )
    response["workspace"]["task_instances"][0]["sample_id"] = "sample-1"
    client = FakeTaskClient(response)
    coordinator = _coordinator(client, lambda *_: None)

    result = coordinator.cycle(
        workflow_path=WORKFLOW_PATH,
        workflow_nodes=[_node("node_001_pick_from_s03", "submit_pick_from_s03")],
    )

    assert result["active"] == 0
    assert client.claims == []
    assert result["diagnostics"] == [
        {
            "category": "scheduler_alarm",
            "code": "plc_alarm",
            "message": "S08 工站报警",
            "severity": "warning",
            "immediate": True,
            "phase": "等待派发",
            "instance_id": "instance-1",
            "sample_id": "sample-1",
            "node_id": "node_001_pick_from_s03",
            "device_id": "",
            "action_name": "",
            "detail": {"status": 0},
            "template_id": "template-1",
        }
    ]


def test_disconnected_plc_does_not_block_claim():
    client = FakeTaskClient(_workspace_response())
    coordinator = _coordinator(
        client,
        lambda *_: None,
        devices=_devices(FakePLC(connected=False)),
    )

    result = coordinator.cycle(
        workflow_path=WORKFLOW_PATH,
        workflow_nodes=[_node("node_001_pick_from_s03", "submit_pick_from_s03")],
    )

    assert result["claimed"] == 1
    coordinator.shutdown()


def test_disconnected_action_device_does_not_block_claim():
    class DisconnectedDevice:
        client = None

        def dose_powder(self):
            return {"success": True}

    client = FakeTaskClient(
        _workspace_response(node_ids=["node_003_dose_powder"])
    )
    devices = _devices()
    devices["device"] = DisconnectedDevice()
    coordinator = _coordinator(client, lambda *_: None, devices=devices)

    result = coordinator.cycle(
        workflow_path=WORKFLOW_PATH,
        workflow_nodes=[_node("node_003_dose_powder", "dose_powder")],
    )

    assert result["claimed"] == 1
    coordinator.shutdown()


def test_gateway_backed_action_device_with_no_private_client_can_claim():
    class GatewayBackedDevice:
        _client = None
        _plc_gateway = FakePLC()

        def run_solvent_addition(self):
            return {"success": True}

    client = FakeTaskClient(
        _workspace_response(node_ids=["node_006_run_solvent_addition"])
    )
    devices = _devices()
    devices["device"] = GatewayBackedDevice()
    coordinator = _coordinator(client, lambda *_: [{"success": True}], devices=devices)

    result = coordinator.cycle(
        workflow_path=WORKFLOW_PATH,
        workflow_nodes=[
            _node("node_006_run_solvent_addition", "run_solvent_addition")
        ],
    )

    assert result["claimed"] == 1
    coordinator.shutdown()


def test_restart_fails_orphaned_server_execution_without_repeating_action():
    instance = _workspace_response()["workspace"]["task_instances"][0]
    execution_id = deterministic_execution_id(
        instance["id"], 0, "node_001_pick_from_s03"
    )
    instance["execution_state"] = {
        "cursor": 0,
        "records": [
            {
                "node_id": "node_001_pick_from_s03",
                "execution_id": execution_id,
                "status": "running",
                "resources": ["robot"],
            }
        ],
        "active_execution_id": execution_id,
        "active_node_id": "node_001_pick_from_s03",
    }
    client = FakeTaskClient(_workspace_response(instances=[instance]))
    calls = []
    coordinator = _coordinator(client, lambda *_: calls.append(1))

    result = coordinator.cycle(
        workflow_path=WORKFLOW_PATH,
        workflow_nodes=[_node("node_001_pick_from_s03", "submit_pick_from_s03")],
    )

    assert result["claimed"] == 0
    assert client.claims == []
    assert calls == []
    assert result["failed"] == 1
    assert [item["code"] for item in result["diagnostics"]] == [
        "orphaned_execution"
    ]
    assert result["diagnostics"][0]["execution_id"] == execution_id
    assert result["diagnostics"][0]["phase"] == "recovering"
    assert client.failed == [{
        "workflow_path": WORKFLOW_PATH,
        "expected_version": 7,
        "instance_id": "instance-1",
        "node_id": "node_001_pick_from_s03",
        "execution_id": execution_id,
        "error": {
            "code": "orphaned_execution",
            "message": "服务端存在活动执行但本地无对应 future，无法安全恢复",
        },
    }]


def test_harvest_only_recovers_orphaned_execution_after_backend_restart():
    instance = _workspace_response()["workspace"]["task_instances"][0]
    execution_id = deterministic_execution_id(
        instance["id"], 0, "node_001_pick_from_s03"
    )
    instance["execution_state"] = {
        "cursor": 0,
        "records": [{
            "node_id": "node_001_pick_from_s03",
            "execution_id": execution_id,
            "status": "running",
            "resources": ["robot"],
        }],
        "active_execution_id": execution_id,
        "active_node_id": "node_001_pick_from_s03",
    }
    client = FakeTaskClient(_workspace_response(instances=[instance]))
    calls = []
    coordinator = _coordinator(client, lambda *_: calls.append(1))

    result = coordinator.cycle(
        workflow_path=WORKFLOW_PATH,
        workflow_nodes=[],
        harvest_only=True,
    )

    assert result["claimed"] == 0
    assert result["failed"] == 1
    assert calls == []
    assert result["diagnostics"][0]["code"] == "orphaned_execution"
    assert client.failed[0]["execution_id"] == execution_id


def test_orphaned_execution_conflict_waits_then_retries_next_tick():
    instance = _workspace_response()["workspace"]["task_instances"][0]
    execution_id = deterministic_execution_id(
        instance["id"], 0, "node_001_pick_from_s03"
    )
    instance["execution_state"] = {
        "cursor": 0,
        "records": [{
            "node_id": "node_001_pick_from_s03",
            "execution_id": execution_id,
            "status": "running",
            "resources": ["robot"],
        }],
        "active_execution_id": execution_id,
        "active_node_id": "node_001_pick_from_s03",
    }

    class ConflictOnceClient(FakeTaskClient):
        def __init__(self, response):
            super().__init__(response)
            self.attempts = 0

        def fail_action(self, **payload):
            self.attempts += 1
            if self.attempts == 1:
                raise TaskApiConflict("version_conflict", "stale")
            return super().fail_action(**payload)

    client = ConflictOnceClient(_workspace_response(instances=[instance]))
    calls = []
    coordinator = _coordinator(client, lambda *_: calls.append(1))
    nodes = [_node("node_001_pick_from_s03", "submit_pick_from_s03")]

    first = coordinator.cycle(
        workflow_path=WORKFLOW_PATH,
        workflow_nodes=nodes,
    )
    second = coordinator.cycle(
        workflow_path=WORKFLOW_PATH,
        workflow_nodes=nodes,
    )

    assert first["failed"] == 0
    assert second["failed"] == 1
    assert client.attempts == 2
    assert calls == []


@pytest.mark.parametrize(
    ("home", "write_allowed"),
    [(True, True), (False, True), (True, False)],
)
def test_action_completion_does_not_read_plc_readiness(home, write_allowed):
    client = FakeTaskClient(_workspace_response())
    plc = FakePLC(home=home, write_allowed=write_allowed)
    coordinator = _coordinator(
        client,
        lambda *_: [{"success": True}],
        devices=_devices(plc),
    )
    nodes = [_node("node_001_pick_from_s03", "submit_pick_from_s03")]

    coordinator.cycle(workflow_path=WORKFLOW_PATH, workflow_nodes=nodes)
    result = _harvest(coordinator, nodes)

    assert plc.reads == []
    assert len(client.succeeded) == 1
    assert client.failed == []
    assert result["completed"] == 1


def test_other_in_flight_action_does_not_block_completion():
    instances = [
        _workspace_response()["workspace"]["task_instances"][0],
        {
            **deepcopy(_workspace_response()["workspace"]["task_instances"][0]),
            "id": "instance-2",
            "template_id": "template-2",
        },
    ]
    response = _workspace_response(instances=instances)
    response["workspace"]["templates"].append(
        {"id": "template-2", "node_ids": ["node_004_pick_from_s072"]}
    )
    client = FakeTaskClient(response)
    second_started = threading.Event()
    release_second = threading.Event()

    def runner(node, _devices, _action_callable):
        if node.uuid == "node_004_pick_from_s072":
            second_started.set()
            release_second.wait(timeout=2)
        return [{"success": True}]

    coordinator = _coordinator(client, runner)
    nodes = [
        _node(
            "node_001_pick_from_s03",
            "submit_pick_from_s03",
            device_name="robot-a",
        ),
        _node(
            "node_004_pick_from_s072",
            "submit_pick_from_s072",
            device_name="robot-b",
        ),
    ]

    coordinator.cycle(workflow_path=WORKFLOW_PATH, workflow_nodes=nodes)
    assert second_started.wait(timeout=1)
    result = _harvest(coordinator, nodes)

    assert result["completed"] == 1
    assert client.failed == []
    release_second.set()
    coordinator.shutdown()


def test_missing_workflow_node_is_claimed_only_to_fail_without_execution():
    client = FakeTaskClient(_workspace_response(node_ids=["unknown-node"]))
    calls = []
    coordinator = _coordinator(client, lambda *_: calls.append(1))

    result = coordinator.cycle(
        workflow_path=WORKFLOW_PATH,
        workflow_nodes=[],
    )

    assert result["claimed"] == 1
    assert result["failed"] == 1
    assert client.claims[0]["resources"] == []
    assert client.failed[0]["error"]["code"] == "unsupported_action"
    assert calls == []


def test_missing_workflow_node_fail_conflict_retries_same_report_next_tick():
    class UnknownConflictClient(FakeTaskClient):
        def __init__(self, response):
            super().__init__(response)
            self.fail_attempts = 0

        def claim_action(self, **payload):
            super().claim_action(**payload)
            state = self.response["workspace"]["task_instances"][0][
                "execution_state"
            ]
            state["active_execution_id"] = payload["execution_id"]
            state["active_node_id"] = payload["node_id"]
            state["records"] = [{
                "node_id": payload["node_id"],
                "execution_id": payload["execution_id"],
                "status": "running",
                "resources": [],
            }]
            return deepcopy(self.response)

        def fail_action(self, **payload):
            self.fail_attempts += 1
            if self.fail_attempts == 1:
                raise TaskApiConflict("version_conflict", "stale")
            return super().fail_action(**payload)

    client = UnknownConflictClient(
        _workspace_response(node_ids=["unknown-node"])
    )
    calls = []
    coordinator = _coordinator(client, lambda *_: calls.append(1))
    nodes = []

    first = coordinator.cycle(
        workflow_path=WORKFLOW_PATH,
        workflow_nodes=nodes,
    )
    second = coordinator.cycle(
        workflow_path=WORKFLOW_PATH,
        workflow_nodes=nodes,
    )

    assert first["claimed"] == 1
    assert first["failed"] == 0
    assert second["claimed"] == 0
    assert second["failed"] == 1
    assert client.failed[0]["error"]["code"] == "unsupported_action"
    assert calls == []


def test_unsupported_action_transport_failure_retries_same_terminal_report():
    class RetryTwiceClient(FakeTaskClient):
        def __init__(self, response):
            super().__init__(response)
            self.fail_attempts = []

        def fail_action(self, **payload):
            self.fail_attempts.append(deepcopy(payload))
            if len(self.fail_attempts) <= 2:
                raise TimeoutError("response unavailable")
            return super().fail_action(**payload)

    client = RetryTwiceClient(
        _workspace_response(node_ids=["unknown-node"])
    )
    action_calls = []
    coordinator = _coordinator(client, lambda *_: action_calls.append(1))

    first = coordinator.cycle(
        workflow_path=WORKFLOW_PATH,
        workflow_nodes=[],
    )
    retry = coordinator.cycle(
        workflow_path=WORKFLOW_PATH,
        workflow_nodes=[],
        harvest_only=True,
    )
    recovered = coordinator.cycle(
        workflow_path=WORKFLOW_PATH,
        workflow_nodes=[],
        harvest_only=True,
    )

    for pending in (first, retry):
        assert pending["success"] is True
        assert pending["active"] == 1
        assert pending["in_flight"] == 1
        assert pending["failed"] == 0
    assert recovered["in_flight"] == 0
    assert recovered["failed"] == 1
    assert len(client.claims) == 1
    assert len(client.fail_attempts) == 3
    assert all(
        attempt["error"]["code"] == "unsupported_action"
        for attempt in client.fail_attempts
    )
    assert action_calls == []


def test_orphan_terminal_report_conflict_is_retried_by_harvest_only():
    instance = _workspace_response()["workspace"]["task_instances"][0]
    execution_id = deterministic_execution_id(
        instance["id"], 0, "node_001_pick_from_s03"
    )
    instance["execution_state"] = {
        "cursor": 0,
        "records": [{
            "node_id": "node_001_pick_from_s03",
            "execution_id": execution_id,
            "status": "running",
            "resources": [],
        }],
        "active_execution_id": execution_id,
        "active_node_id": "node_001_pick_from_s03",
    }

    class ConflictOnceClient(FakeTaskClient):
        def __init__(self, response):
            super().__init__(response)
            self.fail_attempts = []

        def fail_action(self, **payload):
            self.fail_attempts.append(deepcopy(payload))
            if len(self.fail_attempts) == 1:
                raise TaskApiConflict("version_conflict", "stale")
            return super().fail_action(**payload)

    client = ConflictOnceClient(_workspace_response(instances=[instance]))
    action_calls = []
    coordinator = _coordinator(client, lambda *_: action_calls.append(1))

    first = coordinator.cycle(
        workflow_path=WORKFLOW_PATH,
        workflow_nodes=[],
    )
    recovered = coordinator.cycle(
        workflow_path=WORKFLOW_PATH,
        workflow_nodes=[],
        harvest_only=True,
    )

    assert first["success"] is True
    assert first["active"] == 1
    assert first["in_flight"] == 1
    assert first["failed"] == 0
    assert recovered["in_flight"] == 0
    assert recovered["failed"] == 1
    assert len(client.fail_attempts) == 2
    assert all(
        attempt["error"]["code"] == "orphaned_execution"
        for attempt in client.fail_attempts
    )
    assert action_calls == []


def test_terminal_report_response_loss_retries_idempotently():
    class ResponseLostClient(FakeTaskClient):
        def __init__(self, response):
            super().__init__(response)
            self.fail_attempts = []
            self.server_writes = []

        def fail_action(self, **payload):
            self.fail_attempts.append(deepcopy(payload))
            if not self.server_writes:
                self.server_writes.append(deepcopy(payload))
                self.response["version"] += 1
                raise TimeoutError("response lost after commit")
            assert payload["execution_id"] == self.server_writes[0]["execution_id"]
            assert payload["error"] == self.server_writes[0]["error"]
            return deepcopy(self.response)

    client = ResponseLostClient(
        _workspace_response(node_ids=["unknown-node"])
    )
    action_calls = []
    coordinator = _coordinator(client, lambda *_: action_calls.append(1))

    first = coordinator.cycle(
        workflow_path=WORKFLOW_PATH,
        workflow_nodes=[],
    )
    recovered = coordinator.cycle(
        workflow_path=WORKFLOW_PATH,
        workflow_nodes=[],
        harvest_only=True,
    )

    assert first["in_flight"] == 1
    assert [item["code"] for item in first["diagnostics"]] == [
        "unsupported_action",
        "terminal_report_failed",
    ]
    assert recovered["in_flight"] == 0
    assert recovered["failed"] == 1
    assert len(client.fail_attempts) == 2
    assert len(client.server_writes) == 1
    assert len(client.claims) == 1
    assert action_calls == []


def test_non_retryable_terminal_conflict_is_retained_and_marks_failure():
    class ReplayConflictClient(FakeTaskClient):
        def __init__(self, response):
            super().__init__(response)
            self.fail_attempts = 0

        def fail_action(self, **_payload):
            self.fail_attempts += 1
            raise TaskApiConflict(
                "action_replay_conflict",
                "different terminal semantics",
            )

    client = ReplayConflictClient(
        _workspace_response(node_ids=["unknown-node"])
    )
    coordinator = _coordinator(client, lambda *_: None)

    first = coordinator.cycle(
        workflow_path=WORKFLOW_PATH,
        workflow_nodes=[],
    )
    harvest = coordinator.cycle(
        workflow_path=WORKFLOW_PATH,
        workflow_nodes=[],
        harvest_only=True,
    )

    for result in (first, harvest):
        assert result["success"] is False
        assert result["active"] == 1
        assert result["in_flight"] == 1
        assert result["failed"] == 0
    assert [item["code"] for item in first["diagnostics"]] == [
        "unsupported_action",
        "terminal_report_failed",
    ]
    assert first["diagnostics"][1]["detail"]["retryable"] is False
    assert client.fail_attempts == 1
    assert len(coordinator._pending_terminal_reports) == 1


def test_workflow_payload_parser_uses_data_wrapper_without_temp_files(monkeypatch):
    def reject_temp_file(*_args, **_kwargs):
        raise AssertionError("workflow payload 解析不得创建临时文件")

    monkeypatch.setattr(tempfile, "NamedTemporaryFile", reject_temp_file)
    nodes = workflow_nodes_from_payload({
        "data": {
            "nodes": [{
                "uuid": "node-1",
                "name": "auto-dose_powder",
                "device_name": "device",
                "param": {"amount": 1},
            }]
        }
    })

    assert nodes == [
        WorkflowNode(
            uuid="node-1",
            name="auto-dose_powder",
            device_name="device",
            param={"amount": 1},
        )
    ]


def test_workflow_payload_parser_prefers_explicit_action_contract():
    nodes = workflow_nodes_from_payload({
        "nodes": [{
            "workflow_node_id": "custom-node",
            "device_id": "custom-device",
            "method": "execute_custom",
            "params": {"amount": 2},
            "opc_variables": ["ready", "done"],
            "disabled": False,
        }]
    })

    assert nodes == [
        WorkflowNode(
            uuid="custom-node",
            name="execute_custom",
            device_name="custom-device",
            param={"amount": 2},
            method="execute_custom",
            legacy_route_compatible=False,
        )
    ]
    assert run_workflow_local.node_method(nodes[0]) == "execute_custom"


@pytest.mark.parametrize(
    "opc_variables",
    [
        "ready",
        [""],
        ["ready", 1],
    ],
)
def test_workflow_payload_parser_rejects_invalid_explicit_opc_metadata(
    opc_variables,
):
    with pytest.raises(ValueError, match="opc_variables"):
        workflow_nodes_from_payload({
            "nodes": [{
                "workflow_node_id": "custom-node",
                "device_id": "custom-device",
                "method": "execute_custom",
                "params": {},
                "opc_variables": opc_variables,
            }]
        })


def test_workflow_payload_parser_rejects_partial_explicit_contract():
    with pytest.raises(
        ValueError,
        match=(
            r"^workflow node explicit 缺少字段: "
            r"device_id, method, params$"
        ),
    ):
        workflow_nodes_from_payload({
            "nodes": [{"workflow_node_id": "custom-node"}]
        })


def test_workflow_payload_parser_rejects_partial_legacy_contract():
    with pytest.raises(
        ValueError,
        match=r"^workflow node legacy 缺少字段: param$",
    ):
        workflow_nodes_from_payload({
            "nodes": [{
                "uuid": "legacy-node",
                "name": "auto-execute_custom",
                "device_name": "custom-device",
            }]
        })


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (
            {
                "workflow_node_id": "custom-node",
                "device_id": "custom-device",
                "method": "execute_custom",
                "params": {},
                "uuid": "legacy-node",
            },
            "workflow node 字段格式冲突: explicit 不允许 legacy 字段 uuid",
        ),
        (
            {
                "uuid": "legacy-node",
                "name": "auto-execute_custom",
                "device_name": "custom-device",
                "param": {},
                "action_method": "execute_custom",
            },
            "workflow node 不支持字段: action_method",
        ),
        (
            {
                "uuid": "legacy-node",
                "name": "auto-execute_custom",
                "resource_name": "custom-device",
                "param": {},
            },
            "workflow node 不支持字段: resource_name",
        ),
    ],
)
def test_workflow_payload_parser_rejects_mixed_or_alias_fields(payload, message):
    with pytest.raises(ValueError, match=f"^{message}$"):
        workflow_nodes_from_payload({"nodes": [payload]})


@pytest.mark.parametrize(
    "field,value",
    [
        ("workflow_node_id", {"bad": "id"}),
        ("workflow_node_id", " "),
        ("device_id", ["bad-device"]),
        ("device_id", ""),
        ("method", {"bad": "method"}),
        ("method", " "),
        ("params", ["bad-params"]),
    ],
)
def test_workflow_payload_parser_rejects_malicious_explicit_fields(field, value):
    item = {
        "workflow_node_id": "custom-node",
        "device_id": "custom-device",
        "method": "execute_custom",
        "params": {},
    }
    item[field] = value

    with pytest.raises(ValueError, match=field):
        workflow_nodes_from_payload({"nodes": [item]})


def test_explicit_node_never_uses_legacy_route_alias():
    runtime_config = RuntimeConfig(
        path=Path("runtime.json"),
        device_factory=RuntimeDeviceFactoryConfig(
            target_device_id="routed-device",
            route_aliases={"explicit-device"},
        ),
        opc_snapshot=RuntimeOpcSnapshotConfig(),
    )
    node = WorkflowNode(
        uuid="custom-node",
        name="misleading-name",
        device_name="explicit-device",
        param={},
        method="execute_custom",
        legacy_route_compatible=False,
    )

    assert route_node_device(node, runtime_config) == "explicit-device"
    assert run_workflow_local.node_method(node) == "execute_custom"


def test_workflow_payload_parser_rejects_invalid_node_without_temp_files(
    monkeypatch,
):
    def reject_temp_file(*_args, **_kwargs):
        raise AssertionError("异常解析路径不得创建临时文件")

    monkeypatch.setattr(tempfile, "NamedTemporaryFile", reject_temp_file)

    with pytest.raises(
        ValueError,
        match=r"^workflow node legacy 缺少字段: param, uuid$",
    ):
        workflow_nodes_from_payload({
            "nodes": [{"name": "auto-dose_powder", "device_name": "device"}]
        })


def test_shutdown_waits_for_running_action_then_harvests_success():
    client = FakeTaskClient(
        _workspace_response(node_ids=["node_003_dose_powder"])
    )
    started = threading.Event()
    release = threading.Event()
    shutdown_done = threading.Event()
    shutdown_results = []

    def runner(_node, _devices, _action_callable):
        started.set()
        release.wait(timeout=2)
        return [{"success": True}]

    coordinator = _coordinator(client, runner)
    nodes = [_node("node_003_dose_powder", "dose_powder")]
    coordinator.cycle(workflow_path=WORKFLOW_PATH, workflow_nodes=nodes)
    assert started.wait(timeout=1)

    def close():
        shutdown_results.append(coordinator.shutdown())
        shutdown_done.set()

    close_thread = threading.Thread(target=close)
    close_thread.start()
    assert not shutdown_done.wait(timeout=0.03)
    assert client.succeeded == []

    release.set()
    close_thread.join(timeout=1)

    assert shutdown_done.is_set()
    assert shutdown_results == [{
        "success": True,
        "in_flight": 0,
        "completed": 1,
        "failed": 0,
    }]
    assert len(client.succeeded) == 1
    assert client.failed == []


def test_shutdown_prevents_new_claims():
    client = FakeTaskClient(_workspace_response())
    calls = []
    coordinator = _coordinator(client, lambda *_: calls.append(1))

    coordinator.shutdown()
    result = coordinator.cycle(
        workflow_path=WORKFLOW_PATH,
        workflow_nodes=[_node(
            "node_001_pick_from_s03",
            "submit_pick_from_s03",
        )],
    )

    assert result["success"] is False
    assert result["claimed"] == 0
    assert client.claims == []
    assert calls == []


def test_shutdown_reports_pending_terminal_upload_without_failing_action():
    class OfflineTaskClient(FakeTaskClient):
        def succeed_action(self, **_payload):
            raise ConnectionError("scheduler offline")

    client = OfflineTaskClient(
        _workspace_response(node_ids=["node_003_dose_powder"])
    )
    calls = []
    coordinator = _coordinator(
        client,
        lambda *_: calls.append(1) or [{"success": True}],
    )
    coordinator.cycle(
        workflow_path=WORKFLOW_PATH,
        workflow_nodes=[_node("node_003_dose_powder", "dose_powder")],
    )

    result = coordinator.shutdown()

    assert result["success"] is False
    assert result["in_flight"] == 1
    assert "终态上报暂未完成" in result["message"]
    assert calls == [1]
    assert client.failed == []


def test_concurrent_cycle_and_shutdown_preserve_in_flight_mapping():
    class BlockingClaimClient(FakeTaskClient):
        def __init__(self, response):
            super().__init__(response)
            self.claim_started = threading.Event()
            self.release_claim = threading.Event()

        def claim_action(self, **payload):
            self.claim_started.set()
            self.release_claim.wait(timeout=2)
            return super().claim_action(**payload)

    client = BlockingClaimClient(
        _workspace_response(node_ids=["node_003_dose_powder"])
    )
    runner_started = threading.Event()
    release_runner = threading.Event()
    errors = []
    shutdown_results = []
    coordinator = _coordinator(
        client,
        lambda *_: (
            runner_started.set(),
            release_runner.wait(timeout=2),
            [{"success": True}],
        )[-1],
    )
    nodes = [_node("node_003_dose_powder", "dose_powder")]

    cycle_thread = threading.Thread(
        target=lambda: _capture_error(
            errors,
            coordinator.cycle,
            workflow_path=WORKFLOW_PATH,
            workflow_nodes=nodes,
        )
    )
    cycle_thread.start()
    assert client.claim_started.wait(timeout=1)
    close_thread = threading.Thread(
        target=lambda: shutdown_results.append(coordinator.shutdown())
    )
    close_thread.start()
    client.release_claim.set()
    assert runner_started.wait(timeout=1)
    release_runner.set()
    cycle_thread.join(timeout=1)
    close_thread.join(timeout=1)

    assert errors == []
    assert shutdown_results[0]["in_flight"] == 0
    assert shutdown_results[0]["completed"] == 1
    assert len(client.claims) == 1
    assert len(client.succeeded) == 1


def _harvest(
    coordinator: TaskExecutionCoordinator,
    nodes: list[WorkflowNode],
) -> dict:
    deadline = time.monotonic() + 1
    while time.monotonic() < deadline:
        result = coordinator.cycle(
            workflow_path=WORKFLOW_PATH,
            workflow_nodes=nodes,
        )
        if result["completed"] or result["failed"]:
            return result
        time.sleep(0.005)
    raise AssertionError("future 未在超时前完成")


def _wait_for(
    coordinator: TaskExecutionCoordinator,
    nodes: list[WorkflowNode],
    predicate,
) -> dict:
    deadline = time.monotonic() + 1
    while time.monotonic() < deadline:
        result = coordinator.cycle(
            workflow_path=WORKFLOW_PATH,
            workflow_nodes=nodes,
        )
        if predicate():
            return result
        time.sleep(0.005)
    raise AssertionError("条件未在超时前满足")


def _capture_error(errors, function, **kwargs):
    try:
        function(**kwargs)
    except Exception as exc:
        errors.append(exc)


def test_custom_node_claims_without_resources_and_runs_with_disconnected_client():
    class ActionDevice:
        client = None

        def execute_custom(self):
            return {"success": True}

    client = FakeTaskClient(_workspace_response(node_ids=["custom-node"]))
    calls = []
    node = WorkflowNode(
        uuid="custom-node",
        name="ignored-name",
        device_name="custom-device",
        param={},
        method="execute_custom",
        legacy_route_compatible=False,
    )
    coordinator = TaskExecutionCoordinator(
        task_client=client,
        node_runner=lambda current, devices, action_callable: calls.append(
            (current, devices)
        )
        or [action_callable(**current.param)],
        device_provider=lambda: {"custom-device": ActionDevice()},
        max_workers=1,
    )

    result = coordinator.cycle(
        workflow_path=WORKFLOW_PATH,
        workflow_nodes=[node],
    )
    harvested = _harvest(coordinator, [node])

    assert result["claimed"] == 1
    assert client.claims[0]["resources"] == []
    assert calls and calls[0][0] == node
    assert harvested["completed"] == 1
    assert client.succeeded[0]["release_resources"] == []


def test_built_graph_payload_runs_custom_action_through_coordinator_tick():
    class ActionDevice:
        def execute_custom(self, amount):
            return {"success": True, "amount": amount}

    preset = replace(
        DEFAULT_PRESET,
        target_device_id="custom-device",
        target_device_ids=["custom-device"],
        actions={
            "execute_custom": ActionSpec(
                method="execute_custom",
                label="Custom",
                description="",
                params=[{"name": "amount", "type": "integer", "default": 1}],
                device_id="custom-device",
            )
        },
    )
    payload = build_graph_workflow(
        flow_nodes=[{
            "id": "custom-node",
            "data": {
                "device_id": "custom-device",
                "method": "execute_custom",
                "params": {"amount": 2},
                "opc_variables": ["ready"],
            },
        }],
        flow_edges=[],
        preset=preset,
    )
    nodes = workflow_nodes_from_payload(payload)
    client = FakeTaskClient(_workspace_response(node_ids=["custom-node"]))
    coordinator = TaskExecutionCoordinator(
        task_client=client,
        node_runner=lambda node, _devices, action_callable: [
            action_callable(**node.param)
        ],
        device_provider=lambda: {"custom-device": ActionDevice()},
        max_workers=1,
    )

    result = coordinator.cycle(
        workflow_path=WORKFLOW_PATH,
        workflow_nodes=nodes,
    )
    harvested = _harvest(coordinator, nodes)

    assert result["claimed"] == 1
    assert harvested["completed"] == 1
    assert client.failed == []
    assert client.succeeded[0]["result"] == [
        {"success": True, "amount": 2}
    ]


def test_action_descriptor_is_bound_once_before_claim_and_reused_by_runner():
    class CountingDescriptor:
        def __init__(self):
            self.binds = 0
            self.calls = 0

        def __get__(self, instance, _owner):
            if instance is None:
                return self
            self.binds += 1

            def bound(**_params):
                self.calls += 1
                return {"success": True}

            return bound

    descriptor = CountingDescriptor()

    class ActionDevice:
        execute_custom = descriptor

    client = FakeTaskClient(_workspace_response(node_ids=["custom-node"]))
    coordinator = TaskExecutionCoordinator(
        task_client=client,
        node_runner=lambda node, _devices, action_callable: [
            action_callable(**node.param)
        ],
        device_provider=lambda: {"custom-device": ActionDevice()},
        max_workers=1,
    )
    node = WorkflowNode(
        uuid="custom-node",
        name="ignored-name",
        device_name="custom-device",
        param={"amount": 2},
        method="execute_custom",
        legacy_route_compatible=False,
    )

    coordinator.cycle(
        workflow_path=WORKFLOW_PATH,
        workflow_nodes=[node],
    )
    result = _harvest(coordinator, [node])

    assert result["completed"] == 1
    assert descriptor.binds == 1
    assert descriptor.calls == 1


def test_missing_action_method_is_unsupported_without_entering_future():
    client = FakeTaskClient(_workspace_response(node_ids=["custom-node"]))
    calls = []
    coordinator = TaskExecutionCoordinator(
        task_client=client,
        node_runner=lambda *_: calls.append(1),
        device_provider=lambda: {"custom-device": object()},
        max_workers=1,
    )
    node = WorkflowNode(
        uuid="custom-node",
        name="ignored-name",
        device_name="custom-device",
        param={},
        method="missing_method",
        legacy_route_compatible=False,
    )

    result = coordinator.cycle(
        workflow_path=WORKFLOW_PATH,
        workflow_nodes=[node],
    )

    assert result["claimed"] == 1
    assert result["failed"] == 1
    assert client.failed[0]["error"]["code"] == "unsupported_action"
    assert calls == []
    assert coordinator._in_flight == {}


def test_same_device_instances_claim_only_one_action_per_cycle():
    class SharedDevice:
        def custom_action_a(self):
            return {"success": True}

        def custom_action_b(self):
            return {"success": True}

    instances = [
        deepcopy(_workspace_response()["workspace"]["task_instances"][0]),
        {
            **deepcopy(_workspace_response()["workspace"]["task_instances"][0]),
            "id": "instance-2",
            "template_id": "template-2",
        },
    ]
    response = _workspace_response(
        instances=instances,
        node_ids=["custom-node-a"],
    )
    response["workspace"]["templates"].append(
        {"id": "template-2", "node_ids": ["custom-node-b"]}
    )
    client = FakeTaskClient(response)
    release = threading.Event()
    started = []

    def runner(node, _devices, _action_callable):
        started.append(node.uuid)
        release.wait(timeout=2)
        return [{"success": True}]

    coordinator = TaskExecutionCoordinator(
        task_client=client,
        node_runner=runner,
        device_provider=lambda: {"shared-device": SharedDevice()},
        max_workers=2,
    )
    nodes = [
        WorkflowNode(
            uuid="custom-node-a",
            name="ignored-a",
            device_name="shared-device",
            param={},
            method="custom_action_a",
            legacy_route_compatible=False,
        ),
        WorkflowNode(
            uuid="custom-node-b",
            name="ignored-b",
            device_name="shared-device",
            param={},
            method="custom_action_b",
            legacy_route_compatible=False,
        ),
    ]

    result = coordinator.cycle(
        workflow_path=WORKFLOW_PATH,
        workflow_nodes=nodes,
    )
    deadline = time.monotonic() + 1
    while len(started) < 1 and time.monotonic() < deadline:
        time.sleep(0.005)

    assert result["claimed"] == 1
    assert len(client.claims) == 1
    assert all(claim["resources"] == [] for claim in client.claims)
    assert len(started) == 1
    assert started[0] in {"custom-node-a", "custom-node-b"}
    release.set()
    coordinator.shutdown()


def test_started_atomic_task_keeps_device_until_its_next_action_is_claimed():
    class SharedDevice:
        def first_action(self):
            return {"success": True}

        def place_action(self):
            return {"success": True}

        def competing_action(self):
            return {"success": True}

    competing = {
        **deepcopy(_workspace_response()["workspace"]["task_instances"][0]),
        "id": "instance-competing",
        "template_id": "template-competing",
        "started_at": 200,
    }
    owner = {
        **deepcopy(_workspace_response()["workspace"]["task_instances"][0]),
        "id": "instance-owner",
        "template_id": "template-owner",
        "started_at": 100,
        "execution_state": {
            "cursor": 1,
            "records": [
                {
                    "node_id": "owner-pick",
                    "status": "succeeded",
                    "started_at": 100,
                    "finished_at": 150,
                }
            ],
            "active_execution_id": None,
            "active_node_id": None,
        },
    }
    response = _workspace_response(
        instances=[competing, owner],
        node_ids=["competing-pick"],
    )
    response["workspace"]["templates"] = [
        {
            "id": "template-competing",
            "node_ids": ["competing-pick"],
        },
        {
            "id": "template-owner",
            "node_ids": ["owner-pick", "owner-place"],
        },
    ]
    client = FakeTaskClient(response)
    release = threading.Event()

    def runner(_node, _devices, _action_callable):
        release.wait(timeout=2)
        return [{"success": True}]

    coordinator = TaskExecutionCoordinator(
        task_client=client,
        node_runner=runner,
        device_provider=lambda: {"shared-device": SharedDevice()},
        max_workers=2,
    )
    nodes = [
        WorkflowNode(
            uuid="competing-pick",
            name="competing-pick",
            device_name="shared-device",
            param={},
            method="competing_action",
            legacy_route_compatible=False,
        ),
        WorkflowNode(
            uuid="owner-pick",
            name="owner-pick",
            device_name="shared-device",
            param={},
            method="first_action",
            legacy_route_compatible=False,
        ),
        WorkflowNode(
            uuid="owner-place",
            name="owner-place",
            device_name="shared-device",
            param={},
            method="place_action",
            legacy_route_compatible=False,
        ),
    ]

    result = coordinator.cycle(
        workflow_path=WORKFLOW_PATH,
        workflow_nodes=nodes,
    )

    assert result["claimed"] == 1
    assert client.claims[0]["instance_id"] == "instance-owner"
    assert client.claims[0]["node_id"] == "owner-place"
    release.set()
    coordinator.shutdown()


def test_same_device_action_can_be_claimed_after_prior_action_finishes():
    class SharedDevice:
        def custom_action_a(self):
            return {"success": True}

        def custom_action_b(self):
            return {"success": True}

    instances = [
        deepcopy(_workspace_response()["workspace"]["task_instances"][0]),
        {
            **deepcopy(_workspace_response()["workspace"]["task_instances"][0]),
            "id": "instance-2",
            "template_id": "template-2",
        },
    ]
    response = _workspace_response(
        instances=instances,
        node_ids=["custom-node-a"],
    )
    response["workspace"]["templates"].append(
        {"id": "template-2", "node_ids": ["custom-node-b"]}
    )
    client = FakeTaskClient(response)
    release = threading.Event()

    def runner(_node, _devices, _action_callable):
        release.wait(timeout=2)
        return [{"success": True}]

    coordinator = TaskExecutionCoordinator(
        task_client=client,
        node_runner=runner,
        device_provider=lambda: {"shared-device": SharedDevice()},
        max_workers=2,
    )
    nodes = [
        WorkflowNode(
            uuid="custom-node-a",
            name="ignored-a",
            device_name="shared-device",
            param={},
            method="custom_action_a",
            legacy_route_compatible=False,
        ),
        WorkflowNode(
            uuid="custom-node-b",
            name="ignored-b",
            device_name="shared-device",
            param={},
            method="custom_action_b",
            legacy_route_compatible=False,
        ),
    ]

    first = coordinator.cycle(
        workflow_path=WORKFLOW_PATH,
        workflow_nodes=nodes,
    )

    assert first["claimed"] == 1
    release.set()
    second = _harvest(coordinator, nodes)

    assert second["claimed"] == 1
    assert [claim["instance_id"] for claim in client.claims] == [
        "instance-1",
        "instance-2",
    ]
    coordinator.shutdown()


@pytest.mark.parametrize(
    "devices",
    [{}, {"missing-device": None}],
)
def test_missing_device_is_claimed_then_failed_as_unsupported_action(devices):
    client = FakeTaskClient(_workspace_response(node_ids=["custom-node"]))
    calls = []
    coordinator = TaskExecutionCoordinator(
        task_client=client,
        node_runner=lambda *_: calls.append(1),
        device_provider=lambda: devices,
        max_workers=1,
    )

    result = coordinator.cycle(
        workflow_path=WORKFLOW_PATH,
        workflow_nodes=[_node("custom-node", "custom-action", "missing-device")],
    )

    assert result["claimed"] == 1
    assert result["failed"] == 1
    assert client.claims[0]["resources"] == []
    assert client.failed[0]["error"]["code"] == "unsupported_action"
    assert calls == []


@pytest.mark.parametrize(
    "payload",
    [
        {"nodes": [{"uuid": {}, "name": "auto-action", "device_name": "device"}]},
        {"nodes": [{"uuid": "node", "name": [], "device_name": "device"}]},
        {"nodes": [{"uuid": "node", "name": "auto-action", "param": []}]},
    ],
)
def test_workflow_payload_parser_rejects_wrong_field_types(payload):
    with pytest.raises(ValueError):
        workflow_nodes_from_payload(payload)


def test_coordinator_does_not_duplicate_low_level_robot_handshake_policy():
    source = Path(
        "scripts/task_execution_coordinator.py"
    ).read_text(encoding="utf-8").lower()
    forbidden = (
        "robot_home",
        "robot_任务允许写入",
        "node_001",
    )

    assert all(token not in source for token in forbidden)
