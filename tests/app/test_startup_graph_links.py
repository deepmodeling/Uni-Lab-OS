"""开机图的拓扑边落到持有物料的权威：默认拓扑的 Host 子进程不持有物料，连线要写进权威的 material_link。

起真实 ``unilab``（``slow``）：默认拓扑（调度权威 + Host 子进程）与 ``--no_safe_restart`` 单进程，
三台内置虚拟设备加两条连线，开机后从浏览器连接的那个地址读实时拓扑与库存关系。
"""

from __future__ import annotations

import json
import urllib.request
from pathlib import Path
from typing import Any

import pytest

from tests.app.test_process_lifetime import _Unilab


def _device(node_id: str, template: str) -> dict[str, Any]:
    return {"id": node_id, "name": node_id, "type": "device", "class": template, "template_name": template,
            "parent": None, "pose": {"position": {"x": 0, "y": 0, "z": 0}}, "config": {}, "data": {}}


GRAPH = {
    "nodes": [_device("pump_1", "virtual_transfer_pump"), _device("valve_1", "virtual_multiway_valve"),
              _device("valve_2", "virtual_multiway_valve")],
    "links": [
        {"id": "pump_valve", "source": "pump_1", "target": "valve_1", "type": "fluid",
         "sourceHandle": "transferpump", "targetHandle": "transferpump"},
        {"id": "valve_valve", "source": "valve_1", "target": "valve_2", "type": "fluid",
         "sourceHandle": "1", "targetHandle": "transferpump"},
    ],
}


def _get(port: int, path: str) -> Any:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=10) as response:
        body = json.load(response)
    assert isinstance(body, dict) and body.get("code") == 0, body
    return body.get("data")


@pytest.mark.slow
@pytest.mark.parametrize("topology", ["default", "no_safe_restart"])
def test_startup_graph_links_reach_the_material_authority(tmp_path: Path, topology: str) -> None:
    graph = tmp_path / "linked_lab.json"
    graph.write_text(json.dumps(GRAPH, ensure_ascii=False), encoding="utf-8")
    expected = {(link["source"], link["target"]) for link in GRAPH["links"]}
    args = ["-g", str(graph), *(["--no_safe_restart"] if topology == "no_safe_restart" else [])]
    unilab = _Unilab(tmp_path, topology, args, root=tmp_path / "db", builtin_devices=True)
    try:
        if topology == "default":
            unilab.host_child_pid()
        unilab.wait_for(lambda: "开机拓扑边对齐" in unilab.text(), "开机拓扑边对齐")
        # 单进程模式先写库、后开 HTTP 端口：等端口能答再读
        live = unilab.wait_for(lambda: _get(unilab.port, "/api/v1/graphs/live/payload"), "实时拓扑可读")
        assert {(link["source"], link["target"]) for link in live["links"]} == expected, unilab.text()[-6000:]
        relations = _get(unilab.port, "/api/v1/inventory/relations")
        assert len(relations["links"]) == len(expected)
        assert f"开机拓扑边对齐完成: 新建 {len(expected)} /" in unilab.text()
    finally:
        unilab.close()
