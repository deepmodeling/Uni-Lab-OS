"""只读检查中心冻结任务中已明确的物料输出，不执行节点或取得占用。"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from unilabos.workflow.execution_plan import ExecutionPlanBuilder
from unilabos.workflow.material_ports import _path, ports_from_graph
from unilabos.workflow.run_preflight import _check
from unilabos.workflow.workflow_boundary_binding import flatten_output_bindings
from unilabos.workflow.workflow_io import validate_workflow_graph_io

if TYPE_CHECKING:
    from unilabos.app.scheduler.inventory.store import InventoryStore
    from unilabos.workflow.task_input import PreparedTaskInput


def material_output_checks(
    graph: Mapping[str, Any], *, prepared: PreparedTaskInput | None = None,
    inventory_store: InventoryStore | None = None, run_mode: str = "normal",
    target_node_uuid: str | None = None,
) -> list[dict[str, Any]]:
    """沿正式输出绑定识别共享来源；未知设备产出保留运行时复核。"""
    metadata = graph["workflow"]["meta_data"]
    if not metadata.get("center_material_ports", metadata.get("unilab", {}).get("material_ports", [])):
        return []
    plan = prepared.execution_plan if prepared is not None else ExecutionPlanBuilder().build(
        graph, run_mode=run_mode, target_node_uuid=target_node_uuid)[0]
    nodes = {node["uuid"]: node for node in plan["nodes"]}
    bindings = flatten_output_bindings(graph_nodes=graph.get("nodes", []),
        graph_edges=graph.get("edges", []),
        output_bindings=validate_workflow_graph_io(graph).output_bindings, planned_node_uuids=set(nodes))
    checks = []
    for port in ports_from_graph(graph):
        if port["direction"] != "output":
            continue
        path = _path(port["value_path"])
        binding = bindings.get(path[0], {})
        source = nodes.get(binding.get("workflow_node_uuid"), {})
        details = {"material_port": port["name"], "value_path": port["value_path"]}
        selector = source.get("param", {}) if source.get("kind") == "material_source" else {}
        identities = []
        if binding.get("kind") == "workflow_input" and prepared is not None:
            value = prepared.resolved_input.get(binding["parameter"])
            try:
                for segment in path[1:]:
                    value = value[segment]
                values = value if port["cardinality"] == "many" else [value]
                identities = [item["uuid"] if port["local_value_kind"] == "material_ref" else item for item in values]
                if any(not isinstance(identity, str) for identity in identities):
                    identities = []
            except (KeyError, IndexError, TypeError):
                identities = []
            if identities:
                details["local_material_uuid"] = identities[0]
                candidates = [node for node in nodes.values() if node["kind"] == "material_source"
                    and node.get("param", {}).get("material_uuid") in identities]
                source = next((node for node in candidates if node["param"].get("custody_policy") == "shared_source"),
                              next(iter(candidates), {}))
                selector = source.get("param", {})
        if selector:
            if selector.get("material_uuid"):
                details["local_material_uuid"] = selector["material_uuid"]
                if selector["material_uuid"] not in identities:
                    identities.append(selector["material_uuid"])
        if selector.get("custody_policy") == "shared_source":
            checks.append(_check(check_type="material_output", status="blocked",
                code="invalid_material_output_custody", blocking=True,
                message="声明的物料输出直接返回共享来源；该输出必须取得本任务独占，或改为生成独立样品",
                node_uuid=source["uuid"], details=details))
        else:
            failure = None
            if inventory_store is not None:
                for identity in identities:
                    failure = _inventory_failure(inventory_store, identity, port["resource_template_uuid"])
                    if failure:
                        details["local_material_uuid"] = identity
                        break
            if failure:
                checks.append(_check(check_type="material_output", status="blocked",
                    code="material_output_unavailable", blocking=True, message=failure, details=details))
            else:
                checks.append(_check(check_type="material_output", status="deferred",
                    code="material_output_checked_at_runtime",
                    message="物料输出的实际载体及独占保护将在运行时复核；预检不取得占用", details=details))
    return checks


def _inventory_failure(store: InventoryStore, local_uuid: str, template_uuid: str) -> str | None:
    """复用正式出站的载体解析及来源互斥，只读观察当前状态，不申请 Task 使用权。"""
    from unilabos.app.scheduler.inventory.custody import require_source_custody_available
    from unilabos.app.scheduler.inventory.domain import InventoryError
    from unilabos.app.scheduler.inventory.handoff import StationHandoffAuthority
    from unilabos.app.scheduler.inventory.outbound import _output_carriers

    try:
        with store.read_connection() as connection:
            carriers = _output_carriers(connection, [{"local_material_uuid": local_uuid,
                "resource_template_uuid": template_uuid}])
            for carrier, materials in carriers.items():
                for row in connection.execute(
                    "SELECT amounts_json FROM inventory_reservation WHERE status='active'"
                ).fetchall():
                    if materials.intersection(json.loads(row["amounts_json"]).get("instances", [])):
                        return "声明输出的实际载体或内容当前仍被库存预留占用"
                for local in materials:
                    row = StationHandoffAuthority._material(connection, local)
                    if row["inventory_status"] not in {"warehouse", "reserved"} or row["disposition"] != "active":
                        return "声明输出的实际载体或内容当前不处于可用库存状态"
                require_source_custody_available(connection, material_uuid=carrier, task_uuid="", policy="task_exclusive")
    except (ValueError, InventoryError) as error:
        return str(error)
    return None
