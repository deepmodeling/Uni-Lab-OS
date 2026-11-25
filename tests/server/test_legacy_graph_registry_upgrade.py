"""旧格式物料节点在读图边界按注册表补齐 PLR 类：旧图不改文件也能起来。

``fixtures/legacy_graphs/`` 里是示例图在「容器改成当前写法」那次手工修改之前的版本（原样拷贝，
测试不依赖 git 历史）：

- ``mock_reactor.json``：设备 + 一个 ``"class": "container"``、没有 ``config.type`` 的容器；
- ``simple_stir_heatchill_test_station.json``：工作站 + ``"class": null`` 的容器；
- ``pumptransfer_filterthrough_test_station.json``：工作站 + 容器，其中三个的 ``config`` 里混着
  PLR 构造器不认的 ``filter_type``；
- ``comprehensive_station.json``：容器已有 ``config.type``，但 ``class`` 写成模板名 ``container``。

升级结果要与仓库里手工改好的同名示例图（同样经过读图边界）完全一致，并能经 Graph Authority
装配、建成模板对应的 PLR 类。
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any, Mapping, Optional

import pytest
import yaml

from unilabos.client.utils.envelope import EnvelopeError
from unilabos.config.config import BasicConfig
from unilabos.registry.registry import lab_registry
from unilabos.resources.adapters.registry_materials import registry_resource_class_name
from unilabos.client.host.legacy_adaptor.legacy.graph import (
    LegacyGraphError,
    legacy_graph_markers,
    upgrade_legacy_graph_payload,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).parent / "fixtures" / "legacy_graphs"
EXPERIMENTS = REPO_ROOT / "unilabos" / "test" / "experiments"
CONTAINER_YAML = REPO_ROOT / "unilabos" / "registry" / "resources" / "organic" / "container.yaml"
#: 物料 / 模板的 class：初始化它用的注册表名
CONTAINER_CLASS = "container"

#: 旧夹具 → 手工改成当前写法的示例图
LEGACY_FIXTURES = {
    "mock_reactor.json": "mock_reactor.json",
    "simple_stir_heatchill_test_station.json": "Protocol_Test_Station/simple_stir_heatchill_test_station.json",
    "pumptransfer_filterthrough_test_station.json": (
        "Protocol_Test_Station/pumptransfer_filterthrough_test_station.json"
    ),
    "comprehensive_station.json": "comprehensive_protocol/comprehensive_station.json",
}


def _container_entry(*, config_info: bool = True) -> dict[str, Any]:
    """注册表里的 ``container`` 条目；``config_info`` 按注册表的生成方式补（Host 上传模式才有）。"""

    entry = {"id": "container", "registry_type": "resource"}
    entry.update(yaml.safe_load(CONTAINER_YAML.read_text(encoding="utf-8"))["container"])
    if config_info:
        from unilabos.resources.presets.container import get_regular_container
        from unilabos.resources.resource_tracker import ResourceTreeSet

        dumped = ResourceTreeSet.from_plr_resources(
            [get_regular_container("get_regular_container")], known_random_uuid=True, old_size=True
        ).dump()
        entry["config_info"] = dumped[0]
    return entry


class _Lookup:
    """模板名 → 注册表条目；记录查过哪些模板。"""

    def __init__(self, entries: Mapping[str, Mapping[str, Any]]) -> None:
        self.entries = dict(entries)
        self.calls: list[str] = []

    def __call__(self, name: str) -> Optional[Mapping[str, Any]]:
        self.calls.append(name)
        return self.entries.get(name)


@pytest.fixture()
def registry_lookup() -> _Lookup:
    return _Lookup({"container": _container_entry()})


def _read(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _upgrade(payload: Mapping[str, Any], lookup, reports: Optional[list[str]] = None) -> dict[str, Any]:
    return upgrade_legacy_graph_payload(
        payload,
        source="t",
        report=(reports.append if reports is not None else (lambda _message: None)),
        resource_entry=lookup,
    )


def _by_id(payload: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    return {node["id"]: node for node in payload["nodes"]}


# ── 读图边界：升级规则 ───────────────────────────────────────────


@pytest.mark.parametrize("fixture", sorted(LEGACY_FIXTURES))
def test_legacy_fixture_upgrades_to_the_hand_fixed_example(fixture, registry_lookup) -> None:
    legacy = _read(FIXTURES / fixture)
    current = _read(EXPERIMENTS / LEGACY_FIXTURES[fixture])

    reports: list[str] = []
    upgraded = _upgrade(legacy, registry_lookup, reports)

    assert "container" in registry_lookup.calls
    assert "物料缺 config.type" in reports[0] or "物料 class 写成模板名" in reports[0]
    # 与手工改好的示例图（同样经过读图边界）逐节点、逐连线一致
    assert upgraded == _upgrade(current, registry_lookup)
    for node in upgraded["nodes"]:
        if node.get("type") == "container":
            assert node["template_name"] == "container", node["id"]
            # class 是初始化它用的注册表名，config.type 是 PLR 类名
            assert node["class"] == CONTAINER_CLASS, node["id"]
            assert node["config"]["type"] == "RegularContainer", node["id"]
    # 幂等：升级结果不再带旧字段，再升级不变
    assert legacy_graph_markers(upgraded) == {}
    again: list[str] = []
    assert _upgrade(upgraded, registry_lookup, again) == upgraded and again == []
    # 输入不被修改
    assert legacy == _read(FIXTURES / fixture)


def test_filter_type_moves_from_config_to_data(registry_lookup) -> None:
    upgraded = _by_id(_upgrade(_read(FIXTURES / "pumptransfer_filterthrough_test_station.json"), registry_lookup))
    for node_id, filter_type in (
        ("filter_celite", "celite_pad"),
        ("column_silica_gel", "silica_gel_column"),
        ("filter_C18", "C18_cartridge"),
    ):
        node = upgraded[node_id]
        assert "filter_type" not in node["config"] and "max_volume" in node["config"], node_id
        assert node["data"]["filter_type"] == filter_type, node_id


def test_registry_entry_without_config_info_uses_the_declared_class() -> None:
    """Slave 进程的注册表不生成 config_info：按条目 ``class.module`` 实例化取类名。"""

    entry = _container_entry(config_info=False)
    assert registry_resource_class_name(entry) is None
    upgraded = _upgrade(_read(FIXTURES / "mock_reactor.json"), _Lookup({"container": entry}))
    reactor = _by_id(upgraded)["reactor"]
    assert reactor["class"] == CONTAINER_CLASS
    assert reactor["config"]["type"] == "RegularContainer"


def test_unknown_template_names_the_node_and_the_template(registry_lookup) -> None:
    by_class = {
        "nodes": [
            {"id": "station", "type": "device", "class": "virtual_stirrer"},
            {"id": "flask", "type": "container", "class": "my_flask", "parent": "station",
             "config": {"max_volume": 10.0}},
        ],
        "links": [],
    }
    with pytest.raises(LegacyGraphError, match=r"节点 flask 的模板 'my_flask'（取自 class）不在注册表中"):
        _upgrade(by_class, registry_lookup)

    by_type = {"nodes": [{"id": "bottle_1", "type": "bottle", "config": {}}], "links": []}
    with pytest.raises(LegacyGraphError, match=r"节点 bottle_1 的模板 'bottle'（取自 type）不在注册表中"):
        _upgrade(by_type, registry_lookup)

    # 旧版 PLR 序列化（config.type 与注册表模板的类不一致）同样明确拒绝，不改写 config.type
    stale = {
        "nodes": [{"id": "tank", "type": "container", "class": "container", "config": {"type": "Plate"}}],
        "links": [],
    }
    with pytest.raises(LegacyGraphError, match=r"节点 tank 的 config.type='Plate' 与注册表模板 'container'"):
        _upgrade(stale, registry_lookup)


def test_comprehensive_slim_fails_with_a_clear_error(registry_lookup) -> None:
    """台面是旧版 PLR 序列化、根模板也不在注册表的示例图：不救，但报错指出节点与模板。"""

    payload = _read(EXPERIMENTS / "comprehensive_protocol" / "comprehensive_slim.json")
    with pytest.raises(LegacyGraphError, match=r"节点 tip_rack 的模板 'opentrons_96_filtertiprack_1000ul'"):
        _upgrade(payload, registry_lookup)


def test_current_format_graph_is_left_alone() -> None:
    def no_lookup(name: str):
        raise AssertionError(f"当前写法的物料不该查注册表: {name}")

    # 手工改好的示例图：只做形状转换（根级 position、name 等），物料节点不查注册表
    current = _read(EXPERIMENTS / "Protocol_Test_Station" / "simple_stir_heatchill_test_station.json")
    upgraded = _upgrade(current, no_lookup)
    assert _by_id(upgraded)["main_reactor"]["class"] == CONTAINER_CLASS

    # 完全当前契约的图：原样返回、不提示；class 为空而 config.type 已有也是合法写法
    payload = {
        "nodes": [
            {"id": "flask", "uuid": "0b0f9d8e-2c39-4c5e-9d07-9c2d6f0e1a11",
             "name": "0b0f9d8e-2c39-4c5e-9d07-9c2d6f0e1a11", "type": "container",
             "class": "RegularContainer", "template_name": "container",
             "config": {"type": "RegularContainer", "max_volume": 10.0}, "data": {}},
            {"id": "well", "uuid": "5a3e7c41-1f0b-4c47-8d1e-0d5c3a9b7e22",
             "name": "5a3e7c41-1f0b-4c47-8d1e-0d5c3a9b7e22", "type": "well",
             "config": {"type": "Well"}, "data": {}},
        ],
        "links": [],
    }
    reports: list[str] = []
    assert _upgrade(payload, no_lookup, reports) == payload and reports == []


# ── -g：Host 进程按本地注册表补齐，经 Graph Authority 装配 ───────────────


@pytest.fixture()
def host_registry(monkeypatch):
    """Host 进程的本地扫描注册表里有 ``container``（读图时 Registry Authority 尚未装配）。"""

    from unilabos.client.host.runtime import workstation_protocol

    monkeypatch.setitem(lab_registry.resource_type_registry, "container", _container_entry())
    monkeypatch.setattr(workstation_protocol, "_physical_setup_graph", None)
    monkeypatch.setattr(BasicConfig, "machine_name", "lab-pc")


def _boot(graph: Path, tmp_path: Path):
    from unilabos.app.main import _load_startup_graph

    graph_file = tmp_path / graph.name
    shutil.copy(graph, graph_file)
    return _load_startup_graph(
        {"server_database_root": str(tmp_path), "graph": str(graph_file)}, str(tmp_path), is_slave=False
    )


def _material_view(loaded) -> dict[str, tuple]:
    return {
        node.res_content.id: (
            node.res_content.type,
            node.res_content.template_name,
            node.res_content.klass,
            node.res_content.parent.id if node.res_content.parent else None,
            json.dumps(node.res_content.config, sort_keys=True, default=str),
            json.dumps(node.res_content.data, sort_keys=True, default=str),
        )
        for node in loaded.resource_tree_set.all_nodes
    }


@pytest.mark.parametrize("fixture", sorted(LEGACY_FIXTURES))
def test_legacy_fixture_boots_like_the_hand_fixed_example(fixture, host_registry, tmp_path, capsys) -> None:
    from unilabos.resources.resource_tracker import ResourceTreeInstance, ResourceTreeSet

    legacy_root, current_root = tmp_path / "legacy", tmp_path / "current"
    legacy_root.mkdir()
    current_root.mkdir()
    legacy = _boot(FIXTURES / fixture, legacy_root)
    out = capsys.readouterr().out
    assert "物料缺 config.type" in out or "物料 class 写成模板名" in out
    current = _boot(EXPERIMENTS / LEGACY_FIXTURES[fixture], current_root)

    # 设备、物料、模板、类、config / data 与手工改好的版本一致（uuid 由各自的权威发号）
    assert _material_view(legacy) == _material_view(current)
    assert sorted(legacy.physical_setup_graph.edges) == sorted(current.physical_setup_graph.edges)

    # 开机对齐物料权威时按模板校验类名、按 config.type 建 PLR：每个顶层物料都建成模板的类
    entry_class = registry_resource_class_name(_container_entry())
    for node in legacy.resource_tree_set.all_nodes:
        content = node.res_content
        if content.type == "device" or (content.parent is not None and content.parent.type != "device"):
            continue
        (resource,) = ResourceTreeSet([ResourceTreeInstance(node)]).to_plr_resources()
        # class 是初始化它用的注册表名；建出来的 PLR 类就是 config.type 指的那个
        assert content.klass == CONTAINER_CLASS, content.id
        assert type(resource).__name__ == entry_class == content.config["type"], content.id


def test_unknown_template_refuses_to_start(host_registry, tmp_path, monkeypatch, capsys) -> None:
    import unilabos.app.main as main_module

    class _Exit(Exception):
        pass

    def _exit(code: int) -> None:
        raise _Exit(code)

    monkeypatch.setattr(main_module.os, "_exit", _exit)
    graph = tmp_path / "src" / "old_lab.json"
    graph.parent.mkdir()
    graph.write_text(json.dumps({
        "nodes": [
            {"id": "stirrer", "type": "device", "class": "virtual_stirrer"},
            {"id": "flask", "type": "container", "class": "my_flask", "parent": "stirrer", "config": {}},
        ],
        "links": [],
    }), encoding="utf-8")
    with pytest.raises(_Exit) as excinfo:
        _boot(graph, tmp_path)
    assert excinfo.value.args == (1,)
    out = capsys.readouterr().out
    assert "无法转换为当前契约，拒绝启动" in out and "节点 flask 的模板 'my_flask'" in out


# ── unilab graph upload：CLI 进程查上传目标调度权威的注册表 ─────────────────


class _AuthorityClient:
    base_url = "http://127.0.0.1:8002/api/v1"

    def __init__(self, *, reported: bool = True) -> None:
        self.reported = reported
        self.requests: list[str] = []

    def registry_status(self) -> dict[str, Any]:
        self.requests.append("status")
        return {"reported": self.reported}

    def registry_entry(self, name: str) -> dict[str, Any]:
        self.requests.append(name)
        if name != "container":
            # 与权威一致：条目不存在是信封业务码 4001（HTTP 200）
            raise EnvelopeError(4001, f"entry {name} not found", http_status=200)
        return {"name": name, "status": ["active"], "active_payload": _container_entry()}


def test_graph_upload_looks_templates_up_in_the_target_authority(tmp_path) -> None:
    from unilabos.app.cli.graph import _load_graph_file

    graph = tmp_path / "mock_reactor.json"
    shutil.copy(FIXTURES / "mock_reactor.json", graph)
    client = _AuthorityClient()
    upgraded = _load_graph_file(str(graph), client)
    assert upgraded == _upgrade(_read(EXPERIMENTS / "mock_reactor.json"), _Lookup({"container": _container_entry()}))
    assert client.requests == ["status", "container"]

    # 当前写法的图不发任何注册表请求
    current = tmp_path / "current.json"
    shutil.copy(EXPERIMENTS / "mock_reactor.json", current)
    quiet = _AuthorityClient()
    _load_graph_file(str(current), quiet)
    assert quiet.requests == []

    unknown = tmp_path / "unknown.json"
    unknown.write_text(json.dumps({
        "nodes": [{"id": "flask", "type": "container", "class": "my_flask", "config": {}}], "links": [],
    }), encoding="utf-8")
    with pytest.raises(LegacyGraphError, match=r"节点 flask 的模板 'my_flask'（取自 class）不在注册表中"):
        _load_graph_file(str(unknown), _AuthorityClient())
    with pytest.raises(LegacyGraphError, match="尚未收到 Host 的注册表上报"):
        _load_graph_file(str(graph), _AuthorityClient(reported=False))


# ── 端到端：旧工作站图不改文件，在真实 HostLink backend 上起来并跑通代表协议 ─────────


@pytest.mark.slow
def test_legacy_workstation_graph_runs_on_a_real_hostlink_backend(tmp_path: Path) -> None:
    import urllib.request

    from tests.app.test_process_lifetime import _Unilab

    graph = tmp_path / "old_station.json"
    shutil.copy(FIXTURES / "simple_stir_heatchill_test_station.json", graph)
    unilab = _Unilab(tmp_path, "legacy", ["-g", str(graph)], root=tmp_path / "db", builtin_devices=True)

    def request(method: str, path: str, body: Optional[dict[str, Any]] = None) -> Any:
        req = urllib.request.Request(
            f"http://127.0.0.1:{unilab.port}/api/v1{path}",
            method=method,
            data=None if body is None else json.dumps(body).encode(),
            headers={"Content-Type": "application/json"} if body is not None else {},
        )
        with urllib.request.urlopen(req, timeout=10) as response:
            payload = json.load(response)
        assert payload["code"] == 0, payload
        return payload["data"]

    try:
        unilab.wait_for(lambda: "Host 已完全就绪" in unilab.text(), "Host 就绪")
        assert "物料缺 config.type ×2" in unilab.text()
        assert "开机物料权威对齐完成" in unilab.text()
        live = {node["id"].rsplit("/", 1)[-1]: node for node in request("GET", "/graphs/live/payload")["nodes"]}
        for container in ("main_reactor", "secondary_reactor"):
            node = live[container]
            assert (node["template_name"], node["class"], node["config"]["type"]) == (
                "container", CONTAINER_CLASS, "RegularContainer"
            ), node
        # 文件本身没被改写
        assert _read(graph) == _read(FIXTURES / "simple_stir_heatchill_test_station.json")

        job = request("POST", "/job/add", {
            "device_id": "SimpleStirHeatChillTestStation",
            "action": "StirProtocol",
            "action_args": {"vessel": {"id": "main_reactor", "uuid": live["main_reactor"]["uuid"]}, "time": "5 s"},
        })["jobId"]

        def finished() -> Optional[dict[str, Any]]:
            status = request("GET", f"/job/{job}/status")
            return status if status["status"] in (4, 5, 6) else None

        status = unilab.wait_for(finished, "StirProtocol 结束", timeout=120)
        assert status["status"] == 4, (status, unilab.text()[-4000:])
        assert status["result"]["return_value"]["steps_executed"] >= 1
    finally:
        unilab.close()
