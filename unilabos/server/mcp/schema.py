"""把现有 OpenAPI 操作转换成 MCP JSON Schema；协议模型仍只有一份。"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from .catalog import Operation


def mutation_payload_spec(operation_id: str):
    from unilabos.protocol import materials

    return {
        "materials-v1.templates.create": (materials.ResourceTemplateWrite, ["create_template"]),
        "materials-v1.templates.put": (materials.ResourceTemplateWrite, ["put_template"]),
        "materials-v1.instances.instantiate": (materials.MaterialInstantiate, ["create_material_tree"]),
        "materials-v1.trees.create": (materials.MaterialTreeCreate, ["create_material_tree", "create_material"]),
        "materials-v1.instances.patch": (materials.MaterialPatch, ["patch_material", "update_material"]),
        "materials-v1.instances.data": (materials.MaterialDataWrite, ["put_data", "update_data", "update_substances"]),
        "materials-v1.instances.position": (materials.MaterialPosition, ["put_position", "update_position"]),
        "materials-v1.instances.delete": (materials.MaterialDelete, ["delete_material"]),
        "materials-v1.move": (materials.MaterialMove, ["move_material"]),
        "materials-v1.transfer": (materials.MaterialTransfer, ["transfer_material"]),
        "materials-v1.lots.inbound": (materials.InventoryLotInbound, ["inbound_inventory_lot"]),
        "materials-v1.lots.correct": (materials.InventoryLotCorrection, ["correct_inventory_lot"]),
    }.get(operation_id)


#: 统一库存命令入口：dev ``materials.v1`` 变更全部经这里，body 是带 operation 的变更信封。
COMMANDS_PATH = "/api/v1/inventory/commands"
#: 原路径参数（物料 uuid）在命令里放到顶层 ``target_uuid``。
TARGETED_OPERATIONS = frozenset({
    "materials-v1.instances.patch", "materials-v1.instances.data", "materials-v1.instances.position",
})


def normalize_body(operation: Operation, body: dict) -> dict:
    """与现有 Materials HTTP client 一样，用 protocol 补齐默认值后绑定幂等信封。"""
    spec = mutation_payload_spec(operation.id)
    if spec is None:
        return body
    from unilabos.client.materials import bind_payload
    from unilabos.protocol.materials import InventoryMutation

    values = dict(body)
    target_uuid = values.pop("target_uuid", None)
    if spec[1] == ["create_material_tree"] and values.get("operation") == "create_material_tree":
        # 出库实例化：服务端展开 registry 资源类，载荷是 MaterialInstantiate
        values["operation"] = "instantiate_material"
    mutation = InventoryMutation.model_validate(values)
    payload = spec[0].model_validate(mutation.payload)
    # 原始 payload 已经严格解析，先取出，再像 HTTP client 那样显式绑定完整 typed payload。
    # 不能把省略默认值的原 JSON 与补齐后的 model_dump 直接比较，否则合法入库也会报 422。
    envelope = mutation.model_copy(update={"payload": {}})
    bound = bind_payload(envelope, payload).model_dump(mode="json", exclude_none=False)
    if target_uuid is not None:
        bound["target_uuid"] = target_uuid
    return bound


def input_schema(document: dict[str, Any], operation: Operation) -> dict[str, Any]:
    item = document["paths"][operation.path]
    spec = item[operation.method.lower()]
    properties: dict[str, Any] = {}
    required: list[str] = []
    for location in ("path", "query"):
        parameters = [
            value for value in [*item.get("parameters", []), *spec.get("parameters", [])]
            if value.get("in") == location
        ]
        if not parameters:
            continue
        fields = {}
        mandatory = []
        for parameter in parameters:
            fields[parameter["name"]] = deepcopy(parameter["schema"])
            if parameter.get("description"):
                fields[parameter["name"]]["description"] = parameter["description"]
            if parameter.get("required"):
                mandatory.append(parameter["name"])
        properties[location] = {"type": "object", "properties": fields, "additionalProperties": False}
        if mandatory:
            properties[location]["required"] = mandatory
            required.append(location)
    request = spec.get("requestBody")
    command_body: dict[str, Any] | None = None
    command_defs: dict[str, Any] = {}
    command_spec = mutation_payload_spec(operation.id) if operation.path == COMMANDS_PATH else None
    if command_spec is not None:
        # 命令入口的 OpenAPI 请求体是通用对象：直接用 protocol 的变更模型 + 本操作载荷模型给出 body 契约
        from unilabos.protocol.materials import InventoryMutation

        model, names = command_spec
        command_body = InventoryMutation.model_json_schema()
        command_defs = command_body.pop("$defs", {})
        payload = model.model_json_schema()
        command_defs.update(payload.pop("$defs", {}))
        command_body["properties"]["payload"] = payload
        command_body["properties"]["operation"]["enum"] = names
        command_body.setdefault("required", []).append("payload")
        if operation.id in TARGETED_OPERATIONS:
            command_body["properties"]["target_uuid"] = {"type": "string", "description": "目标物料 uuid"}
            command_body["required"].append("target_uuid")
        required.append("body")
        request = None
    if request:
        content = request["content"]
        if "application/json" not in content:
            raise ValueError(f"MCP 操作需要显式适配非 JSON 请求体：{operation.id}")
        properties["body"] = deepcopy(content["application/json"]["schema"])
        if request.get("required"):
            required.append("body")
    schema: dict[str, Any] = {
        "type": "object", "properties": properties, "additionalProperties": False,
    }
    if required:
        schema["required"] = required

    # 只携带本操作实际使用的定义，避免给每个工具重复塞整个注册表契约。
    definitions: dict[str, Any] = {}

    def rewrite(value: Any) -> None:
        if isinstance(value, list):
            for child in value:
                rewrite(child)
        elif isinstance(value, dict):
            ref = value.get("$ref")
            if ref:
                prefix = "#/components/schemas/"
                if not ref.startswith(prefix):
                    raise ValueError(f"不支持的协议引用：{ref}")
                name = ref.removeprefix(prefix)
                value["$ref"] = f"#/$defs/{name}"
                if name not in definitions:
                    definitions[name] = deepcopy(document["components"]["schemas"][name])
                    rewrite(definitions[name])
            for key, child in value.items():
                if key != "$ref":
                    rewrite(child)

    rewrite(schema)
    if command_body is not None:
        properties["body"] = command_body
        definitions.update(command_defs)
    # HTTP 在 handler 内校验 InventoryMutation.payload，OpenAPI 因而只看到 JsonObject。
    # 在 MCP 中引用同一个 protocol 模型补齐该段；不重新定义 DTO、不改变线上信封。
    if "InventoryMutation" in definitions:
        spec = mutation_payload_spec(operation.id)
        if spec is not None:
            model, names = spec
            payload = model.model_json_schema()
            definitions.update(payload.pop("$defs", {}))
            mutation = definitions["InventoryMutation"]
            mutation["properties"]["payload"] = payload
            mutation["properties"]["operation"]["enum"] = names
            mutation.setdefault("required", []).append("payload")
        elif operation.id == "materials-v1.templates.delete":
            definitions["InventoryMutation"]["properties"]["operation"]["enum"] = ["delete_template"]
    if definitions:
        schema["$defs"] = definitions
    return schema
