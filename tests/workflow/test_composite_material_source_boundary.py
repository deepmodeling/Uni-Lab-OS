"""MaterialSource 准入输出不能冒充子操作的设备 ready 入口。"""

from types import SimpleNamespace

from unilabos.workflow.composite_expansion import _structural_mappings


def test_material_admission_edge_keeps_first_executable_child_entry():
    actions = {
        "source": SimpleNamespace(
            template={"node_type": "material_source"},
            handles=[{"handle_key": "material", "io_type": "source", "uuid": "material"}],
        ),
        "command": SimpleNamespace(
            template={"node_type": "workflow_command"},
            handles=[
                {"handle_key": "ready", "io_type": direction, "uuid": direction}
                for direction in ("source", "target")
            ],
        ),
    }
    catalog = SimpleNamespace(require_template=actions.__getitem__)
    nodes = [
        {"uuid": name, "workflow_node_template_uuid": template}
        for name, template in (("admission", "source"), ("inspect", "command"), ("next", "command"))
    ]
    edges = [
        {"source_node_uuid": source, "target_node_uuid": target}
        for source, target in (("admission", "inspect"), ("inspect", "next"))
    ]
    assert _structural_mappings(nodes, edges, catalog=catalog) == {
        "entry_targets": [{"workflow_node_uuid": "inspect", "target_handle_uuid": "target"}],
        "completion_sources": [{"workflow_node_uuid": "next", "source_handle_uuid": "source"}],
    }
