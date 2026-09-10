"""可信工作流作者源码的纯 AST（抽象语法树）解析层。"""

from __future__ import annotations

import ast
import re
import tokenize
from collections.abc import Mapping
from dataclasses import dataclass
from io import StringIO
from typing import Any, Never

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
from unilabos.workflow.models import CandidateSourceMapEntry, validate_uuid
from unilabos.workflow.source_coordinates import (
    codepoint_offset_to_utf16_column,
    source_lines,
    utf16_length,
    utf8_offset_to_utf16_column,
)

_NODE_ANCHOR = re.compile(
    r"^[ \t]*#[ \t]*unilab:node_uuid=([0-9a-fA-F-]{36})[ \t]*$"
)
_NODE_METADATA_PREFIX = re.compile(r"^[ \t]*#[ \t]*\[")
_NODE_METADATA = re.compile(
    r"^(?P<indent>[ \t]*)#[ \t]*\[(?P<title>[^]\r\n]+)\]"
    r"(?:(?:[ \t]*:[ \t]*)|(?:[ \t]+))"
    r"(?P<description>\S(?:.*\S)?)[ \t]*$"
)
_AUTHORING_MARKERS = {
    "device": "unilabos.workflow.authoring:device",
    "group": "unilabos.workflow.authoring:group",
    "parallel": "unilabos.workflow.authoring:parallel",
    "workflow": "unilabos.workflow.authoring:workflow",
    "workflow_definition": "unilabos.workflow.authoring:workflow_definition",
    "workflow_output": "unilabos.workflow.authoring:workflow_output",
    "MaterialCustodyPolicy": "unilabos.workflow.authoring:MaterialCustodyPolicy",
}
_RESOURCE_REF = "unilabos.workflow.authoring:resource_ref"


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
class WorkflowProgram:
    """作者源码静态子集解析后的不可变中间表示。"""

    workflow_uuid: str
    function_name: str
    function_docstring: str | None
    display_name: str
    description: str | None
    imports: tuple[tuple[str, str], ...]
    devices: tuple[DeviceDeclaration, ...]
    input_contract: dict[str, Any]
    input_resource_template_symbols: tuple[tuple[str, tuple[str, ...]], ...]
    result_record_name: str | None
    declared_output_schemas: tuple[tuple[str, dict[str, Any]], ...]
    output_resource_template_symbols: tuple[tuple[str, tuple[str, ...]], ...]
    actions: tuple[
        ActionDeclaration | CompositeDeclaration | MaterialSourceDeclaration,
        ...,
    ]
    groups: tuple[GroupDeclaration, ...]
    parent_by_node: tuple[tuple[str, str], ...]
    order_dependencies: tuple[tuple[str, str], ...]
    source_order: tuple[str, ...]
    outputs: tuple[tuple[str, ValueBinding], ...]


@dataclass(frozen=True, slots=True)
class _Flow:
    """一段作者源码对执行图公开的入口、出口和新结果名。"""

    entries: tuple[str, ...]
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
        ActionDeclaration | CompositeDeclaration | MaterialSourceDeclaration
    ]
    groups: list[GroupDeclaration]
    parent_by_node: dict[str, str]
    order_dependencies: list[tuple[str, str]]
    source_order: list[str]
    material_results: set[str]


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
    workflow_uuid, display_name, description = _workflow_declaration(
        function,
        imports,
    )
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
        output_resource_template_symbols,
    ) = _result_record(
        function,
        result_records=result_records,
        imports=imports,
    )
    anchors = _source_anchors(python_source)
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
    used_anchor_lines = {
        declaration.source_node.lineno - 1
        for declaration in (*actions, *groups)
    }
    if set(anchors) != used_anchor_lines:
        _fail("invalid_node_anchor", "节点 UUID 锚点必须紧邻一个动作声明")
    return WorkflowProgram(
        workflow_uuid=workflow_uuid,
        function_name=function.name,
        function_docstring=function_docstring,
        display_name=display_name,
        description=description,
        imports=tuple(sorted(imports.items())),
        devices=tuple(devices),
        input_contract=input_contract,
        input_resource_template_symbols=input_resource_template_symbols,
        result_record_name=result_record_name,
        declared_output_schemas=tuple(declared_output_schemas.items()),
        output_resource_template_symbols=output_resource_template_symbols,
        actions=tuple(actions),
        groups=tuple(groups),
        parent_by_node=tuple(sorted(parent_by_node.items())),
        order_dependencies=tuple(order_dependencies),
        source_order=tuple(authoring_source_order),
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
        for declaration in (*program.actions, *program.groups)
    }
    if set(program.source_order) != set(declarations):
        raise ValueError("作者程序的节点顺序与声明不一致")
    source_map: list[dict[str, Any]] = []
    for node_uuid in program.source_order:
        declaration = declarations[node_uuid]
        source_node = declaration.source_node
        start_line = source_node.lineno - (
            2 if declaration.title is not None else 1
        )
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
                _add_import(imports, local_name, f"{statement.module}:{alias.name}", statement)
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

    if not isinstance(statement.target, ast.Name) or not isinstance(statement.annotation, ast.Name):
        _fail("invalid_device_selector", "设备声明必须使用简单名称和导入类型", statement)
    class_identity = imports.get(statement.annotation.id)
    if not isinstance(class_identity, str) or ":" not in class_identity:
        _fail("invalid_device_selector", "设备类型必须来自显式导入", statement)
    call = statement.value
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
) -> tuple[str, str, str | None]:
    """读取工作流定义装饰器的稳定元数据。

    参数说明：``function`` 是唯一函数，``imports`` 用于识别装饰器；返回工作流
    UUID、展示名和可选描述。位置参数、动态值或重复字段失败关闭。
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
        _fail("invalid_workflow_declaration", "工作流函数必须只有 workflow 装饰器", function)
    declaration = declarations[0]
    if declaration.args:
        _fail("invalid_workflow_declaration", "工作流声明不接受位置参数", declaration)
    values = _literal_keywords(declaration, "invalid_workflow_declaration")
    if set(values) - {"workflow_uuid", "displayname", "description"}:
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
    return workflow_uuid, display_name.strip(), description


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
        for argument, default in zip(arguments.kwonlyargs, arguments.kw_defaults, strict=True):
            if argument.annotation is None:
                _fail("invalid_workflow_parameters", "工作流输入必须带类型注解", argument)
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
    tuple[tuple[str, tuple[str, ...]], ...],
]:
    """解析可选 ``TypedDict`` 工作流结果记录。

    参数说明：``function`` 提供返回注解，``result_records`` 是模块级类声明，
    ``imports`` 用于识别 ``TypedDict`` 和字段注解；返回记录类名、字段 Schema
    及按字段保存的资源模板源码身份。未声明返回记录时返回空记录，动态或不一致
    声明失败关闭。
    异常：返回注解、结果记录数量、字段 Schema 或资源模板身份无效时抛出
    ``AuthoringSyntaxError``。
    """

    if not result_records:
        if not _is_none_return_annotation(function.returns):
            _fail("invalid_workflow_output", "工作流返回注解必须引用 TypedDict 结果记录", function)
        return None, {}, ()
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
    resource_templates: list[tuple[str, tuple[str, ...]]] = []
    try:
        for statement in record.body:
            if (
                not isinstance(statement, ast.AnnAssign)
                or not isinstance(statement.target, ast.Name)
                or statement.value is not None
            ):
                _fail("invalid_workflow_output", "结果记录只允许带类型的字段", statement)
            name = statement.target.id
            if name in fields:
                _fail("invalid_workflow_output", "结果记录字段重复", statement)
            parsed = parse_result_annotation(
                name,
                statement.annotation,
                imports=imports,
            )
            fields[name] = parsed.to_dict()["schema"]
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
    return record.name, fields, tuple(resource_templates)


def _is_none_return_annotation(annotation: ast.expr | None) -> bool:
    """判断返回注解是否表示无工作流输出。

    参数：``annotation`` 是工作流函数的可选返回注解。
    返回：未注解或显式 ``None`` 时返回真，其余注解返回假。
    异常：无；本函数只检查静态语法节点，不解析或执行名称。
    """

    return annotation is None or (
        isinstance(annotation, ast.Constant) and annotation.value is None
    )


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
        anchor_indent = anchor_source[: len(anchor_source) - len(anchor_source.lstrip())]
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
    list[ActionDeclaration | CompositeDeclaration | MaterialSourceDeclaration],
    list[GroupDeclaration],
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
    if statements and isinstance(statements[0], ast.Expr) and isinstance(
        statements[0].value, ast.Constant
    ) and isinstance(statements[0].value.value, str):
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
        parent_by_node={},
        order_dependencies=[],
        source_order=[],
        material_results=set(),
    )
    # ``known_results`` 只在递归边界复制，保证同级并行分支互不可见。
    known_results: set[str] = set()
    _parse_sequence(
        statements,
        state=state,
        available_results=known_results,
        parent_uuid=None,
    )
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
                    for target_uuid in segment.entries
                )
            elif not first_entries:
                first_entries = segment.entries
            previous_exits = segment.exits
        available_results.update(segment.result_names)
    return _Flow(
        entries=first_entries,
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
            if parent_uuid is not None:
                _fail(
                    "unsupported_authoring_syntax",
                    "并行结构不能嵌套在展示分组中",
                    statement,
                )
            return _parse_parallel(
                statement,
                state=state,
                available_results=available_results,
            )
        _fail("unsupported_authoring_syntax", "工作流不支持该 with 语句", statement)

    action = _action_declaration(
        statement,
        imports=state.imports,
        devices=state.devices,
        input_names=state.input_names,
        known_results=available_results,
        material_results=state.material_results & available_results,
        anchors=state.anchors,
        node_metadata=state.node_metadata,
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
        return _Flow((), (), frozenset({action.result_name}))
    return _Flow(
        (action.node_uuid,),
        (action.node_uuid,),
        frozenset({action.result_name}),
    )


def _with_marker(statement: ast.With, imports: dict[str, str]) -> str | None:
    """识别单上下文 ``with group`` 或 ``with parallel`` 标记。

    参数说明：``statement`` 是静态 ``with``；``imports`` 证明标记来源。返回：
    ``group``、``parallel`` 或 ``None``。异常：多个上下文或 ``as`` 绑定不属于
    可信作者子集，直接返回 ``None`` 交由调用者产生稳定诊断。
    """

    if len(statement.items) != 1 or statement.items[0].optional_vars is not None:
        return None
    context = statement.items[0].context_expr
    if not isinstance(context, ast.Call):
        return None
    for marker_name in ("group", "parallel"):
        if _is_marker(context.func, imports, marker_name):
            return marker_name
    return None


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
    ``available_results`` 是进入分组前可见结果；``parent_uuid`` 用于拒绝当前未支持
    的嵌套分组；``parallel_scope``/``parallel_order`` 标记可选并行同级关系。
    返回：忽略分组节点本身后的真实动作入口/出口。异常：名称、锚点、空分组或
    嵌套不合法时失败关闭。
    """

    if parent_uuid is not None:
        _fail("unsupported_authoring_syntax", "暂不支持嵌套展示分组", statement)
    context = statement.items[0].context_expr
    assert isinstance(context, ast.Call)
    if context.args or any(item.arg is None for item in context.keywords):
        _fail("invalid_group", "group 只接受 name 命名参数", context)
    keyword_names = [str(item.arg) for item in context.keywords]
    if len(keyword_names) != len(set(keyword_names)) or set(keyword_names) != {"name"}:
        _fail("invalid_group", "group 必须且只能声明唯一 name", context)
    name_expression = context.keywords[0].value
    if not isinstance(name_expression, ast.Constant) or not isinstance(
        name_expression.value, str
    ) or not name_expression.value.strip():
        _fail("invalid_group", "group name 必须是非空字符串字面量", name_expression)
    node_uuid = state.anchors.get(statement.lineno - 1)
    if node_uuid is None:
        _fail("invalid_node_anchor", "每个展示分组前必须有相邻节点 UUID 锚点", statement)
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
    child_results = set(available_results)
    flow = _parse_sequence(
        list(statement.body),
        state=state,
        available_results=child_results,
        parent_uuid=node_uuid,
    )
    if not flow.entries:
        _fail("invalid_group", "展示分组必须至少包含一个可执行动作", statement)
    return flow


def _parse_parallel(
    statement: ast.With,
    *,
    state: _BodyState,
    available_results: set[str],
) -> _Flow:
    """解析由直接展示分组构成的并行结构且隔离同级结果作用域。

    参数说明：``statement`` 是 ``with parallel``；``state`` 收集各分支事实；
    ``available_results`` 是并行开始前已完成且所有分支共享的结果。返回：所有分支
    入口、出口与合并后结果。异常：参数、非分组分支、嵌套并行、同级跨分支引用
    或重复结果失败关闭。
    """

    context = statement.items[0].context_expr
    assert isinstance(context, ast.Call)
    if context.args or context.keywords:
        _fail("invalid_parallel", "parallel 不接受参数", context)
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
            parent_uuid=None,
            parallel_scope=parallel_scope,
            parallel_order=branch_order,
        )
        duplicated = merged_results & set(branch_flow.result_names)
        if duplicated:
            _fail("unsupported_authoring_syntax", "并行分支结果变量重复", branch)
        merged_results.update(branch_flow.result_names)
        entries.extend(branch_flow.entries)
        exits.extend(branch_flow.exits)
    available_results.update(merged_results)
    return _Flow(tuple(entries), tuple(exits), frozenset(merged_results))


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
) -> ActionDeclaration | CompositeDeclaration | MaterialSourceDeclaration:
    """解析一条 ``result = device.action(...)`` 动作声明。

    参数说明：各索引用于验证设备、输入、前序结果、相邻锚点和可选节点展示
    元数据；返回不可变动作、已发布工作流调用或物料来源声明，位置参数、动态
    调用或前向引用通过 ``AuthoringSyntaxError`` 失败关闭。
    异常：语法、身份或引用不合法时抛出 ``AuthoringSyntaxError``。
    """

    material_source = parse_material_source_declaration(
        statement,
        imports=imports,
        anchors=anchors,
        node_metadata=node_metadata,
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
            _fail("invalid_node_anchor", "每个工作流调用前必须有相邻节点 UUID 锚点", statement)
        metadata = node_metadata.get(statement.lineno - 1)
        arguments: list[tuple[str, ValueBinding]] = []
        names: set[str] = set()
        for item in call.keywords:
            if item.arg is None or item.arg in names:
                _fail("invalid_action_arguments", "工作流调用参数重复或包含 ** 展开", call)
            names.add(item.arg)
            # ``resource_binding`` 让组合工作流（Composite Workflow）与普通
            # 动作共享同一部署资源引用语法；此处只保存静态业务身份，实际物料
            # UUID 仍由工作流创作组合根注入的库存权威（Inventory Authority）解析。
            resource_binding = _resource_ref_binding(item.value, imports=imports)
            arguments.append(
                (
                    item.arg,
                    resource_binding
                    or _value_binding(
                        item.value,
                        input_names=input_names,
                        known_results=known_results,
                        material_results=material_results,
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
            source_node=statement,
        )
    if (
        call.args
        or not isinstance(call.func, ast.Attribute)
        or not isinstance(call.func.value, ast.Name)
    ):
        _fail("unsupported_authoring_syntax", "动作只接受命名参数和静态设备选择器", statement)
    device_symbol = call.func.value.id
    if device_symbol not in devices:
        _fail("invalid_device_selector", "动作引用了未知设备选择器", statement)
    node_uuid = anchors.get(statement.lineno - 1)
    if node_uuid is None:
        _fail("invalid_node_anchor", "每个动作前必须有相邻节点 UUID 锚点", statement)
    metadata = node_metadata.get(statement.lineno - 1)
    title = metadata[0] if metadata is not None else None
    description = metadata[1] if metadata is not None else None
    arguments: list[tuple[str, ValueBinding]] = []
    names: set[str] = set()
    for keyword in call.keywords:
        if keyword.arg is None or keyword.arg in names:
            _fail("invalid_action_arguments", "动作命名参数重复或包含 ** 展开", call)
        names.add(keyword.arg)
        # ``resource_binding`` 只识别显式导入的编译标记；解析发生在候选图层，
        # 这里保留部署业务资源 ID，绝不把它误当成实际物料 UUID。
        resource_binding = _resource_ref_binding(keyword.value, imports=imports)
        arguments.append(
            (
                keyword.arg,
                resource_binding
                or _value_binding(
                    keyword.value,
                    input_names=input_names,
                    known_results=known_results,
                    material_results=material_results,
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
        source_node=statement,
    )


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
                _fail("invalid_workflow_output", "结果记录键必须是字符串字面量", expression)
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
    if not isinstance(call, ast.Call) or not _is_marker(call.func, imports, "workflow_output"):
        _fail("invalid_workflow_output", "工作流必须返回结果字典或 workflow_output(...) ", statement)
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
    if allow_literal:
        try:
            return ValueBinding("literal", ast.literal_eval(expression))
        except (ValueError, TypeError):
            pass
    _fail("unsupported_authoring_syntax", "值必须是 JSON 字面量、工作流输入或前序节点输出", expression)


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
    "ValueBinding",
    "WorkflowProgram",
    "author_source_map",
    "diagnostic_source_range",
    "parse_authoring_source",
]
