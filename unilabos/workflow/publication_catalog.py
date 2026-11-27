"""领域包内已发布工作流合同的文件目录。"""

from __future__ import annotations

import hashlib
import json
import stat
import threading
from collections.abc import Mapping
from copy import deepcopy
from pathlib import Path
from typing import Any
from uuid import UUID

from unilabos.workflow.source_file_access import (
    StableFileAccessError,
    assert_directory_identity,
    directory_identity,
    ensure_child_directory,
    is_reparse_point,
    read_regular_path,
)
from unilabos.workflow.source_publication import (
    SourcePublicationConflict,
    SourcePublicationError,
    atomic_publish_source,
)

PUBLICATION_FILE_NAME = "workflow_publications.json"
# 旧文件仅作为迁移/回退格式读取；新写入按工作流分区，避免单个大合同导致每次
# 查询都解析整个领域包历史。
PUBLICATION_DIRECTORY_NAME = "workflow_publications"
PUBLICATION_MANIFEST_NAME = "manifest.json"
PUBLICATION_CONTRACT_DIRECTORY_NAME = "contracts"
PUBLICATION_FILE_BYTE_LIMIT = 16 * 1024 * 1024
# 分区合同包含完整的递归执行图，预算独立于旧聚合索引；仍保留硬上限。
PUBLICATION_CONTRACT_BYTE_LIMIT = 64 * 1024 * 1024
PUBLICATION_MANIFEST_BYTE_LIMIT = 1024 * 1024
PUBLICATION_LIMIT = 1000
_LEGACY_MIGRATION_VERSION = 2
# SQLite ``INTEGER`` 是有符号 64 位整数；在文件边界拒绝更大的 JSON 整数，
# 避免损坏的发布清单直到合同恢复写库时才泄漏未分类 ``OverflowError``。
_SQLITE_INT64_MAX = (1 << 63) - 1


class WorkflowPublicationCatalogError(RuntimeError):
    """发布合同文件输入、并发或持久化错误。"""

    def __init__(self, code: str) -> None:
        """建立可由服务层稳定映射的目录错误。

        参数：``code`` 是输入、冲突或文件不可用的稳定分类。返回：无。异常：无；
        错误对象只携带稳定分类，不泄漏领域包本地路径或系统异常文本。
        """

        self.code = code
        super().__init__(code)


class WorkflowPublicationCatalog:
    """以领域包文件持久保存不可变发布合同。

    新格式按工作流隔离：

    ``workflow_publications/<workflow_uuid>/manifest.json`` 保存最新合同指针和合同
    UUID 列表，``contracts/<contract_uuid>.json`` 保存完整合同。旧的
    ``workflow_publications.json`` 仍可读取，并在首次写入时迁移，以兼容已有领域包。
    """

    def __init__(
        self,
        *,
        package_root: Path,
        package_root_identity: tuple[int, int],
    ) -> None:
        """绑定启动时已冻结身份的唯一领域包目录。

        参数：``package_root`` 是领域包根目录，``package_root_identity`` 是发现
        时的设备号和索引节点。返回：无。异常：目录被替换时在首次读写关闭失败。
        """

        self._package_root = Path(package_root)
        self._package_root_identity = package_root_identity
        # 该进程锁串行化同一领域包发布目录的读改写；跨进程竞争仍由文件哈希 CAS
        # 判断，锁本身不充当文件权威或分布式互斥。
        self._lock = threading.RLock()

    def list_entries(self) -> list[dict[str, Any]]:
        """读取全部不可变发布合同。

        参数：无。返回：按工作流 UUID 和内部发布合同序号排序的深拷贝条目，调用方修改
        返回值不会改变文件权威。异常：目录身份、文件类型或 JSON 合同不可信时
        抛 ``WorkflowPublicationCatalogError``，不返回部分结果。
        """

        entries, _present = self.list_entries_with_presence()
        return entries

    def list_entries_with_presence(self) -> tuple[list[dict[str, Any]], bool]:
        """读取发布合同并明确区分“文件缺失”和“合法空目录”。

        参数：无。返回：``(entries, present)``，其中 ``present`` 仅在发布文件
        存在且通过完整校验时为 ``True``；合法但为空的发布目录仍返回 ``True``。
        异常：目录身份、文件类型或 JSON 合同不可信时抛
        ``WorkflowPublicationCatalogError``，不返回部分结果。

        该区别用于组合根的旧包兼容策略：只有从未拥有发布目录的旧包才允许
        启动期间把源码作为临时候选；一个显式空发布目录表示“当前没有可引用合同”，
        不能误当成缺失文件而放宽权限。
        """

        with self._lock:
            entries, present = self._read_storage()
            # Storage reads allocate fresh contracts; no shared cache escapes this call.
            return entries, present

    def latest_for_workflow(self, workflow_uuid: str) -> dict[str, Any] | None:
        """读取指定工作流最新发布条目。

        参数：``workflow_uuid`` 是工作流稳定身份。返回：内部发布合同序号最大的独立条目，
        没有发布记录时返回 ``None``。异常：UUID 或发布文件不合法时抛目录错误。
        """

        # ``identity`` 是查询目标工作流的规范 UUID，只用于筛选合同所属关系。
        identity = _uuid(workflow_uuid)
        with self._lock:
            publication_root = self._publication_root_if_present()
            if publication_root is not None:
                root, root_identity = publication_root
                try:
                    legacy_entries, _legacy_hash, legacy_kind = (
                        self._read_legacy_document()
                    )
                except WorkflowPublicationCatalogError:
                    # 新目录已经存在时，旧路径可能正处于迁移替换窗口；旧文件
                    # 只作为可选回退，不能覆盖已经提交的新目录权威。
                    legacy_entries, legacy_kind = [], None
                legacy_matches = [
                    entry
                    for entry in legacy_entries
                    if entry["contract"]["workflow_uuid"] == identity
                ]
                workflow_dir = self._workflow_directory_if_present(
                    root,
                    root_identity,
                    identity,
                )
                if workflow_dir is None:
                    if legacy_kind == "v1":
                        return deepcopy(_latest_entry(legacy_matches))
                    return None
                workflow_path, workflow_identity = workflow_dir
                entries, latest_uuid, _manifest_hash = self._read_workflow_entries(
                    workflow_path,
                    workflow_identity,
                    identity,
                )
                if legacy_kind == "v1":
                    entries = _merge_entries(entries, legacy_matches)
                    # 迁移窗口内旧文件可能包含比新 manifest 更晚的合同。manifest
                    # 只是新目录的提交指针，不能让旧格式中的较新合同被查询隐藏。
                    return deepcopy(_latest_entry(entries))
                if latest_uuid is not None:
                    for entry in entries:
                        if entry["contract"]["uuid"] == latest_uuid:
                            return deepcopy(entry)
                return deepcopy(_latest_entry(entries))

            entries, _present, _kind = self._read_legacy_document()
            matches = [
                entry
                for entry in entries
                if entry["contract"]["workflow_uuid"] == identity
            ]
            return deepcopy(_latest_entry(matches))

    def save(
        self,
        *,
        source_draft_hash: str,
        contract: Mapping[str, Any],
    ) -> None:
        """原子保存一次不可变发布结果。

        参数：``source_draft_hash`` 标识发布时的 Python 源码字节，``contract``
        是完整冻结合同。返回：无。同一合同可幂等重写；身份内容冲突、目录并发或
        文件异常时关闭当前发布。
        """

        entry = _entry(source_draft_hash, contract)
        with self._lock:
            entries, _present = self._read_storage()
            existing = next(
                (
                    item
                    for item in entries
                    if item["contract"]["uuid"] == entry["contract"]["uuid"]
                ),
                None,
            )
            if existing is not None and existing != entry:
                raise WorkflowPublicationCatalogError("conflict")

            # 任意写入都会先把旧包升级到新目录，再增加本次合同。所有合同和 manifest
            # 写完后才替换旧文件为轻量标记，因此迁移中断时下次启动仍可从旧文件重试。
            try:
                legacy_entries, legacy_hash, legacy_kind = self._read_legacy_document()
            except WorkflowPublicationCatalogError:
                # 新目录一旦存在即为权威；旧路径被包管理过程暂时替换（包括替换成
                # tombstone）时，不应使有效的工作流分目录不可用。
                if self._publication_root_if_present() is None:
                    raise
                legacy_entries, legacy_hash, legacy_kind = [], None, None
            if legacy_kind == "v1":
                entries = _merge_entries(entries, legacy_entries)

            if existing is None:
                if len(entries) >= PUBLICATION_LIMIT:
                    raise WorkflowPublicationCatalogError("invalid_input")
                entries.append(entry)
            entries = _validate_entries(entries)

            self._write_new_entries(entries)
            # 在历史路径保留轻量兼容索引。这里只含 UUID/修订元数据，不含图快照；
            # 旧工具仍能识别发布目录，同时不会重新制造高频读取的大 JSON 文件。
            try:
                self._write_legacy_marker(
                    entries=entries,
                    expected_hash=legacy_hash,
                )
            except WorkflowPublicationCatalogError:
                # 旧索引不是发布权威；新目录已经提交成功时，索引同步失败不能把
                # 合同回报为未发布，否则运行时回滚后重启反而会恢复幽灵合同。
                pass

    def delete_workflow(self, workflow_uuid: str) -> None:
        """移除已删除工作流的全部发布合同。

        参数：``workflow_uuid`` 是已经完成定义删除的工作流稳定身份。返回：无；
        没有记录时幂等成功且不重写文件。异常：UUID、目录、并发 CAS 或文件内容
        不合法时抛目录错误；删除失败不会伪造发布文件已清理。
        """

        # ``identity`` 是待清理工作流的规范 UUID；合同 UUID 不参与全集筛选。
        identity = _uuid(workflow_uuid)
        with self._lock:
            publication_root = self._publication_root_if_present()
            if publication_root is not None:
                root, root_identity = publication_root
                try:
                    legacy_entries, legacy_hash, legacy_kind = (
                        self._read_legacy_document()
                    )
                except WorkflowPublicationCatalogError:
                    legacy_entries, legacy_hash, legacy_kind = [], None, None
                if legacy_kind == "v1":
                    existing_entries, _ = self._read_storage()
                    merged_entries = _merge_entries(existing_entries, legacy_entries)
                    self._write_new_entries(merged_entries)
                    assert legacy_hash is not None
                    self._write_legacy_marker(
                        entries=merged_entries,
                        expected_hash=legacy_hash,
                    )
                workflow_dir = self._workflow_directory_if_present(
                    root,
                    root_identity,
                    identity,
                )
                if workflow_dir is not None:
                    self._remove_workflow_directory(*workflow_dir)
                    # 删除完成后同步轻量兼容索引，避免旧工具看到已经删除的工作流。
                    # 新目录仍是权威；这里只在索引本来存在时更新，不因幂等删除凭空
                    # 创建历史文件。
                    try:
                        _, marker_hash, marker_kind = self._read_legacy_document()
                    except WorkflowPublicationCatalogError:
                        marker_hash, marker_kind = None, None
                    if marker_kind == "marker":
                        try:
                            remaining_entries, _ = self._read_storage()
                            self._write_legacy_marker(
                                entries=remaining_entries,
                                expected_hash=marker_hash,
                            )
                        except WorkflowPublicationCatalogError:
                            # 删除权威合同已经完成；兼容索引失败只会留下过期元数据，
                            # 不应把成功删除报告为失败。
                            pass
                return

            entries, expected_hash, kind = self._read_legacy_document()
            if kind != "v1":
                return
            remaining = [
                entry
                for entry in entries
                if entry["contract"]["workflow_uuid"] != identity
            ]
            if len(remaining) != len(entries):
                self._write_legacy(remaining, expected_hash=expected_hash)

    def _read_storage(self) -> tuple[list[dict[str, Any]], bool]:
        """读取新目录或兼容旧文件，返回规范合同全集和目录存在标记。"""

        publication_root = self._publication_root_if_present()
        if publication_root is None:
            legacy_entries, _legacy_hash, legacy_kind = self._read_legacy_document()
            if legacy_kind in {"v1", "marker"}:
                return legacy_entries, True
            return [], False

        root, root_identity = publication_root
        try:
            legacy_entries, _legacy_hash, legacy_kind = self._read_legacy_document()
        except WorkflowPublicationCatalogError:
            # 迁移完成后新目录是独立权威；包清理过程删除或暂时替换旧路径时，不应使
            # 已提交的工作流分目录合同失效。
            legacy_entries, legacy_kind = [], None
        entries: list[dict[str, Any]] = []
        try:
            for child in sorted(root.iterdir(), key=lambda item: item.name):
                if child.name.startswith("."):
                    continue
                try:
                    workflow_uuid = _uuid(child.name)
                except WorkflowPublicationCatalogError:
                    raise WorkflowPublicationCatalogError("unavailable") from None
                if workflow_uuid != child.name:
                    raise WorkflowPublicationCatalogError("unavailable")
                workflow_identity = directory_identity(child)
                entries.extend(
                    self._read_workflow_entries(
                        child,
                        workflow_identity,
                        workflow_uuid,
                    )[0]
                )
            assert_directory_identity(root, root_identity)
            # 旧文件还没有被轻量标记替换时，迁移中的新旧目录暂时合并读取。只有内容
            # 完全一致的重复合同才允许通过。
            if legacy_kind == "v1":
                entries = _merge_entries(entries, legacy_entries)
            try:
                normalized = _validate_entries(entries, copy_contracts=False)
            except WorkflowPublicationCatalogError:
                raise WorkflowPublicationCatalogError("unavailable") from None
            return normalized, True
        except WorkflowPublicationCatalogError:
            raise
        except (OSError, StableFileAccessError, TypeError, ValueError):
            raise WorkflowPublicationCatalogError("unavailable") from None

    def _publication_root_if_present(
        self,
    ) -> tuple[Path, tuple[int, int]] | None:
        """返回新发布根目录及其固定身份；缺失时返回 ``None``。"""

        try:
            assert_directory_identity(
                self._package_root,
                self._package_root_identity,
            )
            path = self._package_root / PUBLICATION_DIRECTORY_NAME
            try:
                path.lstat()
            except FileNotFoundError:
                return None
            identity = directory_identity(path)
            assert_directory_identity(self._package_root, self._package_root_identity)
            return path, identity
        except FileNotFoundError:
            return None
        except (OSError, StableFileAccessError):
            raise WorkflowPublicationCatalogError("unavailable") from None

    @staticmethod
    def _workflow_directory_if_present(
        root: Path,
        root_identity: tuple[int, int],
        workflow_uuid: str,
    ) -> tuple[Path, tuple[int, int]] | None:
        """返回一个工作流目录及其身份；缺失时返回 ``None``。"""

        try:
            assert_directory_identity(root, root_identity)
            path = root / workflow_uuid
            try:
                path.lstat()
            except FileNotFoundError:
                return None
            identity = directory_identity(path)
            assert_directory_identity(root, root_identity)
            return path, identity
        except FileNotFoundError:
            return None
        except (OSError, StableFileAccessError):
            raise WorkflowPublicationCatalogError("unavailable") from None

    def _read_workflow_entries(
        self,
        workflow_dir: Path,
        workflow_identity: tuple[int, int],
        workflow_uuid: str,
    ) -> tuple[list[dict[str, Any]], str | None, str | None]:
        """读取一个工作流目录内的合同文件和最新指针。"""

        try:
            manifest = self._read_manifest(
                workflow_dir, workflow_identity, workflow_uuid
            )
            contracts_path = workflow_dir / PUBLICATION_CONTRACT_DIRECTORY_NAME
            try:
                contracts_identity = directory_identity(contracts_path)
            except StableFileAccessError as error:
                try:
                    contracts_path.lstat()
                except FileNotFoundError:
                    if manifest is not None and manifest["contract_uuids"]:
                        raise WorkflowPublicationCatalogError("unavailable") from None
                    return (
                        [],
                        None if manifest is None else manifest["latest_contract_uuid"],
                        None if manifest is None else manifest["content_hash"],
                    )
                raise WorkflowPublicationCatalogError("unavailable") from error

            # 合同文件先写、manifest 后提交；如果 manifest 缺失，说明这批文件没有
            # 成为发布权威（可能是进程在原子提交窗口退出），不能把孤立合同恢复成
            # 已发布记录。下次写入会重用这些文件并重新提交 manifest。
            if manifest is None:
                assert_directory_identity(contracts_path, contracts_identity)
                return [], None, None

            files = self._contract_files(contracts_path, contracts_identity)
            assert manifest is not None
            selected = manifest["contract_uuids"]
            entries = []
            for contract_uuid in selected:
                path = files.get(contract_uuid)
                if path is None:
                    raise WorkflowPublicationCatalogError("unavailable")
                entry = self._read_contract_file(path, contract_uuid, workflow_uuid)
                entries.append(entry)
            latest_uuid = None if manifest is None else manifest["latest_contract_uuid"]
            if latest_uuid is not None and latest_uuid not in files:
                raise WorkflowPublicationCatalogError("unavailable")
            if latest_uuid is not None and latest_uuid not in selected:
                raise WorkflowPublicationCatalogError("unavailable")
            try:
                normalized = _validate_entries(entries, copy_contracts=False)
            except WorkflowPublicationCatalogError:
                raise WorkflowPublicationCatalogError("unavailable") from None
            return normalized, latest_uuid, manifest["content_hash"]
        except WorkflowPublicationCatalogError:
            raise
        except (OSError, StableFileAccessError, TypeError, ValueError):
            raise WorkflowPublicationCatalogError("unavailable") from None

    @staticmethod
    def _contract_files(
        contracts_path: Path,
        contracts_identity: tuple[int, int],
    ) -> dict[str, Path]:
        """校验合同目录内的文件名、类型和身份，返回 UUID 到路径映射。"""

        result: dict[str, Path] = {}
        try:
            for child in sorted(contracts_path.iterdir(), key=lambda item: item.name):
                if child.name.startswith("."):
                    continue
                if child.suffix != ".json" or not child.stem:
                    raise WorkflowPublicationCatalogError("unavailable")
                try:
                    contract_uuid = _uuid(child.stem)
                except WorkflowPublicationCatalogError:
                    raise WorkflowPublicationCatalogError("unavailable") from None
                if child.name != f"{contract_uuid}.json":
                    raise WorkflowPublicationCatalogError("unavailable")
                metadata = child.lstat()
                if not stat.S_ISREG(metadata.st_mode) or is_reparse_point(metadata):
                    raise WorkflowPublicationCatalogError("unavailable")
                result[contract_uuid] = child
            assert_directory_identity(contracts_path, contracts_identity)
            return result
        except WorkflowPublicationCatalogError:
            raise
        except (OSError, StableFileAccessError, TypeError, ValueError):
            raise WorkflowPublicationCatalogError("unavailable") from None

    @staticmethod
    def _read_contract_file(
        path: Path,
        contract_uuid: str,
        workflow_uuid: str,
    ) -> dict[str, Any]:
        """读取并校验一个完整合同文件。"""

        try:
            snapshot = read_regular_path(
                path,
                byte_limit=PUBLICATION_CONTRACT_BYTE_LIMIT,
                missing_ok=False,
            )
            assert snapshot is not None
            document = _load_json(snapshot.content)
            if not isinstance(document, dict) or set(document) != {
                "source_draft_hash",
                "contract",
            }:
                raise WorkflowPublicationCatalogError("unavailable")
            try:
                entry = _entry(document["source_draft_hash"], document["contract"])
            except WorkflowPublicationCatalogError:
                raise WorkflowPublicationCatalogError("unavailable") from None
            if (
                entry["contract"]["uuid"] != contract_uuid
                or entry["contract"]["workflow_uuid"] != workflow_uuid
            ):
                raise WorkflowPublicationCatalogError("unavailable")
            return entry
        except WorkflowPublicationCatalogError:
            raise
        except (OSError, StableFileAccessError, TypeError, ValueError):
            raise WorkflowPublicationCatalogError("unavailable") from None

    @staticmethod
    def _read_manifest(
        workflow_dir: Path,
        workflow_identity: tuple[int, int],
        workflow_uuid: str,
    ) -> dict[str, Any] | None:
        """读取工作流目录的轻量最新指针。"""

        try:
            snapshot = read_regular_path(
                workflow_dir / PUBLICATION_MANIFEST_NAME,
                byte_limit=PUBLICATION_MANIFEST_BYTE_LIMIT,
                missing_ok=True,
            )
            if snapshot is None:
                return None
            document = _load_json(snapshot.content)
            if not isinstance(document, dict) or set(document) != {
                "version",
                "workflow_uuid",
                "latest_contract_uuid",
                "contract_uuids",
            }:
                raise WorkflowPublicationCatalogError("unavailable")
            if document["version"] != 1 or document["workflow_uuid"] != workflow_uuid:
                raise WorkflowPublicationCatalogError("unavailable")
            latest_uuid = document["latest_contract_uuid"]
            if latest_uuid is not None:
                try:
                    latest_uuid = _uuid(latest_uuid)
                except WorkflowPublicationCatalogError:
                    raise WorkflowPublicationCatalogError("unavailable") from None
            raw_contracts = document["contract_uuids"]
            if not isinstance(raw_contracts, list) or not raw_contracts:
                raise WorkflowPublicationCatalogError("unavailable")
            try:
                contract_uuids = tuple(_uuid(value) for value in raw_contracts)
            except WorkflowPublicationCatalogError:
                raise WorkflowPublicationCatalogError("unavailable") from None
            if len(set(contract_uuids)) != len(contract_uuids):
                raise WorkflowPublicationCatalogError("unavailable")
            if latest_uuid is None or latest_uuid not in contract_uuids:
                raise WorkflowPublicationCatalogError("unavailable")
            assert_directory_identity(workflow_dir, workflow_identity)
            return {
                "latest_contract_uuid": latest_uuid,
                "contract_uuids": contract_uuids,
                "content_hash": _sha256(snapshot.content),
            }
        except WorkflowPublicationCatalogError:
            raise
        except (OSError, StableFileAccessError, TypeError, ValueError):
            raise WorkflowPublicationCatalogError("unavailable") from None

    def _write_new_entries(self, entries: list[dict[str, Any]]) -> None:
        """把完整合同按工作流写入新目录，并更新对应 manifest。"""

        grouped: dict[str, list[dict[str, Any]]] = {}
        for entry in entries:
            grouped.setdefault(entry["contract"]["workflow_uuid"], []).append(entry)
        try:
            publication_root = ensure_child_directory(
                self._package_root,
                expected_root_identity=self._package_root_identity,
                child_name=PUBLICATION_DIRECTORY_NAME,
            )
            publication_identity = directory_identity(publication_root)
            for workflow_uuid, workflow_entries in grouped.items():
                workflow_dir = ensure_child_directory(
                    publication_root,
                    expected_root_identity=publication_identity,
                    child_name=workflow_uuid,
                )
                workflow_identity = directory_identity(workflow_dir)
                contracts_dir = ensure_child_directory(
                    workflow_dir,
                    expected_root_identity=workflow_identity,
                    child_name=PUBLICATION_CONTRACT_DIRECTORY_NAME,
                )
                contracts_identity = directory_identity(contracts_dir)
                # 重新读取当前工作流 manifest，并把它并入本轮要写的合同。这样两个
                # OS 进程同时发布不同合同，即使都在第一次全集读取时看到旧状态，后
                # 写入者也不会用快照覆盖先写入者；manifest 的 CAS 仍负责捕获读取
                # 后到提交前的最后一小段竞态。
                current_entries, _current_latest, current_manifest_hash = (
                    self._read_workflow_entries(
                        workflow_dir,
                        workflow_identity,
                        workflow_uuid,
                    )
                )
                merged_entries = _merge_entries(current_entries, workflow_entries)
                for item in merged_entries:
                    self._write_contract_file(
                        contracts_dir,
                        contracts_identity,
                        item,
                    )
                self._write_manifest(
                    workflow_dir,
                    workflow_identity,
                    workflow_uuid,
                    merged_entries,
                    expected_hash=current_manifest_hash,
                )
            assert_directory_identity(self._package_root, self._package_root_identity)
        except WorkflowPublicationCatalogError:
            raise
        except (OSError, StableFileAccessError, TypeError, ValueError):
            raise WorkflowPublicationCatalogError("unavailable") from None

    @staticmethod
    def _write_contract_file(
        contracts_dir: Path,
        contracts_identity: tuple[int, int],
        entry: Mapping[str, Any],
    ) -> None:
        """首次安全写入合同文件；相同内容可幂等重放。"""

        contract_uuid = str(entry["contract"]["uuid"])
        content = (
            json.dumps(
                entry,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8")
        if len(content) > PUBLICATION_CONTRACT_BYTE_LIMIT:
            raise WorkflowPublicationCatalogError("invalid_input")
        path = contracts_dir / f"{contract_uuid}.json"
        try:
            existing = read_regular_path(
                path,
                byte_limit=PUBLICATION_CONTRACT_BYTE_LIMIT,
                missing_ok=True,
            )
            if existing is not None:
                existing_entry = WorkflowPublicationCatalog._read_contract_file(
                    path,
                    contract_uuid,
                    str(entry["contract"]["workflow_uuid"]),
                )
                if existing_entry != dict(entry):
                    raise WorkflowPublicationCatalogError("conflict")
                return
            assert_directory_identity(contracts_dir, contracts_identity)
            atomic_publish_source(
                parent_path=contracts_dir,
                target_name=f"{contract_uuid}.json",
                content=content,
                byte_limit=PUBLICATION_CONTRACT_BYTE_LIMIT,
                expected_hash=None,
            )
        except WorkflowPublicationCatalogError:
            raise
        except SourcePublicationConflict:
            # 另一个进程可能赢得首次创建竞态；重新读取后仅接受内容完全一致的合同，
            # 同一个 UUID 不能指向两份内容。
            try:
                existing_entry = WorkflowPublicationCatalog._read_contract_file(
                    path,
                    contract_uuid,
                    str(entry["contract"]["workflow_uuid"]),
                )
            except WorkflowPublicationCatalogError:
                raise WorkflowPublicationCatalogError("conflict") from None
            if existing_entry != dict(entry):
                raise WorkflowPublicationCatalogError("conflict")
        except (OSError, SourcePublicationError, StableFileAccessError):
            raise WorkflowPublicationCatalogError("unavailable") from None

    @staticmethod
    def _write_manifest(
        workflow_dir: Path,
        workflow_identity: tuple[int, int],
        workflow_uuid: str,
        entries: list[dict[str, Any]],
        *,
        expected_hash: str | None,
    ) -> None:
        """按 CAS 原子更新一个工作流的轻量最新指针。"""

        latest = _latest_entry(entries)
        assert latest is not None
        contract_uuids = [
            item["contract"]["uuid"]
            for item in sorted(
                entries,
                key=lambda item: int(item["contract"]["version"]),
            )
        ]
        content = (
            json.dumps(
                {
                    "version": 1,
                    "workflow_uuid": workflow_uuid,
                    "latest_contract_uuid": latest["contract"]["uuid"],
                    "contract_uuids": contract_uuids,
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8")
        path = workflow_dir / PUBLICATION_MANIFEST_NAME
        try:
            current = read_regular_path(
                path,
                byte_limit=PUBLICATION_MANIFEST_BYTE_LIMIT,
                missing_ok=True,
            )
            if current is not None and current.content == content:
                return
            current_hash = None if current is None else _sha256(current.content)
            # ``expected_hash`` 必须来自调用方在合并当前合同前的稳定读取，不能在这
            # 里把刚刚读到的内容当作期望值；后者会让并发发布静默丢失对方的合同。
            if current_hash != expected_hash:
                raise WorkflowPublicationCatalogError("conflict")
            assert_directory_identity(workflow_dir, workflow_identity)
            atomic_publish_source(
                parent_path=workflow_dir,
                target_name=PUBLICATION_MANIFEST_NAME,
                content=content,
                byte_limit=PUBLICATION_MANIFEST_BYTE_LIMIT,
                expected_hash=expected_hash,
            )
        except SourcePublicationConflict:
            raise WorkflowPublicationCatalogError("conflict") from None
        except (OSError, SourcePublicationError, StableFileAccessError):
            raise WorkflowPublicationCatalogError("unavailable") from None

    def _remove_workflow_directory(
        self,
        workflow_dir: Path,
        workflow_identity: tuple[int, int],
    ) -> None:
        """删除一个工作流的发布合同文件，保留发布根目录作为严格标记。"""

        try:
            manifest_path = workflow_dir / PUBLICATION_MANIFEST_NAME
            _unlink_regular(manifest_path)
            contracts_path = workflow_dir / PUBLICATION_CONTRACT_DIRECTORY_NAME
            contracts_identity = _optional_directory_identity(contracts_path)
            if contracts_identity is not None:
                for path in self._contract_files(
                    contracts_path, contracts_identity
                ).values():
                    _unlink_regular(path)
                contracts_path.rmdir()
            assert_directory_identity(workflow_dir, workflow_identity)
            workflow_dir.rmdir()
        except WorkflowPublicationCatalogError:
            raise
        except (OSError, StableFileAccessError):
            raise WorkflowPublicationCatalogError("unavailable") from None

    def _read_legacy_document(
        self,
    ) -> tuple[list[dict[str, Any]], str | None, str | None]:
        """读取旧总文件，返回条目、文件哈希和 ``v1``/``marker`` 类型。"""

        try:
            assert_directory_identity(
                self._package_root,
                self._package_root_identity,
            )
            snapshot = read_regular_path(
                self._package_root / PUBLICATION_FILE_NAME,
                byte_limit=PUBLICATION_FILE_BYTE_LIMIT,
                missing_ok=True,
            )
            if snapshot is None:
                return [], None, None
            document = _load_json(snapshot.content)
            if (
                isinstance(document, dict)
                and document.get("version") == _LEGACY_MIGRATION_VERSION
                and document.get("storage") == PUBLICATION_DIRECTORY_NAME
                and _valid_legacy_marker_rows(document.get("publications"))
            ):
                return [], _sha256(snapshot.content), "marker"
            return _decode(snapshot.content), _sha256(snapshot.content), "v1"
        except WorkflowPublicationCatalogError:
            raise
        except (OSError, StableFileAccessError):
            raise WorkflowPublicationCatalogError("unavailable") from None

    def _write_legacy_marker(
        self,
        *,
        entries: list[dict[str, Any]],
        expected_hash: str | None,
    ) -> None:
        """把旧文件压缩为轻量兼容索引，避免继续保存完整合同全集。"""

        publications = [
            {
                "workflow_uuid": item["contract"]["workflow_uuid"],
                "contract_uuid": item["contract"]["uuid"],
                "workflow_revision": item["contract"]["workflow_revision"],
                "version": item["contract"]["version"],
            }
            for item in entries
        ]
        content = (
            json.dumps(
                {
                    "version": _LEGACY_MIGRATION_VERSION,
                    "storage": PUBLICATION_DIRECTORY_NAME,
                    "publications": publications,
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8")
        try:
            atomic_publish_source(
                parent_path=self._package_root,
                target_name=PUBLICATION_FILE_NAME,
                content=content,
                byte_limit=PUBLICATION_FILE_BYTE_LIMIT,
                expected_hash=expected_hash,
            )
        except SourcePublicationConflict:
            raise WorkflowPublicationCatalogError("conflict") from None
        except (OSError, SourcePublicationError, StableFileAccessError):
            raise WorkflowPublicationCatalogError("unavailable") from None

    def _write_legacy(
        self,
        entries: list[dict[str, Any]],
        *,
        expected_hash: str | None,
    ) -> None:
        """兼容尚未迁移领域包的旧格式删除写入。"""

        normalized = _validate_entries(entries)
        content = (
            json.dumps(
                {"version": 1, "publications": normalized},
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8")
        try:
            atomic_publish_source(
                parent_path=self._package_root,
                target_name=PUBLICATION_FILE_NAME,
                content=content,
                byte_limit=PUBLICATION_FILE_BYTE_LIMIT,
                expected_hash=expected_hash,
            )
        except SourcePublicationConflict:
            raise WorkflowPublicationCatalogError("conflict") from None
        except (OSError, SourcePublicationError, StableFileAccessError):
            raise WorkflowPublicationCatalogError("unavailable") from None


def _decode(raw: bytes) -> list[dict[str, Any]]:
    """解析封闭 JSON 文档并拒绝重复键和非有限数值。

    参数：``raw`` 是已通过普通文件和大小检查的字节。返回：规范排序后的发布
    条目。异常：编码、重复键、版本、结构或合同不合法时抛 ``unavailable``。
    """

    try:
        document = _load_json(raw)
    except (
        MemoryError,
        RecursionError,
        UnicodeError,
        ValueError,
        json.JSONDecodeError,
    ):
        raise WorkflowPublicationCatalogError("unavailable") from None
    if not isinstance(document, dict) or set(document) != {"version", "publications"}:
        raise WorkflowPublicationCatalogError("unavailable")
    if document["version"] != 1 or not isinstance(document["publications"], list):
        raise WorkflowPublicationCatalogError("unavailable")
    try:
        return _validate_entries(document["publications"])
    except WorkflowPublicationCatalogError:
        raise WorkflowPublicationCatalogError("unavailable") from None


def _load_json(raw: bytes) -> Any:
    """解析无重复键、无非有限数值的 JSON 文档。"""

    def object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result

    try:
        return json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=object_pairs,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
        )
    except (
        MemoryError,
        RecursionError,
        UnicodeError,
        ValueError,
        json.JSONDecodeError,
    ):
        raise WorkflowPublicationCatalogError("unavailable") from None


def _latest_entry(values: list[dict[str, Any]]) -> dict[str, Any] | None:
    """按内部发布序号返回最新合同；空集合返回 ``None``。"""

    if not values:
        return None
    return max(values, key=lambda item: int(item["contract"]["version"]))


def _merge_entries(
    left: list[dict[str, Any]],
    right: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """合并迁移期间的新旧合同，拒绝同一身份的内容漂移。"""

    by_uuid: dict[str, dict[str, Any]] = {}
    for entry in [*left, *right]:
        contract_uuid = entry["contract"]["uuid"]
        existing = by_uuid.get(contract_uuid)
        if existing is not None and existing != entry:
            raise WorkflowPublicationCatalogError("unavailable")
        by_uuid[contract_uuid] = entry
    return _validate_entries(list(by_uuid.values()))


def _valid_legacy_marker_rows(value: Any) -> bool:
    """校验 v2 兼容索引只包含轻量身份元数据。"""

    if not isinstance(value, list) or len(value) > PUBLICATION_LIMIT:
        return False
    contract_uuids: set[str] = set()
    for row in value:
        if not isinstance(row, dict) or set(row) != {
            "workflow_uuid",
            "contract_uuid",
            "workflow_revision",
            "version",
        }:
            return False
        try:
            _uuid(row["workflow_uuid"])
            contract_uuid = _uuid(row["contract_uuid"])
        except WorkflowPublicationCatalogError:
            return False
        if contract_uuid in contract_uuids:
            return False
        contract_uuids.add(contract_uuid)
        for field in ("workflow_revision", "version"):
            value_int = row[field]
            if (
                isinstance(value_int, bool)
                or not isinstance(value_int, int)
                or value_int < 1
                or value_int > _SQLITE_INT64_MAX
            ):
                return False
    return True


def _optional_directory_identity(path: Path) -> tuple[int, int] | None:
    """读取可缺失目录的身份；非目录和链接均按不可信处理。"""

    try:
        path.lstat()
    except FileNotFoundError:
        return None
    except OSError:
        raise WorkflowPublicationCatalogError("unavailable") from None
    try:
        return directory_identity(path)
    except StableFileAccessError:
        raise WorkflowPublicationCatalogError("unavailable") from None


def _unlink_regular(path: Path) -> None:
    """删除一个安全普通文件，文件缺失按幂等成功处理。"""

    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return
    except OSError:
        raise WorkflowPublicationCatalogError("unavailable") from None
    if not stat.S_ISREG(metadata.st_mode) or is_reparse_point(metadata):
        raise WorkflowPublicationCatalogError("unavailable")
    try:
        path.unlink()
    except OSError:
        raise WorkflowPublicationCatalogError("unavailable") from None


def _validate_entries(
    values: list[Any], *, copy_contracts: bool = True
) -> list[dict[str, Any]]:
    """校验发布条目身份、发布序号和合同基本结构。

    参数：``values`` 是 JSON 中的发布条目数组。返回：按工作流与发布序号排序的新
    列表。``copy_contracts=False`` 仅用于本轮读取独占的合同，避免再次复制嵌套图。
    异常：数量超限、合同 UUID 重复、同一工作流发布序号或修订重复时抛输入错误。
    """

    if len(values) > PUBLICATION_LIMIT:
        raise WorkflowPublicationCatalogError("invalid_input")
    entries: list[dict[str, Any]] = []
    # ``contract_uuids`` 保证每个不可变合同身份只表示一份内容；
    # ``node_template_uuids`` 防止两个合同在恢复时占用同一个模板主键；
    # ``publication_sequences`` 保证同一工作流的公开发布序号不会指向两份合同；
    # ``workflow_revisions`` 对齐 SQLite 合同表的唯一约束，避免恶意清单在恢复
    # 第二条同修订合同时才以未分类 ``IntegrityError`` 失败。
    contract_uuids: set[str] = set()
    node_template_uuids: set[str] = set()
    publication_sequences: set[tuple[str, int]] = set()
    workflow_revisions: set[tuple[str, int]] = set()
    for value in values:
        if not isinstance(value, dict) or set(value) != {
            "source_draft_hash",
            "contract",
        }:
            raise WorkflowPublicationCatalogError("invalid_input")
        entry = _entry(
            value["source_draft_hash"], value["contract"], copy_contract=copy_contracts
        )
        contract = entry["contract"]
        version_key = (contract["workflow_uuid"], contract["version"])
        revision_key = (
            contract["workflow_uuid"],
            contract["workflow_revision"],
        )
        if (
            contract["uuid"] in contract_uuids
            or contract["node_template_uuid"] in node_template_uuids
            or version_key in publication_sequences
            or revision_key in workflow_revisions
        ):
            raise WorkflowPublicationCatalogError("invalid_input")
        contract_uuids.add(contract["uuid"])
        node_template_uuids.add(contract["node_template_uuid"])
        publication_sequences.add(version_key)
        workflow_revisions.add(revision_key)
        entries.append(entry)
    return sorted(
        entries,
        key=lambda item: (
            item["contract"]["workflow_uuid"],
            item["contract"]["version"],
        ),
    )


def _entry(
    source_draft_hash: Any, contract: Any, *, copy_contract: bool = True
) -> dict[str, Any]:
    """规范化一条源码哈希与完整发布合同。

    参数：``source_draft_hash`` 是发布时 Python 字节摘要，``contract`` 是含冻结
    图的完整私有合同。默认返回深拷贝；内部读取可用 ``copy_contract=False``
    复用独占的嵌套字段，仍执行全部校验且不修改原对象。异常：哈希、UUID、
    版本、字段集合或 JSON 值不合法时抛输入错误，原对象不会被修改。
    """

    if not isinstance(source_draft_hash, str):
        raise WorkflowPublicationCatalogError("invalid_input")
    digest = source_draft_hash.removeprefix("sha256:")
    if len(digest) != 64 or any(
        character not in "0123456789abcdef" for character in digest
    ):
        raise WorkflowPublicationCatalogError("invalid_input")
    # 即使旧清单使用无前缀摘要，也统一保存并比较带算法前缀的规范形式；源码
    # 工作区始终返回带前缀形式，若保留裸摘要会让相同发布在重启后误判为过期。
    source_draft_hash = "sha256:" + digest
    if not isinstance(contract, Mapping):
        raise WorkflowPublicationCatalogError("invalid_input")
    # Internal reads own the parsed graph already. Nested fields are validated,
    # never mutated here; external writes still receive a detached deep copy.
    copied = deepcopy(dict(contract)) if copy_contract else dict(contract)
    required = {
        "uuid",
        "create_time",
        "update_time",
        "meta_data",
        "workflow_uuid",
        "workflow_revision",
        "version",
        "name",
        "tags",
        "node_template_uuid",
        "input_contract",
        "output_contract",
        "executor_requirements",
        "executor_binding_mapping",
        "boundary_mapping",
        "graph_snapshot",
        "source_hash",
        "contract_digest",
        "node_count",
        "edge_count",
    }
    if not required.issubset(copied) or set(copied) - (required | {"description"}):
        raise WorkflowPublicationCatalogError("invalid_input")
    copied["uuid"] = _uuid(copied["uuid"])
    copied["workflow_uuid"] = _uuid(copied["workflow_uuid"])
    copied["node_template_uuid"] = _uuid(copied["node_template_uuid"])
    if (
        isinstance(copied["workflow_revision"], bool)
        or not isinstance(copied["workflow_revision"], int)
        or copied["workflow_revision"] < 1
        or copied["workflow_revision"] > _SQLITE_INT64_MAX
        or isinstance(copied["version"], bool)
        or not isinstance(copied["version"], int)
        or copied["version"] < 1
        or copied["version"] > _SQLITE_INT64_MAX
    ):
        raise WorkflowPublicationCatalogError("invalid_input")
    # 这两个值在恢复时写入 SQLite INTEGER 列；在清单边界校验具体整数类型和
    # 有符号 64 位范围，防止损坏 JSON 泄漏为 OverflowError 或延迟 CHECK 失败。
    for field, minimum in (("node_count", 1), ("edge_count", 0)):
        value = copied[field]
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or value < minimum
            or value > _SQLITE_INT64_MAX
        ):
            raise WorkflowPublicationCatalogError("invalid_input")
    try:
        json.dumps(copied, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError):
        raise WorkflowPublicationCatalogError("invalid_input") from None
    return {"source_draft_hash": source_draft_hash, "contract": copied}


def _uuid(value: Any) -> str:
    """规范化非零 UUID 字符串。

    参数：``value`` 是合同、工作流或模板身份。返回：小写连字符形式 UUID。
    异常：类型、格式或零 UUID 不合法时抛稳定输入错误。
    """

    if not isinstance(value, str):
        raise WorkflowPublicationCatalogError("invalid_input")
    try:
        # ``identity`` 是解析后的领域稳定身份；零 UUID 被保留为缺失哨兵，不能
        # 成为工作流、合同或节点模板的真实身份。
        identity = UUID(value)
    except (ValueError, TypeError, AttributeError):
        raise WorkflowPublicationCatalogError("invalid_input") from None
    if identity.int == 0:
        raise WorkflowPublicationCatalogError("invalid_input")
    return str(identity)


def _sha256(content: bytes) -> str:
    """计算与源码发布器一致的 SHA-256 CAS 令牌。

    参数：``content`` 是本轮稳定读取的完整发布目录字节。返回：带
    ``sha256:`` 前缀的小写摘要，供原子发布比较文件世代。异常：无。
    """

    return "sha256:" + hashlib.sha256(content).hexdigest()


__all__ = [
    "WorkflowPublicationCatalog",
    "WorkflowPublicationCatalogError",
]
