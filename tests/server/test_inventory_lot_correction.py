"""批次校正：隔离的质量 / 体积批次按实测量校正并登记库位 / 仓储分区。"""

from __future__ import annotations

from unilabos.server.services.materials import MaterialsService
from tests.server.test_scheduler_inventory_authority import _template


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
