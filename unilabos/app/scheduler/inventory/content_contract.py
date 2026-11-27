"""由本地 SQLite 支撑的样品与当前内容物 Backend 公共合同。"""

from __future__ import annotations

import copy
import json
import math
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from uuid import UUID, uuid4

from unilabos.app.scheduler.inventory.backend_contract import (
    INVALID_PARAMETER,
    RESOURCE_DATA_CONFLICT,
    RESOURCE_NOT_FOUND,
    BackendContractError,
)
from unilabos.app.scheduler.inventory.content_rules import (
    ensure_container_empty,
    require_container,
)
from unilabos.app.scheduler.inventory.dispatch_admission import (
    InventoryMutationConflict,
    assert_inventory_mutation_unclaimed,
)
from unilabos.app.scheduler.inventory.domain import new_event_id
from unilabos.app.scheduler.inventory.store import InventoryStore
from unilabos.app.scheduler.inventory.workflow_quantity import (
    WorkflowQuantityReservationError,
    assert_workflow_quantity_mutation_allowed,
)


def _now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _json(value: Any, fallback: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(str(value))
    except (TypeError, ValueError):
        return fallback


def _optional_text(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _non_negative(value: Any, field: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as error:
        raise BackendContractError(
            INVALID_PARAMETER, f"{field} must be non-negative"
        ) from error
    if not math.isfinite(number) or number < 0:
        raise BackendContractError(INVALID_PARAMETER, f"{field} must be non-negative")
    return number


def _page(page: int, page_size: int) -> Tuple[int, int]:
    return max(1, int(page or 1)), min(500, max(1, int(page_size or 20)))


def _observed_at(value: Any) -> str:
    if value in (None, ""):
        return _now()
    try:
        timestamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as error:
        raise BackendContractError(
            INVALID_PARAMETER, "observed_at must be an ISO timestamp"
        ) from error
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    return (
        timestamp.astimezone(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _milliseconds(value: str) -> int:
    return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000)


def _iso_milliseconds(value: Any) -> str:
    return (
        datetime.fromtimestamp(int(value) / 1000, timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _base_row(row: sqlite3.Row | Dict[str, Any]) -> Dict[str, Any]:
    item = dict(row)
    item.pop("deleted_at", None)
    item["meta_data"] = _json(item.get("meta_data"), {})
    return item


def _sample_row(row: sqlite3.Row | Dict[str, Any]) -> Dict[str, Any]:
    item = _base_row(row)
    # 这些列仍保留在物理表中以兼容 Backend schema，但当前公共输入不暴露。
    for key in ("sample_type", "source", "collected_at", "expires_at"):
        item.pop(key, None)
    return item


def _substance_row(row: sqlite3.Row | Dict[str, Any]) -> Dict[str, Any]:
    item = _base_row(row)
    item["composition"] = _json(item.get("composition"), [])
    return item


def _substance_detail(row: sqlite3.Row | Dict[str, Any]) -> Dict[str, Any]:
    item = _substance_row(row)
    item["components"] = [
        {
            "reagent_uuid": component["reagent_uuid"],
            "reagent_info_uuid": component["reagent_info_uuid"],
            "name": component["name"],
            "quantity": component["quantity"],
            "quantity_unit": component["quantity_unit"],
            "sort_order": index,
            **({"source_station_uuid": component["source_station_uuid"]}
               if "source_station_uuid" in component else {}),
        }
        for index, component in enumerate(item["composition"])
    ]
    return item


def _history_row(row: sqlite3.Row | Dict[str, Any]) -> Dict[str, Any]:
    item = dict(row)
    payload = _json(item.get("delta_json"), {})
    event_type = str(item.get("op_type") or "")
    if event_type.startswith("current_substance."):
        event_type = event_type.removeprefix("current_substance.")
    return {
        "uuid": item["entry_uuid"],
        "material_uuid": item["material_uuid"],
        "event_type": event_type,
        "operator_type": item.get("actor") or "system",
        "from_site_uuid": None,
        "to_site_uuid": None,
        "changes": _json(payload.get("changes"), {}),
        "extension": _json(payload.get("extension"), {}),
        "trace_id": item.get("trace_id") or None,
        "recorded_at": _iso_milliseconds(item["occurred_at"]),
        "workflow_task_uuid": item.get("workflow_task_uuid")
        or payload.get("workflow_task_uuid"),
        "workflow_node_job_uuid": item.get("workflow_node_job_uuid")
        or payload.get("workflow_node_job_uuid"),
        "subject_type": item["subject_type"],
        "subject_uuid": item["aggregate_id"],
        "quantity_delta": item.get("quantity_delta"),
        "quantity_unit": item.get("quantity_unit"),
        "revision": item.get("revision"),
    }


class BackendContainerContentService:
    """管理容器上的样品与当前混合物，复用统一库存库、台账和发件箱。"""

    def __init__(
        self,
        store: InventoryStore,
        *,
        edge_id: str = "edge-default",
        lab_id: str = "edge-lab",
    ) -> None:
        self.store = store
        self.edge_id = edge_id
        self.lab_id = lab_id

    # Sample -----------------------------------------------------------

    def create_sample(self, values: Dict[str, Any]) -> Dict[str, Any]:
        with self.store.transaction() as conn:
            return self.create_sample_in_transaction(conn, values)

    def create_sample_in_transaction(
        self, conn: sqlite3.Connection, values: Dict[str, Any],
        *, historical_fields: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """让交接等复合写入复用样品校验；样品、保护与边界事实可同事务提交。"""
        material_uuid, code, name, quantity, unit = self._sample_values(values)
        identity, now = str(uuid4()), _now()
        try:
            require_container(conn, material_uuid)
            self._assert_sample_mutable(conn, material_uuid)
            ensure_container_empty(conn, material_uuid)
            conn.execute(
                """INSERT INTO sample(uuid,create_time,update_time,description,meta_data,
                material_uuid,code,name,quantity,quantity_unit) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (identity, now, now, _optional_text(values.get("description")),
                 _dump(values.get("meta_data") or {}), material_uuid, code, name, quantity, unit),
            )
        except sqlite3.IntegrityError as error:
            raise BackendContractError(
                RESOURCE_DATA_CONFLICT, "sample code or selected container is already used"
            ) from error
        if historical_fields is not None:
            # 仅交接恢复入口接受历史扩展；不把远端字段混入通用样品创建输入。
            from .handoff_content import validate_sample_history

            validate_sample_history(historical_fields)
            conn.execute(
                "UPDATE container_content SET sample_type=?,source=?,collected_at=?,expires_at=? "
                "WHERE uuid=? AND kind='sample'",
                (*(historical_fields[key] for key in ("sample_type", "source", "collected_at", "expires_at")), identity),
            )
        return _sample_row(conn.execute("SELECT * FROM sample WHERE uuid=?", (identity,)).fetchone())

    @staticmethod
    def _assert_sample_mutable(conn: sqlite3.Connection, material_uuid: str) -> None:
        try:
            assert_inventory_mutation_unclaimed(conn, material_uuids=(material_uuid,))
        except InventoryMutationConflict as error:
            raise BackendContractError(RESOURCE_DATA_CONFLICT, str(error)) from error

    def get_sample(self, identity: str) -> Dict[str, Any]:
        row = self.store.query_one(
            "SELECT * FROM sample WHERE uuid=? AND deleted_at IS NULL",
            (identity,),
        )
        if row is None:
            raise BackendContractError(RESOURCE_NOT_FOUND, "sample does not exist")
        return _sample_row(row)

    def list_samples(
        self,
        *,
        page: int = 1,
        page_size: int = 20,
        material_uuid: str = "",
        code: str = "",
        name: str = "",
        keyword: str = "",
        barcode: str = "",
    ) -> Dict[str, Any]:
        page, page_size = _page(page, page_size)
        where = ["sample.deleted_at IS NULL", "material.deleted_at IS NULL"]
        params: List[Any] = []
        if material_uuid.strip():
            where.append("sample.material_uuid=?")
            params.append(material_uuid.strip())
        for column, value in (("sample.code", code), ("sample.name", name)):
            if value.strip():
                where.append(f"LOWER({column}) LIKE LOWER(?)")
                params.append(f"%{value.strip()}%")
        if keyword.strip():
            where.append(
                "(LOWER(sample.code) LIKE LOWER(?) OR "
                "LOWER(sample.name) LIKE LOWER(?) OR "
                "LOWER(material.barcode) LIKE LOWER(?))"
            )
            params.extend([f"%{keyword.strip()}%"] * 3)
        if barcode.strip():
            where.append("LOWER(material.barcode) LIKE LOWER(?)")
            params.append(f"%{barcode.strip()}%")
        clause = " AND ".join(where)
        count = self.store.query_one(
            "SELECT COUNT(*) AS count FROM sample JOIN material "
            "ON material.uuid=sample.material_uuid WHERE " + clause,
            tuple(params),
        ) or {"count": 0}
        rows = self.store.query_all(
            "SELECT sample.* FROM sample JOIN material "
            "ON material.uuid=sample.material_uuid WHERE "
            + clause
            + " ORDER BY sample.create_time DESC,sample.uuid DESC LIMIT ? OFFSET ?",
            (*params, page_size, (page - 1) * page_size),
        )
        return {
            "items": [_sample_row(row) for row in rows],
            "total": int(count["count"]),
            "page": page,
            "page_size": page_size,
        }

    def update_sample(self, identity: str, values: Dict[str, Any]) -> Dict[str, Any]:
        current = self.get_sample(identity)
        material_uuid, code, name, quantity, unit = self._sample_values(values)
        if material_uuid != current["material_uuid"]:
            raise BackendContractError(
                INVALID_PARAMETER, "material_uuid cannot be changed"
            )
        now = _now()
        try:
            with self.store.transaction() as conn:
                self._assert_sample_mutable(conn, material_uuid)
                cursor = conn.execute(
                    """UPDATE container_content SET update_time=?,description=?,meta_data=?,
                    code=?,name=?,quantity=?,quantity_unit=?
                    WHERE uuid=? AND kind='sample' AND deleted_at IS NULL""",
                    (
                        now,
                        _optional_text(values.get("description")),
                        _dump(values.get("meta_data") or {}),
                        code,
                        name,
                        quantity,
                        unit,
                        identity,
                    ),
                )
                if cursor.rowcount != 1:
                    raise BackendContractError(
                        RESOURCE_NOT_FOUND, "sample does not exist"
                    )
        except BackendContractError:
            raise
        except sqlite3.IntegrityError as error:
            raise BackendContractError(
                RESOURCE_DATA_CONFLICT, "sample code is already used"
            ) from error
        return self.get_sample(identity)

    def delete_sample(self, identity: str) -> None:
        current = self.get_sample(identity)
        now = _now()
        with self.store.transaction() as conn:
            self._assert_sample_mutable(conn, current["material_uuid"])
            cursor = conn.execute(
                "UPDATE container_content SET deleted_at=?,update_time=? "
                "WHERE uuid=? AND kind='sample' AND deleted_at IS NULL",
                (now, now, identity),
            )
            if cursor.rowcount != 1:
                raise BackendContractError(RESOURCE_NOT_FOUND, "sample does not exist")

    @staticmethod
    def _sample_values(values: Dict[str, Any]) -> Tuple[str, str, str, float, str]:
        material_uuid = str(values.get("material_uuid") or "")
        code = str(values.get("code") or "").strip()
        name = str(values.get("name") or "").strip()
        unit = str(values.get("quantity_unit") or "").strip()
        if not material_uuid or not code or not name or not unit:
            raise BackendContractError(
                INVALID_PARAMETER,
                "material_uuid, code, name and quantity_unit are required",
            )
        return (
            material_uuid,
            code,
            name,
            _non_negative(values.get("quantity"), "quantity"),
            unit,
        )

    # CurrentSubstance -------------------------------------------------

    def create_current_substance(self, values: Dict[str, Any]) -> Dict[str, Any]:
        with self.store.transaction() as conn:
            return self.create_current_substance_in_transaction(conn, values)

    def create_current_substance_in_transaction(
        self, conn: sqlite3.Connection, values: Dict[str, Any], *,
        historical_composition: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """原子创建当前内容；交接可恢复带工站来源的已核验历史组分，绝不创建虚假试剂瓶。"""
        normalized = self._substance_values(values)
        identity, now = str(uuid4()), _now()
        try:
            require_container(conn, normalized["material_uuid"])
            try:
                assert_inventory_mutation_unclaimed(conn, material_uuids=(normalized["material_uuid"],))
            except InventoryMutationConflict as error:
                raise BackendContractError(RESOURCE_DATA_CONFLICT, str(error)) from error
            ensure_container_empty(conn, normalized["material_uuid"])
            if historical_composition is None:
                composition = self._build_composition(conn, values.get("components") or [])
            else:
                from .handoff_content import validate_local_composition

                validate_local_composition(conn, historical_composition)
                composition = copy.deepcopy(historical_composition)
            conn.execute(
                """INSERT INTO current_substance(
                uuid,create_time,update_time,description,meta_data,material_uuid,
                name,composition,quantity,quantity_unit,physical_state,revision,
                observed_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (identity, now, now, normalized["description"], normalized["meta_data"],
                 normalized["material_uuid"], normalized["name"], _dump(composition),
                 normalized["quantity"], normalized["quantity_unit"],
                 normalized["physical_state"], 1, normalized["observed_at"]),
            )
            self._append_substance_history(
                conn, identity=identity, material_uuid=normalized["material_uuid"],
                event_type="handoff_received" if historical_composition is not None else "add",
                quantity_delta=normalized["quantity"], revision=1, values=values,
            )
        except sqlite3.IntegrityError as error:
            raise BackendContractError(
                RESOURCE_DATA_CONFLICT, "selected container already has current substance",
            ) from error
        return _substance_detail(conn.execute("SELECT * FROM current_substance WHERE uuid=?", (identity,)).fetchone())

    def get_current_substance(self, identity: str) -> Dict[str, Any]:
        row = self.store.query_one(
            "SELECT * FROM current_substance WHERE uuid=? AND deleted_at IS NULL",
            (identity,),
        )
        if row is None:
            raise BackendContractError(
                RESOURCE_NOT_FOUND, "current substance does not exist"
            )
        return _substance_detail(row)

    def get_current_substance_by_material(self, material_uuid: str) -> Dict[str, Any]:
        row = self.store.query_one(
            "SELECT * FROM current_substance "
            "WHERE material_uuid=? AND deleted_at IS NULL",
            (material_uuid,),
        )
        if row is None:
            raise BackendContractError(
                RESOURCE_NOT_FOUND, "material has no current substance"
            )
        return _substance_detail(row)

    def list_current_substances(
        self,
        *,
        page: int = 1,
        page_size: int = 20,
        material_uuid: str = "",
    ) -> Dict[str, Any]:
        page, page_size = _page(page, page_size)
        where = ["deleted_at IS NULL"]
        params: List[Any] = []
        if material_uuid.strip():
            where.append("material_uuid=?")
            params.append(material_uuid.strip())
        clause = " AND ".join(where)
        count = self.store.query_one(
            "SELECT COUNT(*) AS count FROM current_substance WHERE " + clause,
            tuple(params),
        ) or {"count": 0}
        rows = self.store.query_all(
            "SELECT * FROM current_substance WHERE "
            + clause
            + " ORDER BY update_time DESC,uuid DESC LIMIT ? OFFSET ?",
            (*params, page_size, (page - 1) * page_size),
        )
        return {
            "items": [_substance_row(row) for row in rows],
            "total": int(count["count"]),
            "page": page,
            "page_size": page_size,
        }

    def update_current_substance(
        self, identity: str, values: Dict[str, Any]
    ) -> Dict[str, Any]:
        """按期望修订更新当前内容物，并保护活动工作流数量预留。

        参数：内容物身份与完整更新值。返回更新后的 Backend 同形内容物。异常：
        实例不存在、修订冲突、字段非法或修改后余量侵占活动预留时抛
        ``BackendContractError``；成功写入与历史事件在同一库存事务提交。
        """

        current = self.get_current_substance(identity)
        normalized = self._substance_values(values)
        if normalized["material_uuid"] != current["material_uuid"]:
            raise BackendContractError(
                INVALID_PARAMETER, "material_uuid cannot be changed"
            )
        expected = values.get("expected_revision")
        if expected is not None and int(expected) != int(current["revision"]):
            raise BackendContractError(
                RESOURCE_DATA_CONFLICT, "current substance revision has changed"
            )
        revision = int(current["revision"]) + 1
        delta = normalized["quantity"] - float(current["quantity"])
        event_type = "add" if delta > 0 else "adjust"
        now = _now()
        with self.store.transaction() as conn:
            try:
                assert_inventory_mutation_unclaimed(
                    conn, material_uuids=(current["material_uuid"],)
                )
            except InventoryMutationConflict as error:
                raise BackendContractError(
                    RESOURCE_DATA_CONFLICT, str(error)
                ) from error
            try:
                assert_workflow_quantity_mutation_allowed(
                    conn,
                    inventory_type="current_substance",
                    inventory_uuid=identity,
                    quantity=float(normalized["quantity"]),
                    current_unit=str(current["quantity_unit"]),
                    next_unit=str(normalized["quantity_unit"]),
                )
            except WorkflowQuantityReservationError as error:
                raise BackendContractError(
                    RESOURCE_DATA_CONFLICT, str(error)
                ) from error
            composition = self._build_composition(
                conn, values.get("components") or [], existing=current["composition"]
            )
            cursor = conn.execute(
                """UPDATE container_content SET update_time=?,description=?,meta_data=?,
                name=?,composition=?,quantity=?,quantity_unit=?,physical_state=?,
                revision=?,observed_at=?
                WHERE uuid=? AND kind='substance' AND deleted_at IS NULL AND revision=?""",
                (
                    now,
                    normalized["description"],
                    normalized["meta_data"],
                    normalized["name"],
                    _dump(composition),
                    normalized["quantity"],
                    normalized["quantity_unit"],
                    normalized["physical_state"],
                    revision,
                    normalized["observed_at"],
                    identity,
                    current["revision"],
                ),
            )
            if cursor.rowcount != 1:
                raise BackendContractError(
                    RESOURCE_DATA_CONFLICT,
                    "current substance revision has changed",
                )
            if delta != 0:
                self._append_substance_history(
                    conn,
                    identity=identity,
                    material_uuid=current["material_uuid"],
                    event_type=event_type,
                    quantity_delta=delta,
                    revision=revision,
                    values=values,
                )
        return self.get_current_substance(identity)

    def delete_current_substance(self, identity: str) -> None:
        """软删除当前内容物；活动工作流仍有数量预留时关闭式拒绝。

        参数：内容物 UUID。返回无。异常：实例不存在时返回资源未找到；删除会把
        可用数量降到预留以下时返回数据冲突。重复删除仍按不存在处理，不产生重复
        库存事实。
        """

        current = self.get_current_substance(identity)
        now = _now()
        with self.store.transaction() as conn:
            try:
                assert_inventory_mutation_unclaimed(
                    conn, material_uuids=(current["material_uuid"],)
                )
            except InventoryMutationConflict as error:
                raise BackendContractError(
                    RESOURCE_DATA_CONFLICT, str(error)
                ) from error
            try:
                assert_workflow_quantity_mutation_allowed(
                    conn,
                    inventory_type="current_substance",
                    inventory_uuid=identity,
                    quantity=0,
                    current_unit=str(current["quantity_unit"]),
                    next_unit=str(current["quantity_unit"]),
                )
            except WorkflowQuantityReservationError as error:
                raise BackendContractError(
                    RESOURCE_DATA_CONFLICT, str(error)
                ) from error
            cursor = conn.execute(
                "UPDATE container_content SET deleted_at=?,update_time=? "
                "WHERE uuid=? AND kind='substance' AND deleted_at IS NULL",
                (now, now, identity),
            )
            if cursor.rowcount != 1:
                raise BackendContractError(
                    RESOURCE_NOT_FOUND, "current substance does not exist"
                )

    def get_substance_history(self, identity: str) -> Dict[str, Any]:
        row = self.store.query_one(
            "SELECT * FROM inventory_ledger WHERE entry_uuid=? "
            "AND subject_type='current_substance'",
            (identity,),
        )
        if row is None:
            raise BackendContractError(
                RESOURCE_NOT_FOUND, "substance history does not exist"
            )
        return _history_row(row)

    def list_substance_history(
        self, material_uuid: str, *, page: int = 1, page_size: int = 20
    ) -> Dict[str, Any]:
        page, page_size = _page(page, page_size)
        count = self.store.query_one(
            "SELECT COUNT(*) AS count FROM inventory_ledger "
            "WHERE material_uuid=? AND subject_type='current_substance'",
            (material_uuid,),
        ) or {"count": 0}
        rows = self.store.query_all(
            "SELECT * FROM inventory_ledger WHERE material_uuid=? "
            "AND subject_type='current_substance' "
            "ORDER BY occurred_at DESC,ledger_id DESC LIMIT ? OFFSET ?",
            (material_uuid, page_size, (page - 1) * page_size),
        )
        return {
            "items": [_history_row(row) for row in rows],
            "page": page,
            "page_size": page_size,
            "has_more": page * page_size < int(count["count"]),
        }

    @staticmethod
    def _substance_values(values: Dict[str, Any]) -> Dict[str, Any]:
        material_uuid = str(values.get("material_uuid") or "")
        unit = str(values.get("quantity_unit") or "").strip()
        state = str(values.get("physical_state") or "").strip().lower()
        if not material_uuid or not unit or not state:
            raise BackendContractError(
                INVALID_PARAMETER,
                "material_uuid, quantity_unit and physical_state are required",
            )
        return {
            "material_uuid": material_uuid,
            "name": _optional_text(values.get("name")),
            "quantity": _non_negative(values.get("quantity"), "quantity"),
            "quantity_unit": unit,
            "physical_state": state,
            "observed_at": _observed_at(values.get("observed_at")),
            "description": _optional_text(values.get("description")),
            "meta_data": _dump(values.get("meta_data") or {}),
        }

    @staticmethod
    def _build_composition(
        conn: sqlite3.Connection, components: List[Dict[str, Any]], *,
        existing: Optional[List[Dict[str, Any]]] = None,
    ) -> List[Dict[str, Any]]:
        seen: set[tuple[Optional[str], str]] = set()
        composition: List[Dict[str, Any]] = []
        for index, component in enumerate(components):
            reagent_uuid = str(component.get("reagent_uuid") or "")
            unit = str(component.get("quantity_unit") or "").strip()
            if not reagent_uuid or not unit:
                raise BackendContractError(
                    INVALID_PARAMETER,
                    f"components[{index}] reagent_uuid and quantity_unit are required",
                )
            origin = component.get("source_station_uuid")
            if origin is not None:
                try:
                    if not isinstance(origin, str) or str(UUID(origin)) != origin:
                        raise ValueError
                except (ValueError, TypeError):
                    raise BackendContractError(INVALID_PARAMETER, "historical component station identity is invalid") from None
            key = (origin, reagent_uuid)
            if key in seen:
                raise BackendContractError(
                    INVALID_PARAMETER,
                    f"duplicate component reagent_uuid {reagent_uuid}",
                )
            seen.add(key)
            matches = [item for item in existing or []
                       if item.get("source_station_uuid") == origin and item["reagent_uuid"] == reagent_uuid]
            if len(matches) == 1 and all(component.get(field) == matches[0][field]
                                        for field in ("reagent_info_uuid", "name", "quantity", "quantity_unit")):
                # 普通标签编辑也必须保留首次形成的化学事实，不能重新查询目录覆盖历史。
                composition.append(copy.deepcopy(matches[0]))
                continue
            if origin is not None:
                raise BackendContractError(INVALID_PARAMETER, "historical component must match its frozen source fact")
            quantity = _non_negative(
                component.get("quantity"), f"components[{index}].quantity"
            )
            row = conn.execute(
                """SELECT reagent.uuid,reagent.reagent_info_uuid,
                reagent.concentration_value,reagent.concentration_unit,
                reagent_info.name,reagent_info.cas,reagent_info.inchi_key,reagent_info.molecular_formula
                FROM reagent JOIN reagent_info
                  ON reagent_info.uuid=reagent.reagent_info_uuid
                WHERE reagent.uuid=? AND reagent.deleted_at IS NULL
                  AND reagent_info.deleted_at IS NULL""",
                (reagent_uuid,),
            ).fetchone()
            if row is None:
                raise BackendContractError(
                    INVALID_PARAMETER,
                    f"component reagent {reagent_uuid} not found",
                )
            chemistry = {field: row[field] for field in ("cas", "inchi_key", "molecular_formula")}
            if matches and matches[0].get("chemical_identity") != chemistry:
                raise BackendContractError(INVALID_PARAMETER, "historical component chemical identity requires verification")
            composition.append(
                {
                    "chemical_identity": chemistry,
                    "reagent_uuid": row["uuid"],
                    "reagent_info_uuid": row["reagent_info_uuid"],
                    "name": row["name"],
                    "quantity": quantity,
                    "quantity_unit": unit,
                    **(
                        {"concentration_value": row["concentration_value"]}
                        if row["concentration_value"] is not None
                        else {}
                    ),
                    **(
                        {"concentration_unit": row["concentration_unit"]}
                        if row["concentration_unit"] is not None
                        else {}
                    ),
                }
            )
        return composition

    def _append_substance_history(
        self,
        conn: sqlite3.Connection,
        *,
        identity: str,
        material_uuid: str,
        event_type: str,
        quantity_delta: float,
        revision: int,
        values: Dict[str, Any],
    ) -> None:
        row = conn.execute(
            "SELECT * FROM current_substance WHERE uuid=?", (identity,)
        ).fetchone()
        if row is None:
            raise BackendContractError(
                RESOURCE_NOT_FOUND, "current substance does not exist"
            )
        result = {
            "name": row["name"],
            "composition": _json(row["composition"], []),
            "quantity": row["quantity"],
            "quantity_unit": row["quantity_unit"],
            "physical_state": row["physical_state"],
            "revision": row["revision"],
            "description": row["description"],
        }
        occurred_at = _milliseconds(str(row["observed_at"]))
        entry_uuid = new_event_id(occurred_at)
        workflow_task_uuid = _optional_text(values.get("workflow_task_uuid"))
        workflow_node_job_uuid = _optional_text(values.get("workflow_node_job_uuid"))
        payload = {
            "changes": {"result": result},
            "extension": _json(row["meta_data"], {}),
            "workflow_task_uuid": workflow_task_uuid,
            "workflow_node_job_uuid": workflow_node_job_uuid,
            "quantity_delta": quantity_delta,
            "quantity_unit": row["quantity_unit"],
            "revision": revision,
        }
        InventoryStore.tx_append_inventory_event(
            conn,
            entry_uuid=entry_uuid,
            edge_id=self.edge_id,
            lab_id=self.lab_id,
            occurred_at=occurred_at,
            aggregate_type="current_substance",
            aggregate_id=identity,
            aggregate_version=revision,
            event_type=f"current_substance.{event_type}",
            payload=payload,
            actor="frontend",
            trace_id=_optional_text(values.get("trace_id")) or "",
            material_uuid=material_uuid,
            subject_type="current_substance",
            quantity_delta=quantity_delta,
            quantity_unit=row["quantity_unit"],
            revision=revision,
            workflow_task_uuid=workflow_task_uuid,
            workflow_node_job_uuid=workflow_node_job_uuid,
        )


__all__ = ["BackendContainerContentService"]
