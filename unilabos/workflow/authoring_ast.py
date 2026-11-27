"""可信工作流作者源码的纯 AST（抽象语法树）解析层。"""

from __future__ import annotations

import ast
import math
import re
import tokenize
from collections.abc import Mapping
from dataclasses import dataclass, replace
from io import StringIO
from typing import Any, Never
from uuid import NAMESPACE_URL, uuid5

from unilabos.registry.annotation_schema import (
    NO_DEFAULT,
    AnnotationSchemaError,
    parse_parameter_annotation,
    parse_result_annotation,
)
from unilabos.workflow.authoring_material import (
    MaterialSourceDeclaration,
    parse_material_source_declaration,
)
from unilabos.workflow.authoring_material_io import (
    MaterialIOAuthoringError,
    MaterialIODeclaration,
    parse_material_io_declaration,
)
from unilabos.workflow.models import (
    CandidateSourceMapEntry,
    normalize_json_array,
    normalize_json_object,
    validate_uuid,
)
from unilabos.workflow.source_coordinates import (
    codepoint_offset_to_utf16_column,
    source_lines,
    utf8_offset_to_utf16_column,
    utf16_length,
)
from unilabos.workflow.workflow_type import normalize_workflow_type

_NODE_ANCHOR = re.compile(
    r"^[ \t]*#[ \t]*unilab:node_uuid=([0-9a-fA-F-]{36})"
    r"(?:[ \t]+disabled=(true))?"
    r"(?:[ \t]+manual_confirmation_timeout_seconds=([0-9]{1,5}))?[ \t]*$"
)
_READY_EDGE = re.compile(
    r"^[ \t]*#[ \t]*unilab:ready_edge[ \t]+"
    r"source=([0-9a-fA-F-]{36})[ \t]+target=([0-9a-fA-F-]{36})[ \t]*$"
)
_NODE_METADATA_PREFIX = re.compile(r"^[ \t]*#[ \t]*\[")
_MAX_CONTROL_NESTING_DEPTH = 8
_NODE_METADATA = re.compile(
    r"^(?P<indent>[ \t]*)#[ \t]*\[(?P<title>[^]\r\n]+)\]"
    r"(?:(?:[ \t]*:[ \t]*)|(?:[ \t]+))"
    r"(?P<description>\S(?:.*\S)?)[ \t]*$"
)
_AUTHORING_MARKERS = {
    "device": "unilabos.workflow.authoring:device",
    "group": "unilabos.workflow.authoring:group",
    "material_io": "unilabos.workflow.authoring:material_io",
    "parallel": "unilabos.workflow.authoring:parallel",
    "resources": "unilabos.workflow.authoring:resources",
    "quantity_requirement": "unilabos.workflow.authoring:quantity_requirement",
    "repeat_until": "unilabos.workflow.authoring:repeat_until",
    "service": "unilabos.workflow.authoring:service",
    "site_group": "unilabos.workflow.authoring:site_group",
    "until": "unilabos.workflow.authoring:until",
    "workflow": "unilabos.workflow.authoring:workflow",
    "workflow_definition": "unilabos.workflow.authoring:workflow_definition",
    "workflow_output": "unilabos.workflow.authoring:workflow_output",
}
_RESOURCE_REF = "unilabos.workflow.authoring:resource_ref"
_SITE_GROUP = "unilabos.workflow.authoring:site_group"


class AuthoringSyntaxError(ValueError):
    """可稳定投影为编译诊断的作者源码错误。"""

    def __init__(self, code: str, message: str, node: ast.AST | None = None):
        """保存诊断码、中文消息和可选 AST 节点。

        参数说明：``code`` 是稳定机器码，``message`` 是用户消息，``node`` 用于
        生成源码范围。
        """

        super().__init__(message)
        self.code = code
        self.message = message
        self.node = node


@dataclass(frozen=True, slots=True)
class DeviceDeclaration:
    """静态设备声明及其设备类身份。"""

    symbol: str
    class_identity: str
    device_id: str | None
    selector_kind: str = "device"


@dataclass(frozen=True, slots=True)
class ValueBinding:
    """动作参数或工作流输出的一种静态值绑定。"""

    kind: str
    value: Any
    result_name: str | None = None


@dataclass(frozen=True, slots=True)
class ActionDeclaration:
    """一个持久动作节点的作者声明。"""

    node_uuid: str
    result_name: str
    title: str | None
    description: str | None
    device_symbol: str
    action_name: str
    arguments: tuple[tuple[str, ValueBinding], ...]
    control_dependencies: tuple[str, ...] | None
    source_node: ast.Assign


@dataclass(frozen=True, slots=True)
class CompositeDeclaration:
    """一个绝对导入的已发布工作流调用声明。"""

    node_uuid: str
    result_name: str
    title: str | None
    description: str | None
    module: str
    symbol: str
    arguments: tuple[tuple[str, ValueBinding], ...]
    control_dependencies: tuple[str, ...] | None
    source_node: ast.Assign


@dataclass(frozen=True, slots=True)
class GroupDeclaration:
    """一个只表达展示层级的分组（Group）节点声明。"""

    node_uuid: str
    name: str
    title: str | None
    description: str | None
    parallel_scope: str | None
    parallel_order: int | None
    source_node: ast.With


@dataclass(frozen=True, slots=True)
class ResourceScopeDeclaration:
    """不创建执行节点的结构化资源作用域声明。"""

    scope_id: str
    resources: tuple[str, ...]
    parent_scope_id: str | None
    node_uuids: tuple[str, ...]
    entry_node_uuid: str
    exit_node_uuid: str
    source_node: ast.With | ast.Expr
    kind: str = "with"
    name: str = ""


@dataclass(frozen=True, slots=True)
class ConditionBranchDeclaration:
    """条件区域中的一个有序分支。"""

    label: str
    condition: dict[str, Any] | None
    node_uuids: tuple[str, ...]
    entry_node_uuids: tuple[str, ...]
    exit_node_uuids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ConditionDeclaration:
    """由调度器本地求值的结构化条件区域声明。"""

    node_uuid: str
    title: str | None
    description: str | None
    branches: tuple[ConditionBranchDeclaration, ...]
    bindings: tuple[tuple[str, dict[str, str]], ...]
    source_node: ast.If


@dataclass(frozen=True, slots=True)
class RepeatUntilDeclaration:
    """先执行循环体、再由调度器本地判断退出的结构化区域。"""

    node_uuid: str
    title: str | None
    description: str | None
    loop_variable: str
    max_iterations: int
    initial_carry: tuple[tuple[str, ValueBinding], ...]
    next_carry: tuple[tuple[str, ValueBinding], ...]
    until_condition: dict[str, Any]
    bindings: tuple[tuple[str, dict[str, str]], ...]
    node_uuids: tuple[str, ...]
    entry_node_uuids: tuple[str, ...]
    exit_node_uuids: tuple[str, ...]
    source_node: ast.With


@dataclass(frozen=True, slots=True)
class QuantityRequirementDeclaration:
    """来源容器在指定动作成功时消费的任务级数量需求。"""

    requirement_key: str
    source_result_name: str
    consume_result_name: str
    quantity: ValueBinding
    quantity_unit: str
    scale: float
    description: str | None
    source_node: ast.Expr


@dataclass(frozen=True, slots=True)
class WorkflowProgram:
    """作者源码静态子集解析后的不可变中间表示。"""

    workflow_uuid: str
    function_name: str
    function_docstring: str | None
    display_name: str
    description: str | None
    tags: list[Any] | None
    meta_data: dict[str, Any] | None
    workflow_type: str | None
    root_resources: tuple[str, ...]
    imports: tuple[tuple[str, str], ...]
    devices: tuple[DeviceDeclaration, ...]
    input_contract: dict[str, Any]
    input_resource_template_symbols: tuple[tuple[str, tuple[str, ...]], ...]
    result_record_name: str | None
    declared_output_schemas: tuple[tuple[str, dict[str, Any]], ...]
    declared_output_units: tuple[tuple[str, str], ...]
    output_resource_template_symbols: tuple[tuple[str, tuple[str, ...]], ...]
    actions: tuple[
        ActionDeclaration
        | CompositeDeclaration
        | MaterialSourceDeclaration
        | MaterialIODeclaration,
        ...,
    ]
    groups: tuple[GroupDeclaration, ...]
    resource_scopes: tuple[ResourceScopeDeclaration, ...]
    conditions: tuple[ConditionDeclaration, ...]
    repeats: tuple[RepeatUntilDeclaration, ...]
    quantity_requirements: tuple[QuantityRequirementDeclaration, ...]
    parent_by_node: tuple[tuple[str, str], ...]
    order_dependencies: tuple[tuple[str, str], ...]
    source_order: tuple[str, ...]
    disabled_node_uuids: tuple[str, ...]
    manual_confirmation_timeouts: tuple[tuple[str, int], ...]
    outputs: tuple[tuple[str, ValueBinding], ...]


@dataclass(frozen=True, slots=True)
class _Flow:
    """一段作者源码对执行图公开的入口、出口和新结果名。"""

    entries: tuple[str, ...]
    default_entries: tuple[str, ...]
    exits: tuple[str, ...]
    result_names: frozenset[str]


@dataclass(slots=True)
class _BodyState:
    """工作流函数体静态解析期间唯一的可变收集状态。"""

    imports: dict[str, str]
    devices: dict[str, DeviceDeclaration]
    input_names: set[str]
    anchors: dict[int, str]
    node_metadata: dict[int, tuple[str, str]]
    actions: list[
        ActionDeclaration
        | CompositeDeclaration
        | MaterialSourceDeclaration
        | MaterialIODeclaration
    ]
    groups: list[GroupDeclaration]
    resource_scopes: list[ResourceScopeDeclaration]
    conditions: list[ConditionDeclaration]
    repeats: list[RepeatUntilDeclaration]
    quantity_requirements: list[QuantityRequirementDeclaration]
    parent_by_node: dict[str, str]
    order_dependencies: list[tuple[str, str]]
    source_order: list[str]
    material_results: set[str]
    executable_results: dict[str, str]
    control_depth: int
    loop_carry_scopes: dict[str, tuple[str, frozenset[str]]]
    resource_scope_stack: list[str]


def parse_authoring_source(
    *,
    python_source: str,
    expected_workflow_uuid: str,
) -> WorkflowProgram:
    """把不可信 Python 源码解析为静态工作流程序。

    参数说明：``python_source`` 是作者草稿，``expected_workflow_uuid`` 是服务层
    权威身份。函数只调用 ``ast.parse`` 和字面量解析，绝不 import/compile/eval/
    execute；返回不可变中间表示，越出静态子集时抛出 ``AuthoringSyntaxError``。
    """

    try:
        module = ast.parse(python_source)
    except SyntaxError as error:
        failure = AuthoringSyntaxError("syntax_error", "作者源码不是有效 Python")
        failure.node = error
        raise failure from None
    imports, declarations = _module_imports(module)
    devices: list[DeviceDeclaration] = []
    functions: list[ast.FunctionDef] = []
    result_records: list[ast.ClassDef] = []
    for statement in declarations:
        # 允许模块级文档字符串；它只用于说明，不参与工作流定义。
        if (
            isinstance(statement, ast.Expr)
            and isinstance(getattr(statement, "value", None), ast.Constant)
            and isinstance(statement.value.value, str)
        ):
            continue
        if isinstance(statement, ast.AnnAssign):
            devices.append(_device_declaration(statement, imports))
        elif isinstance(statement, ast.FunctionDef):
            functions.append(statement)
        elif isinstance(statement, ast.ClassDef):
            result_records.append(statement)
        else:
            _fail(
                "unsupported_authoring_syntax",
                "模块级源码只允许 import、设备声明和一个工作流函数",
                statement,
            )
    if len(functions) != 1:
        _fail("invalid_workflow_declaration", "必须且只能声明一个工作流函数")
    function = functions[0]
    # ``function_docstring`` 只由 Python AST 的规范函数文档语义读取；普通首表达式
    # 不会成为文档，且整个过程不 import、compile 或执行作者代码。
    function_docstring = ast.get_docstring(function, clean=True)
    (
        workflow_uuid,
        display_name,
        description,
        tags,
        meta_data,
        workflow_type,
        root_resources,
    ) = _workflow_declaration(function, imports)
    if workflow_uuid != validate_uuid(expected_workflow_uuid):
        _fail(
            "invalid_workflow_declaration",
            "作者源码中的工作流 UUID 与权威工作流不一致",
            function,
        )
    input_contract, input_resource_template_symbols = _workflow_parameters(
        function,
        imports,
    )
    (
        result_record_name,
        declared_output_schemas,
        declared_output_units,
        output_resource_template_symbols,
    ) = _result_record(
        function,
        result_records=result_records,
        imports=imports,
    )
    anchors = _source_anchors(python_source)
    disabled_node_uuids = _source_disabled_nodes(python_source)
    # 节点展示元数据以节点 UUID 锚点行号为键，只影响工作流节点（WorkflowNode）
    # 的展示字段，不改变动作结果变量或执行身份。
    node_metadata = _source_node_metadata(
        python_source,
        function=function,
        anchors=anchors,
    )
    (
        actions,
        groups,
        conditions,
        repeats,
        quantity_requirements,
        resource_scopes,
        parent_by_node,
        order_dependencies,
        authoring_source_order,
        outputs,
    ) = _workflow_body(
        function,
        imports=imports,
        devices={item.symbol: item for item in devices},
        input_names={item["name"] for item in input_contract["parameters"]},
        anchors=anchors,
        node_metadata=node_metadata,
    )
    # 相邻语句只能表达紧挨着的 ready 边。跨过中间节点的控制边由注释补上，
    # 否则图上多出来的连线在回编译时会被丢掉。
    action_ids = {item.node_uuid for item in actions}
    for source_uuid, target_uuid in _explicit_ready_edges(python_source):
        if source_uuid not in action_ids or target_uuid not in action_ids:
            _fail("invalid_node_anchor", "ready 控制边引用了不存在的节点")
        pair = (source_uuid, target_uuid)
        if pair not in order_dependencies:
            order_dependencies.append(pair)
    used_anchor_lines = {
        declaration.source_node.lineno - 1
        for declaration in (*actions, *groups, *conditions, *repeats)
    }
    if set(anchors) != used_anchor_lines:
        _fail("invalid_node_anchor", "节点 UUID 锚点必须紧邻一个动作声明")
    return WorkflowProgram(
        workflow_uuid=workflow_uuid,
        function_name=function.name,
        function_docstring=function_docstring,
        display_name=display_name,
        description=description,
        tags=tags,
        meta_data=meta_data,
        workflow_type=workflow_type,
        root_resources=root_resources,
        imports=tuple(sorted(imports.items())),
        devices=tuple(devices),
        input_contract=input_contract,
        input_resource_template_symbols=input_resource_template_symbols,
        result_record_name=result_record_name,
        declared_output_schemas=tuple(declared_output_schemas.items()),
        declared_output_units=tuple(declared_output_units.items()),
        output_resource_template_symbols=output_resource_template_symbols,
        actions=tuple(actions),
        groups=tuple(groups),
        resource_scopes=tuple(resource_scopes),
        conditions=tuple(conditions),
        repeats=tuple(repeats),
        quantity_requirements=tuple(quantity_requirements),
        parent_by_node=tuple(sorted(parent_by_node.items())),
        order_dependencies=tuple(order_dependencies),
        source_order=tuple(authoring_source_order),
        disabled_node_uuids=tuple(sorted(disabled_node_uuids)),
        manual_confirmation_timeouts=tuple(
            (anchors[line_number], int(match.group(3)))
            for line_number, line in enumerate(source_lines(python_source), start=1)
            if (match := _NODE_ANCHOR.fullmatch(line)) is not None
            and match.group(3) is not None
        ),
        outputs=tuple(outputs),
    )


def diagnostic_source_range(
    node: ast.AST | SyntaxError | None,
    python_source: str,
) -> dict[str, int] | None:
    """把 AST 或语法错误位置转换为一基 UTF-16 源码范围。

    参数说明：``node`` 是失败位置，``python_source`` 是原始源码；无法安全确定
    位置时返回 ``None``，否则返回前端可直接消费的范围字典。
    """

    if node is None:
        return None
    lines = source_lines(python_source)
    line_number = getattr(node, "lineno", None)
    column_offset = getattr(node, "col_offset", None)
    end_line_number = getattr(node, "end_lineno", line_number)
    end_column_offset = getattr(node, "end_col_offset", column_offset)
    if isinstance(node, SyntaxError):
        line_number = node.lineno
        column_offset = max((node.offset or 1) - 1, 0)
        end_line_number = node.end_lineno or line_number
        end_column_offset = max((node.end_offset or node.offset or 1) - 1, 0)
        # ``SyntaxError.offset`` 使用 Python 字符偏移；普通 AST 列偏移使用 UTF-8
        # 字节。这里必须走独立转换，才能让非 BMP 字符后的前端列号保持 UTF-16。
        if (
            type(line_number) is int
            and type(end_line_number) is int
            and 1 <= line_number <= len(lines)
            and 1 <= end_line_number <= len(lines)
        ):
            start_column = codepoint_offset_to_utf16_column(
                lines[line_number - 1],
                min(column_offset, len(lines[line_number - 1])),
            )
            end_column = codepoint_offset_to_utf16_column(
                lines[end_line_number - 1],
                min(end_column_offset, len(lines[end_line_number - 1])),
            )
            return {
                "start_line": line_number,
                "start_column": start_column,
                "end_line": end_line_number,
                "end_column": max(end_column, start_column),
            }
    if not all(
        type(value) is int
        for value in (line_number, column_offset, end_line_number, end_column_offset)
    ):
        return None
    try:
        return {
            "start_line": line_number,
            "start_column": utf8_offset_to_utf16_column(
                lines[line_number - 1], column_offset
            ),
            "end_line": end_line_number,
            "end_column": utf8_offset_to_utf16_column(
                lines[end_line_number - 1], end_column_offset
            ),
        }
    except (IndexError, ValueError):
        return None


def author_source_map(
    *,
    program: WorkflowProgram,
    python_source: str,
) -> list[dict[str, Any]]:
    """为原始作者源码建立节点到 UTF-16 范围的稳定映射。

    参数说明：``program`` 必须由同一 ``python_source`` 静态解析产生；返回按
    作者源码顺序排列的节点映射，范围包含可选 ``[title]: description`` 注释、
    UUID 锚点与动作声明。分组只映射 ``with`` 头，避免范围覆盖其子节点。
    异常：程序与源码不一致或 AST 坐标越界时抛出 ``ValueError``。
    """

    lines = source_lines(python_source)
    declarations = {
        declaration.node_uuid: declaration
        for declaration in (
            *program.actions,
            *program.groups,
            *program.conditions,
            *program.repeats,
        )
    }
    if set(program.source_order) != set(declarations):
        raise ValueError("作者程序的节点顺序与声明不一致")
    source_map: list[dict[str, Any]] = []
    for node_uuid in program.source_order:
        declaration = declarations[node_uuid]
        source_node = declaration.source_node
        start_line = source_node.lineno - (2 if declaration.title is not None else 1)
        if start_line < 1:
            raise ValueError("节点源码范围缺少 UUID 锚点")
        start_column = utf8_offset_to_utf16_column(
            lines[start_line - 1],
            source_node.col_offset,
        )
        if isinstance(source_node, ast.With):
            # ``ast.With.end_lineno`` 覆盖整个分组体；映射到头行即可避免与子节点
            # 范围重叠，同时仍让画布点击稳定跳转到该分组声明。
            end_line = source_node.lineno
            end_column = utf16_length(lines[end_line - 1]) + 1
        else:
            end_line = source_node.end_lineno
            end_column_offset = source_node.end_col_offset
            if type(end_line) is not int or type(end_column_offset) is not int:
                raise ValueError("节点源码范围缺少结束位置")
            end_column = utf8_offset_to_utf16_column(
                lines[end_line - 1],
                end_column_offset,
            )
        source_map.append(
            CandidateSourceMapEntry(
                workflow_node_uuid=node_uuid,
                start_line=start_line,
                start_column=start_column,
                end_line=end_line,
                end_column=end_column,
            ).model_dump()
        )
    return source_map


def _module_imports(
    module: ast.Module,
) -> tuple[dict[str, str], list[ast.stmt]]:
    """收集静态 import 身份并返回其余模块声明。

    参数说明：``module`` 是已解析 AST；返回局部名到 ``module:symbol`` 的映射
    及非 import 语句。星号导入、相对导入和重复局部名失败关闭。
    """

    imports: dict[str, str] = {}
    declarations: list[ast.stmt] = []
    for statement in module.body:
        if isinstance(statement, ast.ImportFrom):
            if statement.level or statement.module is None:
                _fail("unsupported_authoring_syntax", "不允许相对导入", statement)
            for alias in statement.names:
                if alias.name == "*":
                    _fail("unsupported_authoring_syntax", "不允许星号导入", statement)
                local_name = alias.asname or alias.name
                _add_import(
                    imports, local_name, f"{statement.module}:{alias.name}", statement
                )
        elif isinstance(statement, ast.Import):
            for alias in statement.names:
                local_name = alias.asname or alias.name.split(".", 1)[0]
                _add_import(imports, local_name, alias.name, statement)
        else:
            declarations.append(statement)
    return imports, declarations


def _add_import(
    imports: dict[str, str],
    local_name: str,
    identity: str,
    node: ast.AST,
) -> None:
    """向 import 映射加入一个无歧义局部名。

    参数说明：``imports`` 是可变索引，其余参数是局部名、限定身份和错误位置；
    重复局部名抛出 ``AuthoringSyntaxError``。
    """

    if local_name in imports:
        _fail("unsupported_authoring_syntax", "import 局部名称重复", node)
    imports[local_name] = identity


def _device_declaration(
    statement: ast.AnnAssign,
    imports: dict[str, str],
) -> DeviceDeclaration:
    """解析一个带类型的设备声明。

    参数说明：``statement`` 必须为 ``name: Device = device(...)``，``imports``
    提供静态类身份；返回设备声明，动态参数或空固定身份失败关闭。
    """

    if not isinstance(statement.target, ast.Name) or not isinstance(
        statement.annotation, ast.Name
    ):
        _fail(
            "invalid_device_selector", "设备声明必须使用简单名称和导入类型", statement
        )
    class_identity = imports.get(statement.annotation.id)
    if not isinstance(class_identity, str) or ":" not in class_identity:
        _fail("invalid_device_selector", "设备类型必须来自显式导入", statement)
    call = statement.value
    if isinstance(call, ast.Call) and _is_marker(call.func, imports, "service"):
        from unilabos.workflow.native_commands import COMMAND_CLASSES
        if call.args or call.keywords or class_identity not in COMMAND_CLASSES:
            _fail("invalid_service_selector", "service() 必须选择已注册的 OS 本地服务且不绑定设备", statement)
        return DeviceDeclaration(statement.target.id, class_identity, None, "service")
    if not isinstance(call, ast.Call) or not _is_marker(call.func, imports, "device"):
        _fail("invalid_device_selector", "设备声明必须调用 device()", statement)
    if call.keywords or len(call.args) > 1:
        _fail("invalid_device_selector", "device() 只接受一个可选位置参数", call)
    device_id: str | None = None
    if call.args:
        try:
            device_id = ast.literal_eval(call.args[0])
        except (ValueError, TypeError):
            _fail("invalid_device_selector", "固定设备身份必须是字符串字面量", call)
        if not isinstance(device_id, str) or not device_id:
            _fail("invalid_device_selector", "固定设备身份不能为空", call)
    return DeviceDeclaration(statement.target.id, class_identity, device_id)


def _workflow_declaration(
    function: ast.FunctionDef,
    imports: dict[str, str],
) -> tuple[
    str,
    str,
    str | None,
    list[Any] | None,
    dict[str, Any] | None,
    str | None,
    tuple[str, ...],
]:
    """读取工作流定义装饰器的稳定元数据。

    参数说明：``function`` 是唯一函数，``imports`` 用于识别装饰器；返回工作流
    UUID、展示名、可选描述、标签、公开元数据和可选工作流类型。位置参数、动态
    值或重复字段失败关闭；标签和元数据只接受有限 JSON 字面量。
    """

    declarations = [
        item
        for item in function.decorator_list
        if isinstance(item, ast.Call)
        and (
            _is_marker(item.func, imports, "workflow")
            or _is_marker(item.func, imports, "workflow_definition")
        )
    ]
    if len(declarations) != 1 or len(function.decorator_list) != 1:
        _fail(
            "invalid_workflow_declaration",
            "工作流函数必须只有 workflow 装饰器",
            function,
        )
    declaration = declarations[0]
    if declaration.args:
        _fail("invalid_workflow_declaration", "工作流声明不接受位置参数", declaration)
    values = _literal_keywords(declaration, "invalid_workflow_declaration")
    if set(values) - {
        "workflow_uuid",
        "displayname",
        "description",
        "tags",
        "meta_data",
        "workflow_type",
        "resources",
    }:
        _fail("invalid_workflow_declaration", "工作流声明包含未知字段", declaration)
    try:
        workflow_uuid = validate_uuid(values["workflow_uuid"])
        display_name = values["displayname"]
    except (KeyError, TypeError, ValueError):
        _fail("invalid_workflow_declaration", "工作流 UUID 或展示名无效", declaration)
    description = values.get("description")
    if not isinstance(display_name, str) or not display_name.strip():
        _fail("invalid_workflow_declaration", "工作流展示名不能为空", declaration)
    if description is not None and not isinstance(description, str):
        _fail("invalid_workflow_declaration", "工作流描述必须是字符串", declaration)
    try:
        tags = normalize_json_array(values["tags"]) if "tags" in values else None
        meta_data = (
            normalize_json_object(values["meta_data"])
            if "meta_data" in values
            else None
        )
    except ValueError:
        _fail(
            "invalid_workflow_declaration",
            "工作流标签和公开元数据必须是 JSON 字面量",
            declaration,
        )
    if meta_data is not None and "unilab" in meta_data:
        _fail(
            "invalid_workflow_declaration",
            "工作流公开元数据不能覆盖系统保留的 unilab 字段",
            declaration,
        )
    try:
        workflow_type = (
            normalize_workflow_type(values["workflow_type"])
            if "workflow_type" in values
            else None
        )
    except ValueError:
        _fail(
            "invalid_workflow_declaration",
            "工作流类型只能是 normal 或 experiment_operation",
            declaration,
        )
    root_resources: tuple[str, ...] = ()
    if "resources" in values:
        raw_resources = values["resources"]
        if not isinstance(raw_resources, (list, tuple)) or not raw_resources:
            _fail(
                "invalid_workflow_declaration",
                "根 resources 必须是非空字符串 tuple/list 字面量",
                declaration,
            )
        aliases: list[str] = []
        for value in raw_resources:
            if not isinstance(value, str) or not value.strip():
                _fail(
                    "invalid_workflow_declaration",
                    "根 resources 只能包含非空字符串别名",
                    declaration,
                )
            normalized = value.strip()
            if normalized in aliases:
                _fail(
                    "duplicate_resource_alias",
                    f"资源别名重复：{normalized}",
                    declaration,
                )
            aliases.append(normalized)
        root_resources = tuple(aliases)
    return (
        workflow_uuid,
        display_name.strip(),
        description,
        tags,
        meta_data,
        workflow_type,
        root_resources,
    )


def _workflow_parameters(
    function: ast.FunctionDef,
    imports: dict[str, str],
) -> tuple[dict[str, Any], tuple[tuple[str, tuple[str, ...]], ...]]:
    """静态解析工作流输入合同（Workflow Input Contract）。

    参数说明：只接受关键字专用参数；``imports`` 交给共享参数注解解析器。返回
    版本 1 输入合同及按参数保存的资源模板源码身份；注解错误转换为稳定作者
    语法错误。
    """

    arguments = function.args
    if arguments.posonlyargs or arguments.args or arguments.vararg or arguments.kwarg:
        _fail("invalid_workflow_parameters", "工作流输入必须是关键字专用参数", function)
    parameters: list[dict[str, Any]] = []
    resource_templates: list[tuple[str, tuple[str, ...]]] = []
    try:
        for argument, default in zip(
            arguments.kwonlyargs, arguments.kw_defaults, strict=True
        ):
            if argument.arg == "depends_on":
                _fail("reserved_authoring_parameter", "depends_on 是工作流创作保留参数", argument)
            if argument.annotation is None:
                _fail(
                    "invalid_workflow_parameters", "工作流输入必须带类型注解", argument
                )
            parsed = parse_parameter_annotation(
                argument.arg,
                argument.annotation,
                default=NO_DEFAULT if default is None else default,
                imports=imports,
            )
            parameters.append(parsed.to_dict())
            if parsed.resource_templates:
                resource_templates.append(
                    (
                        argument.arg,
                        tuple(
                            symbol.qualified_name
                            for symbol in parsed.resource_templates
                        ),
                    )
                )
    except AnnotationSchemaError as error:
        raise AuthoringSyntaxError(error.code, error.message, function) from None
    return (
        {"version": 1, "parameters": parameters},
        tuple(resource_templates),
    )


def _result_record(
    function: ast.FunctionDef,
    *,
    result_records: list[ast.ClassDef],
    imports: dict[str, str],
) -> tuple[
    str | None,
    dict[str, dict[str, Any]],
    dict[str, str],
    tuple[tuple[str, tuple[str, ...]], ...],
]:
    """解析可选 ``TypedDict`` 工作流结果记录。

    参数说明：``function`` 提供返回注解，``result_records`` 是模块级类声明，
    ``imports`` 用于识别 ``TypedDict`` 和字段注解；返回记录类名、字段 Schema
    、字段单位及按字段保存的资源模板源码身份。未声明返回记录时返回空记录，动态或不一致
    声明失败关闭。
    异常：返回注解、结果记录数量、字段 Schema 或资源模板身份无效时抛出
    ``AuthoringSyntaxError``。
    """

    if not result_records:
        if not _is_none_return_annotation(function.returns):
            _fail(
                "invalid_workflow_output",
                "工作流返回注解必须引用 TypedDict 结果记录",
                function,
            )
        return None, {}, {}, ()
    if len(result_records) != 1:
        _fail("invalid_workflow_output", "只能声明一个工作流结果记录", function)
    record = result_records[0]
    if (
        len(record.bases) != 1
        or not isinstance(record.bases[0], ast.Name)
        or imports.get(record.bases[0].id) != "typing:TypedDict"
        or record.decorator_list
        or record.keywords
    ):
        _fail("invalid_workflow_output", "工作流结果记录必须是普通 TypedDict", record)
    if not isinstance(function.returns, ast.Name) or function.returns.id != record.name:
        _fail("invalid_workflow_output", "工作流返回注解必须引用结果记录", function)
    fields: dict[str, dict[str, Any]] = {}
    units: dict[str, str] = {}
    resource_templates: list[tuple[str, tuple[str, ...]]] = []
    try:
        for statement in record.body:
            if (
                not isinstance(statement, ast.AnnAssign)
                or not isinstance(statement.target, ast.Name)
                or statement.value is not None
            ):
                _fail(
                    "invalid_workflow_output", "结果记录只允许带类型的字段", statement
                )
            name = statement.target.id
            if name in fields:
                _fail("invalid_workflow_output", "结果记录字段重复", statement)
            parsed = parse_result_annotation(
                name,
                statement.annotation,
                imports=imports,
            )
            fields[name] = parsed.to_dict()["schema"]
            unit = parsed.to_dict().get("unit")
            if isinstance(unit, str):
                units[name] = unit
            if parsed.resource_templates:
                resource_templates.append(
                    (
                        name,
                        tuple(
                            symbol.qualified_name
                            for symbol in parsed.resource_templates
                        ),
                    )
                )
    except AnnotationSchemaError as error:
        raise AuthoringSyntaxError(error.code, error.message, record) from None
    return record.name, fields, units, tuple(resource_templates)


def _is_none_return_annotation(annotation: ast.expr | None) -> bool:
    """判断返回注解是否表示无工作流输出。

    参数：``annotation`` 是工作流函数的可选返回注解。
    返回：未注解或显式 ``None`` 时返回真，其余注解返回假。
    异常：无；本函数只检查静态语法节点，不解析或执行名称。
    """

    return annotation is None or (
        isinstance(annotation, ast.Constant) and annotation.value is None
    )


def _explicit_ready_edges(python_source: str) -> list[tuple[str, str]]:
    """读取源码里显式声明、且不能由相邻语句表达的 ready 控制边。"""

    edges: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for line in source_lines(python_source):
        if "unilab:ready_edge" not in line:
            continue
        match = _READY_EDGE.fullmatch(line)
        if match is None:
            _fail("invalid_node_anchor", "ready 控制边注释格式无效")
        try:
            pair = (validate_uuid(match.group(1)), validate_uuid(match.group(2)))
        except ValueError:
            _fail("invalid_node_anchor", "ready 控制边不是有效 UUID")
        if pair[0] == pair[1] or pair in seen:
            _fail("invalid_node_anchor", "ready 控制边重复或指向自身")
        seen.add(pair)
        edges.append(pair)
    return edges


def _source_anchors(python_source: str) -> dict[int, str]:
    """读取严格格式的节点 UUID 锚点。

    参数说明：``python_source`` 是原始源码；返回锚点行号到 UUID 的映射。任何
    含锚点前缀但格式不精确、UUID 重复的注释都失败关闭。
    """

    anchors: dict[int, str] = {}
    identities: set[str] = set()
    for line_number, line in enumerate(source_lines(python_source), start=1):
        if "unilab:node_uuid" not in line:
            continue
        match = _NODE_ANCHOR.fullmatch(line)
        if match is None:
            _fail("invalid_node_anchor", "节点 UUID 锚点格式无效")
        try:
            identity = validate_uuid(match.group(1))
        except ValueError:
            _fail("invalid_node_anchor", "节点 UUID 锚点不是有效 UUID")
        if identity in identities:
            _fail("duplicate_node_uuid", "节点 UUID 锚点重复")
        identities.add(identity)
        anchors[line_number] = identity
    return anchors


def _source_disabled_nodes(python_source: str) -> set[str]:
    """读取 UUID 锚点上显式声明的静态禁用标记。"""

    disabled: set[str] = set()
    for line in source_lines(python_source):
        if "unilab:node_uuid" not in line:
            continue
        match = _NODE_ANCHOR.fullmatch(line)
        if match is not None and match.group(2) == "true":
            disabled.add(validate_uuid(match.group(1)))
    return disabled


def _source_node_metadata(
    python_source: str,
    *,
    function: ast.FunctionDef,
    anchors: dict[int, str],
) -> dict[int, tuple[str, str]]:
    """读取与节点 UUID 锚点相邻的节点展示注释。

    参数说明：``python_source`` 是不可信作者源码，``function`` 限定工作流函数
    范围，``anchors`` 提供合法节点锚点行号；返回以锚点行号为键的标题、描述。
    注释格式、缩进或相邻关系不成立时抛出 ``AuthoringSyntaxError``，防止注释
    被静默绑定到错误的工作流节点（WorkflowNode）。
    """

    lines = source_lines(python_source)
    metadata: dict[int, tuple[str, str]] = {}
    function_end_line = function.end_lineno or function.lineno
    # 注释词法单元用于区分真正的行尾注释与字符串中的 ``# [`` 文本。
    comment_tokens = tokenize.generate_tokens(StringIO(python_source).readline)
    for token_info in comment_tokens:
        if token_info.type != tokenize.COMMENT:
            continue
        line_number, column = token_info.start
        if not function.lineno <= line_number <= function_end_line:
            continue
        if _NODE_METADATA_PREFIX.match(token_info.string) is None:
            continue
        if lines[line_number - 1][:column].strip():
            _fail(
                "invalid_node_metadata",
                "节点展示注释必须独占一行并位于动作声明前",
            )
    for line_number in range(function.lineno, function_end_line + 1):
        line = lines[line_number - 1]
        if _NODE_METADATA_PREFIX.match(line) is None:
            continue
        match = _NODE_METADATA.fullmatch(line)
        if match is None:
            _fail("invalid_node_metadata", "节点展示注释格式无效")
        anchor_line = line_number + 1
        if anchor_line not in anchors:
            _fail("invalid_node_metadata", "节点展示注释必须紧邻节点 UUID 锚点")
        anchor_source = lines[anchor_line - 1]
        anchor_indent = anchor_source[
            : len(anchor_source) - len(anchor_source.lstrip())
        ]
        if match.group("indent") != anchor_indent:
            _fail("invalid_node_metadata", "节点展示注释必须与节点 UUID 锚点同级")
        title = match.group("title").strip()
        description = match.group("description").strip()
        if not title or not description:
            _fail("invalid_node_metadata", "节点标题和描述不能为空")
        metadata[anchor_line] = (title, description)
    return metadata


def _workflow_body(
    function: ast.FunctionDef,
    *,
    imports: dict[str, str],
    devices: dict[str, DeviceDeclaration],
    input_names: set[str],
    anchors: dict[int, str],
    node_metadata: dict[int, tuple[str, str]],
) -> tuple[
    list[
        ActionDeclaration
        | CompositeDeclaration
        | MaterialSourceDeclaration
        | MaterialIODeclaration
    ],
    list[GroupDeclaration],
    list[ConditionDeclaration],
    list[RepeatUntilDeclaration],
    list[QuantityRequirementDeclaration],
    list[ResourceScopeDeclaration],
    dict[str, str],
    list[tuple[str, str]],
    list[str],
    list[tuple[str, ValueBinding]],
]:
    """解析工作流函数中的动作、展示结构、执行顺序与输出声明。

    参数说明：``function`` 是唯一工作流函数；``imports``、``devices`` 与
    ``input_names`` 是可信静态身份索引；``anchors`` 固定所有持久节点身份；
    ``node_metadata`` 保存展示覆盖。返回：动作、分组、父子关系、顺序依赖、源码
    节点顺序与命名输出。异常：动态控制流、非法分组或并行分支失败关闭。
    """

    statements = list(function.body)
    if (
        statements
        and isinstance(statements[0], ast.Expr)
        and isinstance(statements[0].value, ast.Constant)
        and isinstance(statements[0].value.value, str)
    ):
        statements.pop(0)
    if statements and isinstance(statements[-1], ast.Return):
        # ``return_statement`` 是显式工作流输出，继续接受结果字典或 workflow_output。
        return_statement = statements.pop()
    else:
        # ``return_statement`` 把 Python 的隐式 ``return None`` 规范化为空输出合同；
        # 声明了 TypedDict 结果记录时，后续合同一致性校验仍会关闭式拒绝缺失字段。
        return_statement = ast.Return(value=ast.Dict(keys=[], values=[]))
    state = _BodyState(
        imports=imports,
        devices=devices,
        input_names=input_names,
        anchors=anchors,
        node_metadata=node_metadata,
        actions=[],
        groups=[],
        resource_scopes=[],
        conditions=[],
        repeats=[],
        quantity_requirements=[],
        parent_by_node={},
        order_dependencies=[],
        source_order=[],
        material_results=set(),
        executable_results={},
        control_depth=0,
        loop_carry_scopes={},
        resource_scope_stack=[],
    )
    # ``known_results`` 只在递归边界复制，保证同级并行分支互不可见。
    known_results: set[str] = set()
    _parse_sequence(
        statements,
        state=state,
        available_results=known_results,
        parent_uuid=None,
    )
    _finalize_resource_scope_ids(state)
    outputs = _workflow_outputs(
        return_statement,
        imports=imports,
        input_names=input_names,
        known_results=known_results,
        material_results=state.material_results,
    )
    return (
        state.actions,
        state.groups,
        state.conditions,
        state.repeats,
        state.quantity_requirements,
        state.resource_scopes,
        state.parent_by_node,
        state.order_dependencies,
        state.source_order,
        outputs,
    )


def _parse_sequence(
    statements: list[ast.stmt],
    *,
    state: _BodyState,
    available_results: set[str],
    parent_uuid: str | None,
) -> _Flow:
    """解析一个严格顺序片段并建立相邻执行片段依赖。

    参数说明：``statements`` 是同一词法层级的语句；``state`` 收集不可变 IR
    所需事实；``available_results`` 是当前作用域可读且由本函数原位扩充的结果名；
    ``parent_uuid`` 是可选展示分组父节点。返回：片段真实执行入口、出口及本层新增
    结果名。异常：任一语句超出静态子集时原样传播。
    """

    initial_results = set(available_results)
    first_entries: tuple[str, ...] = ()
    first_default_entries: tuple[str, ...] = ()
    previous_exits: tuple[str, ...] = ()
    for statement in statements:
        segment = _parse_statement(
            statement,
            state=state,
            available_results=available_results,
            parent_uuid=parent_uuid,
        )
        if segment.entries:
            if previous_exits:
                state.order_dependencies.extend(
                    (source_uuid, target_uuid)
                    for source_uuid in previous_exits
                    for target_uuid in segment.default_entries
                )
            elif not first_entries:
                first_entries = segment.entries
                first_default_entries = segment.default_entries
            previous_exits = segment.exits
        available_results.update(segment.result_names)
    return _Flow(
        entries=first_entries,
        default_entries=first_default_entries,
        exits=previous_exits,
        result_names=frozenset(available_results - initial_results),
    )


def _parse_statement(
    statement: ast.stmt,
    *,
    state: _BodyState,
    available_results: set[str],
    parent_uuid: str | None,
) -> _Flow:
    """把一条动作、分组或并行语句解析为执行流片段。

    参数说明：``statement`` 是当前 AST 语句；``state`` 是本次函数体收集状态；
    ``available_results`` 限定合法反向引用；``parent_uuid`` 指定动作展示父节点。
    返回：无合成节点的入口/出口流。异常：未知 ``with`` 或动态语句失败关闭。
    """

    if isinstance(statement, ast.If):
        return _parse_condition(
            statement,
            state=state,
            available_results=available_results,
            parent_uuid=parent_uuid,
        )

    if isinstance(statement, ast.With):
        marker = _with_marker(statement, state.imports)
        if marker == "group":
            return _parse_group(
                statement,
                state=state,
                available_results=available_results,
                parent_uuid=parent_uuid,
                parallel_scope=None,
                parallel_order=None,
            )
        if marker == "parallel":
            return _parse_parallel(
                statement,
                state=state,
                available_results=available_results,
                parent_uuid=parent_uuid,
            )
        if marker == "resource_holds":
            return _parse_resource_holds(statement, state=state, available_results=available_results)
        if marker == "resources":
            return _parse_resource_scope(
                statement,
                state=state,
                available_results=available_results,
                parent_uuid=parent_uuid,
            )
        if marker == "repeat_until":
            return _parse_repeat_until(
                statement,
                state=state,
                available_results=available_results,
                parent_uuid=parent_uuid,
            )
        _fail("unsupported_authoring_syntax", "工作流不支持该 with 语句", statement)

    quantity_requirement = _quantity_requirement_declaration(
        statement,
        state=state,
        available_results=available_results,
    )
    if quantity_requirement is not None:
        if any(
            item.requirement_key == quantity_requirement.requirement_key
            for item in state.quantity_requirements
        ):
            _fail(
                "invalid_quantity_requirement",
                "数量库存需求键不能重复",
                statement,
            )
        state.quantity_requirements.append(quantity_requirement)
        return _Flow((), (), (), frozenset())

    action = _action_declaration(
        statement,
        imports=state.imports,
        devices=state.devices,
        input_names=state.input_names,
        known_results=available_results,
        material_results=state.material_results & available_results,
        anchors=state.anchors,
        node_metadata=state.node_metadata,
        executable_results=state.executable_results,
        carry_scopes=state.loop_carry_scopes,
    )
    if action.result_name in available_results or any(
        existing.result_name == action.result_name for existing in state.actions
    ):
        _fail("unsupported_authoring_syntax", "动作结果变量重复", statement)
    state.actions.append(action)
    state.source_order.append(action.node_uuid)
    if parent_uuid is not None:
        state.parent_by_node[action.node_uuid] = parent_uuid
    if isinstance(action, MaterialSourceDeclaration):
        state.material_results.add(action.result_name)
        if not action.supports_replenishment and state.control_depth == 0:
            return _Flow((), (), (), frozenset({action.result_name}))
    if isinstance(action, MaterialIODeclaration) and action.mode in {
        "load",
        "refill",
    }:
        state.material_results.add(action.result_name)
    state.executable_results[action.result_name] = action.node_uuid
    dependencies = getattr(action, "control_dependencies", None)
    if dependencies is not None:
        state.order_dependencies.extend((source, action.node_uuid) for source in dependencies)
    return _Flow(
        (action.node_uuid,),
        (action.node_uuid,) if dependencies is None else (),
        (action.node_uuid,),
        frozenset({action.result_name}),
    )


def _quantity_requirement_declaration(
    statement: ast.stmt,
    *,
    state: _BodyState,
    available_results: set[str],
) -> QuantityRequirementDeclaration | None:
    """解析不产生执行节点的 ``quantity_requirement`` 静态标记。"""

    if not (
        isinstance(statement, ast.Expr)
        and isinstance(statement.value, ast.Call)
        and _is_marker(
            statement.value.func,
            state.imports,
            "quantity_requirement",
        )
    ):
        return None
    call = statement.value
    if state.control_depth:
        _fail(
            "invalid_quantity_requirement",
            "数量需求暂不允许声明在条件或循环区域内",
            statement,
        )
    if call.args or any(keyword.arg is None for keyword in call.keywords):
        _fail(
            "invalid_quantity_requirement",
            "quantity_requirement 只接受命名参数",
            call,
        )
    keywords = {str(keyword.arg): keyword.value for keyword in call.keywords}
    if len(keywords) != len(call.keywords):
        _fail("invalid_quantity_requirement", "数量需求参数不能重复", call)
    required = {
        "requirement_key",
        "source",
        "consume",
        "quantity",
        "quantity_unit",
    }
    if not required <= set(keywords) or set(keywords) - {
        *required,
        "scale",
        "description",
    }:
        _fail(
            "invalid_quantity_requirement",
            "quantity_requirement 参数不完整或包含未知字段",
            call,
        )
    try:
        requirement_key = ast.literal_eval(keywords["requirement_key"])
        quantity_unit = ast.literal_eval(keywords["quantity_unit"])
        scale = (
            ast.literal_eval(keywords["scale"])
            if "scale" in keywords
            else 1.0
        )
        description = (
            ast.literal_eval(keywords["description"])
            if "description" in keywords
            else None
        )
    except (TypeError, ValueError):
        _fail(
            "invalid_quantity_requirement",
            "数量需求键、单位、倍率和说明必须是静态字面量",
            call,
        )
    if (
        not isinstance(requirement_key, str)
        or not requirement_key.strip()
        or requirement_key != requirement_key.strip()
    ):
        _fail("invalid_quantity_requirement", "数量需求键不能为空", call)
    if (
        not isinstance(quantity_unit, str)
        or not quantity_unit.strip()
        or quantity_unit != quantity_unit.strip()
    ):
        _fail("invalid_quantity_requirement", "数量需求单位不能为空", call)
    if isinstance(scale, bool) or not isinstance(scale, (int, float)):
        _fail("invalid_quantity_requirement", "数量换算倍率必须是有限正数", call)
    scale = float(scale)
    if not math.isfinite(scale) or scale <= 0:
        _fail("invalid_quantity_requirement", "数量换算倍率必须是有限正数", call)
    if description is not None and (
        not isinstance(description, str) or not description.strip()
    ):
        _fail("invalid_quantity_requirement", "数量需求说明必须是非空字符串", call)
    source = keywords["source"]
    consume = keywords["consume"]
    if not isinstance(source, ast.Name) or source.id not in available_results:
        _fail(
            "invalid_quantity_requirement",
            "source 必须直接引用此前的 material_source 结果",
            source,
        )
    if source.id not in state.material_results:
        _fail(
            "invalid_quantity_requirement",
            "source 必须直接引用 material_source 结果",
            source,
        )
    if not isinstance(consume, ast.Name) or consume.id not in available_results:
        _fail(
            "invalid_quantity_requirement",
            "consume 必须直接引用此前的动作结果",
            consume,
        )
    consume_declaration = next(
        (
            item
            for item in state.actions
            if item.result_name == consume.id
        ),
        None,
    )
    if consume_declaration is None or isinstance(
        consume_declaration,
        MaterialSourceDeclaration,
    ):
        _fail(
            "invalid_quantity_requirement",
            "consume 必须直接引用普通动作或组合动作结果",
            consume,
        )
    quantity = _value_binding(
        keywords["quantity"],
        input_names=state.input_names,
        known_results=available_results,
        material_results=state.material_results & available_results,
    )
    if quantity.kind not in {"literal", "workflow_input"}:
        _fail(
            "invalid_quantity_requirement",
            "quantity 只能是正数字面量或工作流输入",
            keywords["quantity"],
        )
    if quantity.kind == "literal":
        value = quantity.value
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or float(value) <= 0
        ):
            _fail(
                "invalid_quantity_requirement",
                "quantity 字面量必须是有限正数",
                keywords["quantity"],
            )
    return QuantityRequirementDeclaration(
        requirement_key=requirement_key,
        source_result_name=source.id,
        consume_result_name=consume.id,
        quantity=quantity,
        quantity_unit=quantity_unit,
        scale=scale,
        description=description.strip() if isinstance(description, str) else None,
        source_node=statement,
    )


def _parse_condition(
    statement: ast.If,
    *,
    state: _BodyState,
    available_results: set[str],
    parent_uuid: str | None,
) -> _Flow:
    """解析原生 ``if / elif / else`` 为一个有序结构化控制区域。"""

    if state.control_depth >= _MAX_CONTROL_NESTING_DEPTH:
        _fail("control_nesting_too_deep", "条件区域嵌套深度不能超过 8", statement)
    state.control_depth += 1

    node_uuid = state.anchors.get(statement.lineno - 1)
    if node_uuid is None:
        _fail(
            "invalid_node_anchor", "每个条件区域前必须有相邻节点 UUID 锚点", statement
        )
    metadata = state.node_metadata.get(statement.lineno - 1)
    if parent_uuid is not None:
        state.parent_by_node[node_uuid] = parent_uuid
    state.source_order.append(node_uuid)

    clauses: list[tuple[str, ast.expr | None, list[ast.stmt]]] = []
    current = statement
    branch_index = 0
    while True:
        label = "if" if branch_index == 0 else f"elif{branch_index - 1}"
        clauses.append((label, current.test, list(current.body)))
        if len(current.orelse) == 1 and isinstance(current.orelse[0], ast.If):
            current = current.orelse[0]
            branch_index += 1
            continue
        if current.orelse:
            clauses.append(("else", None, list(current.orelse)))
        break

    branches: list[ConditionBranchDeclaration] = []
    all_exits: list[str] = []
    executable_before = dict(state.executable_results)
    for label, test, body in clauses:
        state.executable_results = dict(executable_before)
        before = len(state.source_order)
        branch_results = set(available_results)
        flow = _parse_sequence(
            body,
            state=state,
            available_results=branch_results,
            parent_uuid=node_uuid,
        )
        node_uuids = tuple(state.source_order[before:])
        if not node_uuids:
            _fail("invalid_condition", "条件分支至少包含一个工作流节点", statement)
        condition = (
            None
            if test is None
            else _condition_expression(
                test,
                input_names=state.input_names,
                known_results=available_results,
                carry_scopes=state.loop_carry_scopes,
            )
        )
        branches.append(
            ConditionBranchDeclaration(
                label=label,
                condition=condition,
                node_uuids=node_uuids,
                entry_node_uuids=flow.entries,
                exit_node_uuids=flow.exits,
            )
        )
        all_exits.extend(flow.exits)

    state.executable_results = executable_before
    variable_names = {
        name
        for branch in branches
        if branch.condition is not None
        for name in _condition_variable_names(branch.condition)
    }
    result_nodes = {
        declaration.result_name: declaration.node_uuid
        for declaration in state.actions
        if declaration.result_name in available_results
    }
    bindings: list[tuple[str, dict[str, str]]] = []
    for name in sorted(variable_names):
        if name in state.input_names:
            bindings.append((name, {"kind": "workflow_input", "parameter": name}))
        elif name in result_nodes:
            bindings.append(
                (name, {"kind": "node_result", "node_uuid": result_nodes[name]})
            )
        else:
            _fail("invalid_condition_expression", "条件变量缺少稳定来源", statement)

    state.conditions.append(
        ConditionDeclaration(
            node_uuid=node_uuid,
            title=metadata[0] if metadata is not None else None,
            description=metadata[1] if metadata is not None else None,
            branches=tuple(branches),
            bindings=tuple(bindings),
            source_node=statement,
        )
    )
    state.control_depth -= 1
    return _Flow(
        entries=(node_uuid,),
        default_entries=(node_uuid,),
        exits=tuple(all_exits),
        result_names=frozenset(),
    )


def _condition_expression(
    expression: ast.expr,
    *,
    input_names: set[str],
    known_results: set[str],
    carry_scopes: Mapping[str, tuple[str, frozenset[str]]] | None = None,
) -> dict[str, Any]:
    """把条件 AST 编译为 pTLC 兼容的封闭结构化表达式。"""

    if isinstance(expression, ast.Constant):
        if expression.value is None or isinstance(
            expression.value, (bool, int, float, str)
        ):
            return {"lit": expression.value}
        _fail("invalid_condition_expression", "条件字面量不是 JSON 标量", expression)
    if isinstance(expression, ast.Name):
        if expression.id not in input_names | known_results:
            _fail("invalid_condition_expression", "条件引用了不可见变量", expression)
        return {"var": expression.id}
    if isinstance(expression, ast.Attribute):
        return {
            "field": _condition_expression(
                expression.value,
                input_names=input_names,
                known_results=known_results,
                carry_scopes=carry_scopes,
            ),
            "name": expression.attr,
        }
    if isinstance(expression, ast.Subscript):
        if (
            carry_scopes is not None
            and isinstance(expression.value, ast.Attribute)
            and expression.value.attr == "carry"
            and isinstance(expression.value.value, ast.Name)
            and expression.value.value.id in carry_scopes
            and isinstance(expression.slice, ast.Constant)
            and isinstance(expression.slice.value, str)
        ):
            region_uuid, keys = carry_scopes[expression.value.value.id]
            if expression.slice.value not in keys:
                _fail("invalid_loop_carry", "循环 carry 引用了未知键", expression)
            return {
                "carry": expression.slice.value,
                "control_region_uuid": region_uuid,
            }
        return {
            "index": _condition_expression(
                expression.value,
                input_names=input_names,
                known_results=known_results,
                carry_scopes=carry_scopes,
            ),
            "key": _condition_expression(
                expression.slice,
                input_names=input_names,
                known_results=known_results,
                carry_scopes=carry_scopes,
            ),
        }
    if isinstance(expression, ast.BoolOp):
        operator_name = "and" if isinstance(expression.op, ast.And) else "or"
        values = [
            _condition_expression(
                value,
                input_names=input_names,
                known_results=known_results,
                carry_scopes=carry_scopes,
            )
            for value in expression.values
        ]
        result = values[0]
        for value in values[1:]:
            result = {"binop": operator_name, "left": result, "right": value}
        return result
    if (
        isinstance(expression, ast.Compare)
        and len(expression.ops) == len(expression.comparators) == 1
    ):
        operators = {
            ast.Eq: "==",
            ast.NotEq: "!=",
            ast.Gt: ">",
            ast.GtE: ">=",
            ast.Lt: "<",
            ast.LtE: "<=",
        }
        operator_name = operators.get(type(expression.ops[0]))
        if operator_name is None:
            _fail("invalid_condition_expression", "条件比较运算符不受支持", expression)
        return {
            "binop": operator_name,
            "left": _condition_expression(
                expression.left,
                input_names=input_names,
                known_results=known_results,
                carry_scopes=carry_scopes,
            ),
            "right": _condition_expression(
                expression.comparators[0],
                input_names=input_names,
                known_results=known_results,
                carry_scopes=carry_scopes,
            ),
        }
    if isinstance(expression, ast.BinOp):
        operators = {
            ast.Add: "+",
            ast.Sub: "-",
            ast.Mult: "*",
            ast.Div: "/",
            ast.FloorDiv: "//",
            ast.Mod: "%",
        }
        operator_name = operators.get(type(expression.op))
        if operator_name is None:
            _fail("invalid_condition_expression", "条件算术运算符不受支持", expression)
        return {
            "binop": operator_name,
            "left": _condition_expression(
                expression.left,
                input_names=input_names,
                known_results=known_results,
                carry_scopes=carry_scopes,
            ),
            "right": _condition_expression(
                expression.right,
                input_names=input_names,
                known_results=known_results,
                carry_scopes=carry_scopes,
            ),
        }
    if isinstance(expression, ast.UnaryOp):
        operator_name = (
            "not"
            if isinstance(expression.op, ast.Not)
            else "neg"
            if isinstance(expression.op, ast.USub)
            else None
        )
        if operator_name is None:
            _fail("invalid_condition_expression", "条件一元运算符不受支持", expression)
        return {
            "unop": operator_name,
            "operand": _condition_expression(
                expression.operand,
                input_names=input_names,
                known_results=known_results,
                carry_scopes=carry_scopes,
            ),
        }
    if isinstance(expression, ast.Call) and isinstance(expression.func, ast.Name):
        allowed_calls = {"len", "min", "max", "abs", "round", "contains", "get"}
        if expression.func.id not in allowed_calls or expression.keywords:
            _fail(
                "invalid_condition_expression", "条件函数不在纯函数白名单中", expression
            )
        return {
            "call": expression.func.id,
            "args": [
                _condition_expression(
                    argument,
                    input_names=input_names,
                    known_results=known_results,
                    carry_scopes=carry_scopes,
                )
                for argument in expression.args
            ],
        }
    _fail("invalid_condition_expression", "条件表达式超出可信静态子集", expression)


def _condition_variable_names(expression: Mapping[str, Any]) -> set[str]:
    """返回结构化表达式中所有变量引用。"""

    result: set[str] = set()

    def visit(value: Any) -> None:
        if isinstance(value, Mapping):
            if set(value) == {"var"} and isinstance(value.get("var"), str):
                result.add(str(value["var"]))
                return
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(expression)
    return result


def _with_marker(statement: ast.With, imports: dict[str, str]) -> str | None:
    """识别单个可信上下文及其绑定形式，区分连续词法范围和节点预留声明块。"""

    if len(statement.items) != 1:
        return None
    context = statement.items[0].context_expr
    if not isinstance(context, ast.Call):
        return None
    for marker_name in ("group", "parallel", "repeat_until", "resources"):
        if _is_marker(context.func, imports, marker_name):
            if marker_name == "resources" and isinstance(statement.items[0].optional_vars, ast.Name):
                return "resource_holds"
            if marker_name == "repeat_until":
                return (
                    marker_name
                    if isinstance(statement.items[0].optional_vars, ast.Name)
                    else None
                )
            if statement.items[0].optional_vars is not None:
                return None
            return marker_name
    return None


def _parse_resource_holds(
    statement: ast.With, *, state: _BodyState, available_results: set[str],
) -> _Flow:
    """只登记资源区间，不将声明块或 hold 调用加入执行图。"""
    context = statement.items[0]
    receiver = context.optional_vars
    assert isinstance(receiver, ast.Name) and isinstance(context.context_expr, ast.Call)
    if context.context_expr.args or context.context_expr.keywords:
        _fail("invalid_reservation", "预留声明块必须写成 with resources() as 名称", statement)
    if state.control_depth:
        _fail("invalid_reservation", "资源预留声明必须位于条件和循环区域之外", statement)
    occupied_names = available_results | state.input_names | state.devices.keys() | state.imports.keys()
    if receiver.id in occupied_names:
        _fail("invalid_reservation", "预留声明块名称不能遮蔽已有名称", receiver)

    executable = {
        action.result_name: action.node_uuid for action in state.actions
        if isinstance(action, (ActionDeclaration, CompositeDeclaration))
        and action.result_name in available_results
    }
    for member in statement.body:
        if not isinstance(member, ast.Expr) or not isinstance(member.value, ast.Call):
            _fail("invalid_reservation", "预留声明块只能包含 hold 调用", member)
        call = member.value
        method = call.func
        if not (
            isinstance(method, ast.Attribute)
            and method.attr == "hold"
            and isinstance(method.value, ast.Name)
            and method.value.id == receiver.id
        ):
            _fail("invalid_reservation", f"此块只接受 {receiver.id}.hold(...) 声明", member)
        kwargs = {kw.arg: kw.value for kw in call.keywords}
        if len(kwargs) != len(call.keywords) or set(kwargs) - {"nodes", "name"}:
            _fail("invalid_reservation", "hold 只接受资源别名及 nodes、name", call)
        arguments = kwargs.get("nodes")
        if not isinstance(arguments, ast.List) or not arguments.elts:
            _fail("invalid_reservation", "nodes 必须是非空动作结果列表", call)
        nodes = []
        for item in arguments.elts:
            if not isinstance(item, ast.Name) or item.id not in executable:
                _fail("invalid_reservation", "只能预留已声明且当前作用域可见的动作结果", item)
            nodes.append(executable[item.id])
        if len(set(nodes)) != len(nodes):
            _fail("invalid_reservation", "预留节点不能重复", arguments)
        aliases = _literal_resource_aliases(
            call, code="invalid_reservation", message="预留资源必须是非空资源别名字面量",
            allow_keywords=True,
        )
        name = kwargs.get("name", ast.Constant(value=""))
        if not isinstance(name, ast.Constant) or not isinstance(name.value, str):
            _fail("invalid_reservation", "预留名称必须为字符串字面量", name)
        state.resource_scopes.append(
            ResourceScopeDeclaration(
                scope_id=f"pending-reservation-{len(state.resource_scopes)}",
                kind="reservation",
                name=name.value,
                resources=aliases,
                parent_scope_id=None,
                node_uuids=tuple(sorted(nodes)),
                entry_node_uuid="",
                exit_node_uuid="",
                source_node=member,
            )
        )
    return _Flow((), (), (), frozenset())


def _parse_resource_scope(
    statement: ast.With,
    *,
    state: _BodyState,
    available_results: set[str],
    parent_uuid: str | None,
) -> _Flow:
    """解析 ``with resources(...)`` 的词法硬边界而不创建执行节点。"""

    context = statement.items[0].context_expr
    assert isinstance(context, ast.Call)
    aliases = _literal_resource_aliases(
        context,
        code="invalid_resource_scope",
        message="resources 只接受非空字符串资源别名",
    )
    # 解析嵌套 body 前使用仅限本次解析的临时 ID，避免把源码行号写入持久
    # 图身份。真正稳定的 scope_id 在 body 解析完成、拿到节点 UUID 集合后生成。
    scope_id = f"pending-resource-scope-{len(state.resource_scopes)}-{len(state.resource_scope_stack)}"
    parent_scope_id = (
        state.resource_scope_stack[-1] if state.resource_scope_stack else None
    )
    start_index = len(state.source_order)
    state.resource_scope_stack.append(scope_id)
    try:
        flow = _parse_sequence(
            list(statement.body),
            state=state,
            available_results=available_results,
            parent_uuid=parent_uuid,
        )
    finally:
        state.resource_scope_stack.pop()
    members = tuple(state.source_order[start_index:])
    if not flow.entries or not members:
        _fail("invalid_resource_scope", "resources 作用域必须包含至少一个可执行节点", statement)
    state.resource_scopes.append(
        ResourceScopeDeclaration(
            scope_id=scope_id,
            resources=aliases,
            parent_scope_id=parent_scope_id,
            node_uuids=members,
            entry_node_uuid=flow.entries[0],
            exit_node_uuid=flow.exits[-1] if flow.exits else flow.entries[-1],
            source_node=statement,
        )
    )
    return flow


def _finalize_resource_scope_ids(state: _BodyState) -> None:
    """将资源作用域临时 ID 规范化为与源码位置无关的稳定身份。

    作用域身份由资源别名、按源码顺序排列的节点 UUID、入口/出口节点组成；
    因而作者源码被格式化或插入注释后，重复编译仍得到相同图。嵌套作用域在
    解析时引用父临时 ID，这里统一替换为最终 ID。
    """

    if not state.resource_scopes:
        return
    stable_by_pending: dict[str, str] = {}
    for scope in state.resource_scopes:
        identity = "|".join(
            (
                "unilabos:resource-scope:v1" if scope.kind == "with" else "unilabos:reservation:v1",
                ",".join(scope.resources),
                ",".join(scope.node_uuids),
                scope.entry_node_uuid,
                scope.exit_node_uuid,
            )
        )
        stable_id = f"resource-scope-{uuid5(NAMESPACE_URL, identity)}"
        previous = stable_by_pending.get(scope.scope_id)
        if previous is not None and previous != stable_id:
            raise AuthoringSyntaxError(
                "invalid_resource_scope",
                "资源作用域身份无法稳定化",
                scope.source_node,
            )
        if stable_id in stable_by_pending.values():
            raise AuthoringSyntaxError(
                "invalid_resource_scope",
                "资源作用域身份重复",
                scope.source_node,
            )
        stable_by_pending[scope.scope_id] = stable_id
    state.resource_scopes[:] = [
        replace(
            scope,
            scope_id=stable_by_pending[scope.scope_id],
            parent_scope_id=(
                stable_by_pending.get(scope.parent_scope_id)
                if scope.parent_scope_id is not None
                else None
            ),
        )
        for scope in state.resource_scopes
    ]


def _literal_resource_aliases(
    call: ast.Call,
    *,
    code: str,
    message: str,
    allow_keywords: bool = False,
) -> tuple[str, ...]:
    """读取 resources 调用中的静态字符串别名。"""

    if call.keywords and not allow_keywords:
        _fail(code, message, call)
    aliases: list[str] = []
    for argument in call.args:
        try:
            value = ast.literal_eval(argument)
        except (TypeError, ValueError):
            _fail(code, message, argument)
        if not isinstance(value, str) or not value.strip():
            _fail(code, message, argument)
        normalized = value.strip()
        if normalized in aliases:
            _fail("duplicate_resource_alias", f"资源别名重复：{normalized}", argument)
        aliases.append(normalized)
    if not aliases:
        _fail(code, message, call)
    return tuple(aliases)


def _parse_repeat_until(
    statement: ast.With,
    *,
    state: _BodyState,
    available_results: set[str],
    parent_uuid: str | None,
) -> _Flow:
    """解析 ``repeat_until``、显式 carry/next 与末尾 until。"""

    if state.control_depth >= _MAX_CONTROL_NESTING_DEPTH:
        _fail("control_nesting_too_deep", "控制区域嵌套深度不能超过 8", statement)
    context = statement.items[0].context_expr
    loop_target = statement.items[0].optional_vars
    assert isinstance(context, ast.Call) and isinstance(loop_target, ast.Name)
    if context.args or any(item.arg is None for item in context.keywords):
        _fail("invalid_repeat_until", "repeat_until 只接受命名参数", context)
    keyword_values = {str(item.arg): item.value for item in context.keywords}
    if len(keyword_values) != len(context.keywords) or set(keyword_values) != {
        "max_iterations",
        "carry",
    }:
        _fail(
            "invalid_repeat_until",
            "repeat_until 必须且只能声明 max_iterations 和 carry",
            context,
        )
    maximum = keyword_values["max_iterations"]
    if (
        not isinstance(maximum, ast.Constant)
        or isinstance(maximum.value, bool)
        or not isinstance(maximum.value, int)
        or maximum.value < 1
    ):
        _fail("invalid_repeat_until", "max_iterations 必须是正整数字面量", maximum)
    carry_expression = keyword_values["carry"]
    if not isinstance(carry_expression, ast.Dict):
        _fail("invalid_loop_carry", "carry 必须是字符串键字典", carry_expression)
    node_uuid = state.anchors.get(statement.lineno - 1)
    if node_uuid is None:
        _fail(
            "invalid_node_anchor", "每个循环区域前必须有相邻节点 UUID 锚点", statement
        )
    if (
        loop_target.id in state.input_names
        or loop_target.id in available_results
        or loop_target.id in state.loop_carry_scopes
    ):
        _fail("invalid_repeat_until", "循环绑定名称与已有变量冲突", loop_target)

    initial_carry: list[tuple[str, ValueBinding]] = []
    carry_keys: set[str] = set()
    for key, value in zip(carry_expression.keys, carry_expression.values, strict=True):
        if not isinstance(key, ast.Constant) or not isinstance(key.value, str):
            _fail("invalid_loop_carry", "carry 键必须是非空字符串字面量", key)
        carry_key = key.value
        if not carry_key or carry_key in carry_keys:
            _fail("invalid_loop_carry", "carry 键必须唯一且非空", key)
        carry_keys.add(carry_key)
        initial_carry.append(
            (
                carry_key,
                _value_binding(
                    value,
                    input_names=state.input_names,
                    known_results=available_results,
                    material_results=state.material_results & available_results,
                    carry_scopes=state.loop_carry_scopes,
                ),
            )
        )
    if not statement.body or not _is_until_statement(statement.body[-1], state.imports):
        _fail("invalid_repeat_until", "until(...) 必须是循环体最后一条语句", statement)

    metadata = state.node_metadata.get(statement.lineno - 1)
    if parent_uuid is not None:
        state.parent_by_node[node_uuid] = parent_uuid
    state.source_order.append(node_uuid)
    body_start = len(state.source_order)
    executable_before = dict(state.executable_results)
    state.control_depth += 1
    state.loop_carry_scopes[loop_target.id] = (node_uuid, frozenset(carry_keys))
    body_results = set(available_results)
    first_entries: tuple[str, ...] = ()
    previous_exits: tuple[str, ...] = ()
    next_carry: list[tuple[str, ValueBinding]] | None = None
    try:
        for body_statement in statement.body[:-1]:
            next_call = _loop_next_call(body_statement, loop_target.id)
            if next_call is not None:
                if next_carry is not None:
                    _fail(
                        "invalid_loop_carry",
                        "每个循环只能声明一次 loop.next",
                        body_statement,
                    )
                next_carry = _parse_loop_next(
                    next_call,
                    expected_keys=carry_keys,
                    state=state,
                    available_results=body_results,
                )
                continue
            segment = _parse_statement(
                body_statement,
                state=state,
                available_results=body_results,
                parent_uuid=node_uuid,
            )
            if segment.entries:
                if previous_exits:
                    state.order_dependencies.extend(
                        (source_uuid, target_uuid)
                        for source_uuid in previous_exits
                        for target_uuid in segment.default_entries
                    )
                elif not first_entries:
                    first_entries = segment.entries
                previous_exits = segment.exits
            body_results.update(segment.result_names)
        if not first_entries:
            _fail("invalid_repeat_until", "循环体至少包含一个工作流节点", statement)
        if carry_keys and next_carry is None:
            _fail("invalid_loop_carry", "非空 carry 必须声明 loop.next", statement)
        if not carry_keys and next_carry is None:
            next_carry = []
        until_statement = statement.body[-1]
        assert isinstance(until_statement, ast.Expr)
        until_call = until_statement.value
        assert isinstance(until_call, ast.Call)
        if len(until_call.args) != 1 or until_call.keywords:
            _fail("invalid_repeat_until", "until 必须接收唯一退出条件", until_statement)
        until_condition = _condition_expression(
            until_call.args[0],
            input_names=state.input_names,
            known_results=body_results,
            carry_scopes=state.loop_carry_scopes,
        )
    finally:
        state.loop_carry_scopes.pop(loop_target.id, None)
        state.control_depth -= 1
        state.executable_results = executable_before

    body_node_uuids = tuple(state.source_order[body_start:])
    result_nodes = {
        declaration.result_name: declaration.node_uuid
        for declaration in state.actions
        if declaration.result_name in body_results
    }
    bindings: list[tuple[str, dict[str, str]]] = []
    for name in sorted(_condition_variable_names(until_condition)):
        if name in state.input_names:
            bindings.append((name, {"kind": "workflow_input", "parameter": name}))
        elif name in result_nodes:
            bindings.append(
                (name, {"kind": "node_result", "node_uuid": result_nodes[name]})
            )
        else:
            _fail("invalid_condition_expression", "循环条件变量缺少稳定来源", statement)
    state.repeats.append(
        RepeatUntilDeclaration(
            node_uuid=node_uuid,
            title=metadata[0] if metadata is not None else None,
            description=metadata[1] if metadata is not None else None,
            loop_variable=loop_target.id,
            max_iterations=maximum.value,
            initial_carry=tuple(initial_carry),
            next_carry=tuple(next_carry or []),
            until_condition=until_condition,
            bindings=tuple(bindings),
            node_uuids=body_node_uuids,
            entry_node_uuids=first_entries,
            exit_node_uuids=previous_exits,
            source_node=statement,
        )
    )
    return _Flow(
        entries=(node_uuid,),
        default_entries=(node_uuid,),
        exits=(node_uuid,),
        # 循环体结果只在本区域的 until/next 中可见；循环外必须通过稳定 carry
        # 或未来显式控制输出访问，不能绑定某个动态轮次的模板节点结果。
        result_names=frozenset(),
    )


def _is_until_statement(statement: ast.stmt, imports: Mapping[str, str]) -> bool:
    """判断语句是否是显式导入的 ``until`` 标记。"""

    return (
        isinstance(statement, ast.Expr)
        and isinstance(statement.value, ast.Call)
        and _is_marker(statement.value.func, dict(imports), "until")
    )


def _loop_next_call(statement: ast.stmt, loop_name: str) -> ast.Call | None:
    """识别当前循环绑定上的 ``loop.next(...)`` 声明。"""

    if not isinstance(statement, ast.Expr) or not isinstance(statement.value, ast.Call):
        return None
    call = statement.value
    if (
        isinstance(call.func, ast.Attribute)
        and call.func.attr == "next"
        and isinstance(call.func.value, ast.Name)
        and call.func.value.id == loop_name
    ):
        return call
    return None


def _parse_loop_next(
    call: ast.Call,
    *,
    expected_keys: set[str],
    state: _BodyState,
    available_results: set[str],
) -> list[tuple[str, ValueBinding]]:
    """解析下一轮 carry，并要求键集合与初始 carry 完全一致。"""

    if call.args or any(item.arg is None for item in call.keywords):
        _fail("invalid_loop_carry", "loop.next 只接受命名参数", call)
    names = [str(item.arg) for item in call.keywords]
    if len(names) != len(set(names)) or set(names) != expected_keys:
        _fail("invalid_loop_carry", "loop.next 必须完整且唯一地提交 carry 键", call)
    return [
        (
            str(item.arg),
            _value_binding(
                item.value,
                input_names=state.input_names,
                known_results=available_results,
                material_results=state.material_results & available_results,
                carry_scopes=state.loop_carry_scopes,
            ),
        )
        for item in call.keywords
    ]


def _parse_group(
    statement: ast.With,
    *,
    state: _BodyState,
    available_results: set[str],
    parent_uuid: str | None,
    parallel_scope: str | None,
    parallel_order: int | None,
) -> _Flow:
    """解析一个真实展示分组节点并递归解析其动作子节点。

    参数说明：``statement`` 是 ``with group``；``state`` 收集节点；
    ``available_results`` 是进入分组前可见结果；``parent_uuid`` 允许控制区域拥有
    展示分组，但仍拒绝分组直接嵌套分组；``parallel_scope``/``parallel_order``
    标记可选并行同级关系。
    返回：忽略分组节点本身后的真实动作入口/出口。异常：名称、锚点、空分组或
    嵌套不合法时失败关闭。
    """

    if parent_uuid is not None and parent_uuid in {
        group.node_uuid for group in state.groups
    }:
        _fail("unsupported_authoring_syntax", "暂不支持嵌套展示分组", statement)
    context = statement.items[0].context_expr
    assert isinstance(context, ast.Call)
    if context.args or any(item.arg is None for item in context.keywords):
        _fail("invalid_group", "group 只接受 name 命名参数", context)
    keyword_names = [str(item.arg) for item in context.keywords]
    if len(keyword_names) != len(set(keyword_names)) or set(keyword_names) != {"name"}:
        _fail("invalid_group", "group 必须且只能声明唯一 name", context)
    name_expression = context.keywords[0].value
    if (
        not isinstance(name_expression, ast.Constant)
        or not isinstance(name_expression.value, str)
        or not name_expression.value.strip()
    ):
        _fail("invalid_group", "group name 必须是非空字符串字面量", name_expression)
    node_uuid = state.anchors.get(statement.lineno - 1)
    if node_uuid is None:
        _fail(
            "invalid_node_anchor", "每个展示分组前必须有相邻节点 UUID 锚点", statement
        )
    metadata = state.node_metadata.get(statement.lineno - 1)
    declaration = GroupDeclaration(
        node_uuid=node_uuid,
        name=name_expression.value.strip(),
        title=metadata[0] if metadata is not None else None,
        description=metadata[1] if metadata is not None else None,
        parallel_scope=parallel_scope,
        parallel_order=parallel_order,
        source_node=statement,
    )
    state.groups.append(declaration)
    state.source_order.append(node_uuid)
    if parent_uuid is not None:
        state.parent_by_node[node_uuid] = parent_uuid
    child_results = set(available_results)
    flow = _parse_sequence(
        list(statement.body),
        state=state,
        available_results=child_results,
        parent_uuid=node_uuid,
    )
    if not flow.entries and not flow.result_names:
        _fail("invalid_group", "展示分组必须至少包含一个动作或物料声明", statement)
    return flow


def _parse_parallel_actions(
    statement: ast.With,
    *,
    state: _BodyState,
    available_results: set[str],
    parent_uuid: str | None,
) -> _Flow:
    """解析由动作语句直接组成的并行，分支之间不产生 ready 控制边。"""

    entries: list[str] = []
    default_entries: list[str] = []
    exits: list[str] = []
    merged_results: set[str] = set()
    base_results = set(available_results)
    for branch in statement.body:
        branch_results = set(base_results)
        action_count = len(state.actions)
        branch_flow = _parse_statement(
            branch,
            state=state,
            available_results=branch_results,
            parent_uuid=parent_uuid,
        )
        # 根物料来源只是入口声明，没有执行前沿；规范生成器可将独立声明
        # 放入 parallel。仅认可本分支新建的物料来源，其他空语句仍失败关闭。
        material_declaration = (
            len(state.actions) == action_count + 1
            and isinstance(state.actions[-1], MaterialSourceDeclaration)
        )
        if not branch_flow.entries and not material_declaration:
            _fail("invalid_parallel", "parallel 分支必须是一个动作", branch)
        duplicated = merged_results & set(branch_flow.result_names)
        if duplicated:
            _fail("unsupported_authoring_syntax", "并行分支结果变量重复", branch)
        merged_results.update(branch_flow.result_names)
        entries.extend(branch_flow.entries)
        default_entries.extend(branch_flow.default_entries)
        exits.extend(branch_flow.exits)
    available_results.update(merged_results)
    return _Flow(tuple(entries), tuple(default_entries), tuple(exits), frozenset(merged_results))


def _parse_parallel(
    statement: ast.With,
    *,
    state: _BodyState,
    available_results: set[str],
    parent_uuid: str | None,
) -> _Flow:
    """解析由直接展示分组构成的并行结构且隔离同级结果作用域。

    参数说明：``statement`` 是 ``with parallel``；``state`` 收集各分支事实；
    ``available_results`` 是并行开始前已完成且所有分支共享的结果。返回：所有分支
    入口、出口与合并后结果。异常：参数、非分组分支、嵌套并行、同级跨分支引用
    或重复结果失败关闭。
    """

    context = statement.items[0].context_expr
    assert isinstance(context, ast.Call)
    if context.args or any(keyword.arg != "depends_on" for keyword in context.keywords):
        _fail("invalid_parallel", "parallel 只接受 depends_on 命名参数", context)
    dependencies = _explicit_control_dependencies(context, executable_results=state.executable_results)

    def inherit(flow: _Flow) -> _Flow:
        # 块前序只作用于继承默认前沿的分支；节点显式覆盖仍有优先权。
        if dependencies is None:
            return flow
        state.order_dependencies.extend(
            (source, target) for source in dependencies for target in flow.default_entries
        )
        return _Flow(flow.entries, (), flow.exits, flow.result_names)
    if len(statement.body) >= 2 and all(
        not isinstance(branch, ast.With) for branch in statement.body
    ):
        # 没有展示分组的并行只用于互不依赖的动作。分支之间不建立 ready 边，
        # 这样画布上删掉的控制连线才能在源码往返后仍然不存在。
        return inherit(_parse_parallel_actions(
            statement,
            state=state,
            available_results=available_results,
            parent_uuid=parent_uuid,
        ))
    if len(statement.body) < 2 or any(
        not isinstance(branch, ast.With)
        or _with_marker(branch, state.imports) != "group"
        for branch in statement.body
    ):
        _fail("invalid_parallel", "parallel 必须直接包含至少两个展示分组", statement)
    group_uuids = [
        state.anchors.get(branch.lineno - 1)
        for branch in statement.body
        if isinstance(branch, ast.With)
    ]
    if any(group_uuid is None for group_uuid in group_uuids):
        _fail("invalid_node_anchor", "并行分组前必须有相邻节点 UUID 锚点", statement)
    parallel_scope = str(group_uuids[0])
    entries: list[str] = []
    default_entries: list[str] = []
    exits: list[str] = []
    merged_results: set[str] = set()
    base_results = set(available_results)
    for branch_order, branch in enumerate(statement.body):
        assert isinstance(branch, ast.With)
        branch_results = set(base_results)
        branch_flow = _parse_group(
            branch,
            state=state,
            available_results=branch_results,
            parent_uuid=parent_uuid,
            parallel_scope=parallel_scope,
            parallel_order=branch_order,
        )
        duplicated = merged_results & set(branch_flow.result_names)
        if duplicated:
            _fail("unsupported_authoring_syntax", "并行分支结果变量重复", branch)
        merged_results.update(branch_flow.result_names)
        entries.extend(branch_flow.entries)
        default_entries.extend(branch_flow.default_entries)
        exits.extend(branch_flow.exits)
    available_results.update(merged_results)
    return inherit(_Flow(
        tuple(entries),
        tuple(default_entries),
        tuple(exits),
        frozenset(merged_results),
    ))


def _action_declaration(
    statement: ast.stmt,
    *,
    imports: dict[str, str],
    devices: dict[str, DeviceDeclaration],
    input_names: set[str],
    known_results: set[str],
    material_results: set[str],
    anchors: dict[int, str],
    node_metadata: dict[int, tuple[str, str]],
    executable_results: Mapping[str, str],
    carry_scopes: Mapping[str, tuple[str, frozenset[str]]],
) -> (
    ActionDeclaration
    | CompositeDeclaration
    | MaterialSourceDeclaration
    | MaterialIODeclaration
):
    """解析一条 ``result = device.action(...)`` 动作声明。

    参数说明：各索引用于验证设备、输入、前序结果、相邻锚点和可选节点展示
    元数据；返回不可变动作、已发布工作流调用或物料来源声明，位置参数、动态
    调用或前向引用通过 ``AuthoringSyntaxError`` 失败关闭。
    异常：语法、身份或引用不合法时抛出 ``AuthoringSyntaxError``。
    """

    try:
        material_io = parse_material_io_declaration(
            statement,
            imports=imports,
            anchors=anchors,
            node_metadata=node_metadata,
            input_names=input_names,
            known_results=known_results,
            material_results=material_results,
        )
    except MaterialIOAuthoringError as error:
        raise AuthoringSyntaxError(error.code, error.message, error.node) from None
    if material_io is not None:
        return material_io
    material_source = parse_material_source_declaration(
        statement,
        imports=imports,
        anchors=anchors,
        node_metadata=node_metadata,
        input_names=input_names,
        known_results=known_results,
        material_results=material_results,
    )
    if material_source is not None:
        return material_source
    if not isinstance(statement, ast.Assign) or len(statement.targets) != 1:
        _fail("unsupported_authoring_syntax", "工作流函数只允许动作赋值", statement)
    target = statement.targets[0]
    call = statement.value
    if not isinstance(target, ast.Name) or not isinstance(call, ast.Call):
        _fail("unsupported_authoring_syntax", "动作必须赋值给简单名称", statement)
    if isinstance(call.func, ast.Name):
        import_identity = imports.get(call.func.id)
        if (
            call.args
            or not isinstance(import_identity, str)
            or import_identity.count(":") != 1
            or import_identity in _AUTHORING_MARKERS.values()
            or import_identity == _RESOURCE_REF
        ):
            _fail(
                "unsupported_authoring_syntax",
                "已发布工作流必须通过绝对导入并只接受命名参数",
                statement,
            )
        node_uuid = anchors.get(statement.lineno - 1)
        if node_uuid is None:
            _fail(
                "invalid_node_anchor",
                "每个工作流调用前必须有相邻节点 UUID 锚点",
                statement,
            )
        metadata = node_metadata.get(statement.lineno - 1)
        control_dependencies = _explicit_control_dependencies(
            call, executable_results=executable_results,
        )
        arguments: list[tuple[str, ValueBinding]] = []
        names: set[str] = set()
        for item in call.keywords:
            if item.arg == "depends_on":
                continue
            if item.arg is None or item.arg in names:
                _fail(
                    "invalid_action_arguments", "工作流调用参数重复或包含 ** 展开", call
                )
            names.add(item.arg)
            # ``resource_binding`` 让组合工作流（Composite Workflow）与普通
            # 动作共享同一部署资源引用语法；此处只保存静态业务身份，实际物料
            # UUID 仍由工作流创作组合根注入的库存权威（Inventory Authority）解析。
            resource_binding = _resource_ref_binding(item.value, imports=imports)
            site_group_binding = _site_group_binding(
                item.value,
                imports=imports,
                input_names=input_names,
            )
            arguments.append(
                (
                    item.arg,
                    resource_binding
                    or site_group_binding
                    or _value_binding(
                        item.value,
                        input_names=input_names,
                        known_results=known_results,
                        material_results=material_results,
                        carry_scopes=carry_scopes,
                    ),
                )
            )
        module, symbol = import_identity.split(":", 1)
        return CompositeDeclaration(
            node_uuid=node_uuid,
            result_name=target.id,
            title=metadata[0] if metadata is not None else None,
            description=metadata[1] if metadata is not None else None,
            module=module,
            symbol=symbol,
            arguments=tuple(arguments),
            control_dependencies=control_dependencies,
            source_node=statement,
        )
    if (
        call.args
        or not isinstance(call.func, ast.Attribute)
        or not isinstance(call.func.value, ast.Name)
    ):
        _fail(
            "unsupported_authoring_syntax",
            "动作只接受命名参数和静态设备选择器",
            statement,
        )
    device_symbol = call.func.value.id
    if device_symbol not in devices:
        _fail("invalid_device_selector", "动作引用了未知设备选择器", statement)
    node_uuid = anchors.get(statement.lineno - 1)
    if node_uuid is None:
        _fail("invalid_node_anchor", "每个动作前必须有相邻节点 UUID 锚点", statement)
    metadata = node_metadata.get(statement.lineno - 1)
    title = metadata[0] if metadata is not None else None
    description = metadata[1] if metadata is not None else None
    control_dependencies = _explicit_control_dependencies(
        call,
        executable_results=executable_results,
    )
    arguments: list[tuple[str, ValueBinding]] = []
    names: set[str] = set()
    for keyword in call.keywords:
        if keyword.arg == "depends_on":
            continue
        if keyword.arg is None or keyword.arg in names:
            _fail("invalid_action_arguments", "动作命名参数重复或包含 ** 展开", call)
        names.add(keyword.arg)
        # ``resource_binding`` 只识别显式导入的编译标记；解析发生在候选图层，
        # 这里保留部署业务资源 ID，绝不把它误当成实际物料 UUID。
        resource_binding = _resource_ref_binding(keyword.value, imports=imports)
        site_group_binding = _site_group_binding(
            keyword.value,
            imports=imports,
            input_names=input_names,
        )
        arguments.append(
            (
                keyword.arg,
                resource_binding
                or site_group_binding
                or _value_binding(
                    keyword.value,
                    input_names=input_names,
                    known_results=known_results,
                    material_results=material_results,
                    carry_scopes=carry_scopes,
                ),
            )
        )
    return ActionDeclaration(
        node_uuid=node_uuid,
        result_name=target.id,
        title=title,
        description=description,
        device_symbol=device_symbol,
        action_name=call.func.attr,
        arguments=tuple(arguments),
        control_dependencies=control_dependencies,
        source_node=statement,
    )


def _explicit_control_dependencies(
    call: ast.Call,
    *,
    executable_results: Mapping[str, str],
) -> tuple[str, ...] | None:
    """解析作者专用 ``depends_on`` 并返回上游可执行节点 UUID。

    未声明时返回 ``None``，表示保留词法顺序产生的默认控制前序；显式空列表返回
    空元组，表示删除默认控制前序。首版只接受无重复的列表字面量，且每一项必须是
    已在源码前方声明的可执行结果变量。该参数不会进入动作业务参数合同。
    """

    keywords = [item for item in call.keywords if item.arg == "depends_on"]
    if not keywords:
        return None
    if len(keywords) != 1:
        _fail("invalid_explicit_dependency", "depends_on 只能声明一次", call)
    expression = keywords[0].value
    if not isinstance(expression, ast.List):
        _fail(
            "invalid_explicit_dependency",
            "depends_on 必须是前序可执行结果变量的列表字面量",
            expression,
        )
    names: list[str] = []
    for item in expression.elts:
        if not isinstance(item, ast.Name) or item.id not in executable_results:
            _fail(
                "invalid_explicit_dependency",
                "depends_on 只能引用源码前方的可执行结果变量",
                item,
            )
        if item.id in names:
            _fail(
                "invalid_explicit_dependency",
                "depends_on 不能重复引用同一节点",
                item,
            )
        names.append(item.id)
    return tuple(executable_results[name] for name in names)


def _resource_ref_binding(
    expression: ast.expr,
    *,
    imports: Mapping[str, str],
) -> ValueBinding | None:
    """识别动作或已发布工作流参数中的静态 ``resource_ref`` 声明。

    参数：``expression`` 是动作参数 AST，``imports`` 证明局部函数身份。返回：
    非 ``resource_ref`` 调用时为 ``None``，合法调用返回保存部署业务资源 ID 的
    ``resource_ref`` 绑定。异常：参数不是单个无首尾空白字符串时抛出稳定
    ``AuthoringSyntaxError``，不得降级成普通字面量。
    """

    if (
        not isinstance(expression, ast.Call)
        or not isinstance(expression.func, ast.Name)
        or imports.get(expression.func.id) != _RESOURCE_REF
    ):
        return None
    if len(expression.args) != 1 or expression.keywords:
        _fail(
            "invalid_action_arguments",
            "resource_ref 必须接收单个静态资源 ID",
            expression,
        )
    try:
        # ``resource_id`` 是部署资源业务身份，仍需由库存权威解析为实际物料 UUID。
        resource_id = ast.literal_eval(expression.args[0])
    except (TypeError, ValueError):
        _fail(
            "invalid_action_arguments",
            "resource_ref 必须接收单个静态资源 ID",
            expression,
        )
    if (
        not isinstance(resource_id, str)
        or not resource_id.strip()
        or resource_id != resource_id.strip()
    ):
        _fail(
            "invalid_action_arguments",
            "resource_ref 必须接收无首尾空白的非空资源 ID",
            expression,
        )
    return ValueBinding("resource_ref", resource_id)


def _site_group_binding(
    expression: ast.expr,
    *,
    imports: Mapping[str, str],
    input_names: set[str],
) -> ValueBinding | None:
    """识别命名库位组的静态作者标记。

    参数：``expression`` 是动作参数 AST，``imports`` 证明本地函数身份。返回：
    非 ``site_group`` 调用返回 ``None``；合法调用返回组键绑定。异常：参数不是
    单个无首尾空白字符串时关闭式抛出稳定作者语法错误；可选 ``exact`` 必须
    直接引用本工作流输入参数。
    """

    if (
        not isinstance(expression, ast.Call)
        or not isinstance(expression.func, ast.Name)
        or imports.get(expression.func.id) != _SITE_GROUP
    ):
        return None
    if len(expression.args) != 1 or len(expression.keywords) > 1:
        _fail(
            "invalid_action_arguments",
            "site_group 必须接收静态组键和可选 exact 工作流参数",
            expression,
        )
    try:
        group_key = ast.literal_eval(expression.args[0])
    except (TypeError, ValueError):
        _fail(
            "invalid_action_arguments",
            "site_group 必须接收单个静态库位组键",
            expression,
        )
    if (
        not isinstance(group_key, str)
        or not group_key.strip()
        or group_key != group_key.strip()
    ):
        _fail(
            "invalid_action_arguments",
            "site_group 必须接收无首尾空白的非空库位组键",
            expression,
        )
    exact_parameter = ""
    if expression.keywords:
        keyword = expression.keywords[0]
        if (
            keyword.arg != "exact"
            or not isinstance(keyword.value, ast.Name)
            or keyword.value.id not in input_names
        ):
            _fail(
                "invalid_action_arguments",
                "site_group.exact 必须直接引用工作流输入参数",
                expression,
            )
        exact_parameter = keyword.value.id
    return ValueBinding(
        "site_group",
        {
            "group_key": group_key,
            "exact_parameter": exact_parameter,
        },
    )


def _workflow_outputs(
    statement: ast.Return,
    *,
    imports: dict[str, str],
    input_names: set[str],
    known_results: set[str],
    material_results: set[str],
) -> list[tuple[str, ValueBinding]]:
    """解析命名工作流输出绑定。

    参数说明：``statement`` 是末尾 return，其他索引用于静态身份解析；返回有序
    输出二元组列表，动态输出或重复名称失败关闭。
    """

    expression = statement.value
    if isinstance(expression, ast.Dict):
        outputs: list[tuple[str, ValueBinding]] = []
        names: set[str] = set()
        for key, value in zip(expression.keys, expression.values, strict=True):
            if not isinstance(key, ast.Constant) or not isinstance(key.value, str):
                _fail(
                    "invalid_workflow_output",
                    "结果记录键必须是字符串字面量",
                    expression,
                )
            if key.value in names:
                _fail("invalid_workflow_output", "工作流输出名称重复", expression)
            names.add(key.value)
            outputs.append(
                (
                    key.value,
                    _value_binding(
                        value,
                        input_names=input_names,
                        known_results=known_results,
                        material_results=material_results,
                        allow_literal=False,
                    ),
                )
            )
        return outputs
    call = expression
    if not isinstance(call, ast.Call) or not _is_marker(
        call.func, imports, "workflow_output"
    ):
        _fail(
            "invalid_workflow_output",
            "工作流必须返回结果字典或 workflow_output(...) ",
            statement,
        )
    if call.args:
        _fail("invalid_workflow_output", "workflow_output 只接受命名参数", call)
    outputs: list[tuple[str, ValueBinding]] = []
    names: set[str] = set()
    for keyword in call.keywords:
        if keyword.arg is None or keyword.arg in names:
            _fail("invalid_workflow_output", "工作流输出名称重复或包含 ** 展开", call)
        names.add(keyword.arg)
        binding = _value_binding(
            keyword.value,
            input_names=input_names,
            known_results=known_results,
            material_results=material_results,
            allow_literal=False,
        )
        outputs.append((keyword.arg, binding))
    return outputs


def _value_binding(
    expression: ast.expr,
    *,
    input_names: set[str],
    known_results: set[str],
    material_results: set[str],
    allow_literal: bool = True,
    carry_scopes: Mapping[str, tuple[str, frozenset[str]]] | None = None,
) -> ValueBinding:
    """把参数表达式解析为字面量、工作流输入或节点输出绑定。

    参数：AST 表达式和引用身份集合。返回静态绑定；输出可禁止字面量。
    """

    if isinstance(expression, ast.Name) and expression.id in input_names:
        return ValueBinding("workflow_input", expression.id)
    if isinstance(expression, ast.Name) and expression.id in material_results:
        return ValueBinding("node_output", "material", expression.id)
    if (
        isinstance(expression, ast.Attribute)
        and isinstance(expression.value, ast.Name)
        and expression.value.id in known_results
    ):
        return ValueBinding("node_output", expression.attr, expression.value.id)
    if (
        carry_scopes is not None
        and isinstance(expression, ast.Subscript)
        and isinstance(expression.value, ast.Attribute)
        and expression.value.attr == "carry"
        and isinstance(expression.value.value, ast.Name)
        and expression.value.value.id in carry_scopes
        and isinstance(expression.slice, ast.Constant)
        and isinstance(expression.slice.value, str)
    ):
        region_uuid, keys = carry_scopes[expression.value.value.id]
        if expression.slice.value not in keys:
            _fail("invalid_loop_carry", "循环 carry 引用了未知键", expression)
        return ValueBinding(
            "loop_carry",
            {"control_region_uuid": region_uuid, "key": expression.slice.value},
        )
    if allow_literal:
        try:
            return ValueBinding("literal", ast.literal_eval(expression))
        except (ValueError, TypeError):
            pass
    _fail(
        "unsupported_authoring_syntax",
        "值必须是 JSON 字面量、工作流输入或前序节点输出",
        expression,
    )


def _literal_keywords(call: ast.Call, code: str) -> dict[str, Any]:
    """读取无重复的字面量关键字参数。

    参数说明：``call`` 是静态标记调用，``code`` 是失败诊断码；返回名称到 JSON
    字面量的映射，动态值、重复名称或 ``**`` 展开失败关闭。
    """

    values: dict[str, Any] = {}
    for keyword in call.keywords:
        if keyword.arg is None or keyword.arg in values:
            _fail(code, "静态标记关键字重复或包含 ** 展开", call)
        try:
            values[keyword.arg] = ast.literal_eval(keyword.value)
        except (ValueError, TypeError):
            _fail(code, "静态标记参数必须是字面量", keyword.value)
    return values


def _is_marker(
    expression: ast.expr,
    imports: dict[str, str],
    marker_name: str,
) -> bool:
    """判断表达式是否引用一个显式导入的创作标记。

    参数说明：``expression`` 是调用目标，``imports`` 是局部身份表，
    ``marker_name`` 是标准标记名；返回布尔结果。
    """

    return (
        isinstance(expression, ast.Name)
        and imports.get(expression.id) == _AUTHORING_MARKERS[marker_name]
    )


def _fail(code: str, message: str, node: ast.AST | None = None) -> Never:
    """抛出稳定作者语法错误。

    参数说明：``code``、``message`` 和 ``node`` 分别是机器码、中文消息与可选
    位置；函数永不返回。
    """

    raise AuthoringSyntaxError(code, message, node)


__all__ = [
    "ActionDeclaration",
    "AuthoringSyntaxError",
    "CompositeDeclaration",
    "DeviceDeclaration",
    "GroupDeclaration",
    "MaterialIODeclaration",
    "QuantityRequirementDeclaration",
    "RepeatUntilDeclaration",
    "ResourceScopeDeclaration",
    "ValueBinding",
    "WorkflowProgram",
    "author_source_map",
    "diagnostic_source_range",
    "parse_authoring_source",
]
