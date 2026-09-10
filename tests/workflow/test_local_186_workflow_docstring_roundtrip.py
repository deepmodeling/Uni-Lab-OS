"""LOCAL-186 工作流函数 docstring 的规范化与源码映射回归合同。"""

from __future__ import annotations

import ast

from .test_authoring_engine import (
    ANALYZE_NODE_UUID,
    PREPARE_NODE_UUID,
    _compile,
    _engine,
)
from .test_authoring_syntax import _annotated_source

# ``_WORKFLOW_DOCSTRING`` 是规范化前后必须保持等价的中文工作流函数合同。
_WORKFLOW_DOCSTRING = """执行单样品的预处理与分析。

参数：
    sample：待处理物料。
    cycles：处理循环次数。
    mode：分析模式。
返回：
    样品与报告结果。"""

# ``_CANONICAL_SOURCE_PREFIX`` 是规范作者源码中工作流装饰器之前的稳定声明区。
_CANONICAL_SOURCE_PREFIX = """from typing import Annotated, Literal, TypedDict

from pydantic import Field
from lab.devices import Reactor
from unilabos.registry.placeholder_type import ResourceSlot
from unilabos.workflow.authoring import device, workflow


class PrepareSampleResult(TypedDict):
    sample: ResourceSlot
    report: str


reactor: Reactor = device()


"""

# ``_NO_DOCSTRING_FUNCTION`` 是功能修改前既有规范输出的独立金丝雀文本。
_NO_DOCSTRING_FUNCTION = """@workflow(
    workflow_uuid="10000000-0000-4000-8000-000000000001",
    displayname='Sample preparation',
    description='Prepare and analyze one sample.',
)
def prepare_sample(
    *,
    sample: ResourceSlot,
    cycles: Annotated[int, Field(ge=1, le=10)] = 3,
    mode: Literal['fast', 'safe'] = 'safe',
) -> PrepareSampleResult:
    # [加入预混液]: PCR 中预混液的分配
    # unilab:node_uuid=20000000-0000-4000-8000-000000000001
    prepared = reactor.prepare(cycles=cycles, sample=sample)
    # [分析产物]: PCR产物的质量分析
    # unilab:node_uuid=20000000-0000-4000-8000-000000000002
    analyzed = reactor.analyze(label=mode, prepared=prepared.prepared)
    return {'sample': prepared.prepared, 'report': analyzed.report}
"""


def _source_with_docstring() -> str:
    """构造带中文多行函数 docstring 与节点展示注释的作者源码。

    参数：无。
    返回：在第一个工作流节点前插入规范函数 docstring 的完整作者源码。
    异常：基础夹具结构漂移而找不到唯一函数体入口时抛出 ``AssertionError``。
    """

    # ``source`` 是已经规范化的作者源码，包含两个稳定工作流节点身份。
    source = _CANONICAL_SOURCE_PREFIX + _NO_DOCSTRING_FUNCTION
    # ``body_marker`` 精确标识函数签名结束与第一个节点展示注释之间的接缝。
    body_marker = (
        ") -> PrepareSampleResult:\n"
        "    # [加入预混液]: PCR 中预混液的分配"
    )
    assert source.count(body_marker) == 1
    # ``rendered_docstring`` 是包作者输入的标准三引号中文函数合同。
    rendered_docstring = ''') -> PrepareSampleResult:
    """执行单样品的预处理与分析。

    参数：
        sample：待处理物料。
        cycles：处理循环次数。
        mode：分析模式。
    返回：
        样品与报告结果。
    """

    # [加入预混液]: PCR 中预混液的分配'''
    return source.replace(body_marker, rendered_docstring, 1)


def _workflow_function(python_source: str) -> ast.FunctionDef:
    """从规范源码中取得唯一工作流函数的静态 AST 节点。

    参数：``python_source`` 是编译器返回的规范 Python 源码。
    返回：唯一的 ``ast.FunctionDef``，用于观察 docstring 与真实源码坐标。
    异常：源码不是有效 Python 或函数数量不是一时抛出解析/断言错误。
    """

    # ``module`` 是纯静态解析结果，测试与生产编译器都不执行作者代码。
    module = ast.parse(python_source)
    # ``functions`` 是规范源码中的工作流函数候选集合。
    functions = [
        statement for statement in module.body if isinstance(statement, ast.FunctionDef)
    ]
    assert len(functions) == 1
    return functions[0]


def test_multiline_chinese_docstring_reaches_normalized_source_fixed_point() -> None:
    """中文多行函数 docstring 应经编译、渲染与重编译保持语义和文本固定点。

    参数：无。
    返回：无；断言规范源码的函数 docstring 内容及第二次规范源码完全相同。
    异常：编译、静态解析或固定点断言失败时由 pytest 报告。
    """

    # ``source`` 是采用常见作者版式的规范输入：空行不含尾随空格，闭合三引号
    # 独占一行，并在第一个节点展示注释之前保留一个空行。
    source = _source_with_docstring()
    # ``compiled`` 是第一次从包作者源码产生的工作流创作候选。
    compiled = _compile(_engine(), source)
    assert compiled.valid and compiled.graph is not None, compiled.diagnostics
    assert compiled.normalized_python_source is not None
    # ``workflow_function`` 是第一次规范源码中的工作流函数声明。
    workflow_function = _workflow_function(compiled.normalized_python_source)
    assert ast.get_docstring(workflow_function, clean=True) == _WORKFLOW_DOCSTRING
    # ``source_function`` 与 ``normalized_function`` 从工作流装饰器起比较，排除
    # 导入整理之外的噪声，直接证明真实作者源码已经达到规范化固定点。
    source_function = source[source.index("@workflow(") :]
    normalized_function = compiled.normalized_python_source[
        compiled.normalized_python_source.index("@workflow(") :
    ]
    assert normalized_function == source_function
    assert not any(
        line and not line.strip()
        for line in compiled.normalized_python_source.splitlines()
    )

    # ``repeated`` 证明规范源码自身再次编译后不丢失或重写 docstring。
    repeated = _compile(
        _engine(),
        compiled.normalized_python_source,
        graph=compiled.graph,
    )
    assert repeated.valid, repeated.diagnostics
    assert repeated.normalized_python_source == compiled.normalized_python_source


def test_docstring_preserves_node_comment_anchor_and_source_map_lines() -> None:
    """docstring 增加的源码行不得破坏节点标题、UUID 锚点或 SourceMap 坐标。

    参数：无。
    返回：无；逐节点断言映射起始行、相邻 UUID 行和动作结束行的真实文本。
    异常：编译失败、节点身份缺失或坐标漂移时由 pytest 报告。
    """

    # ``compiled`` 是同时包含函数 docstring 和节点展示覆盖的候选结果。
    compiled = _compile(_engine(), _source_with_docstring())
    assert compiled.valid and compiled.graph is not None, compiled.diagnostics
    assert compiled.normalized_python_source is not None
    # ``source_lines`` 按一基 SourceMap 行号提供规范源码的独立文本判据。
    source_lines = compiled.normalized_python_source.splitlines()
    assert not any(line and not line.strip() for line in source_lines)
    # ``workflow_function`` 与 ``docstring_statement`` 证明映射从真实多行文档之后开始。
    workflow_function = _workflow_function(compiled.normalized_python_source)
    docstring_statement = workflow_function.body[0]
    assert isinstance(docstring_statement, ast.Expr)
    assert ast.get_docstring(workflow_function, clean=True) == _WORKFLOW_DOCSTRING
    assert isinstance(docstring_statement.end_lineno, int)
    # ``source_map_by_node`` 把每个稳定工作流节点 UUID 映射到编译器坐标事实。
    source_map_by_node = {
        item["workflow_node_uuid"]: item for item in compiled.source_map
    }
    # ``expected_lines`` 固定每个节点的标题、UUID 锚点与动作调用文本。
    expected_lines = {
        PREPARE_NODE_UUID: (
            "# [加入预混液]: PCR 中预混液的分配",
            f"# unilab:node_uuid={PREPARE_NODE_UUID}",
            "prepared = reactor.prepare(cycles=cycles, sample=sample)",
        ),
        ANALYZE_NODE_UUID: (
            "# [分析产物]: PCR产物的质量分析",
            f"# unilab:node_uuid={ANALYZE_NODE_UUID}",
            "analyzed = reactor.analyze(label=mode, prepared=prepared.prepared)",
        ),
    }
    for node_uuid, (title, anchor, action) in expected_lines.items():
        # ``source_range`` 是当前节点在规范源码中的一基 UTF-16 映射范围。
        source_range = source_map_by_node[node_uuid]
        start_index = source_range["start_line"] - 1
        end_index = source_range["end_line"] - 1
        assert source_lines[start_index].strip() == title
        assert source_lines[start_index + 1].strip() == anchor
        assert source_lines[end_index].strip() == action
    assert source_map_by_node[PREPARE_NODE_UUID]["start_line"] == (
        docstring_statement.end_lineno + 2
    )


def test_source_without_docstring_keeps_existing_normalized_output() -> None:
    """没有函数 docstring 的工作流必须保持修改前的规范源码文本。

    参数：无。
    返回：无；断言从装饰器到函数结尾的金丝雀文本完全不漂移。
    异常：编译失败或既有规范化格式变化时由 pytest 报告。
    """

    # ``compiled`` 是不含函数 docstring 的既有节点展示注释场景。
    compiled = _compile(_engine(), _annotated_source())
    assert compiled.valid, compiled.diagnostics
    assert compiled.normalized_python_source is not None
    # ``function_source`` 是本功能不得改写的既有规范函数与装饰器文本。
    function_source = compiled.normalized_python_source[
        compiled.normalized_python_source.index("@workflow(") :
    ]
    assert function_source == _NO_DOCSTRING_FUNCTION


def test_non_string_leading_expression_is_not_treated_as_docstring() -> None:
    """普通首表达式不得冒充函数 docstring 或绕过可信作者语法检查。

    参数：无。
    返回：无；断言数值表达式继续失败关闭且不产生规范源码。
    异常：基础夹具入口漂移或关闭式诊断缺失时由 pytest 报告。
    """

    # ``source`` 在首节点前加入无副作用但不属于可信创作子集的普通表达式。
    source = _annotated_source().replace(
        "    # [加入预混液]: PCR 中预混液的分配",
        "    42\n    # [加入预混液]: PCR 中预混液的分配",
        1,
    )
    # ``compiled`` 必须把普通表达式作为非法作者语法处理，而非静默丢弃。
    compiled = _compile(_engine(), source)
    assert not compiled.valid
    assert compiled.normalized_python_source is None
    assert any(
        diagnostic["code"] == "unsupported_authoring_syntax"
        for diagnostic in compiled.diagnostics
    )
