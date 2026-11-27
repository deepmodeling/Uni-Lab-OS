"""从已提交的物理分装台账读取数量事实，防止通用消费再次扣量。"""

from __future__ import annotations

import json
import math
import sqlite3
from collections.abc import Mapping
from typing import Any


def aliquot_consumption_fact(
    connection: sqlite3.Connection,
    record: Mapping[str, Any],
) -> dict[str, Any] | None:
    """按作业、库存及容器身份读取唯一分装扣量事实。

    只信任库存权威在完整 Permit/Fence 校验后写入的源瓶事件；设备请求不能
    自行声明已经扣过量。无对应事实返回 ``None``，重复或损坏事实关闭式失败。
    """

    if record.get("inventory_type") != "current_substance":
        return None
    rows = connection.execute(
        """SELECT entry_uuid,quantity_delta,quantity_unit,revision,delta_json
        FROM inventory_ledger
        WHERE op_type='current_substance.aliquot_source'
          AND aggregate_type='current_substance' AND aggregate_id=?
          AND workflow_node_job_uuid=? AND material_uuid=?
        ORDER BY ledger_id LIMIT 2""",
        (
            str(record["inventory_uuid"]),
            str(record["workflow_node_job_uuid"]),
            str(record["material_uuid"]),
        ),
    ).fetchall()
    if not rows:
        return None
    if len(rows) != 1:
        raise ValueError("同一作业存在多条分装来源扣量事实，须人工核对")
    row = rows[0]
    try:
        result = json.loads(row["delta_json"])["changes"]["result"]
        quantity = -float(row["quantity_delta"])
        remaining = float(result["source_quantity"])
        unit = str(row["quantity_unit"] or "").strip()
        targets = result["targets"]
        target_total = math.fsum(float(target["quantity"]) for target in targets)
        if (
            not row["entry_uuid"]
            or not unit
            or result["quantity_unit"] != unit
            or result["source_material_uuid"] != record["material_uuid"]
            or not math.isfinite(quantity)
            or not math.isfinite(remaining)
            or quantity < 0
            or remaining < 0
            or not targets
            or any(
                not math.isfinite(float(target["quantity"]))
                or float(target["quantity"]) < 0
                or target["quantity_unit"] != unit
                for target in targets
            )
            or not math.isclose(target_total, quantity, rel_tol=1e-12, abs_tol=1e-9)
            or int(row["revision"]) <= 0
        ):
            raise ValueError
    except (KeyError, TypeError, ValueError, OverflowError) as error:
        raise ValueError("已提交分装台账数量或身份损坏，须人工核对") from error
    return {
        "entry_uuid": str(row["entry_uuid"]),
        "actual_quantity": quantity,
        "quantity_unit": unit,
        "balance_before": remaining + quantity,
        "balance_after": remaining,
        "revision": int(row["revision"]),
    }
