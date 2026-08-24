"""已发布工作流合同兼容性与陈旧 pin 校验。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Literal

from unilabos.server.workflow.json_codec import strict_json_equal
from unilabos.server.workflow.models import validate_uuid

PublishedWorkflowCompatibility = Literal["exact", "additive", "breaking"]


def published_workflow_compatibility_projection(
    template: Mapping[str, Any],
    handles: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """提取可随组合调用冻结的最小兼容性投影。"""

    if not isinstance(template, Mapping) or not isinstance(handles, Sequence):
        raise TypeError("已发布工作流合同聚合无效")
    template_uuid = validate_uuid(template.get("uuid"))
    schema = template.get("schema")
    if not isinstance(schema, Mapping):
        raise ValueError("已发布工作流 Schema 无效")
    extension = schema.get("x-unilabos-workflow-contract")
    if not isinstance(extension, Mapping):
        raise ValueError("已发布工作流合同扩展无效")
    workflow_uuid = validate_uuid(extension.get("workflow_uuid"))
    input_order = _order(extension.get("input_order"))
    output_order = _order(extension.get("output_order"))
    if (
        extension.get("version") != 1
        or extension.get("compatibility_version") != 1
        or not isinstance(extension.get("composition_allow_transparent"), bool)
        or template.get("type") != "workflow"
        or template.get("node_type") != "workflow"
        or template.get("name") != f"workflow:{workflow_uuid}"
    ):
        raise ValueError("已发布工作流合同身份无效")
    goal = _envelope(schema, "goal", input_order)
    result = _envelope(schema, "result", output_order)
    defaults = template.get("goal_default")
    if not isinstance(defaults, Mapping) or set(defaults) - set(input_order):
        raise ValueError("已发布工作流默认值无效")
    by_key: dict[tuple[str, str], Mapping[str, Any]] = {}
    for handle in handles:
        if handle.get("workflow_node_template_uuid") != template_uuid:
            continue
        key = (str(handle.get("io_type") or ""), str(handle.get("handle_key") or ""))
        if key in by_key:
            raise ValueError("已发布工作流 Handle 重复")
        by_key[key] = handle
    expected = {
        *(("target", name) for name in input_order),
        *(("source", name) for name in output_order),
        ("target", "ready"),
        ("source", "ready"),
    }
    if set(by_key) != expected:
        raise ValueError("已发布工作流 Handle 集合不完整")
    inputs: list[dict[str, Any]] = []
    required = set(goal.get("required", []))
    for name in input_order:
        item: dict[str, Any] = {
            "name": name,
            "schema": _without_template_allowlist(
                _plain(goal["properties"][name])
            ),
            "required": name in required,
            "has_default": name in defaults,
            "handle_uuid": validate_uuid(by_key[("target", name)].get("uuid")),
        }
        if name in defaults:
            item["default"] = _plain(defaults[name])
        inputs.append(item)
    outputs = [
        {
            "name": name,
            "schema": _without_template_allowlist(
                _plain(result["properties"][name])
            ),
            "implicit": bool(
                _unilab(by_key[("source", name)]).get("implicit_passthrough", False)
            ),
            "handle_uuid": validate_uuid(by_key[("source", name)].get("uuid")),
        }
        for name in output_order
    ]
    return {
        "template_uuid": template_uuid,
        "workflow_uuid": workflow_uuid,
        "mode": bool(extension["composition_allow_transparent"]),
        "digest": str(extension.get("contract_digest") or ""),
        "inputs": inputs,
        "outputs": outputs,
    }


def classify_published_workflow_compatibility_projections(
    previous: Mapping[str, Any],
    current: Mapping[str, Any],
) -> PublishedWorkflowCompatibility:
    """区分完全一致、末尾可选输入扩展和破坏性演进。"""

    try:
        if (
            validate_uuid(previous.get("workflow_uuid"))
            != validate_uuid(current.get("workflow_uuid"))
            or not isinstance(previous.get("mode"), bool)
            or not isinstance(current.get("mode"), bool)
            or previous.get("mode") != current.get("mode")
            or not isinstance(previous.get("inputs"), list)
            or not isinstance(current.get("inputs"), list)
            or not isinstance(previous.get("outputs"), list)
            or not isinstance(current.get("outputs"), list)
        ):
            return "breaking"
    except (TypeError, ValueError):
        return "breaking"
    previous_inputs = previous["inputs"]
    current_inputs = current["inputs"]
    if not strict_json_equal(
        _without_template_allowlist(previous["outputs"]),
        _without_template_allowlist(current["outputs"]),
    ):
        return "breaking"
    if len(current_inputs) < len(previous_inputs):
        return "breaking"
    for old, new in zip(previous_inputs, current_inputs):
        if not _same_boundary(old, new, include_handle=False):
            return "breaking"
    additions = current_inputs[len(previous_inputs) :]
    if any(
        item.get("required") is not False or item.get("has_default") is not True
        for item in additions
        if isinstance(item, Mapping)
    ) or any(not isinstance(item, Mapping) for item in additions):
        return "breaking"
    if not additions and previous.get("digest") == current.get("digest"):
        return "exact"
    if not additions and all(
        _same_boundary(old, new, include_handle=False)
        for old, new in zip(previous_inputs, current_inputs)
    ):
        return "exact"
    return "additive"


def classify_pinned_published_workflow_invocation(
    previous_node: Mapping[str, Any],
    current_template: Mapping[str, Any],
    current_handles: Sequence[Mapping[str, Any]],
) -> PublishedWorkflowCompatibility:
    """把节点冻结的上一代投影与当前已认证目录合同比较。"""

    composite = _unilab(previous_node).get("composite")
    if not isinstance(composite, Mapping):
        return "breaking"
    previous = composite.get("contract_compatibility")
    if not isinstance(previous, Mapping):
        return "breaking"
    try:
        current = published_workflow_compatibility_projection(
            current_template,
            current_handles,
        )
    except (KeyError, TypeError, ValueError):
        return "breaking"
    return classify_published_workflow_compatibility_projections(previous, current)


def published_workflow_projection_is_canonical(
    projection: Mapping[str, Any],
    template: Mapping[str, Any],
    handles: Sequence[Mapping[str, Any]],
) -> bool:
    try:
        return strict_json_equal(
            _without_template_allowlist(_plain(projection)),
            _without_template_allowlist(
                published_workflow_compatibility_projection(template, handles)
            ),
        )
    except (KeyError, TypeError, ValueError):
        return False


def _same_boundary(
    previous: Mapping[str, Any],
    current: Mapping[str, Any],
    *,
    include_handle: bool,
) -> bool:
    ignored = set() if include_handle else {"handle_uuid"}
    left = {key: _plain(value) for key, value in previous.items() if key not in ignored}
    right = {key: _plain(value) for key, value in current.items() if key not in ignored}
    return strict_json_equal(_without_template_allowlist(left), _without_template_allowlist(right))


def _without_template_allowlist(value: Any) -> Any:
    """移除仅供前端提示的模板 UUID allowlist 后比较业务合同。"""

    if isinstance(value, Mapping):
        is_resource_slot = value.get("$slot") == "ResourceSlot"
        return {
            str(key): _without_template_allowlist(item)
            for key, item in value.items()
            if not (is_resource_slot and key == "allowed_resource_template_uuids")
        }
    if isinstance(value, (list, tuple)):
        return [_without_template_allowlist(item) for item in value]
    return value


def _envelope(
    schema: Mapping[str, Any],
    key: str,
    order: Sequence[str],
) -> Mapping[str, Any]:
    value = schema.get("properties", {}).get(key)
    if not isinstance(value, Mapping) or value.get("type") != "object":
        raise ValueError("已发布工作流合同 envelope 无效")
    properties = value.get("properties")
    if not isinstance(properties, Mapping) or set(properties) != set(order):
        raise ValueError("已发布工作流合同属性无效")
    return value


def _order(value: Any) -> list[str]:
    if (
        not isinstance(value, Sequence)
        or isinstance(value, (str, bytes))
        or any(
        not isinstance(item, str) or not item for item in value
        )
        or len(set(value)) != len(value)
    ):
        raise ValueError("已发布工作流合同顺序无效")
    return list(value)


def _unilab(entity: Mapping[str, Any]) -> Mapping[str, Any]:
    meta_data = entity.get("meta_data")
    unilab = meta_data.get("unilab") if isinstance(meta_data, Mapping) else None
    return unilab if isinstance(unilab, Mapping) else {}


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


__all__ = [
    "PublishedWorkflowCompatibility",
    "classify_pinned_published_workflow_invocation",
    "classify_published_workflow_compatibility_projections",
    "published_workflow_compatibility_projection",
    "published_workflow_projection_is_canonical",
]
