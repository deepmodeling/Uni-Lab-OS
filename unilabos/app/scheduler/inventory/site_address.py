"""库位地址（Site Address）：上层物料拥有的名字，指向下层载架上的物理库位（Site）。

物理库位只有一份，建在实际承载物料的载架上：放上去的物料以载架为父物料，占用记在载架的库位上。
台面、堆栈这类上层物料保留自己的编号（“23 号位”“7 号料架第 4 层”），但它拥有的是**地址**而不是库位：
地址不能被占用，也不能当父物料，只负责把“上层物料 + 地址名”翻译成一个物理库位。

地址表随资源图进入库存，写在上层物料的 ``config.site_addresses`` 里：

    [{"name": "position_x23_y01", "target": {"node": "<载架的资源图业务 ID>", "site": "<载架库位名>"},
      "number": 23, "status": "inferred", "basis": "…"}]

``target.node`` 用资源图业务 ID（``meta_data.source_node_id``）而不是 UUID，和 ``resource_ref`` 的解析口径一致。
这里只读库存、只做翻译；不建库位、不改占用。
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol


class SiteAddressError(RuntimeError):
    """地址表本身有问题，或地址指向的物料、库位在库存里不能唯一确定。"""


@dataclass(frozen=True, slots=True)
class SiteAddressTarget:
    """一个地址指向的物理库位。"""

    address_owner_material_uuid: str
    address: str
    owner_material_uuid: str
    site_uuid: str
    site_name: str


class _Store(Protocol):
    def query_all(self, sql: str, parameters: tuple[Any, ...] = ()) -> list[Mapping[str, Any]]: ...


def _addresses(raw_config: Any, owner: str) -> list[Mapping[str, Any]]:
    """读出上层物料声明的地址表；没有声明返回空表，结构不对时报错而不是当作没有。"""
    config = raw_config if isinstance(raw_config, Mapping) else json.loads(str(raw_config or "{}"))
    declared = config.get("site_addresses")
    if declared is None:
        return []
    if not isinstance(declared, list):
        raise SiteAddressError(f"{owner}: config.site_addresses 必须是数组")
    seen: set[str] = set()
    for entry in declared:
        target = entry.get("target") if isinstance(entry, Mapping) else None
        if (not isinstance(entry, Mapping) or not str(entry.get("name") or "").strip() or not isinstance(target, Mapping)
                or not str(target.get("node") or "").strip() or not str(target.get("site") or "").strip()):
            raise SiteAddressError(f"{owner}: 地址必须带 name 和 target.node、target.site")
        key = str(entry["name"]).strip().casefold()
        if key in seen:
            raise SiteAddressError(f"{owner}: 地址 {entry['name']} 重复")
        seen.add(key)
    return declared


def _resolve_entry(store: _Store, owner_material_uuid: str, entry: Mapping[str, Any]) -> SiteAddressTarget:
    """一条地址 → 物理库位（两次查询）。"""
    node, site_name = str(entry["target"]["node"]).strip(), str(entry["target"]["site"]).strip()
    owners = store.query_all(
        "SELECT uuid FROM material WHERE deleted_at IS NULL "
        "AND json_extract(meta_data, '$.source') = 'resource-tree-set' "
        "AND json_extract(meta_data, '$.source_node_id') = ? ORDER BY uuid LIMIT 2", (node,))
    if len(owners) != 1:
        raise SiteAddressError(f"地址 {entry['name']} 指向的物料 {node} 在库存里有 {len(owners)} 个")
    sites = store.query_all(
        "SELECT uuid, name FROM site WHERE material_uuid=? AND LOWER(name)=LOWER(?) AND deleted_at IS NULL "
        "ORDER BY uuid LIMIT 2", (owners[0]["uuid"], site_name))
    if len(sites) != 1:
        raise SiteAddressError(f"地址 {entry['name']} 指向的库位 {node}/{site_name} 在库存里有 {len(sites)} 个")
    return SiteAddressTarget(
        address_owner_material_uuid=owner_material_uuid, address=str(entry["name"]).strip(),
        owner_material_uuid=str(owners[0]["uuid"]), site_uuid=str(sites[0]["uuid"]), site_name=str(sites[0]["name"]))


def list_site_addresses(store: _Store, owner_material_uuid: str) -> list[SiteAddressTarget]:
    """上层物料的全部地址及各自指向的物理库位，按声明顺序。

    参数：``owner_material_uuid`` 是拥有地址的上层物料。返回：每个地址解析出的物理库位；物料没有地址表时为空。
    异常：地址指向的物料或库位不存在、不唯一时抛 ``SiteAddressError``——地址表与库存不一致不能悄悄跳过。
    """
    rows = store.query_all("SELECT config FROM material WHERE uuid=? AND deleted_at IS NULL", (owner_material_uuid,))
    if not rows:
        raise SiteAddressError(f"地址的拥有者不存在：{owner_material_uuid}")
    resolved = [_resolve_entry(store, owner_material_uuid, entry) for entry in _addresses(rows[0]["config"], owner_material_uuid)]
    targets = [item.site_uuid for item in resolved]
    if len(set(targets)) != len(targets):
        raise SiteAddressError(f"{owner_material_uuid}: 两个地址指向了同一个物理库位")
    return resolved


def resolve_site_address(store: _Store, owner_material_uuid: str, address: str) -> SiteAddressTarget | None:
    """把“上层物料 + 地址名”翻译成物理库位；该物料没有这个地址时返回 ``None``（调用方按普通库位处理）。"""
    wanted = str(address or "").strip().casefold()
    rows = store.query_all("SELECT config FROM material WHERE uuid=? AND deleted_at IS NULL", (owner_material_uuid,))
    if not rows:
        raise SiteAddressError(f"地址的拥有者不存在：{owner_material_uuid}")
    entry = next((e for e in _addresses(rows[0]["config"], owner_material_uuid)
                  if str(e["name"]).strip().casefold() == wanted), None)
    return _resolve_entry(store, owner_material_uuid, entry) if entry is not None else None


def addresses_of_site(store: _Store, site_uuid: str) -> list[SiteAddressTarget]:
    """反查：一个物理库位在哪些上层物料里叫什么（给展示用，比如在载架库位上标出位号）。"""
    owners = store.query_all(
        "SELECT uuid FROM material WHERE deleted_at IS NULL AND json_extract(config, '$.site_addresses') IS NOT NULL ORDER BY uuid")
    return [item for owner in owners for item in list_site_addresses(store, str(owner["uuid"])) if item.site_uuid == site_uuid]


def addresses_on_owner(store: _Store, owner_material_uuid: str, address: str) -> SiteAddressTarget | None:
    """地址名指向的库位恰好在 ``owner_material_uuid``（载架）上时返回它；没有或不唯一时返回 ``None``。

    计划里有时所属物料已换成载架、库位名还是上层物料的地址（两者分别来自不同参数）；按“哪个上层物料的
    同名地址指向这个载架”反查。
    """
    wanted = str(address or "").strip().casefold()
    # 只解析同名地址：排程每次重排都会对每个待排搬运调用这里，逐条解析全部地址（每条两次全表查询）会把重排拖到几十分钟。
    rows = store.query_all(
        "SELECT uuid, config FROM material WHERE deleted_at IS NULL AND json_extract(config, '$.site_addresses') IS NOT NULL "
        "ORDER BY uuid")
    found = []
    for row in rows:
        for entry in _addresses(row["config"], str(row["uuid"])):
            if str(entry["name"]).strip().casefold() != wanted:
                continue
            item = _resolve_entry(store, str(row["uuid"]), entry)
            if item.owner_material_uuid == owner_material_uuid:
                found.append(item)
    return found[0] if len(found) == 1 else None
