"""解析可编辑包（Editable Package）的封闭工作流源码声明。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

import yaml
from yaml.constructor import ConstructorError
from yaml.events import (
    AliasEvent,
    CollectionEndEvent,
    DocumentStartEvent,
    MappingStartEvent,
    ScalarEvent,
    SequenceStartEvent,
)
from yaml.nodes import MappingNode

from unilabos.workflow.models import validate_uuid

YAML_DEPTH_LIMIT = 32
WORKFLOW_ENTRY_LIMIT = 1024
YAML_SCALAR_BYTE_LIMIT = 1024 * 1024
_PACKAGE_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


class _ClosedSafeLoader(yaml.SafeLoader):
    """在 PyYAML 安全类型范围上额外拒绝重复映射键。"""

    def construct_mapping(
        self,
        node: MappingNode,
        deep: bool = False,
    ) -> dict[Any, Any]:
        """构造不存在重复键的映射。

        参数：``node`` 是 YAML 映射节点；``deep`` 控制 PyYAML 的递归构造方式。
        返回：键唯一的 Python 字典。
        异常：键不可哈希或重复时抛出 ``ConstructorError``。
        """

        if not isinstance(node, MappingNode):
            raise ConstructorError(
                None,
                None,
                "expected a mapping node",
                node.start_mark,
            )
        seen_keys: set[Any] = set()
        for key_node, _value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            try:
                duplicate = key in seen_keys
            except TypeError as error:
                raise ConstructorError(
                    None,
                    None,
                    "unhashable mapping key",
                    key_node.start_mark,
                ) from error
            if duplicate:
                raise ConstructorError(
                    None,
                    None,
                    "duplicate mapping key",
                    key_node.start_mark,
                )
            seen_keys.add(key)
        return super().construct_mapping(node, deep=deep)


class SourceManifestError(RuntimeError):
    """表示不可信 ``package.yaml`` 违反了封闭声明合同。"""

    def __init__(self, code: str):
        """保存稳定错误码，不把不可信 YAML 内容带入异常消息。

        参数：``code`` 是供源码发现模块映射的稳定声明错误码。
        返回：无；构造出的异常只暴露固定错误码。
        """

        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class WorkflowSourceEntry:
    """一项工作流（Workflow）到 Python 源码的稳定声明。"""

    workflow_uuid: str
    relative_path: str


@dataclass(frozen=True)
class EditablePackageManifest:
    """完成结构校验后的可编辑包（Editable Package）声明。"""

    package_id: str
    workflows: tuple[WorkflowSourceEntry, ...]


def parse_editable_package_manifest(raw: bytes) -> EditablePackageManifest:
    """把 UTF-8 YAML 解析为封闭的可编辑包声明。

    参数：``raw`` 是从已授权目录安全读取的 ``package.yaml`` 字节。
    返回：只含稳定包身份和工作流源码（Workflow Source）声明的不可变模型。
    异常：格式、字段或身份不符合合同时抛出 ``SourceManifestError``。
    """

    document = _load_closed_yaml(raw)
    if not isinstance(document, dict) or set(document) != {"package", "workflows"}:
        raise SourceManifestError("invalid_manifest")

    package = document["package"]
    workflow_rows = document["workflows"]
    if not isinstance(package, dict) or set(package) != {"name"}:
        raise SourceManifestError("invalid_package")
    package_id = package["name"]
    if not isinstance(package_id, str) or _PACKAGE_NAME.fullmatch(package_id) is None:
        raise SourceManifestError("invalid_package")
    # ``workflow_rows`` 允许显式空列表，使新建可编辑包先形成合法身份，再逐步加入
    # 工作流源码（Workflow Source）；null 或缺失字段仍不是空声明。
    if (
        not isinstance(workflow_rows, list)
        or len(workflow_rows) > WORKFLOW_ENTRY_LIMIT
    ):
        raise SourceManifestError("invalid_manifest")

    # 工作流身份与相对路径共同决定后续来源注册，必须保持声明顺序稳定。
    entries = tuple(
        _parse_workflow_source(row, package_id=package_id) for row in workflow_rows
    )
    return EditablePackageManifest(package_id=package_id, workflows=entries)


def _load_closed_yaml(raw: bytes) -> Any:
    """在结构预算内解析单一、无别名和无显式标签的 YAML 文档。

    参数：``raw`` 是已受文件大小约束的 UTF-8 manifest 字节。
    返回：PyYAML 安全类型组成的文档值。
    异常：编码、语法、重复键、深度或节点预算不合法时抛出
    ``SourceManifestError``。
    """

    try:
        text = raw.decode("utf-8")
        document_count = 0
        depth = 0
        node_count = 0
        for event in yaml.parse(text):
            if isinstance(event, AliasEvent):
                raise SourceManifestError("invalid_manifest")
            if isinstance(event, (MappingStartEvent, SequenceStartEvent)):
                depth += 1
                node_count += 1
                if depth > YAML_DEPTH_LIMIT:
                    raise SourceManifestError("invalid_manifest")
            elif isinstance(event, CollectionEndEvent):
                depth -= 1
            elif isinstance(event, ScalarEvent):
                node_count += 1
                if len(event.value.encode("utf-8")) > YAML_SCALAR_BYTE_LIMIT:
                    raise SourceManifestError("invalid_manifest")
            if isinstance(
                event,
                (MappingStartEvent, SequenceStartEvent, ScalarEvent),
            ) and (event.anchor is not None or event.tag is not None):
                raise SourceManifestError("invalid_manifest")
            if isinstance(event, DocumentStartEvent):
                document_count += 1
            if node_count > WORKFLOW_ENTRY_LIMIT * 8 + 32:
                raise SourceManifestError("invalid_manifest")
        if document_count != 1 or depth != 0:
            raise SourceManifestError("invalid_manifest")
        return yaml.load(text, Loader=_ClosedSafeLoader)
    except SourceManifestError:
        raise
    except (
        MemoryError,
        OverflowError,
        RecursionError,
        UnicodeError,
        ValueError,
        yaml.YAMLError,
    ):
        raise SourceManifestError("invalid_manifest") from None


def _parse_workflow_source(raw: Any, *, package_id: str) -> WorkflowSourceEntry:
    """校验一项工作流源码（Workflow Source）声明。

    参数：``raw`` 是单项 YAML 值；``package_id`` 是已验证的稳定包身份。
    返回：规范 UUID 和 ``workflows/*.py`` 相对路径。
    异常：字段、UUID 或路径不符合合同时抛出 ``SourceManifestError``。
    """

    if not isinstance(raw, dict) or set(raw) != {"workflow_uuid", "source"}:
        raise SourceManifestError("invalid_workflow_source")
    raw_uuid = raw["workflow_uuid"]
    raw_source = raw["source"]
    if not isinstance(raw_uuid, str) or not isinstance(raw_source, str):
        raise SourceManifestError("invalid_workflow_source")
    try:
        workflow_uuid = validate_uuid(raw_uuid)
    except (TypeError, ValueError):
        raise SourceManifestError("invalid_workflow_source") from None
    if workflow_uuid != raw_uuid or "\\" in raw_source or "\x00" in raw_source:
        raise SourceManifestError("invalid_workflow_source")

    source_path = PurePosixPath(raw_source)
    if (
        source_path.is_absolute()
        or len(source_path.parts) != 3
        or any(part in {"", ".", ".."} for part in source_path.parts)
        or source_path.parts[0] != package_id
        or source_path.parts[1] != "workflows"
        or source_path.suffix != ".py"
        or not source_path.stem
    ):
        raise SourceManifestError("invalid_workflow_source")
    relative_path = PurePosixPath(*source_path.parts[1:]).as_posix()
    return WorkflowSourceEntry(
        workflow_uuid=workflow_uuid,
        relative_path=relative_path,
    )


__all__ = [
    "EditablePackageManifest",
    "SourceManifestError",
    "WorkflowSourceEntry",
    "parse_editable_package_manifest",
]
