"""工作流候选运行预检（RunPreflight）的只读报告构造。"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from unilabos.workflow._execution_plan_graph import ExecutionPlanBuildError
from unilabos.workflow.execution_plan import ExecutionPlanBuilder
from unilabos.workflow.store import StoreConflict, utc_now


def _check(
    *,
    check_type: str,
    status: str,
    code: str,
    message: str,
    blocking: bool = False,
    node_uuid: str | None = None,
    node_name: str | None = None,
    details: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """构造一个 Backend 形状的预检检查项。"""

    result: dict[str, Any] = {
        "type": check_type,
        "status": status,
        "code": code,
        "message": message,
        "blocking": blocking,
        "details": dict(details or {}),
    }
    if node_uuid is not None:
        result["node_uuid"] = node_uuid
    if node_name:
        result["node_name"] = node_name
    return result


def _finalize(report: dict[str, Any]) -> dict[str, Any]:
    """根据结构化缺口计算文档规定的预检三态与兼容布尔值。"""

    checks = report["checks"]
    summary = report["summary"]
    summary["passed_check_count"] = sum(item["status"] == "passed" for item in checks)
    summary["blocking_check_count"] = sum(
        item["status"] == "blocked" for item in checks
    )
    summary["deferred_check_count"] = sum(
        item["status"] == "deferred" for item in checks
    )
    summary["confirmation_required_count"] = sum(
        item["status"] == "confirmation_required" for item in checks
    )
    blocking = [
        item
        for item in checks
        if item["status"] == "blocked" and item["blocking"]
    ]
    invalid = any(
        item["code"] == "execution_plan_invalid"
        or str(item["code"]).startswith("invalid_")
        for item in blocking
    )
    if invalid:
        report["status"] = "invalid"
    elif blocking:
        report["status"] = "temporarily_unavailable"
    else:
        report["status"] = "runnable_now"
    report["can_run"] = report["status"] == "runnable_now"
    return report


def unavailable_material_source_checks(plan: Mapping[str, Any]) -> list[dict[str, Any]]:
    """已知来源缺少库存读取端口时明确拒绝，不能冒充库存已验证。"""
    return [_check(check_type="material_source", node_uuid=node["uuid"], status="blocked",
        blocking=True, code="material_source_unavailable", message="未装配库存权威，无法核实来源容器")
        for node in plan.get("nodes", []) if node.get("kind") == "material_source" and not node.get("runtime_material_source")]


def _append_quantity_inventory_check(
    report: dict[str, Any],
    quantity_inventory_check: Mapping[str, Any] | None,
) -> None:
    """把可选共享数量库存预检追加到报告且不改变其他检查。"""

    if quantity_inventory_check is None:
        return
    if quantity_inventory_check.get("status") == "deferred":
        report["checks"].append(_check(
            check_type="quantity_inventory", status="deferred",
            code="upstream_inventory_pending",
            message=str(quantity_inventory_check["message"]),
            details=_quantity_inventory_details(quantity_inventory_check),
        ))
        return
    blocked = quantity_inventory_check.get("status") == "blocked"
    report["checks"].append(
        _check(
            check_type="quantity_inventory",
            status="blocked" if blocked else "passed",
            code=(
                "quantity_inventory_unavailable"
                if blocked
                else "quantity_inventory_ready"
            ),
            message=str(
                quantity_inventory_check.get("message")
                or (
                    "共享数量库存当前不足"
                    if blocked
                    else "共享数量库存当前可完成整任务准入"
                )
            ),
            blocking=blocked,
            details=_quantity_inventory_details(quantity_inventory_check),
        )
    )


def _append_execution_admission_check(
    report: dict[str, Any],
    *,
    run_mode: str,
    blocker: Mapping[str, Any] | None,
) -> None:
    """把普通/单步任务之间的执行准入冲突加入预检报告。"""

    if blocker is None:
        report["checks"].append(
            _check(
                check_type="execution_admission",
                status="passed",
                code="execution_admission_ready",
                message=(
                    "单步运行当前没有其他活动任务"
                    if run_mode == "step"
                    else "当前没有活动的单步调试任务"
                ),
            )
        )
        return
    report["checks"].append(
        _check(
            check_type="execution_admission",
            status="blocked",
            code="debug_task_conflict",
            message=(
                "单步运行需要当前 Runtime 没有其他活动任务"
                if run_mode == "step"
                else "当前存在单步调试任务，普通任务需要等待调试结束"
            ),
            blocking=True,
            details={
                "task_uuid": blocker.get("uuid"),
                "status": blocker.get("status"),
                "execution_mode": blocker.get("execution_mode"),
                "run_mode": blocker.get("run_mode"),
            },
        )
    )


def _quantity_inventory_details(check: Mapping[str, Any]) -> dict[str, Any]:
    """保留分配数量，并把每个不够的库位原样放进预检详情。"""

    details: dict[str, Any] = {
        "allocation_count": int(check.get("allocation_count") or 0)
    }
    shortages = check.get("shortages")
    if isinstance(shortages, list) and shortages:
        details["shortages"] = [
            dict(item) for item in shortages if isinstance(item, Mapping)
        ]
    return details


def build_run_preflight_report(
    *,
    graph: Mapping[str, Any],
    run_mode: str,
    target_node_uuid: str | None,
    material_resolver: Callable[[str], Mapping[str, Any] | None] | None,
    device_preflight: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None,
    quantity_inventory_check: Mapping[str, Any] | None = None,
    execution_admission: Mapping[str, Any] | None = None,
    source_error: str | None = None,
    material_output_checks: list[dict[str, Any]] | None = None,
    material_source_checks: list[dict[str, Any]] | None = None,
    execution_plan: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """只读检查当前图能否形成执行计划及其明显运行前条件。

    参数：``graph`` 是同一修订快照；运行模式和可选目标固定候选范围；
    ``material_resolver`` 只读确认设备物料身份；``device_preflight`` 用派发相同
    注册事实检查当前在线、健康、能力和忙碌状态；可选数量库存检查来自同一候选
    输入的只读库存快照。返回可展示报告；本函数不创建 Task/Job、不预留库存、
    不取得执行锁，动态条件明确标成 ``deferred``。
    """

    workflow = graph["workflow"]
    report: dict[str, Any] = {
        "workflow_uuid": workflow["uuid"],
        "workflow_revision": workflow["revision"],
        "run_mode": run_mode,
        "status": "runnable_now",
        "can_run": True,
        "checked_at": utc_now(),
        "summary": {
            "execution_node_count": 0,
            "passed_check_count": 0,
            "blocking_check_count": 0,
            "deferred_check_count": 0,
            "confirmation_required_count": 0,
        },
        "checks": list(material_output_checks or []) + list(material_source_checks or []),
    }
    if target_node_uuid is not None:
        report["target_node_uuid"] = target_node_uuid
    if source_error:
        report["checks"].append(
            _check(
                check_type="execution_plan",
                status="blocked",
                code="invalid_initial_source",
                message=source_error,
                blocking=True,
            )
        )
        if run_mode == "step" or execution_admission is not None:
            _append_execution_admission_check(
                report,
                run_mode=run_mode,
                blocker=execution_admission,
            )
        return _finalize(report)
    try:
        if execution_plan is None:
            plan, _jobs = ExecutionPlanBuilder().build(
                graph,
                run_mode=run_mode,
                target_node_uuid=target_node_uuid,
            )
        else:
            plan = execution_plan
    except (
        ExecutionPlanBuildError,
        KeyError,
        StoreConflict,
        TypeError,
        ValueError,
    ) as error:
        report["checks"].append(
            _check(
                check_type="execution_plan",
                status="blocked",
                code="execution_plan_invalid",
                message=str(error),
                blocking=True,
            )
        )
        _append_quantity_inventory_check(report, quantity_inventory_check)
        if run_mode == "step" or execution_admission is not None:
            _append_execution_admission_check(
                report,
                run_mode=run_mode,
                blocker=execution_admission,
            )
        return _finalize(report)

    planned_nodes = plan["nodes"]
    report["summary"]["execution_node_count"] = len(planned_nodes)
    _append_quantity_inventory_check(report, quantity_inventory_check)
    report["checks"].append(
        _check(
            check_type="execution_plan",
            status="passed",
            code="execution_plan_ready",
            message="工作流执行计划可以生成",
            details={"execution_node_count": len(planned_nodes)},
        )
    )
    graph_nodes = {
        str(node["uuid"]): node
        for node in graph.get("nodes", [])
        if isinstance(node, Mapping) and isinstance(node.get("uuid"), str)
    }
    for planned in planned_nodes:
        node_uuid = str(planned["uuid"])
        node = graph_nodes.get(node_uuid, {})
        kind = str(planned["kind"])
        node_name = str(node.get("name") or "")
        if kind == "manual_confirm":
            report["checks"].append(
                _check(
                    check_type="manual_confirmation",
                    status="confirmation_required",
                    code="manual_confirmation_required",
                    message="运行到该节点时需要人工确认",
                    node_uuid=node_uuid,
                    node_name=node_name,
                )
            )
        if kind == "material_source" and not any(check.get("node_uuid") == node_uuid for check in (material_source_checks or [])):
            report["checks"].append(
                _check(
                    check_type="material_source",
                    status="deferred",
                    code="material_source_checked_at_runtime" if planned.get("runtime_material_source") else "material_source_checked_at_admission",
                    message="运行到该来源时检查物料；当前缺料不阻止任务提交" if planned.get("runtime_material_source") else "物料来源将在任务准入时按同一冻结条件重新解析",
                    node_uuid=node_uuid,
                    node_name=node_name,
                )
            )
        if kind in {"device_action", "material_transfer", "manual_confirm"}:
            if device_preflight is not None:
                try:
                    current_device = device_preflight(planned)
                except Exception as error:
                    report["checks"].append(
                        _check(
                            check_type="device_selection",
                            status="blocked",
                            code=str(
                                getattr(error, "code", "device_currently_unavailable")
                            ),
                            message=str(error),
                            blocking=True,
                            node_uuid=node_uuid,
                            node_name=node_name,
                            details={
                                "resources": list(getattr(error, "resources", ()))
                            },
                        )
                    )
                else:
                    report["checks"].append(
                        _check(
                            check_type="device_selection",
                            status="passed",
                            code="device_currently_ready",
                            message="当前设备在线、健康、具备动作能力且未被占用",
                            node_uuid=node_uuid,
                            node_name=node_name,
                            details=dict(current_device),
                        )
                    )
            else:
                report["checks"].append(
                    _check(
                        check_type="device_selection",
                        status="deferred",
                        code="device_instance_selected_at_dispatch",
                        message="未装配设备注册读取端口，将在派发门禁复核",
                        node_uuid=node_uuid,
                        node_name=node_name,
                    )
                )
        material_uuid = planned.get("material_uuid")
        if kind == "device_action" and isinstance(material_uuid, str):
            material = (
                material_resolver(material_uuid)
                if material_resolver is not None
                else None
            )
            if not isinstance(material, Mapping):
                report["checks"].append(
                    _check(
                        check_type="device",
                        status="blocked",
                        code="device_material_unavailable",
                        message="设备物料不存在或当前不可用",
                        blocking=True,
                        node_uuid=node_uuid,
                        node_name=node_name,
                    )
                )
            elif device_preflight is None:
                report["checks"].append(
                    _check(
                        check_type="device",
                        status="deferred",
                        code="device_dispatch_recheck_required",
                        message="设备在线状态与动作能力将在派发时原子复核",
                        node_uuid=node_uuid,
                        node_name=node_name,
                        details={"material_uuid": material_uuid},
                    )
                )
    report["checks"].append(
        _check(
            check_type="resource_lock",
            status="deferred",
            code="resource_admission_at_dispatch",
            message=(
                "静态资源取得顺序已验证；设备、库位、物料条件和当前占用只能在"
                "派发门禁的库存事务中原子复核"
            ),
        )
    )
    if run_mode == "step" or execution_admission is not None:
        _append_execution_admission_check(
            report,
            run_mode=run_mode,
            blocker=execution_admission,
        )
    return _finalize(report)


__all__ = ["build_run_preflight_report"]


def build_batch_run_preflight_report(
    items: list[dict[str, Any]], quantity_inventory_check: Mapping[str, Any],
    material_source_checks: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """保留各项原报告，并以整批库存检查和全部候选计算最终是否可运行。"""

    report: dict[str, Any] = {
        "checked_at": utc_now(), "items": items, "checks": list(material_source_checks or []),
        "summary": {"item_count": len(items), "execution_node_count": sum(
            item["summary"]["execution_node_count"] for item in items
        )},
    }
    _append_quantity_inventory_check(report, quantity_inventory_check)
    _finalize(report)
    for key in ("passed_check_count", "blocking_check_count", "deferred_check_count", "confirmation_required_count"):
        report["summary"][key] += sum(item["summary"][key] for item in items)
    if any(item["status"] == "invalid" for item in items):
        report["status"] = "invalid"
    elif any(not item["can_run"] for item in items):
        report["status"] = "temporarily_unavailable"
    report["can_run"] = report["status"] == "runnable_now"
    return report


def mark_upstream_input_pending(report: dict[str, Any]) -> None:
    """上游未产出时延后参数和库存检查；通过条件由真实阻断决定。"""

    report["checks"].append(_check(
        check_type="workflow_input", status="deferred", code="upstream_input_pending",
        message="等待上游输出或物料交接，实际输入与数量库存尚未完成检查",
    ))
    _finalize(report)
