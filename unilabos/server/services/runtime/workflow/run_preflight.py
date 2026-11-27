"""候选运行的只读预检；复用任务计划和库存分配，不取得执行许可。"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from unilabos.protocol.materials import InventoryRequirement
from unilabos.protocol.runtime.site_references import owner_reference
from unilabos.registry.typed_contract import validate_action_values
from .entry_parameters import declared_input_contract, resolve_task_input
from .store import utc_now
from .errors import StoreConflict


class ResourceUnavailableError(ValueError):
    """物料或库位的动态状态暂时不允许执行。

    ``wait_code`` 是会随其他作业 / 物料移动而解除的临时条件（如 ``site_occupied``）：派发时遇到它
    attempt 继续排队（``scheduler/resource_wait_policy.py``）；为空则按失败处理。
    """

    def __init__(self, message: str, *, wait_code: str | None = None) -> None:
        super().__init__(message)
        self.wait_code = wait_code


def deferred_parameters(plan: Mapping[str, Any], node: Mapping[str, Any]) -> list[str]:
    """上游输出、库存分配和循环变量在派发时才能确定；按所属物料选择的库位参数在任务准入时才选定。"""
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
            return value.get("kind") == "node_output" or any(dynamic(item) for item in value.values())
        return isinstance(value, list) and any(dynamic(item) for item in value)
    names.update(key for key, value in node.get("param", {}).items() if dynamic(value))
    names.update(key for key, value in node.get("param", {}).items() if owner_reference(value) is not None)
    return sorted(names - {""})


def build_run_preflight_report(
    service: Any, *, workflow_uuid: str, run_mode: str, target_node_uuid: str | None,
    input_value: dict[str, Any], expected_revision: int | None = None,
    inventory_bindings: Sequence[Any] = (),
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
        input_contract = declared_input_contract(graph["workflow"])
        resolved = resolve_task_input(input_contract, input_value)
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
    if inventory_bindings:
        # 与建任务同一规则把绑定落到候选计划：下面的库存检查按绑定的来源报告
        from .task_lifecycle import bind_plan_inventory, inventory_binding_sources, normalize_inventory_bindings
        try:
            bindings = normalize_inventory_bindings(list(inventory_bindings))
            bind_plan_inventory(plan, bindings, inventory_binding_sources(bindings, materials)
                                if materials is not None else None)
        except ValueError as error:
            check("inventory", "blocked", "invalid_inventory_binding", str(error))
            return finalize(report)
        except (RuntimeError, OSError) as error:
            check("inventory", "blocked", "inventory_unavailable", f"无法读取绑定的库存：{error}")
            return finalize(report)
        check("inventory", "passed", "inventory_bindings_applied", "库存绑定已对应到计划中的库存需求",
              details={"bindings": bindings})
    # 按库位绑定的物料参数（{"site_uuid"}）：与建任务同一条解析路径，换成此刻占着库位的物料
    from .material_sites import (
        MaterialSiteError,
        bind_material_sites,
        check_input_material_templates,
        input_template_allowlists,
        read_site_facts,
        site_references,
    )

    references = site_references([resolved, *(node.get("param") or {} for node in plan["nodes"])])
    if references:
        try:
            site_bindings = bind_material_sites(plan, dict(resolved), read_site_facts(materials, references),
                                                input_contract=input_contract)
        except MaterialSiteError as error:
            node = next((item for item in plan["nodes"] if item["uuid"] == error.details.get("node_uuid")), None)
            check("resources", "blocked", "invalid_material_site", error.message, node=node, details=error.details)
            return finalize(report)
        except (RuntimeError, OSError) as error:
            check("resources", "blocked", "material_site_unavailable", f"无法读取按库位绑定的库位：{error}")
            return finalize(report)
        check("resources", "passed", "material_sites_bound", "按库位绑定的物料参数已解析为库位上的物料",
              details={"bindings": site_bindings})
    # 直接给 {"uuid"} 的工作流物料输入：按 @workflow(allowed_templates=...) 核对模板（按库位绑定的已在上面核对）
    allowlists = input_template_allowlists(input_contract)
    if allowlists and materials is not None:
        try:
            check_input_material_templates(resolved, allowlists, lambda identity: material_template_facts(materials, identity))
        except MaterialSiteError as error:
            check("resources", "blocked", "invalid_material_template", error.message, details=error.details)
            return finalize(report)
        except (RuntimeError, OSError) as error:
            check("resources", "blocked", "material_template_unavailable", f"无法读取工作流物料输入的模板：{error}")
            return finalize(report)
    # 按所属物料选择的库位参数（{"owner_uuid"}）：任务准入时才选定并预留，这里只读候选
    from .site_selections import SiteSelectionError, check_owner_sites, owner_site_requests, read_owner_facts

    nodes_by_uuid = {item["uuid"]: item for item in plan["nodes"]}
    try:
        selection_requests = owner_site_requests(plan)
        selection_results = check_owner_sites(selection_requests, read_owner_facts(materials, selection_requests))
    except SiteSelectionError as error:
        check("resources", "blocked", "invalid_site_selection", error.message,
              node=nodes_by_uuid.get(error.details.get("node_uuid")), details=error.details)
        return finalize(report)
    except (RuntimeError, OSError) as error:
        check("resources", "blocked", "site_selection_facts_unavailable", f"无法读取按所属物料选择的库位：{error}")
        return finalize(report)
    for request, result in zip(selection_requests, selection_results):
        details = {"parameter": request.parameter, "owner_uuid": request.owner_uuid,
                   "occupant_uuid": request.occupant_uuid, **result}
        if result["available"]:
            check("resources", "passed", "site_selection_ready",
                  f"库位参数 {request.parameter} 将在任务准入时按库位序号选定（当前可用 {len(result['available'])} 个）",
                  node=nodes_by_uuid.get(request.node_uuid), details=details)
        else:
            check("resources", "blocked", "site_selection_unavailable",
                  f"库位参数 {request.parameter}：所属物料下接受放入物料的库位此刻都被占用或已预留（提交后排队，空出后选定）",
                  node=nodes_by_uuid.get(request.node_uuid), details=details)
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
        if node.get("inventory_deferred"):
            check("inventory", "deferred", "selected_inventory_pending",
                  "节点选中并就绪后、动作执行前预留；那时不足报动作预检错误，挂起等处置（补料后重试，或跳过 / 人工替换 / 放弃）", node=node)
        else:
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
            # Host 因 backend 不支持跳过的设备登记为停用 route，带不可用原因
            skipped = next((route["config"]["unavailable"] for endpoint in endpoints
                            for route in endpoint.get("device_routes") or []
                            if str(route.get("device_uuid")) in identities and route.get("enabled") is False
                            and isinstance((route.get("config") or {}).get("unavailable"), dict)), None)
            if len(matches) == 1:
                message = "设备在线且动作已注册"
            elif skipped is not None:
                message = f"设备未在 Host 上启动：{skipped.get('reason') or '当前 backend 不支持该设备'}"
            else:
                message = "设备离线、能力未注册或存在重复路由"
            check("device", "passed" if len(matches) == 1 else "blocked",
                  "device_action_ready" if len(matches) == 1 else "device_action_unavailable",
                  message, node=node, details={"unavailable": skipped} if skipped is not None else None)
        else:
            check("device", "blocked", "device_snapshot_unavailable", "尚未接入设备在线快照", node=node)
        schema = node.get("param_schema")
        if schema and "x-unilabos-action-contract" in schema:
            try:
                values = validate_action_values(schema, node["param"], deferred=pending, managed_resources=True)
                validate_resource_values(schema, values, materials, deferred=pending, device_id=device or None)
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
                    ) + ("（实物够，被其他任务预留：提交后排队，释放后取用）"
                         if shortage.get("cause") == "reserved_by_other_tasks" else ""))
                check("inventory", "passed" if result["available"] else "blocked",
                      "inventory_ready" if result["available"] else "inventory_shortage",
                      "整任务当前库存足够" if result["available"] else "；".join(shortage_messages), details=result)
            except (ValueError, RuntimeError) as error:
                check("inventory", "blocked", "inventory_unavailable", str(error))
    check("dispatch", "deferred", "dispatch_recheck_required", "派发时重新确认在线状态、库存、资源锁及暂停状态；预检不占用资源")
    return finalize(report)


def material_template_facts(materials: Any, identity: str) -> dict[str, Any]:
    """物料的模板事实（``template_uuid`` / ``template_name``），供工作流物料输入的模板核对。"""

    material = materials.get_material(identity).material
    template_uuid = str(material.template_uuid or "")
    return {"template_uuid": template_uuid, "template_name": getattr(material, "template_name", None) or template_uuid}


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
    *, deferred: Sequence[str] = (), device_id: str | None = None,
) -> None:
    """校验已知物料身份、设备参数及库位参数（MaterialRole.site_picker 声明的库位关系）的归属／占用；预检与派发共用。

    ``device_id`` 是执行设备：``site_picker`` 省略 ``of`` 时库位属于它的根节点（不给时跳过这条核对）。
    """
    from unilabos.registry.action_resource_contract import EXECUTING_DEVICE
    from unilabos.server.scheduler.tenancy_contract import slot_parameters
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
            # 物料引用的各种线上形态（{uuid} 引用、上游端口的扁平资源树、uuid 字符串）都取权威 uuid；
            # 只给名字的引用在派发前才解析，这里没有 uuid 可查，跳过
            from unilabos.server.scheduler.materials import extract_material_uuids

            return sorted(extract_material_uuids(value))
        if isinstance(value, list):
            return [uuid for item in value for uuid in resources(field.get("items", {}), item)]
        return []
    contract = schema.get("x-unilabos-action-contract", {}).get("resource_contract") or {}
    device_parameters = set(contract.get("required_device_params", []))
    device_parameters.update(item["param"] for item in contract.get("resource_params", []) if item["role"] in {"device","motion"})
    tenancy = contract.get("device_tenancy") or {}
    device_parameters.update(tenancy[field] for field in ("acquire_device_param","release_device_param") if tenancy.get(field))
    device_parameters.discard(EXECUTING_DEVICE)
    # 按类型：DeviceSlot 参数（含列表）都要指向已登记设备（派发时按它加设备锁）；没有物料权威时（如 Host 本地）不核对
    if materials is not None:
        device_parameters.update(slot_parameters(schema, "device"))

    def device_aggregate(name: str, value: str) -> Any:
        if materials is None:
            raise ResourceUnavailableError("物料权威尚未接入，无法校验设备 ID")
        try:
            aggregate = materials.get_material_by_resource_id(value)
        except (RuntimeError, OSError) as error:
            if getattr(error,"code",None) == "not_found":
                raise ValueError(f"设备参数 {name} 指向不存在的设备 {value}") from None
            raise ResourceUnavailableError(f"无法读取设备参数 {name}：{error}") from error
        if aggregate.material.resource_type != "device" or aggregate.material.resource_id != value:
            raise ValueError(f"设备参数 {name} 不是有效的设备 ID")
        return aggregate

    for name in device_parameters:
        if name in deferred or name not in values:
            continue
        raw = values[name]
        for value in ([raw] if isinstance(raw, str) else raw if isinstance(raw, list) else []):
            if not isinstance(value, str) or not value.strip():
                continue
            aggregate = device_aggregate(name, value)
            if aggregate.material.lifecycle_status in {"quarantined","consumed","disposed","deleted","retired"}:
                raise ResourceUnavailableError(f"设备参数 {name} 当前不可用")
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
            owner_value = values.get(selector["owner"]) if selector.get("owner") else device_id
            if isinstance(owner_value, str):
                # 所属方是设备（DeviceSlot 参数，或省略 of 时的执行设备）：库位属于设备根节点
                if not owner_value.strip():
                    continue
                owner = device_aggregate(selector.get("owner") or name, owner_value).material.material_uuid
            else:
                owner = (owner_value or {}).get("uuid")
            if not owner:
                continue
            occupant = values.get(selector.get("occupant"), {}) or {}
            aggregate = material(owner, name)
            # 库位 uuid；或所属方下唯一的库位名 / 序号（工作流里写死的工位，派发前解析成库位 uuid）
            matches = [site for site in aggregate.sites if site.site_uuid == values[name]] or [
                site for site in aggregate.sites
                if isinstance(values[name], str)
                and values[name] in (getattr(site, "label", None), str(getattr(site, "site_index", None)))]
            site = matches[0] if len(matches) == 1 else None
            if site is None:
                raise ValueError(f"库位参数 {name} 不属于所选容器或已失效")
            if not selector["allow_occupied"] and site.occupied_material_uuid not in {None, occupant.get("uuid")}:
                raise ResourceUnavailableError(f"库位参数 {name} 已被其他物料占用", wait_code="site_occupied")
