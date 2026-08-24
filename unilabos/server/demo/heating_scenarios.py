"""加热 demo 的 canonical 多 Job Workflow Graph。"""

from __future__ import annotations

from typing import Any, Literal
from uuid import uuid4

from unilabos.server.protocol.heating_demo import HeatingScenarioEnvironment
from unilabos.server.workflow.builtin_catalog import (
    HEAT_READY_SOURCE_UUID,
    HEAT_READY_TARGET_UUID,
    HEAT_SITE_TEMPLATE_UUID,
    MATERIAL_TRANSFER_TEMPLATE_UUID,
    TRANSFER_READY_SOURCE_UUID,
    TRANSFER_READY_TARGET_UUID,
)


def build_heating_scenario_graph(
    scenario_id: Literal[
        "single_sequential",
        "parallel_three_site",
        "cross_device_transfer",
    ],
    *,
    revision: int,
    environment: HeatingScenarioEnvironment | dict[str, Any],
    target_temperature_c: float,
    duration_seconds: float,
) -> dict[str, Any]:
    """构造与 OpenLab 页面同形的真实 DAG。"""

    state = (
        environment
        if isinstance(environment, HeatingScenarioEnvironment)
        else HeatingScenarioEnvironment.model_validate(environment)
    )
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []

    def heat(
        *,
        name: str,
        device_id: str,
        platform_uuid: str,
        site_id: int,
        target: float,
        x: int,
        y: int = 120,
        parallel: bool = False,
    ) -> dict[str, Any]:
        return {
            "uuid": str(uuid4()),
            "workflow_node_template_uuid": HEAT_SITE_TEMPLATE_UUID,
            "material_uuid": platform_uuid,
            "name": name,
            "type": "device_action",
            "pose": {"x": x, "y": y},
            "param": {
                "site_id": site_id,
                "target_temperature_c": target,
                "duration_seconds": duration_seconds,
            },
            "action_name": "heat_site",
            "action_type": "UniLabJsonCommand",
            "execution_policy": {
                "execution_timeout_seconds": int(duration_seconds * 2 + 60),
                **({"always_free": True} if parallel else {}),
            },
            "meta_data": {
                "demo": "openlab-heating-scenarios",
                "scenario_id": scenario_id,
                "target_device_id": device_id,
                "site_id": site_id,
            },
        }

    def edge(
        source: dict[str, Any],
        target: dict[str, Any],
        source_handle_uuid: str,
        target_handle_uuid: str,
    ) -> None:
        edges.append(
            {
                "uuid": str(uuid4()),
                "source_node_uuid": source["uuid"],
                "target_node_uuid": target["uuid"],
                "source_handle_uuid": source_handle_uuid,
                "target_handle_uuid": target_handle_uuid,
                "description": "ready dependency",
                "meta_data": {"dependency_only": True},
            }
        )

    if scenario_id == "single_sequential":
        midpoint = round((25.0 + target_temperature_c) / 2.0, 3)
        first = heat(
            name="Site 1 · 第一段升温",
            device_id=state.source_device_id,
            platform_uuid=state.source_platform_uuid,
            site_id=1,
            target=midpoint,
            x=100,
        )
        second = heat(
            name="Site 1 · 第二段升温",
            device_id=state.source_device_id,
            platform_uuid=state.source_platform_uuid,
            site_id=1,
            target=target_temperature_c,
            x=420,
        )
        nodes.extend((first, second))
        edge(first, second, HEAT_READY_SOURCE_UUID, HEAT_READY_TARGET_UUID)
    elif scenario_id == "parallel_three_site":
        nodes.extend(
            heat(
                name=f"Site {site_id} · 并行加热",
                device_id=state.source_device_id,
                platform_uuid=state.source_platform_uuid,
                site_id=site_id,
                target=target_temperature_c + offset,
                x=160,
                y=40 + (site_id - 1) * 180,
                parallel=True,
            )
            for site_id, offset in enumerate((-10.0, 0.0, 10.0), start=1)
        )
    else:
        if not (
            state.target_platform_uuid
            and state.transfer_material_uuid
            and state.transfer_target_site_uuid
        ):
            raise ValueError("cross-device provision result is incomplete")
        source = heat(
            name="来源设备 · 预热",
            device_id=state.source_device_id,
            platform_uuid=state.source_platform_uuid,
            site_id=1,
            target=round((25.0 + target_temperature_c) / 2.0, 3),
            x=80,
        )
        transfer = {
            "uuid": str(uuid4()),
            "workflow_node_template_uuid": MATERIAL_TRANSFER_TEMPLATE_UUID,
            "name": "materials.v1 · 权威转移",
            "type": "tool_call",
            "pose": {"x": 390, "y": 120},
            "param": {
                "source_device_id": state.source_device_id,
                "target_device_id": state.target_device_id,
                "items": [
                    {
                        "material_uuid": state.transfer_material_uuid,
                        "target_material_uuid": state.target_platform_uuid,
                        "target_site": state.transfer_target_site_uuid,
                    }
                ],
            },
            "action_name": "materials.transfer",
            "execution_policy": {},
            "meta_data": {
                "demo": "openlab-heating-scenarios",
                "scenario_id": scenario_id,
                "authority": "materials.v1",
            },
        }
        target = heat(
            name="目标设备 · 继续加热",
            device_id=state.target_device_id,
            platform_uuid=state.target_platform_uuid,
            site_id=3,
            target=target_temperature_c,
            x=700,
        )
        nodes.extend((source, transfer, target))
        edge(source, transfer, HEAT_READY_SOURCE_UUID, TRANSFER_READY_TARGET_UUID)
        edge(transfer, target, TRANSFER_READY_SOURCE_UUID, HEAT_READY_TARGET_UUID)

    return {"revision": revision, "nodes": nodes, "edges": edges}


__all__ = ["build_heating_scenario_graph"]
