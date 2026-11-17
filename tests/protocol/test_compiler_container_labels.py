"""协议编译器按图节点 id 找容器；模糊匹配的辅助信号是人读标签 display_name（物料 name 是 uuid）。"""

from __future__ import annotations

from uuid import uuid4

import networkx as nx

from unilabos.experiments.compile.adjustph_protocol import find_acid_base_vessel
from unilabos.experiments.compile.reset_handling_protocol import find_solvent_vessel


def _graph(node_id: str, label: str) -> nx.DiGraph:
    graph = nx.DiGraph()
    identity = str(uuid4())
    graph.add_node(node_id, type="container", name=identity, display_name=label, data={})
    return graph


def test_fuzzy_container_lookup_matches_the_display_label() -> None:
    assert find_acid_base_vessel(_graph("vessel_7", "NaOH 储液瓶"), "sodium hydroxide") == "vessel_7"
    assert find_solvent_vessel(_graph("vessel_8", "乙醇 储液瓶"), "乙醇") == "vessel_8"
