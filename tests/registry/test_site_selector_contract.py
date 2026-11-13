"""库位选择器（SiteSelector）静态动作合同的公共行为测试。"""

from __future__ import annotations

import ast
import textwrap
from typing import Any

import pytest

from unilabos.registry.action_contract_schema import (
    ActionContractError,
    parse_action_contract,
)
from unilabos.registry.action_template_projection import (
    ActionTemplateProjectionError,
    compile_action_template_handles,
)
from unilabos.registry.annotations import SiteSelector

_MODULE_NAME = "lab.devices.site_selector"


def _parse_action(source: str) -> Any:
    """通过公开 AST 接缝解析一份动作合同。

    参数说明：``source`` 是不执行、不导入的设备包 Python 源码。返回：规范动作
    合同（Action Contract）；源码非法时保留公共解析器的稳定异常。
    """

    # ``module`` 是待静态分析的设备包语法树，不具有运行时驱动副作用。
    module = ast.parse(textwrap.dedent(source))
    # ``action`` 是本测试唯一的动作定义，也是库位关系的声明边界。
    action = next(
        node
        for node in ast.walk(module)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "action"
    )
    return parse_action_contract(module, action, module_name=_MODULE_NAME)


def test_site_selector_annotation_is_public_and_validates_direct_use() -> None:
    """设备包必须能公开导入库位选择器（SiteSelector）并获得不可变元数据。

    参数：无。返回：无；断言编辑器/类型检查器直接构造时保留完整字段关系和策略，
    而真正合同权威仍由后续静态 AST 编译产生。
    """

    # ``selector`` 是只服务源码工具的注解值，不查询库位（Site）或库存权威。
    selector = SiteSelector(
        owner="warehouse",
        occupant="resource",
        show_occupied=True,
        allow_occupied=False,
    )

    assert selector.owner == "warehouse"
    assert selector.occupant == "resource"
    assert selector.show_occupied is True
    assert selector.allow_occupied is False


def test_action_site_selector_compiles_complete_relation_metadata() -> None:
    """动作库位参数必须生成完整、可供前端和调度门禁消费的关系元数据。

    参数：无。返回：无；断言 owner/occupant 均保持显式物料占位符
    （ResourceSlot）字段身份，且已占用库位（Site）的展示和选择策略不漂移。
    """

    contract = _parse_action(
        """
        from typing import Annotated
        from unilabos.registry.annotations import SiteSelector
        from unilabos.registry.placeholder_type import ResourceSlot

        def action(
            warehouse: ResourceSlot,
            resource: ResourceSlot,
            site: Annotated[
                str | None,
                SiteSelector(
                    owner="warehouse",
                    occupant="resource",
                    show_occupied=True,
                    allow_occupied=False,
                ),
            ] = None,
        ) -> None:
            pass
        """
    )

    # ``site_schema`` 是动作输入的规范库位选择器（SiteSelector）wire 合同。
    site_schema = contract.to_action_schema(action_name="action")["properties"]["goal"][
        "properties"
    ]["site"]
    assert site_schema == {
        "type": ["string", "null"],
        "default": None,
        "format": "uuid",
        "x-unilabos-editor-control": "site_selector",
        "x-unilabos-site-selector": {
            "version": 1,
            "owner": "warehouse",
            "occupant": "resource",
            "show_occupied": True,
            "allow_occupied": False,
        },
    }


@pytest.mark.parametrize(
    ("owner_annotation", "occupant_annotation", "owner", "occupant"),
    [
        pytest.param(
            "ResourceSlot", "ResourceSlot", "missing", "resource", id="missing-owner"
        ),
        pytest.param("str", "ResourceSlot", "warehouse", "resource", id="scalar-owner"),
        pytest.param(
            "ResourceSlot", "str", "warehouse", "resource", id="scalar-occupant"
        ),
    ],
)
def test_action_site_selector_rejects_unknown_or_non_material_relations(
    owner_annotation: str,
    occupant_annotation: str,
    owner: str,
    occupant: str,
) -> None:
    """owner/occupant 必须显式指向单个物料占位符（ResourceSlot）输入。

    参数说明：两个 annotation 参数构造字段类型反例；``owner`` 与 ``occupant``
    构造关系反例。返回：无；断言静态编译关闭式失败，不按字段名猜测物料关系。
    """

    source = f"""
        from typing import Annotated
        from unilabos.registry.annotations import SiteSelector
        from unilabos.registry.placeholder_type import ResourceSlot

        def action(
            warehouse: {owner_annotation},
            resource: {occupant_annotation},
            site: Annotated[
                str,
                SiteSelector(
                    owner={owner!r},
                    occupant={occupant!r},
                    show_occupied=True,
                    allow_occupied=False,
                ),
            ],
        ) -> None:
            pass
    """
    with pytest.raises(ActionContractError) as caught:
        _parse_action(source)
    assert caught.value.code == "invalid_annotation"
    assert "site_selector" in caught.value.path


@pytest.mark.parametrize(
    ("site_import", "site_annotation"),
    [
        pytest.param(
            "from unilabos.registry.annotations import SiteSelector",
            "Annotated[int, SiteSelector(owner='warehouse')]",
            id="non-string-site",
        ),
        pytest.param(
            "from unilabos.registry.annotations import SiteSelector",
            "Annotated[str, SiteSelector('warehouse')]",
            id="positional-relation",
        ),
        pytest.param(
            "from unilabos.registry.annotations import SiteSelector",
            "Annotated[str, SiteSelector(owner=OWNER)]",
            id="dynamic-relation",
        ),
        pytest.param(
            "from malicious.annotations import SiteSelector",
            "Annotated[str, SiteSelector(owner='warehouse')]",
            id="forged-import",
        ),
    ],
)
def test_action_site_selector_rejects_unsafe_annotation_shapes(
    site_import: str,
    site_annotation: str,
) -> None:
    """库位选择器（SiteSelector）必须只接受真实导入和静态命名参数。

    参数说明：``site_import`` 构造真实或伪造导入；``site_annotation`` 构造类型、
    位置参数或动态表达式反例。返回：无；断言编译关闭式失败且不执行作者代码。
    """

    source = f"""
        from typing import Annotated
        {site_import}
        from unilabos.registry.placeholder_type import ResourceSlot

        OWNER = "warehouse"

        def action(
            warehouse: ResourceSlot,
            site: {site_annotation},
        ) -> None:
            pass
    """
    with pytest.raises(ActionContractError) as caught:
        _parse_action(source)
    assert caught.value.code == "invalid_annotation"


def test_action_site_selector_allows_omitting_optional_occupant_relation() -> None:
    """只依赖 owner 的库位选择器（SiteSelector）应规范化 occupant 为 null。

    参数：无。返回：无；断言移除/检查等动作不需伪造待放入物料字段，仍明确拥有者
    和默认已占用库位（Site）策略。
    """

    contract = _parse_action(
        """
        from typing import Annotated
        from unilabos.registry.annotations import SiteSelector
        from unilabos.registry.placeholder_type import ResourceSlot

        def action(
            warehouse: ResourceSlot,
            site: Annotated[str, SiteSelector(owner="warehouse")],
        ) -> None:
            pass
        """
    )
    # ``selector`` 是缺省 occupant 后仍完整的规范库位关系扩展。
    selector = contract.to_action_schema(action_name="action")["properties"]["goal"][
        "properties"
    ]["site"]["x-unilabos-site-selector"]
    assert selector == {
        "version": 1,
        "owner": "warehouse",
        "occupant": None,
        "show_occupied": True,
        "allow_occupied": False,
    }


def test_action_template_handle_projects_complete_site_selector_metadata() -> None:
    """库位输入连接点必须直接投影完整库位选择器（SiteSelector）合同。

    参数：无。返回：无；断言前端无需重新解释动作根 Schema，也不依赖参数名猜测
    owner/occupant 关系；连接点值模式仍保留相同扩展作为行为权威。
    """

    contract = _parse_action(
        """
        from typing import Annotated
        from unilabos.registry.annotations import SiteSelector
        from unilabos.registry.placeholder_type import ResourceSlot

        def action(
            warehouse: ResourceSlot,
            resource: ResourceSlot,
            site: Annotated[
                str,
                SiteSelector(
                    owner="warehouse",
                    occupant="resource",
                    show_occupied=True,
                    allow_occupied=False,
                ),
            ],
        ) -> None:
            pass
        """
    )
    # ``handles`` 是前端查询使用的动作模板连接点全集，UUID 由持久投影稍后分配。
    handles = compile_action_template_handles(
        contract.to_action_schema(action_name="action"),
        node_business_key=("owner-template", "action"),
        resource_template_identity_resolver=None,
    )
    # ``site_handle`` 是本测试唯一库位（Site）输入连接点。
    site_handle = next(
        handle
        for handle in handles
        if handle["io_type"] == "target" and handle["handle_key"] == "site"
    )
    assert site_handle["meta_data"]["unilab"]["site_selector"] == {
        "version": 1,
        "owner": "warehouse",
        "occupant": "resource",
        "show_occupied": True,
        "allow_occupied": False,
    }
    assert (
        site_handle["meta_data"]["unilab"]["value_schema"]["x-unilabos-site-selector"]
        == site_handle["meta_data"]["unilab"]["site_selector"]
    )


@pytest.mark.parametrize(
    ("field_name", "schema_change", "message"),
    [
        pytest.param(
            "site",
            {"x-unilabos-site-selector": None},
            "库位选择控件缺少完整库位选择合同",
            id="missing-site-selector-contract",
        ),
        pytest.param(
            "site",
            {"x-unilabos-site-selector": "warehouse"},
            "库位选择控件缺少完整库位选择合同",
            id="non-object-site-selector-contract",
        ),
        pytest.param(
            "site",
            {
                "x-unilabos-editor-control": "variable_selector",
                "x-unilabos-site-selector": {
                    "version": 1,
                    "owner": "warehouse",
                    "occupant": None,
                    "show_occupied": True,
                    "allow_occupied": False,
                },
            },
            "非库位选择控件不能携带库位选择合同",
            id="extension-on-non-site-control",
        ),
        pytest.param(
            "warehouse",
            {
                "x-unilabos-editor-control": "site_selector",
                "x-unilabos-site-selector": {
                    "version": 1,
                    "owner": "warehouse",
                    "occupant": None,
                    "show_occupied": True,
                    "allow_occupied": False,
                },
            },
            "非库位选择控件不能携带库位选择合同",
            id="extension-on-material-port",
        ),
    ],
)
def test_action_template_projection_rejects_invalid_site_selector_extensions(
    field_name: str,
    schema_change: dict[str, object],
    message: str,
) -> None:
    """连接点投影必须对不完整或错位的库位选择合同失败关闭。

    参数说明：``field_name`` 选择待篡改字段；``schema_change`` 构造库位选择
    （Site Selection）扩展反例；``message`` 是稳定中文合同错误。返回：无；断言
    投影器不降级或猜测。
    """

    contract = _parse_action(
        """
        from typing import Annotated
        from unilabos.registry.annotations import SiteSelector
        from unilabos.registry.placeholder_type import ResourceSlot

        def action(
            warehouse: ResourceSlot,
            site: Annotated[str, SiteSelector(owner="warehouse")],
        ) -> None:
            pass
        """
    )
    # ``action_schema`` 是待投影的规范动作合同副本。
    action_schema = contract.to_action_schema(action_name="action")
    # ``field_schema`` 是本轮待篡改的库位或物料字段模式。
    field_schema = action_schema["properties"]["goal"]["properties"][field_name]
    for key, value in schema_change.items():
        if value is None:
            field_schema.pop(key, None)
        else:
            field_schema[key] = value

    with pytest.raises(ActionTemplateProjectionError, match=message):
        compile_action_template_handles(
            action_schema,
            node_business_key=("owner-template", "action"),
            resource_template_identity_resolver=None,
        )
