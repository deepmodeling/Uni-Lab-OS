"""由边缘端（Edge）库存 SQLite 支撑的后端形态资源模块。

它是共享前端接口（Frontend Interface）背后的领域实现。遗留 ``/inventory``
适配器仍是边缘端专用操作面；两个适配器写入同一组规范
``resource_template/material/site`` 行。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from uuid import uuid4

from unilabos.app.scheduler.inventory.capacity import (
    CAPACITY_KEY,
    capacity_projection,
    material_capacity,
    normalize_capacity,
    validate_material_config,
)
from unilabos.app.scheduler.inventory.dispatch_admission import (
    InventoryMutationConflict,
    assert_inventory_mutation_unclaimed,
)
from unilabos.app.scheduler.inventory.material_io import (
    MaterialIOError,
    MaterialIOService,
)
from unilabos.app.scheduler.inventory.store import (
    InventoryStore,
    SiteOccupancyConflict,
    clear_site_occupancy,
    set_site_occupancy,
)
from unilabos.resources.site_definition import normalize_available_sites

_POSE_SELECT = """
SELECT pose_uuid AS uuid, create_time, update_time, NULL AS deleted_at,
       pose_description AS description, pose_meta_data AS meta_data,
       uuid AS material_uuid, position_x, position_y, position_z, depth, length,
       width, scale_x, scale_y, scale_z, rotation_x, rotation_y, rotation_z
FROM material
WHERE uuid=? AND deleted_at IS NULL AND position_x IS NOT NULL
"""

# 物理离站与运输保护分别建模：已取货的物料仍锁定，但不属于当前库存。
_DEPARTED_MATERIAL_SQL = """(
    material.inventory_status IN ('handoff_departed','incoming_canceled')
    OR EXISTS (
        SELECT 1 FROM station_handoff_resource AS protected
        JOIN station_handoff_source_location AS location
          ON location.handoff_item_uuid=protected.handoff_item_uuid
        WHERE protected.material_uuid=material.uuid AND protected.active=1
          AND location.pickup_json IS NOT NULL
    )
)"""
_INVENTORY_PRESENCE_SQL = (
    f"CASE WHEN {_DEPARTED_MATERIAL_SQL} THEN 'departed' ELSE 'current' END "
    "AS inventory_presence"
)


def _inventory_scope_predicate(inventory_scope: str) -> str:
    """为列表和物料图生成同一固定范围条件；未知范围返回参数错误。"""
    if inventory_scope == "current":
        return f"NOT {_DEPARTED_MATERIAL_SQL}"
    if inventory_scope == "history":
        return _DEPARTED_MATERIAL_SQL
    raise BackendContractError(INVALID_PARAMETER, "Unknown inventory_scope")


def _clear_material_pose(connection: sqlite3.Connection, material_uuid: str) -> None:
    """软删除位姿：几何列留空，不另写删除行。"""

    connection.execute(
        """
        UPDATE material SET
            pose_uuid=NULL, pose_description=NULL, pose_meta_data='{}',
            position_x=NULL, position_y=NULL, position_z=NULL,
            depth=NULL, length=NULL, width=NULL,
            scale_x=NULL, scale_y=NULL, scale_z=NULL,
            rotation_x=NULL, rotation_y=NULL, rotation_z=NULL
        WHERE uuid=?
        """,
        (material_uuid,),
    )


class BackendContractError(RuntimeError):
    """使用后端（Backend）数值响应合同编码的业务错误。"""

    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


INVALID_PARAMETER = 1000
DATABASE_CONFLICT = 2005
REAGENT_INFO_IN_USE = 4000
RESOURCE_NOT_FOUND = 4001
RESOURCE_DATA_CONFLICT = 4002
RESOURCE_TEMPLATE_NOT_FOUND = 5000
TEMPLATE_DEFINITION_INVALID = 5003
TEMPLATE_DATA_CONFLICT = 5004
MATERIAL_NOT_FOUND = 6000
MATERIAL_TEMPLATE_NOT_FOUND = 6001
MATERIAL_TEMPLATE_IMMUTABLE = 6002
MATERIAL_PARENT_NOT_FOUND = 6003
MATERIAL_PARENT_CYCLE = 6004
MATERIAL_SITE_NOT_FOUND = 6006
MATERIAL_SITE_OCCUPIED = 6007
MATERIAL_SITE_TEMPLATE_NOT_ALLOWED = 6008
MATERIAL_SITE_CYCLE = 6009
MATERIAL_IDENTITY_CONFLICT = 6010
MATERIAL_ACTIVE_CLAIM_CONFLICT = 6011
MAX_SITE_IDENTITY_RESOLUTION_IDS = 500


def _now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _json(value: Any, fallback: Any) -> Any:
    if value is None:
        return fallback
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(str(value))
    except (TypeError, ValueError):
        return fallback


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _optional(value: Any) -> Any:
    return None if value in (None, "") else value


def _optional_uuid(value: Any) -> Optional[str]:
    """把未传、空串和全零 UUID 统一解释为未设置。

    参数：``value`` 是已通过 HTTP DTO 校验的可空 UUID。返回：规范
    UUID 字符串或 ``None``。异常：无；非空值交由调用方校验引用。
    """

    identity = str(value or "")
    if identity in ("", "00000000-0000-0000-0000-000000000000"):
        return None
    return identity


class BackendResourceService:
    """提供资源模板（ResourceTemplate）、物料（Material）与库位（Site）权威写入。"""

    def __init__(
        self,
        store: InventoryStore,
        *,
        edge_id: str = "edge-default",
        lab_id: str = "edge-lab",
        monitor: Any = None,
    ):
        """绑定 OS Local 库与当前 Edge 身份。

        参数：``store`` 是唯一库存数据库；``edge_id`` 与 ``lab_id`` 用于同一
        HTTP 合同下需要写入事务发件箱的领域事件。返回：初始化后的资源服务。
        """

        self.store = store
        self.edge_id = edge_id
        self.lab_id = lab_id
        self._monitor = monitor
        self._material_io = MaterialIOService(store)

    def _notify_material_changed(self, material_uuid: str, operation: str) -> None:
        """在 Backend 物料写事务提交后通知本地调度器。"""

        if self._monitor is None:
            return
        try:
            self._monitor.emit(
                "material",
                "material_changed",
                {
                    "material_uuid": str(material_uuid),
                    "operation": operation,
                    "edge_id": self.edge_id,
                    "lab_id": self.lab_id,
                },
            )
        except Exception:
            # 通知故障不能回滚已经成功提交的物料事务。
            pass

    # Resource Template -------------------------------------------------

    def sync_resource_templates(
        self, resources: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """同步一代活动资源模板并返回稳定身份回执。

        参数说明：``resources`` 是同批设备与物料资源模板（ResourceTemplate）
        定义。返回：每个注册表（Registry）业务 ID 对应的活动 UUID；已有活动名
        复用 UUID，只有软删除历史时创建新 UUID。异常：空定义、重复业务 ID 或
        SQLite 约束冲突转换为 ``BackendContractError``，整个事务回滚。
        """

        if not resources:
            raise BackendContractError(
                TEMPLATE_DEFINITION_INVALID, "resources is required"
            )
        # ``normalized_names`` 是本批注册表（Registry）业务 ID 的规范唯一集合。
        normalized_names = [
            str(resource.get("id") or "").strip() for resource in resources
        ]
        if any(not name for name in normalized_names) or len(
            set(normalized_names)
        ) != len(normalized_names):
            raise BackendContractError(
                TEMPLATE_DEFINITION_INVALID,
                "resource names are required and must be unique",
            )
        runtime_catalog = self.store.runtime_device_template_catalog
        runtime_catalog_required = (
            self.store.runtime_device_template_catalog_required
        )
        runtime_device_names = self.store.runtime_device_template_names
        # 安装运行时目录后，当前领域包中已有设备仍按原同步接口幂等返回身份，
        # 但不写 SQLite；未知设备或用 resource 类型覆盖设备名会关闭式失败。
        runtime_identity_by_name: dict[str, str] = {}
        for resource, name in zip(resources, normalized_names):
            registry_type = str(
                resource.get("registry_type") or "resource"
            ).strip().lower()
            if name in runtime_device_names and registry_type != "device":
                raise BackendContractError(
                    TEMPLATE_DATA_CONFLICT,
                    "resource template name is owned by the runtime device catalog",
                )
            if registry_type == "device" and runtime_catalog is None:
                if runtime_catalog_required:
                    raise BackendContractError(
                        TEMPLATE_DATA_CONFLICT,
                        "runtime device template catalog is not ready",
                    )
                continue
            if runtime_catalog is not None:
                runtime_uuid = runtime_catalog.resolve_uuid(name)
                if registry_type == "device":
                    if not runtime_uuid:
                        raise BackendContractError(
                            TEMPLATE_DATA_CONFLICT,
                            "device templates are owned by the runtime package catalog",
                        )
                    runtime_identity_by_name[name] = runtime_uuid
                elif runtime_uuid:
                    raise BackendContractError(
                        TEMPLATE_DATA_CONFLICT,
                        "resource template name conflicts with a runtime device",
                    )
        # ``identities`` 只记录本次事务最终采用的活动资源模板稳定身份。
        identities: List[Dict[str, str]] = []
        try:
            with self.store.transaction() as conn:
                for resource, name in zip(resources, normalized_names):
                    runtime_uuid = runtime_identity_by_name.get(name)
                    if runtime_uuid is not None:
                        identities.append({"uuid": runtime_uuid, "name": name})
                        continue
                    # ``existing`` 只能是当前活动业务 ID；软删除历史不得被复活。
                    existing = conn.execute(
                        "SELECT uuid,meta_data,resource_type FROM resource_template "
                        "WHERE name = ? AND deleted_at IS NULL",
                        (name,),
                    ).fetchone()
                    if (
                        runtime_catalog_required
                        and existing is not None
                        and str(existing["resource_type"]) == "device"
                    ):
                        raise BackendContractError(
                            TEMPLATE_DATA_CONFLICT,
                            "persisted device templates are read-only during runtime catalog startup",
                        )
                    template_uuid = str(existing["uuid"]) if existing else str(uuid4())
                    existing_meta = _json(existing["meta_data"], {}) if existing else {}
                    source_uri = resource.get("source_uri")
                    if source_uri is not None and (
                        not isinstance(source_uri, str)
                        or not source_uri.startswith("package://")
                    ):
                        raise BackendContractError(
                            TEMPLATE_DEFINITION_INVALID,
                            "resource template source_uri must use package://",
                        )
                    meta_data = dict(existing_meta)
                    metadata = resource.get("metadata") or {}
                    if not isinstance(metadata, dict):
                        raise BackendContractError(TEMPLATE_DEFINITION_INVALID, "metadata 须为 JSON 对象")
                    if CAPACITY_KEY in metadata:
                        meta_data[CAPACITY_KEY] = normalize_capacity(metadata[CAPACITY_KEY])
                    if "twin" in metadata:
                        from unilabos.runtime.observations import Descriptor

                        try:
                            meta_data["twin"] = Descriptor.model_validate(metadata["twin"]).model_dump()
                        except ValueError as error:
                            raise BackendContractError(TEMPLATE_DEFINITION_INVALID, str(error)) from error
                    if source_uri:
                        meta_data["unilab"] = {
                            **_json(meta_data.get("unilab"), {}),
                            "source_uri": source_uri,
                        }
                    # ``class_definition`` 是当前模板冻结的 Python 实现身份合同。
                    class_definition = resource.get("class") or {}
                    # ``schema`` 是资源模板初始化参数的数据/配置命名空间合同。
                    schema = resource.get("init_param_schema") or {}
                    try:
                        # 本地 Backend 也在写边界关闭式校验，避免发布阶段才发现
                        # Registry 库位（Site）模板定义不可被云端实例化。
                        available_sites = normalize_available_sites(
                            resource.get("available_sites")
                        )
                    except ValueError as error:
                        raise BackendContractError(
                            TEMPLATE_DEFINITION_INVALID,
                            f"invalid resource template available_sites: {error}",
                        ) from error
                    # ``data_schema`` 与 ``config_schema`` 分别持久化初始化状态字段合同。
                    data_schema = (schema.get("data") or {}).get("properties") or {}
                    config_schema = (schema.get("config") or {}).get("properties") or {}
                    # ``values`` 是一次 upsert 使用的完整后端（Backend）形态模板行。
                    values = (
                        template_uuid,
                        _now(),
                        _now(),
                        resource.get("description"),
                        _dump(meta_data),
                        name,
                        str(resource.get("display_name") or name),
                        str(resource.get("registry_type") or "resource"),
                        _optional(resource.get("icon")),
                        _dump(resource.get("model") or {}),
                        _optional(class_definition.get("module")),
                        _optional(class_definition.get("type")),
                        _dump(resource.get("category") or []),
                        _dump(data_schema),
                        _dump(config_schema),
                        _dump({}),
                        _dump(resource.get("config_info") or []),
                        _dump(available_sites),
                        _optional(resource.get("cover")),
                        _dump(resource.get("scene") or []),
                        _dump(resource.get("device_params") or {}),
                        _dump({}),
                    )
                    conn.execute(
                        """
                        INSERT INTO resource_template(
                            uuid, create_time, update_time, description, meta_data,
                            name, display_name, resource_type, icon, model, module,
                            language, tags, data_schema, config_schema, pose,
                            config_info, available_sites, cover, scene,
                            device_params, ui_overlay, aggregate_version
                        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)
                        ON CONFLICT(uuid) DO UPDATE SET
                            update_time=excluded.update_time,
                            deleted_at=NULL,
                            description=excluded.description,
                            meta_data=excluded.meta_data,
                            display_name=excluded.display_name,
                            resource_type=excluded.resource_type,
                            icon=excluded.icon,
                            model=excluded.model,
                            module=excluded.module,
                            language=excluded.language,
                            tags=excluded.tags,
                            data_schema=excluded.data_schema,
                            config_schema=excluded.config_schema,
                            config_info=excluded.config_info,
                            available_sites=excluded.available_sites,
                            cover=excluded.cover,
                            scene=excluded.scene,
                            device_params=excluded.device_params,
                            aggregate_version=resource_template.aggregate_version+1
                        """,
                        values,
                    )
                    if "handles" in resource:
                        self._reconcile_resource_handles(
                            conn,
                            template_uuid,
                            resource.get("handles") or [],
                        )
                    identities.append({"uuid": template_uuid, "name": name})
        except sqlite3.IntegrityError as exc:
            raise BackendContractError(
                TEMPLATE_DATA_CONFLICT, "template data conflicts with existing data"
            ) from exc
        return {"templates": identities}

    def list_resource_templates(
        self,
        *,
        page: int,
        page_size: int,
        keyword: str,
        resource_type: str,
    ) -> Dict[str, Any]:
        page = 1 if page <= 0 else page
        page_size = 20 if page_size <= 0 else min(page_size, 100)
        where = ["deleted_at IS NULL", "resource_type <> ?"]
        values: List[Any] = ["framework"]
        if keyword:
            where.append("(LOWER(name) LIKE ? OR LOWER(display_name) LIKE ?)")
            keyword_pattern = f"%{keyword.strip().lower()}%"
            values.extend((keyword_pattern, keyword_pattern))
        if resource_type:
            where.append("resource_type = ?")
            values.append(resource_type.strip())
        offset = (page - 1) * page_size
        rows = self.store.query_all(
            "SELECT uuid,create_time,name,display_name,resource_type,icon,tags "
            f"FROM resource_template WHERE {' AND '.join(where)} "
            "ORDER BY create_time DESC,uuid DESC",
            tuple(values),
        )
        entries = [
            (
                str(row["create_time"]),
                str(row["uuid"]),
                {
                    "uuid": row["uuid"],
                    "name": row["name"],
                    "display_name": row["display_name"],
                    "resource_type": row["resource_type"],
                    "tags": _json(row["tags"], []),
                    **({"icon": row["icon"]} if row["icon"] is not None else {}),
                },
            )
            for row in rows
        ]
        runtime_catalog = self.store.runtime_device_template_catalog
        if runtime_catalog is not None and resource_type.strip() in {"", "device"}:
            normalized_keyword = keyword.strip().lower()
            for detail in runtime_catalog.list():
                if normalized_keyword and normalized_keyword not in str(
                    detail["name"]
                ).lower() and normalized_keyword not in str(
                    detail["display_name"]
                ).lower():
                    continue
                entries.append(
                    (
                        str(detail["create_time"]),
                        str(detail["uuid"]),
                        self._resource_template_summary_from_detail(
                            detail,
                            include_tags=True,
                        ),
                    )
                )
        entries.sort(key=lambda entry: (entry[0], entry[1]), reverse=True)
        items = [entry[2] for entry in entries]
        page_items = items[offset : offset + page_size]
        return {
            "items": page_items,
            "has_more": len(items) > offset + page_size,
            "page": page,
            "page_size": page_size,
        }

    def get_resource_template(self, template_uuid: str) -> Dict[str, Any]:
        runtime_catalog = self.store.runtime_device_template_catalog
        if runtime_catalog is not None:
            runtime_detail = runtime_catalog.get(template_uuid)
            if runtime_detail is not None:
                return runtime_detail
        row = self.store.query_one(
            "SELECT * FROM resource_template WHERE uuid=? AND deleted_at IS NULL",
            (template_uuid,),
        )
        if row is None:
            raise BackendContractError(
                RESOURCE_TEMPLATE_NOT_FOUND, "Resource template not found"
            )
        result = self._resource_template_row(row)
        result["handles"] = [
            self._resource_handle_row(handle)
            for handle in self.store.query_all(
                "SELECT * FROM resource_handle_template "
                "WHERE resource_template_uuid=? AND deleted_at IS NULL "
                "ORDER BY io_type,name,uuid",
                (template_uuid,),
            )
        ]
        return result

    def delete_resource_template(self, template_uuid: str) -> None:
        runtime_catalog = self.store.runtime_device_template_catalog
        if runtime_catalog is not None and runtime_catalog.contains_uuid(template_uuid):
            raise BackendContractError(
                TEMPLATE_DATA_CONFLICT,
                "runtime device templates can only be changed by the domain package",
            )
        with self.store.transaction() as conn:
            row = conn.execute(
                "SELECT uuid,name,resource_type FROM resource_template "
                "WHERE uuid=? AND deleted_at IS NULL",
                (template_uuid,),
            ).fetchone()
            if row is None:
                raise BackendContractError(
                    RESOURCE_TEMPLATE_NOT_FOUND, "Resource template not found"
                )
            if self.store.runtime_device_template_catalog_required and (
                str(row["resource_type"]) == "device"
                or str(row["name"]) in self.store.runtime_device_template_names
            ):
                raise BackendContractError(
                    TEMPLATE_DATA_CONFLICT,
                    "runtime device templates can only be changed by the domain package",
                )
            in_use = conn.execute(
                "SELECT 1 FROM material WHERE resource_template_uuid=? "
                "AND deleted_at IS NULL LIMIT 1",
                (template_uuid,),
            ).fetchone()
            if in_use:
                raise BackendContractError(
                    TEMPLATE_DATA_CONFLICT, "Resource template is in use"
                )
            conn.execute(
                "UPDATE resource_template SET deleted_at=?,update_time=? WHERE uuid=?",
                (_now(), _now(), template_uuid),
            )
            conn.execute(
                "UPDATE resource_handle_template SET deleted_at=?,update_time=? "
                "WHERE resource_template_uuid=? AND deleted_at IS NULL",
                (_now(), _now(), template_uuid),
            )

    # Material ----------------------------------------------------------

    def material_io_load(self, values: Dict[str, Any]) -> Dict[str, Any]:
        """通过窄物料边界原子创建并装载一个模板化容器。"""

        try:
            return self._material_io.load(
                template_uuid=values.get("template_uuid"),
                mount_material_uuid=values.get("mount_material_uuid"),
                site_uuid=values.get("site_uuid"),
                site=values.get("site"),
                name=values.get("name"),
                workflow_id=values.get("workflow_id"),
                node_id=values.get("node_id"),
                attempt=values.get("attempt"),
                reagent=values.get("reagent"),
                barcode=values.get("barcode") or "",
            )
        except MaterialIOError as error:
            raise self._material_io_contract_error(error) from error

    def material_io_unload(self, values: Dict[str, Any]) -> Dict[str, Any]:
        """通过窄物料边界原子解除挂载或软删除一个容器内容树。"""

        try:
            return self._material_io.unload(
                material_uuid=values.get("material_uuid"),
                disposition=values.get("disposition"),
                scope=values.get("scope"),
                workflow_id=values.get("workflow_id"),
                node_id=values.get("node_id"),
                attempt=values.get("attempt"),
            )
        except MaterialIOError as error:
            raise self._material_io_contract_error(error) from error

    def material_io_settle_load(
        self, values: Dict[str, Any], permit: Dict[str, Any]
    ) -> Dict[str, Any]:
        """验证运行时自身完整 Permit/Fence 后原子结算 Load。"""

        try:
            return self._material_io.settle_load(permit=permit, **values)
        except MaterialIOError as error:
            raise self._material_io_contract_error(error) from error

    def material_io_settle_unload(
        self, values: Dict[str, Any], permit: Dict[str, Any]
    ) -> Dict[str, Any]:
        """验证运行时自身完整 Permit/Fence 后原子结算 Unload。"""

        try:
            return self._material_io.settle_unload(permit=permit, **values)
        except MaterialIOError as error:
            raise self._material_io_contract_error(error) from error

    def material_io_refill(self, values: Dict[str, Any]) -> Dict[str, Any]:
        """通过窄物料边界在已占位容器上增加试剂余量。"""

        try:
            return self._material_io.refill(
                material_uuid=values.get("material_uuid"),
                mount_material_uuid=values.get("mount_material_uuid"),
                site_uuid=values.get("site_uuid"),
                site=values.get("site"),
                workflow_id=values.get("workflow_id"),
                node_id=values.get("node_id"),
                attempt=values.get("attempt"),
                reagent=values.get("reagent"),
            )
        except MaterialIOError as error:
            raise self._material_io_contract_error(error) from error

    def material_io_settle_refill(
        self, values: Dict[str, Any], permit: Dict[str, Any]
    ) -> Dict[str, Any]:
        """验证运行时自身完整 Permit/Fence 后原子结算补液。"""

        try:
            return self._material_io.settle_refill(permit=permit, **values)
        except MaterialIOError as error:
            raise self._material_io_contract_error(error) from error

    def create_material(self, values: Dict[str, Any]) -> Dict[str, Any]:
        """创建物料或设备实例，并从对应模板权威校验模板身份。

        参数：``values`` 保持既有 Backend 物料写入合同。返回：新实例详情。
        若模板声明 ``available_sites``，同一事务展开库位及默认子物料。
        设备模板从当前进程内目录解析，物料模板从 SQLite 解析；两者均继续使用
        同一个 ``resource_template_uuid`` 外部字段。
        """

        with self.store.transaction() as conn:
            result = self._create_material_in_transaction(conn, values)
        self._notify_material_changed(result["uuid"], "created")
        return result

    def create_materials(self, values: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """在同一事务内创建整批物料及内联试剂，提交后才发送物料通知。"""

        if not values:
            raise BackendContractError(INVALID_PARAMETER, "items must not be empty")
        with self.store.transaction() as conn:
            result = [self._create_material_in_transaction(conn, item) for item in values]
        for material in result:
            self._notify_material_changed(material["uuid"], "created")
        return result

    def _create_material_in_transaction(
        self, conn: sqlite3.Connection, values: Dict[str, Any]
    ) -> Dict[str, Any]:
        """复用调用方事务创建物料、模板内容及试剂，不提交或发送通知。"""

        material_uuid = str(uuid4())
        template_uuid = str(values.get("resource_template_uuid") or "")
        parent_uuid = _optional(values.get("parent_uuid"))
        barcode = str(values.get("barcode") or "").strip()
        name = str(values.get("name") or "").strip()
        inline_reagent = values.get("reagent")
        content_snapshot: Optional[Dict[str, Dict[str, Any]]] = None
        created_site_uuids: List[str] = []
        initial_material_uuids: List[str] = []
        if not template_uuid or not name:
            raise BackendContractError(
                INVALID_PARAMETER, "resource_template_uuid and name are required"
            )
        runtime_catalog = self.store.runtime_device_template_catalog
        runtime_template = (
            runtime_catalog.get(template_uuid) if runtime_catalog is not None else None
        )
        try:
            template = runtime_template or conn.execute(
                "SELECT resource_type,available_sites FROM resource_template "
                "WHERE uuid=? AND deleted_at IS NULL",
                (template_uuid,),
            ).fetchone()
            if template is None:
                raise BackendContractError(
                    MATERIAL_TEMPLATE_NOT_FOUND,
                    "Resource template associated with the material not found",
                )
            if parent_uuid:
                self._require_material(conn, parent_uuid, MATERIAL_PARENT_NOT_FOUND)
            placement = values.get("site_placement")
            target_site_uuid = _optional_uuid(
                (placement or {}).get("site_uuid")
            )
            if parent_uuid or placement:
                assert_inventory_mutation_unclaimed(
                    conn,
                    material_uuids=(parent_uuid,) if parent_uuid and not target_site_uuid else (),
                    site_uuids=(target_site_uuid,) if target_site_uuid else (),
                )
            now = _now()
            conn.execute(
                """
                INSERT INTO material(
                    uuid,create_time,update_time,deleted_at,description,meta_data,
                    resource_template_uuid,parent_uuid,class,type,barcode,name,config,data,
                    legacy_template_id,aggregate_version
                ) VALUES (?,?,?,NULL,?,?,?,?,?,?,?,?,?,?,?,1)
                """,
                (
                    material_uuid,
                    now,
                    now,
                    values.get("description"),
                    _dump(values.get("meta_data") or {}),
                    template_uuid,
                    parent_uuid,
                    str(template["resource_type"]),
                    str(template["resource_type"]),
                    barcode,
                    name,
                    _dump(values.get("config") or {}),
                    _dump({}),
                    template_uuid,
                ),
            )
            try:
                created_site_uuids, initial_material_uuids = (
                    self._material_io.materialize_template_contents(
                        conn,
                        material_uuid=material_uuid,
                        template=template,
                        material_name=name,
                        now=now,
                    )
                )
            except MaterialIOError as error:
                raise self._material_io_contract_error(error) from error
            if inline_reagent is None:
                validate_material_config(conn, material_uuid, values.get("config") or {})
            if values.get("relative_position") is not None:
                self._upsert_relative_position(
                    conn, material_uuid, values["relative_position"]
                )
            if placement:
                self._apply_site_placement(
                    conn, material_uuid, template_uuid, placement
                )
            if inline_reagent is not None:
                # 物料与内容物必须共享当前事务；不能先提交空容器，再调用独立
                # ``POST /reagents``，否则中途失败会留下半成品。
                from unilabos.app.scheduler.inventory.reagent_contract import (
                    BackendReagentService,
                )

                reagent_values = dict(inline_reagent)
                reagent_values["material_uuid"] = material_uuid
                content_snapshot = BackendReagentService(
                    self.store,
                    edge_id=self.edge_id,
                    lab_id=self.lab_id,
                ).create_reagent_in_transaction(conn, reagent_values)
        except InventoryMutationConflict as error:
            raise BackendContractError(
                MATERIAL_ACTIVE_CLAIM_CONFLICT,
                str(error),
            ) from error
        except BackendContractError:
            raise
        except sqlite3.IntegrityError as exc:
            raise BackendContractError(
                MATERIAL_IDENTITY_CONFLICT,
                "Material barcode or root device name conflicts with an existing material",
            ) from exc
        result = self.get_material(material_uuid)
        result["children"] = [{"uuid": uid} for uid in initial_material_uuids]
        result["site_uuids"] = created_site_uuids
        result["initial_material_uuids"] = initial_material_uuids
        if content_snapshot is not None:
            result.update(content_snapshot)
        return result

    def list_materials(
        self,
        *,
        page: int,
        page_size: int,
        name: str,
        barcode: str,
        resource_template_uuid: Optional[str],
        category: Optional[str] = None,
        inventory_scope: str = "current",
        empty_containers_only: bool = False,
    ) -> Dict[str, Any]:
        page = max(page, 1)
        page_size = 20 if page_size <= 0 else min(page_size, 100)
        where = ["deleted_at IS NULL", _inventory_scope_predicate(inventory_scope)]
        values: List[Any] = []
        if name:
            where.append("name LIKE ?")
            values.append(f"%{name}%")
        if barcode:
            where.append("barcode = ?")
            values.append(barcode)
        if resource_template_uuid:
            where.append("resource_template_uuid = ?")
            values.append(resource_template_uuid)
        if category == "device":
            where.append("type = ?")
            values.append("device")
        elif category == "material":
            where.append("type <> ?")
            values.append("device")
        if empty_containers_only:
            # 容器标签与登记校验一致；数量为零仍属活动内容，移除登记后才视为空。
            # 在 COUNT 和分页之前过滤，不能只从当前页删掉已有内容的容器。
            where.extend([
                "type <> 'device'",
                """EXISTS (SELECT 1 FROM resource_template t, json_each(t.tags) tag
                   WHERE t.uuid=material.resource_template_uuid AND t.deleted_at IS NULL
                     AND LOWER(TRIM(CAST(tag.value AS TEXT)))='container')""",
                """NOT EXISTS (SELECT 1 FROM container_content c
                   WHERE c.material_uuid=material.uuid AND c.deleted_at IS NULL)""",
            ])
        predicate = " AND ".join(where)
        total = self.store.query_one(
            f"SELECT COUNT(*) AS count FROM material WHERE {predicate}", tuple(values)
        )
        rows = self.store.query_all(
            f"SELECT material.*,{_INVENTORY_PRESENCE_SQL},(SELECT meta_data FROM resource_template "
            "WHERE uuid=material.resource_template_uuid AND deleted_at IS NULL) "
            f"AS capacity_template_meta FROM material WHERE {predicate} "
            "ORDER BY create_time DESC,uuid DESC LIMIT ? OFFSET ?",
            (*values, page_size, (page - 1) * page_size),
        )
        return {
            "items": [self._material_row(row) for row in rows],
            "total": int(total["count"] if total else 0),
            "page": page,
            "page_size": page_size,
        }

    def get_material(self, material_uuid: str) -> Dict[str, Any]:
        """读取物料当前详情、修订、相对位置与库位关系。

        参数：``material_uuid`` 是物料稳定身份。返回：面向 Backend
        公共合同的完整物料投影。异常：物料不存在时抛 ``6000``。
        """

        row = self.store.query_one(
            f"SELECT material.*,{_INVENTORY_PRESENCE_SQL},"
            "(SELECT meta_data FROM resource_template WHERE uuid=material.resource_template_uuid "
            "AND deleted_at IS NULL) AS capacity_template_meta "
            "FROM material "
            "WHERE material.uuid=? AND material.deleted_at IS NULL",
            (material_uuid,),
        )
        if row is None:
            raise BackendContractError(MATERIAL_NOT_FOUND, "Material not found")
        result = self._material_row(dict(row))
        position = self.store.query_one(_POSE_SELECT, (material_uuid,))
        result["relative_position"] = (
            self._relative_position_row(dict(position)) if position else None
        )
        result["sites"] = self.list_sites(material_uuid)
        current_site = self.store.query_one(
            "SELECT * FROM site WHERE occupied_material_uuid=? AND deleted_at IS NULL",
            (material_uuid,),
        )
        result["current_site"] = self._site_row(current_site) if current_site else None
        return result

    def list_material_movements(self, **filters: Any) -> Dict[str, Any]:
        """读取本站每次交接的出入站记录，不改变当前/离站库存投影。"""
        from unilabos.app.scheduler.inventory.movement_history import list_material_movements

        return list_material_movements(self.store, **filters)

    def list_material_ledger(
        self, material_uuid: str, *, page: int = 1, page_size: int = 50
    ) -> Dict[str, Any]:
        """按时间倒序读取一只物料自己的台账。"""

        if self.store.query_one(
            "SELECT uuid FROM material WHERE uuid=?", (material_uuid,)
        ) is None:
            raise BackendContractError(MATERIAL_NOT_FOUND, "material does not exist")
        page = page if isinstance(page, int) and page >= 1 else 1
        page_size = page_size if isinstance(page_size, int) and 1 <= page_size <= 100 else 50
        count_row = self.store.query_one(
            "SELECT COUNT(*) AS count FROM inventory_ledger "
            "WHERE subject_type='material' AND material_uuid=?",
            (material_uuid,),
        ) or {"count": 0}
        rows = self.store.query_all(
            "SELECT entry_uuid,occurred_at,op_type,quantity_delta,quantity_unit,"
            "delta_json,workflow_task_uuid,workflow_node_job_uuid FROM inventory_ledger "
            "WHERE subject_type='material' AND material_uuid=? "
            "ORDER BY occurred_at DESC,ledger_id DESC LIMIT ? OFFSET ?",
            (material_uuid, page_size, (page - 1) * page_size),
        )
        items = []
        for row in rows:
            changes = _json(row["delta_json"], {}).get("changes")
            if not isinstance(changes, dict):
                changes = {}
            items.append({
                "uuid": row["entry_uuid"],
                "occurred_at": row["occurred_at"],
                "event_type": row["op_type"],
                "name": str(changes.get("name") or ""),
                "quantity_delta": row["quantity_delta"],
                "quantity_unit": row["quantity_unit"],
                "changes": changes,
                "workflow_task_uuid": row["workflow_task_uuid"],
                "workflow_node_job_uuid": row["workflow_node_job_uuid"],
            })
        total = int(count_row["count"])
        return {
            "items": items,
            "page": page,
            "page_size": page_size,
            "total": total,
            "has_more": page * page_size < total,
        }

    def update_material(
        self, material_uuid: str, values: Dict[str, Any]
    ) -> Dict[str, Any]:
        """按 Backend 公共合同原子更新物料及其库位占用。

        参数：``material_uuid`` 是物料稳定身份，``values`` 包含三态更新字段、可选
        期望修订。返回：提交后的完整物料详情。异常：物料、父级、库位、修订或
        唯一约束冲突时抛
        ``BackendContractError``；事务失败不保留部分更新。
        """

        try:
            with self.store.transaction() as conn:
                current = self._require_material(conn, material_uuid)
                self._assert_material_current(conn, material_uuid)
                specified_marker = values.get("_specified_fields")
                specified = (
                    set(specified_marker)
                    if specified_marker is not None
                    else {key for key in values if not key.startswith("_")}
                )
                placement = values.get("site_placement")
                parent_is_mutated = (
                    "parent_uuid" in specified
                    and values.get("parent_uuid") is not None
                    and _optional_uuid(values.get("parent_uuid"))
                    != _optional_uuid(current["parent_uuid"])
                )
                if placement or parent_is_mutated:
                    target_site_uuid = _optional_uuid(
                        (placement or {}).get("site_uuid")
                    )
                    assert_inventory_mutation_unclaimed(
                        conn,
                        material_uuids=tuple(
                            item
                            for item in (
                                material_uuid,
                                _optional_uuid(current["parent_uuid"]) if not placement else None,
                                _optional_uuid(values.get("parent_uuid")) if not placement else None,
                            )
                            if item
                        ),
                        site_uuids=(target_site_uuid,) if target_site_uuid else (),
                    )
                template_uuid = str(current["resource_template_uuid"])
                template_value = values.get("resource_template_uuid")
                if (
                    "resource_template_uuid" in specified
                    and template_value is not None
                    and str(template_value) != template_uuid
                ):
                    raise BackendContractError(
                        MATERIAL_TEMPLATE_IMMUTABLE,
                        "Resource template of an existing material cannot be changed",
                    )
                inventory = conn.execute(
                    "SELECT aggregate_version FROM material WHERE uuid=?",
                    (material_uuid,),
                ).fetchone()
                expected_revision = values.get("expected_revision")
                if (
                    expected_revision is not None
                    and int(expected_revision) != int(inventory["aggregate_version"])
                ):
                    raise BackendContractError(
                        RESOURCE_DATA_CONFLICT,
                        "Material revision has changed",
                    )
                parent_uuid = (
                    _optional_uuid(values.get("parent_uuid"))
                    if "parent_uuid" in specified
                    and values.get("parent_uuid") is not None
                    else _optional_uuid(current["parent_uuid"])
                )
                if parent_uuid:
                    self._require_material(conn, parent_uuid, MATERIAL_PARENT_NOT_FOUND)
                    self._check_parent_cycle(conn, material_uuid, parent_uuid)
                barcode = (
                    str(values.get("barcode") or "").strip()
                    if "barcode" in specified and values.get("barcode") is not None
                    else str(current["barcode"] or "")
                )
                name = (
                    str(values.get("name") or "").strip()
                    if "name" in specified and values.get("name") is not None
                    else str(current["name"] or "")
                )
                description = (
                    values.get("description")
                    if "description" in specified
                    and values.get("description") is not None
                    else current["description"]
                )
                meta_data = (
                    values.get("meta_data")
                    if "meta_data" in specified
                    and values.get("meta_data") is not None
                    else _json(current["meta_data"], {})
                )
                config = (
                    values.get("config")
                    if "config" in specified
                    and values.get("config") is not None
                    else _json(current["config"], {})
                )
                # 旧客户端整体提交 config 时，不应无意清除新版本维护的上限。
                previous_config = _json(current["config"], {})
                capacity_specified = (
                    "config" in specified and isinstance(values.get("config"), dict)
                    and CAPACITY_KEY in values["config"]
                )
                if capacity_specified:
                    config = {**previous_config, **config,
                              CAPACITY_KEY: normalize_capacity(config[CAPACITY_KEY])}
                for key in (CAPACITY_KEY, "max_volume"):
                    if key in previous_config and key not in config:
                        config = {**config, key: previous_config[key]}
                previous_capacity = material_capacity(conn, material_uuid)["capacity"]
                if config != previous_config or capacity_specified:
                    assert_inventory_mutation_unclaimed(conn, material_uuids=(material_uuid,))
                    validate_material_config(conn, material_uuid, config,
                                             replace_loading_limits=capacity_specified,
                                             validate_stock=(capacity_specified or
                                                             config.get("max_volume") != previous_config.get("max_volume")))
                name_changed = (
                    name != str(current["name"] or "")
                    or parent_uuid != current["parent_uuid"]
                )
                name_before = str(current["name"] or "")
                parent_before = current["parent_uuid"]
                conn.execute(
                    """
                    UPDATE material SET parent_uuid=?,barcode=?,name=?,description=?,
                        meta_data=?,config=?,update_time=?,
                        aggregate_version=aggregate_version+1
                    WHERE uuid=? AND deleted_at IS NULL
                    """,
                    (
                        parent_uuid,
                        barcode,
                        name,
                        description,
                        _dump(meta_data),
                        _dump(config),
                        _now(),
                        material_uuid,
                    ),
                )
                if name_changed:
                    from unilabos.app.scheduler.inventory.material_io import (
                        _record_material_names,
                    )

                    _record_material_names(
                        conn,
                        material_uuids=(material_uuid,),
                        event_type="material.update",
                        changes={
                            "name_before": name_before,
                            "name_after": name,
                            "parent_uuid_before": parent_before,
                            "parent_uuid_after": parent_uuid,
                        },
                    )
                if config != previous_config or capacity_specified:
                    from unilabos.app.scheduler.inventory.reagent_contract import BackendReagentService

                    BackendReagentService(self.store, edge_id=self.edge_id, lab_id=self.lab_id).record_material_capacity_change(
                        conn, material_uuid, previous_capacity, replace_loading_limits=capacity_specified,
                        configuration_changed=(normalize_capacity(previous_config.get(CAPACITY_KEY))
                                               != normalize_capacity(config.get(CAPACITY_KEY))),
                    )
                if values.get("_relative_position_specified"):
                    if values.get("relative_position") is None:
                        _clear_material_pose(conn, material_uuid)
                    else:
                        self._upsert_relative_position(
                            conn, material_uuid, values["relative_position"]
                        )
                if placement:
                    self._apply_site_placement(
                        conn, material_uuid, template_uuid, placement
                    )
        except InventoryMutationConflict as error:
            raise BackendContractError(
                MATERIAL_ACTIVE_CLAIM_CONFLICT,
                str(error),
            ) from error
        except BackendContractError:
            raise
        except sqlite3.IntegrityError as exc:
            raise BackendContractError(
                MATERIAL_IDENTITY_CONFLICT,
                "Material barcode or root device name conflicts with an existing material",
            ) from exc
        self._notify_material_changed(material_uuid, "updated")
        return self.get_material(material_uuid)

    def delete_material(
        self, material_uuid: str, *, scope: str = "material_only"
    ) -> None:
        """删除空闲物料；``material_with_contents`` 时连子物料与内容一起软删。"""

        normalized_scope = str(scope or "material_only").strip().lower()
        if normalized_scope not in {"material_only", "material_with_contents"}:
            raise BackendContractError(
                INVALID_PARAMETER,
                "scope must be material_only or material_with_contents",
            )
        affected = [material_uuid]
        try:
            with self.store.transaction() as conn:
                self._require_material(conn, material_uuid)
                self._assert_material_current(conn, material_uuid)
                now = _now()
                if normalized_scope == "material_with_contents":
                    try:
                        affected = self._material_io.dispose_contents(
                            conn, material_uuid=material_uuid, now=now
                        )
                    except MaterialIOError as error:
                        raise self._material_io_contract_error(error) from error
                else:
                    assert_inventory_mutation_unclaimed(
                        conn,
                        material_uuids=(material_uuid,),
                    )
                    linked = conn.execute(
                    """
                    SELECT 1 FROM material
                    WHERE parent_uuid=? AND deleted_at IS NULL
                    UNION ALL
                    SELECT 1 FROM site
                    WHERE deleted_at IS NULL
                      AND (material_uuid=? OR occupied_material_uuid=?)
                    UNION ALL
                    SELECT 1 FROM container_content
                    WHERE material_uuid=? AND deleted_at IS NULL
                    LIMIT 1
                    """,
                    (
                        material_uuid,
                        material_uuid,
                        material_uuid,
                        material_uuid,
                    ),
                ).fetchone()
                    if linked:
                        raise BackendContractError(
                            DATABASE_CONFLICT,
                            "Material is referenced by a child, Site, or container content",
                        )
                    from unilabos.app.scheduler.inventory.material_io import (
                        _record_material_names,
                        assert_no_foreign_shared_source_binding,
                        retire_deleted_material_inventory,
                    )

                    # 与 material_with_contents 分支一致：公开删除没有任务身份，
                    # 任何任务的活动共享来源绑定都阻塞
                    try:
                        assert_no_foreign_shared_source_binding(
                            conn, material_uuids=(material_uuid,)
                        )
                    except MaterialIOError as error:
                        raise self._material_io_contract_error(error) from error
                    _record_material_names(
                        conn,
                        material_uuids=(material_uuid,),
                        event_type="material.unload",
                        quantity_delta=-1,
                    )
                    conn.execute(
                        "UPDATE material SET deleted_at=?,update_time=?,"
                        "pose_uuid=NULL,pose_description=NULL,pose_meta_data='{}',"
                        "position_x=NULL,position_y=NULL,position_z=NULL,"
                        "depth=NULL,length=NULL,width=NULL,"
                        "scale_x=NULL,scale_y=NULL,scale_z=NULL,"
                        "rotation_x=NULL,rotation_y=NULL,rotation_z=NULL "
                        "WHERE uuid=?",
                        (now, now, material_uuid),
                    )
                    # 软删除即退出有效库存：状态收敛为 discarded 并递增聚合版本，
                    # 避免公开实例列表与后续状态写入把它当作在库物料
                    retire_deleted_material_inventory(
                        conn, material_uuids=(material_uuid,)
                    )
        except InventoryMutationConflict as error:
            raise BackendContractError(
                MATERIAL_ACTIVE_CLAIM_CONFLICT,
                str(error),
            ) from error
        for identity in affected:
            self._notify_material_changed(identity, "deleted")

    def material_graph(self, *, inventory_scope: str = "current", material_uuids: list[str] | None = None) -> Dict[str, Any]:
        """按物理在站范围读取物料图，历史记录仍保留原身份和内部结构。"""
        scope_predicate = _inventory_scope_predicate(inventory_scope)
        parameters = tuple(dict.fromkeys(material_uuids or []))
        if material_uuids is not None:
            if not parameters:
                return {"nodes": []}
            scope_predicate += " AND material.uuid IN (" + ",".join("?" for _ in parameters) + ")"
        materials = self.store.query_all(
            f"SELECT material.*,{_INVENTORY_PRESENCE_SQL},"
            "resource_template.resource_type AS template_resource_type,"
            "resource_template.meta_data AS capacity_template_meta "
            "FROM material "
            "LEFT JOIN resource_template ON resource_template.uuid=material.resource_template_uuid "
            f"WHERE material.deleted_at IS NULL AND {scope_predicate} "
            "ORDER BY material.create_time,material.uuid", parameters,
        )
        return {
            "nodes": [
                {
                    "material": self._material_graph_row(material),
                    "relative_position": self._relative_position_for_material(
                        material["uuid"]
                    ),
                    "sites": self.list_sites(material["uuid"]),
                    "current_site_uuid": self._current_site_uuid(material["uuid"]),
                    "handles": self._resource_template_handles(
                        material["resource_template_uuid"]
                    ),
                    "resource_template": self._resource_template_summary(
                        material["resource_template_uuid"]
                    ),
                }
                for material in materials
            ]
        }

    # Site and state ----------------------------------------------------

    def resolve_site_identities(
        self, source_node_ids: List[str]
    ) -> Dict[str, Dict[str, str]]:
        """批量解析资源图业务 ID 对应的活动库位（Site）UUID。

        参数：``source_node_ids`` 是 Edge 资源树中的库位业务 ID；空白会被去除，
        重复值只解析一次。返回：请求范围内已找到的 ID 到稳定 Site UUID 映射，
        未知 ID 不出现在结果中。异常：请求超过批量上限，或一个 ID 对应多个活动
        Site UUID 时抛出 ``BackendContractError`` 并关闭式失败。
        """

        wanted = {
            str(source_node_id or "").strip()
            for source_node_id in source_node_ids
            if str(source_node_id or "").strip()
        }
        if len(wanted) > MAX_SITE_IDENTITY_RESOLUTION_IDS:
            raise BackendContractError(
                INVALID_PARAMETER,
                "source_node_ids exceeds the batch limit",
            )
        if not wanted:
            return {"site_uuids_by_source_node_id": {}}

        resolved: Dict[str, str] = {}
        # 该端点只读取身份解析所需的活动 Site 窄投影，避免注册阶段下载完整物料图。
        rows = self.store.query_all(
            "SELECT uuid,name,meta_data FROM site "
            "WHERE deleted_at IS NULL ORDER BY uuid"
        )
        for row in rows:
            meta_data = _json(row.get("meta_data"), {})
            source_node_id = ""
            if isinstance(meta_data, dict):
                source_node_id = str(meta_data.get("source_node_id") or "").strip()
            # 兼容尚未写入 source_node_id 的旧 Site 行；新数据始终使用元数据身份。
            if not source_node_id:
                source_node_id = str(row.get("name") or "").strip()
            if source_node_id not in wanted:
                continue
            site_uuid = str(row.get("uuid") or "").strip()
            previous = resolved.get(source_node_id)
            if previous is not None and previous != site_uuid:
                raise BackendContractError(
                    RESOURCE_DATA_CONFLICT,
                    f"source_node_id maps to multiple active Sites: {source_node_id}",
                )
            resolved[source_node_id] = site_uuid
        return {
            "site_uuids_by_source_node_id": {
                source_node_id: resolved[source_node_id]
                for source_node_id in sorted(resolved)
            }
        }

    def list_all_sites(
        self,
        *,
        page: int = 1,
        page_size: int = 20,
        keyword: str = "",
        device_uuid: Optional[str] = None,
        material_uuid: Optional[str] = None,
        occupied: Optional[bool] = None,
    ) -> Dict[str, Any]:
        """分页读取工站当前全部库位及最近设备祖先。

        ``material_uuid`` 筛选库位的直接载体；``occupied`` 仅表示物理占位，
        不推断调度预留或可用性。设备归属沿现有组成父链确定，独立载体返回空值。
        过滤后按载体 UUID、库位顺序及库位 UUID 稳定排序，多取一条判断后续页。
        """

        page = max(page, 1)
        page_size = 20 if page_size <= 0 else min(page_size, 100)
        where = ["site.deleted_at IS NULL"]
        values: List[Any] = []
        if keyword:
            # 将通配符视为普通字符，避免名称中的 % 或 _ 扩大匹配范围。
            pattern = "%" + keyword.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            where.append(
                "(LOWER(site.name) LIKE LOWER(?) ESCAPE '\\' "
                "OR LOWER(owner.name) LIKE LOWER(?) ESCAPE '\\' "
                "OR LOWER(device.device_name) LIKE LOWER(?) ESCAPE '\\')"
            )
            values.extend([pattern] * 3)
        if device_uuid is not None:
            where.append("device.device_uuid = ?")
            values.append(device_uuid)
        if material_uuid is not None:
            where.append("site.material_uuid = ?")
            values.append(material_uuid)
        if occupied is not None:
            where.append(
                "site.occupied_material_uuid IS NOT NULL" if occupied
                else "site.occupied_material_uuid IS NULL"
            )
        rows = self.store.query_all(
            f"""
            WITH RECURSIVE current_materials AS (
                SELECT material.uuid, material.name, material.parent_uuid, material.type
                FROM material
                WHERE material.deleted_at IS NULL AND {_inventory_scope_predicate("current")}
            ), owning_devices(material_uuid, device_uuid, device_name) AS (
                SELECT uuid, uuid, name FROM current_materials WHERE type='device'
                UNION ALL
                SELECT child.uuid, parent.device_uuid, parent.device_name
                FROM current_materials AS child
                JOIN owning_devices AS parent ON child.parent_uuid=parent.material_uuid
                WHERE child.type<>'device'
            )
            SELECT site.uuid, site.name, site.material_uuid,
                   owner.name AS material_name,
                   device.device_uuid, device.device_name,
                   site.occupied_material_uuid, occupant.name AS occupied_material_name,
                   site.allowed_resource_template_uuids
            FROM site
            JOIN current_materials AS owner ON owner.uuid=site.material_uuid
            LEFT JOIN owning_devices AS device ON device.material_uuid=owner.uuid
            LEFT JOIN material AS occupant
              ON occupant.uuid=site.occupied_material_uuid AND occupant.deleted_at IS NULL
            WHERE {' AND '.join(where)}
            ORDER BY owner.uuid, site.sort_order, site.uuid
            LIMIT ? OFFSET ?
            """,
            (*values, page_size + 1, (page - 1) * page_size),
        )
        items = []
        for row in rows[:page_size]:
            item = dict(row)
            item["allowed_resource_template_uuids"] = _json(
                item["allowed_resource_template_uuids"], []
            )
            items.append(item)
        return {
            "items": items,
            "has_more": len(rows) > page_size,
            "page": page,
            "page_size": page_size,
        }

    def list_sites(self, material_uuid: str) -> List[Dict[str, Any]]:
        rows = self.store.query_all(
            "SELECT * FROM site WHERE material_uuid=? AND deleted_at IS NULL "
            "ORDER BY sort_order,create_time,uuid",
            (material_uuid,),
        )
        return [self._site_row(row) for row in rows]

    def get_site(self, site_uuid: str) -> Dict[str, Any]:
        row = self.store.query_one(
            "SELECT * FROM site WHERE uuid=? AND deleted_at IS NULL", (site_uuid,)
        )
        if row is None:
            raise BackendContractError(
                MATERIAL_SITE_NOT_FOUND, "Material site not found"
            )
        return self._site_row(row)

    def append_material_state(
        self, material_uuid: str, values: Dict[str, Any]
    ) -> Dict[str, Any]:
        state_data = values.get("state_data")
        if not isinstance(state_data, dict) or not state_data:
            raise BackendContractError(INVALID_PARAMETER, "state_data is required")
        state_uuid = str(uuid4())
        observed_at = values.get("observed_at") or _now()
        now = _now()
        with self.store.transaction() as conn:
            self._require_material(conn, material_uuid)
            self._assert_material_current(conn, material_uuid)
            conn.execute(
                """
                INSERT INTO material_state_history(
                    uuid,create_time,update_time,deleted_at,description,meta_data,
                    material_uuid,status,state_data,source,observed_at
                ) VALUES (?,?,?,NULL,?,?,?,?,?,?,?)
                """,
                (
                    state_uuid,
                    now,
                    now,
                    values.get("description"),
                    _dump(values.get("meta_data") or {}),
                    material_uuid,
                    _optional(values.get("status")),
                    _dump(state_data),
                    _optional(values.get("source")),
                    observed_at,
                ),
            )
            conn.execute(
                "UPDATE material SET data=?,update_time=?,"
                "aggregate_version=aggregate_version+1 WHERE uuid=?",
                (_dump(state_data), now, material_uuid),
            )
        return self.get_material_state(state_uuid)

    def get_material_state(self, state_uuid: str) -> Dict[str, Any]:
        row = self.store.query_one(
            "SELECT * FROM material_state_history WHERE uuid=? AND deleted_at IS NULL",
            (state_uuid,),
        )
        if row is None:
            raise BackendContractError(MATERIAL_NOT_FOUND, "Material state not found")
        return self._state_row(row)

    def list_material_states(
        self,
        material_uuid: str,
        *,
        before_time: Optional[str],
        before_uuid: Optional[str],
        limit: int,
    ) -> Dict[str, Any]:
        self.get_material(material_uuid)
        limit = 20 if limit <= 0 else min(limit, 100)
        where = ["material_uuid=?", "deleted_at IS NULL"]
        values: List[Any] = [material_uuid]
        if before_time and before_uuid:
            where.append("(observed_at < ? OR (observed_at = ? AND uuid < ?))")
            values.extend((before_time, before_time, before_uuid))
        rows = self.store.query_all(
            "SELECT * FROM material_state_history "
            f"WHERE {' AND '.join(where)} "
            "ORDER BY observed_at DESC,uuid DESC LIMIT ?",
            (*values, limit),
        )
        items = [self._state_row(row) for row in rows]
        return {
            "items": items,
            "next_before_time": items[-1]["observed_at"] if items else None,
            "next_before_uuid": items[-1]["uuid"] if items else None,
        }

    def latest_material_state(self, material_uuid: str) -> Dict[str, Any]:
        self.get_material(material_uuid)
        row = self.store.query_one(
            "SELECT * FROM material_state_history WHERE material_uuid=? "
            "AND deleted_at IS NULL ORDER BY observed_at DESC,uuid DESC LIMIT 1",
            (material_uuid,),
        )
        if row is None:
            raise BackendContractError(MATERIAL_NOT_FOUND, "Material state not found")
        return self._state_row(row)

    # Internal invariants ----------------------------------------------

    @staticmethod
    def _reconcile_resource_handles(
        conn: sqlite3.Connection,
        template_uuid: str,
        handles: List[Dict[str, Any]],
    ) -> None:
        seen: set[tuple[str, str]] = set()
        retained: List[str] = []
        for handle in handles:
            name = str(handle.get("handler_key") or "").strip()
            io_type = str(handle.get("io_type") or "").strip()
            handle_type = str(handle.get("data_type") or "").strip()
            if (
                not name
                or not handle_type
                or io_type not in {"source", "target", "bidirectional"}
            ):
                raise BackendContractError(
                    TEMPLATE_DEFINITION_INVALID,
                    "resource handle requires handler_key, data_type, and valid io_type",
                )
            business_key = (io_type, name)
            if business_key in seen:
                raise BackendContractError(
                    TEMPLATE_DEFINITION_INVALID,
                    f"duplicate {io_type} resource handle {name}",
                )
            seen.add(business_key)
            existing = conn.execute(
                "SELECT uuid FROM resource_handle_template "
                "WHERE resource_template_uuid=? AND io_type=? AND name=?",
                (template_uuid, io_type, name),
            ).fetchone()
            handle_uuid = str(existing["uuid"]) if existing else str(uuid4())
            now = _now()
            conn.execute(
                """
                INSERT INTO resource_handle_template(
                    uuid,create_time,update_time,deleted_at,description,meta_data,
                    resource_template_uuid,name,display_name,type,io_type,
                    source,key,side
                ) VALUES (?,?,?,NULL,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(uuid) DO UPDATE SET
                    update_time=excluded.update_time,
                    deleted_at=NULL,
                    description=excluded.description,
                    meta_data=excluded.meta_data,
                    display_name=excluded.display_name,
                    type=excluded.type,
                    source=excluded.source,
                    key=excluded.key,
                    side=excluded.side
                """,
                (
                    handle_uuid,
                    now,
                    now,
                    _optional(handle.get("description")),
                    _dump({}),
                    template_uuid,
                    name,
                    str(handle.get("label") or name),
                    handle_type,
                    io_type,
                    _optional(handle.get("data_source")),
                    _optional(handle.get("data_key")),
                    _optional(handle.get("side")),
                ),
            )
            retained.append(handle_uuid)
        if retained:
            markers = ",".join("?" for _ in retained)
            conn.execute(
                "UPDATE resource_handle_template SET deleted_at=?,update_time=? "
                "WHERE resource_template_uuid=? AND deleted_at IS NULL "
                f"AND uuid NOT IN ({markers})",
                (_now(), _now(), template_uuid, *retained),
            )
        else:
            conn.execute(
                "UPDATE resource_handle_template SET deleted_at=?,update_time=? "
                "WHERE resource_template_uuid=? AND deleted_at IS NULL",
                (_now(), _now(), template_uuid),
            )

    @staticmethod
    def _assert_material_current(conn: sqlite3.Connection, material_uuid: str) -> None:
        """与读范围共用物理离站判据，在写事务内保持历史只读。"""
        historical = conn.execute(
            f"SELECT 1 FROM material WHERE uuid=? AND {_DEPARTED_MATERIAL_SQL}",
            (material_uuid,),
        ).fetchone()
        if historical is not None:
            raise BackendContractError(
                MATERIAL_ACTIVE_CLAIM_CONFLICT, "离站或已取消入站的历史物料只读"
            )

    @staticmethod
    def _require_material(
        conn: sqlite3.Connection, material_uuid: str, code: int = MATERIAL_NOT_FOUND
    ) -> sqlite3.Row:
        row = conn.execute(
            "SELECT * FROM material WHERE uuid=? AND deleted_at IS NULL",
            (material_uuid,),
        ).fetchone()
        if row is None:
            raise BackendContractError(code, "Material not found")
        return row

    @staticmethod
    def _material_io_contract_error(error: MaterialIOError) -> BackendContractError:
        """把 Material I/O 领域错误映射到既有 Backend 数值合同。"""

        code = {
            "invalid_parameter": INVALID_PARAMETER,
            "template_not_found": MATERIAL_TEMPLATE_NOT_FOUND,
            "initial_template_not_found": MATERIAL_TEMPLATE_NOT_FOUND,
            "template_invalid": TEMPLATE_DEFINITION_INVALID,
            "mount_not_found": MATERIAL_PARENT_NOT_FOUND,
            "material_not_found": MATERIAL_NOT_FOUND,
            "site_not_found": MATERIAL_SITE_NOT_FOUND,
            "site_occupied": MATERIAL_SITE_OCCUPIED,
            "site_occupancy_conflict": MATERIAL_SITE_OCCUPIED,
            "site_template_not_allowed": MATERIAL_SITE_TEMPLATE_NOT_ALLOWED,
            "active_claim": MATERIAL_ACTIVE_CLAIM_CONFLICT,
            "effect_conflict": RESOURCE_DATA_CONFLICT,
            "effect_corrupt": RESOURCE_DATA_CONFLICT,
            "permit_invalid": RESOURCE_DATA_CONFLICT,
            "reagent_identity_not_found": RESOURCE_NOT_FOUND,
            "reagent_not_found": RESOURCE_NOT_FOUND,
            "reagent_conflict": RESOURCE_DATA_CONFLICT,
            "material_not_empty": DATABASE_CONFLICT,
            "material_graph_conflict": DATABASE_CONFLICT,
            "identity_conflict": MATERIAL_IDENTITY_CONFLICT,
        }.get(error.code, DATABASE_CONFLICT)
        return BackendContractError(code, error.message)

    @staticmethod
    def _upsert_relative_position(
        conn: sqlite3.Connection,
        material_uuid: str,
        position: Dict[str, Any],
    ) -> None:
        existing = conn.execute(
            "SELECT pose_uuid FROM material WHERE uuid=?",
            (material_uuid,),
        ).fetchone()
        position_uuid = (
            str(existing["pose_uuid"])
            if existing is not None and existing["pose_uuid"]
            else str(uuid4())
        )
        now = _now()
        conn.execute(
            """
            UPDATE material SET
                pose_uuid=?, pose_description=?, pose_meta_data=?, update_time=?,
                position_x=?, position_y=?, position_z=?, depth=?, length=?, width=?,
                scale_x=?, scale_y=?, scale_z=?, rotation_x=?, rotation_y=?, rotation_z=?
            WHERE uuid=?
            """,
            (
                position_uuid,
                position.get("description"),
                _dump(position.get("meta_data") or {}),
                now,
                float(position.get("position_x") or 0),
                float(position.get("position_y") or 0),
                float(position.get("position_z") or 0),
                float(position.get("depth") or 0),
                float(position.get("length") or 0),
                float(position.get("width") or 0),
                float(position.get("scale_x", 1)),
                float(position.get("scale_y", 1)),
                float(position.get("scale_z", 1)),
                float(position.get("rotation_x") or 0),
                float(position.get("rotation_y") or 0),
                float(position.get("rotation_z") or 0),
                material_uuid,
            ),
        )

    def _relative_position_for_material(
        self, material_uuid: str
    ) -> Optional[Dict[str, Any]]:
        row = self.store.query_one(_POSE_SELECT, (material_uuid,))
        return self._relative_position_row(row) if row else None

    @staticmethod
    def _check_parent_cycle(
        conn: sqlite3.Connection, material_uuid: str, parent_uuid: str
    ) -> None:
        cursor: Optional[str] = parent_uuid
        seen = {material_uuid}
        while cursor:
            if cursor in seen:
                raise BackendContractError(
                    MATERIAL_PARENT_CYCLE,
                    "Material parent relationship creates a cycle",
                )
            seen.add(cursor)
            row = conn.execute(
                "SELECT parent_uuid FROM material WHERE uuid=? AND deleted_at IS NULL",
                (cursor,),
            ).fetchone()
            cursor = row["parent_uuid"] if row else None

    def _apply_site_placement(
        self,
        conn: sqlite3.Connection,
        material_uuid: str,
        template_uuid: str,
        placement: Dict[str, Any],
    ) -> None:
        """在当前物料写事务中校验并修改精确库位占用。

        参数：``conn`` 是库存事务；其余参数给出物料、模板和 place/remove 命令。
        返回：无；成功时 place 同步父物料并占用目标库位，remove 仅解除精确库位。
        异常：库位不存在、已占用、模板不允许或形成父级环时抛合同错误。
        """

        action = str(placement.get("action") or "")
        site_uuid = _optional_uuid(placement.get("site_uuid"))
        if action == "remove":
            if site_uuid is not None:
                raise BackendContractError(
                    INVALID_PARAMETER, "remove must not provide site_uuid"
                )
            from unilabos.app.scheduler.inventory.material_io import (
                _record_material_names,
            )

            occupied = conn.execute(
                "SELECT uuid,name FROM site WHERE occupied_material_uuid=? "
                "AND deleted_at IS NULL",
                (material_uuid,),
            ).fetchone()
            if occupied is not None:
                _record_material_names(
                    conn,
                    material_uuids=(material_uuid,),
                    event_type="material.move",
                    changes={
                        "site_name_before": str(occupied["name"] or ""),
                        "site_name_after": "",
                    },
                )
            clear_site_occupancy(
                conn,
                material_uuid=material_uuid,
                update_time=_now(),
            )
            return
        if action != "place" or not site_uuid:
            raise BackendContractError(
                INVALID_PARAMETER, "site_placement action must be place or remove"
            )
        site = conn.execute(
            "SELECT * FROM site WHERE uuid=? AND deleted_at IS NULL", (site_uuid,)
        ).fetchone()
        if site is None:
            raise BackendContractError(
                MATERIAL_SITE_NOT_FOUND, "Material site not found"
            )
        if site["material_uuid"] == material_uuid:
            raise BackendContractError(
                MATERIAL_SITE_CYCLE, "Material cannot occupy its own Site"
            )
        allowed = _json(site["allowed_resource_template_uuids"], [])
        if allowed and template_uuid not in allowed:
            raise BackendContractError(
                MATERIAL_SITE_TEMPLATE_NOT_ALLOWED,
                "Material resource template is not allowed by the target site",
            )
        occupied = _optional(site["occupied_material_uuid"])
        if occupied and occupied != material_uuid:
            raise BackendContractError(
                MATERIAL_SITE_OCCUPIED, "Target site is occupied by another material"
            )
        owner_material_uuid = str(site["material_uuid"])
        self._check_parent_cycle(conn, material_uuid, owner_material_uuid)
        previous = conn.execute(
            "SELECT uuid,name FROM site WHERE occupied_material_uuid=? "
            "AND deleted_at IS NULL",
            (material_uuid,),
        ).fetchone()
        timestamp = _now()
        try:
            set_site_occupancy(
                conn,
                site_uuid=site_uuid,
                material_uuid=material_uuid,
                update_time=timestamp,
            )
        except SiteOccupancyConflict as error:
            code = (
                MATERIAL_SITE_CYCLE
                if error.code == "site_occupancy_cycle"
                else MATERIAL_SITE_OCCUPIED
            )
            raise BackendContractError(code, str(error)) from error
        conn.execute(
            "UPDATE material SET parent_uuid=?,update_time=? "
            "WHERE uuid=? AND deleted_at IS NULL",
            (owner_material_uuid, timestamp, material_uuid),
        )
        if previous is not None and str(previous["uuid"]) == site_uuid:
            return
        from unilabos.app.scheduler.inventory.material_io import (
            _record_material_names,
        )

        _record_material_names(
            conn,
            material_uuids=(material_uuid,),
            event_type="material.move",
            changes={
                "site_name_before": str(previous["name"]) if previous else "",
                "site_name_after": str(site["name"] or ""),
            },
        )

    def _current_site_uuid(self, material_uuid: str) -> Optional[str]:
        row = self.store.query_one(
            "SELECT uuid FROM site WHERE occupied_material_uuid=? "
            "AND deleted_at IS NULL",
            (material_uuid,),
        )
        return str(row["uuid"]) if row else None

    @staticmethod
    def _base_row(row: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "uuid": row["uuid"],
            "create_time": row["create_time"],
            "update_time": row["update_time"],
            "description": row.get("description"),
            "meta_data": _json(row.get("meta_data"), {}),
        }

    @classmethod
    def _resource_template_row(cls, row: Dict[str, Any]) -> Dict[str, Any]:
        """把持久行投影为后端形态资源模板（ResourceTemplate）DTO。

        参数：``cls`` 是投影方法所属服务类，``row`` 是 SQLite 查询得到的资源模板
        行。返回：JSON 字段已解码且 ``available_sites`` 缺省为空数组的独立字典；
        本方法不创建实例库位（Site）。
        """

        result = cls._base_row(row)
        for field in (
            "name",
            "display_name",
            "resource_type",
            "header",
            "footer",
            "icon",
            "module",
            "language",
            "cover",
            "manufacturer_uuid",
        ):
            result[field] = row.get(field)
        for field, fallback in (
            ("model", {}),
            ("tags", []),
            ("data_schema", {}),
            ("config_schema", {}),
            ("pose", {}),
            ("config_info", []),
            ("available_sites", []),
            ("scene", []),
            ("device_params", {}),
            ("ui_overlay", {}),
        ):
            result[field] = _json(row.get(field), fallback)
        return result

    @classmethod
    def _resource_handle_row(cls, row: Dict[str, Any]) -> Dict[str, Any]:
        result = cls._base_row(row)
        result.update(
            {
                "resource_template_uuid": row["resource_template_uuid"],
                "name": row["name"],
                "display_name": row["display_name"],
                "type": row["type"],
                "io_type": row["io_type"],
            }
        )
        for field in ("source", "key", "side"):
            if row.get(field) is not None:
                result[field] = row[field]
        return result

    @classmethod
    def _material_row(cls, row: Dict[str, Any]) -> Dict[str, Any]:
        """把 SQLite 物料行投影为 Backend 公共物料 DTO。

        参数：``row`` 是物料行，可选携带聚合修订。返回：JSON
        字段已解码的物料字典；存在修订时同步返回 ``revision``。
        异常：缺少必需行字段时保留原生映射异常。
        """

        result = cls._base_row(row)
        result.update(
            {
                "resource_template_uuid": row["resource_template_uuid"],
                "parent_uuid": row.get("parent_uuid"),
                "class": row["class"],
                "barcode": row["barcode"],
                "name": row["name"],
                "config": _json(row.get("config"), {}),
                "data": _json(row.get("data"), {}),
                "inventory_status": row["inventory_status"],
                "disposition": row["disposition"],
                "inventory_presence": row["inventory_presence"],
            }
        )
        if "aggregate_version" in row:
            result["revision"] = int(row["aggregate_version"])
        result.update(capacity_projection(
            row.get("config"), row.get("data"), row.get("capacity_template_meta")
        ))
        return result

    @classmethod
    def _material_graph_row(cls, row: Dict[str, Any]) -> Dict[str, Any]:
        """Project the authoritative Backend-shaped Material used by graph reads."""

        result = cls._material_row(row)
        result["type"] = str(row.get("type") or row.get("template_resource_type") or "")
        result["revision"] = int(row["aggregate_version"])
        return result

    def _resource_template_summary(self, template_uuid: str) -> Dict[str, Any]:
        runtime_catalog = self.store.runtime_device_template_catalog
        if runtime_catalog is not None:
            detail = runtime_catalog.get(template_uuid)
            if detail is not None:
                return self._resource_template_summary_from_detail(detail, include_tags=True)
        row = self.store.query_one(
            "SELECT uuid,name,display_name,resource_type,icon,tags FROM resource_template "
            "WHERE uuid=? AND deleted_at IS NULL",
            (template_uuid,),
        )
        if row is None:
            raise BackendContractError(RESOURCE_TEMPLATE_NOT_FOUND, "Resource template not found")
        result = {
            "uuid": row["uuid"],
            "name": row["name"],
            "display_name": row["display_name"],
            "resource_type": row["resource_type"],
            "tags": _json(row.get("tags"), []),
        }
        if row.get("icon") is not None:
            result["icon"] = row["icon"]
        return result

    @staticmethod
    def _resource_template_summary_from_detail(
        detail: Dict[str, Any],
        *,
        include_tags: bool = False,
    ) -> Dict[str, Any]:
        """从内存设备模板详情构造原有列表或物料图摘要。"""

        result = {
            "uuid": detail["uuid"],
            "name": detail["name"],
            "display_name": detail["display_name"],
            "resource_type": detail["resource_type"],
        }
        if include_tags:
            result["tags"] = list(detail.get("tags") or [])
        if detail.get("icon") is not None:
            result["icon"] = detail["icon"]
        return result

    def _resource_template_handles(self, template_uuid: str) -> List[Dict[str, Any]]:
        """从内存设备目录或 SQLite 物料模板读取资源 Handle。"""

        runtime_catalog = self.store.runtime_device_template_catalog
        if runtime_catalog is not None:
            detail = runtime_catalog.get(template_uuid)
            if detail is not None:
                return list(detail.get("handles") or [])
        return [
            self._resource_handle_row(handle)
            for handle in self.store.query_all(
                "SELECT * FROM resource_handle_template "
                "WHERE resource_template_uuid=? AND deleted_at IS NULL "
                "ORDER BY io_type,name,uuid",
                (template_uuid,),
            )
        ]

    @classmethod
    def _site_row(cls, row: Dict[str, Any]) -> Dict[str, Any]:
        result = cls._base_row(row)
        result.update(
            {
                "material_uuid": row["material_uuid"],
                "name": row["name"],
                "sort_order": int(row["sort_order"]),
                "allowed_resource_template_uuids": _json(
                    row.get("allowed_resource_template_uuids"), []
                ),
                "occupied_material_uuid": row.get("occupied_material_uuid"),
                "position_x": float(row["position_x"]),
                "position_y": float(row["position_y"]),
                "position_z": float(row["position_z"]),
                "depth": float(row["depth"]),
                "length": float(row["length"]),
                "width": float(row["width"]),
            }
        )
        return result

    @classmethod
    def _relative_position_row(cls, row: Dict[str, Any]) -> Dict[str, Any]:
        result = cls._base_row(row)
        result["material_uuid"] = row["material_uuid"]
        for field in (
            "position_x",
            "position_y",
            "position_z",
            "depth",
            "length",
            "width",
            "scale_x",
            "scale_y",
            "scale_z",
            "rotation_x",
            "rotation_y",
            "rotation_z",
        ):
            result[field] = float(row[field])
        return result

    @classmethod
    def _state_row(cls, row: Dict[str, Any]) -> Dict[str, Any]:
        result = cls._base_row(row)
        result.update(
            {
                "material_uuid": row["material_uuid"],
                "status": row.get("status"),
                "state_data": _json(row.get("state_data"), {}),
                "source": row.get("source"),
                "observed_at": row["observed_at"],
            }
        )
        return result


__all__ = [
    "BackendContractError",
    "BackendResourceService",
    "MAX_SITE_IDENTITY_RESOLUTION_IDS",
]
