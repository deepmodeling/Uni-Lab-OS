"""工作流节点模板可空读投影的语义固定点回归。"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from unilabos.workflow.authoring_graph_semantics import semantic_graph_equal


def _workflow_graph() -> dict[str, Any]:
    """构造带完整可空字段的最小工作流图；无参数，返回五集合图。"""

    return {
        "workflow": {
            "uuid": "10000000-0000-4000-8000-000000000001",
            "name": "可空节点模板语义回归",
            "description": None,
            "revision": 1,
            "meta_data": {},
            "tags": [],
        },
        "nodes": [],
        "edges": [],
        "node_templates": [
            {
                "uuid": "20000000-0000-4000-8000-000000000001",
                "name": "workflow:child",
                "display_name": "子工作流",
                "description": None,
                "class": None,
                "schema": None,
                "icon": None,
                "header": None,
                "footer": None,
                "meta_data": {},
            }
        ],
        "handle_templates": [],
    }


def test_omitted_nullable_node_template_fields_equal_explicit_nulls() -> None:
    """Backend 省略空模板字段时仍须与编译器显式 ``None`` 语义等价。"""

    explicit = _workflow_graph()
    omitted = deepcopy(explicit)
    template = omitted["node_templates"][0]
    for field_name in (
        "description",
        "class",
        "schema",
        "icon",
        "header",
        "footer",
    ):
        template.pop(field_name)

    assert semantic_graph_equal(explicit, omitted)


def test_omitted_node_template_field_differs_from_non_null_value() -> None:
    """省略字段不得吞掉真实模板展示值变化。"""

    valued = _workflow_graph()
    valued["node_templates"][0]["icon"] = "workflow.svg"
    omitted = _workflow_graph()
    omitted["node_templates"][0].pop("icon")

    assert not semantic_graph_equal(valued, omitted)
