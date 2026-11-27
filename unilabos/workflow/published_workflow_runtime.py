"""已发布工作流（PublishedWorkflow）的生产目录代际构造。"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
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
    workspace_workflow_uuids: Collection[str] | None = None,
    published_workflow_revisions: Mapping[str, int] | None = None,
    published_workflow_source_hashes: Mapping[str, str] | None = None,
) -> PublishedWorkflowGeneration:
    """从活动授权与同修订应用快照构造完整发布扩展代际。

    参数：``registrations`` 是本次进程活动可编辑包来源；``snapshot_provider``
    只读工作流图和应用源码；``base_node_templates`` 是同次设备目录编译结果，用于
    定位唯一宿主节点（Host Node）所有者；``workspace_workflow_uuids`` 是组合根
    明确允许在本地工作区参与组合的来源身份，既可用于无发布目录的旧包兼容，也
    可用于工作区能力允许的尚未发布源码；它们只放宽本地组合解析，不代表已写入发布
    合同。``published_workflow_revisions`` 和 ``published_workflow_source_hashes``
    是组合根按当前发布目录和源码读取事实
    计算出的严格资格（提供空映射也表示“没有可引用发布合同”）。返回：一个来源目录和可追加
    到同事务替换的模板/连接点全集。异常：来源、宿主、应用快照或发布合同不一致时
    抛出 ``PublishedWorkflowGenerationError``，不返回部分代际。
    """

    if not isinstance(registrations, Sequence) or isinstance(
        registrations,
        (str, bytes),
    ):
        raise PublishedWorkflowGenerationError("活动工作流来源必须是数组")
    # 默认仍只把发布目录中的来源投影为组合模板；本地工作区可通过
    # ``workspace_workflow_uuids`` 显式加入未发布来源。内存测试提供者没有该
    # 可选端口时，保留旧夹具语义（由快照本身决定资格）。
    published_projection_reader = getattr(
        snapshot_provider,
        "list_published_template_projections",
        None,
    )
    published_workflow_uuids: set[str] | None = None
    derived_published_revisions: dict[str, int] = {}
    if published_workflow_revisions is not None:
        published_workflow_revisions = _normalize_publication_revisions(
            published_workflow_revisions
        )
        # 显式映射是组合根已经核验过的发布目录；即使为空，也不能再从
        # ``snapshot_provider`` 的历史合同表推断资格。
        published_workflow_uuids = set(published_workflow_revisions)
    elif callable(published_projection_reader):
        try:
            for item in published_projection_reader():
                if not isinstance(item, Mapping) or item.get("workflow_uuid") is None:
                    continue
                workflow_uuid = str(item["workflow_uuid"])
                revision = item.get("workflow_revision")
                if (
                    isinstance(revision, int)
                    and not isinstance(revision, bool)
                    and revision >= 1
                ):
                    derived_published_revisions[workflow_uuid] = max(
                        revision,
                        derived_published_revisions.get(workflow_uuid, 0),
                    )
                else:
                    # 兼容仅提供 UUID 的旧测试/适配器；这类来源仍按旧语义
                    # 交给快照的同修订资格判断。
                    if workflow_uuid:
                        derived_published_revisions.setdefault(workflow_uuid, 0)
        except (KeyError, TypeError, ValueError) as error:
            raise PublishedWorkflowGenerationError("发布目录读取失败") from error
        # 只要存在读取端口，其结果（包括显式空投影）就是权威。空集合必须隐藏
        # 全部未发布注册；只有完全没有该端口的旧适配器才保留下方快照回退语义。
        published_workflow_uuids = set(derived_published_revisions)
        derived_published_revisions = {
            workflow_uuid: revision
            for workflow_uuid, revision in derived_published_revisions.items()
            if revision >= 1
        }
    if published_workflow_source_hashes is not None:
        published_workflow_source_hashes = _normalize_publication_hashes(
            published_workflow_source_hashes
        )
    # 工作区启动计划本身是受控的来源授权，不等同于运行期 API 导入的草稿。启动
    # 固定点必须先让这些来源互相解析，待应用快照形成后再投影模板；动态导入
    # 仍严格要求已发布合同。集合只在组合根内部传入，通用调用保持旧语义。
    workspace_uuids = (
        {str(identity) for identity in workspace_workflow_uuids}
        if workspace_workflow_uuids is not None
        else set()
    )
    # 只有明确由组合根放宽、且不在当前发布合同集合中的来源才属于
    # ``workspace-only``。发布来源即使同时出现在工作区授权集合，也必须继续
    # 走严格的快照/合同校验，不能绕过 fail-closed 边界。
    published_identity_set = (
        set(published_workflow_uuids)
        if published_workflow_uuids is not None
        else set()
    )
    workspace_only_uuids = workspace_uuids - published_identity_set
    if (
        published_workflow_revisions is not None
        and published_workflow_source_hashes is not None
    ):
        expected_hash_keys = {
            workflow_uuid
            for workflow_uuid in published_workflow_revisions
            if workflow_uuid not in workspace_only_uuids
        }
        if set(published_workflow_source_hashes) != expected_hash_keys:
            raise PublishedWorkflowGenerationError("发布目录读取失败")
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
        if (
            published_workflow_uuids is not None
            and workflow_uuid not in published_workflow_uuids
            and workflow_uuid not in workspace_uuids
        ):
            continue
        workspace_only = workflow_uuid in workspace_only_uuids
        # ``catalog_identity`` 来自同次包目录（PackageCatalog）静态编译，不触发
        # 第二次扫描、Python import 或作者源码执行。开发工作区中单个未发布
        # 来源的身份损坏只会让该来源不可组合，不能阻断其他来源的目录构造；
        # 已发布来源仍把同一错误作为目录基础设施失败抛出。
        try:
            catalog_identity = _catalog_identity(registration)
        except PublishedWorkflowGenerationError:
            if workspace_only:
                continue
            raise
        try:
            snapshot: Mapping[str, Any] | None = (
                snapshot_provider.get_published_workflow_snapshot(workflow_uuid)
            )
        except LookupError:
            snapshot = None
        except (AttributeError, KeyError, TypeError, ValueError):
            if workspace_only:
                continue
            raise PublishedWorkflowGenerationError("发布来源快照读取失败") from None
        if catalog_identity is None:
            # 非工作区遗留入口没有冻结包目录身份时，仅保留既有已应用来源行为。
            try:
                eligible = snapshot is not None and _eligible(snapshot)
            except (AttributeError, KeyError, TypeError, ValueError):
                if workspace_only:
                    continue
                raise PublishedWorkflowGenerationError("发布来源快照无效") from None
            if not eligible:
                continue
            try:
                workflow = snapshot["workflow"]
                applied_source = snapshot["applied_source"]
                symbol = _authoring_symbol(workflow)
                module = _source_module(package_id, relative_path)
                definition_content_hash = str(applied_source["source_hash"])
            except PublishedWorkflowGenerationError:
                if workspace_only:
                    continue
                raise
            except (KeyError, TypeError, ValueError):
                if workspace_only:
                    continue
                raise PublishedWorkflowGenerationError("发布来源快照无效") from None
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
        if snapshot is not None:
            try:
                eligible = _eligible(snapshot)
            except (AttributeError, KeyError, TypeError, ValueError):
                if workspace_only:
                    continue
                raise PublishedWorkflowGenerationError("发布来源快照无效") from None
            if eligible:
                try:
                    matches_pin = _matches_publication_pin(
                        workflow_uuid=workflow_uuid,
                        snapshot=snapshot,
                        published_workflow_revisions=(
                            published_workflow_revisions
                            if published_workflow_revisions is not None
                            else derived_published_revisions
                        ),
                        published_workflow_source_hashes=published_workflow_source_hashes,
                        workspace_workflow_uuids=workspace_only_uuids,
                    )
                except (AttributeError, KeyError, TypeError, ValueError):
                    if workspace_only:
                        continue
                    raise PublishedWorkflowGenerationError("发布来源快照无效") from None
                if matches_pin:
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
        except (
            PublishedWorkflowContractError,
            KeyError,
            TypeError,
            ValueError,
        ) as error:
            if source.workflow_uuid in workspace_only_uuids:
                # 未发布来源属于开发工作区的可选组合输入。快照/边界合同损坏时
                # 放弃该来源的模板投影，保留其余目录；父图会收到局部组合诊断，
                # 而不会把整个 Workspace 启动升级为目录不可用。
                continue
            if isinstance(error, PublishedWorkflowContractError):
                raise PublishedWorkflowGenerationError(error.code) from error
            raise PublishedWorkflowGenerationError("发布来源快照无效") from error
        if projected is None:
            if source.workflow_uuid in workspace_only_uuids:
                continue
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


def _normalize_publication_revisions(
    revisions: Mapping[str, int],
) -> dict[str, int]:
    """校验组合根交付的工作流发布修订映射。"""

    if not isinstance(revisions, Mapping):
        raise PublishedWorkflowGenerationError("发布目录读取失败")
    normalized: dict[str, int] = {}
    for workflow_uuid, revision in revisions.items():
        if (
            not isinstance(workflow_uuid, str)
            or not workflow_uuid
            or isinstance(revision, bool)
            or not isinstance(revision, int)
            or revision < 1
        ):
            raise PublishedWorkflowGenerationError("发布目录读取失败")
        normalized[workflow_uuid] = revision
    return normalized


def _normalize_publication_hashes(
    hashes: Mapping[str, str],
) -> dict[str, str]:
    """校验组合根交付的源码发布哈希映射。"""

    if not isinstance(hashes, Mapping):
        raise PublishedWorkflowGenerationError("发布目录读取失败")
    normalized: dict[str, str] = {}
    for workflow_uuid, source_hash in hashes.items():
        if not isinstance(workflow_uuid, str) or not workflow_uuid:
            raise PublishedWorkflowGenerationError("发布目录读取失败")
        canonical = _canonical_hash(source_hash)
        if canonical is None:
            raise PublishedWorkflowGenerationError("发布目录读取失败")
        normalized[workflow_uuid] = canonical
    return normalized


def _matches_publication_pin(
    *,
    workflow_uuid: str,
    snapshot: Mapping[str, Any],
    published_workflow_revisions: Mapping[str, int],
    published_workflow_source_hashes: Mapping[str, str] | None,
    workspace_workflow_uuids: set[str],
) -> bool:
    """确认快照仍与当前发布合同的修订和源码字节绑定。

    ``source_draft_hash``（发布目录记录的源码字节摘要）与
    ``applied_source.source_hash``（规范化 Python 文本摘要）是两个独立的
    证据，不能互相替代。生产存储会在快照顶层提供前者；旧的窄适配器也可以把
    它放在 ``applied_source.draft_hash`` 中。缺少原始摘要时严格 pin 关闭失败，
    避免把一个仅有规范化摘要的旧快照误当成当前发布源码。
    """

    expected_revision = published_workflow_revisions.get(workflow_uuid)
    # ``0`` 是旧适配器仅提供 UUID 时的哨兵，表示不额外约束修订；显式组合根
    # 映射永远不会产生该值。
    if (
        expected_revision is not None
        and expected_revision > 0
        and snapshot.get("workflow", {}).get("revision") != expected_revision
    ):
        return False
    expected_hash = (
        published_workflow_source_hashes.get(workflow_uuid)
        if published_workflow_source_hashes is not None
        else None
    )
    if (
        published_workflow_source_hashes is not None
        and workflow_uuid not in workspace_workflow_uuids
    ):
        # 一旦组合根交付了严格源码 pin，缺少某个已发布 UUID 的 hash 也必须
        # fail-closed；不能因为调用方漏传一个键而退化成只按 revision 放行。
        if expected_hash is None:
            return False
        draft_hash = snapshot.get("source_draft_hash")
        if not isinstance(draft_hash, str):
            # 允许已迁移的内部快照把原始摘要嵌在 applied_source；绝不回退到
            # ``source_hash``，后者可能只是规范化源码的摘要。
            applied = snapshot.get("applied_source")
            if isinstance(applied, Mapping):
                draft_hash = applied.get("draft_hash")
        if not isinstance(draft_hash, str):
            return False
        canonical_draft_hash = _canonical_hash(draft_hash)
        canonical_expected_hash = _canonical_hash(expected_hash)
        if (
            canonical_draft_hash is None
            or canonical_expected_hash is None
            or canonical_draft_hash != canonical_expected_hash
        ):
            return False
    # 遗留工作区来源可明确免除发布 pin；显式分支表明这只是兼容行为，不能被
    # 严格发布映射中的普通来源借用来绕过合同校验。
    if (
        expected_revision is None
        and published_workflow_revisions
        and workflow_uuid not in workspace_workflow_uuids
    ):
        return False
    return True


def _canonical_hash(value: str) -> str | None:
    """把源码摘要规范为带 ``sha256:`` 前缀的形式。

    参数：``value`` 是发布目录或快照提供的源码摘要。返回：格式正确时的规范
    摘要，否则返回 ``None``。异常：无；调用方将格式错误按 pin 不匹配处理。
    """

    if not isinstance(value, str):
        return None
    digest = value.removeprefix("sha256:")
    if len(digest) != 64 or any(
        character not in "0123456789abcdef" for character in digest
    ):
        return None
    return "sha256:" + digest


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
