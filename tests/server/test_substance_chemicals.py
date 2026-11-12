"""物料内容物引用化学品定义：显式校验、同名沿用、按名称自动匹配、显式解除后不再自动匹配、读取摘要、
向后兼容与 materials.db v2 → v3 → v4。"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import replace
from typing import Any, Optional
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.api_envelope import command_rejected, command_result, error_of, ok_data
from unilabos.client.materials import bind_payload
from unilabos.protocol.base import canonical_hash
from unilabos.protocol.materials import (
    InventoryMutation,
    MaterialAggregateRead,
    MaterialDataDelta,
    MaterialDataWrite,
    MaterialDelta,
    MaterialIdentityWrite,
    MaterialNodeCreate,
    MaterialNodeDelta,
    MaterialSnapshot,
    MaterialSubstance,
    MaterialTreeCreate,
    ResourceTemplateWrite,
)
from unilabos.server.api.materials import install_materials_api
from unilabos.server.database import DATABASE_SPECS
from unilabos.server.database.schema import TableSpec, initialize_database
from unilabos.server.database.tables.materials_migrations import (
    _MATERIAL_SUBSTANCE_V3,
    _MATERIAL_SUBSTANCE_V3_INDEXES,
)
from unilabos.server.services.materials import (
    MaterialConflictError,
    MaterialNoChangeError,
    MaterialValidationError,
    MaterialsService,
)
from unilabos.server.services.materials import core as materials_core
from unilabos.server.services.materials.chemicals import ChemicalService

WATER = {"name": "水", "name_en": "Water", "cas": "7732-18-5", "inchi_key": "XLYOFNOQVPJJNP-UHFFFAOYSA-N",
         "molecular_formula": "H2O", "smiles": "O", "molecular_weight": 18.015, "density_g_per_ml": 1.0,
         "physical_state": "liquid"}
ETHANOL = {"name": "乙醇", "aliases": ["酒精", "EtOH"], "cas": "64-17-5", "molecular_formula": "C2H6O",
           "physical_state": "liquid"}
SUMMARY_KEYS = {"chemical_uuid", "name", "cas", "inchi_key", "molecular_formula", "smiles",
                "molecular_weight", "density_g_per_ml", "physical_state"}


def _mutation(operation: str) -> InventoryMutation:
    command_uuid = str(uuid4())
    return InventoryMutation(
        command_uuid=command_uuid, effect_key=f"{operation}:{command_uuid}", operation=operation
    )


def _substance(name: str, quantity: float = 1.0, **values: Any) -> MaterialSubstance:
    return MaterialSubstance(name=name, quantity=quantity, quantity_unit="mL", **values)


def _node(ref: str, *substances: MaterialSubstance, parent: Optional[str] = None) -> MaterialNodeCreate:
    return MaterialNodeCreate(
        client_ref=ref,
        parent_client_ref=parent,
        identity=MaterialIdentityWrite(
            resource_id=f"{ref}-{uuid4()}", name=ref, resource_type="container",
            class_name="Container", template_name="tube",
        ),
        data=MaterialDataWrite(substances=list(substances)),
    )


def _create(service: MaterialsService, *substances: MaterialSubstance) -> MaterialAggregateRead:
    # 根物料名在未删除的根之间唯一
    tree = MaterialTreeCreate(nodes=[_node(f"tube-{uuid4().hex[:8]}", *substances)])
    return service.create_tree(_mutation("create_material_tree"), tree).data.nodes[0]


def _put(service: MaterialsService, material_uuid: str, *substances: MaterialSubstance) -> MaterialAggregateRead:
    return service.put_data(
        _mutation("put_data"), material_uuid, MaterialDataWrite(substances=list(substances))
    ).data


def _links(aggregate: MaterialAggregateRead) -> list[Optional[str]]:
    return [item.chemical_uuid for item in aggregate.data.substances]


def _unlinked(aggregate: MaterialAggregateRead) -> list[bool]:
    return [item.chemical_unlinked for item in aggregate.data.substances]


def _report(service: MaterialsService, material_uuid: str, *substances: MaterialSubstance) -> None:
    """设备按名称上报（增量，不带引用）。"""

    service.apply_delta(
        _mutation("apply_material_delta"),
        MaterialDelta(root_material_uuid=material_uuid, nodes=[MaterialNodeDelta(
            material_uuid=material_uuid, data=MaterialDataDelta(substances=list(substances)),
        )]),
    )


@pytest.fixture
def stack(tmp_path):
    service = MaterialsService(tmp_path / "materials.db")
    chemicals = ChemicalService(service)  # 与物料共用连接和写锁
    service.put_template(
        _mutation("put_template"),
        ResourceTemplateWrite(template_uuid="tube-template", name="tube", resource_type="container",
                              class_name="Container"),
    )
    try:
        yield service, chemicals
    finally:
        service.close()


# ── 显式引用 ────────────────────────────────────────────────


def test_explicit_link_is_stored_and_read_back_with_a_summary(stack) -> None:
    service, chemicals = stack
    water = chemicals.create_chemical(WATER)

    created = _create(service, _substance("溶剂", chemical_uuid=water["chemical_uuid"]))

    assert _links(created) == [water["chemical_uuid"]]
    assert service.list_substances(created.material.material_uuid)[0].chemical_uuid == water["chemical_uuid"]
    summary = created.data.substances[0].chemical
    assert summary is not None and set(summary.model_dump()) == SUMMARY_KEYS
    assert (summary.name, summary.cas, summary.molecular_weight) == ("水", "7732-18-5", pytest.approx(18.015))


def test_unknown_or_deleted_chemical_is_rejected_as_invalid(stack) -> None:
    service, chemicals = stack
    ethanol = chemicals.create_chemical(ETHANOL)
    chemicals.delete_chemical(ethanol["chemical_uuid"], expected_version=ethanol["version"])
    material = _create(service, _substance("样品"))

    for chemical_uuid in ("no-such-chemical", ethanol["chemical_uuid"]):
        with pytest.raises(MaterialValidationError, match="不存在或已删除"):
            _put(service, material.material.material_uuid, _substance("样品", chemical_uuid=chemical_uuid))
    with pytest.raises(MaterialValidationError, match="不存在或已删除"):
        _create(service, _substance("样品", chemical_uuid="no-such-chemical"))
    assert service.get_material(material.material.material_uuid).data.version == 1

    # 拒绝结果按幂等键保存：同一命令重发得到同样的 422，而不是重新执行
    mutation = _mutation("put_data")
    body = MaterialDataWrite(substances=[_substance("样品", chemical_uuid="no-such-chemical")])
    for _ in range(2):
        with pytest.raises(MaterialValidationError, match="不存在或已删除"):
            service.put_data(mutation, material.material.material_uuid, body)

    app = FastAPI()
    install_materials_api(app, service)
    request = bind_payload(_mutation("put_data"), body)
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/inventory/commands",
            json={**request.model_dump(mode="json"), "target_uuid": material.material.material_uuid},
        )
    # 引用已删除化学品：物料校验失败 1000，说明在命令拒绝结果的 error
    assert "不存在或已删除" in command_rejected(response, 1000)["error"]


def test_existing_link_survives_chemical_deletion(stack) -> None:
    service, chemicals = stack
    water = chemicals.create_chemical(WATER)
    material = _create(service, _substance("溶剂", chemical_uuid=water["chemical_uuid"]))
    material_uuid = material.material.material_uuid
    chemicals.delete_chemical(water["chemical_uuid"], expected_version=water["version"])

    read = service.get_material(material_uuid)
    assert _links(read) == [water["chemical_uuid"]] and read.data.substances[0].chemical is None

    # 读-改-写回传已有引用：不因化学品已删除而失败
    echoed = read.data.substances[0].model_copy(update={"quantity": 2.0})
    assert _links(_put(service, material_uuid, echoed)) == [water["chemical_uuid"]]
    # 按名称上报同样沿用
    assert _links(_put(service, material_uuid, _substance("溶剂", 3.0))) == [water["chemical_uuid"]]


# ── 自动匹配 ────────────────────────────────────────────────


def test_auto_resolve_by_name_english_name_alias_and_cas(stack) -> None:
    service, chemicals = stack
    water = chemicals.create_chemical(WATER)
    ethanol = chemicals.create_chemical(ETHANOL)

    created = _create(
        service,
        _substance("水"), _substance("WATER"), _substance("etoh"), _substance("酒精"),
        _substance("64-17-5"), _substance("未登记的试剂"),
    )

    assert _links(created) == [
        water["chemical_uuid"], water["chemical_uuid"], ethanol["chemical_uuid"],
        ethanol["chemical_uuid"], ethanol["chemical_uuid"], None,
    ]
    assert created.data.substances[-1].chemical is None


def test_ambiguous_or_deleted_matches_leave_the_link_empty(stack) -> None:
    service, chemicals = stack
    chemicals.create_chemical({"name": "缓冲液"})
    chemicals.create_chemical({"name": "PBS", "aliases": ["缓冲液"]})
    acetone = chemicals.create_chemical({"name": "丙酮", "cas": "67-64-1"})
    chemicals.delete_chemical(acetone["chemical_uuid"], expected_version=acetone["version"])
    # 同一化学品经名称与别名两次命中仍算唯一
    methanol = chemicals.create_chemical({"name": "甲醇", "aliases": ["甲醇", "MeOH"]})

    created = _create(service, _substance("缓冲液"), _substance("丙酮"), _substance("67-64-1"),
                      _substance("甲醇"))

    assert _links(created) == [None, None, None, methanol["chemical_uuid"]]


# ── 设备按名称上报：沿用已有引用 ───────────────────────────────


def test_by_name_device_report_carries_the_link_over(stack) -> None:
    service, chemicals = stack
    water = chemicals.create_chemical(WATER)
    material = _create(
        service, _substance("Solvent", 10.0, chemical_uuid=water["chemical_uuid"]), _substance("样品", 1.0)
    )
    material_uuid = material.material.material_uuid

    # 增量：顺序变化、名称大小写不同、不带 chemical_uuid
    service.apply_delta(
        _mutation("apply_material_delta"),
        MaterialDelta(
            root_material_uuid=material_uuid,
            nodes=[MaterialNodeDelta(
                material_uuid=material_uuid,
                data=MaterialDataDelta(substances=[_substance("样品", 1.0), _substance("SOLVENT", 8.0)]),
            )],
        ),
    )
    assert _links(service.get_material(material_uuid)) == [None, water["chemical_uuid"]]

    # 内容相同、只是缺少引用的再次上报不算变化（设备不知道化学品引用）
    with pytest.raises(MaterialNoChangeError):
        service.apply_delta(
            _mutation("apply_material_delta"),
            MaterialDelta(
                root_material_uuid=material_uuid,
                nodes=[MaterialNodeDelta(
                    material_uuid=material_uuid,
                    data=MaterialDataDelta(substances=[_substance("样品", 1.0), _substance("SOLVENT", 8.0)]),
                )],
            ),
        )

    # 整树快照同理：设备侧内容物由名称 / 数量重建，不带 uuid 与引用
    tree = service.get_tree(material_uuid)
    node = tree.nodes[0]

    def observed(*substances: MaterialSubstance) -> MaterialSnapshot:
        data = node.data.model_copy(update={"substances": list(substances)})
        return MaterialSnapshot(root_material_uuid=material_uuid, nodes=[node.model_copy(update={"data": data})])

    assert not service.compare_snapshot(observed(_substance("样品", 1.0), _substance("SOLVENT", 8.0))).changed
    service.apply_snapshot(
        _mutation("apply_material_snapshot"), observed(_substance("solvent", 5.0), _substance("样品", 1.0))
    )
    assert _links(service.get_material(material_uuid)) == [water["chemical_uuid"], None]


# ── 读取摘要、state_hash 与幂等 ───────────────────────────────


def test_tree_read_attaches_summaries_in_one_lookup(stack, monkeypatch) -> None:
    service, chemicals = stack
    water = chemicals.create_chemical(WATER)
    ethanol = chemicals.create_chemical(ETHANOL)
    tree = service.create_tree(
        _mutation("create_material_tree"),
        MaterialTreeCreate(nodes=[
            _node("rack"),
            _node("tube-a", _substance("水"), _substance("乙醇"), parent="rack"),
            _node("tube-b", _substance("water"), parent="rack"),
        ]),
    ).data
    root_uuid = tree.root_material_uuid

    calls: list[set[str]] = []
    original = materials_core.chemical_summaries

    def counting(connection, chemical_uuids):
        calls.append(set(chemical_uuids))
        return original(connection, chemical_uuids)

    monkeypatch.setattr(materials_core, "chemical_summaries", counting)
    read = service.get_tree(root_uuid)

    assert calls == [{water["chemical_uuid"], ethanol["chemical_uuid"]}]
    names = {
        node.material.name: [item.chemical.name if item.chemical else None for item in node.data.substances]
        for node in read.nodes
    }
    assert names == {"rack": [], "tube-a": ["水", "乙醇"], "tube-b": ["水"]}

    app = FastAPI()
    install_materials_api(app, service)
    tube_b = next(node for node in read.nodes if node.material.name == "tube-b")
    with TestClient(app) as client:
        body = ok_data(client.get(f"/api/v1/inventory/instances/{tube_b.material.material_uuid}"))
    substance = body["data"]["substances"][0]
    assert substance["chemical_uuid"] == water["chemical_uuid"]
    assert set(substance["chemical"]) == SUMMARY_KEYS and substance["chemical"]["cas"] == "7732-18-5"


def test_link_is_part_of_state_hash_but_the_summary_is_not(stack) -> None:
    service, chemicals = stack
    water = chemicals.create_chemical(WATER)
    material = _create(service, _substance("溶剂", 2.0))
    material_uuid = material.material.material_uuid
    assert _links(material) == [None]

    mutation = _mutation("put_data")
    body = MaterialDataWrite(substances=[_substance("溶剂", 2.0, chemical_uuid=water["chemical_uuid"])])
    linked = service.put_data(mutation, material_uuid, body)
    assert linked.data.state_hash != material.state_hash  # 只改引用也是状态变化
    assert linked.data.data.state_hash != material.data.state_hash
    with pytest.raises(MaterialNoChangeError):  # 同内容的新命令：无变化
        _put(service, material_uuid, _substance("溶剂", 2.0, chemical_uuid=water["chemical_uuid"]))
    with pytest.raises(MaterialNoChangeError):  # 按名称写回：沿用引用，同样无变化
        _put(service, material_uuid, _substance("溶剂", 2.0))

    # 编辑化学品只改摘要，不改物料 state_hash（设备的 expected_state_hash 不受影响）
    chemicals.update_chemical(water["chemical_uuid"], {"density_g_per_ml": 0.997}, expected_version=water["version"])
    after_edit = service.get_material(material_uuid)
    assert after_edit.state_hash == linked.data.state_hash
    assert after_edit.data.substances[0].chemical.density_g_per_ml == pytest.approx(0.997)

    # 同一命令重放：返回首次结果，不重新解析
    replay = service.put_data(mutation, material_uuid, body)
    assert replay.replayed is True and replay.data == linked.data


def test_resolution_does_not_mutate_the_request(stack) -> None:
    """自动匹配只写落库行；请求对象保持原样，同一请求重放仍命中幂等。"""

    service, chemicals = stack
    water = chemicals.create_chemical(WATER)
    request = MaterialTreeCreate(nodes=[_node("tube", _substance("水"))])
    mutation = _mutation("create_material_tree")

    first = service.create_tree(mutation, request)
    assert _links(first.data.nodes[0]) == [water["chemical_uuid"]]
    assert request.nodes[0].data.substances[0].chemical_uuid is None
    replay = service.create_tree(mutation, request)
    assert replay.replayed is True and replay.data == first.data


# ── 向后兼容：省略 / 显式 null / 升级前的哈希 ────────────────────

FIXED_MATERIAL_UUID = "11111111-1111-4111-8111-111111111111"
#: 引入化学品引用之前的代码（17d380a9）对下面固定场景算出的值。未关联的内容物升级后必须逐字不变：
#: 旧行存的 data state_hash 仍能判无变化，设备缓存的 expected_state_hash 照常有效，旧请求可重放。
PRE_LINK_AGGREGATE_STATE_HASH = "d08cf5f11b4b5e877e861de467c2b8dbd8e2eada5b57a0b5af99725e948e2c73"
PRE_LINK_DATA_STATE_HASH = "54a1dc910f70a8df7581b0563badbe670f969aaaaee9fcf3f3fccddf7f9d42b9"
PRE_LINK_PUT_REQUEST_HASH = "db026bd2f201aa01f91db0d1d01624f51c5a1d09848253966fdd063e2d420bc5"


def _fixed_substances(water: float) -> list[MaterialSubstance]:
    return [
        MaterialSubstance(substance_uuid="s-water", name="Water", quantity=water, quantity_unit="mL"),
        MaterialSubstance(substance_uuid="s-salt", name="NaCl", quantity=2.0, quantity_unit="g",
                          physical_state="solid"),
    ]


def _fixed_tree(*, with_substances: bool = True) -> MaterialTreeCreate:
    return MaterialTreeCreate(nodes=[MaterialNodeCreate(
        client_ref="tube",
        material_uuid=FIXED_MATERIAL_UUID,
        identity=MaterialIdentityWrite(resource_id="tube-fixed", name="tube-fixed", resource_type="container",
                                       class_name="Container", template_name="tube"),
        data=MaterialDataWrite(substances=_fixed_substances(5.0) if with_substances else []),
    )])


def _fixed_put_mutation(payload: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """旧客户端会发的原始请求 JSON（payload 是旧模型的完整 dump，没有 chemical_uuid / chemical）。"""

    return {
        "protocol_version": "materials.v1", "command_uuid": "put-fixed", "effect_key": "put_data:put-fixed",
        "operation": "put_data", "actor_type": "human", "actor_uuid": None, "job_uuid": None,
        "observed_at_ms": 1, "preconditions": [],
        "payload": payload if payload is not None else MaterialDataWrite(
            substances=_fixed_substances(4.0)
        ).model_dump(mode="json", exclude_none=False),
    }


def test_unlinked_substances_keep_pre_link_hashes_and_shape(stack) -> None:
    service, chemicals = stack
    chemicals.create_chemical(ETHANOL)  # 有化学品但不匹配：内容物保持未关联
    created = service.create_tree(_mutation("create_material_tree"), _fixed_tree()).data.nodes[0]

    assert _links(created) == [None, None]
    assert created.state_hash == PRE_LINK_AGGREGATE_STATE_HASH
    assert created.data.state_hash == PRE_LINK_DATA_STATE_HASH
    request = InventoryMutation.model_validate(_fixed_put_mutation())
    body = MaterialDataWrite.model_validate(request.payload)
    assert canonical_hash(MaterialsService._bound_request(request, body)) == PRE_LINK_PUT_REQUEST_HASH
    # 读取形态也与之前一致：未关联的内容物不出现新字段（不认识它们的旧客户端照常解析）
    assert all(set(item) == {"substance_uuid", "name", "quantity", "quantity_unit", "physical_state",
                             "composition", "meta_data"}
               for item in created.data.model_dump(mode="json")["substances"])


@pytest.mark.parametrize("variant", ["old_client", "explicit_null", "explicit_uuid_with_summary"])
def test_payload_forms_pass_the_completeness_check_and_replay(stack, variant) -> None:
    service, chemicals = stack
    water = chemicals.create_chemical(WATER)
    created = service.create_tree(_mutation("create_material_tree"), _fixed_tree()).data.nodes[0]
    assert _links(created) == [water["chemical_uuid"], None]  # "Water" 按英文名自动关联

    payload = MaterialDataWrite(substances=_fixed_substances(4.0)).model_dump(mode="json", exclude_none=False)
    expected = {"old_client": water["chemical_uuid"], "explicit_null": None,
                "explicit_uuid_with_summary": water["chemical_uuid"]}[variant]
    if variant == "explicit_null":
        payload["substances"][0].update({"chemical_uuid": None, "chemical": None})
    elif variant == "explicit_uuid_with_summary":
        payload["substances"][0].update({
            "chemical_uuid": water["chemical_uuid"],
            "chemical": created.data.substances[0].chemical.model_dump(mode="json"),
        })
    app = FastAPI()
    install_materials_api(app, service)
    with TestClient(app) as client:
        url = "/api/v1/inventory/commands"
        first = client.post(url, json={**_fixed_put_mutation(payload), "target_uuid": FIXED_MATERIAL_UUID})
        first_result = command_result(first)
        assert first_result["replayed"] is False
        assert [item.get("chemical_uuid") for item in first_result["data"]["data"]["substances"]] == [expected, None]
        replay = client.post(url, json={**_fixed_put_mutation(payload), "target_uuid": FIXED_MATERIAL_UUID})
        assert command_result(replay)["replayed"] is True

    effect = service.get_effect("put-fixed", "put_data:put-fixed")
    if variant == "old_client":
        # 旧版本存下的同一请求哈希与现在一致：升级前发出的命令升级后重发仍是幂等重放
        assert effect.request_hash == PRE_LINK_PUT_REQUEST_HASH
    else:
        assert effect.request_hash != PRE_LINK_PUT_REQUEST_HASH


def test_omitted_and_explicit_null_are_different_requests(stack) -> None:
    service, chemicals = stack
    chemicals.create_chemical(WATER)
    material_uuid = _create(service, _substance("水")).material.material_uuid
    mutation = _mutation("put_data")
    omitted = MaterialDataWrite(substances=[_substance("水", 2.0)])
    nulled = MaterialDataWrite(substances=[_substance("水", 2.0, chemical_uuid=None)])

    assert "chemical_uuid" not in omitted.model_dump(mode="json")["substances"][0]
    assert nulled.model_dump(mode="json")["substances"][0]["chemical_uuid"] is None
    assert canonical_hash(MaterialsService._bound_request(mutation, omitted)) != canonical_hash(
        MaterialsService._bound_request(mutation, nulled)
    )
    service.put_data(mutation, material_uuid, omitted)
    with pytest.raises(MaterialConflictError, match="another request"):
        service.put_data(mutation, material_uuid, nulled)
    assert service.put_data(mutation, material_uuid, omitted).replayed is True


def test_explicit_null_unlinks_without_auto_resolve(stack) -> None:
    service, chemicals = stack
    water = chemicals.create_chemical(WATER)
    material = _create(service, _substance("水", 1.0))
    material_uuid = material.material.material_uuid
    assert _links(material) == [water["chemical_uuid"]]

    unlinked = _put(service, material_uuid, _substance("水", 1.0, chemical_uuid=None))
    assert _links(unlinked) == [None]  # 同名化学品仍在，但本次写入不自动匹配
    assert unlinked.state_hash != material.state_hash
    assert "chemical_uuid" not in unlinked.data.model_dump(mode="json")["substances"][0]

    # 内容不变、只显式改引用的增量 / 快照同样算变化；省略引用的上报不算
    service.apply_delta(
        _mutation("apply_material_delta"),
        MaterialDelta(root_material_uuid=material_uuid, nodes=[MaterialNodeDelta(
            material_uuid=material_uuid,
            data=MaterialDataDelta(substances=[_substance("水", 1.0, chemical_uuid=water["chemical_uuid"])]),
        )]),
    )
    assert _links(service.get_material(material_uuid)) == [water["chemical_uuid"]]
    node = service.get_tree(material_uuid).nodes[0]

    def observed(substance: MaterialSubstance) -> MaterialSnapshot:
        data = node.data.model_copy(update={"substances": [substance]})
        return MaterialSnapshot(root_material_uuid=material_uuid, nodes=[node.model_copy(update={"data": data})])

    assert not service.compare_snapshot(observed(_substance("水", 1.0))).changed
    assert service.compare_snapshot(observed(_substance("水", 1.0, chemical_uuid=None))).changed
    service.apply_snapshot(_mutation("apply_material_snapshot"), observed(_substance("水", 1.0, chemical_uuid=None)))
    assert _links(service.get_material(material_uuid)) == [None]

    # 解除是持久的：之后省略引用的内容变化（设备按名称上报）不再自动匹配
    assert _links(_put(service, material_uuid, _substance("水", 2.0))) == [None]


# ── 显式解除后不再自动匹配（materials.db v4）──────────────────


def test_unlink_survives_device_reports_until_linked_again(stack) -> None:
    service, chemicals = stack
    water = chemicals.create_chemical(WATER)
    material_uuid = _create(service, _substance("水", 1.0), _substance("样品", 1.0)).material.material_uuid
    assert _links(service.get_material(material_uuid)) == [water["chemical_uuid"], None]

    # 界面按所见即所得写回：解除"水"，"样品"本来就没有关联
    unlinked = _put(service, material_uuid, _substance("水", 1.0, chemical_uuid=None),
                    _substance("样品", 1.0, chemical_uuid=None))
    assert _links(unlinked) == [None, None]
    assert _unlinked(unlinked) == [True, False]
    shape = unlinked.data.model_dump(mode="json")["substances"]
    assert shape[0]["chemical_unlinked"] is True and "chemical_uuid" not in shape[0]
    assert "chemical_unlinked" not in shape[1]

    # 设备按名称上报（增量 / 快照，内容变化）：不再自动关联
    _report(service, material_uuid, _substance("水", 3.0), _substance("样品", 1.0))
    node = service.get_tree(material_uuid).nodes[0]
    data = node.data.model_copy(update={"substances": [_substance("水", 2.5), _substance("样品", 1.0)]})
    service.apply_snapshot(
        _mutation("apply_material_snapshot"),
        MaterialSnapshot(root_material_uuid=material_uuid, nodes=[node.model_copy(update={"data": data})]),
    )
    after_reports = service.get_material(material_uuid)
    assert _links(after_reports) == [None, None]
    assert _unlinked(after_reports) == [True, False]

    # 旧客户端读-改-写：回传读取形态（带只读的 chemical_unlinked、不带 chemical_uuid）仍是"省略"
    echoed = after_reports.data.model_dump(mode="json")["substances"]
    echoed[0]["quantity"] = 2.0
    roundtrip = _put(service, material_uuid, *[MaterialSubstance.model_validate(item) for item in echoed])
    assert _links(roundtrip) == [None, None] and _unlinked(roundtrip) == [True, False]

    # 再次显式关联清除"已解除"，之后的上报照常沿用
    relinked = _put(service, material_uuid, _substance("水", 2.0, chemical_uuid=water["chemical_uuid"]),
                    _substance("样品", 1.0))
    assert _links(relinked) == [water["chemical_uuid"], None] and _unlinked(relinked) == [False, False]
    _report(service, material_uuid, _substance("水", 1.5), _substance("样品", 1.0))
    assert _links(service.get_material(material_uuid)) == [water["chemical_uuid"], None]


def test_explicit_null_on_a_never_linked_substance_keeps_auto_matching(stack) -> None:
    """界面回传每份内容物的 null 不会把从未关联过的变成"永不关联"。"""

    service, chemicals = stack
    material_uuid = _create(service, _substance("缓冲液", 1.0)).material.material_uuid
    written = _put(service, material_uuid, _substance("缓冲液", 1.5, chemical_uuid=None))
    assert _links(written) == [None] and _unlinked(written) == [False]

    buffer = chemicals.create_chemical({"name": "缓冲液", "physical_state": "liquid"})
    _report(service, material_uuid, _substance("缓冲液", 2.0))
    assert _links(service.get_material(material_uuid)) == [buffer["chemical_uuid"]]


def test_unlink_follows_the_substance_when_its_position_changes(stack) -> None:
    service, chemicals = stack
    water = chemicals.create_chemical(WATER)
    material_uuid = _create(service, _substance("水", 1.0), _substance("样品", 1.0)).material.material_uuid
    _put(service, material_uuid, _substance("水", 1.0, chemical_uuid=None), _substance("样品", 1.0))

    # 位次变化后按同名找到已解除的那一份
    _report(service, material_uuid, _substance("样品", 1.0), _substance("水", 4.0))
    moved = service.get_material(material_uuid)
    assert _links(moved) == [None, None] and _unlinked(moved) == [False, True]
    assert water["chemical_uuid"] not in _links(moved)


def test_unlinked_flag_is_not_part_of_the_state_hash(stack) -> None:
    service, chemicals = stack
    chemicals.create_chemical(WATER)
    material_uuid = _create(service, _substance("水", 1.0)).material.material_uuid
    unlinked = _put(service, material_uuid, _substance("水", 1.0, chemical_uuid=None))
    data = unlinked.data.model_dump(mode="json")
    assert data["substances"][0]["chemical_unlinked"] is True
    without = {**data, "substances": [{key: value for key, value in item.items() if key != "chemical_unlinked"}
                                      for item in data["substances"]]}
    assert MaterialsService._data_state_hash(data) == MaterialsService._data_state_hash(without)
    assert unlinked.data.state_hash == MaterialsService._data_state_hash(without)

    # 内容不变、只回传读取形态的设备上报不算变化
    node = service.get_tree(material_uuid).nodes[0]
    observed = MaterialSnapshot(root_material_uuid=material_uuid, nodes=[node.model_copy(update={
        "data": node.data.model_copy(update={"substances": [_substance("水", 1.0)]}),
    })])
    assert not service.compare_snapshot(observed).changed


def test_device_snapshot_json_round_trip_keeps_links(stack) -> None:
    """设备快照经 HTTP / HostLink 序列化后仍是"省略"，不会被当成显式解除。"""

    service, chemicals = stack
    water = chemicals.create_chemical(WATER)
    material_uuid = _create(service, _substance("溶剂", 1.0, chemical_uuid=water["chemical_uuid"])).material.material_uuid
    node = service.get_tree(material_uuid).nodes[0]
    data = node.data.model_copy(update={"substances": [_substance("溶剂", 3.0)]})
    snapshot = MaterialSnapshot(root_material_uuid=material_uuid, nodes=[node.model_copy(update={"data": data})])

    wire = MaterialSnapshot.model_validate_json(snapshot.model_dump_json())
    service.apply_snapshot(_mutation("apply_material_snapshot"), wire)

    assert _links(service.get_material(material_uuid)) == [water["chemical_uuid"]]


# ── materials.db v2 → v3 ─────────────────────────────────────

#: v2 冻结的 material_substance（没有 chemical_uuid）；与 v2 的结构指纹逐字一致。
MATERIAL_SUBSTANCE_V2 = TableSpec(
    "material_substance",
    """
        CREATE TABLE IF NOT EXISTS material_substance (
            substance_uuid TEXT PRIMARY KEY CHECK (TRIM(substance_uuid) <> ''),
            material_uuid TEXT NOT NULL,
            ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
            name TEXT NOT NULL CHECK (TRIM(name) <> ''),
            quantity REAL NOT NULL CHECK (quantity >= 0),
            quantity_unit TEXT NOT NULL CHECK (TRIM(quantity_unit) <> ''),
            physical_state TEXT NOT NULL DEFAULT 'liquid' CHECK (
                physical_state IN ('liquid','solid','gas','unknown')
            ),
            composition_json TEXT NOT NULL DEFAULT '[]' CHECK (
                json_valid(composition_json)
                AND json_type(composition_json) = 'array'
            ),
            meta_data_json TEXT NOT NULL DEFAULT '{}' CHECK (
                json_valid(meta_data_json) AND json_type(meta_data_json) = 'object'
            ),
            content_version INTEGER NOT NULL CHECK (content_version > 0),
            observed_at_ms INTEGER NOT NULL CHECK (observed_at_ms >= 0),
            updated_at_ms INTEGER NOT NULL CHECK (updated_at_ms >= observed_at_ms),
            version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
            UNIQUE(material_uuid, ordinal),
            FOREIGN KEY(material_uuid) REFERENCES material_data(material_uuid)
                ON DELETE CASCADE
        )
        """,
    (
        """
            CREATE INDEX IF NOT EXISTS idx_material_substance_name
            ON material_substance(LOWER(name), quantity_unit, material_uuid)
            """,
    ),
)
MATERIALS_V2_CHECKSUM = "40eaebcd605c6eb334dcfb8aa548370386e6f1575c048f464f824cb6ea306e63"

#: v5 新增的表（物理动作占用与结算回执）；v2 / v3 / v4 的库里都没有。
MATERIALS_V5_TABLES = frozenset({"material_execution", "material_execution_hold"})


def _published_materials_spec(version: int, material_substance: TableSpec):
    """按当前声明还原已发布的 v2 / v3：换回当时的 material_substance，去掉之后才新增的表。"""

    from tests.server.materials_published_specs import materials_spec_v5

    spec = materials_spec_v5()
    return replace(
        spec,
        tables=tuple(
            material_substance if table.name == "material_substance" else table
            for table in spec.tables
            if table.name not in MATERIALS_V5_TABLES
        ),
        schema_version=version,
        migrations=spec.migrations[: version - 1],
    )


def _without_v5_execution_guard(service: MaterialsService) -> None:
    """用旧版本写入语义构造旧库：v5 之前没有物理占用守卫（也没有 material_execution_hold 表）。"""

    service.executions.guard_mutation = lambda *_: None


def _ddl(path) -> dict[tuple[str, str], str]:
    """库内全部表 / 索引 / 触发器的建表语句（含 CHECK）；忽略空白、引号与 IF NOT EXISTS。"""

    connection = sqlite3.connect(path)
    try:
        rows = connection.execute(
            "SELECT type, name, sql FROM sqlite_master WHERE sql IS NOT NULL AND name <> 'schema_migration'"
        ).fetchall()
    finally:
        connection.close()
    normalized = {}
    for kind, name, sql in rows:
        text = re.sub(r"\s*([(),])\s*", r"\1", re.sub(r"\s+", " ", sql).strip()).replace('"', "")
        normalized[(kind, name)] = text.replace("IF NOT EXISTS ", "")
    return normalized


def _assert_same_ddl_as_fresh(migrated_path, tmp_path) -> None:
    fresh = tmp_path / "fresh" / "materials.db"
    fresh.parent.mkdir()
    initialize_database(fresh, DATABASE_SPECS["materials"]).close()
    assert _ddl(migrated_path) == _ddl(fresh)


def test_v2_materials_database_migrates_to_latest_keeping_substances(tmp_path) -> None:
    spec = DATABASE_SPECS["materials"]
    v2 = _published_materials_spec(2, MATERIAL_SUBSTANCE_V2)
    assert v2.checksum == MATERIALS_V2_CHECKSUM  # 夹具确实是发布过的 v2 结构
    path = tmp_path / "materials.db"

    connection = initialize_database(path, v2)
    try:
        legacy = MaterialsService(connection)
        _without_v5_execution_guard(legacy)
        legacy.put_template(
            _mutation("put_template"),
            ResourceTemplateWrite(template_uuid="tube-template", name="tube", resource_type="container",
                                  class_name="Container"),
        )
        material_uuid = legacy.create_tree(
            _mutation("create_material_tree"), _fixed_tree(with_substances=False)
        ).data.root_material_uuid
        # 旧版本写下的内容物行与 data state_hash（v2 表没有 chemical_uuid 列）
        with connection:
            connection.executemany(
                "INSERT INTO material_substance(substance_uuid, material_uuid, ordinal, name, quantity, "
                "quantity_unit, physical_state, composition_json, meta_data_json, content_version, "
                "observed_at_ms, updated_at_ms, version) VALUES (?, ?, ?, ?, ?, ?, ?, '[]', '{}', 1, 1, 1, 1)",
                [("s-water", material_uuid, 0, "Water", 5.0, "mL", "liquid"),
                 ("s-salt", material_uuid, 1, "NaCl", 2.0, "g", "solid")],
            )
            connection.execute(
                "UPDATE material_data SET state_hash = ? WHERE material_uuid = ?",
                (PRE_LINK_DATA_STATE_HASH, material_uuid),
            )
    finally:
        connection.close()

    service = MaterialsService(path)
    try:
        migrated = service.get_material(material_uuid)
        assert [(item.substance_uuid, item.name, item.quantity, item.chemical_uuid)
                for item in migrated.data.substances] == [
            ("s-water", "Water", 5.0, None), ("s-salt", "NaCl", 2.0, None),
        ]
        # 升级前后 state_hash 不变：设备缓存的 expected_state_hash 照常可用，旧行的 data 哈希仍能判无变化
        assert migrated.state_hash == PRE_LINK_AGGREGATE_STATE_HASH
        assert migrated.data.state_hash == PRE_LINK_DATA_STATE_HASH
        # 与写下该 data state_hash 的建树请求同一内容（建树按请求体算 data 哈希）
        same = MaterialDataWrite(substances=_fixed_substances(5.0))
        with pytest.raises(MaterialNoChangeError):
            service.put_data(_mutation("put_data"), material_uuid, same)
        # 迁移后的库照常解析引用：按名称匹配新登记的化学品
        water = ChemicalService(service).create_chemical(WATER)
        updated = service.put_data(_mutation("put_data"), material_uuid, same).data
        assert _links(updated) == [water["chemical_uuid"], None]
        assert updated.data.substances[0].substance_uuid == "s-water"
        assert updated.state_hash != PRE_LINK_AGGREGATE_STATE_HASH
    finally:
        service.close()

    peek = sqlite3.connect(path)
    try:
        assert peek.execute("PRAGMA user_version").fetchone()[0] == spec.schema_version == 7
        assert peek.execute("SELECT checksum FROM schema_identity").fetchone()[0] == spec.checksum
        history = [tuple(row) for row in peek.execute("SELECT version, description FROM schema_migration")]
        assert [version for version, _ in history] == [3, 4, 5, 6, 7]
        indexes = {row[0] for row in peek.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'material_substance'"
        )}
        assert {"idx_material_substance_name", "idx_material_substance_chemical"} <= indexes
        columns = [row[1] for row in peek.execute("PRAGMA table_info(material_substance)")]
        assert columns.index("chemical_uuid") == columns.index("name") + 1
        assert columns.index("chemical_unlinked") == columns.index("chemical_uuid") + 1
    finally:
        peek.close()
    assert list(tmp_path.glob("materials.db.pre-v*-*.bak"))
    _assert_same_ddl_as_fresh(path, tmp_path)  # 迁移终点与新建库逐条 DDL 一致（含 CHECK）


# ── materials.db v3 → v4（→ 最新）─────────────────────────────

#: v3 发布的结构指纹（tests/server/test_database_schema_versions.py 曾经固定的值）。
MATERIALS_V3_CHECKSUM = "cc857e09f440d87eaab792134ff8b0d3a5501f76ad59915bddeb322505ce1300"


def test_v3_materials_database_migrates_to_v4_keeping_links(tmp_path) -> None:
    spec = DATABASE_SPECS["materials"]
    v3_table = TableSpec("material_substance", _MATERIAL_SUBSTANCE_V3, _MATERIAL_SUBSTANCE_V3_INDEXES)
    v3 = _published_materials_spec(3, v3_table)
    assert v3.checksum == MATERIALS_V3_CHECKSUM  # 夹具确实是发布过的 v3 结构
    path = tmp_path / "materials.db"

    connection = initialize_database(path, v3)
    try:
        legacy = MaterialsService(connection)
        _without_v5_execution_guard(legacy)
        legacy.put_template(
            _mutation("put_template"),
            ResourceTemplateWrite(template_uuid="tube-template", name="tube", resource_type="container",
                                  class_name="Container"),
        )
        material_uuid = legacy.create_tree(
            _mutation("create_material_tree"), _fixed_tree(with_substances=False)
        ).data.root_material_uuid
        with connection:
            connection.execute(
                "INSERT INTO chemical(chemical_uuid, name, cas, physical_state, created_at_ms, updated_at_ms) "
                "VALUES ('c-water', '水', '7732-18-5', 'liquid', 1, 1)"
            )
            # v3 写下的内容物行：一份已关联、一份未关联（v3 表没有 chemical_unlinked 列）
            connection.executemany(
                "INSERT INTO material_substance(substance_uuid, material_uuid, ordinal, name, chemical_uuid, "
                "quantity, quantity_unit, physical_state, composition_json, meta_data_json, content_version, "
                "observed_at_ms, updated_at_ms, version) VALUES (?, ?, ?, ?, ?, ?, ?, ?, '[]', '{}', 1, 1, 1, 1)",
                [("s-water", material_uuid, 0, "Water", "c-water", 5.0, "mL", "liquid"),
                 ("s-salt", material_uuid, 1, "NaCl", None, 2.0, "g", "solid")],
            )
    finally:
        connection.close()

    service = MaterialsService(path)
    try:
        migrated = service.get_material(material_uuid)
        assert [(item.substance_uuid, item.chemical_uuid, item.chemical_unlinked)
                for item in migrated.data.substances] == [
            ("s-water", "c-water", False), ("s-salt", None, False),
        ]
        # 迁移后的库照常记录解除
        unlinked = _put(service, material_uuid, _substance("Water", 5.0, chemical_uuid=None))
        assert _unlinked(unlinked) == [True]
    finally:
        service.close()

    peek = sqlite3.connect(path)
    try:
        assert peek.execute("PRAGMA user_version").fetchone()[0] == spec.schema_version == 7
        assert peek.execute("SELECT checksum FROM schema_identity").fetchone()[0] == spec.checksum
        assert [row[0] for row in peek.execute("SELECT version FROM schema_migration")] == [4, 5, 6, 7]
        with pytest.raises(sqlite3.IntegrityError):
            peek.execute("UPDATE material_substance SET chemical_unlinked = 1, chemical_uuid = 'c-water'")
    finally:
        peek.close()
    _assert_same_ddl_as_fresh(path, tmp_path)
