"""增量同步协议：设备按 uuid 只报变了的节点 / 段，权威按段合并、乐观锁在节点内。"""

from __future__ import annotations

from uuid import uuid4

import pytest

from tests.api_envelope import error_of, ok_data
from unilabos.protocol.materials import (
    InventoryMutation,
    MaterialDataDelta,
    MaterialDataWrite,
    MaterialDelta,
    MaterialIdentityWrite,
    MaterialNodeCreate,
    MaterialNodeDelta,
    MaterialPosition,
    MaterialSubstance,
    MaterialTreeCreate,
    ResourceTemplateWrite,
    SiteDelta,
)
from unilabos.server.services.materials import (
    MaterialConflictError,
    MaterialNoChangeError,
    MaterialNotFoundError,
    MaterialValidationError,
    MaterialsService,
)


def _mutation(operation: str) -> InventoryMutation:
    command_uuid = str(uuid4())
    return InventoryMutation(
        command_uuid=command_uuid, effect_key=f"{operation}:{command_uuid}", operation=operation
    )


@pytest.fixture
def deck(tmp_path):
    """台面 + 两个孔 + 一块独立的板；台面有两个位点，A1 被板占着。"""
    service = MaterialsService(tmp_path / "materials.db")
    try:
        service.put_template(
            _mutation("put_template"),
            ResourceTemplateWrite(
                template_uuid="deck-template",
                name="delta-deck",
                resource_type="container",
                class_name="Container",
                available_sites=[{"index": 0, "label": "A1"}, {"index": 1, "label": "A2"}],
            ),
        )
        created = service.create_tree(
            _mutation("create_material_tree"),
            MaterialTreeCreate(
                nodes=[
                    MaterialNodeCreate(
                        client_ref="deck",
                        identity=MaterialIdentityWrite(
                            resource_id="deck", name="deck", resource_type="container",
                            class_name="Container", template_name="delta-deck",
                        ),
                    ),
                    MaterialNodeCreate(
                        client_ref="w1", parent_client_ref="deck",
                        identity=MaterialIdentityWrite(
                            resource_id="deck/w1", name="w1", template_name="delta-well"
                        ),
                        data=MaterialDataWrite(data={"volume": 0.0}, sites_initialized=True),
                    ),
                    MaterialNodeCreate(
                        client_ref="w2", parent_client_ref="deck",
                        identity=MaterialIdentityWrite(
                            resource_id="deck/w2", name="w2", template_name="delta-well"
                        ),
                        data=MaterialDataWrite(data={"volume": 0.0}, sites_initialized=True),
                    ),
                ]
            ),
        )
        plate = service.create_tree(
            _mutation("create_material_tree"),
            MaterialTreeCreate(
                nodes=[
                    MaterialNodeCreate(
                        client_ref="plate",
                        identity=MaterialIdentityWrite(
                            resource_id="plate", name="plate", template_name="delta-plate"
                        ),
                        data=MaterialDataWrite(sites_initialized=True),
                    )
                ]
            ),
        )
        nodes = {node.material.name: node for node in created.data.nodes}
        yield service, nodes, plate.data.nodes[0]
    finally:
        service.close()


def test_delta_updates_only_named_sections_and_bumps_versions(deck) -> None:
    service, nodes, _plate = deck
    w1 = nodes["w1"]
    before = service.get_material(w1.material.material_uuid)

    result = service.apply_delta(
        _mutation("apply_material_delta"),
        MaterialDelta(
            root_material_uuid=nodes["deck"].material.material_uuid,
            nodes=[
                MaterialNodeDelta(
                    material_uuid=w1.material.material_uuid,
                    expected_version=before.material.version,
                    data=MaterialDataDelta(
                        data={"volume": 42.0},
                        substances=[MaterialSubstance(name="water", quantity=42.0, quantity_unit="ul")],
                    ),
                )
            ],
        ),
    )

    after = service.get_material(w1.material.material_uuid)
    assert after.data.data == {"volume": 42.0}
    assert [(s.name, s.quantity) for s in after.data.substances] == [("water", 42.0)]
    assert after.data.substances[0].substance_uuid  # 权威发内容物行的身份
    assert after.material.version == before.material.version + 1
    # 没点名的段与其它节点原样不动
    assert after.data.sites_initialized is True
    assert after.position == before.position
    assert service.get_material(nodes["w2"].material.material_uuid).material.version == 1
    assert result.data.applied_material_uuids == [w1.material.material_uuid]
    assert [(a.aggregate_type, a.version) for a in result.affected] == [("material", 2)]
    assert any(row.operation == "apply_delta" for row in service.changes(after_sequence=0, limit=100))


def test_delta_position_only_touches_position_row(deck) -> None:
    service, nodes, _plate = deck
    w2 = nodes["w2"]
    moved = MaterialPosition(position3d_x=1.5, position3d_y=2.5, rotation_z=90.0)
    service.apply_delta(
        _mutation("apply_material_delta"),
        MaterialDelta(
            root_material_uuid=nodes["deck"].material.material_uuid,
            nodes=[MaterialNodeDelta(material_uuid=w2.material.material_uuid, position=moved)],
        ),
    )
    after = service.get_material(w2.material.material_uuid)
    assert after.position.position3d_x == 1.5 and after.position.rotation_z == 90.0
    assert after.position_version == 2
    assert after.data.data == {"volume": 0.0}


def test_delta_site_visibility_and_metadata_but_not_occupancy(deck) -> None:
    """位点增量只管可见性 / 元数据；占用关系与父子绑定，由 move 维护，协议里没有这个字段。"""
    service, nodes, _plate = deck
    deck_node = nodes["deck"]
    a1 = next(site for site in deck_node.sites if site.label == "A1")

    result = service.apply_delta(
        _mutation("apply_material_delta"),
        MaterialDelta(
            root_material_uuid=deck_node.material.material_uuid,
            nodes=[
                MaterialNodeDelta(
                    material_uuid=deck_node.material.material_uuid,
                    sites=[
                        SiteDelta(
                            site_uuid=a1.site_uuid,
                            expected_version=a1.version,
                            visible=False,
                            meta_data={"role": "waste"},
                        )
                    ],
                )
            ],
        ),
    )
    refreshed = {site.label: site for site in service.get_material(deck_node.material.material_uuid).sites}
    assert refreshed["A1"].visible is False and refreshed["A1"].meta_data == {"role": "waste"}
    assert refreshed["A1"].version == a1.version + 1
    assert refreshed["A2"].version == 1
    assert result.data.applied_site_uuids == [a1.site_uuid]
    assert [(a.aggregate_type, a.aggregate_uuid) for a in result.affected] == [("site", a1.site_uuid)]
    # 台面本身没有段变化：不计入 applied_material，也不算 unchanged（位点变了）
    assert result.data.applied_material_uuids == [] and result.data.unchanged_material_uuids == []

    assert "occupied_material_uuid" not in SiteDelta.model_fields
    with pytest.raises(MaterialConflictError, match="version is 2, expected 1"):
        service.apply_delta(
            _mutation("apply_material_delta"),
            MaterialDelta(
                root_material_uuid=deck_node.material.material_uuid,
                nodes=[
                    MaterialNodeDelta(
                        material_uuid=deck_node.material.material_uuid,
                        sites=[SiteDelta(site_uuid=a1.site_uuid, expected_version=1, visible=True)],
                    )
                ],
            ),
        )


def test_delta_rejects_stale_version_wrong_root_and_unknown_node(deck) -> None:
    service, nodes, _plate = deck
    root = nodes["deck"].material.material_uuid
    w1 = nodes["w1"].material.material_uuid

    with pytest.raises(MaterialConflictError, match="version is 1, expected 7"):
        service.apply_delta(
            _mutation("apply_material_delta"),
            MaterialDelta(
                root_material_uuid=root,
                nodes=[MaterialNodeDelta(material_uuid=w1, expected_version=7, position=MaterialPosition())],
            ),
        )
    with pytest.raises(MaterialValidationError, match="is not under root"):
        service.apply_delta(
            _mutation("apply_material_delta"),
            MaterialDelta(
                root_material_uuid=w1,  # 把孔当根，台面不在它下面
                nodes=[MaterialNodeDelta(material_uuid=root, position=MaterialPosition())],
            ),
        )
    with pytest.raises(MaterialNotFoundError):
        service.apply_delta(
            _mutation("apply_material_delta"),
            MaterialDelta(
                root_material_uuid=root,
                nodes=[MaterialNodeDelta(material_uuid=str(uuid4()), position=MaterialPosition())],
            ),
        )
    with pytest.raises(ValueError, match="carries no section"):
        MaterialNodeDelta(material_uuid=w1)


def test_delta_over_http_uses_mutation_payload(deck) -> None:
    """`POST /snapshots/delta`：与其它写请求同一个 InventoryMutation 信封，返回 MutationResult。"""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from unilabos.client.materials import bind_payload
    from unilabos.server.api.materials import install_materials_api

    service, nodes, _plate = deck
    app = FastAPI()
    install_materials_api(app, service)
    w1 = nodes["w1"]
    mutation = bind_payload(
        _mutation("apply_material_delta"),
        MaterialDelta(
            root_material_uuid=nodes["deck"].material.material_uuid,
            nodes=[
                MaterialNodeDelta(
                    material_uuid=w1.material.material_uuid,
                    expected_version=1,
                    data=MaterialDataDelta(data={"volume": 7.0}),
                )
            ],
        ),
    )
    with TestClient(app) as client:
        response = client.post("/api/v1/materials/snapshots/delta", json=mutation.model_dump(mode="json"))
        body = ok_data(response)
        assert body["data"]["applied_material_uuids"] == [w1.material.material_uuid]
        assert body["affected"][0]["version"] == 2
        # 同一 command 重放：幂等返回
        replay = client.post("/api/v1/materials/snapshots/delta", json=mutation.model_dump(mode="json"))
        assert ok_data(replay)["replayed"] is True
    assert service.get_material(w1.material.material_uuid).data.data == {"volume": 7.0}


def test_delta_without_semantic_change_is_no_change(deck) -> None:
    service, nodes, _plate = deck
    w1 = nodes["w1"]
    with pytest.raises(MaterialNoChangeError):
        service.apply_delta(
            _mutation("apply_material_delta"),
            MaterialDelta(
                root_material_uuid=nodes["deck"].material.material_uuid,
                nodes=[
                    MaterialNodeDelta(
                        material_uuid=w1.material.material_uuid,
                        data=MaterialDataDelta(data={"volume": 0.0}, substances=[]),
                        position=w1.position,
                    )
                ],
            ),
        )
    assert service.get_material(w1.material.material_uuid).material.version == 1
