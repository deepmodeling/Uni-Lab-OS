"""V1.0 资源合同（``/resource-templates``、``/materials``、``/sites``、``/material-states``）。

存储与写入口仍是 :class:`MaterialsService`（materials.db 唯一权威）：本模块只把 dev 的
模板 / 物料聚合 / 库位投影成 V1 的 Backend DTO（``uuid`` / ``parent_uuid`` /
``resource_template_uuid``、ISO 毫秒时间），并把 V1 的直写请求翻译成 dev 的幂等变更
（每次调用生成新的 ``command_uuid``）。V1 DTO 之外附带的 dev 字段（``definition_hash``、
``version`` 等）只是超集，V1 客户端忽略即可。
"""

from __future__ import annotations

import json
from typing import Any, Iterable, Mapping, Optional, Sequence
from uuid import NAMESPACE_URL, uuid4, uuid5

from unilabos.protocol.materials import (
    ACTOR_HUMAN,
    InventoryMutation,
    MaterialDataWrite,
    MaterialDelete,
    MaterialIdentityWrite,
    MaterialMove,
    MaterialNodeCreate,
    MaterialPatch,
    MaterialPosition,
    MaterialTreeCreate,
    ResourceTemplateRead,
    ResourceTemplateWrite,
)
from unilabos.server.api.v1_fields import iso_to_ms, ms_to_iso
from unilabos.server.services.materials.store import MaterialsRepository
from unilabos.server.services.materials.core import (
    MaterialConflictError,
    MaterialNoChangeError,
    MaterialNotFoundError,
    MaterialsService,
    MaterialValidationError,
)

#: V1 库位身份批量解析上限（``backend_contract.MAX_SITE_IDENTITY_RESOLUTION_IDS``）。
MAX_SITE_IDENTITY_RESOLUTION_IDS = 500
_HANDLE_NAMESPACE = uuid5(NAMESPACE_URL, "unilabos://resource-template-handles")
_NIL_UUID = "00000000-0000-0000-0000-000000000000"


def new_mutation(operation: str, *, actor_type: str = ACTOR_HUMAN, actor_uuid: Optional[str] = None,
                 payload: Optional[Mapping[str, Any]] = None) -> InventoryMutation:
    """V1 直写请求没有幂等键：每次调用生成新的 command_uuid（与 V1 语义一致）。"""

    return InventoryMutation(
        command_uuid=str(uuid4()),
        effect_key=operation,
        operation=operation,
        actor_type=actor_type,
        actor_uuid=actor_uuid,
        payload=dict(payload or {}),
    )


def optional_uuid(value: Any) -> Optional[str]:
    """未传、空串与全零 UUID 一律视为未设置（V1 ``_optional_uuid``）。"""

    text = str(value or "").strip()
    return None if text in ("", _NIL_UUID) else text


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


# ── 资源模板 ──────────────────────────────────────────────────────


def _schema_properties(definition: Mapping[str, Any], key: str) -> dict[str, Any]:
    schema = _mapping(definition.get("init_param_schema"))
    return _mapping(_mapping(schema.get(key)).get("properties"))


def handle_to_v1(template: ResourceTemplateRead, handle: Any) -> dict[str, Any]:
    values = handle.model_dump(mode="json") if hasattr(handle, "model_dump") else dict(handle)
    key = str(values.get("key") or "")
    result: dict[str, Any] = {
        "uuid": str(uuid5(_HANDLE_NAMESPACE, f"{template.template_uuid}|{key}")),
        "create_time": ms_to_iso(template.created_at_ms),
        "update_time": ms_to_iso(template.updated_at_ms),
        "description": values.get("description") or None,
        "meta_data": values.get("meta_data") or {},
        "resource_template_uuid": template.template_uuid,
        "name": key,
        "display_name": values.get("label") or key,
        "type": values.get("data_type") or "object",
        "io_type": values.get("io_type"),
    }
    if values.get("data_source"):
        result["source"] = values["data_source"]
    if values.get("data_key"):
        result["key"] = values["data_key"]
    if values.get("side"):
        result["side"] = values["side"]
    return result


def template_summary(template: ResourceTemplateRead, *, include_tags: bool = True) -> dict[str, Any]:
    definition = template.definition or {}
    result: dict[str, Any] = {
        "uuid": template.template_uuid,
        "name": template.name,
        "display_name": template.display_name or template.name,
        "resource_type": template.resource_type,
    }
    if include_tags:
        result["tags"] = list(template.category)
    if definition.get("icon") is not None:
        result["icon"] = definition.get("icon")
    return result


def template_to_v1(template: ResourceTemplateRead, *, include_definition: bool = True) -> dict[str, Any]:
    """dev 模板 → V1 ``ResourceTemplate`` 详情（附 dev 版本字段）。"""

    definition = template.definition or {}
    meta_data = _mapping(definition.get("meta_data") or definition.get("metadata"))
    if "capacity" in definition and "capacity" not in meta_data:
        meta_data["capacity"] = definition["capacity"]
    result: dict[str, Any] = {
        "uuid": template.template_uuid,
        "create_time": ms_to_iso(template.created_at_ms),
        "update_time": ms_to_iso(template.updated_at_ms),
        "description": definition.get("description"),
        "meta_data": meta_data,
        "name": template.name,
        "display_name": template.display_name or template.name,
        "resource_type": template.resource_type,
        "header": None,
        "footer": None,
        "icon": definition.get("icon"),
        "module": template.module_name,
        "language": definition.get("language"),
        "cover": definition.get("cover"),
        "manufacturer_uuid": None,
        "model": _mapping(definition.get("model")),
        "tags": list(template.category),
        "data_schema": _schema_properties(definition, "data"),
        "config_schema": _schema_properties(definition, "config"),
        "pose": _mapping(definition.get("pose")),
        "config_info": list(definition.get("config_info") or []),
        "available_sites": list(template.available_sites),
        "scene": list(definition.get("scene") or []),
        "device_params": _mapping(definition.get("device_params")),
        "ui_overlay": {},
        "handles": [handle_to_v1(template, item) for item in template.handles],
        # dev 超集：模板版本与内容哈希（宿主同步 / 变更判定用）
        "class_name": template.class_name,
        "template_version": template.template_version,
        "category": list(template.category),
        "definition_hash": template.definition_hash,
        "status": template.status,
        "version": template.version,
    }
    if include_definition:
        result["definition"] = definition
    return result


def v1_template_to_dev(value: Mapping[str, Any]) -> ResourceTemplateRead:
    """V1 模板详情（含 dev 超集字段）→ dev ``ResourceTemplateRead``（HTTP 客户端用）。"""

    handles = []
    for item in value.get("handles") or []:
        handles.append({
            "key": item.get("name"),
            "label": item.get("display_name") or item.get("name"),
            "io_type": item.get("io_type"),
            "data_type": item.get("type") or "object",
            "side": item.get("side"),
            "data_key": item.get("key"),
            "data_source": item.get("source"),
            "description": item.get("description") or "",
            "meta_data": item.get("meta_data") or {},
        })
    return ResourceTemplateRead.model_validate({
        "template_uuid": value["uuid"],
        "name": value["name"],
        "display_name": value.get("display_name"),
        "resource_type": value.get("resource_type") or "resource",
        "class_name": value.get("class_name"),
        "module_name": value.get("module"),
        "template_version": value.get("template_version") or "1",
        "category": value.get("category") if value.get("category") is not None else value.get("tags") or [],
        "available_sites": value.get("available_sites") or [],
        "handles": handles,
        "definition": value.get("definition") or {},
        "status": value.get("status") or "active",
        "definition_hash": value.get("definition_hash") or "unknown",
        "created_at_ms": iso_to_ms(value.get("create_time")) or 0,
        "updated_at_ms": iso_to_ms(value.get("update_time")) or 0,
        "version": value.get("version") or 1,
    })


# ── 物料 / 库位投影 ───────────────────────────────────────────────


def _pose_number(pose: Mapping[str, Any], *keys: str) -> float:
    for key in keys:
        value = pose.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
    return 0.0


def site_to_v1(site: Any) -> dict[str, Any]:
    pose = _mapping(site.pose)
    position = _mapping(pose.get("position"))
    size = _mapping(pose.get("size"))
    return {
        "uuid": site.site_uuid,
        "create_time": ms_to_iso(site.created_at_ms),
        "update_time": ms_to_iso(site.updated_at_ms),
        "description": site.description or None,
        "meta_data": dict(site.meta_data_json if hasattr(site, "meta_data_json") else site.meta_data or {}),
        "material_uuid": site.owner_material_uuid,
        "name": site.label,
        "sort_order": int(site.ordinal),
        "allowed_resource_template_uuids": list((site.extra_json if hasattr(site, "extra_json") else site.extra or {}).get(
            "allowed_resource_template_uuids", []
        )),
        "occupied_material_uuid": site.occupied_material_uuid,
        "position_x": _pose_number(position, "x") or _pose_number(pose, "x", "position_x"),
        "position_y": _pose_number(position, "y") or _pose_number(pose, "y", "position_y"),
        "position_z": _pose_number(position, "z") or _pose_number(pose, "z", "position_z"),
        "depth": _pose_number(size, "depth", "z") or _pose_number(pose, "depth"),
        "length": _pose_number(size, "length", "y") or _pose_number(pose, "length"),
        "width": _pose_number(size, "width", "x") or _pose_number(pose, "width"),
        # dev 超集
        "site_index": site.site_index,
        "template_name": site.template_name,
        "allowed_resource_categories": list(site.allowed_resource_categories),
        "visible": bool(site.visible),
        "version": site.version,
    }


def position_to_v1(material_uuid: str, position: MaterialPosition, *, updated_at_ms: int) -> dict[str, Any]:
    stamp = ms_to_iso(updated_at_ms)
    return {
        "uuid": material_uuid,
        "create_time": stamp,
        "update_time": stamp,
        "description": None,
        "meta_data": dict(position.extra),
        "material_uuid": material_uuid,
        "position_x": float(position.position_x or 0),
        "position_y": float(position.position_y or 0),
        "position_z": float(position.position_z or 0),
        "depth": float(position.size_depth),
        "length": float(position.size_height),
        "width": float(position.size_width),
        "scale_x": float(position.scale_x or 1),
        "scale_y": float(position.scale_y or 1),
        "scale_z": float(position.scale_z or 1),
        "rotation_x": float(position.rotation_x),
        "rotation_y": float(position.rotation_y),
        "rotation_z": float(position.rotation_z),
    }


class ResourceContract:
    """V1 资源合同；所有写入都委托 :class:`MaterialsService` 的幂等变更。"""

    def __init__(self, materials: MaterialsService) -> None:
        self.materials = materials

    def material_record(self, material_uuid: str, *, include_deleted: bool = False) -> Any:
        """行级读取（不抛异常）；``MaterialsService.get_material`` 返回聚合且不存在时抛 6000。"""

        return MaterialsRepository.get_material(self.materials, material_uuid, include_deleted=include_deleted)

    # -- 资源模板 --------------------------------------------------

    def _template(self, template_uuid: str) -> ResourceTemplateRead:
        try:
            return self.materials.get_template(template_uuid)
        except MaterialNotFoundError:
            raise MaterialNotFoundError("Resource template not found", subject="resource_template") from None

    def sync_resource_templates(self, resources: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        """V1 ``POST /resource-templates``：同步一批注册表资源定义并返回稳定身份回执。"""

        from unilabos.client.materials import LocalMaterialsClient
        from unilabos.resources.adapters.registry_materials import register_resource_definitions

        if not resources:
            raise MaterialValidationError("resources is required")
        names = [str(_mapping(item).get("id") or "").strip() for item in resources]
        if any(not name for name in names) or len(set(names)) != len(names):
            raise MaterialValidationError("resource names are required and must be unique")
        try:
            report = register_resource_definitions(resources, LocalMaterialsClient(self.materials))
        except ValueError as exc:
            raise MaterialValidationError(str(exc)) from None
        return {"templates": [{"uuid": report.template_uuids[name], "name": name} for name in names]}

    def list_resource_templates(
        self, *, page: int, page_size: int, keyword: str = "", resource_type: str = "",
        name: Optional[str] = None, include_definition: bool = False, detail: bool = False,
    ) -> dict[str, Any]:
        page = 1 if page <= 0 else page
        page_size = 20 if page_size <= 0 else min(page_size, 100)
        templates = self.materials.list_templates(name=name, include_definition=include_definition)
        keyword = keyword.strip().lower()
        items = [
            item for item in templates
            if item.resource_type != "framework"
            and (not keyword or keyword in item.name.lower() or keyword in (item.display_name or "").lower())
            and (not resource_type or item.resource_type == resource_type.strip())
        ]
        items.sort(key=lambda item: (item.created_at_ms, item.template_uuid), reverse=True)
        offset = (page - 1) * page_size
        selected = items[offset: offset + page_size]
        projected = [
            template_to_v1(item, include_definition=include_definition) if detail
            else {**template_summary(item), "definition_hash": item.definition_hash, "version": item.version}
            for item in selected
        ]
        return {"items": projected, "has_more": len(items) > offset + page_size, "page": page, "page_size": page_size}

    def get_resource_template(self, template_uuid: str) -> dict[str, Any]:
        return template_to_v1(self._template(template_uuid))

    def update_resource_template(self, template_uuid: str, values: Mapping[str, Any], specified: Iterable[str]) -> dict[str, Any]:
        """V1 ``PUT``：只改展示与定义字段；未给 ``available_sites`` / ``handles`` 时保留原值。"""

        specified = set(specified)
        current = self._template(template_uuid)
        definition = dict(current.definition or {})
        for key in ("description", "icon", "cover", "model", "scene", "device_params", "config_info"):
            if key in specified:
                definition[key] = values.get(key)
        if "init_param_schema" in specified:
            definition["init_param_schema"] = values.get("init_param_schema")
        handles = current.handles
        if "handles" in specified:
            from unilabos.resources.adapters.registry_materials import _handles

            handles = _handles({"handles": values.get("handles") or []})
        write = ResourceTemplateWrite(
            template_uuid=current.template_uuid,
            name=current.name,
            display_name=str(values.get("display_name") or current.display_name or current.name),
            resource_type=str(values.get("registry_type") or current.resource_type) if "registry_type" in specified else current.resource_type,
            class_name=current.class_name,
            module_name=_mapping(values.get("class")).get("module") or current.module_name if "class" in specified else current.module_name,
            template_version=current.template_version,
            category=[str(item) for item in values.get("category") or []] if "category" in specified else current.category,
            available_sites=list(values.get("available_sites") or []) if "available_sites" in specified else current.available_sites,
            handles=handles,
            definition=definition,
        )
        self.materials.put_template(new_mutation("put_template", payload=write.model_dump(mode="json")), write)
        return self.get_resource_template(template_uuid)

    def delete_resource_template(self, template_uuid: str) -> None:
        self._template(template_uuid)
        try:
            self.materials.delete_template(
                new_mutation("delete_template", payload={"template_uuid": template_uuid}), template_uuid
            )
        except MaterialConflictError as exc:
            raise MaterialConflictError(str(exc) or "Resource template is in use", conflict="template_data") from None

    # -- 物料 ------------------------------------------------------

    def _template_meta(self, template_uuid: str) -> Optional[ResourceTemplateRead]:
        try:
            return self.materials.get_template(template_uuid)
        except MaterialNotFoundError:
            return None

    def _capacity(self, config: Mapping[str, Any], template: Optional[ResourceTemplateRead]) -> dict[str, Any]:
        from unilabos.server.services.materials.capacity import capacity_projection

        try:
            capacity = capacity_projection(config, (template.definition if template else {}) or {})
        except ValueError:
            return {}
        if not capacity:
            return {}
        return {"capacity": capacity, "rated_capacity": capacity}

    def material_row(self, aggregate: Any, *, template: Optional[ResourceTemplateRead] = None) -> dict[str, Any]:
        material = aggregate.material
        template = template or self._template_meta(material.template_uuid)
        result: dict[str, Any] = {
            "uuid": material.material_uuid,
            "create_time": ms_to_iso(material.created_at_ms),
            "update_time": ms_to_iso(max(material.updated_at_ms, aggregate.data.updated_at_ms)),
            "description": material.description or None,
            "meta_data": dict(material.meta_data),
            "resource_template_uuid": material.template_uuid,
            "parent_uuid": material.parent_material_uuid,
            "class": material.class_name,
            "type": material.resource_type,
            "barcode": material.barcode,
            "name": material.name,
            "config": dict(material.config),
            "data": dict(aggregate.data.data),
            "revision": material.version,
            # dev 超集
            "resource_id": material.resource_id,
            "display_name": material.display_name,
            "lifecycle_status": material.lifecycle_status,
            "lot_uuid": material.lot_uuid,
            "state_hash": aggregate.state_hash,
        }
        result.update(self._capacity(material.config, template))
        return result

    def material_detail(self, material_uuid: str) -> dict[str, Any]:
        aggregate = self.materials.get_material(material_uuid)
        template = self._template_meta(aggregate.material.template_uuid)
        result = self.material_row(aggregate, template=template)
        position = aggregate.position
        has_position = position.position_x is not None or any(
            (position.size_depth, position.size_width, position.size_height)
        )
        result["relative_position"] = (
            position_to_v1(material_uuid, position, updated_at_ms=aggregate.material.updated_at_ms)
            if has_position else None
        )
        result["sites"] = [site_to_v1(site) for site in aggregate.sites]
        current = self.materials.occupied_site(material_uuid)
        result["current_site"] = site_to_v1(current) if current is not None else None
        if template is not None:
            result["resource_template"] = template_summary(template, include_tags=False)
        return result

    def list_materials(
        self, *, page: int, page_size: int, name: str = "", barcode: str = "",
        resource_template_uuid: Optional[str] = None,
    ) -> dict[str, Any]:
        page = max(page, 1)
        page_size = 20 if page_size <= 0 else min(page_size, 100)
        where = ["deleted_at_ms IS NULL"]
        params: list[Any] = []
        if name:
            where.append("name LIKE ?")
            params.append(f"%{name}%")
        if barcode:
            where.append("barcode = ?")
            params.append(barcode)
        if resource_template_uuid:
            where.append("template_uuid = ?")
            params.append(resource_template_uuid)
        predicate = " AND ".join(where)
        connection = self.materials.connection
        total = int(connection.execute(f"SELECT COUNT(*) FROM material WHERE {predicate}", params).fetchone()[0])
        rows = connection.execute(
            f"SELECT material_uuid FROM material WHERE {predicate} "
            "ORDER BY created_at_ms DESC, material_uuid DESC LIMIT ? OFFSET ?",
            (*params, page_size, (page - 1) * page_size),
        ).fetchall()
        templates: dict[str, Optional[ResourceTemplateRead]] = {}
        items = []
        for row in rows:
            aggregate = self.materials.get_material(row[0])
            template_uuid = aggregate.material.template_uuid
            if template_uuid not in templates:
                templates[template_uuid] = self._template_meta(template_uuid)
            items.append(self.material_row(aggregate, template=templates[template_uuid]))
        return {"items": items, "total": total, "page": page, "page_size": page_size,
                "has_more": page * page_size < total}

    def _resource_id(self, name: str) -> str:
        if MaterialsRepository.get_material_by_resource_id(self.materials, name) is None:
            return name
        return f"{name}-{uuid4().hex[:8]}"

    def create_material(self, values: Mapping[str, Any]) -> dict[str, Any]:
        """V1 ``POST /materials``：从模板实例化一件物料（展开模板库位），可选放入库位 / 挂到父物料。"""

        template_uuid = optional_uuid(values.get("resource_template_uuid"))
        name = str(values.get("name") or "").strip()
        if not template_uuid or not name:
            raise MaterialValidationError("resource_template_uuid and name are required")
        template = self._template_meta(template_uuid)
        if template is None:
            raise MaterialNotFoundError(
                "Resource template associated with the material not found", subject="template"
            )
        parent_uuid = optional_uuid(values.get("parent_uuid"))
        if parent_uuid and self.material_record(parent_uuid) is None:
            raise MaterialNotFoundError(f"parent material not found: {parent_uuid}", subject="parent")
        placement = _mapping(values.get("site_placement"))
        site_uuid = optional_uuid(placement.get("site_uuid")) if placement.get("action") == "place" else None
        if site_uuid:
            site = self.materials.get_site(site_uuid)
            if site is None:
                raise MaterialNotFoundError(f"site not found: {site_uuid}", subject="site")
            if site.occupied_material_uuid is not None:
                raise MaterialConflictError("site is occupied", conflict="site_occupied")
        barcode = str(values.get("barcode") or "")
        if barcode and self.find_by_barcode(barcode):
            raise MaterialConflictError(
                "Material barcode or sibling name conflicts with an existing material", conflict="identity"
            )
        relative = values.get("relative_position")
        position = MaterialPosition()
        if isinstance(relative, Mapping):
            position = self._position_from_v1(relative)
        material_uuid = str(uuid4())
        tree = MaterialTreeCreate(nodes=[MaterialNodeCreate(
            client_ref="material",
            material_uuid=material_uuid,
            identity=MaterialIdentityWrite(
                resource_id=self._resource_id(name),
                name=name,
                display_name=name,
                description=str(values.get("description") or ""),
                resource_type=template.resource_type,
                class_name=template.class_name or template.resource_type or "Resource",
                barcode=barcode,
                template_name=template.name,
                config=_mapping(values.get("config")),
                meta_data=_mapping(values.get("meta_data")),
            ),
            position=position,
        )])
        try:
            self.materials.create_tree(
                new_mutation("create_material_tree", payload=tree.model_dump(mode="json", exclude_none=False)), tree
            )
        except MaterialConflictError as exc:
            if exc.conflict is None:
                raise MaterialConflictError(
                    "Material barcode or sibling name conflicts with an existing material", conflict="identity"
                ) from None
            raise
        try:
            if site_uuid or parent_uuid:
                move = MaterialMove(material_uuid=material_uuid, destination_site_uuid=site_uuid,
                                    parent_material_uuid=None if site_uuid else parent_uuid)
                self.materials.move_material(
                    new_mutation("move_material", payload=move.model_dump(mode="json", exclude_none=False)), move
                )
            reagent = values.get("reagent")
            content: Optional[dict[str, Any]] = None
            if isinstance(reagent, Mapping):
                from unilabos.server.services.materials.contents import ContentContract

                content = ContentContract(self.materials).create_reagent({**reagent, "material_uuid": material_uuid})
        except Exception:
            # 物料与放置 / 装液不是同一事务：后续步骤失败时撤销刚创建的空物料，不留半成品
            delete = MaterialDelete(material_uuid=material_uuid, recursive=True)
            try:
                self.materials.delete_material(
                    new_mutation("delete_material", payload=delete.model_dump(mode="json")), delete
                )
            except Exception:  # noqa: BLE001 - 补偿失败不掩盖原错误
                pass
            raise
        result = self.material_detail(material_uuid)
        aggregate = self.materials.get_material(material_uuid)
        result["children"] = []
        result["site_uuids"] = [site.site_uuid for site in aggregate.sites]
        result["initial_material_uuids"] = []
        if content is not None:
            result["reagent"] = content
        return result

    def find_by_barcode(self, barcode: str) -> Optional[str]:
        row = self.materials.connection.execute(
            "SELECT material_uuid FROM material WHERE barcode=? AND deleted_at_ms IS NULL", (barcode,)
        ).fetchone()
        return row[0] if row else None

    @staticmethod
    def _position_from_v1(relative: Mapping[str, Any]) -> MaterialPosition:
        return MaterialPosition(
            position_x=float(relative.get("position_x") or 0),
            position_y=float(relative.get("position_y") or 0),
            position_z=float(relative.get("position_z") or 0),
            size_depth=float(relative.get("depth") or 0),
            size_width=float(relative.get("width") or 0),
            size_height=float(relative.get("length") or 0),
            scale_x=float(relative.get("scale_x") or 1),
            scale_y=float(relative.get("scale_y") or 1),
            scale_z=float(relative.get("scale_z") or 1),
            rotation_x=float(relative.get("rotation_x") or 0),
            rotation_y=float(relative.get("rotation_y") or 0),
            rotation_z=float(relative.get("rotation_z") or 0),
            extra=_mapping(relative.get("meta_data")),
        )

    def update_material(self, material_uuid: str, values: Mapping[str, Any], specified: Iterable[str]) -> dict[str, Any]:
        """V1 ``PUT /materials/{uuid}``：三态字段更新 + ``site_placement`` 人工上下料。"""

        specified = set(specified)
        aggregate = self.materials.get_material(material_uuid)
        material = aggregate.material
        expected = values.get("expected_revision")
        if expected is not None and int(expected) != material.version:
            raise MaterialConflictError("Material revision has changed")
        template_value = optional_uuid(values.get("resource_template_uuid"))
        if "resource_template_uuid" in specified and template_value and template_value != material.template_uuid:
            raise MaterialConflictError(
                "Resource template of an existing material cannot be changed", conflict="template_immutable"
            )
        barcode = values.get("barcode")
        if "barcode" in specified and barcode is not None and barcode != material.barcode:
            owner = self.find_by_barcode(str(barcode)) if barcode else None
            if owner and owner != material_uuid:
                raise MaterialConflictError(
                    "Material barcode or sibling name conflicts with an existing material", conflict="identity"
                )
        patch: dict[str, Any] = {}
        for key in ("name", "description", "barcode", "meta_data"):
            if key in specified and values.get(key) is not None:
                patch[key] = values[key] if key != "name" else str(values[key]).strip()
        if "config" in specified and isinstance(values.get("config"), Mapping):
            config = dict(values["config"])
            previous = dict(material.config)
            # 旧客户端整体提交 config 时不应无意清除已维护的容量上限
            for key in ("capacity", "max_volume"):
                if key in previous and key not in config:
                    config[key] = previous[key]
            patch["config"] = config
        if patch:
            if "name" in patch:
                patch["display_name"] = patch["name"]
            value = MaterialPatch.model_validate(patch)
            try:
                self.materials.patch_material(
                    new_mutation("patch_material", payload=value.model_dump(mode="json", exclude_none=False)),
                    material_uuid, value,
                )
            except MaterialNoChangeError:
                pass
            except MaterialConflictError as exc:
                if exc.conflict is None and ("name" in patch or "barcode" in patch):
                    raise MaterialConflictError(
                        "Material barcode or sibling name conflicts with an existing material", conflict="identity"
                    ) from None
                raise
        if "relative_position" in specified and isinstance(values.get("relative_position"), Mapping):
            position = self._position_from_v1(values["relative_position"])
            try:
                self.materials.put_position(
                    new_mutation("put_material_position", payload=position.model_dump(mode="json", exclude_none=False)),
                    material_uuid, position,
                )
            except MaterialNoChangeError:
                pass
        parent_uuid = optional_uuid(values.get("parent_uuid"))
        placement = _mapping(values.get("site_placement"))
        move: Optional[MaterialMove] = None
        if placement.get("action") == "place":
            site_uuid = optional_uuid(placement.get("site_uuid"))
            if not site_uuid:
                raise MaterialValidationError("site_placement.place requires site_uuid")
            move = MaterialMove(material_uuid=material_uuid, destination_site_uuid=site_uuid)
        elif placement.get("action") == "remove":
            move = MaterialMove(material_uuid=material_uuid, parent_material_uuid=material.parent_material_uuid)
        elif "parent_uuid" in specified and parent_uuid and parent_uuid != material.parent_material_uuid:
            if self.material_record(parent_uuid) is None:
                raise MaterialNotFoundError(f"parent material not found: {parent_uuid}", subject="parent")
            if parent_uuid in {item.material.material_uuid for item in self.materials.get_tree(material_uuid).nodes}:
                raise MaterialConflictError("material parent would form a cycle", conflict="parent_cycle")
            move = MaterialMove(material_uuid=material_uuid, parent_material_uuid=parent_uuid)
        if move is not None:
            try:
                self.materials.move_material(
                    new_mutation("move_material", payload=move.model_dump(mode="json", exclude_none=False)), move
                )
            except MaterialNoChangeError:
                pass
        return self.material_detail(material_uuid)

    def delete_material(self, material_uuid: str, *, scope: str = "material_only") -> None:
        scope = str(scope or "material_only").strip().lower()
        if scope not in {"material_only", "material_with_contents"}:
            raise MaterialValidationError("scope must be material_only or material_with_contents")
        aggregate = self.materials.get_material(material_uuid)
        if scope == "material_only":
            connection = self.materials.connection
            linked = connection.execute(
                "SELECT 1 FROM material WHERE parent_material_uuid=? AND deleted_at_ms IS NULL "
                "UNION ALL SELECT 1 FROM material_content WHERE material_uuid=? LIMIT 1",
                (material_uuid, material_uuid),
            ).fetchone()
            if linked or any(site.occupied_material_uuid for site in aggregate.sites):
                raise MaterialConflictError(
                    "Material is referenced by a child, Site, or container content", conflict="database"
                )
        bound = self.materials.connection.execute(
            "SELECT task_uuid FROM material_source_binding WHERE material_uuid=? AND status='active' LIMIT 1",
            (material_uuid,),
        ).fetchone()
        if bound:
            # 公开删除没有任务身份：任何任务的活动来源绑定（含共享来源）都阻塞删除
            raise MaterialConflictError(
                f"material is bound as a workflow material source by task {bound[0]}", conflict="active_claim"
            )
        value = MaterialDelete(material_uuid=material_uuid, recursive=scope == "material_with_contents")
        self.materials.delete_material(new_mutation("delete_material", payload=value.model_dump(mode="json")), value)

    def list_material_ledger(self, material_uuid: str, *, page: int = 1, page_size: int = 50) -> dict[str, Any]:
        """一件物料自己的台账（按发生时间倒序）；来源是 dev ``inventory_ledger``。"""

        if self.material_record(material_uuid, include_deleted=True) is None:
            raise MaterialNotFoundError("material does not exist", subject="material")
        page = page if page >= 1 else 1
        page_size = page_size if 1 <= page_size <= 100 else 50
        connection = self.materials.connection
        total = int(connection.execute(
            "SELECT COUNT(*) FROM inventory_ledger WHERE aggregate_type='material' AND aggregate_uuid=?",
            (material_uuid,),
        ).fetchone()[0])
        rows = connection.execute(
            "SELECT * FROM inventory_ledger WHERE aggregate_type='material' AND aggregate_uuid=? "
            "ORDER BY occurred_at_ms DESC, sequence DESC LIMIT ? OFFSET ?",
            (material_uuid, page_size, (page - 1) * page_size),
        ).fetchall()
        items = []
        for row in rows:
            delta = json.loads(row["delta_json"] or "{}")
            changes = delta.get("changes") if isinstance(delta.get("changes"), dict) else delta
            items.append({
                "uuid": row["event_uuid"],
                "occurred_at": row["occurred_at_ms"],
                "event_type": row["operation"],
                "name": str(changes.get("name") or ""),
                "quantity_delta": delta.get("quantity_delta"),
                "quantity_unit": delta.get("quantity_unit"),
                "changes": changes,
                "workflow_task_uuid": delta.get("task_uuid"),
                "workflow_node_job_uuid": row["job_uuid"],
                "sequence": row["sequence"],
            })
        return {"items": items, "page": page, "page_size": page_size, "total": total,
                "has_more": page * page_size < total}

    # -- 库位 ------------------------------------------------------

    def list_sites(self, material_uuid: str) -> list[dict[str, Any]]:
        self.materials.get_material(material_uuid)
        return [site_to_v1(site) for site in self.materials.list_sites(material_uuid)]

    def get_site(self, site_uuid: str) -> dict[str, Any]:
        site = self.materials.get_site(site_uuid)
        if site is None:
            raise MaterialNotFoundError("Site not found", subject="site")
        return site_to_v1(site)

    def resolve_site_identities(self, source_node_ids: Sequence[str]) -> dict[str, dict[str, str]]:
        """资源图库位业务 ID（``<owner resource_id>/<site label|index>`` 或 ``meta_data.source_node_id``）→ 库位 UUID。"""

        wanted = [str(item or "").strip() for item in source_node_ids if str(item or "").strip()]
        wanted = list(dict.fromkeys(wanted))
        if len(wanted) > MAX_SITE_IDENTITY_RESOLUTION_IDS:
            raise MaterialValidationError("too many source_node_ids")
        found: dict[str, set[str]] = {}
        rows = self.materials.connection.execute(
            "SELECT site.site_uuid, site.label, site.site_index, site.meta_data_json, material.resource_id "
            "FROM site JOIN material ON material.material_uuid = site.owner_material_uuid "
            "WHERE site.deleted_at_ms IS NULL AND material.deleted_at_ms IS NULL"
        ).fetchall()
        targets = set(wanted)
        for row in rows:
            meta = json.loads(row["meta_data_json"] or "{}")
            keys = {
                str(meta.get("source_node_id") or ""),
                f"{row['resource_id']}/{row['label']}",
                f"{row['resource_id']}/{row['site_index']}",
            }
            for key in keys & targets:
                found.setdefault(key, set()).add(row["site_uuid"])
        result: dict[str, dict[str, str]] = {}
        for key, uuids in found.items():
            if len(uuids) > 1:
                raise MaterialConflictError(f"site identity is ambiguous: {key}", conflict="identity")
            result[key] = {"uuid": next(iter(uuids))}
        return {"sites": result}

    # -- 状态历史 --------------------------------------------------

    @staticmethod
    def _state_row(row: Any) -> dict[str, Any]:
        return {
            "uuid": row["state_uuid"],
            "create_time": ms_to_iso(row["created_at_ms"]),
            "update_time": ms_to_iso(row["created_at_ms"]),
            "description": row["description"],
            "meta_data": json.loads(row["meta_data_json"] or "{}"),
            "material_uuid": row["material_uuid"],
            "status": row["status"],
            "state_data": json.loads(row["state_data_json"] or "{}"),
            "source": row["source"],
            "observed_at": ms_to_iso(row["observed_at_ms"]),
        }

    def append_material_state(self, material_uuid: str, values: Mapping[str, Any]) -> dict[str, Any]:
        self.materials.get_material(material_uuid)
        now = self.materials._now_ms()
        try:
            observed = iso_to_ms(values.get("observed_at")) if values.get("observed_at") else now
        except ValueError:
            raise MaterialValidationError("observed_at must be ISO 8601") from None
        state_uuid = str(uuid4())
        with self.materials.write() as connection:
            connection.execute(
                "INSERT INTO material_state_history(state_uuid, material_uuid, status, state_data_json, source, "
                "description, meta_data_json, observed_at_ms, created_at_ms) VALUES (?,?,?,?,?,?,?,?,?)",
                (state_uuid, material_uuid, values.get("status"),
                 json.dumps(_mapping(values.get("state_data")), ensure_ascii=False),
                 values.get("source"), values.get("description"),
                 json.dumps(_mapping(values.get("meta_data")), ensure_ascii=False), observed, now),
            )
        return self.get_material_state(state_uuid)

    def get_material_state(self, state_uuid: str) -> dict[str, Any]:
        row = self.materials.connection.execute(
            "SELECT * FROM material_state_history WHERE state_uuid=?", (state_uuid,)
        ).fetchone()
        if row is None:
            raise MaterialNotFoundError("Material state not found")
        return self._state_row(row)

    def list_material_states(self, material_uuid: str, *, before_time: Optional[str] = None,
                             before_uuid: Optional[str] = None, limit: int = 0) -> dict[str, Any]:
        self.materials.get_material(material_uuid)
        limit = 20 if limit <= 0 else min(limit, 100)
        clauses = ["material_uuid=?"]
        params: list[Any] = [material_uuid]
        if before_time:
            try:
                before = iso_to_ms(before_time)
            except ValueError:
                raise MaterialValidationError("before_time must be ISO 8601") from None
            clauses.append("(observed_at_ms < ? OR (observed_at_ms = ? AND state_uuid < ?))")
            params.extend([before, before, before_uuid or "￿"])
        rows = self.materials.connection.execute(
            f"SELECT * FROM material_state_history WHERE {' AND '.join(clauses)} "
            "ORDER BY observed_at_ms DESC, state_uuid DESC LIMIT ?",
            (*params, limit + 1),
        ).fetchall()
        items = [self._state_row(row) for row in rows[:limit]]
        result: dict[str, Any] = {"items": items, "has_more": len(rows) > limit}
        if items and result["has_more"]:
            result["next_before_time"] = items[-1]["observed_at"]
            result["next_before_uuid"] = items[-1]["uuid"]
        return result

    def latest_material_state(self, material_uuid: str) -> dict[str, Any]:
        self.materials.get_material(material_uuid)
        row = self.materials.connection.execute(
            "SELECT * FROM material_state_history WHERE material_uuid=? "
            "ORDER BY observed_at_ms DESC, state_uuid DESC LIMIT 1",
            (material_uuid,),
        ).fetchone()
        if row is None:
            raise MaterialNotFoundError("Material state not found")
        return self._state_row(row)

    # -- 物料图 ----------------------------------------------------

    def material_graph(self) -> dict[str, Any]:
        """V1 ``GET /materials/graph``：全部活动物料 + 相对位置 + 库位 + 模板摘要 + handle。"""

        connection = self.materials.connection
        rows = connection.execute(
            "SELECT material_uuid FROM material WHERE deleted_at_ms IS NULL ORDER BY created_at_ms, material_uuid"
        ).fetchall()
        templates: dict[str, Optional[ResourceTemplateRead]] = {}
        nodes = []
        for row in rows:
            aggregate = self.materials.get_material(row[0])
            material = aggregate.material
            if material.template_uuid not in templates:
                templates[material.template_uuid] = self._template_meta(material.template_uuid)
            template = templates[material.template_uuid]
            current = self.materials.occupied_site(material.material_uuid)
            nodes.append({
                "material": self.material_row(aggregate, template=template),
                "relative_position": position_to_v1(
                    material.material_uuid, aggregate.position, updated_at_ms=material.updated_at_ms
                ),
                "sites": [site_to_v1(site) for site in aggregate.sites],
                "current_site_uuid": current.site_uuid if current is not None else None,
                "handles": [handle_to_v1(template, item) for item in template.handles] if template else [],
                "resource_template": template_summary(template, include_tags=False) if template else None,
            })
        links = [
            {
                "uuid": link["link_uuid"],
                "source_uuid": link["source_material_uuid"],
                "target_uuid": link["target_material_uuid"],
                "source_handle": link["source_handle"],
                "target_handle": link["target_handle"],
                "type": link["link_type"],
            }
            for link in self.materials.list_links()
        ]
        return {"nodes": nodes, "links": links}


__all__ = [
    "MAX_SITE_IDENTITY_RESOLUTION_IDS",
    "ResourceContract",
    "handle_to_v1",
    "new_mutation",
    "optional_uuid",
    "position_to_v1",
    "site_to_v1",
    "template_summary",
    "template_to_v1",
    "v1_template_to_dev",
]
