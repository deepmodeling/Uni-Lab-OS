"""组合工作流依赖的不可变模板目录窄接口。"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from unilabos.server.workflow.json_codec import encode_json
from unilabos.server.workflow.models import validate_uuid


class AuthoringCatalogError(ValueError):
    """模板目录不完整或不一致。"""


@dataclass(frozen=True, slots=True)
class AuthoringCatalogAction:
    template: Mapping[str, Any]
    handles: tuple[Mapping[str, Any], ...]

    def detached_template(self) -> dict[str, Any]:
        return _detach(self.template)

    def detached_handles(self) -> list[dict[str, Any]]:
        return [_detach(handle) for handle in self.handles]


@dataclass(frozen=True, slots=True)
class AuthoringCatalogSnapshot:
    fingerprint: str
    actions: tuple[AuthoringCatalogAction, ...]
    _by_business_key: Mapping[tuple[str, str], AuthoringCatalogAction]
    _by_template_uuid: Mapping[str, AuthoringCatalogAction]

    @classmethod
    def from_entities(
        cls,
        node_templates: Sequence[Mapping[str, Any]],
        handle_templates: Sequence[Mapping[str, Any]],
    ) -> "AuthoringCatalogSnapshot":
        nodes = [_json_mapping(item, "节点模板") for item in node_templates]
        handles = [_json_mapping(item, "Handle 模板") for item in handle_templates]
        node_ids = [_required_uuid(item, "uuid") for item in nodes]
        handle_ids = [_required_uuid(item, "uuid") for item in handles]
        if len(set(node_ids)) != len(node_ids) or len(set(handle_ids)) != len(
            handle_ids
        ):
            raise AuthoringCatalogError("模板目录包含重复 UUID")

        handles_by_parent: dict[str, list[dict[str, Any]]] = {
            identity: [] for identity in node_ids
        }
        for handle in handles:
            parent = _required_uuid(handle, "workflow_node_template_uuid")
            if parent not in handles_by_parent:
                raise AuthoringCatalogError("Handle 引用了未知节点模板")
            handles_by_parent[parent].append(handle)

        actions: list[AuthoringCatalogAction] = []
        by_business_key: dict[tuple[str, str], AuthoringCatalogAction] = {}
        by_template_uuid: dict[str, AuthoringCatalogAction] = {}
        for node in sorted(nodes, key=lambda item: str(item["uuid"])):
            class_identity = node.get("class")
            action_name = node.get("name")
            if not isinstance(class_identity, str) or not class_identity.strip():
                raise AuthoringCatalogError("节点模板缺少类身份")
            if not isinstance(action_name, str) or not action_name.strip():
                raise AuthoringCatalogError("节点模板缺少业务名")
            node_uuid = str(node["uuid"])
            action = AuthoringCatalogAction(
                template=_freeze(node),
                handles=tuple(
                    _freeze(handle)
                    for handle in sorted(
                        handles_by_parent[node_uuid],
                        key=lambda item: (
                            str(item.get("io_type") or ""),
                            str(item.get("handle_key") or ""),
                            str(item["uuid"]),
                        ),
                    )
                ),
            )
            business_key = (class_identity, action_name)
            if business_key in by_business_key:
                raise AuthoringCatalogError("模板目录业务身份重复")
            by_business_key[business_key] = action
            by_template_uuid[node_uuid] = action
            actions.append(action)

        payload = {
            "node_templates": sorted(
                (_semantic_entity(item) for item in nodes),
                key=lambda item: str(item["uuid"]),
            ),
            "handle_templates": sorted(
                (_semantic_entity(item) for item in handles),
                key=lambda item: str(item["uuid"]),
            ),
        }
        fingerprint = "sha256:" + hashlib.sha256(
            encode_json(payload, sort_keys=True)
        ).hexdigest()
        return cls(
            fingerprint=fingerprint,
            actions=tuple(actions),
            _by_business_key=MappingProxyType(by_business_key),
            _by_template_uuid=MappingProxyType(by_template_uuid),
        )

    def require_action(
        self,
        class_identity: str,
        action_name: str,
    ) -> AuthoringCatalogAction:
        try:
            return self._by_business_key[(class_identity, action_name)]
        except (KeyError, TypeError):
            raise AuthoringCatalogError("模板目录缺少动作身份") from None

    def require_template(self, template_uuid: str) -> AuthoringCatalogAction:
        try:
            return self._by_template_uuid[validate_uuid(template_uuid)]
        except (KeyError, TypeError, ValueError):
            raise AuthoringCatalogError("模板目录缺少模板 UUID") from None


def _required_uuid(entity: Mapping[str, Any], field: str) -> str:
    try:
        value = entity[field]
        identity = validate_uuid(value)
    except (KeyError, TypeError, ValueError):
        raise AuthoringCatalogError(f"目录字段 {field} 不是有效 UUID") from None
    if value != identity:
        raise AuthoringCatalogError(f"目录字段 {field} 不是 canonical UUID")
    return identity


def _json_mapping(entity: Mapping[str, Any], label: str) -> dict[str, Any]:
    if not isinstance(entity, Mapping):
        raise AuthoringCatalogError(f"{label}必须是对象")
    try:
        return _detach(_freeze(dict(entity)))
    except (TypeError, ValueError):
        raise AuthoringCatalogError(f"{label}必须是 JSON 对象") from None


def _semantic_entity(entity: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in entity.items()
        if key not in {"create_time", "update_time", "deleted_at"}
    }


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(child) for key, child in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(child) for child in value)
    if value is None or type(value) in {bool, int, float, str}:
        return value
    raise TypeError(f"{type(value).__name__} 不是 JSON 值")


def _detach(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _detach(child) for key, child in value.items()}
    if isinstance(value, tuple):
        return [_detach(child) for child in value]
    return value


__all__ = [
    "AuthoringCatalogAction",
    "AuthoringCatalogError",
    "AuthoringCatalogSnapshot",
]
