"""已发布工作流（PublishedWorkflow）合同兼容性与旧投影认证。"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from typing import Any, Literal

import rfc8785

from unilabos.workflow.handle_projection import (
    resource_slot_schema,
    workflow_handle_type,
)
from unilabos.workflow.json_codec import strict_json_equal
from unilabos.workflow.models import validate_uuid

_SHA256 = re.compile(r"sha256:[0-9a-f]{64}\Z")
PublishedWorkflowCompatibility = Literal["exact", "additive", "breaking"]
_CONTRACT_FIELDS = {
    "version",
    "compatibility_version",
    "workflow_uuid",
    "workflow_revision",
    "applied_source_hash",
    "contract_digest",
    "composition_allow_transparent",
    "input_order",
    "output_order",
}


def published_workflow_compatibility_projection(
    template: Mapping[str, Any],
    handles: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """认证并提取可随组合调用冻结的最小兼容性投影。

    参数：``template`` 与 ``handles`` 是同一已发布工作流模板聚合。返回：只含
    稳定身份、模式、摘要和有序输入/输出描述符的独立 JSON 对象。异常：聚合
    不是框架生成的封闭 v1 合同时抛出 ``TypeError`` 或 ``ValueError``。
    """

    if not isinstance(template, Mapping) or not isinstance(handles, Sequence):
        raise TypeError("已发布工作流合同聚合无效")
    template_uuid = _uuid(template.get("uuid"))
    _uuid(template.get("resource_template_uuid"))
    schema = _schema_object(template.get("schema"))
    extension = schema.get("x-unilabos-workflow-contract")
    if not isinstance(extension, Mapping) or set(extension) != _CONTRACT_FIELDS:
        raise ValueError("已发布工作流合同扩展无效")
    workflow_uuid = _uuid(extension.get("workflow_uuid"))
    revision = extension.get("workflow_revision")
    input_order = _order(extension.get("input_order"))
    output_order = _order(extension.get("output_order"))
    if (
        extension.get("version") != 1
        or extension.get("compatibility_version") != 1
        or isinstance(revision, bool)
        or not isinstance(revision, int)
        or revision < 1
        or not _digest(extension.get("applied_source_hash"))
        or not _digest(extension.get("contract_digest"))
        or not isinstance(extension.get("composition_allow_transparent"), bool)
        or template.get("type") != "workflow"
        or template.get("node_type") != "workflow"
        or template.get("name") != f"workflow:{workflow_uuid}"
    ):
        raise ValueError("已发布工作流合同身份无效")
    _validate_provenance(template)
    goal, result = _contract_envelopes(schema, input_order, output_order)
    goal_properties = goal["properties"]
    result_properties = result["properties"]
    required_inputs = set(goal["required"])
    goal_default = template.get("goal_default")
    if not isinstance(goal_default, Mapping) or set(goal_default) - set(input_order):
        raise ValueError("已发布工作流默认值无效")
    if _plain(template.get("goal")) != {name: name for name in input_order}:
        raise ValueError("已发布工作流输入映射无效")
    if _plain(template.get("result")) != {name: name for name in output_order}:
        raise ValueError("已发布工作流输出映射无效")
    by_key = _handle_index(handles, template_uuid)
    expected_keys = {
        *(("target", name) for name in input_order),
        *(("source", name) for name in output_order),
        ("target", "ready"),
        ("source", "ready"),
    }
    if set(by_key) != expected_keys:
        raise ValueError("已发布工作流连接点集合无效")

    inputs: list[dict[str, Any]] = []
    for name in input_order:
        value_schema = _plain(goal_properties[name])
        if not isinstance(value_schema, dict):
            raise TypeError("已发布工作流输入 Schema 无效")
        has_schema_default = "default" in value_schema
        schema_default = value_schema.pop("default", None)
        has_default = name in goal_default
        if has_schema_default != has_default or (
            has_default
            and not strict_json_equal(schema_default, goal_default[name])
        ):
            raise ValueError("已发布工作流默认值无效")
        handle = by_key[("target", name)]
        _validate_value_handle(
            handle,
            schema=value_schema,
            required=name in required_inputs,
            io_type="target",
        )
        descriptor: dict[str, Any] = {
            "name": name,
            "schema": value_schema,
            "required": name in required_inputs,
            "has_default": has_default,
            "handle_uuid": _uuid(handle.get("uuid")),
        }
        if has_default:
            descriptor["default"] = _plain(goal_default[name])
        inputs.append(descriptor)

    outputs: list[dict[str, Any]] = []
    for name in output_order:
        value_schema = _plain(result_properties[name])
        handle = by_key[("source", name)]
        unilab = _validate_value_handle(
            handle,
            schema=value_schema,
            required=False,
            io_type="source",
        )
        outputs.append(
            {
                "name": name,
                "schema": value_schema,
                "implicit": unilab.get("implicit_passthrough") is True,
                "handle_uuid": _uuid(handle.get("uuid")),
            }
        )
    _validate_ready_handle(by_key[("target", "ready")], "target")
    _validate_ready_handle(by_key[("source", "ready")], "source")
    expected_digest = _contract_digest(
        inputs=inputs,
        outputs=outputs,
        mode=extension["composition_allow_transparent"],
    )
    if extension["contract_digest"] != expected_digest:
        raise ValueError("已发布工作流合同摘要不自洽")
    return {
        "template_uuid": template_uuid,
        "workflow_uuid": workflow_uuid,
        "mode": extension["composition_allow_transparent"],
        "digest": extension["contract_digest"],
        "inputs": inputs,
        "outputs": outputs,
    }


def published_workflow_projection_is_canonical(
    template: Mapping[str, Any],
    handles: Sequence[Mapping[str, Any]],
) -> bool:
    """判断旧目录聚合能否作为组合调用兼容性证据。

    参数：节点模板与其连接点全集。返回：聚合通过完整认证时为 ``True``；任何
    字段、摘要或身份不自洽时为 ``False``。异常：无。
    """

    try:
        published_workflow_compatibility_projection(template, handles)
    except (KeyError, TypeError, ValueError):
        return False
    return True


def classify_published_workflow_compatibility_projections(
    previous: Mapping[str, Any],
    current: Mapping[str, Any],
) -> PublishedWorkflowCompatibility:
    """按 v1 规则分类两个已认证兼容性投影。

    参数：``previous`` 是调用节点冻结值，``current`` 是当前目录值。返回：
    ``exact``、``additive`` 或 ``breaking``；形状不可信一律为 ``breaking``。
    异常：无；所有不可信输入均收敛为 ``breaking``。
    """

    required = {
        "template_uuid",
        "workflow_uuid",
        "mode",
        "digest",
        "inputs",
        "outputs",
    }
    if not isinstance(previous, Mapping) or not isinstance(current, Mapping):
        return "breaking"
    if set(previous) != required or set(current) != required:
        return "breaking"
    if any(
        previous[key] != current[key]
        for key in ("template_uuid", "workflow_uuid", "mode")
    ):
        return "breaking"
    previous_inputs = previous.get("inputs")
    current_inputs = current.get("inputs")
    previous_outputs = previous.get("outputs")
    current_outputs = current.get("outputs")
    if not all(
        isinstance(value, (list, tuple))
        for value in (
            previous_inputs,
            current_inputs,
            previous_outputs,
            current_outputs,
        )
    ):
        return "breaking"
    if previous["digest"] == current["digest"]:
        return (
            "exact"
            if previous_inputs == current_inputs and previous_outputs == current_outputs
            else "breaking"
        )
    if (
        list(current_inputs[: len(previous_inputs)]) != list(previous_inputs)
        or list(current_outputs[: len(previous_outputs)]) != list(previous_outputs)
    ):
        return "breaking"
    added_inputs = current_inputs[len(previous_inputs) :]
    if any(
        not isinstance(item, Mapping)
        or item.get("required") is not False
        or item.get("has_default") is not True
        for item in added_inputs
    ):
        return "breaking"
    return "additive"


def classify_pinned_published_workflow_invocation(
    *,
    previous_node: Mapping[str, Any],
    current_node: Mapping[str, Any],
    previous_templates: Sequence[Mapping[str, Any]],
    previous_handles: Sequence[Mapping[str, Any]],
    current_templates: Sequence[Mapping[str, Any]] | None = None,
    current_handles: Sequence[Mapping[str, Any]] | None = None,
) -> str:
    """认证旧调用 pin 与旧目录聚合后分类当前演进。

    参数：前两项是同一调用 UUID 的旧/新节点；随后是旧应用图目录投影；可选
    当前目录聚合供公共候选边界独立认证。返回：``exact``、``additive`` 或
    ``breaking``；缺失、篡改、混代均关闭为 ``breaking``。异常：无。
    """

    try:
        previous = previous_node["meta_data"]["unilab"]["composite"]
        current = current_node["meta_data"]["unilab"]["composite"]
        previous_projection = previous["contract_compatibility"]
        current_projection = current["contract_compatibility"]
        if not isinstance(previous, Mapping) or not isinstance(current, Mapping):
            return "breaking"
        if not isinstance(previous_projection, Mapping) or not isinstance(
            current_projection,
            Mapping,
        ):
            return "breaking"
        if not _pin_matches_projection(previous, previous_projection):
            return "breaking"
        if not _pin_matches_projection(current, current_projection):
            return "breaking"
        template_uuid = str(previous_projection["template_uuid"])
        if previous_node.get("workflow_node_template_uuid") != template_uuid:
            return "breaking"
        if current_node.get("workflow_node_template_uuid") != current_projection.get(
            "template_uuid"
        ):
            return "breaking"
        templates = [item for item in previous_templates if item.get("uuid") == template_uuid]
        handles = [
            item
            for item in previous_handles
            if item.get("workflow_node_template_uuid") == template_uuid
        ]
        if len(templates) != 1:
            return "breaking"
        canonical = published_workflow_compatibility_projection(templates[0], handles)
        extension = _schema_object(templates[0].get("schema"))[
            "x-unilabos-workflow-contract"
        ]
        if canonical != _plain(previous_projection):
            return "breaking"
        if (
            previous.get("child_workflow_revision")
            != extension.get("workflow_revision")
            or previous.get("child_applied_source_hash")
            != extension.get("applied_source_hash")
        ):
            return "breaking"
        if (current_templates is None) != (current_handles is None):
            return "breaking"
        if current_templates is not None and current_handles is not None:
            current_template_uuid = str(current_projection["template_uuid"])
            current_matches = [
                item
                for item in current_templates
                if item.get("uuid") == current_template_uuid
            ]
            current_owned_handles = [
                item
                for item in current_handles
                if item.get("workflow_node_template_uuid") == current_template_uuid
            ]
            if len(current_matches) != 1:
                return "breaking"
            authenticated_current = published_workflow_compatibility_projection(
                current_matches[0],
                current_owned_handles,
            )
            current_extension = _schema_object(current_matches[0].get("schema"))[
                "x-unilabos-workflow-contract"
            ]
            if authenticated_current != _plain(current_projection):
                return "breaking"
            if (
                current.get("child_workflow_revision")
                != current_extension.get("workflow_revision")
                or current.get("child_applied_source_hash")
                != current_extension.get("applied_source_hash")
            ):
                return "breaking"
        return classify_published_workflow_compatibility_projections(
            previous_projection,
            current_projection,
        )
    except (KeyError, TypeError, ValueError):
        return "breaking"


def _validate_provenance(template: Mapping[str, Any]) -> None:
    """认证模板来源证据与可调用类身份。

    参数：``template`` 是已发布工作流节点模板。返回：无。异常：所有者标志、
    package 来源证据或可调用类身份不自洽时抛出 ``ValueError``。
    """

    meta_data = template.get("meta_data")
    unilab = meta_data.get("unilab") if isinstance(meta_data, Mapping) else None
    source = unilab.get("workflow_source") if isinstance(unilab, Mapping) else None
    if (
        not isinstance(unilab, Mapping)
        or unilab.get("framework_owner_only") is not True
        or not isinstance(source, Mapping)
        or source.get("kind") != "package"
        or not _dotted(source.get("definition_fqid"))
        or not _dotted(source.get("module"))
        or not isinstance(source.get("symbol"), str)
        or not source["symbol"].isidentifier()
        or not _digest(source.get("package_catalog_digest"))
        or not _digest(source.get("definition_content_hash"))
        or template.get("class") != f"{source['module']}:{source['symbol']}"
    ):
        raise ValueError("已发布工作流来源证据无效")


def _contract_envelopes(
    schema: Mapping[str, Any],
    input_order: Sequence[str],
    output_order: Sequence[str],
) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    """认证封闭输入/输出 Schema 并返回两个信封。

    参数：``schema`` 是节点合同，两个顺序固定输入/输出字段。返回：goal 与 result
    封闭 Schema。异常：信封类型错误时抛出 ``TypeError``；字段集、必填集或
    顺序不一致时抛出 ``ValueError``。
    """

    properties = schema.get("properties")
    if not isinstance(properties, Mapping) or set(properties) != {"goal", "result"}:
        raise ValueError("已发布工作流 Schema 信封无效")
    goal = properties["goal"]
    result = properties["result"]
    for envelope, order, require_all in (
        (goal, input_order, False),
        (result, output_order, True),
    ):
        if not isinstance(envelope, Mapping):
            raise TypeError("已发布工作流 Schema 信封无效")
        values = envelope.get("properties")
        required = envelope.get("required")
        if (
            envelope.get("type") != "object"
            or envelope.get("additionalProperties") is not False
            or not isinstance(values, Mapping)
            or set(values) != set(order)
            or not isinstance(required, (list, tuple))
            or len(set(required)) != len(required)
            or any(name not in order for name in required)
            or (require_all and list(required) != list(order))
        ):
            raise ValueError("已发布工作流 Schema 信封无效")
    return goal, result


def _handle_index(
    handles: Sequence[Mapping[str, Any]],
    template_uuid: str,
) -> dict[tuple[str, str], Mapping[str, Any]]:
    """认证连接点父身份并按方向与业务键索引。

    参数：``handles`` 是聚合连接点，``template_uuid`` 是唯一父模板身份。返回：
    按 ``(方向, 业务键)`` 索引的连接点。异常：父身份、方向、业务键或唯一性
    无效时抛出 ``ValueError``。
    """

    indexed: dict[tuple[str, str], Mapping[str, Any]] = {}
    for handle in handles:
        if (
            not isinstance(handle, Mapping)
            or handle.get("workflow_node_template_uuid") != template_uuid
            or handle.get("io_type") not in {"target", "source"}
            or not isinstance(handle.get("handle_key"), str)
        ):
            raise ValueError("已发布工作流连接点身份无效")
        _uuid(handle.get("uuid"))
        key = (str(handle["io_type"]), str(handle["handle_key"]))
        if key in indexed:
            raise ValueError("已发布工作流连接点身份重复")
        indexed[key] = handle
    return indexed


def _validate_value_handle(
    handle: Mapping[str, Any],
    *,
    schema: Mapping[str, Any],
    required: bool,
    io_type: str,
) -> Mapping[str, Any]:
    """认证一个业务值连接点并返回框架元数据。

    参数：``handle`` 是业务连接点，``schema``/``required``/``io_type`` 是合同
    期望。返回：认证后的 ``meta_data.unilab`` 映射。异常：方向、类型、必填性、
    数据键或 Schema 不一致时抛出 ``ValueError``。
    """

    name = str(handle.get("handle_key"))
    meta_data = handle.get("meta_data")
    unilab = meta_data.get("unilab") if isinstance(meta_data, Mapping) else None
    slot = resource_slot_schema(schema)
    allowlist = (
        _plain(slot.get("allowed_resource_template_uuids"))
        if slot is not None
        else None
    )
    if (
        not isinstance(unilab, Mapping)
        or unilab.get("structural_role") is not None
        or handle.get("data_key") != name
        or handle.get("data_source") != ("goal" if io_type == "target" else "result")
        or handle.get("type") != workflow_handle_type(schema)
        or handle.get("required") is not required
        or _plain(unilab.get("value_schema")) != _plain(schema)
        or unilab.get("editor_control")
        != ("material_port" if slot is not None else "variable_selector")
        or _plain(unilab.get("allowed_resource_template_uuids")) != allowlist
        or not isinstance(unilab.get("implicit_passthrough"), bool)
        or (io_type == "target" and unilab.get("implicit_passthrough") is not False)
    ):
        raise ValueError("已发布工作流业务连接点无效")
    return unilab


def _validate_ready_handle(handle: Mapping[str, Any], io_type: str) -> None:
    """认证结构性 ready 连接点（Handle）。

    参数：``handle`` 是连接点，``io_type`` 是期望方向。返回：无。异常：方向或
    后端（Backend）结构控制合同不一致时抛出 ``ValueError``。
    """

    if (
        handle.get("io_type") != io_type
        or handle.get("data_key") is not None
        or handle.get("data_source") is not None
        or handle.get("type") != "default"
        or handle.get("required") is not False
        or _plain(handle.get("meta_data")) != {}
    ):
        raise ValueError("已发布工作流 ready 连接点无效")


def _pin_matches_projection(pin: Mapping[str, Any], projection: Mapping[str, Any]) -> bool:
    """判断实现 pin 与兼容性投影的公共字段是否自洽。

    参数：``pin`` 是调用冻结实现身份，``projection`` 是认证兼容性投影。返回：
    所有公共字段一致时为 ``True``。异常：无；缺字段视为不匹配。
    """

    return all(
        pin.get(key) == projection.get(projection_key)
        for key, projection_key in (
            ("child_workflow_uuid", "workflow_uuid"),
            ("contract_digest", "digest"),
            ("composition_allow_transparent", "mode"),
        )
    )


def _contract_digest(
    *,
    inputs: Sequence[Mapping[str, Any]],
    outputs: Sequence[Mapping[str, Any]],
    mode: bool,
) -> str:
    """从兼容性描述符重算 C1 v1 合同摘要。

    参数：``inputs``/``outputs`` 是认证边界描述符，``mode`` 是组合模式。返回：
    规范 SHA-256 摘要。异常：描述符字段缺失或含不可编码值时抛出相应异常。
    """

    input_descriptors = []
    for item in inputs:
        descriptor = {
            "name": item["name"],
            "schema": _plain(item["schema"]),
            "required": item["required"],
        }
        if item.get("has_default") is True:
            descriptor["default"] = _plain(item["default"])
        input_descriptors.append(descriptor)
    output_descriptors = [
        {
            "name": item["name"],
            "schema": _plain(item["schema"]),
            "implicit": item["implicit"],
        }
        for item in outputs
    ]
    payload = {
        "version": 1,
        "composition_allow_transparent": mode,
        "inputs": input_descriptors,
        "outputs": output_descriptors,
    }
    return "sha256:" + hashlib.sha256(rfc8785.dumps(payload)).hexdigest()


def _schema_object(value: Any) -> dict[str, Any]:
    """把冻结映射或持久 JSON 文本恢复为独立 Schema 对象。

    参数：``value`` 是映射或持久 JSON 文本。返回：独立 Schema 字典。异常：
    JSON 无效或解码结果不是对象时抛出 ``ValueError``/``JSONDecodeError``。
    """

    if isinstance(value, Mapping):
        return _plain(value)
    if isinstance(value, str):
        decoded = json.loads(value)
        if isinstance(decoded, dict):
            return decoded
    raise ValueError("已发布工作流 Schema 无效")


def _uuid(value: Any) -> str:
    """返回规范 UUID。

    参数：``value`` 是待校验身份。返回：规范 UUID 字符串。异常：非法或非规范
    表示时抛出 ``ValueError``。
    """

    identity = validate_uuid(value)
    if identity != value:
        raise ValueError("已发布工作流 UUID 非规范")
    return identity


def _digest(value: Any) -> bool:
    """判断值是否为规范小写 SHA-256 wire 字符串。

    参数：``value`` 是待检查值。返回：格式规范时为 ``True``。异常：无。
    """

    return isinstance(value, str) and _SHA256.fullmatch(value) is not None


def _dotted(value: Any) -> bool:
    """判断值是否为非相对点分 Python 身份。

    参数：``value`` 是待检查值。返回：每段均为标识符且非相对时为 ``True``。
    异常：无；非字符串返回 ``False``。
    """

    return isinstance(value, str) and not value.startswith(".") and all(
        part.isidentifier() for part in value.split(".")
    )


def _order(value: Any) -> list[str]:
    """认证无重复非空字符串顺序并返回独立数组。

    参数：``value`` 是输入或输出顺序候选。返回：独立字符串列表。异常：容器、
    元素或唯一性无效时抛出 ``ValueError``。
    """

    if (
        not isinstance(value, (list, tuple))
        or any(not isinstance(item, str) or not item for item in value)
        or len(set(value)) != len(value)
    ):
        raise ValueError("已发布工作流合同顺序无效")
    return list(value)


def _plain(value: Any) -> Any:
    """递归复制冻结 JSON 容器。

    参数：``value`` 是 JSON 兼容值。返回：容器递归分离后的等价值。异常：无。
    """

    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


__all__ = [
    "classify_pinned_published_workflow_invocation",
    "classify_published_workflow_compatibility_projections",
    "published_workflow_compatibility_projection",
    "published_workflow_projection_is_canonical",
]
