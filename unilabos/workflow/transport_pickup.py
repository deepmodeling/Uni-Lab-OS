"""从冻结图明确声明的成功装载结果生成原 G 的持久取货事件。"""
from __future__ import annotations

import json
import logging
import sqlite3
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from unilabos.center_protocol.transport import validate_pickup_receipt
from unilabos.persistence.center_events import append_transport_progress_in_transaction
from unilabos.persistence.database import Database
from unilabos.persistence.inspection import table_exists

logger = logging.getLogger(__name__)


def pickup_binding(graph: dict[str, Any]) -> dict[str, str] | None:
    """只接受冻结图显式节点/输出绑定，不根据动作名称猜测取货含义。"""
    declaration = graph.get("workflow", {}).get("meta_data", {}).get("center_transport_pickup")
    if declaration is None:
        return None
    if (not isinstance(declaration, dict) or set(declaration) != {"node_uuid", "output_key"}
            or not isinstance(declaration["node_uuid"], str)
            or str(UUID(declaration["node_uuid"])) != declaration["node_uuid"]
            or not isinstance(declaration["output_key"], str) or not declaration["output_key"]):
        raise ValueError("取货声明须指定稳定节点 UUID 和显式结果字段")
    nodes = [node for node in graph.get("nodes", []) if node["uuid"] == declaration["node_uuid"]]
    if len(nodes) != 1 or nodes[0].get("disabled"):
        raise ValueError("取货声明节点不存在或已禁用")
    templates = [template for template in graph.get("node_templates", [])
                 if template["uuid"] == nodes[0].get("workflow_node_template_uuid")]
    schema = (templates[0].get("meta_data", {}).get("unilab", {}).get("action_contract_schema", {})
              .get("properties", {}).get("result", {})) if len(templates) == 1 else {}
    if (schema.get("properties", {}).get(declaration["output_key"], {}).get("type") != "string"
            or declaration["output_key"] not in schema.get("required", [])):
        raise ValueError("取货声明必须绑定设备动作明确且必需的字符串回执输出")
    return declaration


def capture_transport_pickups(
    connection: Database | sqlite3.Connection, *, limit: int = 100, after_invocation_uuid: str = "",
) -> tuple[list[dict[str, Any]], str]:
    """读取不可变成功结果；先提交共享 outbox，再由原运行时发送和精确确认。"""
    from unilabos.persistence import recovery_mode
    from unilabos.workflow.center_boundary import _fact_time, claimed_transport_context

    if recovery_mode.evidence_only(connection):
        return [], ""
    if not all(table_exists(connection, table) for table in ("workflow_task", "workflow_node_job_result")):
        return [], ""
    rows = connection.execute(
        "SELECT i.invocation_uuid,i.package_json,i.local_task_uuid,t.workflow_snapshot "
        "FROM center_invocation_inbox i JOIN workflow_task t ON t.uuid=i.local_task_uuid "
        "WHERE i.claim_confirmed=1 AND json_extract(i.package_json,'$.payload.transport') IS NOT NULL "
        "AND json_extract(t.workflow_snapshot,'$.workflow.meta_data.center_transport_pickup') IS NOT NULL "
        "AND EXISTS(SELECT 1 FROM workflow_node_job j JOIN workflow_node_job_result r ON r.workflow_node_job_uuid=j.uuid "
        "WHERE j.workflow_task_uuid=i.local_task_uuid "
        "AND j.workflow_node_uuid=json_extract(t.workflow_snapshot,'$.workflow.meta_data.center_transport_pickup.node_uuid') "
        "AND r.outcome='succeeded' AND j.deleted_at IS NULL AND r.deleted_at IS NULL) "
        "AND NOT EXISTS(SELECT 1 FROM center_boundary_outbox o WHERE o.invocation_uuid=i.invocation_uuid AND o.phase=2) "
        "AND i.invocation_uuid>? ORDER BY i.invocation_uuid LIMIT ?", (after_invocation_uuid, limit),
    ).fetchall()
    result = []
    for row in rows:
        try:
            if (recovery_mode.held(connection, "invocation", row["invocation_uuid"])
                    or recovery_mode.held(connection, "task", row["local_task_uuid"])):
                continue
            graph = json.loads(row["workflow_snapshot"])
            binding = pickup_binding(graph)
            if binding is None:
                continue
            context = claimed_transport_context(connection, row["invocation_uuid"])
            if context is None:
                continue
            jobs = connection.execute(
                "SELECT j.uuid,j.workflow_node_uuid,j.executor_kind,r.uuid AS result_uuid,r.return_info,r.committed_at "
                "FROM workflow_node_job j JOIN workflow_node_job_result r ON r.workflow_node_job_uuid=j.uuid "
                "WHERE j.workflow_task_uuid=? AND j.workflow_node_uuid=? AND r.outcome='succeeded' "
                "AND j.deleted_at IS NULL AND r.deleted_at IS NULL", (row["local_task_uuid"], binding["node_uuid"]),
            ).fetchall()
            if not jobs:
                continue
            if len(jobs) != 1 or jobs[0]["executor_kind"] != "device_action":
                raise ValueError("取货节点必须有唯一成功设备动作结果")
            job = jobs[0]
            output = json.loads(job["return_info"])
            if not isinstance(output, dict):
                raise ValueError("持久取货动作结果必须是对象")
            output = output.get("return_value", output)
            if not isinstance(output, dict):
                raise ValueError("取货动作结果必须是对象")
            receipt = validate_pickup_receipt(output.get(binding["output_key"]), context)
            progress = {key:receipt[key] for key in ("schema_version", "handoff_uuid", "authorization_uuid", "vehicle_id", "container_uuids", "evidence_refs")}
            if receipt["schema_version"] == 2:
                progress["execution_plan_digest"] = receipt["execution_plan_digest"]
            progress.update(stage="picked_up", job_uuid=job["uuid"], node_uuid=job["workflow_node_uuid"])
            body = {"event_uuid":str(uuid5(NAMESPACE_URL, "unilab:transport-pickup:" + job["result_uuid"])),
                    "local_task_uuid":row["local_task_uuid"], "event_type":"transport_progress",
                    "payload":{"task_payload_digest":json.loads(row["package_json"])["payload_digest"],
                               "transport_progress":progress}, "occurred_at":_fact_time(job["committed_at"])}
            result.append(append_transport_progress_in_transaction(connection, row["invocation_uuid"], body))
        except (ValueError, KeyError, TypeError) as error:
            # 原设备结果不回滚或改写；无法验证时绝不伪造取货，仍保留源库存保护。
            logger.warning("原调用取货证据待核实 invocation=%s error=%s", row["invocation_uuid"], str(error))
    return result, rows[-1]["invocation_uuid"] if rows else ""
