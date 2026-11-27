"""领域包工作流 Python 源码的唯一可写目标。"""

from __future__ import annotations

import hashlib
import threading
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath

import yaml

from unilabos.workflow.source_discovery import (
    EditableSourceDiscoveryPlan,
    EditableSourceRegistration,
)
from unilabos.workflow.source_layout import workflow_source_directory
from unilabos.workflow.source_manifest import (
    EditablePackageManifest,
    SourceManifestError,
    parse_editable_package_manifest,
)
from unilabos.workflow.source_publication import (
    SourcePublicationConflict,
    SourcePublicationError,
    atomic_publish_source,
)
from unilabos.workflow.source_workspace import (
    MANIFEST_BYTE_LIMIT,
    SourceWorkspaceConflict,
    SourceWorkspaceError,
    delete_registered_source,
    read_package_root,
    read_registered_source,
    write_registered_source,
)


class DomainWorkflowSourceError(RuntimeError):
    """领域工作流源码目标不可用、冲突或发布失败。"""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class DomainWorkflowSourceTarget:
    """一个启动代唯一的可写领域包工作流源码目标。"""

    selected_root: Path
    package_root: Path
    package_id: str
    package_root_identity: tuple[int, int]
    _lock: threading.RLock

    @classmethod
    def from_discovery_plan(
        cls,
        plan: EditableSourceDiscoveryPlan,
    ) -> DomainWorkflowSourceTarget | None:
        """从单一主领域包发现计划建立可写目标。

        参数：``plan`` 是启动前冻结的工作流来源计划。返回：只有恰好一个实际
        Python 包根时返回目标；零个或多个包根时返回 ``None``，禁止猜测用户
        希望写入哪个领域包。异常：manifest 与计划包身份不一致时关闭式失败。
        """

        if not isinstance(plan, EditableSourceDiscoveryPlan):
            raise TypeError("工作流来源计划类型无效")
        if len(plan.root_identities) != 1:
            return None
        package_root, expected_identity = plan.root_identities[0]
        package_root = Path(package_root)
        try:
            metadata = package_root.lstat()
            snapshot = read_package_root(package_root.parent)
            manifest = parse_editable_package_manifest(snapshot.manifest_bytes)
        except (OSError, SourceManifestError, SourceWorkspaceError) as error:
            raise DomainWorkflowSourceError("source_target_unavailable") from error
        actual_identity = metadata.st_dev, metadata.st_ino
        if (
            actual_identity != expected_identity
            or snapshot.selected_root / manifest.package_id != package_root
            or any(
                registration.package_id != manifest.package_id
                or registration.package_root != package_root
                for registration in plan.registrations
            )
        ):
            raise DomainWorkflowSourceError("source_target_unavailable")
        return cls(
            selected_root=snapshot.selected_root,
            package_root=package_root,
            package_id=manifest.package_id,
            package_root_identity=actual_identity,
            _lock=threading.RLock(),
        )

    @staticmethod
    def default_file_name(workflow_uuid: str) -> str:
        """为 JSON 或无文件名导入生成稳定、无碰撞的 Python 文件名。"""

        return f"workflow_{workflow_uuid.replace('-', '')}.py"

    def registration(
        self,
        *,
        workflow_uuid: str,
        file_name: str,
        workflow_type: str = "normal",
    ) -> EditableSourceRegistration:
        """构造尚未写盘的规范来源身份。

        参数：``workflow_uuid`` 是工作流（Workflow）稳定身份，``file_name`` 是
        不含路径的 Python 文件名，``workflow_type`` 决定普通工作流或实验操作
        的独立源码目录。返回：尚未写文件或修改 manifest 的来源注册。异常：
        文件名或工作流类型非法时关闭式失败，不创建目录。
        """

        normalized_name = _file_name(file_name)
        source_directory = workflow_source_directory(workflow_type)
        relative_path = PurePosixPath(source_directory, normalized_name).as_posix()
        return EditableSourceRegistration(
            workflow_uuid=workflow_uuid,
            package_id=self.package_id,
            package_root=self.package_root,
            relative_path=relative_path,
            source_uri=f"package://{self.package_id}/{relative_path}",
        )

    def provision(
        self,
        *,
        registration: EditableSourceRegistration,
        python_source: str,
    ) -> EditableSourceRegistration:
        """以 CAS 创建源码并把同一身份追加到 ``package.yaml``。

        参数：``registration`` 必须由当前目标生成；``python_source`` 是已经完成
        AST、目录和图固定点校验的规范源码。返回已发布注册。源码先发布，manifest
        后发布；manifest 发布失败时会按源码内容校验删除本次新建文件，避免留下
        未登记孤儿。已有同内容源码不会被覆盖，发生并发变化时关闭式失败。
        """

        encoded = python_source.encode("utf-8")
        if not encoded:
            raise DomainWorkflowSourceError("source_publication_failed")
        self._require_registration(registration)
        with self._lock:
            manifest, manifest_bytes = self._current_manifest()
            expected_entry = f"{self.package_id}/{registration.relative_path}"
            by_uuid = {
                item.workflow_uuid: item.relative_path for item in manifest.workflows
            }
            by_path = {
                item.relative_path: item.workflow_uuid for item in manifest.workflows
            }
            if registration.workflow_uuid in by_uuid:
                raise DomainWorkflowSourceError("source_identity_conflict")
            if registration.relative_path in by_path:
                raise DomainWorkflowSourceError("source_identity_conflict")

            source_row = _registration_row(registration)
            intended_hash = _sha256(encoded)
            source_created = False
            try:
                existing = read_registered_source(source_row)
                if existing is None:
                    write_registered_source(
                        source_row,
                        encoded,
                        expected_hash=None,
                    )
                    source_created = True
                elif existing.draft_hash != intended_hash:
                    raise DomainWorkflowSourceError("source_identity_conflict")
            except DomainWorkflowSourceError:
                raise
            except SourceWorkspaceConflict as error:
                raise DomainWorkflowSourceError("source_identity_conflict") from error
            except (OSError, SourceWorkspaceError) as error:
                raise DomainWorkflowSourceError("source_publication_failed") from error

            manifest_document = {
                "package": {"name": manifest.package_id},
                "workflows": [
                    {
                        "workflow_uuid": item.workflow_uuid,
                        "source": f"{manifest.package_id}/{item.relative_path}",
                    }
                    for item in manifest.workflows
                ]
                + [
                    {
                        "workflow_uuid": registration.workflow_uuid,
                        "source": expected_entry,
                    }
                ],
            }
            published_manifest = yaml.safe_dump(
                manifest_document,
                allow_unicode=True,
                sort_keys=False,
            ).encode("utf-8")
            try:
                # 先复用封闭解析器证明序列化结果仍是规范 manifest，再做文件 CAS。
                parse_editable_package_manifest(published_manifest)
                atomic_publish_source(
                    parent_path=self.selected_root,
                    target_name="package.yaml",
                    content=published_manifest,
                    byte_limit=MANIFEST_BYTE_LIMIT,
                    expected_hash=_sha256(manifest_bytes),
                )
            except (SourceManifestError, SourcePublicationError) as error:
                try:
                    self._rollback_new_source(
                        source_row,
                        source_created=source_created,
                        expected_hash=intended_hash,
                    )
                except (
                    SourcePublicationConflict,
                    SourceWorkspaceError,
                ) as rollback_error:
                    raise DomainWorkflowSourceError(
                        "source_publication_failed"
                    ) from rollback_error
                raise DomainWorkflowSourceError("source_publication_failed") from error
            except SourcePublicationConflict as error:
                try:
                    self._rollback_new_source(
                        source_row,
                        source_created=source_created,
                        expected_hash=intended_hash,
                    )
                except (
                    SourcePublicationConflict,
                    SourceWorkspaceError,
                ) as rollback_error:
                    raise DomainWorkflowSourceError(
                        "source_publication_failed"
                    ) from rollback_error
                raise DomainWorkflowSourceError("source_identity_conflict") from error
        return registration

    @staticmethod
    def _rollback_new_source(
        source_row: dict[str, str],
        *,
        source_created: bool,
        expected_hash: str,
    ) -> None:
        """manifest 发布失败时按内容校验清理本次新建源码。

        参数：``source_row`` 是待清理的来源身份；``source_created`` 表示本次调用
        是否新建了源码；``expected_hash`` 是本次源码内容哈希。返回：源码原本存在
        或已被其他写入者改变时不删除；内容仍是本次新建版本时删除文件。异常：路径
        不安全、文件读取失败或删除失败抛出 ``SourceWorkspaceError``；并发内容变化
        抛出 ``SourcePublicationConflict``，调用方必须把导入关闭式判为失败。
        """

        if not source_created:
            return
        current = read_registered_source(source_row)
        if current is None:
            return
        if current.draft_hash != expected_hash:
            raise SourcePublicationConflict("draft_hash_conflict")
        delete_registered_source(source_row)

    def unregister(
        self,
        *,
        registration: EditableSourceRegistration,
        remove_source: bool = True,
    ) -> None:
        """以 CAS 从 ``package.yaml`` 注销并删除一个工作流来源。

        参数：``registration`` 必须是当前目标中已登记的同一 UUID 与路径。返回：
        无；成功后来源登记和源码文件都不存在，后续启动不会复活该工作流。
        """

        self._require_registration(registration)
        with self._lock:
            manifest, manifest_bytes = self._current_manifest()
            by_uuid = {
                item.workflow_uuid: item.relative_path for item in manifest.workflows
            }
            by_path = {
                item.relative_path: item.workflow_uuid for item in manifest.workflows
            }
            existing_path = by_uuid.get(registration.workflow_uuid)
            existing_uuid = by_path.get(registration.relative_path)
            if existing_path is None and existing_uuid is None:
                if remove_source:
                    try:
                        delete_registered_source(_registration_row(registration))
                    except SourceWorkspaceError as error:
                        raise DomainWorkflowSourceError("source_publication_failed") from error
                return
            if (
                existing_path != registration.relative_path
                or existing_uuid != registration.workflow_uuid
            ):
                raise DomainWorkflowSourceError("source_identity_conflict")

            source_row = _registration_row(registration)
            source_bytes: bytes | None = None
            if remove_source:
                try:
                    source = read_registered_source(source_row)
                    source_bytes = source.python_source.encode("utf-8") if source else None
                    delete_registered_source(source_row)
                except SourceWorkspaceError as error:
                    raise DomainWorkflowSourceError("source_publication_failed") from error

            published_manifest = yaml.safe_dump(
                {
                    "package": {"name": manifest.package_id},
                    "workflows": [
                        {
                            "workflow_uuid": item.workflow_uuid,
                            "source": (f"{manifest.package_id}/{item.relative_path}"),
                        }
                        for item in manifest.workflows
                        if item.workflow_uuid != registration.workflow_uuid
                    ],
                },
                allow_unicode=True,
                sort_keys=False,
            ).encode("utf-8")
            try:
                parse_editable_package_manifest(published_manifest)
                atomic_publish_source(
                    parent_path=self.selected_root,
                    target_name="package.yaml",
                    content=published_manifest,
                    byte_limit=MANIFEST_BYTE_LIMIT,
                    expected_hash=_sha256(manifest_bytes),
                )
            except (SourceManifestError, SourcePublicationError) as error:
                if source_bytes is not None:
                    try:
                        write_registered_source(source_row, source_bytes, expected_hash=None)
                    except SourceWorkspaceError:
                        raise DomainWorkflowSourceError("source_publication_failed") from error
                raise DomainWorkflowSourceError("source_publication_failed") from error
            except SourcePublicationConflict as error:
                if source_bytes is not None:
                    try:
                        write_registered_source(source_row, source_bytes, expected_hash=None)
                    except SourceWorkspaceError:
                        raise DomainWorkflowSourceError("source_publication_failed") from error
                raise DomainWorkflowSourceError("source_identity_conflict") from error

    def _current_manifest(self) -> tuple[EditablePackageManifest, bytes]:
        """读取并复核当前包目录与 manifest 身份。"""

        try:
            metadata = self.package_root.lstat()
            snapshot = read_package_root(self.selected_root)
            manifest = parse_editable_package_manifest(snapshot.manifest_bytes)
        except (OSError, SourceManifestError, SourceWorkspaceError) as error:
            raise DomainWorkflowSourceError("source_target_unavailable") from error
        if (
            (metadata.st_dev, metadata.st_ino) != self.package_root_identity
            or snapshot.selected_root != self.selected_root
            or manifest.package_id != self.package_id
        ):
            raise DomainWorkflowSourceError("source_target_unavailable")
        return manifest, snapshot.manifest_bytes

    def _require_registration(
        self,
        registration: EditableSourceRegistration,
    ) -> None:
        if (
            not isinstance(registration, EditableSourceRegistration)
            or registration.package_id != self.package_id
            or registration.package_root != self.package_root
            or registration.source_uri
            != f"package://{self.package_id}/{registration.relative_path}"
        ):
            raise DomainWorkflowSourceError("source_target_unavailable")


def _file_name(value: str) -> str:
    """校验一个无路径 Python 文件名。"""

    if not isinstance(value, str):
        raise DomainWorkflowSourceError("source_identity_conflict")
    normalized = value.strip()
    posix = PurePosixPath(normalized)
    windows = PureWindowsPath(normalized)
    if (
        not normalized
        or len(normalized.encode("utf-8")) > 255
        or posix.name != normalized
        or windows.name != normalized
        or posix.suffix.lower() != ".py"
        or any(ord(character) < 0x20 or character == "\x7f" for character in normalized)
    ):
        raise DomainWorkflowSourceError("source_identity_conflict")
    return normalized


def _registration_row(
    registration: EditableSourceRegistration,
) -> dict[str, str]:
    return {
        "workflow_uuid": registration.workflow_uuid,
        "package_id": registration.package_id,
        "package_root": str(registration.package_root),
        "relative_path": registration.relative_path,
        "source_uri": registration.source_uri,
    }


def _sha256(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


__all__ = [
    "DomainWorkflowSourceError",
    "DomainWorkflowSourceTarget",
]
