"""公开合同的保守中心投影；不读取私有执行图，不放宽未知类型约束。"""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Mapping
from uuid import UUID

from unilabos.app.center_client.client import CenterClientError
from unilabos.app.center_client.digest import DigestError, canonical_json, digest, is_digest
from unilabos.workflow.material_ports import (
    MaterialPortError,
    constrain_projected_material_schema,
    validate_material_ports,
)
from unilabos.workflow.schema import (
    WorkflowSchemaError,
    parse_input_contract,
    parse_output_contract,
)


class CatalogProjectionError(CenterClientError):
    """合同不能无损转换；path 精确定位不支持的公开字段。"""

    def __init__(self, path: str, message: str) -> None:
        self.path = path
        super().__init__(f"{path}: {message}")


def _identity(value: Any, path: str) -> str:
    try:
        if not isinstance(value, str) or str(UUID(value)) != value:
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise CatalogProjectionError(path, "必须是规范 UUID") from None
    return value


def _bounded_public(value: Any) -> None:
    """先限制解析工作量，再运行递归 JSON 编码和领域 parser。"""
    pending = [(value, 0)]
    remaining = 200_000
    while pending:
        item, depth = pending.pop()
        remaining -= 1
        if remaining < 0 or depth > 40:
            raise CatalogProjectionError("", "公开合同超过深度或执行预算")
        if isinstance(item, dict):
            pending.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, list):
            pending.extend((child, depth + 1) for child in item)


def _schema_budget(value: dict[str, Any], path: str) -> None:
    if len(canonical_json(value)) > 256 * 1024:
        raise CatalogProjectionError(path, "Schema 超过 256 KiB 限制")
    pending = [(value, path, 0)]
    remaining = 4096
    while pending:
        item, location, depth = pending.pop()
        remaining -= 1
        if remaining < 0 or depth > 32:
            raise CatalogProjectionError(location, "Schema 超过深度或执行预算")
        if len(item.get("enum", [])) > 1024:
            raise CatalogProjectionError(f"{location}/enum", "枚举超过 1024 项")
        pending.extend((child, f"{location}/properties/{name}", depth + 1)
                       for name, child in item.get("properties", {}).items())
        if "items" in item:
            pending.append((item["items"], f"{location}/items", depth + 1))


def _schema(raw: Mapping[str, Any], path: str, *, site_binding: Any = None) -> dict[str, Any]:
    """只转换已通过本地严格 parser 的类型，原样保留 ResourceSlot 允许列表。"""
    if "anyOf" in raw:
        # 本地 parser 仅接受一个非空成员和 null；它不是通用 anyOf 放行。
        schema = _schema(raw["anyOf"][0], f"{path}/anyOf/0", site_binding=site_binding)
        schema["type"] = [schema["type"], "null"]
        if "enum" in schema:
            schema["enum"].append(None)
        return schema
    if "$slot" in raw:
        templates = raw.get("allowed_resource_template_uuids", [])
        if len(templates) > 256:
            raise CatalogProjectionError(f"{path}/allowed_resource_template_uuids", "资源模板约束超过 256 项")
        return {
            "type": "object", "properties": {"uuid": {"type": "string", "format": "uuid"}},
            "required": ["uuid"], "additionalProperties": False,
            "resource_slot": {"allowed_resource_template_uuids": deepcopy(templates)},
        }
    if raw.get("type") == "object":
        raise CatalogProjectionError(path, "开放 object 缺少可验证字段，不能投影为闭合对象")
    result = deepcopy(dict(raw))
    # 动作字段关系需先转换为工作流边界关系，不能把动作局部字段名交给中心猜测。
    if "x-unilabos-site-selector" in result and site_binding is None:
        raise CatalogProjectionError(path, "库位输入缺少冻结的工作流边界关系，请启用 center_site_selectors")
    result.pop("x-unilabos-editor-control", None)
    result.pop("x-unilabos-site-selector", None)
    if raw.get("type") == "array":
        result["items"] = _schema(raw["items"], f"{path}/items")
    return result


def validate_receipt(value: Any, station_uuid: str) -> dict[str, Any] | None:
    """目录 current 仅允许 null 或完整精确回执，不信任远端游标跳过本地记录。"""
    if value is None:
        return None
    keys = {"station_uuid", "catalog_epoch_uuid", "catalog_sequence", "catalog_version",
            "catalog_digest", "contract_count", "tombstone_count"}
    if not isinstance(value, dict) or set(value) != keys:
        raise CenterClientError("目录回执字段无效")
    for key in ("station_uuid", "catalog_epoch_uuid", "catalog_version"):
        _identity(value[key], f"/{key}")
    if (value["station_uuid"] != station_uuid or not is_digest(value["catalog_digest"])
            or type(value["catalog_sequence"]) is not int
            or not 1 <= value["catalog_sequence"] <= 2**53 - 1):
        raise CenterClientError("目录回执身份或序号无效")
    for key in ("contract_count", "tombstone_count"):
        if type(value[key]) is not int or not 0 <= value[key] <= 10000:
            raise CenterClientError("目录回执数量无效")
    return value


def projected_snapshot(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """完整目录一次转换失败即拒绝整份，不省略无法转换的活动工作流。"""
    if (set(snapshot) != {"contracts", "tombstones"}
            or not isinstance(snapshot["contracts"], list) or not isinstance(snapshot["tombstones"], list)):
        raise CatalogProjectionError("", "公开目录快照结构无效")
    contracts = sorted((project_contract(item) for item in snapshot["contracts"]),
                       key=lambda item: item["workflow_uuid"])
    tombstones = deepcopy(snapshot["tombstones"])
    for index, item in enumerate(tombstones):
        if (not isinstance(item, dict) or set(item) != {"workflow_uuid", "reason"}
                or not isinstance(item["reason"], str) or not item["reason"].strip()
                or len(item["reason"].encode("utf-8")) > 500):
            raise CatalogProjectionError(f"/tombstones/{index}", "下架必须含稳定工作流身份和原因")
        _identity(item["workflow_uuid"], f"/tombstones/{index}/workflow_uuid")
    tombstones.sort(key=lambda item: item["workflow_uuid"])
    identities = [item["workflow_uuid"] for item in contracts + tombstones]
    if len(identities) > 10000 or len(set(identities)) != len(identities):
        raise CatalogProjectionError("", "完整目录超过数量限制或重复声明工作流")
    return {"contracts": contracts, "tombstones": tombstones}


def make_upload(snapshot: Mapping[str, Any], *, station_uuid: str, epoch: str,
                sequence: int, version: str) -> dict[str, Any]:
    """网络前固定分块、manifest 和精确回执；块边界不改变完整目录摘要。"""
    identity = {"protocol_version": "1.0", "station_uuid": station_uuid,
                "catalog_epoch_uuid": epoch, "catalog_sequence": sequence, "catalog_version": version}
    catalog = {**identity, **snapshot}
    if len(canonical_json(catalog)) > 16 * 1024 * 1024:
        raise CatalogProjectionError("", "完整目录超过 16 MiB 限制")
    chunks: list[dict[str, Any]] = []
    current = {**identity, "chunk_index": 0, "contracts": [], "tombstones": []}

    def finish(chunk: dict[str, Any]) -> dict[str, Any]:
        return {**chunk, "chunk_digest": digest(chunk)}

    for collection in ("contracts", "tombstones"):
        for entry in snapshot[collection]:
            current[collection].append(entry)
            if len(canonical_json(finish(current))) > 256 * 1024:
                current[collection].pop()
                if not current["contracts"] and not current["tombstones"]:
                    raise CatalogProjectionError("", "单条目录记录超过 256 KiB 分块限制")
                chunks.append(finish(current))
                current = {**identity, "chunk_index": len(chunks), "contracts": [], "tombstones": []}
                current[collection].append(entry)
                if len(canonical_json(finish(current))) > 256 * 1024:
                    raise CatalogProjectionError("", "单条目录记录超过 256 KiB 分块限制")
    chunks.append(finish(current))
    if len(chunks) > 128:
        raise CatalogProjectionError("", "目录分块超过 128 块")
    if sum(len(canonical_json(chunk)) for chunk in chunks) > 16 * 1024 * 1024:
        raise CatalogProjectionError("", "目录分块正文总计超过 16 MiB 限制")
    manifest = {**identity, "chunk_digests": [item["chunk_digest"] for item in chunks],
                "contract_count": len(snapshot["contracts"]), "tombstone_count": len(snapshot["tombstones"]),
                "catalog_digest": digest(catalog)}
    receipt = {key: value for key, value in manifest.items() if key not in {"protocol_version", "chunk_digests"}}
    return {"chunks": chunks, "manifest": manifest, "receipt": receipt}


def project_contract(public: Mapping[str, Any]) -> dict[str, Any]:
    """从服务公开合同构造中心 1.0 DTO；摘要保留本地原值并独立固定投影。

    executor_requirements 属于工站冻结执行约束，由原 contract_digest 固定；
    中心不获得替换本地设备的权限。ResourceSlot 只表达输入类型，不自动声明
    跨站物料端口；仅冻结合同中的显式 material_ports 声明可以成为公开端口。
    """
    try:
        _bounded_public(public)
        if len(canonical_json(public)) > 1024 * 1024:
            raise CatalogProjectionError("", "公开合同超过 1 MiB 限制")
        inputs = parse_input_contract(public["input_contract"]).to_dict()["parameters"]
        outputs = parse_output_contract(public["output_contract"]).to_dict()["outputs"]
    except WorkflowSchemaError as error:
        raise CatalogProjectionError(error.path, error.message) from error
    except (KeyError, TypeError, ValueError, DigestError, RecursionError) as error:
        if isinstance(error, CatalogProjectionError):
            raise
        raise CatalogProjectionError("", "公开合同或输入输出合同无效") from error
    contract_uuid = _identity(public.get("uuid"), "/uuid")
    workflow_uuid = _identity(public.get("workflow_uuid"), "/workflow_uuid")
    for key in ("version", "workflow_revision"):
        if type(public.get(key)) is not int or not 1 <= public[key] <= 2**53 - 1:
            raise CatalogProjectionError(f"/{key}", "发布版本必须是安全正整数")
    for key in ("source_hash", "contract_digest"):
        if not is_digest(public.get(key)):
            raise CatalogProjectionError(f"/{key}", "摘要格式无效")
    if public.get("revision_fingerprint", public["source_hash"]) != public["source_hash"]:
        raise CatalogProjectionError("/revision_fingerprint", "执行指纹必须原样等于 source_hash")

    site_bindings = public.get("site_bindings", {})
    if not isinstance(site_bindings, dict) or not set(site_bindings) <= {item["name"] for item in inputs}:
        raise CatalogProjectionError("/site_bindings", "库位绑定必须引用已声明工作流输入")
    from unilabos.workflow.center_sites import validate_site_binding
    for name, selector in site_bindings.items():
        try:
            validate_site_binding(selector)
        except ValueError as error:
            raise CatalogProjectionError(f"/site_bindings/{name}", str(error)) from error

    def boundary(descriptors: list[dict[str, Any]], direction: str) -> dict[str, Any]:
        properties: dict[str, Any] = {}
        required: list[str] = []
        collection = "parameters" if direction == "input" else "outputs"
        for index, descriptor in enumerate(descriptors):
            path = f"/{direction}_contract/{collection}/{index}"
            selector = site_bindings.get(descriptor["name"]) if direction == "input" else None
            schema = _schema(descriptor["schema"], f"{path}/schema", site_binding=selector)
            for key in ("default", "title", "description", "unit"):
                if key in descriptor:
                    schema[key] = deepcopy(descriptor[key])
            name = descriptor["name"]
            if selector is not None:
                if schema.get("type") not in ("string", ["string", "null"]):
                    raise CatalogProjectionError(path, "库位输入必须是字符串")
                schema["site_selector"] = deepcopy(selector)
            properties[name] = schema
            if direction == "output" or descriptor["required"]:
                required.append(name)
        return {"type": "object", "properties": properties,
                "required": required, "additionalProperties": False}

    result = {
        "protocol_version": "1.0", "contract_uuid": contract_uuid,
        "workflow_uuid": workflow_uuid, "publication_version": public["version"],
        "workflow_revision": public["workflow_revision"],
        "revision_fingerprint": public["source_hash"], "contract_digest": public["contract_digest"],
        "adapter_version": "1.0", "input_schema": boundary(inputs, "input"),
        "output_schema": boundary(outputs, "output"), "material_ports": [], "capabilities": [],
    }
    from unilabos.workflow.center_capabilities import validate_center_capabilities
    try:
        result["capabilities"] = validate_center_capabilities(public.get("capabilities", []))
    except ValueError as error:
        raise CatalogProjectionError("/capabilities", str(error)) from error
    # 只有冻结合同显式声明了等价车辆角色时才发布新版搬运能力。
    if "transport_allocation" in public:
        from unilabos.workflow.transport_profile import validate_profile
        try:
            if validate_profile(public["transport_allocation"]) is None:
                raise ValueError("等价车辆角色声明不能为空")
        except ValueError as error:
            raise CatalogProjectionError("/transport_allocation", str(error)) from error
        result["capabilities"].extend(["transport_preparation.v1", "allocatable_executor_binding.v1", "transport_settlement_hold.v1"])
    if public.get("transport_pickup") is True and "transport_allocation" in public:
        result["capabilities"].append("transport_pickup.v1")
    if "handoff_operations" in public:
        if public["handoff_operations"] != {"schema_version":1}:
            raise CatalogProjectionError("/handoff_operations", "工站交接操作能力声明无效")
        result["capabilities"].extend(["handoff_endpoint_snapshot.v1", "manual_handoff_operations.v1", "ingress_renewal.v1"])
    # 展示信息沿用发布合同原值；不改变执行投影摘要及既有冻结调用。
    name = public.get("name")
    if name is not None:
        if not isinstance(name, str) or not name.strip():
            raise CatalogProjectionError("/name", "工作流名称必须是非空字符串")
        result["name"] = name
    description = public.get("description")
    if description is not None:
        if not isinstance(description, str):
            raise CatalogProjectionError("/description", "工作流描述必须是字符串")
        result["description"] = description
    try:
        result["material_ports"] = validate_material_ports(
            public.get("material_ports", []), {"parameters": inputs}, {"outputs": outputs})
        constrain_projected_material_schema(result["material_ports"], result["input_schema"], result["output_schema"])
    except MaterialPortError as error:
        raise CatalogProjectionError("/material_ports", str(error)) from error
    _schema_budget(result["input_schema"], "/input_schema")
    _schema_budget(result["output_schema"], "/output_schema")
    result["projection_digest"] = digest({key: value for key, value in result.items()
                                          if key not in {"revision_fingerprint", "contract_digest", "name", "description"}})
    return result
