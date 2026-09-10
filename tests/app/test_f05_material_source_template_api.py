"""F05.1 物料来源（MaterialSource）模板查询接口合同。"""

from __future__ import annotations

from pathlib import Path

from tests.registry.test_f05_material_source_catalog import _projection, _Registry
from unilabos.app.workflow_template_api import WorkflowTemplateQueryService


def test_template_query_requires_explicit_material_source_node_type(
    tmp_path: Path,
) -> None:
    """物料来源（MaterialSource）只应在显式 ``node_type`` 筛选中返回。

    参数说明：``tmp_path`` 提供隔离模板存储。返回：无；通过
    查询服务的后端形状分页与详情结果断言。
    """

    projection = _projection(tmp_path / "workflow_history.db")
    projection.refresh(_Registry())
    service = WorkflowTemplateQueryService(projection)
    # ``explicit_page`` 是作者主动请求的非动作框架节点投影。
    explicit_page = service.list_node_templates(
        limit=20,
        cursor_uuid=None,
        keyword="",
        resource_template_uuid=None,
        action_type="",
        node_type="material_source",
    )
    # ``default_page`` 保留现有动作面板的默认可见类型集，不暗中混入供料边界。
    default_page = service.list_node_templates(
        limit=20,
        cursor_uuid=None,
        keyword="",
        resource_template_uuid=None,
        action_type="",
        node_type="",
    )

    assert [item["node_type"] for item in explicit_page["items"]] == ["material_source"]
    assert default_page["items"] == []
    # ``framework_uuid`` 是列表与详情共用的节点模板稳定身份。
    framework_uuid = explicit_page["items"][0]["uuid"]
    detail = service.get_node_template(framework_uuid)
    assert detail["template"]["uuid"] == framework_uuid
    assert [(item["handle_key"], item["io_type"]) for item in detail["handles"]] == [
        ("material", "source")
    ]
    projection.close()
