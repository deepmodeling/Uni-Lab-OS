"""Backend-shaped resource module backed by the Edge inventory SQLite.

This is the domain implementation behind the shared frontend Interface.  The
legacy ``/inventory`` adapter remains an Edge-only operational surface; both
adapters write the same canonical ``resource_template/material/site`` rows.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from uuid import uuid4

from unilabos.server.scheduler.inventory.material_type import material_type_from_template
from unilabos.server.scheduler.inventory.site_spec import (
    component_relative_position,
    materialize_component_sites,
    merge_json_objects,
    template_components,
)
from unilabos.server.scheduler.inventory.store import InventoryStore


class BackendContractError(RuntimeError):
    """Business error encoded with the Backend numeric response contract."""

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


_SITE_CONTENT_TYPE_TAG_ALIASES = {
    "bottle": {"bottle", "bottles"},
    "bottles": {"bottle", "bottles"},
    "bottle_carrier": {"bottle_carrier", "bottle_carriers"},
    "bottle_carriers": {"bottle_carrier", "bottle_carriers"},
    "tip_rack": {"tip_rack", "tip_racks"},
    "tip_racks": {"tip_rack", "tip_racks"},
}

_SITE_SELECT = (
    "SELECT site.*, resource_template.name AS template_name FROM site "
    "JOIN material AS owner_material ON owner_material.uuid = site.material_uuid "
    "JOIN resource_template ON resource_template.uuid = "
    "owner_material.resource_template_uuid "
)


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
    """Resource Template, Material, Site and state-history authority."""

    def __init__(self, store: InventoryStore):
        self.store = store

    # Resource Template -------------------------------------------------

    def sync_resource_templates(
        self, resources: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        if not resources:
            raise BackendContractError(
                TEMPLATE_DEFINITION_INVALID, "resources is required"
            )
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
        identities: List[Dict[str, str]] = []
        try:
            with self.store.transaction() as conn:
                for resource, name in zip(resources, normalized_names):
                    existing = conn.execute(
                        "SELECT uuid FROM resource_template WHERE name = ?",
                        (name,),
                    ).fetchone()
                    template_uuid = str(existing["uuid"]) if existing else str(uuid4())
                    class_definition = resource.get("class") or {}
                    schema = resource.get("init_param_schema") or {}
                    data_schema = (schema.get("data") or {}).get("properties") or {}
                    config_schema = (schema.get("config") or {}).get("properties") or {}
                    values = (
                        template_uuid,
                        _now(),
                        _now(),
                        resource.get("description"),
                        _dump({}),
                        name,
                        str(resource.get("display_name") or name),
                        str(resource.get("registry_type") or "resource"),
                        _optional(resource.get("icon")),
                        _dump(resource.get("model") or {}),
                        _optional(class_definition.get("module")),
                        _optional(class_definition.get("type")),
                        _dump(
                            resource.get("tags")
                            if resource.get("tags") is not None
                            else resource.get("category") or []
                        ),
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
            "SELECT uuid,name,display_name,resource_type,tags "
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
        if parent_uuid == "00000000-0000-0000-0000-000000000000":
            parent_uuid = None
        barcode = str(values.get("barcode") or "").strip()
        name = str(values.get("name") or "").strip()
        if not template_uuid or not name:
            raise BackendContractError(
                INVALID_PARAMETER, "resource_template_uuid and name are required"
            )
        material_uuids: List[str] = []
        try:
            with self.store.transaction() as conn:
                template = conn.execute(
                    "SELECT * FROM resource_template "
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
                self._validate_resource_material_root(conn, template, parent_uuid)

                components = template_components(dict(template))
                root_component = components[0] if components else {}
                root_config = merge_json_objects(
                    root_component.get("config"),
                    values.get("config"),
                    protected_fields=("sites",),
                )
                root_data = merge_json_objects(
                    root_component.get("data"), values.get("data")
                )
                now = _now()
                material_class = str(template["name"])
                material_type = material_type_from_template(
                    dict(template),
                    material_name=name,
                    material_class=material_class,
                    root=True,
                )
                self._insert_material_with_initial_state(
                    conn,
                    material_uuid=material_uuid,
                    timestamp=now,
                    description=values.get("description"),
                    meta_data=values.get("meta_data") or {},
                    template_uuid=template_uuid,
                    parent_uuid=parent_uuid,
                    material_class=material_class,
                    material_type=material_type,
                    barcode=barcode,
                    name=name,
                    config=root_config,
                    data=root_data,
                )
                material_uuids.append(material_uuid)

                for ordinal, component in enumerate(components[1:], start=1):
                    component_id = str(component["id"])
                    child_barcode = f"{barcode}/{component_id}" if barcode else ""
                    if len(child_barcode) > 128:
                        raise BackendContractError(
                            INVALID_PARAMETER,
                            f"generated child material barcode exceeds 128 characters: {child_barcode}",
                        )
                    child_uuid = str(uuid4())
                    child_type = str(
                        component.get("type") or template["resource_type"] or "resource"
                    ).strip()
                    self._insert_material_with_initial_state(
                        conn,
                        material_uuid=child_uuid,
                        timestamp=now,
                        description=component.get("description"),
                        meta_data={},
                        template_uuid=template_uuid,
                        parent_uuid=material_uuid,
                        material_class=str(component.get("class") or "").strip(),
                        material_type=child_type or "resource",
                        barcode=child_barcode,
                        name=str(component.get("name") or component_id),
                        config=merge_json_objects(component.get("config"), {}),
                        data=merge_json_objects(component.get("data"), {}),
                    )
                    material_uuids.append(child_uuid)

                for ordinal, current_uuid in enumerate(material_uuids):
                    component = components[ordinal] if components else {}
                    position = component_relative_position(component)
                    if ordinal == 0 and position is None:
                        position = values.get("relative_position")
                    if position is not None:
                        self._upsert_relative_position(conn, current_uuid, position)
                    if components:
                        materialize_component_sites(conn, current_uuid, component)

                placement = values.get("site_placement")
                if placement:
                    self._apply_site_placement(
                        conn, material_uuid, template_uuid, placement
                    )
        except BackendContractError:
            raise
        except ValueError as exc:
            raise BackendContractError(
                INVALID_PARAMETER,
                f"Resource template material definition is invalid: {exc}",
            ) from exc
        except sqlite3.IntegrityError as exc:
            raise BackendContractError(
                MATERIAL_IDENTITY_CONFLICT,
                "Material barcode or sibling name conflicts with an existing material",
            ) from exc
        result = self.get_material(material_uuid)
        result["children"] = [
            self._material_row(
                self.store.query_one(
                    "SELECT * FROM material WHERE uuid=? AND deleted_at IS NULL",
                    (child_uuid,),
                )
            )
            for child_uuid in material_uuids[1:]
        ]
        result["sites"] = [
            self._site_row(site)
            for current_uuid in material_uuids
            for site in self.store.query_all(
                _SITE_SELECT
                + "WHERE site.material_uuid=? AND site.deleted_at IS NULL "
                "ORDER BY site.sort_order,site.create_time,site.uuid",
                (current_uuid,),
            )
        ]
        return result

    def list_materials(
        self,
        *,
        page: int,
        page_size: int,
        name: str,
        barcode: str,
        resource_template_uuid: Optional[str],
        with_children: bool = False,
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
        if not with_children:
            where.append("parent_uuid IS NULL")
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
            _SITE_SELECT
            + "WHERE site.occupied_material_uuid=? AND site.deleted_at IS NULL",
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
                template_uuid = str(current["resource_template_uuid"])
                template = conn.execute(
                    "SELECT * FROM resource_template WHERE uuid=? AND deleted_at IS NULL",
                    (template_uuid,),
                ).fetchone()
                if template is None:
                    raise BackendContractError(
                        MATERIAL_TEMPLATE_NOT_FOUND,
                        "Resource template associated with the material not found",
                    )

                parent_uuid = current["parent_uuid"]
                if "parent_uuid" in values and values["parent_uuid"] is not None:
                    parent_uuid = _optional(values["parent_uuid"])
                    if parent_uuid == "00000000-0000-0000-0000-000000000000":
                        parent_uuid = None
                if parent_uuid:
                    self._require_material(conn, parent_uuid, MATERIAL_PARENT_NOT_FOUND)
                    self._check_parent_cycle(conn, material_uuid, parent_uuid)
                self._validate_resource_material_root(conn, template, parent_uuid)

                barcode = str(current["barcode"])
                if "barcode" in values and values["barcode"] is not None:
                    barcode = str(values["barcode"]).strip()
                name = str(current["name"])
                if "name" in values and values["name"] is not None:
                    name = str(values["name"]).strip()
                if not name:
                    raise BackendContractError(INVALID_PARAMETER, "name is required")
                description = current["description"]
                if "description" in values and values["description"] is not None:
                    description = values["description"]
                meta_data = _json(current["meta_data"], {})
                if "meta_data" in values and values["meta_data"] is not None:
                    meta_data = values["meta_data"]
                config = _json(current["config"], {})
                if "config" in values and values["config"] is not None:
                    requested_config = dict(values["config"])
                    if "sites" in config:
                        requested_config["sites"] = config["sites"]
                    else:
                        requested_config.pop("sites", None)
                    config = requested_config

                material_changed = any(
                    (
                        parent_uuid != current["parent_uuid"],
                        barcode != current["barcode"],
                        name != current["name"],
                        description != current["description"],
                        _dump(meta_data) != current["meta_data"],
                        _dump(config) != current["config"],
                    )
                )
                if material_changed:
                    conn.execute(
                        """
                        UPDATE material SET parent_uuid=?,barcode=?,name=?,description=?,
                            meta_data=?,config=?,update_time=?
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

                aggregate_changed = material_changed
                if values.get("_relative_position_specified"):
                    if values.get("relative_position") is None:
                        cursor = conn.execute(
                            "UPDATE relative_position SET deleted_at=?,update_time=? "
                            "WHERE material_uuid=? AND deleted_at IS NULL",
                            (_now(), _now(), material_uuid),
                        )
                        aggregate_changed = aggregate_changed or cursor.rowcount > 0
                    else:
                        self._upsert_relative_position(
                            conn, material_uuid, values["relative_position"]
                        )
                        aggregate_changed = True
                placement = values.get("site_placement")
                if values.get("_site_placement_specified") and placement:
                    self._apply_site_placement(
                        conn, material_uuid, template_uuid, placement
                    )
                    aggregate_changed = True
                if aggregate_changed:
                    conn.execute(
                        "UPDATE material_inventory "
                        "SET aggregate_version=aggregate_version+1 "
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
            rows = conn.execute(
                """
                WITH RECURSIVE material_tree(uuid,depth) AS (
                    SELECT uuid,0 FROM material
                    WHERE uuid=? AND deleted_at IS NULL
                    UNION ALL
                    SELECT child.uuid,material_tree.depth+1
                    FROM material AS child
                    JOIN material_tree ON child.parent_uuid=material_tree.uuid
                    WHERE child.deleted_at IS NULL
                )
                SELECT uuid,depth FROM material_tree ORDER BY depth DESC,uuid
                """,
                (material_uuid,),
            ).fetchall()
            material_uuids = [str(row["uuid"]) for row in rows]
            placeholders = ",".join("?" for _ in material_uuids)
            now = _now()
            conn.execute(
                "UPDATE site SET occupied_material_uuid=NULL,update_time=? "
                f"WHERE occupied_material_uuid IN ({placeholders}) "
                "AND deleted_at IS NULL",
                (now, *material_uuids),
            )
            conn.execute(
                "UPDATE site SET deleted_at=?,update_time=? "
                f"WHERE material_uuid IN ({placeholders}) AND deleted_at IS NULL",
                (now, now, *material_uuids),
            )
            conn.execute(
                "UPDATE relative_position SET deleted_at=?,update_time=? "
                f"WHERE material_uuid IN ({placeholders}) AND deleted_at IS NULL",
                (now, now, *material_uuids),
            )
            conn.execute(
                "UPDATE material SET deleted_at=?,update_time=? "
                f"WHERE uuid IN ({placeholders}) AND deleted_at IS NULL",
                (now, now, *material_uuids),
            )
            conn.execute(
                "UPDATE material_inventory SET inventory_status='discarded',"
                "disposition='discarded',aggregate_version=aggregate_version+1 "
                f"WHERE material_uuid IN ({placeholders})",
                tuple(material_uuids),
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
            _SITE_SELECT
            + "WHERE site.material_uuid=? AND site.deleted_at IS NULL "
            "ORDER BY site.sort_order,site.create_time,site.uuid",
            (material_uuid,),
        )
        return [self._site_row(row) for row in rows]

    def get_site(self, site_uuid: str) -> Dict[str, Any]:
        row = self.store.query_one(
            _SITE_SELECT + "WHERE site.uuid=? AND site.deleted_at IS NULL",
            (site_uuid,),
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
    def _insert_material_with_initial_state(
        conn: sqlite3.Connection,
        *,
        material_uuid: str,
        timestamp: str,
        description: Optional[str],
        meta_data: Dict[str, Any],
        template_uuid: str,
        parent_uuid: Optional[str],
        material_class: str,
        material_type: str,
        barcode: str,
        name: str,
        config: Dict[str, Any],
        data: Dict[str, Any],
    ) -> None:
        """Create one canonical Material and its immutable initial state."""

        conn.execute(
            """
            INSERT INTO material(
                uuid,create_time,update_time,deleted_at,description,meta_data,
                resource_template_uuid,parent_uuid,class,type,barcode,name,config,data
            ) VALUES (?,?,?,NULL,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                material_uuid,
                timestamp,
                timestamp,
                description,
                _dump(meta_data),
                template_uuid,
                parent_uuid,
                material_class,
                material_type,
                barcode,
                name,
                _dump(config),
                _dump(data),
            ),
        )
        conn.execute(
            "INSERT INTO material_inventory(material_uuid,legacy_template_id) "
            "VALUES (?,?)",
            (material_uuid, template_uuid),
        )
        conn.execute(
            "INSERT INTO material_content_version(material_uuid,version) VALUES (?,1)",
            (material_uuid,),
        )
        conn.execute(
            """
            INSERT INTO material_state_history(
                uuid,create_time,update_time,deleted_at,description,meta_data,
                material_uuid,status,state_data,source,observed_at
            ) VALUES (?,?,?,NULL,NULL,'{}',?,NULL,?,NULL,?)
            """,
            (str(uuid4()), timestamp, timestamp, material_uuid, _dump(data), timestamp),
        )

    @classmethod
    def _validate_resource_material_root(
        cls,
        conn: sqlite3.Connection,
        template: sqlite3.Row,
        parent_uuid: Optional[str],
    ) -> None:
        """Require ``resource`` Materials to belong to a non-resource root."""

        if str(template["resource_type"] or "").strip().casefold() != "resource":
            return
        if not parent_uuid:
            raise BackendContractError(
                INVALID_PARAMETER,
                "parent_uuid is required when resource_type is resource",
            )
        cursor: Optional[str] = parent_uuid
        seen: set[str] = set()
        root_type = "resource"
        while cursor:
            if cursor in seen:
                raise BackendContractError(
                    MATERIAL_PARENT_CYCLE,
                    "Material parent relationship creates a cycle",
                )
            seen.add(cursor)
            row = conn.execute(
                "SELECT material.parent_uuid,template.resource_type "
                "FROM material AS material "
                "JOIN resource_template AS template "
                "ON template.uuid=material.resource_template_uuid "
                "WHERE material.uuid=? AND material.deleted_at IS NULL",
                (cursor,),
            ).fetchone()
            if row is None:
                raise BackendContractError(
                    MATERIAL_PARENT_NOT_FOUND, "Material parent not found"
                )
            root_type = str(row["resource_type"] or "").strip().casefold()
            cursor = row["parent_uuid"]
        if root_type == "resource":
            raise BackendContractError(
                INVALID_PARAMETER,
                "resource material hierarchy root must use a non-resource template",
            )

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
        content_types = _json(site["content_type"], [])
        template_allowed = template_uuid in allowed
        content_type_allowed = False
        if content_types:
            template = conn.execute(
                "SELECT tags FROM resource_template "
                "WHERE uuid=? AND deleted_at IS NULL",
                (template_uuid,),
            ).fetchone()
            template_tags = (
                {
                    str(tag).strip().casefold()
                    for tag in _json(template["tags"], [])
                    if str(tag).strip()
                }
                if template is not None
                else set()
            )
            accepted_tags = {
                alias
                for value in content_types
                for alias in _SITE_CONTENT_TYPE_TAG_ALIASES.get(
                    str(value).strip().casefold(),
                    {str(value).strip().casefold()},
                )
                if alias
            }
            content_type_allowed = bool(template_tags & accepted_tags)
        if (allowed or content_types) and not (
            template_allowed or content_type_allowed
        ):
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
                "type": row["type"],
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
        site_index = _json(row.get("site_index"), int(row["sort_order"]))
        result.update(
            {
                # Site Model v1 规范字段。
                "schema_version": int(row.get("schema_version", 1)),
                "template_name": row["template_name"],
                "material_uuid": row["material_uuid"],
                "index": site_index,
                "label": row["name"],
                "visible": bool(row.get("visible", 1)),
                "occupied_material_uuid": row.get("occupied_material_uuid"),
                "position_x": float(row["position_x"]),
                "position_y": float(row["position_y"]),
                "position_z": float(row["position_z"]),
                "width": float(row["width"]),
                "length": float(row["length"]),
                "depth": float(row["depth"]),
                "rotation_x": float(row.get("rotation_x", 0)),
                "rotation_y": float(row.get("rotation_y", 0)),
                "rotation_z": float(row.get("rotation_z", 0)),
                "content_type": _json(row.get("content_type"), []),
                "allowed_resource_template_uuids": _json(
                    row.get("allowed_resource_template_uuids"), []
                ),
                "parent_link": str(row.get("parent_link") or ""),
                "description": str(row.get("description") or ""),
                # 旧 Backend/Edge 调用方的只读兼容别名；新代码不得写入。
                "name": row["name"],
                "sort_order": int(row["sort_order"]),
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
