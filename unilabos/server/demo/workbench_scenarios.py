"""虚拟工作台的 canonical 多 Job Workflow Graph。"""

from __future__ import annotations

from typing import Any, Literal
from uuid import uuid4

from unilabos.server.workflow.builtin_catalog import (
    WORKBENCH_DATA_HANDLE_UUIDS,
    WORKBENCH_READY_HANDLE_UUIDS,
    WORKBENCH_TEMPLATE_UUIDS,
)

WorkbenchScenarioId = Literal[
    "single_sample",
    "sequential_two_samples",
    "parallel_three_samples",
]

WORKBENCH_TARGET_DEVICE_ID = "virtual-workbench"
DEFAULT_WORKBENCH_MATERIAL_UUID = "8c340a91-f905-41b0-83b3-5b8774724f02"

_ACTION_NAMES = {
    "prepare": "auto-prepare_materials",
    "move": "auto-move_to_heating_station",
    "heat": "auto-start_heating",
    "output": "auto-move_to_output",
}
_DISPLAY_NAMES = {
    "prepare": "准备工作台物料",
    "move": "移入加热工位",
    "heat": "启动加热",
    "output": "移至输出位",
}


def build_workbench_scenario_graph(
    scenario_id: WorkbenchScenarioId,
    *,
    revision: int,
    workbench_material_uuid: str = DEFAULT_WORKBENCH_MATERIAL_UUID,
) -> dict[str, Any]:
    """构造完全由 ``VirtualWorkbench`` 原子动作组成的真实 DAG。"""

    sample_counts = {
        "single_sample": 1,
        "sequential_two_samples": 2,
        "parallel_three_samples": 3,
    }
    try:
        sample_count = sample_counts[scenario_id]
    except KeyError:
        raise ValueError(f"unsupported workbench scenario: {scenario_id}") from None

    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []

    def node(
        phase: str,
        *,
        x: int,
        y: int,
        sample_number: int | None = None,
        param: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        suffix = "" if sample_number is None else f" · 样品 {sample_number}"
        return {
            "uuid": str(uuid4()),
            "workflow_node_template_uuid": WORKBENCH_TEMPLATE_UUIDS[phase],
            "material_uuid": workbench_material_uuid,
            "name": f"{_DISPLAY_NAMES[phase]}{suffix}",
            "type": "device_action",
            "pose": {"x": x, "y": y},
            "param": dict(param or {}),
            "action_name": _ACTION_NAMES[phase],
            "action_type": "UniLabJsonCommand",
            "execution_policy": (
                {"always_free": True} if phase == "heat" else {}
            ),
            "meta_data": {
                "demo": "virtual-workbench-scenarios",
                "scenario_id": scenario_id,
                "target_device_id": WORKBENCH_TARGET_DEVICE_ID,
                "phase": phase,
                **(
                    {"sample_number": sample_number}
                    if sample_number is not None
                    else {}
                ),
            },
        }

    def edge(
        source: dict[str, Any],
        target: dict[str, Any],
        source_handle_uuid: str,
        target_handle_uuid: str,
        *,
        dependency_only: bool = False,
    ) -> None:
        edges.append(
            {
                "uuid": str(uuid4()),
                "source_node_uuid": source["uuid"],
                "target_node_uuid": target["uuid"],
                "source_handle_uuid": source_handle_uuid,
                "target_handle_uuid": target_handle_uuid,
                "description": (
                    "ready dependency" if dependency_only else "typed data flow"
                ),
                "meta_data": (
                    {"dependency_only": True}
                    if dependency_only
                    else {"data_flow": True}
                ),
            }
        )

    prepare = node(
        "prepare",
        x=80,
        y=80 + (sample_count - 1) * 100,
        param={"count": sample_count},
    )
    nodes.append(prepare)

    cycles: list[dict[str, dict[str, Any]]] = []
    for sample_number in range(1, sample_count + 1):
        row_y = 80 + (sample_number - 1) * 200
        move = node("move", x=360, y=row_y, sample_number=sample_number)
        heat = node("heat", x=660, y=row_y, sample_number=sample_number)
        output = node("output", x=960, y=row_y, sample_number=sample_number)
        nodes.extend((move, heat, output))
        cycles.append({"move": move, "heat": heat, "output": output})

        edge(
            prepare,
            move,
            WORKBENCH_DATA_HANDLE_UUIDS[
                f"prepare_material_{sample_number}_source"
            ],
            WORKBENCH_DATA_HANDLE_UUIDS["move_material_target"],
        )
        edge(
            move,
            heat,
            WORKBENCH_DATA_HANDLE_UUIDS["move_station_source"],
            WORKBENCH_DATA_HANDLE_UUIDS["heat_station_target"],
        )
        edge(
            move,
            heat,
            WORKBENCH_DATA_HANDLE_UUIDS["move_material_source"],
            WORKBENCH_DATA_HANDLE_UUIDS["heat_material_target"],
        )
        edge(
            heat,
            output,
            WORKBENCH_DATA_HANDLE_UUIDS["heat_station_source"],
            WORKBENCH_DATA_HANDLE_UUIDS["output_station_target"],
        )
        edge(
            heat,
            output,
            WORKBENCH_DATA_HANDLE_UUIDS["heat_material_source"],
            WORKBENCH_DATA_HANDLE_UUIDS["output_material_target"],
        )

    if scenario_id == "sequential_two_samples":
        edge(
            cycles[0]["output"],
            cycles[1]["move"],
            WORKBENCH_READY_HANDLE_UUIDS["output_source"],
            WORKBENCH_READY_HANDLE_UUIDS["move_target"],
            dependency_only=True,
        )
    elif scenario_id == "parallel_three_samples":
        # 三个样品共用一只机械臂：放样和出样动作必须分别由调度器串行，
        # 但每次放样后对应的加热节点即可与后续放样并行。
        for previous_cycle, next_cycle in zip(cycles, cycles[1:]):
            edge(
                previous_cycle["move"],
                next_cycle["move"],
                WORKBENCH_READY_HANDLE_UUIDS["move_source"],
                WORKBENCH_READY_HANDLE_UUIDS["move_target"],
                dependency_only=True,
            )
            edge(
                previous_cycle["output"],
                next_cycle["output"],
                WORKBENCH_READY_HANDLE_UUIDS["output_source"],
                WORKBENCH_READY_HANDLE_UUIDS["output_target"],
                dependency_only=True,
            )

    return {"revision": revision, "nodes": nodes, "edges": edges}


__all__ = [
    "DEFAULT_WORKBENCH_MATERIAL_UUID",
    "WORKBENCH_TARGET_DEVICE_ID",
    "WorkbenchScenarioId",
    "build_workbench_scenario_graph",
]
