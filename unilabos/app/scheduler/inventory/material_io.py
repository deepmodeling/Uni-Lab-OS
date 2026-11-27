"""容器物料的原子装载与卸载操作。

本模块刻意停留在物料图边界：Material、Site、RelativePosition、
MaterialInventory 及容器内容。工作流只提供幂等效果身份；调度、数量库存、
批次、预留和仓库账本不属于这里。可选的上料试剂复用既有试剂合同，在同一
事务内创建身份和/或试剂实例。
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from collections.abc import Mapping
from copy import deepcopy
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from unilabos.app.scheduler.inventory.dispatch_admission import (
    DispatchAdmissionConflict,
    InventoryMutationConflict,
    assert_inventory_mutation_unclaimed,
    validate_physical_settlement_credentials,
)
from unilabos.app.scheduler.inventory.store import (
    InventoryStore,
    SiteOccupancyConflict,
    clear_site_occupancy,
    set_site_occupancy,
)
from unilabos.app.scheduler.resource_lock import (
    conflicting_resource_lock_keys,
    material_lock_key,
    site_lock_key,
)
from unilabos.persistence.database import Database
from unilabos.persistence.inspection import table_exists
from unilabos.resources.site_definition import INITIAL_MATERIAL_TEMPLATE_HINT

if TYPE_CHECKING:
    from unilabos.app.scheduler.inventory.store import InventoryStore

MATERIAL_IO_COMMAND_KIND = "material_io"
_REAGENT_IDENTITY_FIELDS = (
    "cas",
    "name",
    "name_en",
    "aliases",
    "molecular_formula",
    "smiles",
    "inchi_key",
    "molecular_weight",
    "density_g_per_ml",
    "physical_state",
)
_REAGENT_INSTANCE_FIELDS = (
    "quantity",
    "quantity_unit",
    "concentration_value",
    "concentration_unit",
    "description",
    "source",
    "observed_at",
    "container_capacity",
    "meta_data",
)
_REAGENT_ALLOWED_FIELDS = frozenset(
    ("reagent_info_uuid", *_REAGENT_IDENTITY_FIELDS, *_REAGENT_INSTANCE_FIELDS)
)
_QUANTITY_CAPACITY = {
    "ul": ("max_volume_ul", 1.0),
    "µl": ("max_volume_ul", 1.0),
    "μl": ("max_volume_ul", 1.0),
    "ml": ("max_volume_ul", 1000.0),
    "l": ("max_volume_ul", 1_000_000.0),
    "mg": ("max_mass_g", 0.001),
    "g": ("max_mass_g", 1.0),
    "kg": ("max_mass_g", 1000.0),
    "粒": ("max_count", 1.0),
}


#: ``resolve_resources`` 的可选参数未由调用方提供时的占位值：未提供即跳过
#: 对应的准备期预检，与未传入该参数的旧调用保持一致
_NOT_PROVIDED: Any = object()


class MaterialIOError(RuntimeError):
    """物料 I/O 合同错误。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def material_io_command_id(workflow_id: str, node_id: str, attempt: int) -> str:
    """按任务、节点和尝试次构造库存对账表中的确定性命令身份。"""

    return f"{MATERIAL_IO_COMMAND_KIND}:{workflow_id}:{node_id}:{attempt}"


def migrate_material_io_schema(connection: Database | sqlite3.Connection) -> None:
    """把旧效果表折进 processed_command，不再单独建 Material I/O 表。"""

    if not table_exists(connection, "material_io_effect"):
        return
    rows = connection.execute(
        "SELECT effect_uuid,workflow_id,node_id,attempt,operation,"
        "request_hash,request_json,result_json FROM material_io_effect"
    ).fetchall()
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    for row in rows:
        result = _json(row["result_json"], None)
        request = _json(row["request_json"], None)
        if not isinstance(result, dict) or not isinstance(request, dict):
            raise MaterialIOError(
                "effect_corrupt", "Material I/O effect result is invalid"
            )
        envelope = _effect_envelope(
            operation=str(row["operation"]),
            request_hash=str(row["request_hash"]),
            effect_uuid=str(row["effect_uuid"]),
            request=request,
            result=result,
        )
        connection.execute(
            """
            INSERT OR IGNORE INTO processed_command(
                command_id,result_json,status,processed_at
            ) VALUES (?,?, 'completed', ?)
            """,
            (
                material_io_command_id(
                    str(row["workflow_id"]),
                    str(row["node_id"]),
                    int(row["attempt"]),
                ),
                _dump(envelope),
                now_ms,
            ),
        )
    connection.execute("DROP TABLE material_io_effect")


def _now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _json(value: Any, fallback: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(str(value))
    except (TypeError, ValueError):
        return fallback


def _effect_envelope(
    *,
    operation: str,
    request_hash: str,
    effect_uuid: str,
    request: Mapping[str, Any],
    result: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "kind": MATERIAL_IO_COMMAND_KIND,
        "operation": operation,
        "request_hash": request_hash,
        "effect_uuid": effect_uuid,
        "request": dict(request),
        "result": dict(result),
    }


def _required_text(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise MaterialIOError("invalid_parameter", f"{field} is required")
    return text


def _resolve_mount_site(
    connection: sqlite3.Connection,
    *,
    mount_material_uuid: str,
    site_uuid: str,
    site_name: str,
) -> sqlite3.Row:
    """按改挂同一协议，在指定挂载物料上解析目标库位。

    参数：``mount_material_uuid`` 是上料已经给出的父物料；``site_uuid`` 只对
    ``site.uuid``；``site_name`` 只对 ``site.name``。返回：该挂载物料上未删除的
    库位行。传了 ``site_uuid`` 就只按 UUID 查，查不到不再用名字兜底；UUID 为空
    时才按名字查。两个都传时，先按 UUID 定位，名字必须与该行一致。名字只在这只
    父物料内按大小写不敏感匹配，不跨物料搜索。异常：都没传、找不到、名字不一致
    或同名超过一行时抛 ``MaterialIOError``。
    """

    if site_uuid:
        try:
            canonical_uuid = str(UUID(site_uuid))
        except (AttributeError, TypeError, ValueError) as error:
            raise MaterialIOError("invalid_parameter", "site_uuid 不是合法 UUID") from error
        by_uuid = connection.execute(
            "SELECT uuid,material_uuid,name,occupied_material_uuid,"
            "allowed_resource_template_uuids FROM site "
            "WHERE uuid=? AND material_uuid=? AND deleted_at IS NULL",
            (canonical_uuid, mount_material_uuid),
        ).fetchone()
        if by_uuid is None:
            raise MaterialIOError("site_not_found", "Mount Site not found")
        if site_name and str(by_uuid["name"]).casefold() != site_name.casefold():
            raise MaterialIOError(
                "invalid_parameter",
                "site_uuid 与 site 名称不一致",
            )
        return by_uuid
    if not site_name:
        raise MaterialIOError("invalid_parameter", "site_uuid 或 site 名称必须提供一个")
    matches = connection.execute(
        "SELECT uuid,material_uuid,name,occupied_material_uuid,"
        "allowed_resource_template_uuids FROM site "
        "WHERE material_uuid=? AND deleted_at IS NULL AND LOWER(name)=LOWER(?) "
        "ORDER BY uuid",
        (mount_material_uuid, site_name),
    ).fetchall()
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise MaterialIOError("site_not_found", "Mount Site name is ambiguous")
    raise MaterialIOError("site_not_found", "Mount Site not found")


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _load_name_prefix(template_name: str) -> str:
    """取模板名最后一段，作为自动上料名字的前缀。"""

    short = template_name.strip().rsplit(".", 1)[-1].rsplit("/", 1)[-1]
    cleaned = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in short).strip("-")
    return cleaned or "material"


def _material_registry_class(template: Mapping[str, Any] | sqlite3.Row) -> str:
    """用模板注册表业务 ID 作为物料 class，type 仍保留 resource/device。

    设备图导入的物料 class 是 ``community...szlab_tip_box`` 这类身份。上料若把
    class 写成泛化的 ``resource``，机械臂无法从 ResourceSlot 认出枪头盒负载。
    """

    name = str(template["name"] or "").strip()
    resource_type = str(template["resource_type"] or "").strip()
    return name or resource_type or "resource"


def allocate_load_material_name(
    *,
    prefix: str,
    now: datetime,
    taken,
) -> str:
    """用本地时间到秒生成上料名字；同一秒已占用时再补毫秒和序号。

    参数：``prefix`` 是模板短名；``now`` 是本地时间；``taken`` 判断该名字或
    「名字 / 孔名」是否已被未删除物料占用。返回：可用名字。异常：一百个候选
    都冲突时抛 ``MaterialIOError``。
    """

    base = f"{prefix}-{now.strftime('%Y%m%d-%H%M%S')}"
    if not taken(base):
        return base
    stamped = f"{base}-{now.microsecond // 1000:03d}"
    if not taken(stamped):
        return stamped
    for index in range(2, 100):
        candidate = f"{stamped}-{index}"
        if not taken(candidate):
            return candidate
    raise MaterialIOError("identity_conflict", "无法分配不重复的上料名称")


def _load_name_taken(connection: sqlite3.Connection, name: str) -> bool:
    """未删除物料已使用该名字，或已有「该名字 / 孔名」时视为占用。"""

    prefix = f"{name} / "
    row = connection.execute(
        "SELECT 1 FROM material WHERE deleted_at IS NULL "
        "AND (name=? OR substr(name, 1, ?)=?) LIMIT 1",
        (name, len(prefix), prefix),
    ).fetchone()
    return row is not None


def _canonical_positive_number(value: Any, field: str) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise MaterialIOError("invalid_parameter", f"{field} must be positive")
    try:
        number = float(value)
    except (TypeError, ValueError) as error:
        raise MaterialIOError("invalid_parameter", f"{field} must be positive") from error
    if not math.isfinite(number) or number <= 0:
        raise MaterialIOError("invalid_parameter", f"{field} must be positive")
    if number == int(number) and abs(number) < 2**53:
        return int(number)
    return number


def _canonical_uuid_text(value: Any, field: str) -> str:
    text = str(value or "").strip()
    try:
        return str(UUID(text))
    except (TypeError, ValueError) as error:
        raise MaterialIOError("invalid_parameter", f"{field} must be a UUID") from error


def canonicalize_load_reagent(value: Any) -> dict[str, Any] | None:
    """把上料可选试剂规范成三种互斥模式之一，供哈希与结算共用。

    省略或 ``None``：不建身份也不建试剂。``reagent_info_uuid``：复用已有身份
    只建试剂。``name``：新建身份再挂试剂；若同时给出已登记 CAS 则复用该身份。
    仅 ``cas``：按已有身份查找，未登记则失败。
    """

    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise MaterialIOError("invalid_parameter", "reagent must be an object")
    unknown = set(value) - _REAGENT_ALLOWED_FIELDS
    if unknown:
        raise MaterialIOError(
            "invalid_parameter",
            "reagent has unknown fields: " + ", ".join(sorted(unknown)),
        )
    quantity = _canonical_positive_number(value.get("quantity"), "reagent.quantity")
    quantity_unit = _required_text(value.get("quantity_unit"), "reagent.quantity_unit")
    info_uuid = value.get("reagent_info_uuid")
    if info_uuid not in (None, ""):
        info_uuid = _canonical_uuid_text(info_uuid, "reagent.reagent_info_uuid")
    else:
        info_uuid = None
    name = _optional_text(value.get("name"))
    cas = _optional_text(value.get("cas"))
    if info_uuid and (name or cas):
        raise MaterialIOError(
            "invalid_parameter",
            "reagent_info_uuid cannot be combined with name or cas",
        )
    if info_uuid is None and name is None and cas is None:
        raise MaterialIOError(
            "invalid_parameter",
            "reagent requires reagent_info_uuid, name, or cas",
        )
    if cas is not None:
        from unilabos.app.scheduler.inventory.backend_contract import (
            BackendContractError,
        )
        from unilabos.app.scheduler.inventory.reagent_contract import normalize_cas

        try:
            cas = normalize_cas(cas)
        except BackendContractError as error:
            raise MaterialIOError("invalid_parameter", error.message) from error
    canonical: dict[str, Any] = {}
    if info_uuid is not None:
        canonical["reagent_info_uuid"] = info_uuid
    if cas is not None:
        canonical["cas"] = cas
    if name is not None:
        canonical["name"] = name
        raw_state = (_optional_text(value.get("physical_state")) or "").lower()
        if raw_state in {"", "unknown"}:
            canonical["physical_state"] = "liquid"
    for field in _REAGENT_IDENTITY_FIELDS:
        if field in {"cas", "name"} or field not in value or info_uuid is not None:
            continue
        item = value.get(field)
        if field == "aliases":
            if item in (None, []):
                continue
            if not isinstance(item, list) or any(
                not isinstance(alias, str) or not alias.strip() for alias in item
            ):
                raise MaterialIOError("invalid_parameter", "reagent.aliases is invalid")
            canonical[field] = [alias.strip() for alias in item]
            continue
        if field in {"molecular_weight", "density_g_per_ml"}:
            if item is None:
                continue
            canonical[field] = _canonical_positive_number(item, f"reagent.{field}")
            continue
        text = _optional_text(item)
        if text is None:
            continue
        if field == "physical_state":
            if name is not None and text.lower() == "unknown":
                continue
            canonical[field] = text.lower()
            continue
        canonical[field] = text
    canonical["quantity"] = quantity
    canonical["quantity_unit"] = quantity_unit
    for field in _REAGENT_INSTANCE_FIELDS:
        if field in {"quantity", "quantity_unit"} or field not in value:
            continue
        item = value.get(field)
        if item in (None, "", {}, []):
            continue
        if field in {"concentration_value"}:
            canonical[field] = _canonical_positive_number(item, f"reagent.{field}")
            continue
        if field in {"description", "source", "observed_at", "concentration_unit"}:
            text = _optional_text(item)
            if text is not None:
                canonical[field] = text
            continue
        canonical[field] = item
    return canonical


def canonicalize_refill_reagent(value: Any) -> dict[str, Any]:
    """把补液试剂规范成只含加量与单位的对象；禁止再建身份。"""

    if not isinstance(value, Mapping):
        raise MaterialIOError("invalid_parameter", "reagent must be an object")
    unknown = set(value) - {"quantity", "quantity_unit"}
    if unknown:
        raise MaterialIOError(
            "invalid_parameter",
            "refill reagent only accepts quantity and quantity_unit",
        )
    return {
        "quantity": _canonical_positive_number(value.get("quantity"), "reagent.quantity"),
        "quantity_unit": _required_text(value.get("quantity_unit"), "reagent.quantity_unit"),
    }


def _default_container_capacity(quantity: int | float, unit: str) -> dict[str, float]:
    spec = _QUANTITY_CAPACITY.get(str(unit).strip().lower())
    if spec is None:
        raise MaterialIOError(
            "invalid_parameter",
            "reagent.quantity_unit must be µL, mL, L, mg, g, kg or 粒",
        )
    field, factor = spec
    return {field: float(quantity) * factor}


def _material_io_error_from_reagent(error: Exception) -> MaterialIOError:
    from unilabos.app.scheduler.inventory.backend_contract import (
        INVALID_PARAMETER,
        MATERIAL_NOT_FOUND,
        RESOURCE_DATA_CONFLICT,
        RESOURCE_NOT_FOUND,
    )

    code = getattr(error, "code", None)
    message = str(getattr(error, "message", error))
    mapped = {
        INVALID_PARAMETER: "invalid_parameter",
        RESOURCE_NOT_FOUND: "reagent_identity_not_found",
        RESOURCE_DATA_CONFLICT: "reagent_conflict",
        MATERIAL_NOT_FOUND: "material_not_found",
    }.get(code, "invalid_parameter")
    return MaterialIOError(mapped, message)


def _initial_material_template_hint(definition: Mapping[str, Any]) -> str:
    """读取库位模板元数据中待解析的默认子物料短名。"""

    metadata = definition.get("meta_data")
    unilab = (
        metadata.get("unilab")
        if isinstance(metadata, Mapping) and isinstance(metadata.get("unilab"), Mapping)
        else {}
    )
    raw_hint = unilab.get(INITIAL_MATERIAL_TEMPLATE_HINT)
    if not isinstance(raw_hint, str):
        return ""
    return raw_hint.strip()


def _lookup_resource_template_uuid(
    connection: sqlite3.Connection,
    identity: str,
) -> str:
    """按业务名或唯一短名解析活动资源模板 UUID。"""

    exact = connection.execute(
        "SELECT uuid FROM resource_template "
        "WHERE name=? AND deleted_at IS NULL",
        (identity,),
    ).fetchone()
    if exact is not None:
        return str(exact["uuid"])
    # 与模板同步短名别名一致：只有全局唯一的末段才可消歧。
    matches = [
        str(row["uuid"])
        for row in connection.execute(
            "SELECT uuid,name FROM resource_template WHERE deleted_at IS NULL"
        ).fetchall()
        if str(row["name"]).rsplit(".", 1)[-1] == identity
    ]
    if len(matches) == 1:
        return matches[0]
    return ""


def _resolve_initial_material_template_uuid(
    connection: sqlite3.Connection,
    definition: Mapping[str, Any],
) -> str:
    """把库位默认子物料 UUID 或短名解析为活动模板身份。"""

    explicit = str(definition.get("initial_material_template_uuid") or "").strip()
    hint = _initial_material_template_hint(definition)
    if explicit:
        return explicit
    if not hint:
        return ""
    resolved = _lookup_resource_template_uuid(connection, hint)
    if not resolved:
        raise MaterialIOError(
            "initial_template_not_found",
            f"Initial material template not found: {hint}",
        )
    return resolved


def _effect_identity(
    *, workflow_id: Any, node_id: Any, attempt: Any
) -> tuple[str, str, int]:
    workflow = _required_text(workflow_id, "workflow_id")
    node = _required_text(node_id, "node_id")
    if isinstance(attempt, bool) or (
        isinstance(attempt, float) and not attempt.is_integer()
    ):
        raise MaterialIOError("invalid_parameter", "attempt must be a positive integer")
    try:
        normalized_attempt = int(attempt)
    except (TypeError, ValueError) as error:
        raise MaterialIOError("invalid_parameter", "attempt must be a positive integer") from error
    if normalized_attempt <= 0:
        raise MaterialIOError("invalid_parameter", "attempt must be a positive integer")
    return workflow, node, normalized_attempt


def _request_hash(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(_dump(dict(payload)).encode("utf-8")).hexdigest()


def material_io_change_set(
    operation: str, canonical_request: Mapping[str, Any]
) -> dict[str, str]:
    """生成派发准入必须冻结的 Material I/O ChangeSet。"""

    normalized_operation = str(operation or "").strip().lower()
    if normalized_operation not in {"load", "unload", "refill"}:
        raise MaterialIOError("invalid_parameter", "Material I/O operation is invalid")
    request = dict(canonical_request)
    if request.get("operation") != normalized_operation:
        raise MaterialIOError(
            "invalid_parameter", "Canonical Material I/O request operation does not match"
        )
    return {
        "kind": "material_io",
        "operation": normalized_operation,
        "request_hash": _request_hash(request),
    }


def _record_material_names(
    connection: sqlite3.Connection,
    *,
    material_uuids: tuple[str, ...],
    event_type: str,
    quantity_delta: float | None = None,
    causation_id: str = "",
    workflow_task_uuid: str | None = None,
    workflow_node_job_uuid: str | None = None,
    changes: dict[str, Any] | None = None,
) -> None:
    """按当前物料名追加台账。名字在删除或改挂前读取。"""

    for material_uuid in material_uuids:
        row = connection.execute(
            "SELECT name FROM material WHERE uuid=?",
            (material_uuid,),
        ).fetchone()
        if row is None:
            continue
        InventoryStore.append_material_ledger(
            connection,
            material_uuid=material_uuid,
            name=str(row["name"] or ""),
            event_type=event_type,
            quantity_delta=quantity_delta,
            changes=changes,
            causation_id=causation_id,
            workflow_task_uuid=workflow_task_uuid,
            workflow_node_job_uuid=workflow_node_job_uuid,
        )


def _permit_job_uuid(permit: Mapping[str, Any] | None) -> str | None:
    """从已校验的 Permit 取出节点 Job；人工入口没有 Permit 时为空。"""

    if not isinstance(permit, Mapping):
        return None
    job_uuid = str(permit.get("job_uuid") or "").strip()
    return job_uuid or None


#: 软删除时保留原值的状态：消耗/报废是终态；已使用（used）同样不参与自动选料，
#: 保留它以区分「用过后随盒卸下」与「未用即随盒报废」（整盒卸料既有合同）
_RETIRED_INVENTORY_STATUSES = ("consumed", "discarded", "used")


def retire_deleted_material_inventory(
    connection: sqlite3.Connection, *, material_uuids: tuple[str, ...]
) -> None:
    """把已软删除物料的库存状态收敛为 discarded，并递增聚合版本。

    参数：``connection`` 是执行软删除的同一事务；``material_uuids`` 是本次软删除
    的物料。返回：无。已处于 consumed/discarded/used 的保持原值；其余状态
    （warehouse/reserved/bench/in_use/quarantined）一律改为 discarded。
    ``material_instance`` 兼容视图的更新触发器会在非终态时清空 ``deleted_at``，
    因此软删除物料不能停留在可选/可预留状态，否则后续状态写入（释放预留、
    上台、标记已使用等）会让物料复活；discarded 在状态机中没有出边。保留的
    used 只能迁往 consumed/discarded/quarantined/warehouse，其中非终态迁移由
    ``InventoryService._tx_set_instance_status`` 对已软删除物料统一拒绝。
    """

    placeholders = ",".join("?" for _ in _RETIRED_INVENTORY_STATUSES)
    for material_uuid in material_uuids:
        connection.execute(
            "UPDATE material SET "
            f"inventory_status=CASE WHEN inventory_status IN ({placeholders}) "
            "THEN inventory_status ELSE 'discarded' END,"
            f"disposition=CASE WHEN inventory_status IN ({placeholders}) "
            "THEN disposition ELSE 'discarded' END,"
            "aggregate_version=aggregate_version+1 WHERE uuid=?",
            (*_RETIRED_INVENTORY_STATUSES, *_RETIRED_INVENTORY_STATUSES, material_uuid),
        )


def assert_no_foreign_shared_source_binding(
    connection: sqlite3.Connection,
    *,
    material_uuids: tuple[str, ...],
    workflow_id: str | None = None,
) -> None:
    """拒绝搬走或删除其他任务仍以共享来源使用的物料（技术方案 §7.9）。

    参数：``material_uuids`` 是本次卸料/删除影响的完整物料集合；``workflow_id``
    是发起方任务，同一任务自己的共享绑定不阻塞（父流程共享绑定、子流程卸同一
    任务的盒是合法的）；为空时（公开整树删除）任何活动共享绑定都阻塞。
    返回：无。异常：命中时抛 ``MaterialIOError("active_claim")``，与活动 Claim
    冲突同一错误码，调用方应等待对方任务释放绑定后重试。
    """

    identities = sorted({str(item) for item in material_uuids if item})
    if not identities:
        return
    placeholders = ",".join("?" for _ in identities)
    sql = (
        "SELECT workflow_id,material_uuid FROM inventory_material_source_binding "
        "WHERE custody_policy='shared_source' AND status='active' "
        f"AND material_uuid IN ({placeholders})"
    )
    values: list[Any] = list(identities)
    owner = str(workflow_id or "").strip()
    if owner:
        sql += " AND workflow_id<>?"
        values.append(owner)
    blocker = connection.execute(
        sql + " ORDER BY created_at,binding_id LIMIT 1", tuple(values)
    ).fetchone()
    if blocker is not None:
        raise MaterialIOError(
            "active_claim",
            f"物料 {blocker['material_uuid']} 正由任务 {blocker['workflow_id']} "
            "以共享来源使用，需等待该任务释放后再卸下或删除",
        )


def _validate_unload_disposition(disposition: Any, scope: Any) -> None:
    """校验下料处置与范围组合；准备期预检与结算共用同一规则。"""

    normalized_disposition = str(disposition or "").strip().lower()
    normalized_scope = str(scope or "").strip().lower()
    if normalized_disposition not in {"detach", "soft_delete"}:
        raise MaterialIOError(
            "invalid_parameter", "disposition must be detach or soft_delete"
        )
    if normalized_scope not in {"material_only", "material_with_contents"}:
        raise MaterialIOError(
            "invalid_parameter",
            "scope must be material_only or material_with_contents",
        )
    if normalized_disposition == "detach" and normalized_scope != "material_only":
        raise MaterialIOError(
            "invalid_parameter",
            "detach only supports material_only; use soft_delete for contents",
        )


def _held_by_active_claim(
    connection: sqlite3.Connection, lock_keys: tuple[str, ...]
) -> bool:
    """判断资源是否正被任一活动 Claim（含本任务）持有，按调度同一层级冲突规则。

    持有期间调度会让本作业等待，库存状态可能在对方结算后改变，因此准备期
    预检对这类资源只能放行，交由等待后的再次准备与结算判定。
    """

    requested = {key for key in lock_keys if key}
    if not requested:
        return False
    held = {
        str(row["lock_key"])
        for row in connection.execute(
            "SELECT lock_key FROM station_execution_lock_lease "
            "WHERE state IN ('prepared','reserved','running','uncertain')"
        ).fetchall()
    }
    return bool(held and conflicting_resource_lock_keys(requested, held))


class MaterialIOService:
    """执行与工作流调度解耦的原子 Material I/O。"""

    def __init__(self, store: InventoryStore) -> None:
        self.store = store

    def material_subtree_uuids(self, material_uuid: str) -> tuple[str, ...]:
        """只读返回物料及其全部有效子物料身份，供卸料前的跨任务使用检查。"""

        with self.store.read_connection() as connection:
            return tuple(
                identity for identity, _ in self._material_tree(connection, material_uuid)
            )

    def resolve_resources(
        self,
        *,
        operation: Any,
        mount_material_uuid: Any = None,
        site_uuid: Any = None,
        site: Any = None,
        material_uuid: Any = None,
        scope: Any = None,
        disposition: Any = _NOT_PROVIDED,
        reagent: Any = _NOT_PROVIDED,
    ) -> tuple[dict[str, str], ...]:
        """按当前物料图解析装卸必须完整申领的 Material/Site 锁，并做准备期预检。

        参数：``operation`` 为 load/unload/refill；其余为冻结节点参数。
        ``disposition``（unload）与 ``reagent``（refill）省略时跳过对应预检。
        返回：稳定排序的锁描述。异常：参数或当前库存已可判定结算必然失败时抛
        ``MaterialIOError``，错误码与结算相同，调用方据此在打开人工确认前失败。
        预检只读、只复用结算规则：纯参数错误总是拒绝；依赖库存状态的规则
        （目标位占用、物料非空、试剂存在/单位/容量）在相关资源正被活动 Claim
        持有时跳过，因为调度会先等待该 Claim，状态可能在其结算后改变，最终仍
        由结算兜底。
        """

        normalized_operation = str(operation or "").strip().lower()
        with self.store.read_connection() as connection:
            if normalized_operation == "load":
                mount_identity = _required_text(
                    mount_material_uuid, "mount_material_uuid"
                )
                site_row = _resolve_mount_site(
                    connection,
                    mount_material_uuid=mount_identity,
                    site_uuid=str(site_uuid or "").strip(),
                    site_name=str(site or "").strip(),
                )
                resolved_site_uuid = str(site_row["uuid"])
                self._precheck_load_target(
                    connection,
                    mount_material_uuid=mount_identity,
                    site_row=site_row,
                )
                keys = {
                    material_lock_key(mount_identity): (
                        "material",
                        mount_identity,
                        "",
                    ),
                    site_lock_key(mount_identity, resolved_site_uuid): (
                        "material_site",
                        mount_identity,
                        resolved_site_uuid,
                    ),
                }
            elif normalized_operation == "unload":
                material_identity = _required_text(material_uuid, "material_uuid")
                normalized_scope = str(scope or "").strip().lower()
                if normalized_scope not in {
                    "material_only",
                    "material_with_contents",
                }:
                    raise MaterialIOError(
                        "invalid_parameter",
                        "scope must be material_only or material_with_contents",
                    )
                if disposition is not _NOT_PROVIDED:
                    _validate_unload_disposition(disposition, normalized_scope)
                target = connection.execute(
                    "SELECT 1 FROM material WHERE uuid=? AND deleted_at IS NULL",
                    (material_identity,),
                ).fetchone()
                if target is None:
                    raise MaterialIOError("material_not_found", "Material not found")
                if normalized_scope == "material_only" and not _held_by_active_claim(
                    connection, (material_lock_key(material_identity),)
                ):
                    self._require_empty_material(connection, material_identity)
                material_uuids = (
                    {
                        identity
                        for identity, _depth in self._material_tree(
                            connection, material_identity
                        )
                    }
                    if normalized_scope == "material_with_contents"
                    else {material_identity}
                )
                owned_sites = self._owned_sites(connection, material_uuids)
                keys = {
                    key: ("material", identity, "")
                    for identity in sorted(material_uuids)
                    if (key := material_lock_key(identity))
                }
                placeholders = ",".join("?" for _ in material_uuids)
                external_sites = connection.execute(
                    "SELECT uuid,material_uuid,name,occupied_material_uuid FROM site "
                    f"WHERE occupied_material_uuid IN ({placeholders}) "
                    "AND deleted_at IS NULL ORDER BY uuid",
                    tuple(sorted(material_uuids)),
                ).fetchall()
                requested_mount = str(mount_material_uuid or "").strip()
                requested_site_uuid = str(site_uuid or "").strip()
                requested_site_name = str(site or "").strip()
                if requested_mount or requested_site_uuid or requested_site_name:
                    actual_sites = [
                        row
                        for row in external_sites
                        if str(row["occupied_material_uuid"]) == material_identity
                    ]
                    if len(actual_sites) != 1:
                        raise MaterialIOError(
                            "site_not_found",
                            "Material is not occupying a unique mount site",
                        )
                    actual_site = actual_sites[0]
                    if (
                        requested_mount
                        and str(actual_site["material_uuid"]) != requested_mount
                    ):
                        raise MaterialIOError(
                            "site_not_found",
                            "Material is not mounted on the given mount",
                        )
                    if requested_site_uuid:
                        try:
                            canonical_site = str(UUID(requested_site_uuid))
                        except (AttributeError, TypeError, ValueError) as error:
                            raise MaterialIOError(
                                "invalid_parameter", "site_uuid 不是合法 UUID"
                            ) from error
                        if str(actual_site["uuid"]) != canonical_site:
                            raise MaterialIOError(
                                "site_not_found",
                                "Material is not occupying the given site",
                            )
                    if (
                        requested_site_name
                        and str(actual_site["name"]).casefold()
                        != requested_site_name.casefold()
                    ):
                        raise MaterialIOError(
                            "invalid_parameter",
                            "site 名称与物料实际库位不一致",
                        )
                for row in [*owned_sites, *external_sites]:
                    owner_uuid = str(row["material_uuid"])
                    site_identity = str(row["uuid"])
                    owner_key = material_lock_key(owner_uuid)
                    keys.setdefault(owner_key, ("material", owner_uuid, ""))
                    keys[site_lock_key(owner_uuid, site_identity)] = (
                        "material_site",
                        owner_uuid,
                        site_identity,
                    )
            elif normalized_operation == "refill":
                material_identity = _required_text(material_uuid, "material_uuid")
                mount_identity = _required_text(
                    mount_material_uuid, "mount_material_uuid"
                )
                keys = self._refill_lock_keys(
                    connection,
                    material_uuid=material_identity,
                    mount_material_uuid=mount_identity,
                    site_uuid=site_uuid,
                    site=site,
                )
                normalized_reagent = (
                    canonicalize_refill_reagent(reagent)
                    if reagent is not _NOT_PROVIDED
                    else None
                )
                if not _held_by_active_claim(
                    connection, (material_lock_key(material_identity),)
                ):
                    self._check_refill_reagent(
                        connection,
                        material_uuid=material_identity,
                        quantity=(
                            normalized_reagent["quantity"]
                            if normalized_reagent is not None
                            else None
                        ),
                        quantity_unit=(
                            normalized_reagent["quantity_unit"]
                            if normalized_reagent is not None
                            else None
                        ),
                    )
            else:
                raise MaterialIOError(
                    "invalid_parameter", "Material I/O operation is invalid"
                )
        return tuple(
            {
                "lock_key": key,
                "scope": values[0],
                "material_uuid": values[1],
                "site_uuid": values[2],
            }
            for key, values in sorted(keys.items())
        )

    def committed_production(self, *, task_uuid: str, job_uuid: str) -> dict[str, Any] | None:
        """投影崩溃恢复只读取原产样效果，验证归属后导出窄结果。"""
        saved = self.store.get_processed_command(material_io_command_id(task_uuid, job_uuid, 1))
        if saved is None:
            return None
        envelope = _json(saved["result_json"], {})
        request = envelope.get("request") or {}
        production = request.get("production")
        if (saved["status"] != "completed" or envelope.get("kind") != MATERIAL_IO_COMMAND_KIND
                or envelope.get("operation") != "load" or not isinstance(production, dict)
                or request.get("workflow_id") != task_uuid or request.get("node_id") != job_uuid):
            raise MaterialIOError("effect_corrupt", "产样登记的持久效果不一致")
        return {"material": deepcopy(envelope["result"]["material"]),
                "evidence_job_uuid": production["evidence_job_uuid"]}

    def load(
        self,
        *,
        template_uuid: Any,
        mount_material_uuid: Any,
        site_uuid: Any = None,
        site: Any = None,
        name: Any = None,
        workflow_id: Any,
        node_id: Any,
        attempt: Any,
        reagent: Any = None,
        barcode: Any = "",
        _production: Mapping[str, Any] | None = None,
        _permit: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """原子创建容器、模板 Site、初始子物料并装入目标 Site。"""

        template_identity = _required_text(template_uuid, "template_uuid")
        mount_identity = _required_text(mount_material_uuid, "mount_material_uuid")
        site_identity = str(site_uuid or "").strip()
        site_name = str(site or "").strip()
        supplied_name = _optional_text(name)
        material_barcode = _optional_text(barcode) or ""
        normalized_reagent = canonicalize_load_reagent(reagent)
        workflow, node, normalized_attempt = _effect_identity(
            workflow_id=workflow_id,
            node_id=node_id,
            attempt=attempt,
        )
        request = {
            "operation": "load",
            "template_uuid": template_identity,
            "mount_material_uuid": mount_identity,
            "workflow_id": workflow,
            "node_id": node,
            "attempt": normalized_attempt,
        }
        if site_identity:
            request["site_uuid"] = site_identity
        if site_name:
            request["site"] = site_name
        if supplied_name:
            request["name"] = supplied_name
        if material_barcode:
            request["barcode"] = material_barcode
        if normalized_reagent is not None:
            request["reagent"] = normalized_reagent
        if _production is not None:
            request["production"] = dict(_production)
        digest = _request_hash(request)

        try:
            with self.store.transaction() as connection:
                claim_state = self._validate_settlement_permit(
                    connection,
                    permit=_permit,
                    request_hash=digest,
                    operation="load",
                    attempt=normalized_attempt,
                    evidence_only=True,
                )
                replay = self._replay(
                    connection,
                    workflow_id=workflow,
                    node_id=node,
                    attempt=normalized_attempt,
                    operation="load",
                    request_hash=digest,
                    expected_effect_uuid=(
                        str(_permit.get("effect_uuid") or "") if _permit else ""
                    ),
                )
                if replay is not None:
                    return replay
                if _production is not None and connection.execute(
                    "SELECT 1 FROM material WHERE barcode=? AND deleted_at IS NULL", (material_barcode,)
                ).fetchone():
                    raise MaterialIOError("production_barcode_conflict", "产物条码已存在，须核实原产样记录")
                if claim_state == "released":
                    raise MaterialIOError(
                        "permit_invalid",
                        "Released Claim has no committed Material I/O effect",
                    )

                template = connection.execute(
                    "SELECT name,resource_type,available_sites,tags FROM resource_template "
                    "WHERE uuid=? AND deleted_at IS NULL",
                    (template_identity,),
                ).fetchone()
                if template is None:
                    raise MaterialIOError("template_not_found", "Material template not found")
                if normalized_reagent is not None:
                    tags = {
                        str(tag).strip().lower()
                        for tag in _json(template["tags"], [])
                    }
                    if "container" not in tags:
                        raise MaterialIOError(
                            "invalid_parameter",
                            "load reagent requires a container material template",
                        )
                mount = connection.execute(
                    "SELECT uuid FROM material WHERE uuid=? AND deleted_at IS NULL",
                    (mount_identity,),
                ).fetchone()
                if mount is None:
                    raise MaterialIOError("mount_not_found", "Mount material not found")
                target_site = _resolve_mount_site(
                    connection,
                    mount_material_uuid=mount_identity,
                    site_uuid=site_identity,
                    site_name=site_name,
                )
                resolved_site_uuid = str(target_site["uuid"])
                occupied = str(target_site["occupied_material_uuid"] or "")
                if occupied:
                    occupier = connection.execute(
                        "SELECT deleted_at FROM material WHERE uuid=?",
                        (occupied,),
                    ).fetchone()
                    if occupier is not None and occupier["deleted_at"] is None:
                        raise MaterialIOError("site_occupied", "Mount Site is occupied")
                    # 软删物料若仍写在 occupied_material_uuid，视为空位并清掉脏占用。
                    clear_site_occupancy(connection, material_uuid=occupied)
                allowed = _json(target_site["allowed_resource_template_uuids"], [])
                if allowed and template_identity not in allowed:
                    raise MaterialIOError(
                        "site_template_not_allowed",
                        "Material template is not allowed by the Mount Site",
                    )

                if _permit is None:
                    assert_inventory_mutation_unclaimed(
                        connection,
                        material_uuids=(mount_identity,),
                        site_uuids=(resolved_site_uuid,),
                    )
                else:
                    self._validate_settlement_permit(
                        connection,
                        permit=_permit,
                        request_hash=digest,
                        operation="load",
                        attempt=normalized_attempt,
                        required_fence_keys={
                            material_lock_key(mount_identity),
                            site_lock_key(mount_identity, resolved_site_uuid),
                        },
                    )
                now = _now()
                material_name = supplied_name or allocate_load_material_name(
                    prefix=_load_name_prefix(str(template["name"])),
                    now=datetime.now().astimezone(),
                    taken=lambda candidate: _load_name_taken(connection, candidate),
                )
                material_uuid = str(uuid4())
                self._insert_material(
                    connection,
                    material_uuid=material_uuid,
                    template_uuid=template_identity,
                    parent_uuid=mount_identity,
                    registry_class=_material_registry_class(template),
                    resource_type=str(template["resource_type"]),
                    name=material_name,
                    barcode=material_barcode,
                    now=now,
                    meta_data={"material_io_effect": f"{workflow}/{node}/{normalized_attempt}",
                               **({"runtime_production": dict(_production), "simulation": _production["simulation"]}
                                  if _production is not None else {})},
                )

                created_site_uuids, initial_material_uuids = (
                    self.materialize_template_contents(
                        connection,
                        material_uuid=material_uuid,
                        template=template,
                        material_name=material_name,
                        now=now,
                        effect_token=f"{workflow}/{node}/{normalized_attempt}",
                    )
                )

                reagent_result = (
                    self._attach_load_reagent(
                        connection,
                        material_uuid=material_uuid,
                        reagent=normalized_reagent,
                    )
                    if normalized_reagent is not None
                    else {}
                )
                set_site_occupancy(
                    connection,
                    site_uuid=resolved_site_uuid,
                    material_uuid=material_uuid,
                    update_time=now,
                )
                _record_material_names(
                    connection,
                    material_uuids=(material_uuid, *initial_material_uuids),
                    event_type="material.load",
                    quantity_delta=1,
                    causation_id=f"{workflow}/{node}/{normalized_attempt}",
                    workflow_task_uuid=workflow,
                    workflow_node_job_uuid=(node if _production is not None else _permit_job_uuid(_permit)),
                )
                result = {
                    "effect_uuid": (
                        str(_permit["effect_uuid"]) if _permit else str(uuid4())
                    ),
                    "operation": "load",
                    "request_hash": digest,
                    "material_uuid": material_uuid,
                    "name": material_name,
                    "material": {"uuid": material_uuid},
                    "mount_material_uuid": mount_identity,
                    "mount_site_uuid": resolved_site_uuid,
                    "site_uuids": created_site_uuids,
                    "initial_material_uuids": initial_material_uuids,
                    **reagent_result,
                }
                self._record_effect(
                    connection,
                    result=result,
                    request=request,
                    workflow_id=workflow,
                    node_id=node,
                    attempt=normalized_attempt,
                )
                return result
        except InventoryMutationConflict as error:
            raise MaterialIOError("active_claim", str(error)) from error
        except SiteOccupancyConflict as error:
            raise MaterialIOError("site_occupancy_conflict", str(error)) from error
        except sqlite3.IntegrityError as error:
            raise MaterialIOError(
                "identity_conflict", "Material or Site identity conflicts"
            ) from error
        except DispatchAdmissionConflict as error:
            raise MaterialIOError("permit_invalid", str(error)) from error

    def settle_load(
        self,
        *,
        permit: Mapping[str, Any],
        **values: Any,
    ) -> dict[str, Any]:
        """用当前完整 Permit/Fence 在同一事务执行批准后的 Load。"""

        return self.load(**values, _permit=permit)

    def refill(
        self,
        *,
        material_uuid: Any,
        mount_material_uuid: Any,
        workflow_id: Any,
        node_id: Any,
        attempt: Any,
        site_uuid: Any = None,
        site: Any = None,
        reagent: Any = None,
        quantity: Any = None,
        quantity_unit: Any = None,
        _permit: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """在已占位容器上增加试剂余量，不改库位占用、不新建身份。"""

        material_identity = _required_text(material_uuid, "material_uuid")
        mount_identity = _required_text(mount_material_uuid, "mount_material_uuid")
        site_identity = (
            _required_text(site_uuid, "site_uuid") if site_uuid not in (None, "") else ""
        )
        if reagent is None:
            reagent = {"quantity": quantity, "quantity_unit": quantity_unit}
        normalized_reagent = canonicalize_refill_reagent(reagent)
        workflow, node, normalized_attempt = _effect_identity(
            workflow_id=workflow_id,
            node_id=node_id,
            attempt=attempt,
        )
        request = {
            "operation": "refill",
            "material_uuid": material_identity,
            "mount_material_uuid": mount_identity,
            "workflow_id": workflow,
            "node_id": node,
            "attempt": normalized_attempt,
            "reagent": normalized_reagent,
        }
        if site_identity:
            request["site_uuid"] = site_identity
        requested_site_name = str(site or "").strip()
        if requested_site_name:
            request["site"] = requested_site_name
        digest = _request_hash(request)

        try:
            with self.store.transaction() as connection:
                claim_state = self._validate_settlement_permit(
                    connection,
                    permit=_permit,
                    request_hash=digest,
                    operation="refill",
                    attempt=normalized_attempt,
                    evidence_only=True,
                )
                replay = self._replay(
                    connection,
                    workflow_id=workflow,
                    node_id=node,
                    attempt=normalized_attempt,
                    operation="refill",
                    request_hash=digest,
                    expected_effect_uuid=(
                        str(_permit.get("effect_uuid") or "") if _permit else ""
                    ),
                )
                if replay is not None:
                    return replay
                if claim_state == "released":
                    raise MaterialIOError(
                        "permit_invalid",
                        "Released Claim has no committed Material I/O effect",
                    )
                lock_keys = self._refill_lock_keys(
                    connection,
                    material_uuid=material_identity,
                    mount_material_uuid=mount_identity,
                    site_uuid=site_identity or None,
                    site=site,
                )
                occupying = connection.execute(
                    "SELECT uuid,material_uuid FROM site "
                    "WHERE occupied_material_uuid=? AND deleted_at IS NULL",
                    (material_identity,),
                ).fetchone()
                if occupying is None:
                    raise MaterialIOError(
                        "site_not_found", "Material is not occupying a mount site"
                    )
                if _permit is None:
                    assert_inventory_mutation_unclaimed(
                        connection,
                        material_uuids=(material_identity, mount_identity),
                        site_uuids=(str(occupying["uuid"]),),
                    )
                else:
                    self._validate_settlement_permit(
                        connection,
                        permit=_permit,
                        request_hash=digest,
                        operation="refill",
                        attempt=normalized_attempt,
                        required_fence_keys=set(lock_keys),
                    )
                added = self._increment_existing_reagent(
                    connection,
                    material_uuid=material_identity,
                    quantity=normalized_reagent["quantity"],
                    quantity_unit=normalized_reagent["quantity_unit"],
                )
                result = {
                    "effect_uuid": (
                        str(_permit["effect_uuid"]) if _permit else str(uuid4())
                    ),
                    "operation": "refill",
                    "request_hash": digest,
                    "material_uuid": material_identity,
                    "material": {"uuid": material_identity},
                    "mount_material_uuid": mount_identity,
                    "mount_site_uuid": str(occupying["uuid"]),
                    "quantity_added": normalized_reagent["quantity"],
                    **added,
                }
                self._record_effect(
                    connection,
                    result=result,
                    request=request,
                    workflow_id=workflow,
                    node_id=node,
                    attempt=normalized_attempt,
                )
                return result
        except InventoryMutationConflict as error:
            raise MaterialIOError("active_claim", str(error)) from error
        except DispatchAdmissionConflict as error:
            raise MaterialIOError("permit_invalid", str(error)) from error

    def settle_refill(
        self,
        *,
        permit: Mapping[str, Any],
        **values: Any,
    ) -> dict[str, Any]:
        """用当前完整 Permit/Fence 在同一事务执行批准后的补液。"""

        return self.refill(**values, _permit=permit)

    def _attach_load_reagent(
        self,
        connection: sqlite3.Connection,
        *,
        material_uuid: str,
        reagent: Mapping[str, Any],
    ) -> dict[str, Any]:
        """在已创建的上料容器上按合同创建或复用试剂身份，再写入试剂实例。"""

        from unilabos.app.scheduler.inventory.backend_contract import (
            BackendContractError,
        )
        from unilabos.app.scheduler.inventory.reagent_contract import (
            BackendReagentService,
        )

        service = BackendReagentService(self.store)
        created_identity = False
        info_uuid = str(reagent.get("reagent_info_uuid") or "")
        try:
            if not info_uuid:
                cas = str(reagent.get("cas") or "")
                if cas:
                    existing = connection.execute(
                        "SELECT uuid FROM reagent_info "
                        "WHERE LOWER(cas)=LOWER(?) AND deleted_at IS NULL",
                        (cas,),
                    ).fetchone()
                    if existing is not None:
                        info_uuid = str(existing["uuid"])
                if not info_uuid:
                    name = str(reagent.get("name") or "").strip()
                    if not name:
                        raise MaterialIOError(
                            "reagent_identity_not_found",
                            "reagent identity is not registered",
                        )
                    identity_values = {
                        field: reagent[field]
                        for field in _REAGENT_IDENTITY_FIELDS
                        if field in reagent
                    }
                    identity_values["name"] = name
                    created = service.create_reagent_info_in_transaction(
                        connection, identity_values
                    )
                    info_uuid = str(created["uuid"])
                    created_identity = True
            instance_values = {
                field: reagent[field]
                for field in _REAGENT_INSTANCE_FIELDS
                if field in reagent
            }
            instance_values["material_uuid"] = material_uuid
            instance_values["reagent_info_uuid"] = info_uuid
            if "container_capacity" not in instance_values:
                instance_values["container_capacity"] = (
                    _default_container_capacity(
                        instance_values["quantity"],
                        instance_values["quantity_unit"],
                    )
                )
            snapshot = service.create_reagent_in_transaction(
                connection, instance_values
            )
        except BackendContractError as error:
            raise _material_io_error_from_reagent(error) from error
        reagent_row = snapshot["reagent"]
        info_row = snapshot["reagent_info"]
        return {
            "reagent": {
                "uuid": reagent_row["uuid"],
                "material_uuid": reagent_row["material_uuid"],
                "reagent_info_uuid": reagent_row["reagent_info_uuid"],
                "quantity": reagent_row["quantity"],
                "quantity_unit": reagent_row["quantity_unit"],
                "concentration_value": reagent_row.get("concentration_value"),
                "concentration_unit": reagent_row.get("concentration_unit"),
            },
            "reagent_info": {
                "uuid": info_row["uuid"],
                "name": info_row.get("name"),
                "cas": info_row.get("cas"),
                "physical_state": info_row.get("physical_state"),
            },
            "reagent_info_created": created_identity,
        }

    def unload(
        self,
        *,
        material_uuid: Any,
        disposition: Any,
        scope: Any,
        workflow_id: Any,
        node_id: Any,
        attempt: Any,
        _permit: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """原子解除空物料挂载，或叶到根软删除物料内容树。"""

        material_identity = _required_text(material_uuid, "material_uuid")
        normalized_disposition = str(disposition or "").strip().lower()
        normalized_scope = str(scope or "").strip().lower()
        _validate_unload_disposition(normalized_disposition, normalized_scope)
        workflow, node, normalized_attempt = _effect_identity(
            workflow_id=workflow_id,
            node_id=node_id,
            attempt=attempt,
        )
        request = {
            "operation": "unload",
            "material_uuid": material_identity,
            "disposition": normalized_disposition,
            "scope": normalized_scope,
            "workflow_id": workflow,
            "node_id": node,
            "attempt": normalized_attempt,
        }
        digest = _request_hash(request)

        try:
            with self.store.transaction() as connection:
                claim_state = self._validate_settlement_permit(
                    connection,
                    permit=_permit,
                    request_hash=digest,
                    operation="unload",
                    attempt=normalized_attempt,
                    evidence_only=True,
                )
                replay = self._replay(
                    connection,
                    workflow_id=workflow,
                    node_id=node,
                    attempt=normalized_attempt,
                    operation="unload",
                    request_hash=digest,
                    expected_effect_uuid=(
                        str(_permit.get("effect_uuid") or "") if _permit else ""
                    ),
                )
                if replay is not None:
                    return replay
                if claim_state == "released":
                    raise MaterialIOError(
                        "permit_invalid",
                        "Released Claim has no committed Material I/O effect",
                    )
                target = connection.execute(
                    "SELECT uuid FROM material WHERE uuid=? AND deleted_at IS NULL",
                    (material_identity,),
                ).fetchone()
                if target is None:
                    raise MaterialIOError("material_not_found", "Material not found")

                if normalized_scope == "material_only":
                    self._require_empty_material(connection, material_identity)
                    material_depths = [(material_identity, 0)]
                else:
                    material_depths = self._material_tree(connection, material_identity)
                    descendants = {identity for identity, _ in material_depths}
                    owned_sites = self._owned_sites(connection, descendants)
                    foreign_occupants = {
                        str(row["occupied_material_uuid"])
                        for row in owned_sites
                        if row["occupied_material_uuid"]
                        and str(row["occupied_material_uuid"]) not in descendants
                    }
                    if foreign_occupants:
                        raise MaterialIOError(
                            "material_graph_conflict",
                            "A contained Site is occupied by a Material outside the subtree",
                        )

                material_uuids = tuple(identity for identity, _ in material_depths)
                owned_sites = self._owned_sites(connection, set(material_uuids))
                if _permit is None:
                    # 执行租约之外，其他任务的活动共享来源绑定同样表示托盘仍在使用。
                    # 工作流结算入口（带 Permit）不在此处复查：派发准入才是等待点，
                    # 批准后的结算再拒绝只会让作业失败，且会让两个都以共享来源
                    # 绑定同一旧盒、各自准备卸下它的任务互相阻塞。
                    assert_no_foreign_shared_source_binding(
                        connection,
                        material_uuids=material_uuids,
                        workflow_id=workflow,
                    )
                    assert_inventory_mutation_unclaimed(
                        connection,
                        material_uuids=material_uuids,
                        site_uuids=tuple(str(row["uuid"]) for row in owned_sites),
                    )
                else:
                    self._validate_settlement_permit(
                        connection,
                        permit=_permit,
                        request_hash=digest,
                        operation="unload",
                        attempt=normalized_attempt,
                        required_fence_keys=self._unload_fence_keys(
                            connection,
                            material_uuids=set(material_uuids),
                            owned_sites=owned_sites,
                        ),
                    )
                now = _now()
                if normalized_disposition == "detach":
                    _record_material_names(
                        connection,
                        material_uuids=(material_identity,),
                        event_type="material.detach",
                        causation_id=f"{workflow}/{node}/{normalized_attempt}",
                        workflow_task_uuid=workflow,
                        workflow_node_job_uuid=_permit_job_uuid(_permit),
                    )
                    clear_site_occupancy(
                        connection, material_uuid=material_identity, update_time=now
                    )
                    connection.execute(
                        "UPDATE material SET parent_uuid=NULL,update_time=?,"
                        "aggregate_version=aggregate_version+1 "
                        "WHERE uuid=? AND deleted_at IS NULL",
                        (now, material_identity),
                    )
                    affected = [material_identity]
                else:
                    affected = self._soft_delete_tree(
                        connection,
                        material_depths=material_depths,
                        now=now,
                        causation_id=f"{workflow}/{node}/{normalized_attempt}",
                        workflow_task_uuid=workflow,
                        workflow_node_job_uuid=_permit_job_uuid(_permit),
                    )

                result = {
                    "effect_uuid": (
                        str(_permit["effect_uuid"]) if _permit else str(uuid4())
                    ),
                    "operation": "unload",
                    "request_hash": digest,
                    "material_uuid": material_identity,
                    "material": {"uuid": material_identity},
                    "disposition": normalized_disposition,
                    "scope": normalized_scope,
                    "affected_material_uuids": affected,
                }
                self._record_effect(
                    connection,
                    result=result,
                    request=request,
                    workflow_id=workflow,
                    node_id=node,
                    attempt=normalized_attempt,
                )
                return result
        except InventoryMutationConflict as error:
            raise MaterialIOError("active_claim", str(error)) from error
        except SiteOccupancyConflict as error:
            raise MaterialIOError("site_occupancy_conflict", str(error)) from error
        except DispatchAdmissionConflict as error:
            raise MaterialIOError("permit_invalid", str(error)) from error

    def settle_unload(
        self,
        *,
        permit: Mapping[str, Any],
        **values: Any,
    ) -> dict[str, Any]:
        """用当前完整 Permit/Fence 在同一事务执行批准后的 Unload。"""

        return self.unload(**values, _permit=permit)

    def dispose_contents(
        self,
        connection: sqlite3.Connection,
        *,
        material_uuid: str,
        now: str,
    ) -> list[str]:
        """在当前事务软删除物料整树，不写工作流效果身份。"""

        target = connection.execute(
            "SELECT uuid FROM material WHERE uuid=? AND deleted_at IS NULL",
            (material_uuid,),
        ).fetchone()
        if target is None:
            raise MaterialIOError("material_not_found", "Material not found")
        material_depths = self._material_tree(connection, material_uuid)
        descendants = {identity for identity, _ in material_depths}
        owned_sites = self._owned_sites(connection, descendants)
        foreign_occupants = {
            str(row["occupied_material_uuid"])
            for row in owned_sites
            if row["occupied_material_uuid"]
            and str(row["occupied_material_uuid"]) not in descendants
        }
        if foreign_occupants:
            raise MaterialIOError(
                "material_graph_conflict",
                "A contained Site is occupied by a Material outside the subtree",
            )
        assert_inventory_mutation_unclaimed(
            connection,
            material_uuids=tuple(descendants),
            site_uuids=tuple(str(row["uuid"]) for row in owned_sites),
        )
        # 公开整树删除没有任务身份：任何任务的活动共享绑定都阻塞
        assert_no_foreign_shared_source_binding(
            connection, material_uuids=tuple(descendants)
        )
        return self._soft_delete_tree(
            connection, material_depths=material_depths, now=now
        )

    @staticmethod
    def _validate_settlement_permit(
        connection: sqlite3.Connection,
        *,
        permit: Mapping[str, Any] | None,
        request_hash: str,
        operation: str,
        attempt: int,
        required_fence_keys: set[str] | None = None,
        evidence_only: bool = False,
    ) -> str | None:
        """严格验证批准效果及完整 Fence；公共调用不经过此旁路。"""

        if permit is None:
            return None
        if not isinstance(permit, Mapping):
            raise MaterialIOError("permit_invalid", "Material I/O Permit must be an object")
        expected_change_set = permit.get("expected_change_set")
        expected_material_io_change = {
            "kind": "material_io",
            "operation": operation,
            "request_hash": request_hash,
        }
        if (
            not isinstance(expected_change_set, Mapping)
            or dict(expected_change_set) != expected_material_io_change
        ):
            raise MaterialIOError(
                "permit_invalid",
                "Material I/O request does not match the approved ChangeSet",
            )
        raw_fences = permit.get("fences")
        if not isinstance(raw_fences, Mapping):
            raise MaterialIOError(
                "permit_invalid", "Material I/O Permit fences must be an object"
            )
        try:
            fences: dict[str, int] = {}
            for lock_key, token in raw_fences.items():
                if (
                    not isinstance(lock_key, str)
                    or not lock_key.strip()
                    or lock_key != lock_key.strip()
                    or isinstance(token, bool)
                    or not isinstance(token, int)
                    or token <= 0
                ):
                    raise MaterialIOError(
                        "permit_invalid", "Material I/O Permit Fence is invalid"
                    )
                fences[lock_key] = token
            effect_uuid = _required_text(permit.get("effect_uuid"), "permit.effect_uuid")
            claim_uuid = _required_text(permit.get("claim_uuid"), "permit.claim_uuid")
            task_uuid = _required_text(permit.get("task_uuid"), "permit.task_uuid")
            job_uuid = _required_text(permit.get("job_uuid"), "permit.job_uuid")
            parameter_hash = _required_text(
                permit.get("parameter_hash"), "permit.parameter_hash"
            )
            permit_attempt = int(permit.get("attempt"))
        except (TypeError, ValueError) as error:
            raise MaterialIOError(
                "permit_invalid", "Material I/O Permit identity is invalid"
            ) from error
        if permit_attempt != attempt:
            raise MaterialIOError(
                "permit_invalid", "Material I/O Permit attempt does not match the request"
            )
        claim_state = validate_physical_settlement_credentials(
            connection,
            effect_uuid=effect_uuid,
            claim_uuid=claim_uuid,
            job_uuid=job_uuid,
            attempt=permit_attempt,
            parameter_hash=parameter_hash,
            expected_change_set=expected_change_set,
            fences=fences,
            allow_released_replay=True,
            evidence_only=evidence_only,
        )
        claim = connection.execute(
            "SELECT task_uuid,resource_keys FROM station_execution_claim WHERE claim_uuid=?",
            (claim_uuid,),
        ).fetchone()
        if claim is None or str(claim["task_uuid"]) != task_uuid:
            raise MaterialIOError(
                "permit_invalid", "Material I/O Permit task identity does not match"
            )
        persisted_resource_keys = _json(claim["resource_keys"], None)
        if (
            not isinstance(persisted_resource_keys, list)
            or any(not isinstance(key, str) or not key for key in persisted_resource_keys)
            or len(set(persisted_resource_keys)) != len(persisted_resource_keys)
            or set(persisted_resource_keys) != set(fences)
        ):
            raise MaterialIOError(
                "permit_invalid",
                "Material I/O Claim resource set does not match its complete Fence set",
            )
        if required_fence_keys is not None and not required_fence_keys.issubset(fences):
            raise MaterialIOError(
                "permit_invalid",
                "Material I/O Permit does not fence the complete Material/Site write set",
            )
        return claim_state

    @staticmethod
    def _unload_fence_keys(
        connection: sqlite3.Connection,
        *,
        material_uuids: set[str],
        owned_sites: list[sqlite3.Row],
    ) -> set[str]:
        """构造与公共 Claim 检查一致的 Unload 完整物料图锁集合。"""

        required = {
            material_lock_key(material_uuid) for material_uuid in material_uuids
        }
        for site in owned_sites:
            required.add(site_lock_key(str(site["material_uuid"]), str(site["uuid"])))
        if not material_uuids:
            return required
        placeholders = ",".join("?" for _ in material_uuids)
        external_sites = connection.execute(
            "SELECT uuid,material_uuid FROM site "
            f"WHERE occupied_material_uuid IN ({placeholders}) "
            "AND deleted_at IS NULL ORDER BY uuid",
            tuple(sorted(material_uuids)),
        ).fetchall()
        for site in external_sites:
            owner_uuid = str(site["material_uuid"])
            required.update(
                {
                    material_lock_key(owner_uuid),
                    site_lock_key(owner_uuid, str(site["uuid"])),
                }
            )
        return required

    def materialize_template_contents(
        self,
        connection: sqlite3.Connection,
        *,
        material_uuid: str,
        template: Mapping[str, Any],
        material_name: str,
        now: str,
        effect_token: str = "",
    ) -> tuple[list[str], list[str]]:
        """按模板 available_sites 建库位，并放入默认子物料。"""

        if isinstance(template, Mapping):
            raw_sites = template.get("available_sites")
        else:
            raw_sites = (
                template["available_sites"]
                if "available_sites" in template.keys()
                else []
            )
        available_sites = _json(raw_sites, [])
        if not isinstance(available_sites, list):
            raise MaterialIOError(
                "template_invalid", "Material template available_sites is invalid"
            )
        initial_templates: dict[str, sqlite3.Row] = {}
        for definition in available_sites:
            if not isinstance(definition, dict):
                raise MaterialIOError(
                    "template_invalid", "Material template Site definition is invalid"
                )
            initial_uuid = _resolve_initial_material_template_uuid(
                connection,
                definition,
            )
            if not initial_uuid or initial_uuid in initial_templates:
                continue
            initial_template = connection.execute(
                "SELECT name,resource_type FROM resource_template "
                "WHERE uuid=? AND deleted_at IS NULL",
                (initial_uuid,),
            ).fetchone()
            if initial_template is None:
                raise MaterialIOError(
                    "initial_template_not_found",
                    f"Initial material template not found: {initial_uuid}",
                )
            initial_templates[initial_uuid] = initial_template

        created_site_uuids: list[str] = []
        initial_material_uuids: list[str] = []
        for ordinal, definition in enumerate(available_sites):
            created_site_uuid = str(uuid4())
            created_site_uuids.append(created_site_uuid)
            label = _required_text(
                definition.get("label", definition.get("index", ordinal)),
                f"available_sites[{ordinal}].label",
            )
            metadata = dict(definition.get("meta_data") or {})
            metadata.setdefault("template_site_index", definition.get("index", ordinal))
            connection.execute(
                """
                INSERT INTO site(
                    uuid,create_time,update_time,deleted_at,description,meta_data,
                    material_uuid,name,sort_order,allowed_resource_template_uuids,
                    occupied_material_uuid,position_x,position_y,position_z,
                    depth,length,width
                ) VALUES (?,?,?,NULL,?,?,?,?,?,?,NULL,?,?,?,?,?,?)
                """,
                (
                    created_site_uuid,
                    now,
                    now,
                    definition.get("description") or None,
                    _dump(metadata),
                    material_uuid,
                    label,
                    ordinal,
                    _dump(definition.get("allowed_resource_template_uuids") or []),
                    float(definition.get("position_x") or 0),
                    float(definition.get("position_y") or 0),
                    float(definition.get("position_z") or 0),
                    float(definition.get("depth") or 0),
                    float(definition.get("length") or 0),
                    float(definition.get("width") or 0),
                ),
            )
            initial_uuid = _resolve_initial_material_template_uuid(
                connection,
                definition,
            )
            if not initial_uuid:
                continue
            child_uuid = str(uuid4())
            initial_material_uuids.append(child_uuid)
            child_meta = {"material_io_initial_site_uuid": created_site_uuid}
            if effect_token:
                child_meta["material_io_effect"] = effect_token
            self._insert_material(
                connection,
                material_uuid=child_uuid,
                template_uuid=initial_uuid,
                parent_uuid=material_uuid,
                registry_class=_material_registry_class(
                    initial_templates[initial_uuid]
                ),
                resource_type=str(initial_templates[initial_uuid]["resource_type"]),
                name=f"{material_name} / {label}",
                now=now,
                meta_data=child_meta,
            )
            set_site_occupancy(
                connection,
                site_uuid=created_site_uuid,
                material_uuid=child_uuid,
                update_time=now,
            )
        return created_site_uuids, initial_material_uuids

    @staticmethod
    def _insert_material(
        connection: sqlite3.Connection,
        *,
        material_uuid: str,
        template_uuid: str,
        parent_uuid: str,
        registry_class: str,
        resource_type: str,
        name: str,
        now: str,
        meta_data: Mapping[str, Any],
        barcode: str = "",
    ) -> None:
        connection.execute(
            """
            INSERT INTO material(
                uuid,create_time,update_time,deleted_at,description,meta_data,
                resource_template_uuid,parent_uuid,class,type,barcode,name,config,data,
                legacy_template_id,aggregate_version
            ) VALUES (?,?,?,NULL,NULL,?,?,?,?,?,?,?,'{}','{}',?,1)
            """,
            (
                material_uuid,
                now,
                now,
                _dump(dict(meta_data)),
                template_uuid,
                parent_uuid,
                registry_class,
                resource_type,
                barcode,
                name,
                template_uuid,
            ),
        )

    @staticmethod
    def _precheck_load_target(
        connection: sqlite3.Connection,
        *,
        mount_material_uuid: str,
        site_row: sqlite3.Row,
    ) -> None:
        """准备期只读预检上料目标位占用，口径与结算相同。

        结算把已软删除的占用者视为空位；目标位或占用者正被活动 Claim 持有
        （例如同任务或他任务的下料/搬出尚未结算）时放行，由锁等待与结算判定。
        """

        occupied = str(site_row["occupied_material_uuid"] or "")
        if not occupied:
            return
        occupier = connection.execute(
            "SELECT deleted_at FROM material WHERE uuid=?",
            (occupied,),
        ).fetchone()
        if occupier is None or occupier["deleted_at"] is not None:
            return
        if _held_by_active_claim(
            connection,
            (
                site_lock_key(mount_material_uuid, str(site_row["uuid"])),
                material_lock_key(occupied),
            ),
        ):
            return
        raise MaterialIOError("site_occupied", "Mount Site is occupied")

    @staticmethod
    def _require_empty_material(
        connection: sqlite3.Connection, material_uuid: str
    ) -> None:
        linked = connection.execute(
            """
            SELECT 1 FROM site
            WHERE material_uuid=? AND deleted_at IS NULL
            UNION ALL
            SELECT 1 FROM material
            WHERE parent_uuid=? AND deleted_at IS NULL
            UNION ALL
            SELECT 1 FROM container_content
            WHERE material_uuid=? AND deleted_at IS NULL
            LIMIT 1
            """,
            (material_uuid,) * 3,
        ).fetchone()
        if linked is not None:
            raise MaterialIOError(
                "material_not_empty",
                "material_only requires no Sites, child Materials, or container contents",
            )

    @staticmethod
    def _material_tree(
        connection: sqlite3.Connection, root_uuid: str
    ) -> list[tuple[str, int]]:
        result: list[tuple[str, int]] = [(root_uuid, 0)]
        seen = {root_uuid}
        frontier = [root_uuid]
        depth = 0
        while frontier:
            placeholders = ",".join("?" for _ in frontier)
            rows = connection.execute(
                "SELECT uuid FROM material "
                f"WHERE parent_uuid IN ({placeholders}) AND deleted_at IS NULL "
                "ORDER BY uuid",
                tuple(frontier),
            ).fetchall()
            next_frontier: list[str] = []
            depth += 1
            for row in rows:
                identity = str(row["uuid"])
                if identity in seen:
                    raise MaterialIOError(
                        "material_graph_conflict", "Material parent graph contains a cycle"
                    )
                seen.add(identity)
                next_frontier.append(identity)
                result.append((identity, depth))
            frontier = next_frontier
        return result

    @staticmethod
    def _owned_sites(
        connection: sqlite3.Connection, material_uuids: set[str]
    ) -> list[sqlite3.Row]:
        if not material_uuids:
            return []
        placeholders = ",".join("?" for _ in material_uuids)
        return list(
            connection.execute(
                "SELECT uuid,material_uuid,occupied_material_uuid FROM site "
                f"WHERE material_uuid IN ({placeholders}) AND deleted_at IS NULL "
                "ORDER BY uuid",
                tuple(sorted(material_uuids)),
            ).fetchall()
        )

    @staticmethod
    def _soft_delete_tree(
        connection: sqlite3.Connection,
        *,
        material_depths: list[tuple[str, int]],
        now: str,
        causation_id: str = "",
        workflow_task_uuid: str | None = None,
        workflow_node_job_uuid: str | None = None,
    ) -> list[str]:
        ordered = [
            identity
            for identity, _ in sorted(material_depths, key=lambda item: (-item[1], item[0]))
        ]
        for material_uuid in ordered:
            _record_material_names(
                connection,
                material_uuids=(material_uuid,),
                event_type="material.unload",
                quantity_delta=-1,
                causation_id=causation_id,
                workflow_task_uuid=workflow_task_uuid,
                workflow_node_job_uuid=workflow_node_job_uuid,
            )
            clear_site_occupancy(
                connection, material_uuid=material_uuid, update_time=now
            )
            connection.execute(
                "UPDATE site SET occupied_material_uuid=NULL,deleted_at=?,update_time=? "
                "WHERE material_uuid=? AND deleted_at IS NULL",
                (now, now, material_uuid),
            )
            connection.execute(
                "UPDATE container_content SET deleted_at=?,update_time=? "
                "WHERE material_uuid=? AND deleted_at IS NULL",
                (now, now, material_uuid),
            )
            connection.execute(
                "UPDATE material SET deleted_at=?,update_time=?,"
                "pose_uuid=NULL,pose_description=NULL,pose_meta_data='{}',"
                "position_x=NULL,position_y=NULL,position_z=NULL,"
                "depth=NULL,length=NULL,width=NULL,"
                "scale_x=NULL,scale_y=NULL,scale_z=NULL,"
                "rotation_x=NULL,rotation_y=NULL,rotation_z=NULL "
                "WHERE uuid=? AND deleted_at IS NULL",
                (now, now, material_uuid),
            )
            # 整树软删除即退出有效库存：状态同步收敛为 discarded（并递增版本），
            # 公开实例列表与后续预留释放都不会再把它当作在库物料
            retire_deleted_material_inventory(
                connection, material_uuids=(material_uuid,)
            )
        return ordered

    def _refill_lock_keys(
        self,
        connection: sqlite3.Connection,
        *,
        material_uuid: str,
        mount_material_uuid: str,
        site_uuid: Any,
        site: Any = None,
    ) -> dict[str, tuple[str, str, str]]:
        """解析补液必须持有的物料锁与当前占位库位锁。"""

        target = connection.execute(
            "SELECT uuid FROM material WHERE uuid=? AND deleted_at IS NULL",
            (material_uuid,),
        ).fetchone()
        if target is None:
            raise MaterialIOError("material_not_found", "Material not found")
        mount = connection.execute(
            "SELECT uuid FROM material WHERE uuid=? AND deleted_at IS NULL",
            (mount_material_uuid,),
        ).fetchone()
        if mount is None:
            raise MaterialIOError("mount_not_found", "Mount material not found")
        occupying = connection.execute(
            "SELECT uuid,material_uuid,name FROM site "
            "WHERE occupied_material_uuid=? AND deleted_at IS NULL",
            (material_uuid,),
        ).fetchall()
        if len(occupying) != 1:
            raise MaterialIOError(
                "site_not_found", "Material is not occupying a unique mount site"
            )
        owner_uuid = str(occupying[0]["material_uuid"])
        occupying_site = str(occupying[0]["uuid"])
        if owner_uuid != mount_material_uuid:
            raise MaterialIOError(
                "site_not_found", "Material is not mounted on the given mount"
            )
        if site_uuid not in (None, "") and str(site_uuid) != occupying_site:
            raise MaterialIOError(
                "site_not_found", "Material is not occupying the given site"
            )
        requested_site_name = str(site or "").strip()
        if (
            requested_site_name
            and str(occupying[0]["name"]).casefold() != requested_site_name.casefold()
        ):
            raise MaterialIOError(
                "invalid_parameter", "site 名称与物料实际库位不一致"
            )
        return {
            material_lock_key(material_uuid): ("material", material_uuid, ""),
            material_lock_key(mount_material_uuid): (
                "material",
                mount_material_uuid,
                "",
            ),
            site_lock_key(owner_uuid, occupying_site): (
                "material_site",
                owner_uuid,
                occupying_site,
            ),
        }

    @staticmethod
    def _check_refill_reagent(
        connection: sqlite3.Connection,
        *,
        material_uuid: str,
        quantity: int | float | None,
        quantity_unit: str | None,
    ) -> tuple[dict[str, Any], int | float | None, dict[str, Any] | None]:
        """只读校验补液目标：唯一试剂、单位一致、加量后不超容量。

        参数：``quantity``/``quantity_unit`` 为空时只校验试剂存在且唯一（准备期
        未提供加量时）。返回：当前试剂行、加量后的余量与容量投影（后两者在
        未提供加量时为 None）。异常：与结算相同的 ``MaterialIOError``。准备期
        预检与结算共用本函数，保证两处口径一致。
        """

        from unilabos.app.scheduler.inventory.backend_contract import (
            BackendContractError,
        )
        from unilabos.app.scheduler.inventory.capacity import (
            LOADING_LIMITS_KEY,
            json_object,
            material_capacity,
            normalize_capacity,
            validate_quantity,
        )

        rows = connection.execute(
            "SELECT * FROM reagent WHERE material_uuid=? AND deleted_at IS NULL",
            (material_uuid,),
        ).fetchall()
        if not rows:
            raise MaterialIOError(
                "reagent_not_found", "Material has no reagent to refill"
            )
        if len(rows) != 1:
            raise MaterialIOError(
                "reagent_conflict", "Material has more than one reagent"
            )
        current = dict(rows[0])
        if quantity is None or quantity_unit is None:
            return current, None, None
        if str(current["quantity_unit"]).lower() != str(quantity_unit).lower():
            raise MaterialIOError(
                "invalid_parameter",
                "reagent.quantity_unit must match the existing reagent",
            )
        new_quantity = float(current["quantity"]) + float(quantity)
        if new_quantity == int(new_quantity) and abs(new_quantity) < 2**53:
            new_quantity = int(new_quantity)
        metadata = json_object(current.get("meta_data"))
        try:
            projection = material_capacity(connection, material_uuid)
            validate_quantity(
                new_quantity,
                quantity_unit,
                normalize_capacity(metadata.get(LOADING_LIMITS_KEY)),
                rated_capacity=projection["rated_capacity"],
                configured_capacity=projection["configured_capacity"],
                context={**current, "meta_data": metadata},
            )
        except BackendContractError as error:
            raise _material_io_error_from_reagent(error) from error
        return current, new_quantity, projection

    def _increment_existing_reagent(
        self,
        connection: sqlite3.Connection,
        *,
        material_uuid: str,
        quantity: int | float,
        quantity_unit: str,
    ) -> dict[str, Any]:
        """在当前事务内给已有试剂加量，并写入 add 台账。"""

        from unilabos.app.scheduler.inventory.backend_contract import (
            BackendContractError,
        )
        from unilabos.app.scheduler.inventory.capacity import json_object
        from unilabos.app.scheduler.inventory.reagent_contract import (
            BackendReagentService,
            _reagent_history_state,
        )
        from unilabos.app.scheduler.inventory.workflow_quantity import (
            WorkflowQuantityReservationError,
            assert_workflow_quantity_mutation_allowed,
        )

        current, new_quantity, projection = self._check_refill_reagent(
            connection,
            material_uuid=material_uuid,
            quantity=quantity,
            quantity_unit=quantity_unit,
        )
        metadata = json_object(current.get("meta_data"))
        context = {**current, "meta_data": metadata}
        service = BackendReagentService(self.store)
        now = _now()
        revision = int(current["revision"]) + 1
        try:
            assert_workflow_quantity_mutation_allowed(
                connection,
                inventory_type="reagent",
                inventory_uuid=str(current["uuid"]),
                quantity=new_quantity,
                current_unit=str(current["quantity_unit"]),
                next_unit=quantity_unit,
            )
            cursor = connection.execute(
                """UPDATE container_content SET update_time=?,quantity=?,revision=?
                WHERE uuid=? AND kind='reagent' AND deleted_at IS NULL AND revision=?""",
                (
                    now,
                    new_quantity,
                    revision,
                    current["uuid"],
                    current["revision"],
                ),
            )
            if cursor.rowcount != 1:
                raise MaterialIOError(
                    "reagent_conflict", "reagent revision has changed"
                )
            service._append_history(
                connection,
                material_uuid=material_uuid,
                reagent_uuid=str(current["uuid"]),
                event_type="add",
                quantity_delta=float(quantity),
                quantity_unit=str(current["quantity_unit"]),
                revision=revision,
                values={},
                recorded_at=now,
                previous=_reagent_history_state(context, projection["capacity"]),
            )
        except BackendContractError as error:
            raise _material_io_error_from_reagent(error) from error
        except WorkflowQuantityReservationError as error:
            raise MaterialIOError("reagent_conflict", str(error)) from error
        info = connection.execute(
            "SELECT uuid,name,cas,physical_state FROM reagent_info "
            "WHERE uuid=? AND deleted_at IS NULL",
            (current["reagent_info_uuid"],),
        ).fetchone()
        info_row = dict(info) if info is not None else {}
        return {
            "quantity": new_quantity,
            "quantity_unit": current["quantity_unit"],
            "reagent": {
                "uuid": current["uuid"],
                "material_uuid": material_uuid,
                "reagent_info_uuid": current["reagent_info_uuid"],
                "quantity": new_quantity,
                "quantity_unit": current["quantity_unit"],
                "concentration_value": current.get("concentration_value"),
                "concentration_unit": current.get("concentration_unit"),
            },
            "reagent_info": {
                "uuid": (
                    str(info_row["uuid"])
                    if info_row
                    else current["reagent_info_uuid"]
                ),
                "name": info_row.get("name"),
                "cas": info_row.get("cas"),
                "physical_state": info_row.get("physical_state"),
            },
        }

    @staticmethod
    def _replay(
        connection: sqlite3.Connection,
        *,
        workflow_id: str,
        node_id: str,
        attempt: int,
        operation: str,
        request_hash: str,
        expected_effect_uuid: str = "",
    ) -> dict[str, Any] | None:
        existing = connection.execute(
            "SELECT result_json,status FROM processed_command WHERE command_id=?",
            (material_io_command_id(workflow_id, node_id, attempt),),
        ).fetchone()
        if existing is None:
            return None
        if str(existing["status"] or "") != "completed":
            raise MaterialIOError(
                "effect_corrupt", "Material I/O effect result is invalid"
            )
        envelope = _json(existing["result_json"], None)
        if not isinstance(envelope, dict):
            raise MaterialIOError(
                "effect_corrupt", "Material I/O effect result is invalid"
            )
        result = envelope.get("result")
        if (
            str(envelope.get("kind") or "") != MATERIAL_IO_COMMAND_KIND
            or not isinstance(result, dict)
        ):
            raise MaterialIOError(
                "effect_corrupt", "Material I/O effect result is invalid"
            )
        if (
            str(envelope.get("operation") or "") != operation
            or str(envelope.get("request_hash") or "") != request_hash
        ):
            raise MaterialIOError(
                "effect_conflict",
                "Material I/O effect identity was reused with a different request",
            )
        if expected_effect_uuid and str(result.get("effect_uuid") or "") != expected_effect_uuid:
            raise MaterialIOError(
                "effect_conflict",
                "Material I/O replay effect does not match the approved Permit",
            )
        return result

    @staticmethod
    def _record_effect(
        connection: sqlite3.Connection,
        *,
        result: Mapping[str, Any],
        request: Mapping[str, Any],
        workflow_id: str,
        node_id: str,
        attempt: int,
    ) -> None:
        connection.execute(
            """
            INSERT INTO processed_command(
                command_id,result_json,status,processed_at
            ) VALUES (?,?,'completed',?)
            """,
            (
                material_io_command_id(workflow_id, node_id, attempt),
                _dump(
                    _effect_envelope(
                        operation=str(result["operation"]),
                        request_hash=str(result["request_hash"]),
                        effect_uuid=str(result["effect_uuid"]),
                        request=request,
                        result=result,
                    )
                ),
                int(datetime.now(timezone.utc).timestamp() * 1000),
            ),
        )


__all__ = [
    "MATERIAL_IO_COMMAND_KIND",
    "MaterialIOError",
    "MaterialIOService",
    "canonicalize_load_reagent",
    "canonicalize_refill_reagent",
    "material_io_change_set",
    "material_io_command_id",
    "migrate_material_io_schema",
]
