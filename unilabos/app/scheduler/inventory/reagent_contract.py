"""由本地 SQLite 支撑的 Backend 同形试剂（Reagent）领域合同。"""

from __future__ import annotations

import json
import math
import re
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from uuid import uuid4

from unilabos.app.scheduler.inventory.backend_contract import (
    INVALID_PARAMETER,
    MATERIAL_NOT_FOUND,
    REAGENT_INFO_IN_USE,
    RESOURCE_DATA_CONFLICT,
    RESOURCE_NOT_FOUND,
    BackendContractError,
)
from unilabos.app.scheduler.inventory.capacity import (
    CAPACITY_KEY,
    LOADING_LIMITS_KEY,
    material_capacity,
    maximum_capacity,
    normalize_capacity,
    reagent_metadata,
    validate_material_config,
    validate_quantity,
)
from unilabos.app.scheduler.inventory.dispatch_admission import (
    InventoryMutationConflict,
    assert_inventory_mutation_unclaimed,
)
from unilabos.app.scheduler.inventory.domain import new_event_id
from unilabos.app.scheduler.inventory.store import InventoryStore
from unilabos.app.scheduler.inventory.workflow_quantity import (
    WorkflowQuantityReservationError,
    active_workflow_reserved_quantities,
    assert_workflow_quantity_mutation_allowed,
    subtract_quantity,
)

_PHYSICAL_STATES = {"solid", "liquid", "gas", "other", "unknown"}
_CAS_PATTERN = re.compile(r"^(\d{2,7})-(\d{2})-(\d)$")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _milliseconds(value: Any) -> int:
    """把 Backend 接收的 ISO 时间或毫秒值转换成 Edge 台账 UTC 毫秒。"""

    if isinstance(value, (int, float)):
        return int(value)
    timestamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    return int(timestamp.timestamp() * 1000)


def _iso_milliseconds(value: Any) -> str:
    """把 Edge 台账 UTC 毫秒投影成 Backend 历史接口的 ISO 时间。"""

    return datetime.fromtimestamp(
        int(value) / 1000, timezone.utc
    ).isoformat(timespec="milliseconds").replace("+00:00", "Z")


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


def _positive_number(value: Any, field: str) -> Optional[float]:
    if value is None:
        return None
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise BackendContractError(INVALID_PARAMETER, f"{field} must be positive")
    return number


def _non_negative_number(value: Any, field: str) -> float:
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise BackendContractError(INVALID_PARAMETER, f"{field} must be non-negative")
    return number


def _physical_state(value: Any) -> str:
    state = str(value or "").strip().lower()
    if not state:
        return "unknown"
    if state not in _PHYSICAL_STATES:
        raise BackendContractError(INVALID_PARAMETER, "physical_state is invalid")
    return state


def normalize_cas(value: Any) -> str:
    """校验并规范化 CAS 登记号。

    参数：``value`` 是待校验文本。返回：去空格后的 CAS。异常：格式或校验位
    不合法时抛出 Backend 数值合同的参数错误。
    """

    cas = str(value or "").strip()
    match = _CAS_PATTERN.fullmatch(cas)
    if match is None:
        raise BackendContractError(INVALID_PARAMETER, "CAS number is invalid")
    digits = "".join(match.groups()[:2])
    checksum = sum(int(char) * weight for weight, char in enumerate(reversed(digits), 1)) % 10
    if checksum != int(match.group(3)):
        raise BackendContractError(INVALID_PARAMETER, "CAS checksum is invalid")
    return cas


def _page(page: int, page_size: int) -> Tuple[int, int]:
    return max(1, int(page or 1)), min(500, max(1, int(page_size or 20)))


def _info_row(row: sqlite3.Row | Dict[str, Any]) -> Dict[str, Any]:
    item = dict(row)
    item["aliases"] = _json(item.get("aliases"), [])
    item["meta_data"] = _json(item.get("meta_data"), {})
    for key in (
        "structure_3d_identity_key",
        "structure_3d_format",
        "structure_3d_source",
        "structure_3d_source_id",
        "structure_3d_content",
        "structure_3d_checksum",
        "structure_3d_status",
        "structure_3d_generated_at",
        "structure_3d_error_message",
    ):
        item.pop(key, None)
    return item


def _reagent_row(row: sqlite3.Row | Dict[str, Any]) -> Dict[str, Any]:
    item = dict(row)
    item["meta_data"] = _json(item.get("meta_data"), {})
    item["aliases"] = _json(item.get("aliases"), [])
    return item


def _reagent_history_state(
    row: sqlite3.Row | Dict[str, Any], capacity: Dict[str, float]
) -> Dict[str, Any]:
    """生成台账业务快照，供变更前后及同步事件使用同一字段合同。"""

    reagent = dict(row)
    metadata = _json(reagent.get("meta_data"), {})
    return {
        key: reagent.get(key)
        for key in (
            "quantity", "quantity_unit", "concentration_value", "concentration_unit",
            "physical_state", "density_g_per_ml", "revision", "description",
        )
    } | {
        "meta_data": metadata,
        LOADING_LIMITS_KEY: normalize_capacity(metadata.get(LOADING_LIMITS_KEY)),
        "container_capacity": capacity,
        "maximum_capacity": maximum_capacity(capacity, metadata),
    }


def _ledger_row(row: sqlite3.Row | Dict[str, Any]) -> Dict[str, Any]:
    """把 Edge 统一台账行投影为 Backend 的物料历史返回结构。"""

    item = dict(row)
    payload = _json(item.get("delta_json"), {})
    event_type = str(item.get("op_type") or "")
    if event_type.startswith("reagent."):
        event_type = event_type.removeprefix("reagent.")
    return {
        "uuid": item["entry_uuid"],
        "material_uuid": item["material_uuid"],
        "event_type": event_type,
        "operator_type": item.get("actor") or "system",
        "from_site_uuid": None,
        "to_site_uuid": None,
        # 分装等复合操作按 causation_id 聚合同一次动作的多条台账；读侧原样透出。
        "causation_id": str(item.get("causation_id") or ""),
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
        "quantity_delta": item.get("quantity_delta")
        if item.get("quantity_delta") is not None
        else payload.get("quantity_delta"),
        "quantity_unit": item.get("quantity_unit")
        or payload.get("quantity_unit"),
        "revision": item.get("revision")
        if item.get("revision") is not None
        else payload.get("revision"),
    }


class BackendReagentService:
    """管理本地化学品身份、容器级试剂实例与不可变变更台账。"""

    def __init__(
        self,
        store: InventoryStore,
        *,
        edge_id: str = "edge-default",
        lab_id: str = "edge-lab",
        compound_source: Any = None,
    ):
        """绑定库存库与发件箱身份。

        参数：``store`` 是 OS Local 的唯一 ``inventory.db``；``edge_id`` 和
        ``lab_id`` 写入试剂变化的事务发件箱。返回：初始化后的领域服务。
        """

        self.store = store
        self.edge_id = edge_id
        self.lab_id = lab_id
        self.compound_source = compound_source

    def create_reagent_info(self, values: Dict[str, Any]) -> Dict[str, Any]:
        """登记一条不依赖库存存在的化学品身份。

        参数：``values`` 是名称、物态及可选化学属性。返回：已提交身份详情。
        异常：必填值、CAS、数值或唯一身份冲突时抛出 Backend 合同错误。
        """

        with self.store.transaction() as conn:
            created = self.create_reagent_info_in_transaction(conn, values)
        return created

    def create_reagent_info_in_transaction(
        self,
        conn: sqlite3.Connection,
        values: Dict[str, Any],
    ) -> Dict[str, Any]:
        """在调用方事务内登记一条化学品身份。

        参数：``conn`` 是已经开启的库存写事务；``values`` 是单条身份输入。
        返回：当前事务内已写入的身份投影。异常：字段、CAS、数值或唯一身份
        冲突时抛出 Backend 合同错误，由调用方决定是否回滚整批。
        """

        name = str(values.get("name") or "").strip()
        if not name:
            raise BackendContractError(INVALID_PARAMETER, "name is required")
        cas = _optional_text(values.get("cas"))
        if cas:
            cas = normalize_cas(cas)
        molecular_weight = _positive_number(values.get("molecular_weight"), "molecular_weight")
        density = _positive_number(values.get("density_g_per_ml"), "density_g_per_ml")
        identity = str(uuid4())
        now = _now()
        try:
            conn.execute(
                """INSERT INTO reagent_info(
                uuid,create_time,update_time,description,meta_data,cas,name,name_en,
                aliases,molecular_formula,smiles,inchi_key,molecular_weight,
                density_g_per_ml,physical_state)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    identity, now, now, _optional_text(values.get("description")),
                    _dump(values.get("meta_data") or {}), cas, name,
                    _optional_text(values.get("name_en")),
                    _dump(values.get("aliases") or []),
                    _optional_text(values.get("molecular_formula")),
                    _optional_text(values.get("smiles")),
                    _optional_text(values.get("inchi_key")), molecular_weight,
                    density, _physical_state(values.get("physical_state")),
                ),
            )
        except sqlite3.IntegrityError as error:
            raise BackendContractError(
                RESOURCE_DATA_CONFLICT,
                "CAS number or InChIKey already belongs to another reagent identity",
            ) from error
        row = conn.execute(
            "SELECT * FROM reagent_info WHERE uuid=? AND deleted_at IS NULL",
            (identity,),
        ).fetchone()
        if row is None:  # pragma: no cover - 同一事务内插入后的防御性检查。
            raise BackendContractError(
                RESOURCE_NOT_FOUND, "reagent identity does not exist"
            )
        return _info_row(row)

    def create_reagent_infos(self, values: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """在一个事务内批量登记化学品身份。"""

        if not values:
            raise BackendContractError(INVALID_PARAMETER, "items must not be empty")
        with self.store.transaction() as conn:
            return [
                self.create_reagent_info_in_transaction(conn, item)
                for item in values
            ]

    def list_reagent_infos(
        self, *, page: int = 1, page_size: int = 20, name: str = "",
        cas: str = "", physical_state: str = "",
    ) -> Dict[str, Any]:
        """分页读取活动化学品身份；筛选条件为空时返回全部。"""

        page, page_size = _page(page, page_size)
        where = ["deleted_at IS NULL"]
        params: List[Any] = []
        if name.strip():
            where.append("LOWER(name) LIKE LOWER(?)")
            params.append(f"%{name.strip()}%")
        if cas.strip():
            where.append("LOWER(cas) = LOWER(?)")
            params.append(cas.strip())
        if physical_state.strip():
            where.append("physical_state = ?")
            params.append(_physical_state(physical_state))
        clause = " AND ".join(where)
        total = self.store.query_one(
            f"SELECT COUNT(*) AS count FROM reagent_info WHERE {clause}", tuple(params)
        ) or {"count": 0}
        rows = self.store.query_all(
            f"SELECT * FROM reagent_info WHERE {clause} "
            "ORDER BY LOWER(name),uuid LIMIT ? OFFSET ?",
            (*params, page_size, (page - 1) * page_size),
        )
        return {
            "items": [_info_row(row) for row in rows], "total": int(total["count"]),
            "page": page, "page_size": page_size,
        }

    def get_reagent_info(self, identity: str) -> Dict[str, Any]:
        """按稳定 UUID 读取一个活动化学品身份；不存在时返回资源未找到。"""

        row = self.store.query_one(
            "SELECT * FROM reagent_info WHERE uuid=? AND deleted_at IS NULL", (identity,)
        )
        if row is None:
            raise BackendContractError(RESOURCE_NOT_FOUND, "reagent identity does not exist")
        return _info_row(row)

    def update_reagent_info(self, identity: str, values: Dict[str, Any]) -> Dict[str, Any]:
        """按三态字段纠错化学品身份；未出现的字段保持不变，null 清空可空值。"""

        current = self.get_reagent_info(identity)
        if "physical_state" in values and values["physical_state"] is None:
            raise BackendContractError(
                INVALID_PARAMETER, "physical_state cannot be null"
            )
        merged = dict(current)
        merged.update(values)
        name = str(merged.get("name") or "").strip()
        if not name:
            raise BackendContractError(INVALID_PARAMETER, "name is required")
        cas = _optional_text(merged.get("cas"))
        if cas:
            cas = normalize_cas(cas)
        molecular_weight = _positive_number(merged.get("molecular_weight"), "molecular_weight")
        density = _positive_number(merged.get("density_g_per_ml"), "density_g_per_ml")
        try:
            with self.store.transaction() as conn:
                self._assert_identity_not_handoff_protected(conn, identity)
                cursor = conn.execute(
                    """UPDATE reagent_info SET update_time=?,description=?,meta_data=?,cas=?,
                    name=?,name_en=?,aliases=?,molecular_formula=?,smiles=?,inchi_key=?,
                    molecular_weight=?,density_g_per_ml=?,physical_state=?
                    WHERE uuid=? AND deleted_at IS NULL""",
                    (
                        _now(), _optional_text(merged.get("description")),
                        _dump(merged.get("meta_data") or {}), cas, name,
                        _optional_text(merged.get("name_en")),
                        _dump(merged.get("aliases") or []),
                        _optional_text(merged.get("molecular_formula")),
                        _optional_text(merged.get("smiles")),
                        _optional_text(merged.get("inchi_key")), molecular_weight,
                        density, _physical_state(merged.get("physical_state")), identity,
                    ),
                )
                if cursor.rowcount != 1:
                    raise BackendContractError(
                        RESOURCE_NOT_FOUND, "reagent identity does not exist"
                    )
        except sqlite3.IntegrityError as error:
            raise BackendContractError(
                RESOURCE_DATA_CONFLICT, "CAS number or InChIKey is already used"
            ) from error
        return self.get_reagent_info(identity)

    def delete_reagent_info(self, identity: str) -> None:
        """软删除未被活动试剂实例引用的误建化学品身份。"""

        self.get_reagent_info(identity)
        with self.store.transaction() as conn:
            self._assert_identity_not_handoff_protected(conn, identity)
            referenced = conn.execute(
                "SELECT 1 FROM reagent WHERE reagent_info_uuid=? LIMIT 1",
                (identity,),
            ).fetchone()
            if referenced:
                raise BackendContractError(REAGENT_INFO_IN_USE, "reagent identity is in use")
            conn.execute(
                "UPDATE reagent_info SET deleted_at=?,update_time=? "
                "WHERE uuid=? AND deleted_at IS NULL",
                (_now(), _now(), identity),
            )

    @staticmethod
    def _assert_identity_not_handoff_protected(conn: sqlite3.Connection, identity: str) -> None:
        """目录纠错与交接保护共用库存写锁，禁止重解释受保护源瓶或 incoming 化学身份。"""
        protected_materials = """SELECT material_uuid FROM station_handoff_resource WHERE active=1
            UNION SELECT r.material_uuid FROM station_material_input_resource r
            JOIN station_material_input h USING(invocation_uuid) WHERE h.state='active'"""
        protected = conn.execute(
            f"""SELECT 1 FROM container_content c
            JOIN ({protected_materials}) h ON h.material_uuid=c.material_uuid
            WHERE c.kind='reagent' AND c.reagent_info_uuid=? AND c.deleted_at IS NULL
              LIMIT 1""",
            (identity,),
        ).fetchone()
        if protected is None:
            mixtures = conn.execute(
                f"""SELECT c.composition FROM container_content c
                JOIN ({protected_materials}) h ON h.material_uuid=c.material_uuid
                WHERE c.kind='substance' AND c.deleted_at IS NULL"""
            ).fetchall()
            protected = next((True for row in mixtures for component in _json(row["composition"], [])
                              if component.get("reagent_info_uuid") == identity), None)
        if protected is not None:
            raise BackendContractError(
                RESOURCE_DATA_CONFLICT, "试剂身份被跨站交接保护，暂不能修改或删除目录事实"
            )

    def lookup_compound(self, cas_value: str) -> Dict[str, Any]:
        """按 CAS 查询本地身份，未登记时可选查询 PubChem 候选信息。"""

        cas = normalize_cas(cas_value)
        row = self.store.query_one(
            "SELECT uuid FROM reagent_info WHERE LOWER(cas)=LOWER(?) AND deleted_at IS NULL",
            (cas,),
        )
        if row:
            return {"cas": cas, "status": "registered", "message": "该 CAS 已在本地试剂身份目录登记"}
        if self.compound_source is None:
            return {"cas": cas, "status": "unavailable", "message": "OS 本地模式未配置外部化合物数据源"}
        try:
            compound = self.compound_source.lookup_by_cas(cas)
        except LookupError:
            return {"cas": cas, "status": "not_found", "message": "化合物数据源没有收录该 CAS，请手工填写化学信息"}
        except Exception:  # noqa: BLE001 - 外部数据源故障不得阻断试剂录入
            return {"cas": cas, "status": "unavailable", "message": "化合物数据源暂时不可用，请手工填写化学信息"}
        return {"cas": cas, "status": "ok", "compound": compound}

    def get_reagent_info_structure(self, identity: str) -> Dict[str, Any]:
        """返回本地化学品三维缓存投影；尚未生成时明确返回 pending。"""

        row = self.store.query_one(
            "SELECT * FROM reagent_info WHERE uuid=? AND deleted_at IS NULL", (identity,)
        )
        if row is None:
            raise BackendContractError(RESOURCE_NOT_FOUND, "reagent identity does not exist")
        return {
            "reagent_info_uuid": identity,
            "identity_key": row.get("structure_3d_identity_key") or "",
            "format": row.get("structure_3d_format") or "",
            "source": row.get("structure_3d_source") or "",
            "source_id": row.get("structure_3d_source_id"),
            "content": row.get("structure_3d_content"),
            "checksum": row.get("structure_3d_checksum"),
            "status": row.get("structure_3d_status") or "pending",
            "generated_at": row.get("structure_3d_generated_at"),
            "error_message": row.get("structure_3d_error_message"),
            "update_time": row["update_time"],
        }

    def create_reagent(self, values: Dict[str, Any]) -> Dict[str, Any]:
        """在一个活动容器物料中登记试剂实例并原子追加入库台账。"""

        with self.store.transaction() as conn:
            created = self.create_reagent_in_transaction(conn, values)
        result = self.get_reagent(created["reagent"]["uuid"])
        result["reagent_info"] = created["reagent_info"]
        return result

    def create_reagents(self, values: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """在一个事务内批量登记容器级试剂实例。"""

        if not values:
            raise BackendContractError(INVALID_PARAMETER, "items must not be empty")
        with self.store.transaction() as conn:
            created = [
                self.create_reagent_in_transaction(conn, item)
                for item in values
            ]
        result: List[Dict[str, Any]] = []
        for snapshot in created:
            item = self.get_reagent(snapshot["reagent"]["uuid"])
            item["reagent_info"] = snapshot["reagent_info"]
            result.append(item)
        return result

    def create_reagent_in_transaction(
        self,
        conn: sqlite3.Connection,
        values: Dict[str, Any],
        *,
        record_history: bool = True,
        source_snapshot: Optional[Dict[str, Any]] = None,
        restore_empty: bool = False,
    ) -> Dict[str, Dict[str, Any]]:
        """复用调用方事务创建试剂，并返回试剂与化学身份快照。

        参数：``conn`` 是已经开启的库存写事务；``values`` 与独立创建试剂的
        HTTP 输入一致且必须含 ``material_uuid``。返回：可嵌入物料创建响应的
        ``reagent`` 与 ``reagent_info``。异常：容器、身份、数量或内容互斥校验
        失败时抛出 Backend 合同错误，由调用方统一回滚整个事务。
        """

        material_uuid = str(values.get("material_uuid") or "")
        info_uuid = _optional_text(values.get("reagent_info_uuid"))
        cas = _optional_text(values.get("cas"))
        if bool(info_uuid) == bool(cas):
            raise BackendContractError(
                INVALID_PARAMETER,
                "provide exactly one of reagent_info_uuid or cas",
            )
        quantity = _non_negative_number(values.get("quantity"), "quantity")
        if quantity <= 0 and not (restore_empty and source_snapshot is not None and source_snapshot.get("quantity") == 0):
            raise BackendContractError(INVALID_PARAMETER, "quantity must be positive")
        unit = str(values.get("quantity_unit") or "").strip()
        if not unit:
            raise BackendContractError(INVALID_PARAMETER, "quantity_unit is required")
        concentration, concentration_unit = self._concentration(values)
        identity = str(uuid4())
        now = _now()
        try:
            self._require_container(conn, material_uuid)
            try:
                assert_inventory_mutation_unclaimed(
                    conn, material_uuids=(material_uuid,)
                )
            except InventoryMutationConflict as error:
                raise BackendContractError(
                    RESOURCE_DATA_CONFLICT, str(error)
                ) from error
            # 延迟导入避免共享容器规则在模块初始化时形成循环依赖。
            from unilabos.app.scheduler.inventory.content_rules import (
                ensure_container_empty,
            )

            ensure_container_empty(conn, material_uuid)
            if cas:
                cas = normalize_cas(cas)
                info = conn.execute(
                    "SELECT * FROM reagent_info WHERE LOWER(cas)=LOWER(?) "
                    "AND deleted_at IS NULL",
                    (cas,),
                ).fetchone()
            else:
                info = conn.execute(
                    "SELECT * FROM reagent_info WHERE uuid=? AND deleted_at IS NULL",
                    (info_uuid,),
                ).fetchone()
            if info is None:
                raise BackendContractError(
                    RESOURCE_NOT_FOUND, "reagent identity is not registered"
                )
            # 新瓶使用目录快照；分装保留源瓶物性，不能被之后的目录纠错重解释。
            physical = dict(source_snapshot) if source_snapshot is not None else dict(info)
            context = {**physical, "concentration_value": concentration}
            meta_data = reagent_metadata(values)
            if values.get("container_capacity") is not None:
                self._set_container_capacity(conn, material_uuid, values, now, reagent_context=context)
                meta_data.pop(LOADING_LIMITS_KEY, None)
            projection = material_capacity(conn, material_uuid)
            validate_quantity(quantity, unit, normalize_capacity(meta_data.get(LOADING_LIMITS_KEY)),
                              rated_capacity=projection["rated_capacity"],
                              configured_capacity=projection["configured_capacity"], context=context)
            conn.execute(
                """INSERT INTO reagent(uuid,create_time,update_time,description,meta_data,
                material_uuid,reagent_info_uuid,concentration_value,concentration_unit,
                quantity,quantity_unit,revision,physical_state,density_g_per_ml,density_source)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    identity, now, now, _optional_text(values.get("description")),
                    _dump(meta_data), material_uuid, info["uuid"],
                    concentration, concentration_unit, quantity, unit, 1,
                    physical["physical_state"], physical["density_g_per_ml"],
                    (physical.get("density_source") if source_snapshot is not None else
                     "dictionary" if physical["density_g_per_ml"] is not None else None),
                ),
            )
            # 分装等复合操作自行写带 causation_id 的台账，跳过默认的 add 历史。
            if record_history:
                self._append_history(
                    conn, material_uuid=material_uuid, reagent_uuid=identity,
                    event_type="add", quantity_delta=quantity, quantity_unit=unit,
                    revision=1, values=values, recorded_at=now,
                )
        except sqlite3.IntegrityError as error:
            raise BackendContractError(
                RESOURCE_DATA_CONFLICT,
                "selected container already has reagent content",
            ) from error
        reagent = conn.execute(
            "SELECT * FROM reagent WHERE uuid=? AND deleted_at IS NULL",
            (identity,),
        ).fetchone()
        if reagent is None:  # pragma: no cover - 同一事务内插入后的防御性检查。
            raise BackendContractError(RESOURCE_NOT_FOUND, "reagent does not exist")
        reagent_row = dict(reagent)
        reagent_row["meta_data"] = _json(reagent_row.get("meta_data"), {})
        return {
            "reagent": reagent_row,
            "reagent_info": _info_row(info),
        }

    def dispense_reagent_in_transaction(
        self,
        conn: sqlite3.Connection,
        *,
        source_reagent_uuid: str,
        expected_revision: Optional[int],
        quantity_unit: str,
        targets: List[Dict[str, Any]],
        command_id: str,
        actor: str = "",
        reason: str = "",
    ) -> Dict[str, Any]:
        """在调用方事务内把一瓶源试剂分装到若干空容器。

        参数：``conn`` 是已开启的库存写事务；``source_reagent_uuid`` 是源瓶试剂
        UUID；``expected_revision`` 为源瓶乐观锁；``quantity_unit`` 必须与源瓶一致；
        ``targets`` 每项含 ``material_uuid`` 与 ``quantity``；``command_id`` 作为全部
        台账条目的 ``causation_id``。返回：源瓶新状态与各目标试剂摘要。
        异常：源瓶不存在、修订不符、单位不符、目标重复、目标非空容器、总量超过
        可分量（源瓶余量减去工作流预留）时抛 Backend 合同错误，由调用方回滚。
        状态不变量：源瓶与全部目标的数量总和在事务前后相等；任一步失败不留下
        任何目标试剂或台账。
        """

        unit = str(quantity_unit or "").strip()
        if not unit:
            raise BackendContractError(INVALID_PARAMETER, "quantity_unit is required")
        if not targets:
            raise BackendContractError(INVALID_PARAMETER, "targets must not be empty")
        normalized: List[tuple[str, float, Dict[str, Any]]] = []
        seen: set[str] = set()
        for index, target in enumerate(targets):
            material_uuid = str((target or {}).get("material_uuid") or "").strip()
            if not material_uuid:
                raise BackendContractError(
                    INVALID_PARAMETER, f"targets[{index}].material_uuid is required"
                )
            if material_uuid in seen:
                raise BackendContractError(
                    INVALID_PARAMETER,
                    f"targets[{index}] duplicates container {material_uuid}",
                )
            seen.add(material_uuid)
            quantity = _non_negative_number(
                (target or {}).get("quantity"), f"targets[{index}].quantity"
            )
            if quantity <= 0:
                raise BackendContractError(
                    INVALID_PARAMETER, f"targets[{index}].quantity must be positive"
                )
            normalized.append((material_uuid, quantity, target))
        try:
            total = math.fsum(quantity for _, quantity, _ in normalized)
        except OverflowError as error:
            raise BackendContractError(INVALID_PARAMETER, "dispense total must be finite") from error

        source = conn.execute(
            "SELECT * FROM reagent WHERE uuid=? AND deleted_at IS NULL",
            (source_reagent_uuid,),
        ).fetchone()
        if source is None:
            raise BackendContractError(RESOURCE_NOT_FOUND, "reagent does not exist")
        source = dict(source)
        if expected_revision is not None and int(expected_revision) != int(
            source["revision"]
        ):
            raise BackendContractError(
                RESOURCE_DATA_CONFLICT, "reagent revision has changed"
            )
        if unit.lower() != str(source["quantity_unit"]).lower():
            raise BackendContractError(
                INVALID_PARAMETER, "quantity_unit must match the source reagent"
            )
        available = float(source["quantity"])
        try:
            remaining = subtract_quantity(available, total)
        except WorkflowQuantityReservationError as error:
            raise BackendContractError(
                INVALID_PARAMETER,
                f"dispense total {total} exceeds source quantity {available}",
            ) from error

        try:
            assert_inventory_mutation_unclaimed(
                conn, material_uuids=(source["material_uuid"],)
            )
        except InventoryMutationConflict as error:
            raise BackendContractError(RESOURCE_DATA_CONFLICT, str(error)) from error
        try:
            assert_workflow_quantity_mutation_allowed(
                conn,
                inventory_type="reagent",
                inventory_uuid=source_reagent_uuid,
                quantity=remaining,
                current_unit=str(source["quantity_unit"]),
                next_unit=unit,
            )
        except WorkflowQuantityReservationError as error:
            raise BackendContractError(RESOURCE_DATA_CONFLICT, str(error)) from error

        inherited = {
            "reagent_info_uuid": source["reagent_info_uuid"],
            "quantity_unit": source["quantity_unit"],
            "concentration_value": source["concentration_value"],
            "concentration_unit": source["concentration_unit"],
            "description": source.get("description"),
        }
        created: List[Dict[str, Any]] = []
        for material_uuid, quantity, target in normalized:
            snapshot = self.create_reagent_in_transaction(
                conn,
                {
                    **inherited,
                    "material_uuid": material_uuid,
                    "quantity": quantity,
                    "container_capacity": target.get("container_capacity"),
                    "expected_material_revision": target.get("expected_material_revision"),
                    "meta_data": {
                        LOADING_LIMITS_KEY: normalize_capacity(target.get(LOADING_LIMITS_KEY)),
                        "source_reagent_uuid": source_reagent_uuid,
                        "dispense_command_id": command_id,
                    },
                },
                record_history=False,
                source_snapshot=source,
            )
            created.append(snapshot["reagent"])

        now = _now()
        revision = int(source["revision"]) + 1
        cursor = conn.execute(
            "UPDATE container_content SET update_time=?,quantity=?,revision=? "
            "WHERE uuid=? AND kind='reagent' AND deleted_at IS NULL AND revision=?",
            (now, remaining, revision, source_reagent_uuid, source["revision"]),
        )
        if cursor.rowcount != 1:
            raise BackendContractError(
                RESOURCE_DATA_CONFLICT, "reagent revision has changed"
            )

        occurred_at = _milliseconds(now)
        self._append_dispense_event(
            conn,
            reagent_uuid=source_reagent_uuid,
            material_uuid=str(source["material_uuid"]),
            event_type="dispense_source",
            quantity_delta=remaining - available,
            quantity_unit=str(source["quantity_unit"]),
            revision=revision,
            occurred_at=occurred_at,
            command_id=command_id,
            actor=actor,
            reason=reason,
            extra={"target_reagent_uuids": [row["uuid"] for row in created]},
        )
        for row in created:
            self._append_dispense_event(
                conn,
                reagent_uuid=str(row["uuid"]),
                material_uuid=str(row["material_uuid"]),
                event_type="dispense_target",
                quantity_delta=float(row["quantity"]),
                quantity_unit=str(row["quantity_unit"]),
                revision=int(row["revision"]),
                occurred_at=occurred_at,
                command_id=command_id,
                actor=actor,
                reason=reason,
                extra={"source_reagent_uuid": source_reagent_uuid},
            )

        return {
            "source": {
                "reagent_uuid": source_reagent_uuid,
                "material_uuid": source["material_uuid"],
                "quantity": remaining,
                "quantity_unit": source["quantity_unit"],
                "revision": revision,
            },
            "targets": [
                {
                    "material_uuid": row["material_uuid"],
                    "reagent_uuid": row["uuid"],
                    "quantity": row["quantity"],
                    "quantity_unit": row["quantity_unit"],
                    "revision": row["revision"],
                }
                for row in created
            ],
        }

    def _append_dispense_event(
        self,
        conn: sqlite3.Connection,
        *,
        reagent_uuid: str,
        material_uuid: str,
        event_type: str,
        quantity_delta: float,
        quantity_unit: str,
        revision: int,
        occurred_at: int,
        command_id: str,
        actor: str,
        reason: str,
        extra: Dict[str, Any],
    ) -> None:
        """为分装写一条试剂台账，``causation_id`` 固定为分装命令 ID。"""

        reagent = conn.execute(
            """SELECT quantity,quantity_unit,concentration_value,concentration_unit,
            physical_state,density_g_per_ml,revision,meta_data FROM reagent WHERE uuid=?""",
            (reagent_uuid,),
        ).fetchone()
        if reagent is None:
            raise BackendContractError(RESOURCE_NOT_FOUND, "reagent does not exist")
        capacity = material_capacity(conn, material_uuid)["capacity"]
        payload = {
            "changes": {
                "result": {
                    "quantity": reagent["quantity"],
                    "quantity_unit": reagent["quantity_unit"],
                    "concentration_value": reagent["concentration_value"],
                    "concentration_unit": reagent["concentration_unit"],
                    "physical_state": reagent["physical_state"],
                    "density_g_per_ml": reagent["density_g_per_ml"],
                    "revision": reagent["revision"],
                    LOADING_LIMITS_KEY: normalize_capacity(_json(reagent["meta_data"], {}).get(LOADING_LIMITS_KEY)),
                    "container_capacity": capacity,
                    "maximum_capacity": maximum_capacity(capacity, reagent["meta_data"]),
                }
            },
            "extension": {**_json(reagent["meta_data"], {}), **extra},
            "workflow_task_uuid": None,
            "workflow_node_job_uuid": None,
            "quantity_delta": quantity_delta,
            "quantity_unit": quantity_unit,
            "revision": revision,
        }
        InventoryStore.tx_append_inventory_event(
            conn,
            entry_uuid=new_event_id(occurred_at),
            edge_id=self.edge_id,
            lab_id=self.lab_id,
            occurred_at=occurred_at,
            aggregate_type="reagent",
            aggregate_id=reagent_uuid,
            aggregate_version=revision,
            event_type=f"reagent.{event_type}",
            payload=payload,
            actor=actor or "frontend",
            reason=reason,
            causation_id=command_id,
            material_uuid=material_uuid,
            subject_type="reagent",
            quantity_delta=quantity_delta,
            quantity_unit=quantity_unit,
            revision=revision,
        )

    def list_reagents(
        self, *, page: int = 1, page_size: int = 20, material_uuid: str = "",
        reagent_info_uuid: str = "", keyword: str = "", cas: str = "", barcode: str = "",
    ) -> Dict[str, Any]:
        """分页读取容器级活动试剂，并联接化学身份和容器展示字段。"""

        page, page_size = _page(page, page_size)
        where = [
            "reagent.deleted_at IS NULL",
            "material.deleted_at IS NULL",
            "reagent_info.deleted_at IS NULL",
        ]
        params: List[Any] = []
        filters = {
            "reagent.material_uuid": material_uuid,
            "reagent.reagent_info_uuid": reagent_info_uuid,
            "reagent_info.cas": cas,
            "material.barcode": barcode,
        }
        for column, value in filters.items():
            if value.strip():
                where.append(f"LOWER({column})=LOWER(?)")
                params.append(value.strip())
        if keyword.strip():
            where.append(
                "(LOWER(reagent_info.name) LIKE LOWER(?) "
                "OR LOWER(material.name) LIKE LOWER(?))"
            )
            params.extend([f"%{keyword.strip()}%", f"%{keyword.strip()}%"])
        clause = " AND ".join(where)
        base = self._reagent_select()
        count = self.store.query_one(
            "SELECT COUNT(*) AS count FROM reagent JOIN reagent_info "
            "ON reagent_info.uuid=reagent.reagent_info_uuid "
            "JOIN material ON material.uuid=reagent.material_uuid WHERE " + clause,
            tuple(params),
        ) or {"count": 0}
        rows = self.store.query_all(
            base
            + " WHERE "
            + clause
            + " ORDER BY LOWER(reagent_info.name),reagent.uuid LIMIT ? OFFSET ?",
            (*params, page_size, (page - 1) * page_size),
        )
        items = [_reagent_row(row) for row in rows]
        self._annotate_active_reservations(items)
        return {
            "items": items,
            "total": int(count["count"]),
            "page": page,
            "page_size": page_size,
        }

    def _annotate_active_reservations(self, items: List[Dict[str, Any]]) -> None:
        """为试剂投影补上工作流活动预留量（与数量同单位）。

        参数：``items`` 是已投影的试剂行，原地写入
        ``active_workflow_reserved_quantity``。预留在建任务事务内生效、任务结算或
        取消后释放；每页只扫描一次预留，不开事务、不改动库存。异常：预留载荷损坏时由
        ``active_workflow_reserved_quantities`` 关闭式失败，原样上抛。
        """
        if not items:
            return
        with self.store.read_connection() as conn:
            reserved = active_workflow_reserved_quantities(conn)
            for item in items:
                item["active_workflow_reserved_quantity"] = reserved.get(("reagent", str(item["uuid"])), 0.0)
                projection = material_capacity(conn, str(item["material_uuid"]))
                item["container_capacity"] = projection["capacity"]
                item["configured_capacity"] = projection["configured_capacity"]
                item["maximum_capacity"] = maximum_capacity(projection["capacity"], item["meta_data"])
                item["rated_capacity"] = projection["rated_capacity"]
                item["material_revision"] = projection["material_revision"]

    def get_reagent(self, identity: str) -> Dict[str, Any]:
        """读取一个活动试剂详情；不存在时返回资源未找到。"""

        row = self.store.query_one(
            self._reagent_select()
            + " WHERE reagent.uuid=? AND reagent.deleted_at IS NULL "
            "AND material.deleted_at IS NULL AND reagent_info.deleted_at IS NULL",
            (identity,),
        )
        if row is None:
            raise BackendContractError(RESOURCE_NOT_FOUND, "reagent does not exist")
        item = _reagent_row(row)
        self._annotate_active_reservations([item])
        return item

    def _set_container_capacity(
        self, conn: sqlite3.Connection, material_uuid: str, values: Dict[str, Any],
        now: str, *, updating_reagent_uuid: Optional[str] = None,
        reagent_context: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """在数量写事务中保存唯一最大装料量，保留容器的其他配置。"""
        material = conn.execute(
            "SELECT m.config,m.aggregate_version FROM material m WHERE m.uuid=?",
            (material_uuid,),
        ).fetchone()
        expected = values.get("expected_material_revision")
        if expected is not None and int(expected) != int(material["aggregate_version"]):
            raise BackendContractError(RESOURCE_DATA_CONFLICT, "Material revision has changed")
        previous_config = _json(material["config"], {})
        config = {**previous_config, CAPACITY_KEY: normalize_capacity(values["container_capacity"])}
        validate_material_config(conn, material_uuid, config, replace_loading_limits=True,
                                 updating_reagent_uuid=updating_reagent_uuid, reagent_context=reagent_context)
        if normalize_capacity(previous_config.get(CAPACITY_KEY)) == config[CAPACITY_KEY]:
            return False
        conn.execute(
            "UPDATE material SET config=?,update_time=?,aggregate_version=aggregate_version+1 "
            "WHERE uuid=?",
            (_dump(config), now, material_uuid),
        )
        return True

    def record_material_capacity_change(
        self, conn: sqlite3.Connection, material_uuid: str, previous_capacity: Dict[str, float],
        *, replace_loading_limits: bool, configuration_changed: bool,
    ) -> None:
        """物料详情保存上限时，同事务折叠旧限制并记录试剂的前后有效值。"""
        capacity = material_capacity(conn, material_uuid)["capacity"]
        for reagent in conn.execute(
            "SELECT * FROM reagent WHERE material_uuid=? AND deleted_at IS NULL", (material_uuid,),
        ).fetchall():
            previous = _reagent_history_state(reagent, previous_capacity)
            metadata = _json(reagent["meta_data"], {})
            previous_limits = normalize_capacity(metadata.get(LOADING_LIMITS_KEY))
            previous_maximum = maximum_capacity(previous_capacity, metadata)
            if replace_loading_limits:
                metadata.pop(LOADING_LIMITS_KEY, None)
            current_maximum = maximum_capacity(capacity, metadata)
            if (not configuration_changed and previous_maximum == current_maximum
                    and metadata == _json(reagent["meta_data"], {})):
                continue
            now, revision = _now(), int(reagent["revision"]) + 1
            conn.execute("UPDATE container_content SET meta_data=?,revision=?,update_time=? WHERE uuid=? AND kind='reagent'",
                         (_dump(metadata), revision, now, reagent["uuid"]))
            self._append_history(
                conn, material_uuid=material_uuid, reagent_uuid=reagent["uuid"],
                event_type="adjust", quantity_delta=0, quantity_unit=reagent["quantity_unit"],
                revision=revision, values={"_previous_loading_limits": previous_limits,
                                          "_previous_maximum_capacity": previous_maximum},
                recorded_at=now, previous=previous,
            )

    def update_reagent(self, identity: str, values: Dict[str, Any]) -> Dict[str, Any]:
        """以期望修订更新余量；单位与化学身份保持不可变，并原子追加台账。"""

        current = self.get_reagent(identity)
        expected = values.get("expected_revision")
        if expected is not None and int(expected) != int(current["revision"]):
            raise BackendContractError(RESOURCE_DATA_CONFLICT, "reagent revision has changed")
        quantity = _non_negative_number(values.get("quantity"), "quantity")
        unit = str(values.get("quantity_unit") or "").strip()
        if unit.lower() != str(current["quantity_unit"]).lower():
            raise BackendContractError(INVALID_PARAMETER, "quantity_unit is immutable")
        concentration, concentration_unit = self._concentration({
            "concentration_value": current["concentration_value"],
            "concentration_unit": current["concentration_unit"], **values,
        })
        revision = int(current["revision"]) + 1
        now = _now()
        # 公共人工更新增加余量记 add，其余变化记 adjust；consume 只留给可信
        # 工作流结算入口，不能由普通编辑请求伪造。
        event = "add" if quantity > float(current["quantity"]) else "adjust"
        delta = quantity - float(current["quantity"])
        meta_data = reagent_metadata(values, current["meta_data"])
        description = _optional_text(values.get("description", current["description"]))
        previous_limits = normalize_capacity(current["meta_data"].get(LOADING_LIMITS_KEY))
        # 旧瓶可原单位减量或编辑非容量信息；加量、上限和浓度变更须满足新规则。
        context_changed = (concentration != current["concentration_value"]
                           or concentration_unit != current["concentration_unit"])
        strict = (quantity > float(current["quantity"]) or values.get("container_capacity") is not None
                  or normalize_capacity(meta_data.get(LOADING_LIMITS_KEY)) != previous_limits or context_changed)
        context = {**current, "concentration_value": concentration}
        with self.store.transaction() as conn:
            latest = conn.execute(
                "SELECT revision FROM reagent WHERE uuid=? AND deleted_at IS NULL", (identity,),
            ).fetchone()
            if latest is None or int(latest["revision"]) != int(current["revision"]):
                raise BackendContractError(RESOURCE_DATA_CONFLICT, "reagent revision has changed")
            try:
                assert_inventory_mutation_unclaimed(
                    conn, material_uuids=(current["material_uuid"],)
                )
            except InventoryMutationConflict as error:
                raise BackendContractError(
                    RESOURCE_DATA_CONFLICT, str(error)
                ) from error
            previous_capacity = material_capacity(conn, current["material_uuid"])["capacity"]
            previous = _reagent_history_state(current, previous_capacity)
            previous_maximum = maximum_capacity(previous_capacity, current["meta_data"])
            capacity_changed = False
            if values.get("container_capacity") is not None:
                capacity_changed = self._set_container_capacity(
                    conn, current["material_uuid"], values, now, updating_reagent_uuid=identity,
                    reagent_context=context)
                meta_data.pop(LOADING_LIMITS_KEY, None)
            projection = material_capacity(conn, current["material_uuid"])
            capacity = projection["capacity"]
            limits = normalize_capacity(meta_data.get(LOADING_LIMITS_KEY))
            if strict:
                validate_quantity(quantity, unit, limits, rated_capacity=projection["rated_capacity"],
                                  configured_capacity=projection["configured_capacity"], context=context)
            current_maximum = maximum_capacity(capacity, meta_data)
            try:
                assert_workflow_quantity_mutation_allowed(
                    conn,
                    inventory_type="reagent",
                    inventory_uuid=identity,
                    quantity=quantity,
                    current_unit=str(current["quantity_unit"]),
                    next_unit=unit,
                )
            except WorkflowQuantityReservationError as error:
                raise BackendContractError(
                    RESOURCE_DATA_CONFLICT, str(error)
                ) from error
            changed = (delta != 0 or context_changed or description != current["description"]
                       or meta_data != current["meta_data"]
                       or current_maximum != previous_maximum or capacity_changed)
            if changed:
                cursor = conn.execute(
                    """UPDATE container_content SET update_time=?,description=?,meta_data=?,
                    concentration_value=?,concentration_unit=?,quantity=?,revision=?
                    WHERE uuid=? AND kind='reagent' AND deleted_at IS NULL AND revision=?""",
                    (
                        now, description, _dump(meta_data), concentration,
                        concentration_unit, quantity, revision, identity, current["revision"],
                    ),
                )
                if cursor.rowcount != 1:
                    raise BackendContractError(RESOURCE_DATA_CONFLICT, "reagent revision has changed")
                self._append_history(
                    conn,
                    material_uuid=current["material_uuid"],
                    reagent_uuid=identity,
                    event_type=event,
                    quantity_delta=delta,
                    quantity_unit=current["quantity_unit"],
                    revision=revision,
                    values={**values, "_previous_loading_limits": previous_limits,
                            "_previous_maximum_capacity": previous_maximum},
                    recorded_at=now, previous=previous,
                )
        return self.get_reagent(identity)

    def delete_reagent(self, identity: str) -> None:
        """软删除试剂，余量闭合为零并追加 remove 台账。"""

        current = self.get_reagent(identity)
        revision = int(current["revision"]) + 1
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
                    inventory_type="reagent",
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
                "UPDATE container_content SET deleted_at=?,update_time=?,quantity=0,revision=? "
                "WHERE uuid=? AND kind='reagent' AND deleted_at IS NULL AND revision=?",
                (now, now, revision, identity, current["revision"]),
            )
            if cursor.rowcount != 1:
                raise BackendContractError(RESOURCE_DATA_CONFLICT, "reagent revision has changed")
            self._append_history(
                conn, material_uuid=current["material_uuid"], reagent_uuid=identity,
                event_type="remove", quantity_delta=-float(current["quantity"]),
                quantity_unit=current["quantity_unit"], revision=revision,
                values={}, recorded_at=now,
            )

    def get_reagent_history(self, identity: str) -> Dict[str, Any]:
        """按稳定 UUID 读取一条试剂变更历史。"""

        row = self.store.query_one(
            "SELECT * FROM inventory_ledger "
            "WHERE entry_uuid=? AND subject_type='reagent'",
            (identity,),
        )
        if row is None:
            raise BackendContractError(RESOURCE_NOT_FOUND, "reagent history does not exist")
        return _ledger_row(row)

    def list_reagent_history(
        self, material_uuid: str, *, page: int = 1, page_size: int = 20
    ) -> Dict[str, Any]:
        """按时间倒序分页读取一个容器上的不可变试剂台账。"""

        if self.store.query_one(
            "SELECT uuid FROM material WHERE uuid=? AND deleted_at IS NULL", (material_uuid,)
        ) is None:
            raise BackendContractError(MATERIAL_NOT_FOUND, "material does not exist")
        page, page_size = _page(page, page_size)
        count = self.store.query_one(
            "SELECT COUNT(*) AS count FROM inventory_ledger "
            "WHERE material_uuid=? AND subject_type='reagent'",
            (material_uuid,),
        ) or {"count": 0}
        rows = self.store.query_all(
            "SELECT * FROM inventory_ledger WHERE material_uuid=? AND subject_type='reagent' "
            "ORDER BY occurred_at DESC,ledger_id DESC LIMIT ? OFFSET ?",
            (material_uuid, page_size, (page - 1) * page_size),
        )
        return {
            "items": [_ledger_row(row) for row in rows], "page": page,
            "page_size": page_size, "has_more": page * page_size < int(count["count"]),
        }

    @staticmethod
    def _concentration(values: Dict[str, Any]) -> Tuple[Optional[float], Optional[str]]:
        concentration = values.get("concentration_value")
        unit = _optional_text(values.get("concentration_unit"))
        if (concentration is None) != (unit is None):
            raise BackendContractError(
                INVALID_PARAMETER,
                "concentration value and unit must be provided together",
            )
        if concentration is None:
            return None, None
        number = _non_negative_number(concentration, "concentration_value")
        return number, unit

    @staticmethod
    def _require_container(conn: sqlite3.Connection, material_uuid: str) -> None:
        from unilabos.app.scheduler.inventory.content_rules import require_container

        require_container(conn, material_uuid)

    def _append_history(
        self,
        conn: sqlite3.Connection, *, material_uuid: str, reagent_uuid: str,
        event_type: str, quantity_delta: float, quantity_unit: str, revision: int,
        values: Dict[str, Any], recorded_at: str,
        previous: Optional[Dict[str, Any]] = None,
    ) -> None:
        # 公共 HTTP 写入口的操作方恒为 frontend；source 只是审计扩展，不能由
        # 未鉴权请求伪造为 Edge 或系统权威。Edge 内部写入需另走可信服务入口。
        operator = "frontend"
        observed = _optional_text(values.get("observed_at")) or recorded_at
        reagent = conn.execute(
            """SELECT quantity,quantity_unit,concentration_value,
            concentration_unit,physical_state,density_g_per_ml,revision,meta_data,description
            FROM reagent WHERE uuid=?""",
            (reagent_uuid,),
        ).fetchone()
        if reagent is None:
            raise BackendContractError(
                RESOURCE_NOT_FOUND, "reagent does not exist"
            )
        extension = _json(reagent["meta_data"], {})
        if values.get("source"):
            extension = {**extension, "source": values["source"]}
        capacity = material_capacity(conn, material_uuid)["capacity"]
        result = _reagent_history_state(reagent, capacity)
        occurred_at = _milliseconds(observed)
        entry_uuid = new_event_id(occurred_at)
        trace_id = _optional_text(values.get("trace_id")) or ""
        payload = {
            "changes": {"result": result},
            "extension": extension,
            "workflow_task_uuid": _optional_text(
                values.get("workflow_task_uuid")
            ),
            "workflow_node_job_uuid": _optional_text(
                values.get("workflow_node_job_uuid")
            ),
            "quantity_delta": quantity_delta,
            "quantity_unit": quantity_unit,
            "revision": revision,
        }
        if previous is not None:
            payload["changes"]["previous"] = previous
        if "_previous_loading_limits" in values:
            payload["changes"].setdefault("previous", {})[LOADING_LIMITS_KEY] = values["_previous_loading_limits"]
        if "_previous_maximum_capacity" in values:
            payload["changes"].setdefault("previous", {})["maximum_capacity"] = values["_previous_maximum_capacity"]
        InventoryStore.tx_append_inventory_event(
            conn,
            entry_uuid=entry_uuid,
            edge_id=self.edge_id,
            lab_id=self.lab_id,
            occurred_at=occurred_at,
            aggregate_type="reagent",
            aggregate_id=reagent_uuid,
            aggregate_version=revision,
            event_type=f"reagent.{event_type}",
            payload=payload,
            actor=operator,
            trace_id=trace_id,
            material_uuid=material_uuid,
            subject_type="reagent",
            quantity_delta=quantity_delta,
            quantity_unit=quantity_unit,
            revision=revision,
            workflow_task_uuid=payload["workflow_task_uuid"],
            workflow_node_job_uuid=payload["workflow_node_job_uuid"],
        )

    @staticmethod
    def _reagent_select() -> str:
        return """SELECT reagent.*,reagent_info.name,reagent_info.name_en,
        reagent_info.aliases,reagent_info.cas,reagent_info.molecular_formula,
        reagent_info.smiles,reagent_info.inchi_key,reagent_info.molecular_weight,
        material.barcode AS container_barcode,material.name AS container_name
        FROM reagent JOIN reagent_info ON reagent_info.uuid=reagent.reagent_info_uuid
        JOIN material ON material.uuid=reagent.material_uuid"""


__all__ = ["BackendReagentService", "normalize_cas"]
