"""本地 Backend 形态资源模板（ResourceTemplate）的库位定义持久化测试。"""

from __future__ import annotations

import sqlite3

import pytest

from unilabos.app.scheduler.inventory.backend_contract import BackendResourceService
from unilabos.app.scheduler.inventory.store import SCHEMA_VERSION, InventoryStore
from unilabos.config.config import BasicConfig

AVAILABLE_SITES = [
    {
        "schema_version": 1,
        "index": "A1",
        "label": "反应瓶位",
        "visible": True,
        "position_x": 1,
        "position_y": 2,
        "position_z": 3,
        "rotation_x": 4,
        "rotation_y": 5,
        "rotation_z": 6,
        "width": 7,
        "length": 8,
        "depth": 9,
        "content_type": ["bottle"],
        "allowed_resource_template_uuids": [
            "10000000-0000-4000-8000-000000000001"
        ],
        "initial_material_template_uuid": (
            "10000000-0000-4000-8000-000000000001"
        ),
        "parent_link": "deck",
        "description": "",
        "meta_data": {},
    }
]


def test_schema_migration_adds_available_sites_to_v6_template(tmp_path) -> None:
    """第 6 版库存数据库应无损增加资源模板库位定义列。

    参数：``tmp_path`` 是隔离数据库目录。返回：无；断言迁移版本前进且既有模板
    得到 JSON 空数组，避免缺省值变成 ``null``。
    """

    if BasicConfig.database_type == "postgresql":
        pytest.skip("该测试直接构造 SQLite v6 文件并检查 PRAGMA 版本")

    database = tmp_path / "inventory-v6.db"
    connection = sqlite3.connect(database)
    connection.executescript(
        """
        CREATE TABLE resource_template (
            uuid TEXT PRIMARY KEY,
            resource_type TEXT NOT NULL
        );
        CREATE TABLE material (
            uuid TEXT PRIMARY KEY,
            resource_template_uuid TEXT NOT NULL,
            -- v6 已有的物料字段，后续设备名称索引迁移仍会使用。
            name TEXT NOT NULL,
            parent_uuid TEXT,
            deleted_at DATETIME,
            type TEXT NOT NULL DEFAULT ''
        );
        INSERT INTO resource_template(uuid, resource_type)
        VALUES ('template-a', 'device');
        PRAGMA user_version = 6;
        """
    )
    connection.commit()
    connection.close()

    store = InventoryStore(str(database))

    assert store.query_one("PRAGMA user_version")["user_version"] == SCHEMA_VERSION
    assert store.query_one(
        "SELECT available_sites FROM resource_template WHERE uuid='template-a'"
    )["available_sites"] == "[]"
    store.close()


def test_local_backend_round_trips_resource_template_available_sites(
    tmp_path,
) -> None:
    """本地 Backend 形态存储应无损保存并回读库位模板定义。

    参数：``tmp_path`` 是隔离数据库目录。返回：无；断言 Workspace 发布链路读取
    模板详情时不会丢失 ``available_sites``。
    """

    store = InventoryStore(str(tmp_path / "inventory.db"))
    service = BackendResourceService(store)

    identity = service.sync_resource_templates(
        [
            {
                "id": "site_device",
                "display_name": "库位设备",
                "registry_type": "device",
                "class": {},
                "available_sites": AVAILABLE_SITES,
            }
        ]
    )["templates"][0]

    template = service.get_resource_template(identity["uuid"])
    assert template["available_sites"] == AVAILABLE_SITES
    store.close()
