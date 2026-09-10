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

from unilabos.app.scheduler.inventory.store import InventoryStore


class BackendContractError(RuntimeError):
    """使用后端（Backend）数值响应合同编码的业务错误。"""

    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


INVALID_PARAMETER = 1000
DATABASE_CONFLICT = 2005
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


class BackendResourceService:
    """提供资源模板（ResourceTemplate）、物料（Material）与库位（Site）权威写入。"""

    def __init__(self, store: InventoryStore):
        self.store = store

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
        # ``identities`` 只记录本次事务最终采用的活动资源模板稳定身份。
        identities: List[Dict[str, str]] = []
        try:
            with self.store.transaction() as conn:
                for resource, name in zip(resources, normalized_names):
                    # ``existing`` 只能是当前活动业务 ID；软删除历史不得被复活。
                    existing = conn.execute(
                        "SELECT uuid,meta_data FROM resource_template "
                        "WHERE name = ? AND deleted_at IS NULL",
                        (name,),
                    ).fetchone()
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
                    if source_uri:
                        meta_data["unilab"] = {
                            **_json(meta_data.get("unilab"), {}),
                            "source_uri": source_uri,
                        }
                    # ``class_definition`` 是当前模板冻结的 Python 实现身份合同。
                    class_definition = resource.get("class") or {}
                    # ``schema`` 是资源模板初始化参数的数据/配置命名空间合同。
                    schema = resource.get("init_param_schema") or {}
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
                            config_info, cover, scene, device_params, ui_overlay
                        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
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
                            cover=excluded.cover,
                            scene=excluded.scene,
                            device_params=excluded.device_params
                        """,
                        values,
                    )
                    conn.execute(
                        """
                        INSERT INTO resource_template_inventory(
                            resource_template_uuid, aggregate_version
                        ) VALUES (?,1)
                        ON CONFLICT(resource_template_uuid) DO UPDATE SET
                            aggregate_version=aggregate_version+1
                        """,
                        (template_uuid,),
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
        limit: int,
        cursor_uuid: Optional[str],
        keyword: str,
        resource_type: str,
    ) -> Dict[str, Any]:
        limit = 20 if limit <= 0 else min(limit, 100)
        where = ["deleted_at IS NULL"]
        values: List[Any] = []
        if cursor_uuid:
            where.append("uuid > ?")
            values.append(cursor_uuid)
        if keyword:
            where.append("(name LIKE ? OR display_name LIKE ?)")
            values.extend((f"%{keyword}%", f"%{keyword}%"))
        if resource_type:
            where.append("resource_type = ?")
            values.append(resource_type)
        rows = self.store.query_all(
            "SELECT uuid,name,display_name,resource_type,tags,meta_data "
            f"FROM resource_template WHERE {' AND '.join(where)} "
            "ORDER BY uuid LIMIT ?",
            (*values, limit + 1),
        )
        page = rows[:limit]
        return {
            "items": [
                {
                    "uuid": row["uuid"],
                    "name": row["name"],
                    "display_name": row["display_name"],
                    "resource_type": row["resource_type"],
                    "tags": _json(row["tags"], []),
                    "source_uri": _json(row["meta_data"], {})
                    .get("unilab", {})
                    .get("source_uri"),
                }
                for row in page
            ],
            "has_more": len(rows) > limit,
            "next_cursor_uuid": page[-1]["uuid"] if len(rows) > limit else None,
        }

    def get_resource_template(self, template_uuid: str) -> Dict[str, Any]:
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
        with self.store.transaction() as conn:
            row = conn.execute(
                "SELECT uuid FROM resource_template WHERE uuid=? AND deleted_at IS NULL",
                (template_uuid,),
            ).fetchone()
            if row is None:
                raise BackendContractError(
                    RESOURCE_TEMPLATE_NOT_FOUND, "Resource template not found"
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

    def create_material(self, values: Dict[str, Any]) -> Dict[str, Any]:
        material_uuid = str(uuid4())
        template_uuid = str(values.get("resource_template_uuid") or "")
        parent_uuid = _optional(values.get("parent_uuid"))
        barcode = str(values.get("barcode") or "")
        name = str(values.get("name") or "").strip()
        if not template_uuid or not name:
            raise BackendContractError(
                INVALID_PARAMETER, "resource_template_uuid and name are required"
            )
        try:
            with self.store.transaction() as conn:
                template = conn.execute(
                    "SELECT resource_type FROM resource_template "
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
                now = _now()
                conn.execute(
                    """
                    INSERT INTO material(
                        uuid,create_time,update_time,deleted_at,description,meta_data,
                        resource_template_uuid,parent_uuid,class,barcode,name,config,data
                    ) VALUES (?,?,?,NULL,?,?,?,?,?,?,?,?,?)
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
                        barcode,
                        name,
                        _dump(values.get("config") or {}),
                        _dump({}),
                    ),
                )
                conn.execute(
                    "INSERT INTO material_inventory(material_uuid,legacy_template_id) "
                    "VALUES (?,?)",
                    (material_uuid, template_uuid),
                )
                if values.get("relative_position") is not None:
                    self._upsert_relative_position(
                        conn, material_uuid, values["relative_position"]
                    )
                placement = values.get("site_placement")
                if placement:
                    self._apply_site_placement(
                        conn, material_uuid, template_uuid, placement
                    )
        except BackendContractError:
            raise
        except sqlite3.IntegrityError as exc:
            raise BackendContractError(
                MATERIAL_IDENTITY_CONFLICT,
                "Material barcode or sibling name conflicts with an existing material",
            ) from exc
        result = self.get_material(material_uuid)
        result["children"] = []
        return result

    def list_materials(
        self,
        *,
        page: int,
        page_size: int,
        name: str,
        barcode: str,
        resource_template_uuid: Optional[str],
    ) -> Dict[str, Any]:
        page = max(page, 1)
        page_size = 20 if page_size <= 0 else min(page_size, 100)
        where = ["deleted_at IS NULL"]
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
        predicate = " AND ".join(where)
        total = self.store.query_one(
            f"SELECT COUNT(*) AS count FROM material WHERE {predicate}", tuple(values)
        )
        rows = self.store.query_all(
            f"SELECT * FROM material WHERE {predicate} "
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
        row = self.store.query_one(
            "SELECT * FROM material WHERE uuid=? AND deleted_at IS NULL",
            (material_uuid,),
        )
        if row is None:
            raise BackendContractError(MATERIAL_NOT_FOUND, "Material not found")
        result = self._material_row(row)
        position = self.store.query_one(
            "SELECT * FROM relative_position "
            "WHERE material_uuid=? AND deleted_at IS NULL",
            (material_uuid,),
        )
        result["relative_position"] = (
            self._relative_position_row(position) if position else None
        )
        result["sites"] = self.list_sites(material_uuid)
        current_site = self.store.query_one(
            "SELECT * FROM site WHERE occupied_material_uuid=? AND deleted_at IS NULL",
            (material_uuid,),
        )
        result["current_site"] = self._site_row(current_site) if current_site else None
        return result

    def update_material(
        self, material_uuid: str, values: Dict[str, Any]
    ) -> Dict[str, Any]:
        try:
            with self.store.transaction() as conn:
                current = self._require_material(conn, material_uuid)
                template_uuid = str(values.get("resource_template_uuid") or "")
                if template_uuid != current["resource_template_uuid"]:
                    raise BackendContractError(
                        MATERIAL_TEMPLATE_IMMUTABLE,
                        "Resource template of an existing material cannot be changed",
                    )
                parent_uuid = _optional(values.get("parent_uuid"))
                if parent_uuid:
                    self._require_material(conn, parent_uuid, MATERIAL_PARENT_NOT_FOUND)
                    self._check_parent_cycle(conn, material_uuid, parent_uuid)
                conn.execute(
                    """
                    UPDATE material SET parent_uuid=?,barcode=?,name=?,description=?,
                        meta_data=?,config=?,update_time=?
                    WHERE uuid=? AND deleted_at IS NULL
                    """,
                    (
                        parent_uuid,
                        str(values.get("barcode") or ""),
                        str(values.get("name") or "").strip(),
                        values.get("description"),
                        _dump(values.get("meta_data") or {}),
                        _dump(values.get("config") or {}),
                        _now(),
                        material_uuid,
                    ),
                )
                if values.get("_relative_position_specified"):
                    if values.get("relative_position") is None:
                        conn.execute(
                            "UPDATE relative_position SET deleted_at=?,update_time=? "
                            "WHERE material_uuid=? AND deleted_at IS NULL",
                            (_now(), _now(), material_uuid),
                        )
                    else:
                        self._upsert_relative_position(
                            conn, material_uuid, values["relative_position"]
                        )
                placement = values.get("site_placement")
                if placement:
                    self._apply_site_placement(
                        conn, material_uuid, template_uuid, placement
                    )
                conn.execute(
                    "UPDATE material_inventory SET aggregate_version=aggregate_version+1 "
                    "WHERE material_uuid=?",
                    (material_uuid,),
                )
        except BackendContractError:
            raise
        except sqlite3.IntegrityError as exc:
            raise BackendContractError(
                MATERIAL_IDENTITY_CONFLICT,
                "Material barcode or sibling name conflicts with an existing material",
            ) from exc
        return self.get_material(material_uuid)

    def delete_material(self, material_uuid: str) -> None:
        with self.store.transaction() as conn:
            self._require_material(conn, material_uuid)
            linked = conn.execute(
                """
                SELECT 1 FROM material
                WHERE parent_uuid=? AND deleted_at IS NULL
                UNION ALL
                SELECT 1 FROM site
                WHERE deleted_at IS NULL
                  AND (material_uuid=? OR occupied_material_uuid=?)
                LIMIT 1
                """,
                (material_uuid, material_uuid, material_uuid),
            ).fetchone()
            if linked:
                raise BackendContractError(
                    DATABASE_CONFLICT,
                    "Material is referenced by a child or Site",
                )
            now = _now()
            conn.execute(
                "UPDATE relative_position SET deleted_at=?,update_time=? "
                "WHERE material_uuid=? AND deleted_at IS NULL",
                (now, now, material_uuid),
            )
            conn.execute(
                "UPDATE material SET deleted_at=?,update_time=? WHERE uuid=?",
                (now, now, material_uuid),
            )
            conn.execute(
                "UPDATE material_inventory SET aggregate_version=aggregate_version+1 "
                "WHERE material_uuid=?",
                (material_uuid,),
            )

    def material_graph(self) -> Dict[str, Any]:
        materials = self.store.query_all(
            "SELECT * FROM material WHERE deleted_at IS NULL ORDER BY create_time,uuid"
        )
        return {
            "nodes": [
                {
                    "material": self._material_row(material),
                    "relative_position": self._relative_position_for_material(
                        material["uuid"]
                    ),
                    "sites": self.list_sites(material["uuid"]),
                    "current_site_uuid": self._current_site_uuid(material["uuid"]),
                    "handles": [
                        self._resource_handle_row(handle)
                        for handle in self.store.query_all(
                            "SELECT * FROM resource_handle_template "
                            "WHERE resource_template_uuid=? AND deleted_at IS NULL "
                            "ORDER BY io_type,name,uuid",
                            (material["resource_template_uuid"],),
                        )
                    ],
                }
                for material in materials
            ]
        }

    # Site and state ----------------------------------------------------

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
                "UPDATE material SET data=?,update_time=? WHERE uuid=?",
                (_dump(state_data), now, material_uuid),
            )
            conn.execute(
                "UPDATE material_inventory SET aggregate_version=aggregate_version+1 "
                "WHERE material_uuid=?",
                (material_uuid,),
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
    def _upsert_relative_position(
        conn: sqlite3.Connection,
        material_uuid: str,
        position: Dict[str, Any],
    ) -> None:
        existing = conn.execute(
            "SELECT uuid,create_time FROM relative_position WHERE material_uuid=?",
            (material_uuid,),
        ).fetchone()
        position_uuid = str(existing["uuid"]) if existing else str(uuid4())
        create_time = str(existing["create_time"]) if existing else _now()
        now = _now()
        conn.execute(
            """
            INSERT INTO relative_position(
                uuid,create_time,update_time,deleted_at,description,meta_data,
                material_uuid,position_x,position_y,position_z,depth,length,width,
                scale_x,scale_y,scale_z,rotation_x,rotation_y,rotation_z
            ) VALUES (?,?,?,NULL,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(uuid) DO UPDATE SET
                update_time=excluded.update_time,
                deleted_at=NULL,
                description=excluded.description,
                meta_data=excluded.meta_data,
                position_x=excluded.position_x,
                position_y=excluded.position_y,
                position_z=excluded.position_z,
                depth=excluded.depth,
                length=excluded.length,
                width=excluded.width,
                scale_x=excluded.scale_x,
                scale_y=excluded.scale_y,
                scale_z=excluded.scale_z,
                rotation_x=excluded.rotation_x,
                rotation_y=excluded.rotation_y,
                rotation_z=excluded.rotation_z
            """,
            (
                position_uuid,
                create_time,
                now,
                position.get("description"),
                _dump(position.get("meta_data") or {}),
                material_uuid,
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
            ),
        )

    def _relative_position_for_material(
        self, material_uuid: str
    ) -> Optional[Dict[str, Any]]:
        row = self.store.query_one(
            "SELECT * FROM relative_position "
            "WHERE material_uuid=? AND deleted_at IS NULL",
            (material_uuid,),
        )
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

    @staticmethod
    def _apply_site_placement(
        conn: sqlite3.Connection,
        material_uuid: str,
        template_uuid: str,
        placement: Dict[str, Any],
    ) -> None:
        action = str(placement.get("action") or "")
        site_uuid = _optional(placement.get("site_uuid"))
        if action == "remove":
            if site_uuid is not None:
                raise BackendContractError(
                    INVALID_PARAMETER, "remove must not provide site_uuid"
                )
            conn.execute(
                "UPDATE site SET occupied_material_uuid=NULL,update_time=? "
                "WHERE occupied_material_uuid=? AND deleted_at IS NULL",
                (_now(), material_uuid),
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
        conn.execute(
            "UPDATE site SET occupied_material_uuid=NULL,update_time=? "
            "WHERE occupied_material_uuid=? AND deleted_at IS NULL",
            (_now(), material_uuid),
        )
        conn.execute(
            "UPDATE site SET occupied_material_uuid=?,update_time=? WHERE uuid=?",
            (material_uuid, _now(), site_uuid),
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
            }
        )
        return result

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


__all__ = ["BackendContractError", "BackendResourceService"]
