"""注册表模板（``config_info``）不带实例 uuid：同一资源定义每次生成逐字节相同，旧后端仍拿到带 uuid 的旧形状。"""

from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest
from pylabrobot.resources import boekel_15mL_falcon_carrier, eppendorf_96_tiprack_10ul_eptips

from unilabos.client.host.legacy_adaptor.legacy.sync import _legacy_registry_entry
from unilabos.registry.registry import lab_registry
from unilabos.resources.adapters.registry_materials import registry_definition_to_template
from unilabos.resources.resource_tracker import ResourceTreeSet

_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_MODULES = {
    "tiprack": "pylabrobot.resources:eppendorf_96_tiprack_10ul_eptips",
    # 载架带库位（sites），库位上也有实例 uuid
    "carrier": "pylabrobot.resources:boekel_15mL_falcon_carrier",
}


def _templates(factory) -> list[dict[str, Any]]:
    tree = ResourceTreeSet.from_plr_resources([factory(name=factory.__name__)], known_random_uuid=True, old_size=True)
    return tree.dump_templates()[0]


@pytest.mark.parametrize("factory", [eppendorf_96_tiprack_10ul_eptips, boekel_15mL_falcon_carrier])
def test_template_dump_is_identical_across_generations_and_has_no_uuid(factory) -> None:
    first, second = _templates(factory), _templates(factory)
    text = json.dumps(first, sort_keys=True)
    assert text == json.dumps(second, sort_keys=True)
    assert not _UUID.search(text)
    assert all("uuid" not in node and "parent_uuid" not in node and "name" not in node for node in first)
    # 父子关系用父节点 id 表示：根没有父，其余节点的父都在同一棵树里
    ids = {node["id"] for node in first}
    assert first[0]["parent_id"] is None
    assert all(node["parent_id"] in ids for node in first[1:])
    # 实例化物料需要的内容都还在
    assert first[0]["type"] and first[0]["config"]["type"]


def test_carrier_template_keeps_site_definitions_without_instance_identity() -> None:
    sites = _templates(boekel_15mL_falcon_carrier)[0]["sites"]
    assert sites, "载架模板应保留库位定义"
    assert all({"uuid", "material_uuid", "occupied_material_uuid"}.isdisjoint(site) for site in sites)
    assert all("label" in site and "index" in site for site in sites)


def test_registry_generates_uuid_free_config_info_and_a_stable_template_hash(monkeypatch) -> None:
    """注册表两次重新生成 config_info：内容一样，物料权威的模板哈希也一样（不会每次启动都变一版）。"""

    def generate() -> dict[str, list[dict[str, Any]]]:
        entries = {
            rid: {"id": rid, "class": {"type": "pylabrobot", "module": module}, "config_info": []}
            for rid, module in _MODULES.items()
        }
        monkeypatch.setattr(lab_registry, "resource_type_registry", entries)
        executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="RegistryTemplateTest")
        monkeypatch.setattr(lab_registry, "_startup_executor", executor)
        try:
            lab_registry._populate_resource_config_info(config_cache={})  # noqa: SLF001
        finally:
            executor.shutdown(wait=True)
        return {rid: entry["config_info"] for rid, entry in entries.items()}

    first, second = generate(), generate()
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)
    assert all(first[rid] for rid in _MODULES)
    assert not _UUID.search(json.dumps(first))
    for rid, config_info in first.items():
        a = registry_definition_to_template({"id": rid, "config_info": config_info})
        b = registry_definition_to_template({"id": rid, "config_info": second[rid]})
        assert a.model_dump() == b.model_dump()


def test_legacy_upload_restores_stable_uuids_and_parent_links() -> None:
    config_info = _templates(eppendorf_96_tiprack_10ul_eptips)
    entry = {"id": "tiprack_10ul", "config_info": config_info}
    uploaded = _legacy_registry_entry(entry)["config_info"]
    assert uploaded == _legacy_registry_entry(entry)["config_info"]
    by_id = {node["id"]: node for node in uploaded}
    for template, node in zip(config_info, uploaded):
        assert "parent_id" not in node
        assert _UUID.fullmatch(node["uuid"]) and node["name"] == node["uuid"]
        parent_id = template["parent_id"]
        assert node["parent_uuid"] == (by_id[parent_id]["uuid"] if parent_id else None)
    assert len({node["uuid"] for node in uploaded}) == len(uploaded)
    # 另一个模板里同名的节点得到不同的 uuid
    other = _legacy_registry_entry({"id": "tiprack_other", "config_info": config_info})["config_info"]
    assert other[0]["uuid"] != uploaded[0]["uuid"]
    # 旧版本生成、已带 uuid 的定义原样上报
    legacy_nodes = [{"id": "root", "uuid": "11111111-1111-4111-8111-111111111111", "name": "x", "parent_uuid": None}]
    assert _legacy_registry_entry({"id": "old", "config_info": legacy_nodes})["config_info"] == legacy_nodes
