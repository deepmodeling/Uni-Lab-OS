"""``materials.db`` 的同步 Repository 和单写事务边界。"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Optional

from unilabos.server.database.tables.materials import MATERIALS_DATABASE
from unilabos.server.database.schema import initialize_database
from unilabos.server.database.tables.materials import (
    InventoryCommandEffectRecord,
    InventoryLedgerRecord,
    InventoryLotRecord,
    InventoryReservationRecord,
    MaterialDataRecord,
    MaterialPositionRecord,
    MaterialRecord,
    MaterialSubstanceRecord,
    ResourceTemplateRecord,
    SiteRecord,
)
from unilabos.server.protocol.common import canonical_json


def _load_json(value: Any, fallback: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    if value is None:
        return fallback
    return json.loads(str(value))


class MaterialsRepository:
    """表行 CRUD。

    Repository 独占一个 SQLite connection；所有写操作通过 ``write()`` 的
    ``BEGIN IMMEDIATE`` 串行化，Service 负责聚合规则与 ledger。
    """

    def __init__(self, database: str | Path | sqlite3.Connection):
        if isinstance(database, sqlite3.Connection):
            self.connection = database
            self._owns_connection = False
            self.connection.row_factory = sqlite3.Row
            self.connection.execute("PRAGMA foreign_keys = ON")
        else:
            self.connection = initialize_database(database, MATERIALS_DATABASE)
            self._owns_connection = True
        self._write_lock = threading.RLock()

    def close(self) -> None:
        if self._owns_connection:
            self.connection.close()

    def __enter__(self) -> "MaterialsRepository":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @contextmanager
    def write(self) -> Iterator[sqlite3.Connection]:
        """每个 materials.db 进程内只有这一个 writer 入口。"""

        with self._write_lock:
            # Batch scheduler operations compose existing service mutations
            # under one outer BEGIN IMMEDIATE.  The re-entrant writer never
            # commits or rolls back its caller's transaction.
            if self.connection.in_transaction:
                yield self.connection
                return
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                yield self.connection
            except BaseException:
                self.connection.rollback()
                raise
            else:
                self.connection.commit()

    # -- Template ---------------------------------------------------------

    @staticmethod
    def _template(row: sqlite3.Row) -> ResourceTemplateRecord:
        values = dict(row)
        values.update(
            category=_load_json(values.pop("category_json"), []),
            available_sites=_load_json(values.pop("available_sites_json"), []),
            handles=_load_json(values.pop("handles_json"), []),
        )
        values["definition_json"] = _load_json(values["definition_json"], {})
        return ResourceTemplateRecord.model_validate(values)

    def get_template(
        self, template_uuid: str, *, include_deleted: bool = False
    ) -> Optional[ResourceTemplateRecord]:
        sql = "SELECT * FROM resource_template WHERE template_uuid=?"
        params: list[Any] = [template_uuid]
        if not include_deleted:
            sql += " AND deleted_at_ms IS NULL"
        row = self.connection.execute(sql, params).fetchone()
        return self._template(row) if row is not None else None

    def get_template_by_name(
        self, name: str, *, include_deleted: bool = False
    ) -> Optional[ResourceTemplateRecord]:
        sql = "SELECT * FROM resource_template WHERE LOWER(name)=LOWER(?)"
        if not include_deleted:
            sql += " AND deleted_at_ms IS NULL"
        row = self.connection.execute(sql, (name,)).fetchone()
        return self._template(row) if row is not None else None

    def list_templates(
        self, *, status: Optional[str] = None
    ) -> list[ResourceTemplateRecord]:
        clauses = ["deleted_at_ms IS NULL"]
        params: list[Any] = []
        if status is not None:
            clauses.append("status=?")
            params.append(status)
        rows = self.connection.execute(
            "SELECT * FROM resource_template WHERE "
            + " AND ".join(clauses)
            + " ORDER BY LOWER(name),template_uuid",
            params,
        )
        return [self._template(row) for row in rows]

    def count_active_materials_for_template(self, template_uuid: str) -> int:
        row = self.connection.execute(
            "SELECT COUNT(*) FROM material WHERE template_uuid=? "
            "AND deleted_at_ms IS NULL",
            (template_uuid,),
        ).fetchone()
        return int(row[0])

    def insert_template(self, record: ResourceTemplateRecord) -> None:
        values = record.model_dump(mode="json")
        self.connection.execute(
            """
            INSERT INTO resource_template(
                template_uuid,name,display_name,resource_type,class_name,module_name,
                template_version,category_json,available_sites_json,handles_json,
                definition_json,definition_hash,status,created_at_ms,updated_at_ms,
                deleted_at_ms,version
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                values["template_uuid"], values["name"], values["display_name"],
                values["resource_type"], values["class_name"], values["module_name"],
                values["template_version"], canonical_json(values["category"]),
                canonical_json(values["available_sites"]),
                canonical_json(values["handles"]),
                canonical_json(values["definition_json"]), values["definition_hash"],
                values["status"], values["created_at_ms"], values["updated_at_ms"],
                values["deleted_at_ms"], values["version"],
            ),
        )

    def update_template(self, record: ResourceTemplateRecord) -> None:
        values = record.model_dump(mode="json")
        cursor = self.connection.execute(
            """
            UPDATE resource_template SET
                name=?,display_name=?,resource_type=?,class_name=?,module_name=?,
                template_version=?,category_json=?,available_sites_json=?,handles_json=?,
                definition_json=?,definition_hash=?,status=?,updated_at_ms=?,
                deleted_at_ms=?,version=?
            WHERE template_uuid=? AND version=?
            """,
            (
                values["name"], values["display_name"], values["resource_type"],
                values["class_name"], values["module_name"], values["template_version"],
                canonical_json(values["category"]),
                canonical_json(values["available_sites"]),
                canonical_json(values["handles"]),
                canonical_json(values["definition_json"]), values["definition_hash"],
                values["status"], values["updated_at_ms"], values["deleted_at_ms"],
                values["version"], values["template_uuid"], values["version"] - 1,
            ),
        )
        if cursor.rowcount != 1:
            raise RuntimeError("resource template version conflict")

    # -- Inventory lot ----------------------------------------------------

    @staticmethod
    def _lot(row: sqlite3.Row) -> InventoryLotRecord:
        values = dict(row)
        values["quarantined"] = bool(values["quarantined"])
        return InventoryLotRecord.model_validate(values)

    def get_lot(self, lot_uuid: str) -> Optional[InventoryLotRecord]:
        row = self.connection.execute(
            "SELECT * FROM inventory_lot WHERE lot_uuid=?", (lot_uuid,)
        ).fetchone()
        return self._lot(row) if row is not None else None

    def list_lots(
        self,
        *,
        template_uuid: Optional[str] = None,
        unit: Optional[str] = None,
        include_quarantined: bool = False,
        available_only: bool = False,
    ) -> list[InventoryLotRecord]:
        clauses: list[str] = []
        params: list[Any] = []
        if template_uuid is not None:
            clauses.append("template_uuid=?")
            params.append(template_uuid)
        if unit is not None:
            clauses.append("unit=?")
            params.append(unit)
        if not include_quarantined:
            clauses.append("quarantined=0")
        if available_only:
            clauses.append("quantity_available>0")
        sql = "SELECT * FROM inventory_lot"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += (
            " ORDER BY CASE WHEN expiry_at_ms IS NULL THEN 1 ELSE 0 END,"
            " expiry_at_ms,created_at_ms,lot_uuid"
        )
        return [self._lot(row) for row in self.connection.execute(sql, params)]

    def insert_lot(self, record: InventoryLotRecord) -> None:
        values = record.model_dump(mode="json")
        self.connection.execute(
            """
            INSERT INTO inventory_lot(
                lot_uuid,template_uuid,batch_no,unit,quantity_total,
                quantity_available,quantity_reserved,expiry_at_ms,quarantined,
                created_at_ms,updated_at_ms,version
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                values["lot_uuid"], values["template_uuid"], values["batch_no"],
                values["unit"], values["quantity_total"],
                values["quantity_available"], values["quantity_reserved"],
                values["expiry_at_ms"], values["quarantined"],
                values["created_at_ms"], values["updated_at_ms"], values["version"],
            ),
        )

    def update_lot(self, record: InventoryLotRecord) -> None:
        values = record.model_dump(mode="json")
        cursor = self.connection.execute(
            """
            UPDATE inventory_lot SET template_uuid=?,batch_no=?,unit=?,
                quantity_total=?,quantity_available=?,quantity_reserved=?,
                expiry_at_ms=?,quarantined=?,updated_at_ms=?,version=?
            WHERE lot_uuid=? AND version=?
            """,
            (
                values["template_uuid"], values["batch_no"], values["unit"],
                values["quantity_total"], values["quantity_available"],
                values["quantity_reserved"], values["expiry_at_ms"],
                values["quarantined"], values["updated_at_ms"], values["version"],
                values["lot_uuid"], values["version"] - 1,
            ),
        )
        if cursor.rowcount != 1:
            raise RuntimeError("inventory lot version conflict")

    # -- Inventory reservation ------------------------------------------

    @staticmethod
    def _reservation(row: sqlite3.Row) -> InventoryReservationRecord:
        values = dict(row)
        values["items"] = _load_json(values.pop("items_json"), [])
        return InventoryReservationRecord.model_validate(values)

    def get_reservation(
        self, reservation_uuid: str
    ) -> Optional[InventoryReservationRecord]:
        row = self.connection.execute(
            "SELECT * FROM inventory_reservation WHERE reservation_uuid=?",
            (reservation_uuid,),
        ).fetchone()
        return self._reservation(row) if row is not None else None

    def get_reservation_by_job(
        self, job_uuid: str
    ) -> Optional[InventoryReservationRecord]:
        row = self.connection.execute(
            "SELECT * FROM inventory_reservation WHERE job_uuid=?", (job_uuid,)
        ).fetchone()
        return self._reservation(row) if row is not None else None

    def list_reservations(
        self,
        *,
        task_uuid: Optional[str] = None,
        status: Optional[str] = None,
    ) -> list[InventoryReservationRecord]:
        clauses: list[str] = []
        params: list[Any] = []
        if task_uuid is not None:
            clauses.append("task_uuid=?")
            params.append(task_uuid)
        if status is not None:
            clauses.append("status=?")
            params.append(status)
        sql = "SELECT * FROM inventory_reservation"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at_ms,reservation_uuid"
        return [
            self._reservation(row) for row in self.connection.execute(sql, params)
        ]

    def insert_reservation(self, record: InventoryReservationRecord) -> None:
        values = record.model_dump(mode="json")
        self.connection.execute(
            """
            INSERT INTO inventory_reservation(
                reservation_uuid,task_uuid,node_uuid,job_uuid,scheduler_revision,
                request_hash,items_json,status,expires_at_ms,created_at_ms,
                updated_at_ms,version
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                values["reservation_uuid"], values["task_uuid"],
                values["node_uuid"], values["job_uuid"],
                values["scheduler_revision"], values["request_hash"],
                canonical_json(values["items"]), values["status"],
                values["expires_at_ms"], values["created_at_ms"],
                values["updated_at_ms"], values["version"],
            ),
        )

    def update_reservation(self, record: InventoryReservationRecord) -> None:
        values = record.model_dump(mode="json")
        cursor = self.connection.execute(
            """
            UPDATE inventory_reservation SET task_uuid=?,node_uuid=?,job_uuid=?,
                scheduler_revision=?,request_hash=?,items_json=?,status=?,
                expires_at_ms=?,updated_at_ms=?,version=?
            WHERE reservation_uuid=? AND version=?
            """,
            (
                values["task_uuid"], values["node_uuid"], values["job_uuid"],
                values["scheduler_revision"], values["request_hash"],
                canonical_json(values["items"]), values["status"],
                values["expires_at_ms"], values["updated_at_ms"], values["version"],
                values["reservation_uuid"], values["version"] - 1,
            ),
        )
        if cursor.rowcount != 1:
            raise RuntimeError("inventory reservation version conflict")

    # -- Material aggregate ----------------------------------------------

    @staticmethod
    def _material(row: sqlite3.Row) -> MaterialRecord:
        values = dict(row)
        for field in (
            "resource_schema_json",
            "model_json",
            "config_json",
            "extra_json",
            "meta_data_json",
        ):
            values[field] = _load_json(values[field], {})
        return MaterialRecord.model_validate(values)

    @staticmethod
    def _position(row: sqlite3.Row) -> MaterialPositionRecord:
        values = dict(row)
        values["extra_json"] = _load_json(values["extra_json"], {})
        return MaterialPositionRecord.model_validate(values)

    @staticmethod
    def _substance(row: sqlite3.Row) -> MaterialSubstanceRecord:
        values = dict(row)
        values["composition"] = _load_json(values.pop("composition_json"), [])
        values["meta_data_json"] = _load_json(values["meta_data_json"], {})
        return MaterialSubstanceRecord.model_validate(values)

    def _data(self, row: sqlite3.Row) -> MaterialDataRecord:
        values = dict(row)
        values["data_json"] = _load_json(values["data_json"], {})
        values["sites_initialized"] = bool(values["sites_initialized"])
        values["substances"] = self.list_substances(values["material_uuid"])
        return MaterialDataRecord.model_validate(values)

    @staticmethod
    def _site(row: sqlite3.Row) -> SiteRecord:
        values = dict(row)
        values["visible"] = bool(values["visible"])
        values["pose"] = _load_json(values.pop("pose_json"), {})
        values["allowed_resource_categories"] = _load_json(
            values.pop("allowed_resource_categories_json"), []
        )
        values["meta_data_json"] = _load_json(values["meta_data_json"], {})
        values["extra_json"] = _load_json(values["extra_json"], {})
        return SiteRecord.model_validate(values)

    def get_material(
        self, material_uuid: str, *, include_deleted: bool = False
    ) -> Optional[MaterialRecord]:
        sql = "SELECT * FROM material WHERE material_uuid=?"
        if not include_deleted:
            sql += " AND deleted_at_ms IS NULL"
        row = self.connection.execute(sql, (material_uuid,)).fetchone()
        return self._material(row) if row is not None else None

    def get_material_by_resource_id(
        self, resource_id: str, *, include_deleted: bool = False
    ) -> Optional[MaterialRecord]:
        sql = "SELECT * FROM material WHERE resource_id=?"
        if not include_deleted:
            sql += " AND deleted_at_ms IS NULL"
        row = self.connection.execute(sql, (resource_id,)).fetchone()
        return self._material(row) if row is not None else None

    def list_materials(
        self, *, parent_material_uuid: Optional[str] = None, roots_only: bool = False
    ) -> list[MaterialRecord]:
        if roots_only:
            rows = self.connection.execute(
                "SELECT * FROM material WHERE parent_material_uuid IS NULL "
                "AND deleted_at_ms IS NULL ORDER BY LOWER(name),material_uuid"
            )
        elif parent_material_uuid is not None:
            rows = self.connection.execute(
                "SELECT * FROM material WHERE parent_material_uuid=? "
                "AND deleted_at_ms IS NULL ORDER BY ordinal,material_uuid",
                (parent_material_uuid,),
            )
        else:
            rows = self.connection.execute(
                "SELECT * FROM material WHERE deleted_at_ms IS NULL "
                "ORDER BY material_uuid"
            )
        return [self._material(row) for row in rows]

    def tree_materials(self, root_material_uuid: str) -> list[MaterialRecord]:
        rows = self.connection.execute(
            """
            WITH RECURSIVE tree(material_uuid,depth,path) AS (
                SELECT material_uuid,0,
                       printf('/%010d:%s/',ordinal,material_uuid)
                FROM material
                WHERE material_uuid=? AND deleted_at_ms IS NULL
                UNION ALL
                SELECT child.material_uuid,tree.depth+1,
                       tree.path || printf('%010d:%s/',child.ordinal,child.material_uuid)
                FROM material child JOIN tree
                    ON child.parent_material_uuid=tree.material_uuid
                WHERE child.deleted_at_ms IS NULL
            )
            SELECT material.* FROM material JOIN tree USING(material_uuid)
            ORDER BY tree.path
            """,
            (root_material_uuid,),
        )
        return [self._material(row) for row in rows]

    def get_position(self, material_uuid: str) -> Optional[MaterialPositionRecord]:
        row = self.connection.execute(
            "SELECT * FROM material_position WHERE material_uuid=?", (material_uuid,)
        ).fetchone()
        return self._position(row) if row is not None else None

    def get_data(self, material_uuid: str) -> Optional[MaterialDataRecord]:
        row = self.connection.execute(
            "SELECT * FROM material_data WHERE material_uuid=?", (material_uuid,)
        ).fetchone()
        return self._data(row) if row is not None else None

    def list_substances(self, material_uuid: str) -> list[MaterialSubstanceRecord]:
        rows = self.connection.execute(
            "SELECT * FROM material_substance WHERE material_uuid=? ORDER BY ordinal",
            (material_uuid,),
        )
        return [self._substance(row) for row in rows]

    def list_sites(
        self, owner_material_uuid: str, *, include_deleted: bool = False
    ) -> list[SiteRecord]:
        sql = "SELECT * FROM site WHERE owner_material_uuid=?"
        if not include_deleted:
            sql += " AND deleted_at_ms IS NULL"
        sql += " ORDER BY ordinal,site_uuid"
        return [
            self._site(row)
            for row in self.connection.execute(sql, (owner_material_uuid,))
        ]

    def get_site(
        self, site_uuid: str, *, include_deleted: bool = False
    ) -> Optional[SiteRecord]:
        sql = "SELECT * FROM site WHERE site_uuid=?"
        if not include_deleted:
            sql += " AND deleted_at_ms IS NULL"
        row = self.connection.execute(sql, (site_uuid,)).fetchone()
        return self._site(row) if row is not None else None

    def occupied_site(self, material_uuid: str) -> Optional[SiteRecord]:
        row = self.connection.execute(
            "SELECT * FROM site WHERE occupied_material_uuid=? AND deleted_at_ms IS NULL",
            (material_uuid,),
        ).fetchone()
        return self._site(row) if row is not None else None

    def sites_occupied_by(self, material_uuids: Sequence[str]) -> list[SiteRecord]:
        if not material_uuids:
            return []
        placeholders = ",".join("?" for _ in material_uuids)
        rows = self.connection.execute(
            f"SELECT * FROM site WHERE occupied_material_uuid IN ({placeholders}) "
            "AND deleted_at_ms IS NULL ORDER BY site_uuid",
            tuple(material_uuids),
        )
        return [self._site(row) for row in rows]

    def insert_material(self, record: MaterialRecord) -> None:
        values = record.model_dump(mode="json")
        columns = (
            "material_uuid", "resource_id", "template_uuid", "parent_material_uuid",
            "ordinal", "lot_uuid", "name", "description", "resource_type", "class_name",
            "machine_name", "barcode", "barcode_symbology", "template_name",
            "resource_schema_json", "model_json", "icon_uri", "config_json",
            "extra_json", "meta_data_json", "lifecycle_status", "created_at_ms",
            "updated_at_ms", "deleted_at_ms", "version",
        )
        json_fields = {
            "resource_schema_json", "model_json", "config_json", "extra_json",
            "meta_data_json",
        }
        params = [
            canonical_json(values[name]) if name in json_fields else values[name]
            for name in columns
        ]
        self.connection.execute(
            f"INSERT INTO material({','.join(columns)}) VALUES "
            f"({','.join('?' for _ in columns)})",
            params,
        )

    def update_material(self, record: MaterialRecord) -> None:
        values = record.model_dump(mode="json")
        assignments = (
            "resource_id=?", "template_uuid=?", "parent_material_uuid=?", "ordinal=?", "lot_uuid=?",
            "name=?", "description=?", "resource_type=?", "class_name=?",
            "machine_name=?", "barcode=?", "barcode_symbology=?", "template_name=?",
            "resource_schema_json=?", "model_json=?", "icon_uri=?", "config_json=?",
            "extra_json=?", "meta_data_json=?", "lifecycle_status=?", "updated_at_ms=?",
            "deleted_at_ms=?", "version=?",
        )
        params = (
            values["resource_id"], values["template_uuid"],
            values["parent_material_uuid"], values["ordinal"], values["lot_uuid"], values["name"],
            values["description"], values["resource_type"], values["class_name"],
            values["machine_name"], values["barcode"], values["barcode_symbology"],
            values["template_name"], canonical_json(values["resource_schema_json"]),
            canonical_json(values["model_json"]), values["icon_uri"],
            canonical_json(values["config_json"]), canonical_json(values["extra_json"]),
            canonical_json(values["meta_data_json"]), values["lifecycle_status"],
            values["updated_at_ms"], values["deleted_at_ms"], values["version"],
            values["material_uuid"], values["version"] - 1,
        )
        cursor = self.connection.execute(
            f"UPDATE material SET {','.join(assignments)} "
            "WHERE material_uuid=? AND version=?",
            params,
        )
        if cursor.rowcount != 1:
            raise RuntimeError("material version conflict")

    def replace_position(self, record: MaterialPositionRecord) -> None:
        values = record.model_dump(mode="json")
        columns = tuple(values)
        params = [
            canonical_json(values[name]) if name == "extra_json" else values[name]
            for name in columns
        ]
        updates = ",".join(f"{name}=excluded.{name}" for name in columns[1:])
        self.connection.execute(
            f"INSERT INTO material_position({','.join(columns)}) VALUES "
            f"({','.join('?' for _ in columns)}) ON CONFLICT(material_uuid) "
            f"DO UPDATE SET {updates}",
            params,
        )

    def replace_data(self, record: MaterialDataRecord) -> None:
        values = record.model_dump(mode="json", exclude={"substances"})
        columns = tuple(values)
        params = [
            canonical_json(values[name]) if name == "data_json" else values[name]
            for name in columns
        ]
        updates = ",".join(f"{name}=excluded.{name}" for name in columns[1:])
        self.connection.execute(
            f"INSERT INTO material_data({','.join(columns)}) VALUES "
            f"({','.join('?' for _ in columns)}) ON CONFLICT(material_uuid) "
            f"DO UPDATE SET {updates}",
            params,
        )

    def replace_substances(
        self, material_uuid: str, records: Sequence[MaterialSubstanceRecord]
    ) -> None:
        self.connection.execute(
            "DELETE FROM material_substance WHERE material_uuid=?", (material_uuid,)
        )
        for record in records:
            values = record.model_dump(mode="json")
            self.connection.execute(
                """
                INSERT INTO material_substance(
                    substance_uuid,material_uuid,ordinal,name,quantity,quantity_unit,
                    physical_state,composition_json,meta_data_json,content_version,
                    observed_at_ms,updated_at_ms,version
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    values["substance_uuid"], values["material_uuid"],
                    values["ordinal"], values["name"], values["quantity"],
                    values["quantity_unit"], values["physical_state"],
                    canonical_json(values["composition"]),
                    canonical_json(values["meta_data_json"]),
                    values["content_version"], values["observed_at_ms"],
                    values["updated_at_ms"], values["version"],
                ),
            )

    def insert_site(self, record: SiteRecord) -> None:
        values = record.model_dump(mode="json")
        columns = (
            "site_uuid", "schema_version", "owner_material_uuid", "ordinal", "template_name",
            "site_index", "label", "visible", "occupied_material_uuid", "pose_json",
            "allowed_resource_categories_json", "parent_link", "description",
            "meta_data_json", "extra_json", "changed_by_job_uuid",
            "changed_by_command_uuid", "changed_at_ms", "created_at_ms",
            "updated_at_ms", "deleted_at_ms", "version",
        )
        mapped = {
            **values,
            "pose_json": values["pose"],
            "allowed_resource_categories_json": values[
                "allowed_resource_categories"
            ],
        }
        json_fields = {
            "pose_json", "allowed_resource_categories_json", "meta_data_json",
            "extra_json",
        }
        params = [
            canonical_json(mapped[name]) if name in json_fields else mapped[name]
            for name in columns
        ]
        self.connection.execute(
            f"INSERT INTO site({','.join(columns)}) VALUES "
            f"({','.join('?' for _ in columns)})",
            params,
        )

    def update_site(self, record: SiteRecord) -> None:
        values = record.model_dump(mode="json")
        cursor = self.connection.execute(
            """
            UPDATE site SET schema_version=?,owner_material_uuid=?,ordinal=?,template_name=?,
                site_index=?,label=?,visible=?,occupied_material_uuid=?,pose_json=?,
                allowed_resource_categories_json=?,parent_link=?,description=?,
                meta_data_json=?,extra_json=?,changed_by_job_uuid=?,
                changed_by_command_uuid=?,changed_at_ms=?,updated_at_ms=?,deleted_at_ms=?,
                version=?
            WHERE site_uuid=? AND version=?
            """,
            (
                values["schema_version"], values["owner_material_uuid"],
                values["ordinal"], values["template_name"], values["site_index"], values["label"],
                values["visible"], values["occupied_material_uuid"],
                canonical_json(values["pose"]),
                canonical_json(values["allowed_resource_categories"]),
                values["parent_link"], values["description"],
                canonical_json(values["meta_data_json"]),
                canonical_json(values["extra_json"]), values["changed_by_job_uuid"],
                values["changed_by_command_uuid"], values["changed_at_ms"],
                values["updated_at_ms"], values["deleted_at_ms"], values["version"],
                values["site_uuid"], values["version"] - 1,
            ),
        )
        if cursor.rowcount != 1:
            raise RuntimeError("site version conflict")

    def clear_site_occupants(self, site_uuids: Sequence[str]) -> None:
        """Snapshot move 的事务内准备步骤；不独立形成版本或 ledger。"""

        self.connection.executemany(
            "UPDATE site SET occupied_material_uuid=NULL WHERE site_uuid=?",
            ((site_uuid,) for site_uuid in site_uuids),
        )

    # -- Idempotency / ledger --------------------------------------------

    def get_effect(
        self, command_uuid: str, effect_key: str
    ) -> Optional[InventoryCommandEffectRecord]:
        row = self.connection.execute(
            "SELECT * FROM inventory_command_effect WHERE command_uuid=? AND effect_key=?",
            (command_uuid, effect_key),
        ).fetchone()
        if row is None:
            return None
        values = dict(row)
        values["request_json"] = _load_json(values["request_json"], {})
        values["result_json"] = _load_json(values["result_json"], {})
        return InventoryCommandEffectRecord.model_validate(values)

    def insert_effect(self, record: InventoryCommandEffectRecord) -> None:
        values = record.model_dump(mode="json")
        self.connection.execute(
            """
            INSERT INTO inventory_command_effect(
                command_uuid,effect_key,job_uuid,operation,request_json,request_hash,
                status,result_json,ledger_sequence_start,ledger_sequence_end,error_code,
                error_message,started_at_ms,updated_at_ms,completed_at_ms
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                values["command_uuid"], values["effect_key"], values["job_uuid"],
                values["operation"], canonical_json(values["request_json"]),
                values["request_hash"], values["status"],
                canonical_json(values["result_json"]),
                values["ledger_sequence_start"], values["ledger_sequence_end"],
                values["error_code"], values["error_message"],
                values["started_at_ms"], values["updated_at_ms"],
                values["completed_at_ms"],
            ),
        )

    def complete_effect(
        self,
        *,
        command_uuid: str,
        effect_key: str,
        result: Mapping[str, Any],
        ledger_sequence_start: int,
        ledger_sequence_end: int,
        completed_at_ms: int,
    ) -> None:
        self.connection.execute(
            """
            UPDATE inventory_command_effect SET status='applied',result_json=?,
                ledger_sequence_start=?,ledger_sequence_end=?,updated_at_ms=?,
                completed_at_ms=?
            WHERE command_uuid=? AND effect_key=? AND status='applying'
            """,
            (
                canonical_json(dict(result)), ledger_sequence_start,
                ledger_sequence_end, completed_at_ms, completed_at_ms,
                command_uuid, effect_key,
            ),
        )

    def append_ledger(self, record: InventoryLedgerRecord) -> int:
        values = record.model_dump(mode="json")
        cursor = self.connection.execute(
            """
            INSERT INTO inventory_ledger(
                event_uuid,aggregate_type,aggregate_uuid,operation,previous_version,
                aggregate_version,state_hash,delta_json,job_uuid,command_uuid,effect_key,
                actor_type,actor_uuid,occurred_at_ms,delivery_status,
                delivery_attempt_count,available_at_ms,last_sent_at_ms,acked_at_ms,last_error
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                values["event_uuid"], values["aggregate_type"],
                values["aggregate_uuid"], values["operation"],
                values["previous_version"], values["aggregate_version"],
                values["state_hash"], canonical_json(values["delta_json"]),
                values["job_uuid"], values["command_uuid"], values["effect_key"],
                values["actor_type"], values["actor_uuid"], values["occurred_at_ms"],
                values["delivery_status"], values["delivery_attempt_count"],
                values["available_at_ms"], values["last_sent_at_ms"],
                values["acked_at_ms"], values["last_error"],
            ),
        )
        return int(cursor.lastrowid)

    def latest_ledger_sequence(self) -> int:
        row = self.connection.execute(
            "SELECT COALESCE(MAX(sequence),0) FROM inventory_ledger"
        ).fetchone()
        return int(row[0])

    def list_ledger(
        self, *, after_sequence: int = 0, limit: int = 100
    ) -> list[InventoryLedgerRecord]:
        rows = self.connection.execute(
            "SELECT * FROM inventory_ledger WHERE sequence>? "
            "ORDER BY sequence LIMIT ?",
            (after_sequence, limit),
        )
        result: list[InventoryLedgerRecord] = []
        for row in rows:
            values = dict(row)
            values["delta_json"] = _load_json(values["delta_json"], {})
            result.append(InventoryLedgerRecord.model_validate(values))
        return result

    def acknowledge_ledger(self, through_sequence: int, *, acknowledged_at_ms: int) -> int:
        cursor = self.connection.execute(
            """
            UPDATE inventory_ledger
            SET delivery_status='acknowledged',acked_at_ms=?,last_error=NULL
            WHERE sequence<=? AND delivery_status IN ('pending','sent')
            """,
            (acknowledged_at_ms, through_sequence),
        )
        return int(cursor.rowcount)


__all__ = ["MaterialsRepository"]
