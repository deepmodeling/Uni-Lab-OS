from __future__ import annotations

from collections.abc import Mapping

import pytest

from unilabos.devices.virtual.workbench import VirtualWorkbench
from unilabos.registry.decorators import get_action_meta
from unilabos.server.demo.workbench_scenarios import (
    DEFAULT_WORKBENCH_MATERIAL_UUID,
    WORKBENCH_TARGET_DEVICE_ID,
    build_workbench_scenario_graph,
)
from unilabos.server.scheduler.authority import SchedulerAuthorityProfile
from unilabos.server.workflow.builtin_catalog import (
    BUILTIN_CATALOG_AUTHORITY,
    WORKBENCH_DATA_HANDLE_UUIDS,
    WORKBENCH_READY_HANDLE_UUIDS,
    WORKBENCH_RESOURCE_TEMPLATE_UUID,
    WORKBENCH_TEMPLATE_UUIDS,
    builtin_workflow_catalog,
)
from unilabos.server.workflow.service import WorkflowService
from unilabos.server.workflow.store import WorkflowStore


EXPECTED_TEMPLATE_UUIDS = {
    "prepare": "8fa500bc-e589-5e9f-a767-e315fa4304d7",
    "move": "546f2d30-7206-522f-9f0a-3cd8c7d35a25",
    "heat": "41496da1-57a2-5f88-ba1e-19836a31e4ce",
    "output": "346ba0ef-3147-50b4-b1f5-26d6ef89ce3e",
}
EXPECTED_READY_HANDLE_UUIDS = {
    "prepare_source": "5ce706e0-ab51-5cdc-b526-2fe74d1c1c3c",
    "prepare_target": "d080c71b-0c7b-5292-b9f7-533073a028e4",
    "move_source": "399bccb2-c702-53ac-bf31-596684f6bcd5",
    "move_target": "1c697ef4-7351-5ade-bfc5-88657148c286",
    "heat_source": "310c38e0-dd5e-5a4d-8a0c-c6a6efee15a7",
    "heat_target": "e44fd946-6722-51f3-8f97-8e78b576f412",
    "output_source": "858191c9-dece-5cb4-b917-63386b18a86e",
    "output_target": "5d21bfe8-c048-5b1a-982f-4a18df2f38c7",
}
EXPECTED_DATA_HANDLE_UUIDS = {
    "prepare_material_1_source": "cf8cb107-2558-5a8c-a39b-f9bb9d4bcfd1",
    "prepare_material_2_source": "022c8078-c843-55ea-a887-d27c3fd4fd57",
    "prepare_material_3_source": "efd859a7-578b-5e4e-9fa6-c9e46b072ee8",
    "prepare_material_4_source": "b82677c7-236a-5ae0-868a-2d00d0645e1f",
    "prepare_material_5_source": "5470fc5c-4628-5451-b570-1d642baebcff",
    "move_material_target": "bada5a63-7ce4-52bc-b84f-41dceb36e504",
    "move_station_source": "aefba538-639c-505e-a2f4-a95809875683",
    "move_material_source": "45f5eea0-b146-5dd7-a10d-1bcda299ef28",
    "heat_station_target": "3d3f6ecd-5b0f-5501-b813-ca27e7a5b61c",
    "heat_material_target": "f3e2dc6e-219e-587f-80e6-6ca21262cbbf",
    "heat_station_source": "d0480eb0-412b-55de-b343-8220f48435b4",
    "heat_material_source": "5787201c-67d0-518a-bc39-384216e2d6ac",
    "output_station_target": "f3c0f721-3618-5766-ab34-aac5309079d5",
    "output_material_target": "bb56e7fc-71af-55e7-9467-685ba94dd0b6",
}

ACTION_METHODS = {
    "prepare": "prepare_materials",
    "move": "move_to_heating_station",
    "heat": "start_heating",
    "output": "move_to_output",
}
EXPECTED_GOALS = {
    "prepare": {"count": "int"},
    "move": {"material_number": "int"},
    "heat": {"station_id": "int", "material_number": "int"},
    "output": {"station_id": "int", "material_number": "int"},
}


def _workbench_catalog() -> tuple[dict[str, dict], dict[str, dict]]:
    nodes, handles = builtin_workflow_catalog()
    node_by_uuid = {
        item.uuid: item.model_dump(by_alias=True)
        for item in nodes
        if item.resource_template_uuid == WORKBENCH_RESOURCE_TEMPLATE_UUID
    }
    handle_by_uuid = {
        item.uuid: item.model_dump()
        for item in handles
        if item.workflow_node_template_uuid in node_by_uuid
    }
    return node_by_uuid, handle_by_uuid


def _normalized_registry_handles(
    handles: Mapping[str, list[dict]],
    group: str,
) -> set[tuple[str, str, str | None, str | None]]:
    return {
        (
            item["handler_key"],
            item["data_type"],
            item.get("data_key"),
            item.get("data_source"),
        )
        for item in handles.get(group, [])
    }


def _normalized_catalog_handles(
    handles: Mapping[str, dict],
    template_uuid: str,
    io_type: str,
) -> set[tuple[str, str, str | None, str | None]]:
    return {
        (
            item["handle_key"],
            item["type"],
            item.get("data_key"),
            item.get("data_source"),
        )
        for item in handles.values()
        if item["workflow_node_template_uuid"] == template_uuid
        and item["io_type"] == io_type
        and item["handle_key"] != "ready"
    }


def test_workbench_catalog_uses_stable_uuid_contract() -> None:
    assert WORKBENCH_RESOURCE_TEMPLATE_UUID == (
        "2a2231c9-be3f-5d18-82d1-1d0bac09bda8"
    )
    assert dict(WORKBENCH_TEMPLATE_UUIDS) == EXPECTED_TEMPLATE_UUIDS
    assert dict(WORKBENCH_READY_HANDLE_UUIDS) == EXPECTED_READY_HANDLE_UUIDS
    assert dict(WORKBENCH_DATA_HANDLE_UUIDS) == EXPECTED_DATA_HANDLE_UUIDS

    first_nodes, first_handles = builtin_workflow_catalog()
    second_nodes, second_handles = builtin_workflow_catalog()
    assert [item.uuid for item in first_nodes] == [
        item.uuid for item in second_nodes
    ]
    assert [item.uuid for item in first_handles] == [
        item.uuid for item in second_handles
    ]


def test_workbench_templates_and_handles_match_driver_decorators() -> None:
    nodes, handles = _workbench_catalog()
    assert set(nodes) == set(WORKBENCH_TEMPLATE_UUIDS.values())
    assert set(handles) == {
        *WORKBENCH_READY_HANDLE_UUIDS.values(),
        *WORKBENCH_DATA_HANDLE_UUIDS.values(),
    }

    for phase, method_name in ACTION_METHODS.items():
        template_uuid = WORKBENCH_TEMPLATE_UUIDS[phase]
        template = nodes[template_uuid]
        action_meta = get_action_meta(getattr(VirtualWorkbench, method_name))

        assert action_meta is not None
        assert action_meta["auto_prefix"] is True
        assert template["name"] == f"auto-{method_name}"
        assert template["class"] == (
            "unilabos.devices.virtual.workbench:VirtualWorkbench"
        )
        assert template["goal"] == EXPECTED_GOALS[phase]
        assert "sample_uuids" not in template["goal"]
        assert _normalized_catalog_handles(handles, template_uuid, "target") == (
            _normalized_registry_handles(action_meta["handles"], "input")
        )
        assert _normalized_catalog_handles(handles, template_uuid, "source") == (
            _normalized_registry_handles(action_meta["handles"], "output")
        )

        ready = [
            item
            for item in handles.values()
            if item["workflow_node_template_uuid"] == template_uuid
            and item["handle_key"] == "ready"
        ]
        assert {item["io_type"] for item in ready} == {"source", "target"}
        assert all(item["required"] is False for item in ready)
        assert all(item["data_key"] is None for item in ready)

    data_handles = [
        handles[uuid] for uuid in WORKBENCH_DATA_HANDLE_UUIDS.values()
    ]
    assert all(
        item["required"] is (item["io_type"] == "target")
        for item in data_handles
    )
    assert all(
        item["data_source"]
        == ("handle" if item["io_type"] == "target" else "executor")
        for item in data_handles
    )


def _node_key(node: dict) -> tuple[str, int | None]:
    return (
        node["meta_data"]["phase"],
        node["meta_data"].get("sample_number"),
    )


@pytest.mark.parametrize(
    ("scenario_id", "sample_count", "job_count", "edge_count"),
    [
        ("single_sample", 1, 4, 5),
        ("sequential_two_samples", 2, 7, 11),
        ("parallel_three_samples", 3, 10, 19),
    ],
)
def test_workbench_scenario_graphs_have_canonical_typed_edges(
    scenario_id: str,
    sample_count: int,
    job_count: int,
    edge_count: int,
) -> None:
    graph = build_workbench_scenario_graph(scenario_id, revision=7)

    assert graph["revision"] == 7
    assert len(graph["nodes"]) == job_count
    assert len(graph["edges"]) == edge_count
    assert all(
        node["material_uuid"] == DEFAULT_WORKBENCH_MATERIAL_UUID
        and node["meta_data"]["target_device_id"]
        == WORKBENCH_TARGET_DEVICE_ID
        for node in graph["nodes"]
    )
    assert all(
        node["action_name"] == f"auto-{ACTION_METHODS[_node_key(node)[0]]}"
        for node in graph["nodes"]
    )
    assert graph["nodes"][0]["param"] == {"count": sample_count}
    assert all(node["param"] == {} for node in graph["nodes"][1:])
    assert all(
        node["execution_policy"] == {"always_free": True}
        for node in graph["nodes"]
        if _node_key(node)[0] == "heat"
    )

    nodes_by_uuid = {node["uuid"]: node for node in graph["nodes"]}
    actual_edges = {
        (
            _node_key(nodes_by_uuid[edge["source_node_uuid"]]),
            _node_key(nodes_by_uuid[edge["target_node_uuid"]]),
            edge["source_handle_uuid"],
            edge["target_handle_uuid"],
        )
        for edge in graph["edges"]
    }
    expected_edges = set()
    for sample_number in range(1, sample_count + 1):
        expected_edges.update(
            {
                (
                    ("prepare", None),
                    ("move", sample_number),
                    WORKBENCH_DATA_HANDLE_UUIDS[
                        f"prepare_material_{sample_number}_source"
                    ],
                    WORKBENCH_DATA_HANDLE_UUIDS["move_material_target"],
                ),
                (
                    ("move", sample_number),
                    ("heat", sample_number),
                    WORKBENCH_DATA_HANDLE_UUIDS["move_station_source"],
                    WORKBENCH_DATA_HANDLE_UUIDS["heat_station_target"],
                ),
                (
                    ("move", sample_number),
                    ("heat", sample_number),
                    WORKBENCH_DATA_HANDLE_UUIDS["move_material_source"],
                    WORKBENCH_DATA_HANDLE_UUIDS["heat_material_target"],
                ),
                (
                    ("heat", sample_number),
                    ("output", sample_number),
                    WORKBENCH_DATA_HANDLE_UUIDS["heat_station_source"],
                    WORKBENCH_DATA_HANDLE_UUIDS["output_station_target"],
                ),
                (
                    ("heat", sample_number),
                    ("output", sample_number),
                    WORKBENCH_DATA_HANDLE_UUIDS["heat_material_source"],
                    WORKBENCH_DATA_HANDLE_UUIDS["output_material_target"],
                ),
            }
        )
    if scenario_id == "sequential_two_samples":
        expected_edges.add(
            (
                ("output", 1),
                ("move", 2),
                WORKBENCH_READY_HANDLE_UUIDS["output_source"],
                WORKBENCH_READY_HANDLE_UUIDS["move_target"],
            )
        )
    elif scenario_id == "parallel_three_samples":
        for sample_number in range(1, sample_count):
            expected_edges.add(
                (
                    ("move", sample_number),
                    ("move", sample_number + 1),
                    WORKBENCH_READY_HANDLE_UUIDS["move_source"],
                    WORKBENCH_READY_HANDLE_UUIDS["move_target"],
                )
            )
            expected_edges.add(
                (
                    ("output", sample_number),
                    ("output", sample_number + 1),
                    WORKBENCH_READY_HANDLE_UUIDS["output_source"],
                    WORKBENCH_READY_HANDLE_UUIDS["output_target"],
                )
            )
    assert actual_edges == expected_edges

    dependency_edges = [
        edge
        for edge in graph["edges"]
        if edge["source_handle_uuid"]
        in WORKBENCH_READY_HANDLE_UUIDS.values()
    ]
    expected_dependency_count = {
        "single_sample": 0,
        "sequential_two_samples": 1,
        "parallel_three_samples": 4,
    }[scenario_id]
    assert len(dependency_edges) == expected_dependency_count
    assert all(
        edge["meta_data"] == {"dependency_only": True}
        for edge in dependency_edges
    )


@pytest.mark.parametrize(
    ("scenario_id", "job_count"),
    [
        ("single_sample", 4),
        ("sequential_two_samples", 7),
        ("parallel_three_samples", 10),
    ],
)
def test_workbench_graph_validates_and_plans_expected_jobs(
    scenario_id: str,
    job_count: int,
) -> None:
    service = WorkflowService(
        WorkflowStore(":memory:"),
        authority_profile=SchedulerAuthorityProfile.LOCAL_SCHEDULER,
    )
    try:
        nodes, handles = builtin_workflow_catalog()
        service.sync_template_catalog(
            authority_id=BUILTIN_CATALOG_AUTHORITY,
            node_templates=nodes,
            handle_templates=handles,
        )
        workflow = service.create_workflow(
            name=scenario_id,
            tags=["demo", "workbench"],
            description=None,
            meta_data={},
        )
        graph = build_workbench_scenario_graph(
            scenario_id,
            revision=workflow["revision"],
        )
        saved = service.save_graph(
            workflow["uuid"],
            revision=graph["revision"],
            nodes=graph["nodes"],
            edges=graph["edges"],
        )
        assert len(saved["nodes"]) == job_count

        task = service.create_workflow_task(
            workflow_uuid=workflow["uuid"],
            run_mode="normal",
            target_node_uuid=None,
            input_value={},
            description=None,
            meta_data={},
        )
        jobs = service.list_workflow_node_jobs(task["uuid"])
        assert len(jobs) == job_count
        assert {job["executor_kind"] for job in jobs} == {"device_action"}

        planned_edges = task["execution_plan"]["edges"]
        data_edges = [
            edge for edge in planned_edges if not edge.get("dependency_only")
        ]
        dependency_edges = [
            edge for edge in planned_edges if edge.get("dependency_only")
        ]
        sample_count = (job_count - 1) // 3
        assert len(data_edges) == sample_count * 5
        expected_dependency_count = {
            "single_sample": 0,
            "sequential_two_samples": 1,
            "parallel_three_samples": 4,
        }[scenario_id]
        assert len(dependency_edges) == expected_dependency_count
        assert all(
            edge["source_data_key"]
            and edge["target_data_key"]
            and edge["source_type"] == edge["target_type"]
            for edge in data_edges
        )
    finally:
        service.close()


def test_workbench_scenario_rejects_unknown_id() -> None:
    with pytest.raises(ValueError, match="unsupported workbench scenario"):
        build_workbench_scenario_graph("unknown", revision=1)
