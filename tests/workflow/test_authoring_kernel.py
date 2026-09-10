"""可信工作流创作内核（Authoring Kernel）的纯函数合同测试。"""

from __future__ import annotations

from uuid import UUID

import pytest

from unilabos.workflow.authoring_identity import authoring_edge_uuid
from unilabos.workflow.authoring_kernel import (
    AuthoringCatalogError,
    AuthoringCatalogSnapshot,
)
from unilabos.workflow.source_coordinates import (
    codepoint_offset_to_utf16_column,
    require_utf8_text,
    source_ranges_fit,
)


def _node_template() -> dict[str, object]:
    """构造一个测试用动作节点模板（NodeTemplate）。

    返回值：包含稳定模板身份、动作业务名与设备类身份的最小目录投影。
    """

    return {
        "uuid": "30000000-0000-4000-8000-000000000001",
        "resource_template_uuid": "31000000-0000-4000-8000-000000000001",
        "name": "prepare",
        "display_name": "Prepare",
        "class": "lab.devices:Reactor",
        "description": "Prepare sample",
        "meta_data": {"owner": "test"},
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


def _handle_template() -> dict[str, object]:
    """构造一个测试用连接点（Handle）模板。

    返回值：属于 ``prepare`` 动作的必填目标连接点投影。
    """

    return {
        "uuid": "40000000-0000-4000-8000-000000000001",
        "workflow_node_template_uuid": "30000000-0000-4000-8000-000000000001",
        "handle_key": "sample",
        "io_type": "target",
        "display_name": "Sample",
        "type": "ResourceSlot",
        "required": True,
        "data_source": "executor",
        "data_key": "sample",
        "description": None,
        "meta_data": {"unilab": {"value_schema": {"$slot": "ResourceSlot"}}},
    }


def test_catalog_snapshot_is_detached_and_fingerprinted() -> None:
    """目录快照（Catalog Snapshot）必须与调用方容器隔离且指纹稳定。"""

    node = _node_template()
    handle = _handle_template()
    snapshot = AuthoringCatalogSnapshot.from_entities([node], [handle])
    original_fingerprint = snapshot.fingerprint

    node["name"] = "mutated"
    handle["handle_key"] = "mutated"

    action = snapshot.require_action("lab.devices:Reactor", "prepare")
    assert action.template["name"] == "prepare"
    assert action.handles[0]["handle_key"] == "sample"
    assert snapshot.fingerprint == original_fingerprint
    assert original_fingerprint.startswith("sha256:")


@pytest.mark.parametrize(
    "unsafe_source_identity",
    (
        "lab.resources\nfrom os import system:plate_96",
        "lab.bad-module:plate_96",
        "lab.class:plate_96",
        "lab..resources:plate_96",
    ),
    ids=("control-character", "invalid-segment", "keyword-segment", "empty-segment"),
)
def test_catalog_snapshot_rejects_unsafe_resource_source_identity(
    unsafe_source_identity: str,
) -> None:
    """创作目录快照必须独立拒绝不能安全 import 的资源源码身份。

    参数说明：``unsafe_source_identity`` 是单个不可信 ``source_fqid``。返回：
    无；断言目录快照（CatalogSnapshot）在构造边界关闭失败。
    """

    with pytest.raises(AuthoringCatalogError):
        AuthoringCatalogSnapshot.from_entities(
            [_node_template()],
            [_handle_template()],
            resource_template_symbols={
                unsafe_source_identity: "31000000-0000-4000-8000-000000000002"
            },
        )


def test_authoring_edge_uuid_is_stable_and_endpoint_sensitive() -> None:
    """创作边（Authoring Edge）的 UUID 必须确定且随端点身份变化。"""

    first = authoring_edge_uuid(
        workflow_uuid="10000000-0000-4000-8000-000000000001",
        source_node_uuid="20000000-0000-4000-8000-000000000001",
        source_handle_uuid="40000000-0000-4000-8000-000000000001",
        target_node_uuid="20000000-0000-4000-8000-000000000002",
        target_handle_uuid="40000000-0000-4000-8000-000000000002",
    )
    repeated = authoring_edge_uuid(
        workflow_uuid="10000000-0000-4000-8000-000000000001",
        source_node_uuid="20000000-0000-4000-8000-000000000001",
        source_handle_uuid="40000000-0000-4000-8000-000000000001",
        target_node_uuid="20000000-0000-4000-8000-000000000002",
        target_handle_uuid="40000000-0000-4000-8000-000000000002",
    )
    changed = authoring_edge_uuid(
        workflow_uuid="10000000-0000-4000-8000-000000000001",
        source_node_uuid="20000000-0000-4000-8000-000000000001",
        source_handle_uuid="40000000-0000-4000-8000-000000000001",
        target_node_uuid="20000000-0000-4000-8000-000000000003",
        target_handle_uuid="40000000-0000-4000-8000-000000000002",
    )

    assert first == repeated
    assert first != changed
    assert UUID(first).version == 5


def test_source_coordinates_follow_frontend_utf16_columns() -> None:
    """源码坐标必须按前端使用的 UTF-16 单元计数。"""

    # ``😀`` 在 Python 字符索引中占一位，在 UTF-16 中占两个编码单元。
    line = "物😀a"
    assert codepoint_offset_to_utf16_column(line, 0) == 1
    assert codepoint_offset_to_utf16_column(line, 1) == 2
    assert codepoint_offset_to_utf16_column(line, 2) == 4
    assert codepoint_offset_to_utf16_column(line, 3) == 5
    assert source_ranges_fit(
        line,
        [
            {
                "start_line": 1,
                "start_column": 2,
                "end_line": 1,
                "end_column": 4,
            }
        ],
    )


def test_unpaired_surrogate_is_not_trusted_authoring_text() -> None:
    """不能编码为 UTF-8 的源码必须在纯函数边界失败关闭。"""

    with pytest.raises(ValueError, match="UTF-8"):
        require_utf8_text("bad\ud800source")
