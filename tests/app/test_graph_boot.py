"""``unilab -g`` 启动分派：权威拉取、缓存落盘与创建入口的对账/发号。"""

from __future__ import annotations

import json

import pytest

from unilabos.app.main import (
    _materialize_graph_from_authority,
    _read_graph_json,
    _register_graph_file_to_authority,
)
from unilabos.server.backend.legacy_adaptor.legacy.startup import (
    upgrade_startup_graph_payload,
)
from unilabos.server.database.layout import ServerDatabasePaths
from unilabos.server.services.materials.graph import GraphError, GraphService

PAYLOAD = {"nodes": [{"id": "pump"}], "links": []}


@pytest.fixture()
def authority(tmp_path):
    paths = ServerDatabasePaths.resolve(tmp_path)
    service = GraphService(paths.materials_db)
    try:
        yield service, paths
    finally:
        service.close()


class TestMaterializeFromAuthority:
    def test_by_name_and_uuid_share_one_cache_file(self, authority, tmp_path) -> None:
        service, paths = authority
        record = service.upsert_graph(name="lan", payload=PAYLOAD)
        args_dict = {"server_database_root": str(tmp_path)}

        by_name = _materialize_graph_from_authority("lan", args_dict, str(tmp_path))
        assert by_name is not None
        with open(by_name, encoding="utf-8") as stream:
            cached = json.load(stream)
        # upsert 是创建入口：缓存的是发号后的权威 payload。
        assert [n["id"] for n in cached["nodes"]] == ["pump"]
        assert cached["nodes"][0]["uuid"]
        assert by_name.endswith(f"{record['uuid']}.json")

        by_uuid = _materialize_graph_from_authority(
            record["uuid"], args_dict, str(tmp_path)
        )
        assert by_uuid == by_name

    def test_unknown_identity_returns_none(self, authority, tmp_path) -> None:
        args_dict = {"server_database_root": str(tmp_path)}
        assert (
            _materialize_graph_from_authority("nope", args_dict, str(tmp_path)) is None
        )

    def test_missing_database_returns_none(self, tmp_path) -> None:
        args_dict = {"server_database_root": str(tmp_path / "empty")}
        assert (
            _materialize_graph_from_authority(
                "lan", args_dict, str(tmp_path / "empty")
            )
            is None
        )


class TestUpsertReconcile:
    """upsert 即创建入口：草稿发号、身份复用、冲突拒绝与 diff 摘要。"""

    def test_draft_nodes_get_stable_identity_and_summary(self, authority) -> None:
        service, _paths = authority
        draft = {"nodes": [{"id": "pump"}, {"id": "rack"}], "links": []}

        first = service.upsert_graph(name="lab", payload=draft)
        assert first["revision"] == 1
        assert sorted(first["summary"]["created"]) == ["pump", "rack"]
        assert first["summary"]["uuid_assigned"] == 2
        assigned = {n["id"]: n["uuid"] for n in first["payload"]["nodes"]}
        assert all(assigned.values())

        # 同一草稿重复导入：发号稳定、内容一致、revision 不变。
        second = service.upsert_graph(name="lab", payload=draft)
        assert second["revision"] == 1
        assert sorted(second["summary"]["unchanged"]) == ["pump", "rack"]
        assert {n["id"]: n["uuid"] for n in second["payload"]["nodes"]} == assigned

        # 新增 + 移除节点的 diff 摘要。
        third = service.upsert_graph(
            name="lab", payload={"nodes": [{"id": "pump"}, {"id": "valve"}]}
        )
        assert third["revision"] == 2
        assert third["summary"]["created"] == ["valve"]
        assert third["summary"]["removed"] == ["rack"]
        assert third["summary"]["unchanged"] == ["pump"]
        assert {n["id"]: n["uuid"] for n in third["payload"]["nodes"]}["pump"] == assigned["pump"]

    def test_identity_conflict_is_rejected(self, authority) -> None:
        service, _paths = authority
        service.upsert_graph(name="lab", payload={"nodes": [{"id": "pump"}]})

        with pytest.raises(GraphError) as excinfo:
            service.upsert_graph(
                name="lab",
                payload={"nodes": [{"id": "pump", "uuid": "11111111-1111-1111-1111-111111111111"}]},
            )
        assert excinfo.value.code == "identity_conflict"

    def test_duplicate_node_id_is_rejected(self, authority) -> None:
        service, _paths = authority
        with pytest.raises(GraphError) as excinfo:
            service.upsert_graph(
                name="lab", payload={"nodes": [{"id": "pump"}, {"id": "pump"}]}
            )
        assert excinfo.value.code == "invalid_payload"

    def test_plr_flat_config_sites_become_canonical(self, authority) -> None:
        service, _paths = authority
        draft = {
            "nodes": [
                {
                    "id": "deck1",
                    "type": "deck",
                    "config": {
                        "type": "DemoDeck",
                        "sites": [
                            {
                                "label": "T1",
                                "position": {"x": 1, "y": 2, "z": 0},
                                "size": {"width": 10, "height": 20, "depth": 0},
                                "content_type": ["plate"],
                            }
                        ],
                    },
                }
            ]
        }
        stored = service.upsert_graph(name="lab", payload=draft)
        node = stored["payload"]["nodes"][0]
        assert "sites" not in node["config"]
        assert node["template_name"] == "DemoDeck"
        assert node["sites_initialized"] is True
        site = node["sites"][0]
        assert site["uuid"] and site["material_uuid"] == node["uuid"]
        assert site["template_name"] == "DemoDeck"
        assert site["allowed_resource_categories"] == ["plate"]
        assert site["pose"]["position3d"] == {"x": 1.0, "y": 2.0, "z": 0.0}

        # 重复导入：Site 身份同样稳定。
        again = service.upsert_graph(name="lab", payload=draft)
        assert again["revision"] == stored["revision"]
        assert again["payload"]["nodes"][0]["sites"][0]["uuid"] == site["uuid"]

    def test_authority_does_not_upgrade_legacy_fields(self, authority) -> None:
        """微后端只认当前契约：class 不回填 template_name，根级 position 直接拒绝。"""
        service, _paths = authority
        stored = service.upsert_graph(
            name="lab",
            payload={"nodes": [{"id": "pump", "type": "device", "class": "pump_demo"}]},
        )
        node = stored["payload"]["nodes"][0]
        assert node["class"] == "pump_demo"
        assert "template_name" not in node

        with pytest.raises(GraphError) as excinfo:
            service.upsert_graph(
                name="lab2",
                payload={"nodes": [{"id": "pump", "position": {"x": 1, "y": 2, "z": 0}}]},
            )
        assert excinfo.value.code == "invalid_payload"
        assert "根字段 position" in excinfo.value.message

    def test_children_lists_are_stripped(self, authority) -> None:
        """层级只由 parent 表达：派生的 children 列表不入库。"""
        service, _paths = authority
        stored = service.upsert_graph(
            name="lab",
            payload={
                "nodes": [
                    {"id": "station", "children": ["pump"]},
                    {"id": "pump", "parent": "station"},
                ]
            },
        )
        nodes = {node["id"]: node for node in stored["payload"]["nodes"]}
        assert "children" not in nodes["station"]
        assert nodes["pump"]["parent"] == "station"

    def test_sites_without_template_hint_rejected(self, authority) -> None:
        service, _paths = authority
        with pytest.raises(GraphError) as excinfo:
            service.upsert_graph(
                name="lab",
                payload={"nodes": [{"id": "deck1", "sites": [{"label": "T1"}]}]},
            )
        assert excinfo.value.code == "invalid_payload"


class TestAdoptExistingGraph:
    """启动登记（on_existing="adopt"）：权威已有该图时以权威为准，只补权威没有的。"""

    def test_missing_graph_is_created_like_replace(self, authority) -> None:
        service, _paths = authority
        stored = service.upsert_graph(
            name="lab", payload={"nodes": [{"id": "pump"}]}, on_existing="adopt"
        )
        assert stored["revision"] == 1
        assert stored["summary"]["existing"] is False
        assert stored["summary"]["created"] == ["pump"]
        assert stored["payload"]["nodes"][0]["uuid"]

    def test_existing_nodes_keep_authority_version_and_additions_are_registered(
        self, authority
    ) -> None:
        service, _paths = authority
        first = service.upsert_graph(
            name="lab",
            payload={
                "nodes": [
                    {"id": "pump", "pose": {"position": {"x": 1, "y": 1, "z": 0}}},
                    {"id": "rack"},
                ],
                "links": [{"source": "pump", "target": "rack", "sourceHandle": "out", "targetHandle": "in"}],
            },
        )
        pump_uuid = {n["id"]: n["uuid"] for n in first["payload"]["nodes"]}["pump"]

        # 本地文件：改了 pump 的位置、删了 rack、新增 valve 和一条连线
        local = {
            "nodes": [
                {"id": "pump", "pose": {"position": {"x": 99, "y": 99, "z": 0}}},
                {"id": "valve"},
            ],
            "links": [
                {"source": "pump", "target": "rack", "sourceHandle": "out", "targetHandle": "in"},
                {"source": "pump", "target": "valve"},
            ],
        }
        adopted = service.upsert_graph(name="lab", payload=local, on_existing="adopt")

        nodes = {n["id"]: n for n in adopted["payload"]["nodes"]}
        assert set(nodes) == {"pump", "rack", "valve"}  # 删除不生效、新增补进
        assert nodes["pump"]["uuid"] == pump_uuid
        assert nodes["pump"]["pose"]["position"] == {"x": 1, "y": 1, "z": 0}  # 修改不生效
        assert nodes["valve"]["uuid"]
        assert len(adopted["payload"]["links"]) == 2  # 既有连线不重复，新连线补进
        summary = adopted["summary"]
        assert summary["existing"] is True
        assert summary["adopted"] == ["pump"]
        assert summary["kept"] == ["rack"]
        assert summary["created"] == ["valve"]
        assert summary["removed"] == [] and summary["updated"] == []
        assert adopted["revision"] == 2

        # 同一文件再启动：内容与权威一致，revision 不变
        again = service.upsert_graph(name="lab", payload=local, on_existing="adopt")
        assert again["revision"] == 2
        assert again["summary"]["created"] == []

    def test_conflicting_uuid_is_rejected_even_in_adopt_mode(self, authority) -> None:
        service, _paths = authority
        service.upsert_graph(name="lab", payload={"nodes": [{"id": "pump"}, {"id": "rack"}]})
        rack_uuid = {n["id"]: n["uuid"] for n in service.get_graph("lab")["payload"]["nodes"]}["rack"]

        with pytest.raises(GraphError) as by_id:
            service.upsert_graph(
                name="lab",
                payload={"nodes": [{"id": "pump", "uuid": "11111111-1111-1111-1111-111111111111"}]},
                on_existing="adopt",
            )
        assert by_id.value.code == "identity_conflict"

        with pytest.raises(GraphError) as by_uuid:
            service.upsert_graph(
                name="lab",
                payload={"nodes": [{"id": "pump", "uuid": rack_uuid}]},
                on_existing="adopt",
            )
        assert by_uuid.value.code == "identity_conflict"

    def test_replace_mode_still_applies_file_changes(self, authority) -> None:
        service, _paths = authority
        service.upsert_graph(name="lab", payload={"nodes": [{"id": "pump"}, {"id": "rack"}]})
        replaced = service.upsert_graph(name="lab", payload={"nodes": [{"id": "pump"}]})
        assert [n["id"] for n in replaced["payload"]["nodes"]] == ["pump"]
        assert replaced["summary"]["removed"] == ["rack"]

    def test_invalid_mode_is_rejected(self, authority) -> None:
        service, _paths = authority
        with pytest.raises(GraphError) as excinfo:
            service.upsert_graph(name="lab", payload=PAYLOAD, on_existing="merge")
        assert excinfo.value.code == "invalid_input"


class TestRegisterGraphFileToAuthority:
    def test_modified_file_on_reboot_runs_from_authority_snapshot(
        self, authority, tmp_path
    ) -> None:
        """再次启动时改过的本地文件：既有节点以权威为准，新增节点登记，缓存即权威 payload。"""
        service, _paths = authority
        graph_file = tmp_path / "my_lab.json"
        graph_file.write_text(
            json.dumps({"nodes": [{"id": "pump", "pose": {"position": {"x": 1, "y": 1, "z": 0}}}], "links": []}),
            encoding="utf-8",
        )
        args_dict = {"server_database_root": str(tmp_path)}
        _register_graph_file_to_authority(str(graph_file), args_dict, str(tmp_path))

        graph_file.write_text(
            json.dumps(
                {
                    "nodes": [
                        {"id": "pump", "pose": {"position": {"x": 5, "y": 5, "z": 0}}},
                        {"id": "valve"},
                    ],
                    "links": [],
                }
            ),
            encoding="utf-8",
        )
        cache_path = _register_graph_file_to_authority(str(graph_file), args_dict, str(tmp_path))
        with open(cache_path, encoding="utf-8") as stream:
            cached = {n["id"]: n for n in json.load(stream)["nodes"]}
        assert cached["pump"]["pose"]["position"] == {"x": 1, "y": 1, "z": 0}
        assert cached["valve"]["uuid"]
        assert service.get_graph("my_lab")["revision"] == 2

    def test_conflicting_file_refuses_to_start(self, authority, tmp_path, monkeypatch) -> None:
        import unilabos.app.main as main_module

        class _Exit(Exception):
            pass

        def _exit(code: int) -> None:
            raise _Exit(code)

        monkeypatch.setattr(main_module.os, "_exit", _exit)
        graph_file = tmp_path / "my_lab.json"
        graph_file.write_text(json.dumps(PAYLOAD), encoding="utf-8")
        args_dict = {"server_database_root": str(tmp_path)}
        _register_graph_file_to_authority(str(graph_file), args_dict, str(tmp_path))

        graph_file.write_text(
            json.dumps({"nodes": [{"id": "pump", "uuid": "11111111-1111-1111-1111-111111111111"}]}),
            encoding="utf-8",
        )
        with pytest.raises(_Exit):
            _register_graph_file_to_authority(str(graph_file), args_dict, str(tmp_path))

    def test_draft_file_registered_and_cached_with_identity(
        self, authority, tmp_path
    ) -> None:
        service, paths = authority
        graph_file = tmp_path / "my_lab.json"
        graph_file.write_text(json.dumps(PAYLOAD), encoding="utf-8")
        args_dict = {"server_database_root": str(tmp_path)}

        cache_path = _register_graph_file_to_authority(
            str(graph_file), args_dict, str(tmp_path)
        )
        assert cache_path is not None
        with open(cache_path, encoding="utf-8") as stream:
            cached = json.load(stream)
        assert all(node.get("uuid") for node in cached["nodes"])
        assert service.get_graph("my_lab")["revision"] == 1

        # 再次启动同一文件：登记幂等，revision 不变。
        second = _register_graph_file_to_authority(
            str(graph_file), args_dict, str(tmp_path)
        )
        assert second == cache_path
        assert service.get_graph("my_lab")["revision"] == 1

    def test_legacy_file_is_converted_at_reception(
        self, authority, tmp_path, capsys
    ) -> None:
        """旧格式启动图在读取边界转成当前契约；Graph Authority 只收到当前契约。"""
        service, _paths = authority
        legacy = {
            "nodes": [
                {
                    "id": "host_node",
                    "type": "device",
                    "class": "host_node",
                    "children": ["pump"],
                    "position": {"x": 1, "y": 2, "z": 0},
                    "pose": {"position_3d": {"x": 1, "y": 2, "z": 0}},
                },
                {"id": "pump", "type": "device", "class": "pump_demo", "parent": "host_node"},
            ],
            "edges": [
                {"source": "host_node", "target": "pump", "source_handle": "out", "target_handle": "in"}
            ],
        }
        graph_file = tmp_path / "old_lab.json"
        graph_file.write_text(json.dumps(legacy), encoding="utf-8")
        args_dict = {"server_database_root": str(tmp_path)}

        payload = upgrade_startup_graph_payload(
            _read_graph_json(str(graph_file)), str(graph_file)
        )
        assert "使用旧格式图字段" in capsys.readouterr().out
        cache_path = _register_graph_file_to_authority(
            str(graph_file), args_dict, str(tmp_path), payload
        )
        assert cache_path is not None
        with open(cache_path, encoding="utf-8") as stream:
            cached = json.load(stream)
        host = next(n for n in cached["nodes"] if n["id"] == "host_node")
        assert host["template_name"] == "host_node"
        assert "position" not in host and "children" not in host
        assert host["pose"]["position"] == {"x": 1, "y": 2, "z": 0}
        assert host["pose"]["position3d"] == {"x": 1, "y": 2, "z": 0}
        assert cached["links"][0]["sourceHandle"] == "out"
        assert "source_handle" not in cached["links"][0]
        assert service.get_graph("old_lab")["revision"] == 1


class TestLoadStartupGraph:
    """``-g <文件>.json`` 的完整装配：legacy 升级 → Graph Authority → 运行时结构。"""

    @pytest.fixture()
    def physical_graph_slot(self, monkeypatch):
        from unilabos.backend.runtime import workstation_protocol

        monkeypatch.setattr(workstation_protocol, "_physical_setup_graph", None)
        return workstation_protocol

    def test_example_graph_loads_through_graph_authority(
        self, authority, tmp_path, monkeypatch, physical_graph_slot
    ) -> None:
        import shutil
        from pathlib import Path

        import unilabos
        from unilabos.app.main import _load_startup_graph
        from unilabos.config.config import BasicConfig
        from unilabos.experiments.compile.pump_protocol import generate_pump_protocol

        service, _paths = authority
        monkeypatch.setattr(BasicConfig, "machine_name", "lab-pc")
        source = (
            Path(unilabos.__file__).parent
            / "test/experiments/Protocol_Test_Station/pumptransfer_test_station.json"
        )
        graph_file = tmp_path / source.name
        shutil.copy(source, graph_file)
        args_dict = {"server_database_root": str(tmp_path), "graph": str(graph_file)}

        loaded = _load_startup_graph(args_dict, str(tmp_path), is_slave=False)

        # 本地文件已登记进权威，后续以权威缓存为准
        assert args_dict["_graph_from_authority"] is True
        assert args_dict["_graph_file_path"].endswith(".json")
        assert service.get_graph("pumptransfer_test_station")["revision"] == 1

        tree = loaded.resource_tree_set
        (root,) = tree.root_nodes
        assert root.res_content.id == "SimpleProtocolStation"
        assert len(tree.all_nodes) == 10
        assert all(node.res_content.uuid for node in tree.all_nodes)
        assert {node.res_content.machine_name for node in tree.all_nodes} == {"lab-pc"}

        # backend 连线：port 拆成 sourceHandle / targetHandle
        assert loaded.links
        for link in loaded.links:
            assert "port" not in link
            assert link["id"] == (
                f"reactflow__edge-{link['source']}-{link['sourceHandle']}-"
                f"{link['target']}-{link['targetHandle']}"
            )

        # 物理拓扑图登记给协议编译器，边保留端口字典
        graph = physical_graph_slot.physical_setup_graph()
        assert graph is loaded.physical_setup_graph
        assert set(graph.nodes) == {node.res_content.id for node in tree.all_nodes}
        assert graph.edges["multiway_valve_1", "flask_DMF"]["port"] == {
            "multiway_valve_1": "2",
            "flask_DMF": "outlet",
        }
        steps = generate_pump_protocol(graph, "flask_DMF", "main_reactor", 10.0)
        assert steps[0] == {
            "device_id": "multiway_valve_1",
            "action_name": "set_valve_position",
            "action_kwargs": {"command": "2"},
        }

    def test_without_graph_starts_with_empty_graph(
        self, tmp_path, monkeypatch, physical_graph_slot
    ) -> None:
        from unilabos.app.main import _load_startup_graph
        from unilabos.config.config import BasicConfig

        monkeypatch.setattr(BasicConfig, "startup_json_path", None)
        loaded = _load_startup_graph({"graph": None}, str(tmp_path), is_slave=False)
        assert loaded.resource_tree_set.trees == []
        assert loaded.links == []
        assert loaded.physical_setup_graph.number_of_nodes() == 0
        assert physical_graph_slot.physical_setup_graph() is loaded.physical_setup_graph


class TestRequireNodeLinkJson:
    @pytest.fixture()
    def exits(self, monkeypatch):
        import unilabos.app.main as main_module

        class _Exit(Exception):
            pass

        def _exit(code: int) -> None:
            raise _Exit(code)

        monkeypatch.setattr(main_module.os, "_exit", _exit)
        return _Exit

    def test_graphml_is_rejected_with_conversion_hint(self, tmp_path, exits, capsys) -> None:
        from unilabos.app.main import _require_node_link_json

        graph_file = tmp_path / "lab.graphml"
        graph_file.write_text("<graphml/>", encoding="utf-8")
        for path in (str(graph_file), "missing_lab.GraphML"):
            with pytest.raises(exits):
                _require_node_link_json(path)
            message = capsys.readouterr().out
            assert "不再支持 GraphML" in message
            assert "node-link JSON" in message

    def test_other_local_files_must_be_json(self, tmp_path, exits, capsys) -> None:
        from unilabos.app.main import _require_node_link_json

        graph_file = tmp_path / "lab.yaml"
        graph_file.write_text("nodes: []", encoding="utf-8")
        with pytest.raises(exits):
            _require_node_link_json(str(graph_file))
        assert "node-link JSON" in capsys.readouterr().out

    def test_json_files_and_authority_identities_pass(self, tmp_path, exits) -> None:
        from unilabos.app.main import _require_node_link_json

        graph_file = tmp_path / "lab.json"
        graph_file.write_text(json.dumps(PAYLOAD), encoding="utf-8")
        _require_node_link_json(str(graph_file))
        # 非本地文件：按 uuid / 名称交给 Graph Authority 解析
        _require_node_link_json("my_lab")
