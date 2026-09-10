"""从显式授权目录发现工作流源码（Workflow Source）声明。"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from unilabos.workflow.source_manifest import (
    SourceManifestError,
    parse_editable_package_manifest,
)
from unilabos.workflow.source_workspace import (
    SourceWorkspaceError,
    read_package_root,
    validate_declared_sources,
)

_ERROR_MESSAGES = {
    "invalid_package_root": "可编辑包根目录无效",
    "invalid_manifest": "package.yaml 声明格式不正确",
    "invalid_package": "可编辑包声明无效",
    "invalid_workflow_source": "工作流源码声明无效",
    "duplicate_workflow_source": "工作流源码声明存在重复身份",
}


class SourceDeclarationError(RuntimeError):
    """工作流源码（Workflow Source）发现的稳定、非泄漏错误。"""

    def __init__(self, code: str):
        """用固定消息公开声明错误。

        参数：``code`` 是公开给调用者的稳定发现错误码。
        返回：无；异常消息不包含包路径或不可信 YAML 内容。
        """

        self.code = code
        super().__init__(_ERROR_MESSAGES.get(code, _ERROR_MESSAGES["invalid_manifest"]))


@dataclass(frozen=True)
class EditableSourceRegistration:
    """一项可供后续原子注册的工作流源码（Workflow Source）身份。"""

    workflow_uuid: str
    package_id: str
    package_root: Path
    relative_path: str
    source_uri: str
    module: str | None = None
    symbol: str | None = None
    definition_content_hash: str | None = None


@dataclass(frozen=True)
class EditableSourceDiscoveryPlan:
    """一次显式发现得到的不可变来源注册计划。"""

    registrations: tuple[EditableSourceRegistration, ...]
    root_identities: tuple[tuple[Path, tuple[int, int]], ...]


def discover_editable_sources(
    authorized_roots: Iterable[str | Path],
) -> EditableSourceDiscoveryPlan:
    """只从显式授权目录构建工作流源码发现计划。

    参数：``authorized_roots`` 是启动配置明确提供的包选择目录集合。
    返回：保持输入目录和 manifest 声明顺序的不可变发现计划。
    异常：任一目录或声明无效时抛出 ``SourceDeclarationError``，不返回部分计划。
    """

    registrations: list[EditableSourceRegistration] = []
    root_identities: list[tuple[Path, tuple[int, int]]] = []
    for authorized_root in tuple(authorized_roots):
        try:
            snapshot = read_package_root(authorized_root)
            manifest = parse_editable_package_manifest(snapshot.manifest_bytes)
            source_snapshot = validate_declared_sources(
                snapshot,
                package_id=manifest.package_id,
                relative_paths=(entry.relative_path for entry in manifest.workflows),
            )
        except (SourceWorkspaceError, SourceManifestError) as error:
            raise SourceDeclarationError(error.code) from None

        # 实际 Python 包目录是源码来源身份的一部分，不能由扫描结果替代。
        package_root = source_snapshot.package_root
        root_identities.append((package_root, source_snapshot.identity))
        registrations.extend(
            EditableSourceRegistration(
                workflow_uuid=entry.workflow_uuid,
                package_id=manifest.package_id,
                package_root=package_root,
                relative_path=entry.relative_path,
                source_uri=(f"package://{manifest.package_id}/{entry.relative_path}"),
            )
            for entry in manifest.workflows
        )
    _validate_unique_registrations(registrations)
    return EditableSourceDiscoveryPlan(
        registrations=tuple(registrations),
        root_identities=tuple(root_identities),
    )


def _validate_unique_registrations(
    registrations: Iterable[EditableSourceRegistration],
) -> None:
    """验证一个完整发现计划中的来源身份互不冲突。

    参数：``registrations`` 是所有显式授权包产生的候选来源注册。
    返回：无；全部工作流、物理文件和来源 URI 身份唯一时正常返回。
    异常：任一身份重复或同一包身份指向不同目录时抛出
    ``SourceDeclarationError``。
    """

    workflow_identities: set[str] = set()
    physical_identities: set[tuple[Path, str]] = set()
    source_uri_identities: set[str] = set()
    package_roots: dict[str, Path] = {}
    for registration in registrations:
        # 三种身份分别保护工作流归属、物理文件所有权和跨进程来源寻址。
        physical_identity = (
            registration.package_root,
            registration.relative_path,
        )
        prior_package_root = package_roots.setdefault(
            registration.package_id,
            registration.package_root,
        )
        if (
            registration.workflow_uuid in workflow_identities
            or physical_identity in physical_identities
            or registration.source_uri in source_uri_identities
            or prior_package_root != registration.package_root
        ):
            raise SourceDeclarationError("duplicate_workflow_source")
        workflow_identities.add(registration.workflow_uuid)
        physical_identities.add(physical_identity)
        source_uri_identities.add(registration.source_uri)


__all__ = [
    "EditableSourceDiscoveryPlan",
    "EditableSourceRegistration",
    "SourceDeclarationError",
    "discover_editable_sources",
]
