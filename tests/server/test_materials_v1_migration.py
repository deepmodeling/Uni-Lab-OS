"""materials.db v5 → v7：V1.0 库存概念（试剂 / 样品 / 当前内容物、状态历史、内容物预留、来源绑定、
工站入站、实验室布局）新表，批次库位列与条码唯一索引。"""

from __future__ import annotations

import sqlite3

from unilabos.server.database import DATABASE_SPECS, initialize_database
from unilabos.server.services.materials import MaterialsService
from tests.server.materials_published_specs import (
    MATERIALS_V5_CHECKSUM,
    MATERIALS_V6_TABLES,
    materials_spec_v5,
)
from tests.server.test_scheduler_inventory_authority import _material, _template


def _without_execution_guard(service: MaterialsService) -> None:
    service.executions.guard_mutation = lambda *_: None


def test_v5_materials_database_migrates_to_latest(tmp_path) -> None:
    v5 = materials_spec_v5()
    assert v5.checksum == MATERIALS_V5_CHECKSUM  # 夹具确实是发布过的 v5 结构
    path = tmp_path / "materials.db"
    connection = initialize_database(path, v5)
    try:
        legacy = MaterialsService(connection)
        _without_execution_guard(legacy)
        _template(legacy, "container-template", "container")
        _template(legacy, "reagent-template", "reagent")
        first, second, third = (_material(legacy, "container") for _ in range(3))
        with connection:
            # v5 批次表没有库位列（用 v5 的列集直接写入）
            connection.execute(
                "INSERT INTO inventory_lot(lot_uuid, template_uuid, batch_no, unit, quantity_total, "
                "quantity_available, quantity_reserved, quarantined, created_at_ms, updated_at_ms, version) "
                "VALUES ('lot-a', 'reagent-template', 'B1', 'ul', 25, 25, 0, 0, 1, 1, 1)"
            )
            # v5 没有条码唯一约束：两件物料贴了同一条码，第三件条码不同
            for uuid, barcode, created in ((first, "BC-1", 10), (second, "BC-1", 20), (third, "BC-2", 30)):
                connection.execute(
                    "UPDATE material SET barcode=?, created_at_ms=?, updated_at_ms=? WHERE material_uuid=?",
                    (barcode, created, max(created, 1), uuid),
                )
        sequence = legacy.latest_ledger_sequence()
    finally:
        connection.close()

    service = MaterialsService(path)
    try:
        spec = DATABASE_SPECS["materials"]
        conn = service.connection
        assert conn.execute("PRAGMA user_version").fetchone()[0] == spec.schema_version == 8
        assert [row[0] for row in conn.execute("SELECT version FROM schema_migration")] == [6, 7, 8]
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert MATERIALS_V6_TABLES <= tables
        # 重复条码让给最早创建的物料，后来者条码清空并保留原值供复核
        rows = {
            row["material_uuid"]: (row["barcode"], row["extra_json"])
            for row in conn.execute("SELECT material_uuid, barcode, extra_json FROM material")
        }
        assert rows[first][0] == "BC-1"
        assert rows[second][0] == "" and '"duplicate_barcode":"BC-1"' in rows[second][1]
        assert rows[third][0] == "BC-2"
        try:
            with conn:
                conn.execute("UPDATE material SET barcode='BC-2' WHERE material_uuid=?", (first,))
            raise AssertionError("条码唯一索引未生效")
        except sqlite3.IntegrityError:
            pass
        # 批次与账本原样保留，库位列为缺省值
        lot = service.get_inventory_lot("lot-a")
        assert (lot.quantity_total, lot.location_site_uuid, lot.warehouse_zone_id) == (25, None, "")
        assert service.latest_ledger_sequence() == sequence
        assert len(list(tmp_path.glob("materials.db.pre-v8-*.bak"))) == 1
    finally:
        service.close()


def test_quarantined_mass_lot_can_be_corrected_and_relocated(tmp_path) -> None:
    """隔离的质量 / 体积批次可按实测量校正并登记库位（盘点只支持离散计数）。"""

    import pytest

    from unilabos.protocol.materials import InventoryLotCorrection, InventoryLotInbound, InventoryMutation
    from unilabos.server.services.materials.core import (
        MaterialConflictError,
        MaterialNotFoundError,
        MaterialValidationError,
    )

    def mutation(operation: str, *, actor: str | None = "operator-1") -> InventoryMutation:
        from uuid import uuid4

        return InventoryMutation(
            command_uuid=str(uuid4()), effect_key=operation, operation=operation,
            actor_type="human", actor_uuid=actor,
        )

    service = MaterialsService(tmp_path / "materials.db")
    try:
        _template(service, "reagent-template", "reagent")
        service.inbound_inventory_lot(
            mutation("inbound_inventory_lot"),
            InventoryLotInbound(lot_uuid="lot-g", template_uuid="reagent-template", unit="g", quantity=100,
                                warehouse_zone_id="zone-cold"),
        )
        lot = service.get_inventory_lot("lot-g")
        assert lot.warehouse_zone_id == "zone-cold"
        quarantined = service.correct_inventory_lot(
            mutation("correct_inventory_lot"),
            InventoryLotCorrection(lot_uuid="lot-g", expected_version=lot.version, quarantined=True, reason="受潮"),
        ).data
        assert quarantined.quarantined
        corrected = service.correct_inventory_lot(
            mutation("correct_inventory_lot"),
            InventoryLotCorrection(lot_uuid="lot-g", expected_version=quarantined.version,
                                   measured_quantity=82.5, warehouse_zone_id="zone-waste", reason="称重"),
        ).data
        assert (corrected.quantity_total, corrected.quantity_available) == (82.5, 82.5)
        assert corrected.quarantined and corrected.warehouse_zone_id == "zone-waste"
        with pytest.raises(MaterialConflictError):
            service.correct_inventory_lot(
                mutation("correct_inventory_lot"),
                InventoryLotCorrection(lot_uuid="lot-g", expected_version=quarantined.version,
                                       measured_quantity=1, reason="过期版本"),
            )
        with pytest.raises(MaterialValidationError):
            service.correct_inventory_lot(
                mutation("correct_inventory_lot", actor=None),
                InventoryLotCorrection(lot_uuid="lot-g", expected_version=corrected.version,
                                       measured_quantity=1, reason="无操作人"),
            )
        with pytest.raises(MaterialNotFoundError) as missing:
            service.correct_inventory_lot(
                mutation("correct_inventory_lot"),
                InventoryLotCorrection(lot_uuid="lot-g", expected_version=corrected.version,
                                       location_site_uuid="no-such-site", reason="上架"),
            )
        assert missing.value.subject == "site"
    finally:
        service.close()
