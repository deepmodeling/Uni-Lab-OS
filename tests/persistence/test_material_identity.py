"""SQLite/PostgreSQL 升级保留 UUID 和条码，普通物料名称允许重复。"""

import pytest

from unilabos.app.scheduler.inventory.backend_contract import (
    BackendContractError,
    BackendResourceService,
)
from unilabos.app.scheduler.inventory.store import InventoryStore


def test_existing_root_name_index_is_replaced(database_mode, tmp_path):
    path = str(tmp_path / "identity.db")
    store = InventoryStore(path)
    backend = BackendResourceService(store)
    template = backend.sync_resource_templates(
        [
            {
                "id": "identity.vessel",
                "display_name": "容器",
                "registry_type": "material",
                "class": {},
            }
        ]
    )["templates"][0]["uuid"]
    original = backend.create_material(
        {"resource_template_uuid": template, "name": "同名瓶", "barcode": "ORIGINAL"}
    )
    with store.transaction() as connection:
        connection.execute("DROP INDEX IF EXISTS ux_device_root_name_active")
        connection.execute(
            "CREATE UNIQUE INDEX ux_material_root_name_active ON material(LOWER(name)) WHERE parent_uuid IS NULL AND deleted_at IS NULL"
        )
        if database_mode == "sqlite":
            connection.execute("PRAGMA user_version=19")
    store.close()
    store = InventoryStore(path)
    try:
        backend = BackendResourceService(store)
        for barcode in (None, "", "  ", "SECOND"):
            material = backend.create_material(
                {
                    "resource_template_uuid": template,
                    "name": "同名瓶",
                    "barcode": barcode,
                }
            )
            assert material["uuid"] != original["uuid"]
        with pytest.raises(BackendContractError):
            backend.create_material(
                {
                    "resource_template_uuid": template,
                    "name": "其他名称",
                    "barcode": " original ",
                }
            )
        assert backend.get_material(original["uuid"])["barcode"] == "ORIGINAL"
    finally:
        store.close()
