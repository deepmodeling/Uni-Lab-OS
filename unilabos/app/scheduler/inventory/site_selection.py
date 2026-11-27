"""任务准入阶段的命名库位组与人类友好库位引用解析。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from typing import Any
from uuid import UUID

from unilabos.app.scheduler.inventory.resource_reference import (
    build_inventory_resource_reference_resolver,
)
from unilabos.app.scheduler.inventory.store import InventoryStore


class InventorySiteSelectionError(RuntimeError):
    """库存无法把任务库位声明唯一冻结为当前部署代际。"""


def build_inventory_site_selection_resolver(
    inventory_store: InventoryStore,
) -> Callable[[Mapping[str, Any]], Mapping[str, Any]]:
    """构造只读的 Task 库位选择冻结端口。

    参数：``inventory_store`` 是本地唯一库存权威。返回：接收逻辑组键或精确人类
    引用并返回有序 Site UUID 与部署指纹的函数。异常：参数类型错误立即抛出；
    解析函数遇到不存在、歧义、跨父资源或组外引用时抛
    ``InventorySiteSelectionError``，不会读取占用状态作永久决定。
    """

    if not isinstance(inventory_store, InventoryStore):
        raise TypeError("inventory_store 必须是 InventoryStore")
    resource_resolver = build_inventory_resource_reference_resolver(inventory_store)

    def resolve_site_selection(request: Mapping[str, Any]) -> Mapping[str, Any]:
        """按一个已经规范化的任务选择请求冻结候选库位身份。"""

        if not isinstance(request, Mapping) or request.get("version") != 1:
            raise InventorySiteSelectionError("库位选择请求版本非法")
        owner_uuid = _uuid(
            request.get("owner_material_uuid"),
            field="owner_material_uuid",
        )
        group_key = _optional_text(request.get("group_key"), field="group_key")
        exact_reference = _optional_text(
            request.get("exact_site_reference"),
            field="exact_site_reference",
        )
        if not group_key and not exact_reference:
            raise InventorySiteSelectionError("必须提供命名库位组或精确库位")
        try:
            owner = inventory_store.query_one(
                "SELECT uuid FROM material WHERE uuid=? AND deleted_at IS NULL",
                (owner_uuid,),
            )
            if owner is None:
                raise InventorySiteSelectionError("目标库位所属资源不存在")
            rows = inventory_store.query_all(
                "SELECT uuid,material_uuid,name,sort_order,meta_data,"
                "allowed_resource_template_uuids "
                "FROM site WHERE material_uuid=? AND deleted_at IS NULL "
                "ORDER BY sort_order ASC,create_time ASC,uuid ASC",
                (owner_uuid,),
            )
        except sqlite3.Error as error:
            raise InventorySiteSelectionError("库存权威读取库位失败") from error

        group_rows: list[Mapping[str, Any]] = []
        if group_key:
            group_rows = [row for row in rows if group_key in _site_groups(row)]
            if not group_rows:
                raise InventorySiteSelectionError(
                    f"父资源 {owner_uuid} 下不存在命名库位组 {group_key}"
                )
            selected = group_rows
        else:
            address_rows = _address_rows(inventory_store, owner_uuid, exact_reference, rows)
            selected = address_rows if address_rows is not None else _exact_site_rows(
                rows,
                reference=exact_reference,
                owner_uuid=owner_uuid,
                resource_resolver=resource_resolver,
            )
        if exact_reference and group_key:
            exact_rows = _exact_site_rows(
                rows,
                reference=exact_reference,
                owner_uuid=owner_uuid,
                resource_resolver=resource_resolver,
            )
            if str(exact_rows[0]["uuid"]) not in {
                str(row["uuid"]) for row in group_rows
            }:
                raise InventorySiteSelectionError("精确库位不属于命名库位组")
            selected = exact_rows
        payload = [
            {
                "uuid": str(row["uuid"]),
                "name": str(row["name"]),
                "sort_order": int(row.get("sort_order") or 0),
                "site_groups": sorted(_site_groups(row)),
                "allowed_resource_template_uuids": _json_value(
                    row.get("allowed_resource_template_uuids"),
                    field="allowed_resource_template_uuids",
                ),
            }
            for row in selected
        ]
        fingerprint_rows = group_rows or selected
        fingerprint_payload = [
            {
                "uuid": str(row["uuid"]),
                "name": str(row["name"]),
                "sort_order": int(row.get("sort_order") or 0),
                "site_groups": sorted(_site_groups(row)),
                "allowed_resource_template_uuids": _json_value(
                    row.get("allowed_resource_template_uuids"),
                    field="allowed_resource_template_uuids",
                ),
            }
            for row in fingerprint_rows
        ]
        fingerprint_bytes = json.dumps(
            {
                "version": 1,
                "owner_material_uuid": owner_uuid,
                "group_key": group_key,
                "sites": fingerprint_payload,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return {
            "site_uuids": [item["uuid"] for item in payload],
            # 库位所属物料：精确引用是地址时，库位在载架上，不在声明的父资源上。
            "owner_material_uuids": [str(row["material_uuid"]) for row in selected],
            "fingerprint": "sha256:" + hashlib.sha256(fingerprint_bytes).hexdigest(),
        }

    return resolve_site_selection


def _address_rows(
    inventory_store: InventoryStore,
    owner_uuid: str,
    reference: str,
    rows: Sequence[Mapping[str, Any]],
) -> list[Mapping[str, Any]] | None:
    """精确引用是上层物料的库位地址（``config.site_addresses``）时，给出它指向的载架库位行；否则 ``None``。

    上层物料自己有同名库位时以库位为准（地址只是名字，不覆盖真实库位）。
    """

    if not reference or any(str(row.get("name") or "").casefold() == reference.casefold() for row in rows):
        return None
    from unilabos.app.scheduler.inventory.site_address import resolve_site_address

    try:
        target = resolve_site_address(inventory_store, owner_uuid, reference)
    except Exception as error:  # noqa: BLE001 - 地址表与库存不一致按库存引用失败处理
        raise InventorySiteSelectionError(f"库位地址解析失败：{reference}") from error
    if target is None:
        return None
    try:
        found = inventory_store.query_all(
            "SELECT uuid,material_uuid,name,sort_order,meta_data,"
            "allowed_resource_template_uuids "
            "FROM site WHERE uuid=? AND deleted_at IS NULL",
            (target.site_uuid,),
        )
    except sqlite3.Error as error:
        raise InventorySiteSelectionError("库存权威读取库位失败") from error
    if len(found) != 1:
        raise InventorySiteSelectionError(f"库位地址 {reference} 指向的库位不存在")
    return list(found)


def _exact_site_rows(
    rows: Sequence[Mapping[str, Any]],
    *,
    reference: str,
    owner_uuid: str,
    resource_resolver: Any,
) -> list[Mapping[str, Any]]:
    """在已限定父资源的库位行中解析 UUID、名称或 ``父资源.库位``。"""

    try:
        site_uuid = str(UUID(reference))
    except (AttributeError, TypeError, ValueError):
        site_uuid = ""
    if site_uuid:
        matches = [row for row in rows if str(row.get("uuid") or "") == site_uuid]
    else:
        site_name = reference
        if "." in reference:
            parent_reference, site_name = reference.rsplit(".", 1)
            if not parent_reference or not site_name:
                raise InventorySiteSelectionError("设备.库位 引用格式非法")
            try:
                parent = resource_resolver(parent_reference)
            except Exception as error:  # noqa: BLE001 - 统一转换为库存引用失败
                raise InventorySiteSelectionError("库位父资源引用解析失败") from error
            if not isinstance(parent, Mapping):
                raise InventorySiteSelectionError("库位父资源引用不存在")
            if str(parent.get("uuid") or "") != owner_uuid:
                raise InventorySiteSelectionError("精确库位引用不属于声明的父资源")
        matches = [
            row
            for row in rows
            if str(row.get("name") or "").casefold() == site_name.casefold()
        ]
    if len(matches) != 1:
        raise InventorySiteSelectionError("精确库位引用不存在或不唯一")
    return matches


def _site_groups(row: Mapping[str, Any]) -> set[str]:
    """从既有 Site ``meta_data.unilab.site_groups`` 读取闭合组键集合。"""

    metadata = _json_value(row.get("meta_data"), field="site.meta_data")
    if not isinstance(metadata, Mapping):
        raise InventorySiteSelectionError("库位 meta_data 必须是对象")
    unilab = metadata.get("unilab")
    if unilab is None:
        return set()
    if not isinstance(unilab, Mapping):
        raise InventorySiteSelectionError("库位 meta_data.unilab 必须是对象")
    raw_groups = unilab.get("site_groups", [])
    if not isinstance(raw_groups, list) or any(
        not isinstance(value, str) or not value.strip() or value != value.strip()
        for value in raw_groups
    ):
        raise InventorySiteSelectionError("库位 site_groups 必须是非空字符串数组")
    if len(set(raw_groups)) != len(raw_groups):
        raise InventorySiteSelectionError("库位 site_groups 包含重复组键")
    return set(raw_groups)


def _json_value(value: Any, *, field: str) -> Any:
    """读取 SQLite JSON 文本或已解码值并对损坏内容失败关闭。"""

    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError) as error:
        raise InventorySiteSelectionError(f"{field} 不是合法 JSON") from error


def _uuid(value: Any, *, field: str) -> str:
    """规范一个必填 UUID 字段。"""

    try:
        return str(UUID(str(value)))
    except (AttributeError, TypeError, ValueError) as error:
        raise InventorySiteSelectionError(f"{field} 不是合法 UUID") from error


def _optional_text(value: Any, *, field: str) -> str:
    """规范可空、不可带首尾空白的文本字段。"""

    if value in (None, ""):
        return ""
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise InventorySiteSelectionError(f"{field} 必须是规范非空字符串")
    return value


__all__ = [
    "InventorySiteSelectionError",
    "build_inventory_site_selection_resolver",
]
