"""物料来源（MaterialSource）创作语法、图投影与源码生成深模块。"""

from __future__ import annotations

import ast
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from unilabos.workflow.authoring_kernel import (
    AuthoringCatalogAction,
    AuthoringCatalogError,
    AuthoringCatalogSnapshot,
)
from unilabos.workflow.material_selector import (
    MATERIAL_CUSTODY_POLICY_MEMBERS,
    MATERIAL_CUSTODY_POLICY_VALUES,
    MATERIAL_FLOW_ROLE_MEMBERS,
    MATERIAL_FLOW_ROLE_VALUES,
    MaterialSelectorError,
    validate_canonical_uuid,
    validate_material_source_node,
    validate_material_source_selector,
)
from unilabos.workflow.resource_reference import (
    ResourceReferenceResolutionError,
    ResourceReferenceResolver,
    resolve_resource_reference,
)
from unilabos.workflow.source_identity import (
    PythonSourceIdentityError,
    validate_python_source_identity,
)

_AUTHORING_MODULE = "unilabos.workflow.authoring"
_MATERIAL_SOURCE = f"{_AUTHORING_MODULE}:material_source"
_MATERIAL_FLOW_ROLE = f"{_AUTHORING_MODULE}:MaterialFlowRole"
_MATERIAL_CUSTODY_POLICY = f"{_AUTHORING_MODULE}:MaterialCustodyPolicy"
_RESOURCE_REF = f"{_AUTHORING_MODULE}:resource_ref"
_SELECTOR_FIELDS = frozenset(
    {
        "resource_template",
        "mode",
        "mount",
        "material_uuid",
        "site",
        "slot_range",
        "flow_role",
    }
)
_OPTIONAL_SELECTOR_FIELDS = frozenset({"custody_policy", "quantity_parameter", "quantity_unit"})


class MaterialAuthoringError(ValueError):
    """物料来源（MaterialSource）不能安全编译或生成。"""

    def __init__(self, code: str, message: str, node: ast.AST | None = None):
        """保存稳定诊断、中文消息和可选源码位置。

        参数说明：``code`` 供接口判断错误类别，``message`` 供用户理解，
        ``node`` 是静态 AST（抽象语法树）位置。返回：无；构造异常对象。
        """

        super().__init__(message)
        self.code = code
        self.message = message
        self.node = node


@dataclass(frozen=True, slots=True)
class MaterialSourceDeclaration:
    """一个物料来源（MaterialSource）节点的静态作者声明。"""

    node_uuid: str
    result_name: str
    title: str | None
    description: str | None
    resource_template_symbol: str
    mode: str
    mount_resource_id: str
    material_uuid: str | None
    site: str | None
    slot_range: tuple[str, ...] | None
    flow_role: str
    custody_policy: str | None
    quantity_parameter: str | None
    quantity_unit: str
    source_node: ast.Assign
    arguments: tuple[tuple[str, Any], ...] = ()


@dataclass(frozen=True, slots=True)
class RenderedMaterialSource:
    """物料来源（MaterialSource）的确定性 import 与调用表达式。"""

    resource_import: tuple[str, str]
    call: str


def parse_material_source_declaration(
    statement: ast.stmt,
    *,
    imports: Mapping[str, str],
    anchors: Mapping[int, str],
    node_metadata: Mapping[int, tuple[str, str]],
) -> MaterialSourceDeclaration | None:
    """识别并静态解析一条物料来源（MaterialSource）声明。

    参数说明：``statement`` 是函数体语句，``imports`` 是局部导入身份表，
    ``anchors`` 与 ``node_metadata`` 提供节点身份和展示覆盖。返回：非物料来源
    调用时为 ``None``，否则返回完整声明；选择器不合法时抛出
    ``MaterialAuthoringError``，绝不 import 或执行作者源码。
    """

    if not isinstance(statement, ast.Assign) or len(statement.targets) != 1:
        return None
    call = statement.value
    if (
        not isinstance(call, ast.Call)
        or not isinstance(call.func, ast.Name)
        or imports.get(call.func.id) != _MATERIAL_SOURCE
    ):
        return None
    target = statement.targets[0]
    if not isinstance(target, ast.Name):
        _fail("物料来源必须赋值给一个新名称", statement)
    if call.args:
        _fail("物料来源只接受命名参数", call)
    # ``keywords`` 是七个必需字段；占用策略和数量绑定是可选扩展。
    keywords: dict[str, ast.expr] = {}
    for item in call.keywords:
        if item.arg is None or item.arg in keywords:
            _fail("物料来源参数重复或包含 ** 展开", call)
        keywords[item.arg] = item.value
    keyword_fields = set(keywords)
    if not (
        _SELECTOR_FIELDS.issubset(keyword_fields)
        and keyword_fields - _SELECTOR_FIELDS <= _OPTIONAL_SELECTOR_FIELDS
    ):
        _fail("物料来源必须完整声明规范选择器字段", call)

    resource_expression = keywords["resource_template"]
    if not isinstance(resource_expression, ast.Name):
        _fail("资源模板必须使用显式导入的静态符号", resource_expression)
    resource_symbol = imports.get(resource_expression.id)
    if not isinstance(resource_symbol, str) or ":" not in resource_symbol:
        _fail("资源模板必须使用显式导入的静态符号", resource_expression)
    mode = _literal_string(keywords["mode"], label="物料来源模式")
    if mode not in {"existing", "create_new"}:
        _fail("物料来源模式必须是 existing 或 create_new", keywords["mode"])
    mount_resource_id = _resource_ref_id(keywords["mount"], imports=imports)
    material_uuid = _optional_uuid(
        keywords["material_uuid"],
        label="固定物料 UUID",
    )
    site = _optional_uuid(keywords["site"], label="库位（Site）UUID")
    slot_range = _optional_uuid_list(
        keywords["slot_range"],
        label="库位（Slot）范围",
    )
    if site is not None and slot_range is not None:
        _fail("物料来源不能同时选择库位（Site）和库位（Slot）范围", call)
    if mode == "create_new" and material_uuid is not None:
        _fail("新建物料来源不能预先绑定物料 UUID", keywords["material_uuid"])
    flow_role = _flow_role(keywords["flow_role"], imports=imports)
    custody_policy = (
        _custody_policy(keywords["custody_policy"], imports=imports)
        if "custody_policy" in keywords
        else None
    )
    quantity_parameter = (
        _optional_parameter_name(keywords["quantity_parameter"])
        if "quantity_parameter" in keywords
        else None
    )
    quantity_unit = (
        _literal_string(keywords["quantity_unit"], label="物料数量单位")
        if "quantity_unit" in keywords
        else ""
    )
    if "quantity_unit" in keywords and "quantity_parameter" not in keywords:
        _fail("物料数量单位必须与 quantity_parameter 一起声明", keywords["quantity_unit"])
    node_uuid = anchors.get(statement.lineno - 1)
    if node_uuid is None:
        _fail("每个物料来源前必须有相邻节点 UUID 锚点", statement)
    metadata = node_metadata.get(statement.lineno - 1)
    return MaterialSourceDeclaration(
        node_uuid=node_uuid,
        result_name=target.id,
        title=metadata[0] if metadata is not None else None,
        description=metadata[1] if metadata is not None else None,
        resource_template_symbol=resource_symbol,
        mode=mode,
        mount_resource_id=mount_resource_id,
        material_uuid=material_uuid,
        site=site,
        slot_range=slot_range,
        flow_role=flow_role,
        custody_policy=custody_policy,
        quantity_parameter=quantity_parameter,
        quantity_unit=quantity_unit,
        source_node=statement,
    )


def build_material_source_node(
    declaration: MaterialSourceDeclaration,
    *,
    catalog: AuthoringCatalogSnapshot,
    resource_reference_resolver: ResourceReferenceResolver | None = None,
) -> tuple[dict[str, Any], AuthoringCatalogAction]:
    """把静态物料来源声明投影为后端形状节点。

    参数说明：``declaration`` 是可信 AST 结果，``catalog`` 是同代不可变目录。
    返回：候选节点与框架模板 aggregate；模板或资源身份缺失时抛出带
    ``resource_reference_resolver`` 把挂载业务 ID 解析成实际物料 UUID。返回候选
    节点与框架模板 aggregate；模板、资源或物料身份缺失时抛出稳定的
    ``MaterialAuthoringError``。
    """

    try:
        framework = catalog.require_material_source()
        resource_template_uuid = catalog.require_resource_template_uuid(
            declaration.resource_template_symbol
        )
    except AuthoringCatalogError as error:
        raise MaterialAuthoringError(
            "template_catalog_mismatch",
            "物料来源引用的框架或资源模板身份不在当前目录代际",
            declaration.source_node,
        ) from error
    try:
        # ``mount_reference`` 是库存权威证明的实际挂载物料身份；业务 ID 只进入
        # 保留源码元数据，绝不冒充候选选择器中的 UUID。
        mount_reference = resolve_resource_reference(
            declaration.mount_resource_id,
            resource_reference_resolver,
        )
    except ResourceReferenceResolutionError as error:
        # ``diagnostic_code`` 在无解析器的旧纯编译路径保留已发布诊断；注入库存
        # 解析器后的身份失败使用新的稳定资源解析诊断。
        diagnostic_code = (
            "invalid_material_source"
            if resource_reference_resolver is None
            else "resource_reference_resolution_error"
        )
        raise MaterialAuthoringError(
            diagnostic_code,
            str(error),
            declaration.source_node,
        ) from error
    # ``selector`` 是后续运行时准入和预留消费的完整、稳定选择器事实。
    selector = {
        "mode": declaration.mode,
        "resource_template_uuid": resource_template_uuid,
        "mount": {"uuid": mount_reference["uuid"]},
        "material_uuid": declaration.material_uuid,
        "site": declaration.site,
        "slot_range": (
            list(declaration.slot_range) if declaration.slot_range is not None else None
        ),
        "flow_role": declaration.flow_role,
    }
    try:
        selector = validate_material_source_selector(selector)
    except MaterialSelectorError as error:
        raise MaterialAuthoringError(
            error.code,
            error.message,
            declaration.source_node,
        ) from error
    template = framework.template
    template_title = template.get("display_name") or template.get("name")
    node = {
        "uuid": declaration.node_uuid,
        "workflow_node_template_uuid": str(template["uuid"]),
        "parent_uuid": None,
        "material_uuid": None,
        "name": declaration.title or template_title,
        "type": "material_source",
        "icon": template.get("icon"),
        "pose": {},
        "param": selector,
        "footer": template.get("footer"),
        "action_name": "material_source",
        "action_type": None,
        "execution_policy": {},
        "disabled": False,
        "minimized": False,
        "script": None,
        "description": (
            declaration.description
            if declaration.description is not None
            else template.get("description")
        ),
        "meta_data": {
            "unilab": {
                "input_bindings": {},
                "authoring_result_name": declaration.result_name,
                "resource_refs": {
                    "mount": {"resource_id": declaration.mount_resource_id}
                },
            }
        },
    }
    if declaration.custody_policy is not None:
        node["meta_data"]["unilab"]["custody_policy"] = declaration.custody_policy
    if declaration.quantity_parameter is not None:
        node["meta_data"]["unilab"]["quantity_parameter"] = declaration.quantity_parameter
        node["meta_data"]["unilab"]["quantity_unit"] = declaration.quantity_unit
    return node, framework


def render_material_source_call(
    node: Mapping[str, Any],
    *,
    catalog: AuthoringCatalogSnapshot,
) -> RenderedMaterialSource:
    """从候选节点确定性生成物料来源调用表达式。

    参数说明：``node`` 是后端形状候选节点，``catalog`` 提供资源模板 UUID
    到源码符号的冻结反向映射。返回：资源 import 与单行调用；选择器或双向
    身份不可信时抛出 ``MaterialAuthoringError``。
    """

    try:
        selector = validate_material_source_node(node)
    except MaterialSelectorError as error:
        raise MaterialAuthoringError(error.code, error.message) from error
    try:
        source_symbol = catalog.require_resource_template_symbol(
            selector["resource_template_uuid"]
        )
        # 反向映射后立即再正向确认，防止不互逆的目录适配器污染生成结果。
        if (
            catalog.require_resource_template_uuid(source_symbol)
            != selector["resource_template_uuid"]
        ):
            raise AuthoringCatalogError("资源模板身份映射不互逆")
    except AuthoringCatalogError as error:
        raise MaterialAuthoringError(
            "template_catalog_mismatch",
            "物料来源资源模板 UUID 不能反解为当前源码身份",
        ) from error
    try:
        module, symbol = validate_python_source_identity(source_symbol)
    except PythonSourceIdentityError as error:
        raise MaterialAuthoringError(
            "template_catalog_mismatch",
            "资源模板源码身份不能安全生成 Python import",
        ) from error
    role_member = MATERIAL_FLOW_ROLE_MEMBERS[selector["flow_role"]]
    # ``arguments`` 固定字段顺序，确保同一图跨进程生成完全相同的源码。
    # ``resource_refs`` 保留作者使用的部署业务 ID；旧候选没有该元数据时只生成
    # 已冻结 UUID，保证读取兼容且不反向猜测业务名称。
    unilab = (node.get("meta_data") or {}).get("unilab", {})
    resource_refs = (
        unilab.get("resource_refs", {}) if isinstance(unilab, Mapping) else {}
    )
    mount_binding = (
        resource_refs.get("mount") if isinstance(resource_refs, Mapping) else None
    )
    mount_resource_id = (
        mount_binding.get("resource_id")
        if isinstance(mount_binding, Mapping)
        and isinstance(mount_binding.get("resource_id"), str)
        and mount_binding.get("resource_id")
        else selector["mount"]["uuid"]
    )
    arguments = [
        f"resource_template={symbol}",
        f"mode={selector['mode']!r}",
        f"mount=resource_ref({json.dumps(mount_resource_id, ensure_ascii=False)})",
        f"material_uuid={selector['material_uuid']!r}",
        f"site={selector['site']!r}",
        f"slot_range={selector['slot_range']!r}",
        f"flow_role=MaterialFlowRole.{role_member}",
    ]
    unilab = (node.get("meta_data") or {}).get("unilab", {})
    custody_policy = (
        unilab.get("custody_policy") if isinstance(unilab, Mapping) else None
    )
    if custody_policy is not None:
        policy_member = MATERIAL_CUSTODY_POLICY_MEMBERS[custody_policy]
        arguments.append(f"custody_policy=MaterialCustodyPolicy.{policy_member}")
    quantity_parameter = unilab.get("quantity_parameter") if isinstance(unilab, Mapping) else None
    if quantity_parameter is not None:
        arguments.append(f"quantity_parameter={quantity_parameter!r}")
        arguments.append(f"quantity_unit={str(unilab.get('quantity_unit') or '')!r}")
    return RenderedMaterialSource(
        resource_import=(module, symbol),
        call=f"material_source({', '.join(arguments)})",
    )


def _literal_string(expression: ast.expr, *, label: str) -> str:
    """读取一个非空字符串字面量。

    参数说明：``expression`` 是 AST 表达式，``label`` 用于中文错误说明。
    返回：原字符串；动态值、空值或非字符串抛出 ``MaterialAuthoringError``。
    """

    try:
        value = ast.literal_eval(expression)
    except (TypeError, ValueError):
        _fail(f"{label}必须是字符串字面量", expression)
    if not isinstance(value, str) or not value:
        _fail(f"{label}必须是非空字符串", expression)
    return value


def _optional_parameter_name(expression: ast.expr) -> str | None:
    """解析数量参数名；空字符串表示未绑定任务输入。"""

    value = _literal_string(expression, label="物料数量参数名")
    return value or None


def _resource_ref_id(
    expression: ast.expr,
    *,
    imports: Mapping[str, str],
) -> str:
    """解析 ``resource_ref`` 中的部署资源 ID。

    参数说明：``expression`` 是 mount 表达式，``imports`` 用于证明标记身份。
    返回：非空且无首尾空白的静态资源 ID；动态调用或别名未知时关闭失败。
    """

    if (
        not isinstance(expression, ast.Call)
        or not isinstance(expression.func, ast.Name)
        or imports.get(expression.func.id) != _RESOURCE_REF
        or len(expression.args) != 1
        or expression.keywords
    ):
        _fail("mount 必须调用 resource_ref(资源 ID)", expression)
    return _literal_resource_id(expression.args[0], label="mount 资源 ID")


def _literal_resource_id(expression: ast.expr, *, label: str) -> str:
    """读取一个不带首尾空白的非空资源 ID 字面量。

    参数：``expression`` 是 AST 值，``label`` 用于错误定位。返回原始稳定业务
    ID 或 UUID 字符串；动态值、空值和首尾空白抛出 ``MaterialAuthoringError``。
    """

    try:
        # ``resource_id`` 是作者声明的部署业务身份，尚不是物料 UUID。
        resource_id = ast.literal_eval(expression)
    except (TypeError, ValueError):
        _fail(f"{label}必须是字符串字面量", expression)
    if (
        not isinstance(resource_id, str)
        or not resource_id.strip()
        or resource_id != resource_id.strip()
    ):
        _fail(f"{label}必须是无首尾空白的非空字符串", expression)
    return resource_id


def _flow_role(
    expression: ast.expr,
    *,
    imports: Mapping[str, str],
) -> str:
    """把物料流角色枚举成员解析为 wire 值。

    参数说明：``expression`` 必须是显式导入的 ``MaterialFlowRole`` 成员，
    ``imports`` 证明局部名身份。返回：闭集 wire 值；自由字符串或未知成员失败。
    """

    if (
        not isinstance(expression, ast.Attribute)
        or not isinstance(expression.value, ast.Name)
        or imports.get(expression.value.id) != _MATERIAL_FLOW_ROLE
        or expression.attr not in MATERIAL_FLOW_ROLE_VALUES
    ):
        _fail("物料流角色必须使用 MaterialFlowRole 规范成员", expression)
    return MATERIAL_FLOW_ROLE_VALUES[expression.attr]


def _custody_policy(
    expression: ast.expr,
    *,
    imports: Mapping[str, str],
) -> str:
    """把物料占用策略枚举成员解析为 wire 值。"""

    if (
        not isinstance(expression, ast.Attribute)
        or not isinstance(expression.value, ast.Name)
        or imports.get(expression.value.id) != _MATERIAL_CUSTODY_POLICY
        or expression.attr not in MATERIAL_CUSTODY_POLICY_VALUES
    ):
        _fail("物料占用策略必须使用 MaterialCustodyPolicy 规范成员", expression)
    return MATERIAL_CUSTODY_POLICY_VALUES[expression.attr]


def _optional_uuid(expression: ast.expr, *, label: str) -> str | None:
    """读取 ``None`` 或规范 UUID 字面量。

    参数说明：``expression`` 是 AST 值，``label`` 是中文字段名。返回：规范
    UUID 或 ``None``；其他值抛出 ``MaterialAuthoringError``。
    """

    try:
        value = ast.literal_eval(expression)
        return _validate_optional_uuid_value(value)
    except (TypeError, ValueError):
        _fail(f"{label}必须是 UUID 字面量或 None", expression)


def _optional_uuid_list(
    expression: ast.expr,
    *,
    label: str,
) -> tuple[str, ...] | None:
    """读取 ``None`` 或无重复 UUID 字面量列表。

    参数说明：``expression`` 是 AST 值，``label`` 是中文字段名。返回：排序的
    UUID 元组或 ``None``；空数组、重复或非法身份抛出错误。
    """

    try:
        value = ast.literal_eval(expression)
        validated = _validate_optional_uuid_list_value(value)
    except (TypeError, ValueError):
        _fail(f"{label}必须是无重复 UUID 列表或 None", expression)
    return tuple(validated) if validated is not None else None


def _required_uuid_literal(expression: ast.expr, *, label: str) -> str:
    """读取一个规范非 nil UUID 字符串字面量。

    参数说明：``expression`` 是 AST 值，``label`` 用于中文错误。返回：规范
    UUID；动态值或非法身份抛出 ``MaterialAuthoringError``。
    """

    try:
        value = ast.literal_eval(expression)
        return validate_canonical_uuid(value)
    except (TypeError, ValueError):
        _fail(f"{label}必须是规范 UUID 字符串", expression)


def _validate_optional_uuid_value(value: Any) -> str | None:
    """校验运行中值是否为 ``None`` 或规范 UUID。

    参数说明：``value`` 来自字面量或候选图。返回：规范 UUID 或 ``None``；
    非法值抛出 ``ValueError``，由调用边界转换为稳定领域诊断。
    """

    return None if value is None else validate_canonical_uuid(value)


def _validate_optional_uuid_list_value(value: Any) -> list[str] | None:
    """校验运行中值是否为 ``None`` 或非空无重复 UUID 列表。

    参数：字面量或候选图值。返回排序列表或 ``None``；非法值抛出 ``ValueError``。
    """

    if value is None:
        return None
    if not isinstance(value, list) or not value:
        raise ValueError("库位（Slot）范围必须是非空列表")
    identities = [validate_canonical_uuid(item) for item in value]
    if len(set(identities)) != len(identities):
        raise ValueError("库位（Slot）范围不能重复")
    return sorted(identities)


def _fail(message: str, node: ast.AST | None = None) -> None:
    """抛出稳定物料来源语法诊断。

    参数说明：``message`` 是中文错误，``node`` 是可选 AST 位置。返回：永不
    正常返回，统一抛出 ``invalid_material_source``。
    """

    raise MaterialAuthoringError("invalid_material_source", message, node)


__all__ = [
    "MaterialAuthoringError",
    "MaterialSourceDeclaration",
    "RenderedMaterialSource",
    "build_material_source_node",
    "parse_material_source_declaration",
    "render_material_source_call",
]
