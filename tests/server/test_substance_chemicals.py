"""物料内容物引用化学品定义：显式校验、同名沿用、按名称自动匹配、读取摘要与 materials.db v2 → v3。"""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from typing import Any, Optional
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from unilabos.client.materials import bind_payload
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
from unilabos.server.services.materials import (
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
        response = client.put(
            f"/api/v1/materials/instances/{material.material.material_uuid}/data",
            json=request.model_dump(mode="json"),
        )
    assert response.status_code == 422 and "不存在或已删除" in response.json()["detail"]


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
        body = client.get(f"/api/v1/materials/instances/{tube_b.material.material_uuid}").json()
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


def test_v2_materials_database_migrates_to_v3_keeping_substances(tmp_path) -> None:
    spec = DATABASE_SPECS["materials"]
    v2 = replace(
        spec,
        tables=tuple(MATERIAL_SUBSTANCE_V2 if table.name == "material_substance" else table for table in spec.tables),
        schema_version=2,
        migrations=spec.migrations[:1],
    )
    assert v2.checksum == MATERIALS_V2_CHECKSUM  # 夹具确实是发布过的 v2 结构
    path = tmp_path / "materials.db"

    connection = initialize_database(path, v2)
    try:
        legacy = MaterialsService(connection)
        legacy.put_template(
            _mutation("put_template"),
            ResourceTemplateWrite(template_uuid="tube-template", name="tube", resource_type="container",
                                  class_name="Container"),
        )
        material_uuid = legacy.create_tree(
            _mutation("create_material_tree"), MaterialTreeCreate(nodes=[_node("tube")])
        ).data.root_material_uuid
        with connection:
            connection.executemany(
                "INSERT INTO material_substance(substance_uuid, material_uuid, ordinal, name, quantity, "
                "quantity_unit, physical_state, composition_json, meta_data_json, content_version, "
                "observed_at_ms, updated_at_ms, version) VALUES (?, ?, ?, ?, ?, ?, ?, '[]', '{}', 1, 1, 1, 1)",
                [("s-water", material_uuid, 0, "Water", 5.0, "mL", "liquid"),
                 ("s-salt", material_uuid, 1, "NaCl", 2.0, "g", "solid")],
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
        # 迁移后的库照常解析引用：按名称匹配新登记的化学品
        water = ChemicalService(service).create_chemical(WATER)
        updated = _put(service, material_uuid, _substance("Water", 4.0), _substance("NaCl", 2.0))
        assert _links(updated) == [water["chemical_uuid"], None]
        assert updated.data.substances[0].substance_uuid == "s-water"
    finally:
        service.close()

    peek = sqlite3.connect(path)
    try:
        assert peek.execute("PRAGMA user_version").fetchone()[0] == spec.schema_version == 3
        assert peek.execute("SELECT checksum FROM schema_identity").fetchone()[0] == spec.checksum
        history = [tuple(row) for row in peek.execute("SELECT version, description FROM schema_migration")]
        assert [version for version, _ in history] == [3]
        indexes = {row[0] for row in peek.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'material_substance'"
        )}
        assert {"idx_material_substance_name", "idx_material_substance_chemical"} <= indexes
        columns = [row[1] for row in peek.execute("PRAGMA table_info(material_substance)")]
        assert columns.index("chemical_uuid") == columns.index("name") + 1
    finally:
        peek.close()
    assert list(tmp_path.glob("materials.db.pre-v3-*.bak"))
