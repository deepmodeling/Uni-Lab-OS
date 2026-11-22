"""``--root_devices_as_slaves``：按根设备切子树、生成受管进程规格及其生命周期（不起进程）。"""

from __future__ import annotations

import copy
import json
import re
from dataclasses import asdict
from pathlib import Path
from typing import Any, Optional

import pytest

from unilabos.app import root_device_slaves as rds
from unilabos.config.config import BasicConfig, HostLinkConfig
from unilabos.registry.registry import lab_registry
from unilabos.server.services import device_processes as dpx
from unilabos.server.services.materials.graph import load_graph_payload


def _uuid(number: int) -> str:
    return f"00000000-0000-4000-8000-{number:012d}"


def _device(node_id: str, number: int, *, template: str, parent: Optional[str] = None, **config: Any) -> dict:
    return {
        "id": node_id,
        "uuid": _uuid(number),
        "name": _uuid(number),
        "display_name": node_id,
        "parent": parent,
        "type": "device",
        "template_name": template,
        "config": dict(config),
        "data": {},
        "pose": {"position": {"x": number, "y": 0, "z": 0}},
        "sites_initialized": True,
        "sites": [],
    }


def _resource(node_id: str, number: int, *, parent: Optional[str] = None) -> dict:
    return {
        "id": node_id,
        "uuid": _uuid(number),
        "name": _uuid(number),
        "display_name": node_id,
        "parent": parent,
        "type": "container",
        "config": {"type": "RegularContainer", "name": node_id},
        "data": {},
        "pose": {"position": {"x": number, "y": 0, "z": 0}},
    }


def _payload() -> dict:
    """Graph Authority payload 形状：host_node、带子设备与物料的工作站、单台阀、根级货架，以及四条连线。"""

    return {
        "nodes": [
            _device("host_node", 1, template="host_node"),
            _device("station", 2, template="demo_station", protocol_type=[]),
            _device("pump", 3, template="demo_pump", parent="station", port="COM1"),
            _resource("vial", 4, parent="station"),
            _device("valve", 5, template="demo_valve"),
            _resource("shelf", 6),
        ],
        "links": [
            {"id": "inner", "source": "pump", "target": "station", "type": "fluid",
             "sourceHandle": "out", "targetHandle": "in"},
            {"id": "inner_by_uuid", "source_uuid": _uuid(4), "target_uuid": _uuid(3), "type": "fluid",
             "sourceHandle": "top", "targetHandle": "in"},
            {"id": "cross", "source": "pump", "target": "valve", "type": "fluid",
             "sourceHandle": "out", "targetHandle": "in"},
            {"id": "to_shelf", "source": "valve", "target": "shelf", "type": "fluid",
             "sourceHandle": "out", "targetHandle": "top"},
        ],
    }


def _tree_set(payload: dict):
    # load_graph_payload 原地整理 payload，与 main.py 一样传副本
    return load_graph_payload(copy.deepcopy(payload), machine_name="host").resource_tree_set


def _by_device(subtrees) -> dict:
    return {subtree.device_id: subtree for subtree in subtrees}


def test_split_keeps_original_nodes_and_inner_links_and_skips_host_node() -> None:
    payload = _payload()
    original = copy.deepcopy(payload)
    subtrees = _by_device(rds.split_root_device_subtrees(payload, _tree_set(payload), host_node_name="host_node"))

    # host_node 与根级物料（货架）留在 Host；每个根设备一棵子树
    assert set(subtrees) == {"station", "valve"}
    station = subtrees["station"]
    # 节点 dict 原样（uuid、config、parent 不变），顺序与 payload 一致，物料也在子树里
    assert station.nodes == [original["nodes"][index] for index in (1, 2, 3)]
    assert station.uuid == _uuid(2) and station.template_name == "demo_station"
    assert station.device_ids == ["station", "pump"]
    # 两端都在子树里的连线才带上（按 id 或按 uuid 表达都算）；跨子树、连到根级物料的留在 Host
    assert [link["id"] for link in station.links] == ["inner", "inner_by_uuid"]
    assert subtrees["valve"].nodes == [original["nodes"][4]] and subtrees["valve"].links == []
    # 子树是副本：改它不影响 Host 的 payload
    station.nodes[1]["config"]["port"] = "COM9"
    assert payload == original


def test_split_honours_keep_in_host_and_renamed_host_node() -> None:
    payload = _payload()
    payload["nodes"][0] = _device("lab_host", 1, template="host_node")
    subtrees = rds.split_root_device_subtrees(
        payload, _tree_set(payload), host_node_name="lab_host", keep_in_host=lambda node: node["id"] == "valve"
    )
    assert [subtree.device_id for subtree in subtrees] == ["station"]


def test_generated_spec_ids_are_derived_from_the_device_id_only() -> None:
    assert rds.generated_spec_id("lock_probe_a") == "root-lock_probe_a"
    assert rds.generated_spec_id("DemoWorkstation") == "root-DemoWorkstation"
    odd = rds.generated_spec_id("bay 1/pump:A")
    assert odd == rds.generated_spec_id("bay 1/pump:A")
    assert odd != rds.generated_spec_id("bay 1/pump_A")
    long = rds.generated_spec_id("x" * 80)
    for value in (odd, long):
        # 作为目录名与 URL 路径段都安全
        assert re.fullmatch(r"root-[A-Za-z0-9_-]+", value) and len(value) <= 64, value


def test_specs_inherit_host_devices_switches_and_default_restart_policy(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    config = tmp_path / "local_config.py"
    config.write_text("", encoding="utf-8")
    payload = _payload()
    subtrees = rds.split_root_device_subtrees(payload, _tree_set(payload), host_node_name="host_node")
    args = {
        "devices": ["pkg_a", str(tmp_path / "pkg_b"), "pkg_a"],
        "external_devices_only": True,
        "test_mode": True,
        "registry_path": ["reg"],
        "hostlink_request_timeout": 7.5,
    }
    specs = {
        spec.id: spec
        for spec in rds.build_root_device_specs(subtrees, args, working_dir="work", config_path=str(config))
    }
    assert set(specs) == {"root-station", "root-valve"}
    spec = specs["root-station"]
    assert spec.name == "station" and spec.generated_by == "root_devices_as_slaves"
    assert spec.graph_nodes == _by_device(subtrees)["station"].nodes
    assert spec.graph_links == _by_device(subtrees)["station"].links
    assert spec.devices_dirs == [str(tmp_path / "pkg_a"), str(tmp_path / "pkg_b")]
    assert spec.external_only and spec.auto_start
    assert (spec.restart_policy, spec.max_restarts) == ("on-failure", 5)
    extra = spec.extra_args
    assert "--graph_is_authority_payload" in extra and "--test_mode" in extra
    assert extra[extra.index("--working_dir") + 1] == str(tmp_path / "work")
    assert extra[extra.index("--config") + 1] == str(config)
    assert extra[extra.index("--registry_path") + 1] == str(tmp_path / "reg")
    assert extra[extra.index("--hostlink_request_timeout") + 1] == "7.5"
    assert "--extra_resource" not in extra and "--hostlink_heartbeat_interval" not in extra


@pytest.fixture()
def host(tmp_path: Path, monkeypatch):
    """Host 启动早期的状态：配置已加载、受管进程服务单例尚未创建。"""

    monkeypatch.setattr(BasicConfig, "host_node_name", "host_node")
    monkeypatch.setattr(BasicConfig, "backend", "hostlink")
    monkeypatch.setattr(HostLinkConfig, "enable", True)
    monkeypatch.setattr(dpx, "_service", None)
    return tmp_path / "work"


def _prepare(tmp_path: Path, payload: dict, *, enabled: bool = True):
    cache = tmp_path / "graph_cache.json"
    cache.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    args = {
        "root_devices_as_slaves": enabled,
        "_graph_file_path": str(cache),
        "devices": [str(tmp_path / "pkg")],
        "external_devices_only": True,
    }
    tree_set = _tree_set(payload)
    kept = rds.prepare_root_device_slaves(args, tree_set, working_dir=str(tmp_path / "work"), config_path=None)
    return tree_set, kept


def _roots(tree_set) -> list[str]:
    return [tree.root_node.res_content.id for tree in tree_set.trees]


def _hand_made(process_id: str = "manual-1") -> dpx.DeviceProcessSpec:
    return dpx.DeviceProcessSpec(
        id=process_id,
        name="手建泵站",
        graph_nodes=[dpx.build_device_node("pump_9", "acme_pump")],
        devices_dirs=["/opt/acme"],
        created_at_ms=11,
        updated_at_ms=12,
    )


def test_flag_moves_each_root_device_into_its_own_spec(host: Path, tmp_path: Path) -> None:
    full, kept = _prepare(tmp_path, _payload())

    # Host 自己的 runtime 只剩 host_node 与根级物料；完整树集（开机物料对齐用）不变
    assert _roots(kept) == ["host_node", "shelf"]
    assert _roots(full) == ["host_node", "station", "valve", "shelf"]
    specs = dpx.load_specs(host)
    assert set(specs) == {"root-station", "root-valve"}
    assert all(spec.generated_by == "root_devices_as_slaves" and spec.auto_start for spec in specs.values())
    graph = json.loads((dpx.process_dir(host, "root-station") / dpx.graph_filename(specs["root-station"])).read_text(encoding="utf-8"))
    assert [node["id"] for node in graph["nodes"]] == ["station", "pump", "vial"]
    assert [link["id"] for link in graph["links"]] == ["inner", "inner_by_uuid"]


def test_restarting_the_host_reuses_specs_and_drops_stale_ones(host: Path, tmp_path: Path) -> None:
    _prepare(tmp_path, _payload())
    first = dpx.load_specs(host)
    _prepare(tmp_path, _payload())
    second = dpx.load_specs(host)
    # 同一张图重启：同样的 id，连时间戳都不变（不堆出新规格）
    assert {key: asdict(value) for key, value in second.items()} == {key: asdict(value) for key, value in first.items()}

    changed = _payload()
    changed["nodes"] = [node for node in changed["nodes"] if node["id"] != "valve"]
    changed["nodes"][2]["config"]["port"] = "COM2"
    _, kept = _prepare(tmp_path, changed)
    third = dpx.load_specs(host)
    assert set(third) == {"root-station"} and _roots(kept) == ["host_node", "shelf"]
    assert third["root-station"].created_at_ms == first["root-station"].created_at_ms
    assert third["root-station"].updated_at_ms >= first["root-station"].updated_at_ms
    assert third["root-station"].graph_nodes[1]["config"]["port"] == "COM2"


def test_without_the_flag_generated_specs_go_and_hand_made_ones_stay(host: Path, tmp_path: Path) -> None:
    hand_made = _hand_made()
    dpx.save_specs(host, {"manual-1": hand_made})
    _prepare(tmp_path, _payload())
    assert set(dpx.load_specs(host)) == {"manual-1", "root-station", "root-valve"}

    full, kept = _prepare(tmp_path, _payload(), enabled=False)
    assert kept is full and _roots(kept) == ["host_node", "station", "valve", "shelf"]
    remaining = dpx.load_specs(host)
    assert list(remaining) == ["manual-1"]
    assert asdict(remaining["manual-1"]) == asdict(hand_made)


def test_without_the_flag_and_without_generated_specs_nothing_is_written(host: Path, tmp_path: Path) -> None:
    full, kept = _prepare(tmp_path, _payload(), enabled=False)
    assert kept is full
    assert not dpx.spec_path(host).exists()


def test_hand_made_spec_with_a_generated_id_is_left_alone(host: Path, tmp_path: Path) -> None:
    hand_made = _hand_made("root-valve")
    dpx.save_specs(host, {"root-valve": hand_made})
    _, kept = _prepare(tmp_path, _payload())
    specs = dpx.load_specs(host)
    # 手建规格不改；对应的根设备留在 Host 运行
    assert asdict(specs["root-valve"]) == asdict(hand_made)
    assert specs["root-station"].generated_by == "root_devices_as_slaves"
    assert _roots(kept) == ["host_node", "valve", "shelf"]


def test_root_device_without_support_for_the_backend_stays_in_the_host(
    host: Path, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setitem(
        lab_registry.device_type_registry,
        "demo_valve",
        {"class": {"module": "acme.valve:Valve", "type": "python", "supported_backends": ["ros2"]}},
    )
    _, kept = _prepare(tmp_path, _payload())
    # Host 的 runtime 像今天一样把它登记为不可用并给出原因
    assert _roots(kept) == ["host_node", "valve", "shelf"]
    assert set(dpx.load_specs(host)) == {"root-station"}


def test_flag_needs_hostlink(host: Path, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(HostLinkConfig, "enable", False)
    full, kept = _prepare(tmp_path, _payload())
    assert kept is full and not dpx.spec_path(host).exists()


def test_running_service_stops_slaves_of_removed_devices(host: Path, tmp_path: Path, monkeypatch) -> None:
    """Host 已在运行时再同步（服务单例存在）：被移除规格的进程先停掉，运行态一并清理。"""

    _prepare(tmp_path, _payload())
    service = dpx.DeviceProcessService(host)
    monkeypatch.setattr(dpx, "_service", service)
    stopped: list[str] = []

    class Running:
        pid = 4242

        def poll(self):
            return None if not stopped else 0

    service._procs["root-valve"] = Running()
    monkeypatch.setattr(service, "stop", lambda process_id, wait=True: stopped.append(process_id))
    changed = _payload()
    changed["nodes"] = [node for node in changed["nodes"] if node["id"] != "valve"]
    _prepare(tmp_path, changed)
    assert stopped == ["root-valve"]
    assert set(dpx.load_specs(host)) == {"root-station"}


def test_flag_only_applies_to_a_device_host(capsys) -> None:
    for args in ({"root_devices_as_slaves": True, "is_slave": True}, {"root_devices_as_slaves": True, "role": "backend"}):
        assert rds.root_devices_as_slaves_enabled(args) is False
        assert args["root_devices_as_slaves"] is False
    assert capsys.readouterr().out.count("--root_devices_as_slaves 只对 Host 生效，已忽略") == 2
    assert rds.root_devices_as_slaves_enabled({"root_devices_as_slaves": True, "role": "auto"}) is True
    assert rds.root_devices_as_slaves_enabled({}) is False

    host_args = {"graph_is_authority_payload": True}
    assert rds.graph_is_authority_payload_enabled(host_args) is False and host_args["graph_is_authority_payload"] is False
    assert "只对 Slave 生效" in capsys.readouterr().out
    assert rds.graph_is_authority_payload_enabled({"graph_is_authority_payload": True, "is_slave": True}) is True


def test_authority_payload_file_check(tmp_path: Path) -> None:
    good = tmp_path / "good.json"
    good.write_text(json.dumps({"nodes": [_device("pump", 3, template="demo_pump")], "links": []}), encoding="utf-8")
    assert rds.check_authority_payload_file(str(good)) is None

    draft = tmp_path / "draft.json"
    node = _device("pump", 3, template="demo_pump")
    node.pop("uuid")
    draft.write_text(json.dumps({"nodes": [node]}), encoding="utf-8")
    assert "pump" in (rds.check_authority_payload_file(str(draft)) or "")

    broken = tmp_path / "broken.json"
    broken.write_text("[]", encoding="utf-8")
    assert rds.check_authority_payload_file(str(broken))
    odd = tmp_path / "odd.json"
    odd.write_text(json.dumps({"nodes": ["pump"]}), encoding="utf-8")
    assert rds.check_authority_payload_file(str(odd))


def test_cli_accepts_both_flags() -> None:
    from unilabos.app.cli.parser import build_parser

    args = build_parser().parse_args(["--root-devices-as-slaves", "--graph_is_authority_payload"])
    assert args.root_devices_as_slaves is True and args.graph_is_authority_payload is True
    defaults = build_parser().parse_args([])
    assert defaults.root_devices_as_slaves is False and defaults.graph_is_authority_payload is False
