"""工作流读取必须固定一个设备动作内存目录代际。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from unilabos.workflow.authoring_kernel import AuthoringCatalogSnapshot
from unilabos.workflow.store import WorkflowStore, utc_now

WORKFLOW_UUID = "10000000-0000-4000-8000-000000000001"
NODE_UUID = "20000000-0000-4000-8000-000000000001"
TEMPLATE_UUID = "30000000-0000-4000-8000-000000000001"
RESOURCE_TEMPLATE_UUID = "31000000-0000-4000-8000-000000000001"


@dataclass
class _CountingSnapshotProvider:
    """返回同一不可变目录并记录一次 Store 操作的取快照次数。"""

    catalog: AuthoringCatalogSnapshot
    read_count: int = 0

    def snapshot(self) -> AuthoringCatalogSnapshot:
        """返回目录并递增调用计数。"""

        self.read_count += 1
        return self.catalog


def _catalog() -> AuthoringCatalogSnapshot:
    """构造含一个 ``pump.transfer`` 动作的最小目录。"""

    return AuthoringCatalogSnapshot.from_entities(
        [
            {
                "uuid": TEMPLATE_UUID,
                "resource_template_uuid": RESOURCE_TEMPLATE_UUID,
                "name": "transfer",
                "display_name": "转移",
                "class": "lab.devices:Pump",
                "description": "测试动作",
                "meta_data": {
                    "unilab": {"resource_template": {"name": "pump"}}
                },
                "goal": {},
                "goal_default": {},
                "feedback": {},
                "result": {},
                "schema": None,
                "type": "action",
                "node_type": "compute",
                "icon": None,
                "header": None,
                "footer": None,
            }
        ],
        [],
    )


def test_get_graph_uses_one_catalog_snapshot_for_templates_and_public_nodes(
    tmp_path: Path,
) -> None:
    """单次图读取不得在模板列表和节点投影之间重新取得目录代际。"""

    provider = _CountingSnapshotProvider(_catalog())
    store = WorkflowStore(
        tmp_path / "workflow_history.db",
        template_snapshot_provider=provider,
    )
    try:
        store.create_workflow(
            workflow_uuid=WORKFLOW_UUID,
            name="一致性测试",
            tags=[],
            description=None,
            meta_data={},
        )
        now = utc_now()
        with store.transaction() as connection:
            connection.execute(
                """
                INSERT INTO workflow_node(
                    uuid,create_time,update_time,deleted_at,description,meta_data,
                    workflow_uuid,workflow_node_template_uuid,parent_uuid,
                    material_uuid,name,status,type,icon,pose,param,footer,
                    action_name,action_type,execution_policy,disabled,minimized,script
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    NODE_UUID,
                    now,
                    now,
                    None,
                    None,
                    "{}",
                    WORKFLOW_UUID,
                    "pump.transfer",
                    None,
                    None,
                    "转移",
                    "idle",
                    "action",
                    None,
                    "{}",
                    "{}",
                    None,
                    "transfer",
                    None,
                    "{}",
                    0,
                    0,
                    None,
                ),
            )

        provider.read_count = 0
        graph = store.get_graph(WORKFLOW_UUID)

        assert provider.read_count == 1
        assert graph["nodes"][0]["workflow_node_template_uuid"] == TEMPLATE_UUID
        assert graph["node_templates"][0]["uuid"] == TEMPLATE_UUID
    finally:
        store.close()
