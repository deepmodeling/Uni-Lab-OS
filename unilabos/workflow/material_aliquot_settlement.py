"""成功分装作业到库存内容物事实的原子 PhysicalSettlement。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from unilabos.app.scheduler.inventory.dispatch_admission import DispatchFence
from unilabos.app.scheduler.inventory.station_resource import (
    AliquotReceipt,
    MaterialAliquotCommand,
    StationResourceError,
    StationResourceInventory,
)
from unilabos.workflow.store import StoreConflict


class MaterialAliquotSettlement:
    """校验完整回执和 Permit 后，把分装闭集交给库存单事务结算。"""

    def __init__(self, inventory: StationResourceInventory | None) -> None:
        self._inventory = inventory

    def settle_success(
        self,
        *,
        job: Mapping[str, Any],
        execution_claim: Mapping[str, Any] | None,
        receipts: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any] | None:
        expected = job.get("expected_change_set")
        if not isinstance(expected, Mapping) or expected.get("kind") != "material_content_aliquot":
            if receipts:
                raise StoreConflict("非分装作业上报了 material_aliquot_receipts")
            return None
        if self._inventory is None or not isinstance(execution_claim, Mapping):
            raise StoreConflict("分装作业缺少库存权威或完整 Claim")
        job_uuid = _text(job.get("uuid"), "job.uuid")
        raw_fences = execution_claim.get("fences")
        if not isinstance(raw_fences, list) or not raw_fences:
            raise StoreConflict("分装作业缺少库存 Fence")
        try:
            fences = tuple(
                DispatchFence(
                    lock_key=_text(item.get("lock_key"), "fence.lock_key"),
                    fencing_token=int(item.get("fencing_token")),
                )
                for item in raw_fences
                if isinstance(item, Mapping)
            )
            normalized_receipts = tuple(
                AliquotReceipt(
                    target_material_uuid=_text(
                        item.get("target_material_uuid"),
                        "receipt.target_material_uuid",
                    ),
                    actual_quantity=float(item.get("actual_quantity")),
                    quantity_unit=_text(item.get("quantity_unit"), "receipt.quantity_unit"),
                )
                for item in receipts
                if isinstance(item, Mapping)
            )
            attempt = int(job.get("attempt"))
        except (TypeError, ValueError) as error:
            raise StoreConflict("分装作业回执、Fence 或 attempt 损坏") from error
        if (
            len(fences) != len(raw_fences)
            or len(normalized_receipts) != len(receipts)
            or any(fence.fencing_token <= 0 for fence in fences)
            or attempt <= 0
            or int(execution_claim.get("attempt") or 0) != attempt
        ):
            raise StoreConflict("分装作业回执、Fence 或 attempt 不完整")
        try:
            return dict(
                self._inventory.settle_material_aliquot(
                    MaterialAliquotCommand(
                        source_material_uuid=_text(
                            expected.get("source_material_uuid"),
                            "expected_change_set.source_material_uuid",
                        ),
                        receipts=normalized_receipts,
                        effect_uuid=_text(job.get("dispatch_effect_uuid"), "effect_uuid"),
                        claim_uuid=_text(execution_claim.get("claim_uuid"), "claim_uuid"),
                        job_uuid=job_uuid,
                        attempt=attempt,
                        parameter_hash=_text(
                            job.get("dispatch_parameter_hash"), "parameter_hash"
                        ),
                        expected_change_set=dict(expected),
                        fences=fences,
                    )
                )
            )
        except StationResourceError as error:
            raise StoreConflict(error.message) from error


def _text(value: Any, field: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise StoreConflict(f"分装物理结算缺少 {field}")
    return normalized


__all__ = ["MaterialAliquotSettlement"]
