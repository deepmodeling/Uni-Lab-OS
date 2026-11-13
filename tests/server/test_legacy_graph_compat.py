"""旧后端导出图 / 旧示例图只在 legacy 适配层转换；微后端与 Graph Authority 装配只认当前契约。

覆盖旧 Backend ``/edge/material/download`` 与 dev 分支示例图的形状：``class``
无 ``template_name``、根级 ``position``、``pose.position_3d``、``children``、
``source_uuid/target_uuid`` 边、旧 PLR 液体字段、平铺 Site 的 ``occupied_by``，
以及不同父节点下同名子物料。
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from unilabos.server.backend.legacy_adaptor.legacy.graph import (
    legacy_graph_children_index,
    legacy_graph_markers,
    normalize_legacy_graph,
    normalize_legacy_graph_node,
    normalize_legacy_material_nodes,
    upgrade_legacy_graph_payload,
)


def _legacy_node(**overrides):
    node = {
        "uuid": str(uuid4()),
        "parent_uuid": "",
        "id": "host_node",
        "name": "host_node",
        "type": "device",
        "class": "host_node",
        "parent": "",
        "children": [],
        "position": {"x": 1, "y": 2, "z": 3},
        "pose": {
            "layout": "x-y",
            "position": {"x": 1, "y": 2, "z": 3},
            "position_3d": {"x": 4, "y": 5, "z": 6},
            "size": {"width": 0, "height": 0, "depth": 0},
            "scale": {"x": 1, "y": 1, "z": 1},
            "rotation": {"x": 0, "y": 0, "z": 0},
            "extra": None,
            "cross_section_type": "rectangle",
        },
        "description": "",
        "model": None,
        "schema": {},
        "config": {},
        "data": {},
    }
    node.update(overrides)
    return node


# ── 节点转换 ─────────────────────────────────────────────────────


def test_normalize_folds_root_position_position_3d_class_and_children():
    node = normalize_legacy_graph_node(_legacy_node())
    assert "position" not in node and "children" not in node
    assert node["pose"]["position"] == {"x": 1, "y": 2, "z": 3}
    assert node["pose"]["position3d"] == {"x": 4, "y": 5, "z": 6}
    assert "position_3d" not in node["pose"]
    assert node["template_name"] == "host_node"


def test_normalize_uses_root_position_when_pose_has_none():
    node = normalize_legacy_graph_node(_legacy_node(pose={"layout": "x-y"}))
    assert node["pose"]["position"] == {"x": 1, "y": 2, "z": 3}


def test_normalize_rejects_conflicting_root_position():
    node = _legacy_node()
    node["position"] = {"x": 9, "y": 9, "z": 9}
    with pytest.raises(ValueError, match="根字段 position 与 pose.position 冲突"):
        normalize_legacy_graph_node(node)


def test_class_only_fills_template_name_without_overriding():
    upgraded = normalize_legacy_graph_node({"id": "pump", "class": "pump_demo"})
    assert upgraded["template_name"] == "pump_demo"
    kept = normalize_legacy_graph_node(
        {"id": "pump", "class": "pump_demo", "template_name": "pump_v2"}
    )
    assert kept["template_name"] == "pump_v2"
    blank = normalize_legacy_graph_node({"id": "pump", "class": "  "})
    assert "template_name" not in blank


def test_normalize_is_identity_for_current_contract_node():
    node = {
        "id": "pump",
        "uuid": str(uuid4()),
        "type": "device",
        "class": "pump_demo",
        "template_name": "pump_demo",
        "pose": {"position": {"x": 1, "y": 1, "z": 0}, "position3d": {"x": 1, "y": 1, "z": 0}},
        "config": {},
        "data": {},
        "substances": [["water", 10, "ul"]],
    }
    before = dict(node)
    assert normalize_legacy_graph_node(node) == before
    assert legacy_graph_markers({"nodes": [node], "links": []}) == {}
    # 受管进程图 / 前端导出常带空 children 列表：派生字段，不是旧格式
    assert legacy_graph_markers({"nodes": [{**node, "children": []}], "links": []}) == {}
    assert legacy_graph_markers({"nodes": [{**node, "children": ["x"]}], "links": []}) == {
        "children 列表": 1
    }


def test_normalize_legacy_liquid_fields():
    node = normalize_legacy_graph_node(
        _legacy_node(
            type="container",
            data={
                "liquids": [["", None], ["water", 1]],
                "liquid_history": ["water"],
                "pending_liquids": [["water", 1]],
                "max_volume": 100,
            },
        )
    )
    assert node["data"]["liquids"] == [["water", 1, "ul"]]
    assert "liquid_history" not in node["data"]
    assert "pending_liquids" not in node["data"]
    assert node["data"]["max_volume"] == 100

    dict_form = normalize_legacy_graph_node(
        _legacy_node(
            type="container",
            data={"liquids": [{"liquid_type": "methanol", "liquid_volume": 100}]},
        )
    )
    assert dict_form["data"]["liquids"] == [["methanol", 100, "ul"]]

    empty = normalize_legacy_graph_node(
        _legacy_node(type="container", data={"liquids": [["", None]]})
    )
    assert "liquids" not in empty["data"]



def test_dev_era_container_liquid_becomes_substances():
    """FRAMEWORK_GAPS #32：dev 时代容器的 ``data.liquid=[{liquid_type, liquid_volume}]``。

    升级把它转成当前契约的内容物（``data.liquids`` → ``substances``），不能静默丢掉；
    ``data.liquid`` 原样保留，内置协议编译器仍从物理拓扑图读取它。
    """

    legacy_liquid = [
        {"liquid_type": "CH2Cl2", "liquid_volume": 1500.0},
        {"liquid_type": "", "liquid_volume": None},
    ]
    node = normalize_legacy_graph_node(
        _legacy_node(
            id="flask_CH2Cl2",
            name="flask_CH2Cl2",
            type="container",
            config={"max_volume": 2000.0},
            data={"liquid": [dict(item) for item in legacy_liquid]},
        )
    )
    assert node["data"]["liquids"] == [["CH2Cl2", 1500.0, "ul"]]
    assert node["data"]["liquid"] == legacy_liquid

    # 已有当前契约内容物时不覆盖
    current = normalize_legacy_graph_node(
        _legacy_node(
            type="container",
            data={"liquid": [{"liquid_type": "old", "liquid_volume": 1.0}]},
            substances=[["new", 2.0, "ul"]],
        )
    )
    assert "liquids" not in current["data"] and current["substances"] == [["new", 2.0, "ul"]]

    # 只有 data.liquid 这一处旧字段的图同样会被识别并升级，升级后不再报旧字段
    flask = {
        "id": "flask",
        "uuid": str(uuid4()),
        "type": "container",
        "template_name": "container",
        "pose": {"position": {"x": 0, "y": 0, "z": 0}},
        "config": {"max_volume": 1000.0},
        "data": {"liquid": [{"liquid_type": "DMF", "liquid_volume": 800.0}]},
    }
    payload = {"nodes": [flask], "links": []}
    assert legacy_graph_markers(payload) == {"旧液体字段": 1}
    upgraded = upgrade_legacy_graph_payload(payload, source="t", report=lambda _message: None)
    assert upgraded["nodes"][0]["data"]["liquids"] == [["DMF", 800.0, "ul"]]
    assert legacy_graph_markers(upgraded) == {}
    # 空列表不是旧字段
    assert legacy_graph_markers({"nodes": [{**flask, "data": {"liquid": []}}], "links": []}) == {}


def test_occupied_by_resolves_only_current_children():
    deck_uuid, plate_uuid, other_uuid = str(uuid4()), str(uuid4()), str(uuid4())
    deck = _legacy_node(
        uuid=deck_uuid,
        id="deck",
        name="deck",
        type="deck",
        config={
            "sites": [
                {"label": "T1", "position": {"x": 0, "y": 0, "z": 0}, "occupied_by": "plate"},
                {"label": "T2", "position": {"x": 1, "y": 0, "z": 0}, "occupied_by": "stale_plate"},
                {"label": "T3", "position": {"x": 2, "y": 0, "z": 0}, "occupied_by": None},
            ]
        },
    )
    plate = _legacy_node(uuid=plate_uuid, id="plate", name="plate", type="plate", parent="deck", parent_uuid=deck_uuid)
    # 同名物料挂在别的父节点下：不得被误认为占用者
    stale = _legacy_node(uuid=other_uuid, id="stale_plate", name="stale_plate", type="plate", parent="elsewhere", parent_uuid=str(uuid4()))
    nodes = [deck, plate, stale]
    index = legacy_graph_children_index(nodes)
    normalize_legacy_graph_node(deck, children_by_parent=index)
    sites = deck["config"]["sites"]
    assert sites[0]["occupied_material_uuid"] == plate_uuid
    assert "occupied_by" not in sites[0]
    assert "occupied_material_uuid" not in sites[1]
    assert sites[1]["meta_data"]["legacy_fields"]["occupied_by"] == "stale_plate"
    assert "occupied_by" not in sites[2] and "meta_data" not in sites[2]


def test_occupied_by_resolves_by_parent_id_for_uuid_less_owner():
    """草稿图的 owner 还没有 uuid：按 parent id 也能找到子物料。"""
    plate_uuid = str(uuid4())
    deck = {
        "id": "deck",
        "type": "deck",
        "config": {"sites": [{"label": "T1", "occupied_by": "plate"}]},
    }
    plate = {"id": "plate", "uuid": plate_uuid, "type": "plate", "parent": "deck"}
    converted = normalize_legacy_graph({"nodes": [deck, plate]})
    site = converted["nodes"][0]["config"]["sites"][0]
    assert site["occupied_material_uuid"] == plate_uuid


# ── 整图转换与探测 ─────────────────────────────────────────────


def _legacy_export():
    host_uuid, ws_uuid = str(uuid4()), str(uuid4())
    plate_a, plate_b = str(uuid4()), str(uuid4())
    well_a, well_b = str(uuid4()), str(uuid4())
    payload = {
        "nodes": [
            _legacy_node(uuid=host_uuid, children=["ws"]),
            _legacy_node(uuid=ws_uuid, id="ws", name="ws", **{"class": "virtual_workbench"}),
            _legacy_node(uuid=plate_a, id="plate_a", name="plate_a", type="plate", **{"class": "plate"}, parent="ws", parent_uuid=ws_uuid),
            _legacy_node(uuid=plate_b, id="plate_b", name="plate_b", type="plate", **{"class": "plate"}, parent="ws", parent_uuid=ws_uuid),
            # 两块板各有一个 A1 孔：id 相同、uuid 不同、parent 不同
            _legacy_node(uuid=well_a, id="A1", name="A1", type="well", parent="plate_a", parent_uuid=plate_a),
            _legacy_node(uuid=well_b, id="A1", name="A1", type="well", parent="plate_b", parent_uuid=plate_b),
        ],
        # 旧后端边只有 uuid 对与下划线 handle 名
        "edges": [
            {"source_uuid": host_uuid, "target_uuid": ws_uuid, "source_handle": "out", "target_handle": "in"},
            {"source_uuid": host_uuid, "target_uuid": str(uuid4())},
        ],
    }
    return payload, {"host": host_uuid, "ws": ws_uuid, "plate_a": plate_a, "plate_b": plate_b, "well_a": well_a, "well_b": well_b}


def test_normalize_legacy_graph_converts_nodes_and_uuid_edges():
    payload, ids = _legacy_export()
    converted = normalize_legacy_graph(payload)

    assert "edges" not in converted
    assert [n["template_name"] for n in converted["nodes"][:2]] == ["host_node", "virtual_workbench"]
    assert all("position" not in n and "children" not in n for n in converted["nodes"])
    assert converted["nodes"][0]["pose"]["position3d"] == {"x": 4, "y": 5, "z": 6}
    # 端点不在节点集合中的边被丢弃
    assert len(converted["links"]) == 1
    link = converted["links"][0]
    assert link["source"] == "host_node" and link["target"] == "ws"
    assert link["source_uuid"] == ids["host"] and link["target_uuid"] == ids["ws"]
    assert link["sourceHandle"] == "out" and link["targetHandle"] == "in"
    assert "source_handle" not in link
    # 输入不被修改
    assert "edges" in payload and "position" in payload["nodes"][0]


def test_legacy_graph_markers_and_upgrade_report():
    payload, _ids = _legacy_export()
    markers = legacy_graph_markers(payload)
    assert markers["class 无 template_name"] == 6
    assert markers["根级 position"] == 6
    assert markers["pose.position_3d"] == 6
    # 只有真正用 children 表达层级的节点算旧格式；其余 5 个节点的 children 是空列表
    assert markers["children 列表"] == 1
    assert markers["edges 键"] == 1
    assert markers["边仅有 uuid 端点"] == 2
    assert markers["边 source_handle/target_handle"] == 1

    reports: list[str] = []
    upgraded = upgrade_legacy_graph_payload(payload, source="启动图 lab.json", report=reports.append)
    assert len(reports) == 1 and reports[0].startswith("启动图 lab.json 使用旧格式图字段")
    assert "根级 position ×6" in reports[0]
    assert legacy_graph_markers(upgraded) == {}

    # 当前契约的图：不转换、不提示
    reports.clear()
    same = upgrade_legacy_graph_payload(upgraded, source="x", report=reports.append)
    assert same == upgraded and reports == []


def test_material_nodes_batch_resolves_occupied_by_and_fills_defaults():
    deck_uuid, plate_uuid = str(uuid4()), str(uuid4())
    raw = [
        _legacy_node(
            uuid=deck_uuid,
            id="deck",
            type="deck",
            config=None,
            extra=None,
            sites=[{"label": "T1", "occupied_by": "plate"}],
        ),
        _legacy_node(uuid=plate_uuid, id="plate", type="plate", parent_uuid=deck_uuid, data=None),
    ]
    deck, plate = normalize_legacy_material_nodes(raw)
    assert deck["sites"][0]["occupied_material_uuid"] == plate_uuid
    assert deck["config"] == {} and deck["extra"] == {} and plate["data"] == {}
    assert "model" not in deck and deck["parent_uuid"] is None
    assert plate["parent_uuid"] == deck_uuid and plate["template_name"] == "host_node"


# ── 微后端 / Graph Authority 装配只认当前契约 ───────────────────


def test_graph_loader_loads_converted_export_but_rejects_raw_legacy_shape():
    from unilabos.server.services.materials.graph import load_graph_payload

    payload, ids = _legacy_export()
    with pytest.raises(ValueError, match="根字段 position 不受支持"):
        load_graph_payload(payload, machine_name="lab-pc")

    loaded = load_graph_payload(normalize_legacy_graph(payload), machine_name="lab-pc")
    tree, links = loaded.resource_tree_set, loaded.links
    assert len(tree.all_nodes) == 6
    by_uuid = {node.res_content.uuid: node for node in tree.all_nodes}
    assert by_uuid[ids["well_a"]].res_content.parent.uuid == ids["plate_a"]
    assert by_uuid[ids["well_b"]].res_content.parent.uuid == ids["plate_b"]
    dumped = by_uuid[ids["host"]].res_content.model_dump(by_alias=True)
    assert dumped["pose"]["position3d"] == {"x": 4.0, "y": 5.0, "z": 6.0}
    assert len(links) == 1
    assert links[0]["source"] == "host_node" and links[0]["target"] == "ws"
    assert links[0]["sourceHandle"] == "out" and links[0]["targetHandle"] == "in"


def test_graph_authority_accepts_converted_export_idempotently(tmp_path):
    graph_service = pytest.importorskip(
        "unilabos.server.services.materials.graph", exc_type=ImportError
    )
    deck_uuid, plate_uuid = str(uuid4()), str(uuid4())
    payload = {
        "nodes": [
            _legacy_node(
                uuid=deck_uuid,
                id="deck",
                name="deck",
                type="deck",
                **{"class": "deck"},
                config={"sites": [{"label": "T1", "position": {"x": 0, "y": 0, "z": 0}, "occupied_by": "plate"}]},
            ),
            _legacy_node(
                uuid=plate_uuid,
                id="plate",
                name="plate",
                type="plate",
                **{"class": "plate"},
                parent="deck",
                parent_uuid=deck_uuid,
                data={"liquids": [["", None]]},
            ),
        ],
        "edges": [],
    }
    service = graph_service.GraphService(str(tmp_path / "materials.db"))

    # 原始旧形状：微后端不做兼容，直接拒绝。
    with pytest.raises(graph_service.GraphError) as excinfo:
        service.upsert_graph(name="legacy", payload=payload, device_site_templates={})
    assert excinfo.value.code == "invalid_payload"
    assert "根字段 position" in excinfo.value.message

    converted = normalize_legacy_graph(payload)
    first = service.upsert_graph(name="legacy", payload=converted, device_site_templates={})
    stored = service.get_payload(first["uuid"])
    deck = next(n for n in stored["nodes"] if n["id"] == "deck")
    assert deck["template_name"] == "deck"
    assert deck["sites"][0]["occupied_material_uuid"] == plate_uuid
    assert "occupied_by" not in deck["sites"][0]
    assert "position" not in deck
    plate = next(n for n in stored["nodes"] if n["id"] == "plate")
    assert "liquids" not in plate["data"]

    second = service.upsert_graph(name="legacy", payload=converted, device_site_templates={})
    assert second["revision"] == first["revision"]
    assert sorted(second["summary"]["unchanged"]) == ["deck", "plate"]


def test_graph_authority_keeps_uuid_identity_for_duplicate_child_ids(tmp_path):
    graph_service = pytest.importorskip(
        "unilabos.server.services.materials.graph", exc_type=ImportError
    )
    plate_a, plate_b, well_a, well_b = (str(uuid4()) for _ in range(4))
    payload = {
        "nodes": [
            {"uuid": plate_a, "id": "plate_a", "type": "plate", "template_name": "plate"},
            {"uuid": plate_b, "id": "plate_b", "type": "plate", "template_name": "plate"},
            {"uuid": well_a, "id": "A1", "type": "well", "parent": "plate_a", "parent_uuid": plate_a},
            {"uuid": well_b, "id": "A1", "type": "well", "parent": "plate_b", "parent_uuid": plate_b},
        ]
    }
    service = graph_service.GraphService(str(tmp_path / "materials.db"))
    first = service.upsert_graph(name="dup", payload=payload, device_site_templates={})
    assert {n["uuid"] for n in first["payload"]["nodes"]} == {plate_a, plate_b, well_a, well_b}
    second = service.upsert_graph(name="dup", payload=payload, device_site_templates={})
    assert second["revision"] == first["revision"]
    assert len(second["summary"]["unchanged"]) == 4

    # 没有 uuid 的同名节点仍然拒绝：身份无法区分
    with pytest.raises(graph_service.GraphError) as excinfo:
        service.upsert_graph(name="dup2", payload={"nodes": [{"id": "A1"}, {"id": "A1"}]})
    assert excinfo.value.code == "invalid_payload"
