"""把上传的 Python 工作流源码转换为安全的首次编译输入。"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import PurePosixPath, PureWindowsPath
from urllib.parse import quote

from unilabos.workflow.authoring_identity import (
    declared_workflow_type,
    declared_workflow_uuid,
)
from unilabos.workflow.store import utc_now
from unilabos.workflow.workflow_type import WorkflowType

_SHA256_TOKEN = re.compile(r"sha256:[0-9a-f]{64}\Z")


class PythonWorkflowImportError(ValueError):
    """表示上传文件不能形成可信的 Python 工作流导入请求。"""


@dataclass(frozen=True, slots=True)
class PythonWorkflowImportSource:
    """已验证但尚未编译的 Python 工作流导入事实。"""

    file_name: str
    python_source: str
    workflow_uuid: str
    workflow_type: WorkflowType
    source_uri: str
    source_hash: str

    def initial_graph(self) -> dict[str, object]:
        """构造首次 AST 编译使用的空工作流图。

        参数：无。返回：修订为 1、身份来自源码声明的五集合工作流图；文件名和
        原始源码摘要只作为导入追溯信息，不参与后续执行。异常：无。
        """

        timestamp = utc_now()
        return {
            "workflow": {
                "uuid": self.workflow_uuid,
                "create_time": timestamp,
                "update_time": timestamp,
                "name": PurePosixPath(self.file_name).stem,
                "tags": [],
                "workflow_type": self.workflow_type,
                "description": None,
                "meta_data": {
                    "unilab": {
                        "python_import": {
                            "file_name": self.file_name,
                            "source_uri": self.source_uri,
                            "source_hash": self.source_hash,
                            "compiler_mode": "ast_only",
                        }
                    }
                },
                "revision": 1,
            },
            "nodes": [],
            "edges": [],
            "node_templates": [],
            "handle_templates": [],
        }


def validate_python_workflow_import(
    *,
    file_name: str,
    python_source: str,
    source_hash: str,
) -> PythonWorkflowImportSource:
    """校验上传文件边界并提取源码声明的工作流身份。

    参数：``file_name`` 只允许单个 ``.py`` 文件名；``python_source`` 是严格
    UTF-8 解码后的完整源码；``source_hash`` 是原始上传字节的 SHA-256 标识。
    返回：可供现有 AST 创作编译器消费的不可变输入。异常：文件名、源码或
    ``@workflow`` 的显式 UUID 不合法时抛 ``PythonWorkflowImportError``。

    安全不变量：本函数只做字符串与 AST 身份读取，绝不 import、compile、eval
    或 execute 上传源码。
    """

    if not isinstance(file_name, str):
        raise PythonWorkflowImportError("文件名必须是字符串")
    normalized_name = file_name.strip()
    posix_name = PurePosixPath(normalized_name)
    windows_name = PureWindowsPath(normalized_name)
    if (
        not normalized_name
        or len(normalized_name.encode("utf-8")) > 255
        or posix_name.name != normalized_name
        or windows_name.name != normalized_name
        or posix_name.suffix.lower() != ".py"
        or any(
            ord(character) < 0x20 or character == "\x7f"
            for character in normalized_name
        )
    ):
        raise PythonWorkflowImportError(
            "文件名必须是单个 .py 文件名，不能包含目录路径、控制字符或其他扩展名"
        )
    if not isinstance(python_source, str) or not python_source.strip():
        raise PythonWorkflowImportError("Python 工作流源码不能为空")
    try:
        python_source.encode("utf-8")
    except UnicodeEncodeError:
        raise PythonWorkflowImportError("Python 工作流源码必须是 UTF-8 文本") from None
    try:
        ast.parse(python_source)
    except (SyntaxError, ValueError) as error:
        lineno = getattr(error, "lineno", None)
        offset = getattr(error, "offset", None)
        location = f"第 {lineno} 行第 {offset} 列" if lineno else "源码"
        raise PythonWorkflowImportError(
            f"Python 工作流源码存在语法错误（{location}），请检查括号、缩进和装饰器写法"
        ) from None
    workflow_uuid = declared_workflow_uuid(python_source)
    if workflow_uuid is None:
        raise PythonWorkflowImportError(
            "未找到唯一且有效的工作流身份声明，请保留一个"
            " @workflow(workflow_uuid=\"工作流 UUID\") 装饰器"
        )
    workflow_type = declared_workflow_type(python_source)
    if workflow_type is None:
        raise PythonWorkflowImportError(
            "工作流类型声明无效，workflow_type 只能填写 normal 或 experiment_operation"
        )
    if not isinstance(source_hash, str) or _SHA256_TOKEN.fullmatch(source_hash) is None:
        raise PythonWorkflowImportError("源码摘要无效")
    return PythonWorkflowImportSource(
        file_name=normalized_name,
        python_source=python_source,
        workflow_uuid=workflow_uuid,
        workflow_type=workflow_type,
        source_uri=f"upload://workflows/{quote(normalized_name, safe='._-')}",
        source_hash=source_hash,
    )


__all__ = [
    "PythonWorkflowImportError",
    "PythonWorkflowImportSource",
    "validate_python_workflow_import",
]
