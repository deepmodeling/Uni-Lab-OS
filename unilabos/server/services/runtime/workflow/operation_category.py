"""实验操作（ExperimentOperation）的类型与类别：V1 合同在 dev ``runtime.db`` 上的实现。

V1 把类别存在领域包 ``operation_categories.json``；统一分支按约定落 ``runtime.db`` 的
``workflow_operation_category`` 表（v9 迁移），工作流类型与类别是 ``workflow`` 表的两列。

- 三个产品默认类别使用 V1 的固定 UUID；首次读取时补种，删除为软删除，删掉的默认类别
  不会被重新补种（与 V1"文件缺失才投影默认值、写过之后以文件为准"的语义一致）。
- 类别名按 ``casefold`` 唯一；排序值 0..10000；最多 200 个。
- 普通工作流不能挂类别；实验操作可不分类；省略类别时兼容旧标签
  （``device_operation`` 等）推导类别。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional
from uuid import UUID, uuid4

DEVICE_OPERATION_CATEGORY_UUID = "1ade6f36-40a9-58fe-a6c8-c7418e651a49"
INTEGRATED_OPERATION_CATEGORY_UUID = "10997cc6-104e-53c8-b860-1ccc4957f9a0"
MANUAL_OPERATION_CATEGORY_UUID = "1866cd8a-bf4c-5a44-85e0-325cfc7a063c"

DEFAULT_OPERATION_CATEGORIES = (
    {"uuid": DEVICE_OPERATION_CATEGORY_UUID, "name": "设备操作", "sort_order": 10},
    {"uuid": INTEGRATED_OPERATION_CATEGORY_UUID, "name": "集成操作", "sort_order": 20},
    {"uuid": MANUAL_OPERATION_CATEGORY_UUID, "name": "人工操作", "sort_order": 30},
)
_LEGACY_TAG_CATEGORY_UUIDS = {
    "device_operation": DEVICE_OPERATION_CATEGORY_UUID,
    "integrated_operation": INTEGRATED_OPERATION_CATEGORY_UUID,
    "integration_operation": INTEGRATED_OPERATION_CATEGORY_UUID,
    "manual_operation": MANUAL_OPERATION_CATEGORY_UUID,
}

WORKFLOW_TYPE_NORMAL = "normal"
WORKFLOW_TYPE_EXPERIMENT_OPERATION = "experiment_operation"
WORKFLOW_TYPES = frozenset({WORKFLOW_TYPE_NORMAL, WORKFLOW_TYPE_EXPERIMENT_OPERATION})

CATEGORY_LIMIT = 200
CATEGORY_NAME_BYTE_LIMIT = 256

#: 区分"未提供类别"与"显式 null（清空类别）"。
UNSET: Any = object()


class OperationCategoryError(ValueError):
    """类别输入、身份或唯一性错误；``code`` 是工作流错误分类（invalid_input / not_found / conflict）。"""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code
        self.message = message


def normalize_workflow_type(value: Any, *, default: str = WORKFLOW_TYPE_NORMAL) -> str:
    candidate = default if value is None else value
    if not isinstance(candidate, str) or candidate not in WORKFLOW_TYPES:
        raise OperationCategoryError("invalid_input", "workflow_type 只能是 normal 或 experiment_operation")
    return candidate


def legacy_operation_category_uuid(tags: Any) -> Optional[str]:
    """旧版类别标签 → 稳定类别 UUID；未命中返回 ``None``。"""

    if not isinstance(tags, list):
        return None
    for value in tags:
        if isinstance(value, str) and value in _LEGACY_TAG_CATEGORY_UUIDS:
            return _LEGACY_TAG_CATEGORY_UUIDS[value]
    return None


def category_uuid(value: Any) -> str:
    if not isinstance(value, str):
        raise OperationCategoryError("invalid_input", "类别 UUID 必须是字符串")
    try:
        identity = UUID(value)
    except (AttributeError, TypeError, ValueError):
        raise OperationCategoryError("invalid_input", "类别 UUID 格式不正确") from None
    if identity.int == 0:
        raise OperationCategoryError("invalid_input", "类别 UUID 不能为零值")
    return str(identity)


def _category_name(value: Any) -> str:
    if not isinstance(value, str):
        raise OperationCategoryError("invalid_input", "类别名称必须是字符串")
    normalized = value.strip()
    if (
        not normalized
        or len(normalized.encode("utf-8")) > CATEGORY_NAME_BYTE_LIMIT
        or any(ord(character) < 0x20 for character in normalized)
    ):
        raise OperationCategoryError("invalid_input", "类别名称不能为空、过长或包含控制字符")
    return normalized


def _sort_order(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 10_000:
        raise OperationCategoryError("invalid_input", "sort_order 必须是 0..10000 的整数")
    return value


def _row(row: Any) -> Dict[str, Any]:
    return {"uuid": row["uuid"], "name": row["name"], "sort_order": int(row["sort_order"])}


class OperationCategories:
    """``workflow_operation_category`` 表的读写；与工作流共用 runtime.db 的写锁与事务。"""

    def __init__(self, store: Any) -> None:
        self._store = store
        self._seeded = False

    def _ensure_defaults(self) -> None:
        if self._seeded:
            return
        from unilabos.server.services.runtime.workflow.store import utc_now

        now = utc_now()
        with self._store.transaction() as conn:
            for category in DEFAULT_OPERATION_CATEGORIES:
                # 已软删除的默认类别保留删除标记，不会被重新补种
                conn.execute(
                    "INSERT OR IGNORE INTO workflow_operation_category"
                    "(uuid, create_time, update_time, deleted_at, name, sort_order) "
                    "VALUES (?, ?, ?, NULL, ?, ?)",
                    (category["uuid"], now, now, category["name"], category["sort_order"]),
                )
        self._seeded = True

    def list(self) -> List[Dict[str, Any]]:
        self._ensure_defaults()
        with self._store.write_lock:
            rows = self._store.connection.execute(
                "SELECT uuid, name, sort_order FROM workflow_operation_category "
                "WHERE deleted_at IS NULL ORDER BY sort_order, name, uuid"
            ).fetchall()
        return [_row(row) for row in rows]

    def get(self, identity: Any) -> Dict[str, Any]:
        normalized = category_uuid(identity)
        for category in self.list():
            if category["uuid"] == normalized:
                return category
        raise OperationCategoryError("not_found", "实验操作类别不存在或已被删除")

    def exists(self, identity: str) -> bool:
        try:
            self.get(identity)
        except OperationCategoryError:
            return False
        return True

    def _require_unique_name(self, name: str, *, excluding: Optional[str] = None) -> None:
        folded = name.casefold()
        if any(item["uuid"] != excluding and item["name"].casefold() == folded for item in self.list()):
            raise OperationCategoryError("conflict", "同名实验操作类别已存在")

    def create(self, *, name: str, sort_order: int = 100) -> Dict[str, Any]:
        from unilabos.server.services.runtime.workflow.store import utc_now

        normalized_name = _category_name(name)
        normalized_order = _sort_order(sort_order)
        with self._store.write_lock:
            if len(self.list()) >= CATEGORY_LIMIT:
                raise OperationCategoryError("invalid_input", f"实验操作类别最多 {CATEGORY_LIMIT} 个")
            self._require_unique_name(normalized_name)
            identity = str(uuid4())
            now = utc_now()
            with self._store.transaction() as conn:
                conn.execute(
                    "INSERT INTO workflow_operation_category"
                    "(uuid, create_time, update_time, deleted_at, name, sort_order) "
                    "VALUES (?, ?, ?, NULL, ?, ?)",
                    (identity, now, now, normalized_name, normalized_order),
                )
        return {"uuid": identity, "name": normalized_name, "sort_order": normalized_order}

    def update(self, identity: Any, *, name: Any = UNSET, sort_order: Any = UNSET) -> Dict[str, Any]:
        from unilabos.server.services.runtime.workflow.store import utc_now

        if (name is UNSET or name is None) and (sort_order is UNSET or sort_order is None):
            raise OperationCategoryError("invalid_input", "至少提供 name 或 sort_order")
        with self._store.write_lock:
            current = self.get(identity)
            if name is not UNSET and name is not None:
                current["name"] = _category_name(name)
                self._require_unique_name(current["name"], excluding=current["uuid"])
            if sort_order is not UNSET and sort_order is not None:
                current["sort_order"] = _sort_order(sort_order)
            with self._store.transaction() as conn:
                conn.execute(
                    "UPDATE workflow_operation_category SET name = ?, sort_order = ?, update_time = ? "
                    "WHERE uuid = ? AND deleted_at IS NULL",
                    (current["name"], current["sort_order"], utc_now(), current["uuid"]),
                )
        return current

    def delete(self, identity: Any) -> None:
        from unilabos.server.services.runtime.workflow.store import utc_now

        with self._store.write_lock:
            current = self.get(identity)
            referenced = self._store.connection.execute(
                "SELECT 1 FROM workflow WHERE deleted_at IS NULL AND workflow_type = ? "
                "AND operation_category_uuid = ? LIMIT 1",
                (WORKFLOW_TYPE_EXPERIMENT_OPERATION, current["uuid"]),
            ).fetchone()
            if referenced is not None:
                raise OperationCategoryError("conflict", "该类别仍被实验操作引用，请先调整对应实验操作")
            now = utc_now()
            with self._store.transaction() as conn:
                conn.execute(
                    "UPDATE workflow_operation_category SET deleted_at = ?, update_time = ? WHERE uuid = ?",
                    (now, now, current["uuid"]),
                )

    # ── 工作流类型 / 类别 ────────────────────────────────────────────

    def resolve_for_workflow(self, *, workflow_type: str, category: Any, tags: Any) -> Optional[str]:
        """创建 / 更新时的类别裁决：普通工作流不能挂类别；实验操作的类别必须存在。

        ``category`` 为 :data:`UNSET` 时按旧标签推导（仅实验操作）；显式 ``None`` 表示不分类。
        """

        if category is UNSET:
            if workflow_type != WORKFLOW_TYPE_EXPERIMENT_OPERATION:
                return None
            legacy = legacy_operation_category_uuid(tags)
            return legacy if legacy is not None and self.exists(legacy) else None
        if category is None:
            return None
        if workflow_type != WORKFLOW_TYPE_EXPERIMENT_OPERATION:
            raise OperationCategoryError("invalid_input", "普通工作流不能设置实验操作类别")
        identity = category_uuid(category)
        if not self.exists(identity):
            raise OperationCategoryError("invalid_input", "实验操作类别不存在或已被删除")
        return identity

    def workflow_kind(self, workflow_uuid: str) -> tuple[str, Optional[str]]:
        with self._store.write_lock:
            row = self._store.connection.execute(
                "SELECT workflow_type, operation_category_uuid FROM workflow WHERE uuid = ?",
                (workflow_uuid,),
            ).fetchone()
        if row is None:
            return WORKFLOW_TYPE_NORMAL, None
        return str(row["workflow_type"] or WORKFLOW_TYPE_NORMAL), row["operation_category_uuid"]

    def set_workflow_kind(self, workflow_uuid: str, *, workflow_type: str, category: Optional[str]) -> None:
        with self._store.transaction() as conn:
            conn.execute(
                "UPDATE workflow SET workflow_type = ?, operation_category_uuid = ? WHERE uuid = ?",
                (workflow_type, category, workflow_uuid),
            )


__all__ = [
    "DEFAULT_OPERATION_CATEGORIES",
    "DEVICE_OPERATION_CATEGORY_UUID",
    "INTEGRATED_OPERATION_CATEGORY_UUID",
    "MANUAL_OPERATION_CATEGORY_UUID",
    "OperationCategories",
    "OperationCategoryError",
    "UNSET",
    "WORKFLOW_TYPE_EXPERIMENT_OPERATION",
    "WORKFLOW_TYPE_NORMAL",
    "WORKFLOW_TYPES",
    "category_uuid",
    "legacy_operation_category_uuid",
    "normalize_workflow_type",
]
