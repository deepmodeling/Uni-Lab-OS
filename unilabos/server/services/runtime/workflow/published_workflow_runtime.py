"""已发布工作流目录代际构造。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

from unilabos.server.services.runtime.workflow.catalog import PublishedSourceCatalog
from unilabos.server.services.runtime.workflow.composite import (
    PublishedWorkflowContractError,
    project_published_workflow_contract,
)
from unilabos.server.services.runtime.workflow.composite_expansion import (
    PublishedWorkflowSnapshotProvider,
)


class PublishedWorkflowGenerationError(RuntimeError):
    """活动来源不能安全构成一个封闭目录代际。"""


@dataclass(frozen=True, slots=True)
class PublishedWorkflowGeneration:
    source_catalog: PublishedSourceCatalog
    node_templates: tuple[dict[str, Any], ...]
    handle_templates: tuple[dict[str, Any], ...]


def build_published_workflow_generation(
    *,
    registrations: Sequence[Mapping[str, Any]],
    snapshot_provider: PublishedWorkflowSnapshotProvider,
    base_node_templates: Sequence[Mapping[str, Any]],
) -> PublishedWorkflowGeneration:
    """从活动来源和同修订应用快照构造完整工作流模板目录。"""

    if not isinstance(registrations, Sequence) or isinstance(
        registrations,
        (str, bytes),
    ):
        raise PublishedWorkflowGenerationError("活动工作流来源必须是数组")
    records: list[dict[str, str]] = []
    snapshots: dict[str, Mapping[str, Any]] = {}
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
        try:
            snapshot = snapshot_provider.get_published_workflow_snapshot(workflow_uuid)
        except LookupError:
            snapshot = None
        module = registration.get("module")
        symbol = registration.get("symbol")
        content_hash = registration.get("definition_content_hash")
        if snapshot is not None and _eligible(snapshot):
            snapshots[workflow_uuid] = snapshot
            applied = snapshot["applied_source"]
            workflow = snapshot["workflow"]
            if not isinstance(module, str) or not module:
                module = _source_module(package_id, relative_path)
            if not isinstance(symbol, str) or not symbol:
                symbol = _authoring_symbol(workflow, relative_path=relative_path)
            if not isinstance(content_hash, str) or not content_hash:
                content_hash = str(applied["source_hash"])
        # 封闭代际只公布能够从同修订已应用快照投影模板的来源。否则 resolver
        # 会暴露一个目录中存在、却永远没有对应节点模板的半发布身份。
        if workflow_uuid not in snapshots:
            continue
        if not all(isinstance(item, str) and item for item in (module, symbol, content_hash)):
            continue
        records.append(
            {
                "workflow_uuid": workflow_uuid,
                "definition_fqid": f"{module}.{symbol}",
                "module": module,
                "symbol": symbol,
                "source_uri": source_uri,
                "definition_content_hash": content_hash,
            }
        )
    try:
        source_catalog = PublishedSourceCatalog.from_records(records)
    except (TypeError, ValueError) as error:
        raise PublishedWorkflowGenerationError(str(error)) from error
    if not snapshots:
        return PublishedWorkflowGeneration(source_catalog, (), ())
    host = _host_summary(base_node_templates)
    nodes: list[dict[str, Any]] = []
    handles: list[dict[str, Any]] = []
    for source in source_catalog.sources:
        snapshot = snapshots.get(source.workflow_uuid)
        if snapshot is None:
            continue
        try:
            contract = project_published_workflow_contract(
                source=source,
                applied_snapshot=snapshot,
                host_node_resource_template=host,
            )
        except PublishedWorkflowContractError as error:
            raise PublishedWorkflowGenerationError(error.code) from error
        if contract is None:
            raise PublishedWorkflowGenerationError("发布资格在目录构造期间发生漂移")
        nodes.append(contract.template)
        handles.extend(contract.handles)
    return PublishedWorkflowGeneration(
        source_catalog=source_catalog,
        node_templates=tuple(nodes),
        handle_templates=tuple(handles),
    )


def _eligible(snapshot: Mapping[str, Any]) -> bool:
    workflow = snapshot.get("workflow") if isinstance(snapshot, Mapping) else None
    applied = snapshot.get("applied_source") if isinstance(snapshot, Mapping) else None
    if not isinstance(workflow, Mapping) or not isinstance(applied, Mapping):
        return False
    revision = workflow.get("revision")
    return (
        isinstance(revision, int)
        and not isinstance(revision, bool)
        and revision >= 1
        and applied.get("workflow_revision") == revision
        and isinstance(applied.get("source_hash"), str)
    )


def _source_module(package_id: str, relative_path: str) -> str:
    path = PurePosixPath(relative_path)
    if path.is_absolute() or relative_path != path.as_posix() or path.suffix != ".py":
        raise PublishedWorkflowGenerationError("工作流来源不能转换为绝对模块")
    parts = path.with_suffix("").parts
    if parts[:1] == (package_id,):
        parts = parts[1:]
    result = (package_id, *parts)
    if any(not part.isidentifier() for part in result):
        raise PublishedWorkflowGenerationError("工作流来源不能转换为绝对模块")
    return ".".join(result)


def _authoring_symbol(
    workflow: Mapping[str, Any],
    *,
    relative_path: str,
) -> str:
    meta_data = workflow.get("meta_data")
    unilab = meta_data.get("unilab") if isinstance(meta_data, Mapping) else None
    symbol = (
        unilab.get("authoring_function_name")
        if isinstance(unilab, Mapping)
        else None
    )
    if not isinstance(symbol, str) or not symbol.isidentifier():
        symbol = PurePosixPath(relative_path).stem
    if not symbol.isidentifier():
        raise PublishedWorkflowGenerationError("已应用工作流缺少作者函数符号")
    return symbol


def _host_summary(
    node_templates: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    matches: list[dict[str, Any]] = []
    for template in node_templates:
        meta_data = template.get("meta_data")
        unilab = meta_data.get("unilab") if isinstance(meta_data, Mapping) else None
        summary = (
            unilab.get("resource_template")
            if isinstance(unilab, Mapping)
            else None
        )
        if not isinstance(summary, Mapping) and isinstance(meta_data, Mapping):
            summary = meta_data.get("resource_template")
        if isinstance(summary, Mapping) and summary.get("name") == "host_node":
            candidate = {
                "uuid": summary.get("uuid"),
                "name": summary.get("name"),
                "display_name": summary.get("display_name"),
            }
            if candidate not in matches:
                matches.append(candidate)
    if len(matches) != 1:
        raise PublishedWorkflowGenerationError("目录缺少唯一 Host Node 所有者")
    return matches[0]


__all__ = [
    "PublishedWorkflowGeneration",
    "PublishedWorkflowGenerationError",
    "build_published_workflow_generation",
]
