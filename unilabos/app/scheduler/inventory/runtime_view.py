"""执行与观测对规范库存的只读投影；身份索引和数据库模式归 OS 所有。"""

import hashlib
import json
from typing import Any


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


class InventoryView:
    """将资源本地身份解析为规范 UUID，并以事务读取当前关系。"""

    def __init__(self, store):
        """构建存续材料身份索引；同一本地身份对应多个 UUID 时拒绝初始化。"""
        self.store = store
        self.by_id: dict[str, dict[str, Any]] = {}
        for material in store.query_all(
            "SELECT uuid,parent_uuid,meta_data FROM material WHERE deleted_at IS NULL"
        ):
            meta = json.loads(material["meta_data"])
            key = (
                meta.get("source_node_id")
                if meta.get("source") == "resource-tree-set"
                else meta.get("edge_local_id")
            )
            if key:
                if key in self.by_id:
                    raise ValueError("native_identity_ambiguous")
                self.by_id[key] = dict(material)

    def snapshot(self, roles, *, quantity_unit="mL"):
        """按领域角色读取身份和当前数量，单位不符立即拒绝；不修改库存。"""
        snapshot = {}
        for role, identifier in roles.items():
            row = dict(self.by_id[identifier])
            content = self.store.query_one(
                "SELECT quantity,quantity_unit FROM current_substance WHERE material_uuid=? AND deleted_at IS NULL",
                (row["uuid"],),
            )
            if content and content["quantity_unit"] != quantity_unit:
                raise ValueError("native_quantity_unit_mismatch")
            row["quantity"] = float(content["quantity"]) if content else 0.0
            row["quantity_unit"] = quantity_unit
            snapshot[role] = row
        return snapshot

    def relations(self, identifiers, *, scene_parent=None):
        """在同一事务读取存续材料的父级；仅当 DB 父级为空时回落到 scene 父级。

        `scene_parent` 是 `Callable[[str], str|None]`，按规范 UUID 查询领域场景父级。
        DB 已有父级不得被覆盖；已删除材料不因 scene 父级复活；本方法不写库。
        """
        result = {}
        with self.store.transaction() as connection:
            for identifier in dict.fromkeys(identifiers):
                row = connection.execute(
                    "SELECT parent_uuid FROM material WHERE uuid=? AND deleted_at IS NULL",
                    (identifier,),
                ).fetchone()
                if row is not None:
                    result[identifier] = row["parent_uuid"]
        if scene_parent is not None:
            for identifier, parent in result.items():
                if parent is None:
                    merged = scene_parent(identifier)
                    if merged is not None:
                        result[identifier] = merged
        return result


class ObservationInventoryView:
    """状态服务的规范库存投影；仅此适配器了解库存表和锁。"""

    def __init__(self, store):
        self.store = store

    def transaction(self):
        return self.store.transaction()

    def project(self, connection):
        materials = [
            dict(row)
            for row in connection.execute(
                "SELECT uuid,name,create_time,meta_data,parent_uuid,resource_template_uuid "
                "FROM material WHERE deleted_at IS NULL ORDER BY uuid"
            )
        ]
        sites = [
            dict(row)
            for row in connection.execute(
                "SELECT uuid,material_uuid,name,occupied_material_uuid FROM site WHERE deleted_at IS NULL ORDER BY uuid"
            )
        ]
        positions = {
            row["material_uuid"]: dict(row)
            for row in connection.execute(
                "SELECT pose_uuid AS uuid, uuid AS material_uuid, position_x, position_y, position_z, "
                "depth, length, width, scale_x, scale_y, scale_z, rotation_x, rotation_y, rotation_z "
                "FROM material WHERE deleted_at IS NULL AND position_x IS NOT NULL ORDER BY uuid"
            )
        }
        # 一次遍历建立两种索引，避免每个物料再次扫描全部库位。
        sites_by_owner: dict[str, list[dict[str, Any]]] = {}
        site_by_occupant: dict[str, str] = {}
        for site in sites:
            sites_by_owner.setdefault(site["material_uuid"], []).append(site)
            if site["occupied_material_uuid"]:
                site_by_occupant.setdefault(
                    site["occupied_material_uuid"], site["uuid"]
                )
        nodes = {}
        for material in materials:
            meta = json.loads(material.pop("meta_data"))
            material["id"] = (
                meta.get("source_node_id")
                if meta.get("source") == "resource-tree-set"
                else None
            )
            material["generation"] = hashlib.sha256(
                _json([material["uuid"], material.pop("create_time")]).encode()
            ).hexdigest()
            material["sites"] = sites_by_owner.get(material["uuid"], [])
            material["relative_position"] = positions.get(material["uuid"])
            material["current_site_uuid"] = site_by_occupant.get(material["uuid"])
            nodes[material["uuid"]] = material
        revision = connection.execute(
            "SELECT revision FROM runtime_scene_revision WHERE id=1"
        ).fetchone()[0]
        return nodes, revision

    @property
    def catalog_fingerprint(self):
        catalog = self.store.runtime_device_template_catalog
        return catalog.fingerprint if catalog else None

    def template_metadata(self, template_uuid):
        catalog = self.store.runtime_device_template_catalog
        template = catalog.get(template_uuid) if catalog else None
        if template is None:
            template = self.store.query_one(
                "SELECT meta_data FROM resource_template WHERE uuid=? AND deleted_at IS NULL",
                (template_uuid,),
            )
        metadata = (template or {}).get("meta_data") or {}
        return json.loads(metadata) if isinstance(metadata, str) else metadata

    def has_claims(self, connection, members):
        if not members:
            return False
        placeholders = ",".join("?" for _ in members)
        return (
            connection.execute(
                "SELECT 1 FROM station_execution_lock_lease WHERE material_uuid IN ("
                + placeholders
                + ") "
                "AND state IN ('prepared','reserved','running','uncertain') LIMIT 1",
                tuple(members),
            ).fetchone()
            is not None
        )

    def has_receipt(self, job_id, receipt_id):
        row = self.store.query_one(
            "SELECT workflow_node_job_uuid FROM inventory_ledger WHERE entry_uuid=?",
            (receipt_id,),
        )
        return row is not None and row["workflow_node_job_uuid"] == job_id


def initialize_observation_projection(connection):
    """迁移旧修订并由库存持有触发器，避免状态服务修改库存模式。"""
    connection.execute(
        "CREATE TABLE IF NOT EXISTS runtime_scene_revision "
        "(id INTEGER PRIMARY KEY CHECK(id=1), revision INTEGER NOT NULL)"
    )
    connection.execute("INSERT OR IGNORE INTO runtime_scene_revision VALUES (1,0)")
    if connection.table_exists("twin_scene_revision"):
        connection.execute(
            "UPDATE runtime_scene_revision SET revision="
            "MAX(revision,(SELECT revision FROM twin_scene_revision WHERE id=1)) WHERE id=1"
        )
    for table in ("material", "site", "resource_template"):
        # 历史仅含定义表的库存库可独立迁移；事实表出现后下次初始化补齐触发器。
        if not connection.table_exists(table):
            continue
        for operation in ("INSERT", "UPDATE", "DELETE"):
            connection.execute(
                f"DROP TRIGGER IF EXISTS twin_{table}_{operation.lower()}"
            )
            connection.execute(
                f"CREATE TRIGGER IF NOT EXISTS runtime_{table}_{operation.lower()} "
                f"AFTER {operation} ON {table} BEGIN "
                "UPDATE runtime_scene_revision SET revision=revision+1 WHERE id=1; END"
            )
    if connection.table_exists("twin_scene_revision"):
        connection.execute("DROP TABLE twin_scene_revision")
    connection.commit()
