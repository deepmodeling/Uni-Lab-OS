"""候选运行的只读预检；复用任务计划和库存分配，不取得执行许可。"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from unilabos.protocol.materials import InventoryRequirement
from unilabos.registry.typed_contract import validate_action_values
from .entry_parameters import declared_input_contract, resolve_task_input
from .store import utc_now
from .errors import StoreConflict


class ResourceUnavailableError(ValueError):
    """物料或库位的动态状态暂时不允许执行。"""


def deferred_parameters(plan: Mapping[str, Any], node: Mapping[str, Any]) -> list[str]:
    """上游输出、库存分配和循环变量在派发时才能确定。"""
    names = {
        str(edge.get("target_data_key") or "").split("@@@")[-1]
        for edge in plan.get("edges", [])
        if edge.get("target_node_uuid") == node["uuid"] and not edge.get("dependency_only")
    }
    names.update(item["key"] for item in node.get("inventory_requirements", []))
    def dynamic(value: Any) -> bool:
        if isinstance(value, str):
            return "{{loop." in value
        if isinstance(value, Mapping):
            return any(dynamic(item) for item in value.values())
        return isinstance(value, list) and any(dynamic(item) for item in value)
    names.update(key for key, value in node.get("param", {}).items() if dynamic(value))
    return sorted(names - {""})


def build_run_preflight_report(
    service: Any, *, workflow_uuid: str, run_mode: str, target_node_uuid: str | None,
    input_value: dict[str, Any], expected_revision: int | None = None,
) -> dict[str, Any]:
    # 同一读事务冻结图、模板及修订；后续外部状态分别采样，不伪称跨库原子快照。
    with service._lock:
        nested = service.connection.in_transaction
        if not nested:
            service.connection.execute("BEGIN")
        try:
            graph = service.get_graph(workflow_uuid)
        finally:
            if not nested:
                service.connection.rollback()
    report: dict[str, Any] = {
        "workflow_uuid": graph["workflow"]["uuid"],
        "workflow_revision": graph["workflow"]["revision"],
        "run_mode": run_mode, "checked_at": utc_now(), "checks": [],
        "summary": {"execution_node_count": 0},
    }
    if target_node_uuid is not None:
        report["target_node_uuid"] = target_node_uuid

    def check(kind: str, status: str, code: str, message: str, *, node=None, details=None):
        item = {"type": kind, "status": status, "code": code, "message": message,
                "blocking": status == "blocked", "details": details or {}}
        if node is not None:
            item["node_uuid"] = node["uuid"]
        report["checks"].append(item)

    try:
        if run_mode not in {"normal", "step", "single_node"}:
            raise ValueError("运行模式无效")
        if expected_revision is not None and expected_revision != graph["workflow"]["revision"]:
            raise ValueError("工作流修订已变化，请刷新后重新预检")
        resolved = resolve_task_input(declared_input_contract(graph["workflow"]), input_value)
        plan, _jobs = service._build_execution_plan(
            graph, run_mode=run_mode, target_node_uuid=target_node_uuid, entry_input=resolved,
        )
    except (ValueError, KeyError, TypeError, StoreConflict) as error:
        check("execution_plan", "blocked", "invalid_execution_plan", str(error))
        return finalize(report)
    report["summary"]["execution_node_count"] = len(plan["nodes"])
    check("execution_plan", "passed", "execution_plan_ready", "候选输入可形成执行计划")
    sources = {node["uuid"]: node for node in graph["nodes"]}
    requirements = []
    materials = service._preflight_materials
    endpoints = None
    if service._preflight_endpoints is not None:
        try:
            endpoints = service._preflight_endpoints()
        except (RuntimeError, ValueError, OSError) as error:
            check("device", "blocked", "device_snapshot_unavailable", str(error))
    for node in plan["nodes"]:
        pending = deferred_parameters(plan, node)
        if pending:
            check("parameters", "deferred", "runtime_values_pending", "上游结果、循环变量或库存分配将在派发时校验",
                  node=node, details={"parameters": pending})
        for item in node.get("inventory_requirements", []):
            requirements.append(InventoryRequirement.model_validate({**item, "key": f'{node["uuid"]}/{item["key"]}'}))
        if node["kind"] == "loop":
            check("inventory", "deferred", "loop_inventory_pending", "循环后续迭代需要重新预留库存", node=node)
        if node["kind"] != "device_action":
            continue
        source = sources[node["uuid"]]
        device = str((source.get("meta_data") or {}).get("target_device_id") or node.get("material_uuid") or source.get("material_uuid") or node["param"].get("device_id") or "")
        action = str(source.get("action_name") or node["param"].get("action") or "")
        if not device or not action:
            check("device", "blocked", "invalid_device_action", "节点缺少设备或动作", node=node)
        elif endpoints is not None:
            identities = {device}
            if materials is not None:
                for lookup_name in ("get_material", "get_material_by_resource_id"):
                    try:
                        aggregate = getattr(materials, lookup_name)(device)
                        identities.update((aggregate.material.material_uuid, aggregate.material.resource_id))
                        break
                    except Exception as error:
                        if getattr(error, "code", None) != "not_found":
                            check("device", "blocked", "device_identity_unavailable", str(error), node=node)
                            break
            matches = [cap for endpoint in endpoints if endpoint.get("state") == "online"
                       for cap in endpoint.get("action_capabilities", [])
                       if cap.get("state") == "active" and str(cap.get("device_uuid")) in identities
                       and cap.get("action_name") in {action, f"auto-{action}"}]
            check("device", "passed" if len(matches) == 1 else "blocked",
                  "device_action_ready" if len(matches) == 1 else "device_action_unavailable",
                  "设备在线且动作已注册" if len(matches) == 1 else "设备离线、能力未注册或存在重复路由", node=node)
        else:
            check("device", "blocked", "device_snapshot_unavailable", "尚未接入设备在线快照", node=node)
        schema = node.get("param_schema")
        if schema and "x-unilabos-action-contract" in schema:
            try:
                values = validate_action_values(schema, node["param"], deferred=pending)
                validate_resource_values(schema, values, materials, deferred=pending)
                check("action_contract", "passed", "action_contract_ready", "静态动作参数符合合同", node=node)
            except ResourceUnavailableError as error:
                check("resources", "blocked", "resource_unavailable", str(error), node=node)
            except ValueError as error:
                check("action_contract", "blocked", "invalid_action_parameters", str(error), node=node)
    if requirements:
        if materials is None:
            check("inventory", "blocked", "inventory_authority_unavailable", "库存权威尚未接入")
        else:
            try:
                result = materials.preview_inventory(requirements)
                shortage_messages = []
                for shortage in result["shortages"]:
                    identity, _, key = shortage["key"].partition("/")
                    shortage["node_uuid"] = identity
                    shortage["requirement_key"] = key
                    name = sources.get(identity, {}).get("name", identity)
                    shortage_messages.append(f"{name}：{key} " + (
                        f"缺少 {shortage['shortage']:g} {shortage['unit']}" if "shortage" in shortage else shortage["message"]
                    ))
                check("inventory", "passed" if result["available"] else "blocked",
                      "inventory_ready" if result["available"] else "inventory_shortage",
                      "整任务当前库存足够" if result["available"] else "；".join(shortage_messages), details=result)
            except (ValueError, RuntimeError) as error:
                check("inventory", "blocked", "inventory_unavailable", str(error))
    check("dispatch", "deferred", "dispatch_recheck_required", "派发时重新确认在线状态、库存、资源锁及暂停状态；预检不占用资源")
    return finalize(report)


def finalize(report: dict[str, Any]) -> dict[str, Any]:
    checks = report["checks"]
    for status in ("passed", "blocked", "deferred", "confirmation_required"):
        key = "blocking" if status == "blocked" else status
        report["summary"][f"{key}_check_count"] = sum(item["status"] == status for item in checks)
    blocking = [item for item in checks if item["blocking"]]
    report["status"] = "invalid" if any(item["code"].startswith("invalid_") for item in blocking) else (
        "temporarily_unavailable" if blocking else "runnable_now"
    )
    report["can_run"] = report["status"] == "runnable_now"
    return report


def validate_resource_values(
    schema: Mapping[str, Any], values: Mapping[str, Any], materials: Any,
    *, deferred: Sequence[str] = (),
) -> None:
    """校验已知物料身份及 SiteSelector 的归属／占用；预检与派发共用。"""
    fields = schema.get("properties", {}).get("goal", {}).get("properties", {})

    def material(identity: str | None, name: str) -> Any:
        if materials is None:
            raise ResourceUnavailableError("物料权威尚未接入，无法校验资源参数")
        try:
            return materials.get_material(identity)
        except (RuntimeError, OSError) as error:
            if getattr(error, "code", None) == "not_found":
                raise ValueError(f"物料参数 {name} 指向不存在的物料 {identity}") from None
            raise ResourceUnavailableError(f"无法读取物料参数 {name}：{error}") from error

    def resources(field: Mapping[str, Any], value: Any) -> list[str]:
        if value is None:
            return []
        if "x-unilabos-material-lock" in field:
            return [value["uuid"]]
        if isinstance(value, list):
            return [uuid for item in value for uuid in resources(field.get("items", {}), item)]
        return []
    for name, field in fields.items():
        if name in deferred or name not in values:
            continue
        for identity in resources(field, values[name]):
            try:
                aggregate = material(identity, name)
                if aggregate.material.lifecycle_status in {"quarantined", "consumed", "disposed", "deleted"}:
                    raise ResourceUnavailableError(f"物料参数 {name} 当前处于 {aggregate.material.lifecycle_status} 状态")
                symbols = schema["x-unilabos-action-contract"].get("resource_template_symbols", {}).get("goal", {}).get(name, [])
                if symbols:
                    template = materials.get_template(aggregate.material.template_uuid)
                    if template.module_name not in symbols:
                        raise ValueError(f"物料参数 {name} 不属于合同允许的资源模板")
                contract = schema["x-unilabos-action-contract"].get("resource_contract", {})
                device_parameters = set(contract.get("required_device_params", []))
                device_parameters.update(item["param"] for item in contract.get("resource_params", []) if item["role"] in {"device", "motion"})
                if name in device_parameters and aggregate.material.resource_type != "device":
                    raise ValueError(f"资源参数 {name} 必须指向设备")
            except (RuntimeError, OSError) as error:
                if getattr(error, "code", None) == "not_found":
                    raise ValueError(f"物料参数 {name} 的模板不存在") from None
                raise ResourceUnavailableError(f"无法校验物料参数 {name}：{error}") from error
        selector = field.get("x-unilabos-site-selector")
        if selector and values[name] is not None:
            if selector["owner"] in deferred or selector.get("occupant") in deferred:
                continue
            owner = (values.get(selector["owner"]) or {}).get("uuid")
            occupant = values.get(selector.get("occupant"), {}) or {}
            aggregate = material(owner, name)
            site = next((site for site in aggregate.sites if site.site_uuid == values[name]), None)
            if site is None:
                raise ValueError(f"库位参数 {name} 不属于所选容器或已失效")
            if not selector["allow_occupied"] and site.occupied_material_uuid not in {None, occupant.get("uuid")}:
                raise ResourceUnavailableError(f"库位参数 {name} 已被其他物料占用")
