"""工作流源码（Workflow Source）工作区的失败关闭安全测试。"""

import os
from dataclasses import dataclass
from pathlib import Path

import pytest

from unilabos.workflow import source_workspace
from unilabos.workflow.service import WorkflowError, WorkflowService
from unilabos.workflow.source_discovery import (
    SourceDeclarationError,
    discover_editable_sources,
)
from unilabos.workflow.source_workspace import (
    MANIFEST_BYTE_LIMIT,
    SourceWorkspaceError,
)
from unilabos.workflow.store import WorkflowStore

SOURCE_BYTE_LIMIT = 8 * 1024 * 1024
WORKFLOW_UUID = "11111111-1111-4111-8111-111111111111"
FILE_ATTRIBUTE_REPARSE_POINT = 0x400


@dataclass(frozen=True)
class ReparseDirectoryStat:
    """提供 Windows 重解析点（reparse point）目录所需的最小元数据。"""

    st_mode: int
    st_dev: int
    st_ino: int
    st_file_attributes: int


def _write_package(selected_root: Path, *, create_source: bool = True) -> Path:
    """创建安全工作区测试所需的最小可编辑包（Editable Package）。

    参数：``selected_root`` 是显式授权目录；``create_source`` 控制是否创建源码。
    返回：声明指向的工作流源码（Workflow Source）路径。
    """

    package_root = selected_root / "demo_package"
    source_path = package_root / "workflows" / "demo.py"
    package_root.mkdir(parents=True)
    if create_source:
        source_path.parent.mkdir()
        source_path.write_text("@workflow\ndef demo():\n    pass\n", encoding="utf-8")
    selected_root.joinpath("package.yaml").write_text(
        "package:\n"
        "  name: demo_package\n"
        "workflows:\n"
        f"  - workflow_uuid: {WORKFLOW_UUID}\n"
        "    source: demo_package/workflows/demo.py\n",
        encoding="utf-8",
    )
    return source_path


@pytest.mark.parametrize("unsafe_kind", ("symlink", "directory", "fifo"))
def test_discovery_rejects_existing_source_that_is_not_regular_file(
    tmp_path: Path,
    unsafe_kind: str,
) -> None:
    """证明符号链接、目录和 FIFO 都不能冒充工作流源码。

    参数：``tmp_path`` 是隔离授权目录；``unsafe_kind`` 指定非普通文件类型。
    返回：无；测试断言发现过程快速失败并使用稳定错误码。
    """

    selected_root = tmp_path / "selected"
    source_path = _write_package(selected_root, create_source=False)
    source_path.parent.mkdir()
    if unsafe_kind == "symlink":
        outside = tmp_path / "outside.py"
        outside.write_text("secret = True\n", encoding="utf-8")
        source_path.symlink_to(outside)
    elif unsafe_kind == "directory":
        source_path.mkdir()
    else:
        os.mkfifo(source_path)

    with pytest.raises(SourceDeclarationError) as caught:
        discover_editable_sources((selected_root,))

    assert caught.value.code == "invalid_workflow_source"


def test_discovery_rejects_non_utf8_source(tmp_path: Path) -> None:
    """证明已存在的工作流源码必须是严格 UTF-8 文本。

    参数：``tmp_path`` 保存一份编码非法的隔离源码。
    返回：无；测试断言非法内容不进入发现计划。
    """

    selected_root = tmp_path / "selected"
    source_path = _write_package(selected_root)
    source_path.write_bytes(b"\xff\xfe")

    with pytest.raises(SourceDeclarationError) as caught:
        discover_editable_sources((selected_root,))

    assert caught.value.code == "invalid_workflow_source"


def test_discovery_enforces_manifest_and_source_byte_budgets(tmp_path: Path) -> None:
    """证明 manifest 与 Python 源码都在解析前受硬字节预算限制。

    参数：``tmp_path`` 隔离两个分别超过 manifest 与源码预算的包。
    返回：无；测试断言两类超限得到各自稳定错误分类。
    """

    manifest_root = tmp_path / "manifest-limit"
    _write_package(manifest_root)
    manifest_root.joinpath("package.yaml").write_bytes(b"x" * (MANIFEST_BYTE_LIMIT + 1))
    source_root = tmp_path / "source-limit"
    source_path = _write_package(source_root)
    source_path.write_bytes(b"x" * (SOURCE_BYTE_LIMIT + 1))

    observed_codes: list[str] = []
    for selected_root in (manifest_root, source_root):
        try:
            discover_editable_sources((selected_root,))
        except SourceDeclarationError as error:
            observed_codes.append(error.code)

    assert observed_codes == ["invalid_manifest", "invalid_workflow_source"]


@pytest.mark.parametrize("symlink_level", ("selected-root", "package-root"))
def test_discovery_rejects_symlink_directory_at_every_authorized_level(
    tmp_path: Path,
    symlink_level: str,
) -> None:
    """证明授权目录和实际 Python 包目录都不能由符号链接替代。

    参数：``tmp_path`` 是隔离目录；``symlink_level`` 指定被替换的目录层级。
    返回：无；测试断言目录身份错误失败关闭。
    """

    real_root = tmp_path / "real"
    _write_package(real_root)
    if symlink_level == "selected-root":
        selected_root = tmp_path / "linked"
        selected_root.symlink_to(real_root, target_is_directory=True)
    else:
        selected_root = real_root
        package_root = real_root / "demo_package"
        actual_root = real_root / "actual-package"
        package_root.rename(actual_root)
        package_root.symlink_to(actual_root, target_is_directory=True)

    with pytest.raises(SourceDeclarationError) as caught:
        discover_editable_sources((selected_root,))

    assert caught.value.code == "invalid_package_root"


def test_no_dir_fd_workspace_rejects_windows_reparse_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """无 ``dir_fd`` 路径必须拒绝 junction 等 Windows 重解析目录。

    参数：``tmp_path`` 提供真实可编辑包（Editable Package）；``monkeypatch``
    只为显式授权根附加重解析属性并切换到路径回退。返回：无；即使该目录不是普通
    符号链接，也必须以 ``invalid_package_root`` 失败关闭。
    """

    selected_root = tmp_path / "selected"
    _write_package(selected_root)
    original_lstat = Path.lstat

    def lstat_with_reparse_attribute(
        path: Path,
    ) -> os.stat_result | ReparseDirectoryStat:
        """返回原元数据，仅为授权根添加重解析点（reparse point）属性。"""

        metadata = original_lstat(path)
        if Path(path) != selected_root:
            return metadata
        return ReparseDirectoryStat(
            st_mode=metadata.st_mode,
            st_dev=metadata.st_dev,
            st_ino=metadata.st_ino,
            st_file_attributes=FILE_ATTRIBUTE_REPARSE_POINT,
        )

    monkeypatch.setattr(source_workspace, "_DIRECTORY_FD_PATHS_SUPPORTED", False)
    monkeypatch.setattr(Path, "lstat", lstat_with_reparse_attribute)

    with pytest.raises(SourceWorkspaceError) as caught:
        source_workspace.read_package_root(selected_root)

    assert caught.value.code == "invalid_package_root"


def test_discovery_rejects_selected_root_replaced_after_identity_check(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """证明授权目录在检查与打开之间被替换时不能混入发现计划。

    参数：``tmp_path`` 提供原目录和替换目录；``monkeypatch`` 注入竞态窗口。
    返回：无；测试断言文件描述符身份核验拒绝 TOCTOU 替换。
    """

    selected_root = tmp_path / "selected"
    replacement_root = tmp_path / "replacement"
    saved_root = tmp_path / "saved-selected"
    _write_package(selected_root)
    _write_package(replacement_root)
    original_contains_symlink = source_workspace._contains_symlink
    replaced = False

    def check_then_replace(path: Path) -> bool:
        """完成静态链接检查后原子替换目录，以模拟 TOCTOU 竞态。

        参数：``path`` 是被工作区验证的目录。
        返回：替换前的符号链接检查结果。
        """

        nonlocal replaced
        result = original_contains_symlink(path)
        if path == selected_root and not replaced:
            selected_root.rename(saved_root)
            replacement_root.rename(selected_root)
            replaced = True
        return result

    monkeypatch.setattr(source_workspace, "_contains_symlink", check_then_replace)

    with pytest.raises(SourceDeclarationError) as caught:
        discover_editable_sources((selected_root,))

    assert caught.value.code == "invalid_package_root"


def test_service_rejects_external_source_larger_than_eight_mib_without_event(
    tmp_path: Path,
) -> None:
    """证明外部超大源码不能进入工作流创作状态或产生事件。

    参数：``tmp_path`` 保存工作流数据库和外部超限源码。
    返回：无；测试断言持久创作记录和事件流保持不变。
    """

    store = WorkflowStore(tmp_path / "workflow.db")
    service = WorkflowService(store)
    package_root = tmp_path / "package"
    source_path = package_root / "workflows" / "demo.py"
    package_root.mkdir()
    service.create_workflow(
        name="源码预算合同",
        tags=[],
        description=None,
        meta_data={},
        workflow_uuid=WORKFLOW_UUID,
    )
    service.replace_active_editable_source_authorization(
        workflow_uuid=WORKFLOW_UUID,
        package_id="source_budget_contract",
        package_root=package_root,
        relative_path="workflows/demo.py",
    )
    record_before = store.get_authoring_record(WORKFLOW_UUID)
    source_path.parent.mkdir()
    source_path.write_bytes(b"x" * (SOURCE_BYTE_LIMIT + 1))

    try:
        with pytest.raises(WorkflowError) as caught:
            service.get_authoring(WORKFLOW_UUID)
        record_after = store.get_authoring_record(WORKFLOW_UUID)
        events_after = service.list_events(after_id=0)["items"]
    finally:
        service.close()

    assert caught.value.code == "invalid_input"
    assert record_after == record_before
    assert events_after == []


def test_save_draft_rejects_oversized_source_before_replacing_file(
    tmp_path: Path,
) -> None:
    """证明保存超大草稿在 CAS 写入前失败，规范源码文件保持不变。

    参数：``tmp_path`` 保存工作流数据库和原始规范源码。
    返回：无；测试断言错误发生在物理替换以前。
    """

    store = WorkflowStore(tmp_path / "workflow.db")
    service = WorkflowService(store)
    package_root = tmp_path / "package"
    source_path = package_root / "workflows" / "demo.py"
    source_path.parent.mkdir(parents=True)
    original_source = "@workflow\ndef demo():\n    pass\n"
    source_path.write_text(original_source, encoding="utf-8")
    service.create_workflow(
        name="保存预算合同",
        tags=[],
        description=None,
        meta_data={},
        workflow_uuid=WORKFLOW_UUID,
    )
    service.replace_active_editable_source_authorization(
        workflow_uuid=WORKFLOW_UUID,
        package_id="save_budget_contract",
        package_root=package_root,
        relative_path="workflows/demo.py",
    )
    baseline = service.get_authoring(WORKFLOW_UUID)

    try:
        with pytest.raises(WorkflowError) as caught:
            service.save_draft(
                WORKFLOW_UUID,
                python_source="x" * (SOURCE_BYTE_LIMIT + 1),
                expected_draft_hash=baseline["draft"]["draft_hash"],
                expected_workflow_revision=1,
            )
        persisted_source = source_path.read_text(encoding="utf-8")
    finally:
        service.close()

    assert caught.value.code == "invalid_input"
    assert persisted_source == original_source


def test_service_rejects_package_root_replaced_during_registration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """证明来源注册检查期间被替换的普通目录不能成为新权威路径。

    参数：``tmp_path`` 保存原包目录、替换目录和工作流数据库；``monkeypatch``
    在静态符号链接检查后注入 TOCTOU 目录替换。
    返回：无；测试断言注册失败且数据库中不存在部分来源身份。
    """

    store = WorkflowStore(tmp_path / "workflow.db")
    service = WorkflowService(store)
    package_root = tmp_path / "package"
    replacement_root = tmp_path / "replacement"
    saved_root = tmp_path / "saved-package"
    package_root.mkdir()
    replacement_root.mkdir()
    service.create_workflow(
        name="目录身份合同",
        tags=[],
        description=None,
        meta_data={},
        workflow_uuid=WORKFLOW_UUID,
    )
    original_contains_symlink = source_workspace._contains_symlink
    replaced = False

    def check_then_replace(path: Path) -> bool:
        """静态检查后替换包目录以模拟注册期 TOCTOU。

        参数：``path`` 是源码工作区正在验证的实际包目录。
        返回：替换前的符号链接检查结果。
        """

        nonlocal replaced
        result = original_contains_symlink(path)
        if Path(os.path.abspath(path)) == package_root and not replaced:
            package_root.rename(saved_root)
            replacement_root.rename(package_root)
            replaced = True
        return result

    monkeypatch.setattr(source_workspace, "_contains_symlink", check_then_replace)

    try:
        with pytest.raises(WorkflowError) as caught:
            service.replace_active_editable_source_authorization(
                workflow_uuid=WORKFLOW_UUID,
                package_id="root_identity_contract",
                package_root=package_root,
                relative_path="workflows/demo.py",
            )
        registrations = service.list_registered_sources()
    finally:
        service.close()

    assert caught.value.code == "invalid_input"
    assert registrations == []


def test_service_rejects_a_source_that_changes_during_same_descriptor_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """同一描述符读取期间变化的源码不得作为一致草稿返回。

    参数：``tmp_path`` 隔离数据库和源码；``monkeypatch`` 在首次读取字节后用
    同长度新内容改写规范文件。返回：无；读取关闭失败且创作记录、事件保持不变。
    """

    store = WorkflowStore(tmp_path / "workflow.db")
    service = WorkflowService(store)
    package_root = tmp_path / "package"
    source_path = package_root / "workflows" / "demo.py"
    source_path.parent.mkdir(parents=True)
    source_path.write_text("value = 'old'\n", encoding="utf-8")
    service.create_workflow(
        name="撕裂读取合同",
        tags=[],
        description=None,
        meta_data={},
        workflow_uuid=WORKFLOW_UUID,
    )
    service.replace_active_editable_source_authorization(
        workflow_uuid=WORKFLOW_UUID,
        package_id="stable_read_contract",
        package_root=package_root,
        relative_path="workflows/demo.py",
    )
    record_before = store.get_authoring_record(WORKFLOW_UUID)
    original_read = os.read
    target_identity = (source_path.stat().st_dev, source_path.stat().st_ino)
    source_changed = False

    def change_after_first_read(descriptor: int, length: int) -> bytes:
        """返回首段旧字节后改写同一规范路径，制造真实撕裂读取窗口。

        参数：``descriptor`` 和 ``length`` 与 ``os.read`` 一致。返回：宿主读取的
        字节；目标文件只在首次非空读取后改写一次。
        """

        nonlocal source_changed
        chunk = original_read(descriptor, length)
        metadata = os.fstat(descriptor)
        if (
            not source_changed
            and chunk
            and (metadata.st_dev, metadata.st_ino) == target_identity
        ):
            source_path.write_text("value = 'new'\n", encoding="utf-8")
            source_changed = True
        return chunk

    monkeypatch.setattr(source_workspace.os, "read", change_after_first_read)
    try:
        with pytest.raises(WorkflowError) as caught:
            service.get_authoring(WORKFLOW_UUID)
        record_after = store.get_authoring_record(WORKFLOW_UUID)
        events_after = service.list_events(after_id=0)["items"]
    finally:
        service.close()

    assert source_changed
    assert caught.value.code == "invalid_input"
    assert record_after == record_before
    assert events_after == []
