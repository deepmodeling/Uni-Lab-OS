"""库位地址：上层物料的名字翻译成载架上的物理库位。"""
import json
import sqlite3

import pytest

from unilabos.app.scheduler.inventory.site_address import (
    SiteAddressError,
    addresses_of_site,
    list_site_addresses,
    resolve_site_address,
)


class _Store:
    """只有地址解析用到的三列表的最小库存。"""

    def __init__(self, addresses):
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.db.executescript(
            "CREATE TABLE material (uuid TEXT, meta_data TEXT, config TEXT, deleted_at TEXT);"
            "CREATE TABLE site (uuid TEXT, material_uuid TEXT, name TEXT, occupied_material_uuid TEXT, deleted_at TEXT);")
        meta = lambda node: json.dumps({"source": "resource-tree-set", "source_node_id": node})
        self.db.executemany("INSERT INTO material VALUES (?,?,?,NULL)", [
            ("deck", meta("tecan_deck"), json.dumps({"site_addresses": addresses})),
            ("nest", meta("nest_004"), "{}"), ("stack", meta("stack"), "{}")])
        self.db.executemany("INSERT INTO site VALUES (?,?,?,NULL,NULL)", [
            ("s3", "nest", "Nest61mm_Pos:3"), ("s4", "nest", "Nest61mm_Pos:4")])

    def query_all(self, sql, parameters=()):
        return [dict(row) for row in self.db.execute(sql, parameters)]

    def query_one(self, sql, parameters=()):
        rows = self.query_all(sql, parameters)
        return rows[0] if rows else None


ADDRESSES = [{"name": "position_x23_y01", "number": 23, "target": {"node": "nest_004", "site": "Nest61mm_Pos:3"}},
             {"name": "position_x24_y01", "number": 24, "target": {"node": "nest_004", "site": "nest61mm_pos:4"}}]


def test_an_address_resolves_to_the_site_on_the_carrier_and_back():
    store = _Store(ADDRESSES)
    target = resolve_site_address(store, "deck", "POSITION_X23_Y01")
    # 放上去的物料以载架为父物料、占载架的库位；台面只是名字的拥有者。
    assert (target.owner_material_uuid, target.site_uuid, target.site_name) == ("nest", "s3", "Nest61mm_Pos:3")
    assert (target.address_owner_material_uuid, target.address) == ("deck", "position_x23_y01")
    assert [item.site_uuid for item in list_site_addresses(store, "deck")] == ["s3", "s4"]
    assert [(item.address_owner_material_uuid, item.address) for item in addresses_of_site(store, "s4")] == [("deck", "position_x24_y01")]
    assert resolve_site_address(store, "deck", "position_x99_y01") is None     # 不是地址：按普通库位处理
    assert list_site_addresses(store, "stack") == [] and resolve_site_address(store, "stack", "x") is None


@pytest.mark.parametrize("addresses, message", [
    ([{"name": "a", "target": {"node": "gone", "site": "Nest61mm_Pos:3"}}], "指向的物料 gone 在库存里有 0 个"),
    ([{"name": "a", "target": {"node": "nest_004", "site": "missing"}}], "指向的库位 nest_004/missing 在库存里有 0 个"),
    ([{"name": "a", "target": {"node": "nest_004", "site": "Nest61mm_Pos:3"}},
      {"name": "b", "target": {"node": "nest_004", "site": "Nest61mm_Pos:3"}}], "两个地址指向了同一个物理库位"),
    ([{"name": "a", "target": {"node": "nest_004", "site": "Nest61mm_Pos:3"}},
      {"name": "A", "target": {"node": "nest_004", "site": "Nest61mm_Pos:4"}}], "重复"),
    ([{"name": "a"}], "必须带 name 和 target"),
])
def test_a_table_that_disagrees_with_the_inventory_is_an_error_not_a_guess(addresses, message):
    with pytest.raises(SiteAddressError, match=message):
        list_site_addresses(_Store(addresses), "deck")


def test_transfer_source_check_accepts_the_author_address_for_the_actual_carrier_site():
    """搬运来源核对：所属父物料已换成载架、库位名还是上层物料地址时，按地址表认可并保留地址写法；别的名字仍拒绝。"""
    from unilabos.app.scheduler.inventory.site_address import addresses_on_owner
    from unilabos.app.scheduler.service import EdgeScheduler, TransferResourceSetError

    store = _Store(ADDRESSES)
    resolver = lambda name: (lambda a: str(a.site_uuid) if a else None)(addresses_on_owner(store, "nest", name))
    nest = "6f1c0e52-8a0d-4f7e-9a51-2d3c4b5a6e7f"  # 载架 "nest" 的物料 UUID（地址表的小库存里叫 nest）
    contract = {"source_owner_param": "source_owner", "source_site_name_param": "source_site"}
    args = {"source_owner": {"uuid": nest}, "source_site": "position_x23_y01"}
    out = EdgeScheduler._inject_actual_transfer_source(
        args, transfer_contract=contract, source_owner_material_uuid=nest, source_site_uuid="s3",
        source_site_name="Nest61mm_Pos:3", address_site_uuid=resolver)
    # 地址写法原样交给驱动（厂家位号），所属父物料换成实际载架。
    assert out["source_site"] == "position_x23_y01" and out["source_owner"] == {"uuid": nest}
    with pytest.raises(TransferResourceSetError, match="来源库位名称"):
        EdgeScheduler._inject_actual_transfer_source(
            {**args, "source_site": "position_x24_y01"}, transfer_contract=contract, source_owner_material_uuid=nest,
            source_site_uuid="s3", source_site_name="Nest61mm_Pos:3", address_site_uuid=resolver)


def test_carrier_site_lookup_by_name_also_accepts_the_address_written_for_the_driver():
    """库存按“载架 + 名字”找库位：名字是上层物料地址（交给驱动的厂家位号）时按地址表找到同一库位。"""
    from types import SimpleNamespace

    from unilabos.app.scheduler.inventory.station_resource import SqliteStationResourceInventory

    store = _Store(ADDRESSES)
    store.db.executescript("ALTER TABLE site ADD COLUMN allowed_resource_template_uuids TEXT;")
    lookup = lambda name: SqliteStationResourceInventory._site_candidates(
        SimpleNamespace(_store=store), owner_material_uuid="nest", site_uuid="", site_name=name, equivalent_site_uuids=())
    assert [row["uuid"] for row in lookup("Nest61mm_Pos:3")] == ["s3"]        # 物理库位名
    assert [row["uuid"] for row in lookup("position_x23_y01")] == ["s3"]      # 台面地址，指到同一库位
    assert lookup("position_x99_y01") == []                                    # 不是地址：照旧找不到


def test_physical_settlement_accepts_the_address_name_of_its_target_site():
    """结算比对目标库位名：地址写法按地址表指到的正是这个库位才认可。"""
    from unilabos.app.scheduler.inventory.service import _address_names_site

    store = _Store(ADDRESSES)
    assert _address_names_site(store.db, "nest", "position_x23_y01", "s3")
    assert not _address_names_site(store.db, "nest", "position_x24_y01", "s3")   # 地址指向别的库位
    assert not _address_names_site(store.db, "stack", "position_x23_y01", "s3")  # 不在这个载架上


def test_deck_lookup_by_its_own_address_finds_the_carrier_site():
    """按“台面 + 台面地址”找库位（例如库位预留 site:台面/位号）：台面自己没有物理库位，按它的地址表翻到载架上的库位。"""
    from types import SimpleNamespace

    from unilabos.app.scheduler.inventory.station_resource import SqliteStationResourceInventory

    store = _Store(ADDRESSES)
    store.db.executescript("ALTER TABLE site ADD COLUMN allowed_resource_template_uuids TEXT;")
    rows = SqliteStationResourceInventory._site_candidates(
        SimpleNamespace(_store=store), owner_material_uuid="deck", site_uuid="", site_name="position_x24_y01",
        equivalent_site_uuids=())
    assert [(row["uuid"], row["material_uuid"]) for row in rows] == [("s4", "nest")]   # 锁和预留按载架记


def test_address_lookup_resolves_only_the_matching_address():
    """排程每次重排都对每个待排搬运按地址反查；只解析同名地址，不逐条解析整张地址表（否则一次重排几十分钟）。"""
    from unilabos.app.scheduler.inventory.site_address import addresses_on_owner

    broken = ADDRESSES + [{"name": "position_x30_y01", "target": {"node": "gone", "site": "x"}}]
    store = _Store(broken)
    queries = []
    original = store.query_all
    store.query_all = lambda sql, parameters=(): (queries.append(sql), original(sql, parameters))[1]
    assert resolve_site_address(store, "deck", "position_x23_y01").site_uuid == "s3"
    assert addresses_on_owner(store, "nest", "position_x24_y01").site_uuid == "s4"
    assert len(queries) <= 6        # 每次 1 次读地址表 + 同名那条的 2 次解析；不碰坏掉的那条
