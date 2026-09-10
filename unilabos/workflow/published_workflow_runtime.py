"""已发布工作流（PublishedWorkflow）的生产目录代际构造。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

from unilabos.workflow.catalog import PublishedSourceCatalog
from unilabos.workflow.composite import (
    PublishedWorkflowContractError,
    PublishedWorkflowSnapshotProvider,
    project_published_workflow_contract,
)


class PublishedWorkflowGenerationError(RuntimeError):
    """活动来源不能安全构成一个封闭的工作流模板目录代际。"""


@dataclass(frozen=True, slots=True)
class PublishedWorkflowGeneration:
    """同一来源目录摘要下的工作流模板与连接点（Handle）全集。"""

    source_catalog: PublishedSourceCatalog
    node_templates: tuple[dict[str, Any], ...]
    handle_templates: tuple[dict[str, Any], ...]


def build_published_workflow_generation(
    *,
    registrations: Sequence[Mapping[str, Any]],
    snapshot_provider: PublishedWorkflowSnapshotProvider,
    base_node_templates: Sequence[Mapping[str, Any]],
) -> PublishedWorkflowGeneration:
    """从活动授权与同修订应用快照构造完整发布扩展代际。

    参数：``registrations`` 是本次进程活动可编辑包来源；``snapshot_provider``
    只读工作流图和应用源码；``base_node_templates`` 是同次设备目录编译结果，用于
    定位唯一宿主节点（Host Node）所有者。返回：一个来源目录和可追加到同事务
    替换的模板/连接点全集。异常：来源、宿主、应用快照或发布合同不一致时抛出
    ``PublishedWorkflowGenerationError``，不返回部分代际。
    """

    if not isinstance(registrations, Sequence) or isinstance(
        registrations,
        (str, bytes),
    ):
        raise PublishedWorkflowGenerationError("活动工作流来源必须是数组")
    # ``snapshots`` 只保存与活动包目录源码内容一致的同修订应用事实；来源解析
    # 目录仍保留已登记未应用项，以便组合编译返回准确诊断。
    snapshots: dict[str, Mapping[str, Any]] = {}
    records: list[dict[str, str]] = []
    for index, registration in enumerate(registrations):
        try:
            workflow_uuid = str(registration["workflow_uuid"])
            package_id = str(registration["package_id"])
            relative_path = str(registration["relative_path"])
            source_uri = str(registration["source_uri"])
        except (KeyError, TypeError):
            raise PublishedWorkflowGenerationError(
                f"活动工作流来源 {index} 字段不完整"
            ) from None
        # ``catalog_identity`` 来自同次包目录（PackageCatalog）静态编译，不触发
        # 第二次扫描、Python import 或作者源码执行。
        catalog_identity = _catalog_identity(registration)
        try:
            snapshot: Mapping[str, Any] | None = (
                snapshot_provider.get_published_workflow_snapshot(workflow_uuid)
            )
        except LookupError:
            snapshot = None
        if catalog_identity is None:
            # 非工作区遗留入口没有冻结包目录身份时，仅保留既有已应用来源行为。
            if snapshot is None or not _eligible(snapshot):
                continue
            workflow = snapshot["workflow"]
            applied_source = snapshot["applied_source"]
            symbol = _authoring_symbol(workflow)
            module = _source_module(package_id, relative_path)
            definition_content_hash = str(applied_source["source_hash"])
        else:
            module, symbol, definition_content_hash = catalog_identity
        records.append(
            {
                "workflow_uuid": workflow_uuid,
                "definition_fqid": f"{module}.{symbol}",
                "module": module,
                "symbol": symbol,
                "source_uri": source_uri,
                "definition_content_hash": definition_content_hash,
            }
        )
        if snapshot is not None and _eligible(snapshot):
            snapshots[workflow_uuid] = snapshot
    try:
        source_catalog = PublishedSourceCatalog.from_records(records)
    except (TypeError, ValueError) as error:
        raise PublishedWorkflowGenerationError(str(error)) from error
    if not snapshots:
        return PublishedWorkflowGeneration(
            source_catalog=source_catalog,
            node_templates=(),
            handle_templates=(),
        )
    host_summary = _host_summary(base_node_templates)
    nodes: list[dict[str, Any]] = []
    handles: list[dict[str, Any]] = []
    for source in source_catalog.sources:
        if source.workflow_uuid not in snapshots:
            continue
        try:
            projected = project_published_workflow_contract(
                source=source,
                applied_snapshot=snapshots[source.workflow_uuid],
                host_node_resource_template=host_summary,
            )
        except PublishedWorkflowContractError as error:
            raise PublishedWorkflowGenerationError(error.code) from error
        if projected is None:
            raise PublishedWorkflowGenerationError("发布资格在同一目录构造期间发生漂移")
        nodes.append(projected.template)
        handles.extend(projected.handles)
    return PublishedWorkflowGeneration(
        source_catalog=source_catalog,
        node_templates=tuple(nodes),
        handle_templates=tuple(handles),
    )


def _eligible(snapshot: Mapping[str, Any]) -> bool:
    """判断快照是否具有同修订应用源码且哈希可用于静态发布。

    参数：``snapshot`` 是只读存储回执。返回：工作流、正修订和同修订应用哈希
    完整时为 ``True``。异常：无；包目录源码哈希和规范化应用源码哈希分别作为
    来源证据与应用证据保存，不要求字节相同。
    """

    workflow = snapshot.get("workflow") if isinstance(snapshot, Mapping) else None
    applied = snapshot.get("applied_source") if isinstance(snapshot, Mapping) else None
    if not isinstance(workflow, Mapping) or not isinstance(applied, Mapping):
        return False
    revision = workflow.get("revision")
    source_hash = applied.get("source_hash")
    return (
        isinstance(revision, int)
        and not isinstance(revision, bool)
        and revision >= 1
        and applied.get("workflow_revision") == revision
        and isinstance(source_hash, str)
    )


def _catalog_identity(
    registration: Mapping[str, Any],
) -> tuple[str, str, str] | None:
    """读取包目录（PackageCatalog）已经静态编译的来源身份。

    参数：``registration`` 是同一发现计划中的源码登记项。
    返回：模块、符号和内容哈希均缺失时返回 ``None``；三者完整时返回元组。
    异常：字段部分缺失或不是字符串时抛出
    ``PublishedWorkflowGenerationError``，禁止退回路径猜测。
    """

    # ``values`` 是同一冻结包目录必须整体交付的三项源码证据。
    values = (
        registration.get("module"),
        registration.get("symbol"),
        registration.get("definition_content_hash"),
    )
    if values == (None, None, None):
        return None
    if any(not isinstance(value, str) or not value for value in values):
        raise PublishedWorkflowGenerationError("活动工作流来源目录身份不完整")
    module, symbol, definition_content_hash = values
    return module, symbol, definition_content_hash


def _authoring_symbol(workflow: Mapping[str, Any]) -> str:
    """读取已应用工作流唯一作者函数符号。

    参数：``workflow`` 是工作流读投影。返回：Python 标识符。异常：保留元数据
    缺失或符号非法时抛出 ``PublishedWorkflowGenerationError``。
    """

    meta_data = workflow.get("meta_data")
    unilab = meta_data.get("unilab") if isinstance(meta_data, Mapping) else None
    symbol = (
        unilab.get("authoring_function_name") if isinstance(unilab, Mapping) else None
    )
    if not isinstance(symbol, str) or not symbol.isidentifier():
        raise PublishedWorkflowGenerationError("已应用工作流缺少作者函数符号")
    return symbol


def _source_module(package_id: str, relative_path: str) -> str:
    """把已授权包身份和规范相对路径转换为绝对 Python 模块。

    参数：包 ID 与源码相对路径来自来源注册；路径必须是规范 POSIX 相对路径、
    以 ``.py`` 结尾，且既可相对包根，也可包含与包 ID 精确相等的首段。
    返回：包根恰好出现一次且不导入模块的静态点分身份。异常：路径不规范、
    后缀不是 ``.py`` 或任一分段不是 Python 标识符时抛出发布代际错误；只按
    原始完整首段去重，不对去除后缀后的文件名或前缀相似包名做模糊裁剪。
    """

    # ``path`` 是来源注册交付的 POSIX 源码身份，不访问本地文件系统。
    path = PurePosixPath(relative_path)
    if path.is_absolute() or relative_path != path.as_posix() or path.suffix != ".py":
        raise PublishedWorkflowGenerationError("工作流来源不能转换为绝对模块")
    # ``source_parts`` 保留文件后缀，确保同名 ``pkg.py`` 不会被误当成包根。
    source_parts = path.parts
    # ``has_package_root`` 只记录原始首段是否为完整包身份，不做字符串前缀匹配。
    has_package_root = source_parts[:1] == (package_id,)
    # ``relative_parts`` 仅在判断原始首段后移除精确 ``.py``，保留真实子包层级。
    relative_parts = path.with_suffix("").parts
    if has_package_root:
        relative_parts = relative_parts[1:]
    # ``parts`` 是最终绝对 Python 模块的有序身份分段。
    parts = (package_id, *relative_parts)
    if any(not part.isidentifier() for part in parts):
        raise PublishedWorkflowGenerationError("工作流来源不能转换为绝对模块")
    return ".".join(parts)


def _host_summary(
    node_templates: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """从本代框架模板取得唯一宿主资源模板摘要。

    参数：``node_templates`` 是尚未持久化的设备/框架模板候选。返回：含 UUID、
    名称和展示名的分离摘要。异常：缺失或多个宿主摘要时抛出发布代际错误。
    """

    matches: list[dict[str, Any]] = []
    for template in node_templates:
        meta_data = template.get("meta_data")
        unilab = meta_data.get("unilab") if isinstance(meta_data, Mapping) else None
        summary = (
            unilab.get("resource_template") if isinstance(unilab, Mapping) else None
        )
        if isinstance(summary, Mapping) and summary.get("name") == "host_node":
            candidate = {
                "uuid": summary.get("uuid"),
                "name": summary.get("name"),
                "display_name": summary.get("display_name"),
            }
            if candidate not in matches:
                matches.append(candidate)
    if len(matches) != 1:
        raise PublishedWorkflowGenerationError("目录缺少唯一宿主节点所有者")
    return matches[0]


__all__ = [
    "PublishedWorkflowGeneration",
    "PublishedWorkflowGenerationError",
    "build_published_workflow_generation",
]
