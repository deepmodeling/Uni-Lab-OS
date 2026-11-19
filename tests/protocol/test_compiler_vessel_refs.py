"""协议编译器给设备动作传容器统一走 ``vessel_ref``：带物理拓扑图里的 uuid（ResourceSlot 引用），
设备节点与空值留空；编译器源码里不再手拼 ``{"id": ...}``。"""

from __future__ import annotations

import re
from pathlib import Path
from uuid import uuid4

import networkx as nx

from unilabos.experiments.compile.utils.vessel_parser import vessel_ref

COMPILE_DIR = Path(__file__).resolve().parents[2] / "unilabos" / "experiments" / "compile"


def _graph() -> tuple[nx.Graph, str]:
    graph = nx.Graph()
    flask_uuid = str(uuid4())
    graph.add_node("flask", type="container", uuid=flask_uuid, display_name="反应瓶")
    graph.add_node("filter_1", type="device", uuid=str(uuid4()))
    graph.add_node("bare", type="container")
    return graph, flask_uuid


def test_vessel_ref_carries_the_graph_uuid_and_keeps_the_id() -> None:
    graph, flask_uuid = _graph()
    expected = {"id": "flask", "uuid": flask_uuid}
    assert vessel_ref(graph, "flask") == expected
    # 协议参数里的容器字典（权威展开后的完整资源 dict、外包一层的旧形态）同样取图节点 id
    assert vessel_ref(graph, {"id": "flask", "uuid": flask_uuid, "data": {}}) == expected
    assert vessel_ref(graph, {"vessel": {"id": "flask"}}) == expected


def test_vessel_ref_leaves_empty_values_and_devices_unset() -> None:
    graph, _ = _graph()
    for empty in (None, "", {}, {"id": ""}):
        assert vessel_ref(graph, empty) is None
    # 过滤器、旋蒸本身是设备，不是物料：容器参数留空，框架不解析、不加锁
    assert vessel_ref(graph, "filter_1") is None


def test_vessel_ref_passes_unknown_names_through_for_the_framework_to_resolve() -> None:
    graph, _ = _graph()
    assert vessel_ref(graph, "bare") == {"id": "bare"}
    assert vessel_ref(graph, "elsewhere") == {"id": "elsewhere"}
    other = str(uuid4())
    assert vessel_ref(graph, {"id": "elsewhere", "uuid": other}) == {"id": "elsewhere", "uuid": other}
    assert vessel_ref(None, {"uuid": other}) == {"uuid": other}


def test_compilers_build_device_vessel_arguments_through_the_shared_helper() -> None:
    hand_built = re.compile(r'"(?:vessel|from_vessel|to_vessel|filtrate_vessel)"\s*:\s*\{\s*"id"')
    offenders = [
        f"{path.name}:{number}"
        for path in sorted(COMPILE_DIR.glob("*.py"))
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if hand_built.search(line.split("#", 1)[0])
    ]
    assert offenders == []
