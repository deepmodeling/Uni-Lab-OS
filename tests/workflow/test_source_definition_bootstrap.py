"""可编辑包（Editable Package）首次安装工作流定义的原子合同测试。"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from unilabos.workflow.store import StoreConflict, StoreNotFound, WorkflowStore

WORKFLOW_A_UUID = "11111111-1111-4111-8111-111111111111"
WORKFLOW_B_UUID = "22222222-2222-4222-8222-222222222222"


def _registration(
    *,
    workflow_uuid: str = WORKFLOW_A_UUID,
    package_id: str = "alpha_lab",
    package_root: str = "/workspace/alpha_lab",
    relative_path: str = "workflows/demo.py",
    source_uri: str | None = None,
) -> dict[str, str]:
    """构造一项已通过源码发现（Source Discovery）校验的注册行。

    参数：``workflow_uuid`` 是清单声明的工作流（Workflow）身份；包身份、包目录、
    相对路径与来源 URI 共同形成工作流源码（Workflow Source）身份。
    返回：可交给存储安装接口的独立字典；本函数不读写文件或数据库。
    """

    # ``resolved_source_uri`` 是跨重启稳定的工作流源码（Workflow Source）寻址身份。
    resolved_source_uri = source_uri or f"package://{package_id}/{relative_path}"
    return {
        "workflow_uuid": workflow_uuid,
        "package_id": package_id,
        "package_root": package_root,
        "relative_path": relative_path,
        "source_uri": resolved_source_uri,
    }


def _assert_absent(
    store: WorkflowStore,
    workflow_uuid: str,
) -> None:
    """断言一个身份没有留下定义、来源或空创作事实。

    参数：``store`` 是本用例唯一工作流写模型（Workflow Write Model）；
    ``workflow_uuid`` 是应完全回滚的工作流（Workflow）身份。返回：无；任一事实
    残留都会触发断言失败。
    """

    with pytest.raises(StoreNotFound):
        store.get_workflow(workflow_uuid)
    with pytest.raises(StoreNotFound):
        store.get_source_registration(workflow_uuid)
    # ``authoring_record`` 的空时间表示没有持久创作行，而不是已有空候选。
    authoring_record = store.get_authoring_record(workflow_uuid)
    assert authoring_record["update_time"] is None


@pytest.fixture()
def workflow_store(tmp_path: Path) -> Iterator[WorkflowStore]:
    """创建隔离的真实 SQLite 工作流写模型。

    参数：``tmp_path`` 是 pytest 提供的独立目录。返回：初始化完成的
    ``WorkflowStore``；用例结束后关闭连接。
    """

    # ``store`` 是单个测试内唯一允许提交工作流（Workflow）事实的工作流权威
    # （Workflow Authority）。
    store = WorkflowStore(tmp_path / "workflow_history.db")
    try:
        yield store
    finally:
        store.close()


def test_missing_definition_is_created_with_stable_manifest_provenance(
    workflow_store: WorkflowStore,
) -> None:
    """缺失身份应创建最小工作流骨架、来源注册与空创作事实。

    参数：``workflow_store`` 是真实 SQLite 工作流权威（Workflow Authority）。
    返回：无；测试冻结首次名称、修订和清单来源坐标，且不触发源码执行或应用。
    """

    # ``registration`` 是显式授权清单产生的唯一可信身份输入。
    registration = _registration()

    installed = workflow_store.install_discovered_sources((registration,))

    # ``workflow`` 是首次安装生成的后端形态（Backend-shaped）工作流（Workflow）骨架。
    workflow = workflow_store.get_workflow(WORKFLOW_A_UUID)
    assert installed == [workflow_store.get_source_registration(WORKFLOW_A_UUID)]
    assert workflow == {
        "uuid": WORKFLOW_A_UUID,
        "create_time": workflow["create_time"],
        "update_time": workflow["update_time"],
        "meta_data": {
            "unilab": {
                "source_bootstrap": {
                    "kind": "editable_package_manifest",
                    "package_id": "alpha_lab",
                    "relative_path": "workflows/demo.py",
                    "source_uri": "package://alpha_lab/workflows/demo.py",
                }
            }
        },
        "name": "alpha_lab.demo",
        "tags": [],
        "revision": 1,
    }
    # ``authoring_record`` 证明同一事务已经创建空创作事实，但没有编译候选。
    authoring_record = workflow_store.get_authoring_record(WORKFLOW_A_UUID)
    assert authoring_record["diagnostics"] == []
    assert authoring_record["candidate"] is None
    assert authoring_record["update_time"] is not None


def test_active_definition_is_reused_without_overwriting_user_fields_on_restart(
    tmp_path: Path,
) -> None:
    """活动工作流定义在重复安装和重启后保持用户字段与修订不变。

    参数：``tmp_path`` 保存可关闭后重开的 SQLite 文件。返回：无；测试证明首次来源
    安装只补来源与创作事实，不覆盖名称、描述、元数据、标签或修订。
    """

    database_path = tmp_path / "workflow_history.db"
    # ``registration`` 是两次进程生命周期都使用的同一工作流源码（Workflow Source）。
    registration = _registration()
    first = WorkflowStore(database_path)
    first.create_workflow(
        workflow_uuid=WORKFLOW_A_UUID,
        name="用户命名的实验",
        tags=["用户标签"],
        description="用户说明",
        meta_data={"owner": "operator"},
    )
    first.save_graph(WORKFLOW_A_UUID, revision=1, nodes=[], edges=[])
    before = first.get_workflow(WORKFLOW_A_UUID)
    first.install_discovered_sources((registration,))
    first.close()

    reopened = WorkflowStore(database_path)
    try:
        reopened.install_discovered_sources((registration,))
        after = reopened.get_workflow(WORKFLOW_A_UUID)
    finally:
        reopened.close()

    assert after == before
    assert after["revision"] == 2


def test_bootstrapped_definition_replays_without_changing_any_persisted_fact(
    tmp_path: Path,
) -> None:
    """自动首装后重开同一数据库应逐字段复用全部持久事实。

    参数：``tmp_path`` 保存首次自动首装后关闭并重开的 ``workflow_history.db``。
    返回：无；断言最小工作流定义（Workflow Definition）、工作流源码（Workflow
    Source）注册、空工作流创作（Authoring）事实及其创建/更新时间戳均不变化。
    异常：数据库打开、首装或重放失败时原样传播，使测试不能跳过持久生命周期。
    """

    database_path = tmp_path / "workflow_history.db"
    # ``registration`` 是两个进程生命周期重放的同一显式源码发现计划事实。
    registration = _registration()
    first = WorkflowStore(database_path)
    first.install_discovered_sources((registration,))
    # 三份 ``before`` 快照共同形成首装后不得被幂等重放改写的持久基线。
    before_workflow = first.get_workflow(WORKFLOW_A_UUID)
    before_source = first.get_source_registration(WORKFLOW_A_UUID)
    before_authoring = first.get_authoring_record(WORKFLOW_A_UUID)
    first.close()

    reopened = WorkflowStore(database_path)
    try:
        reopened.install_discovered_sources((registration,))
        after_workflow = reopened.get_workflow(WORKFLOW_A_UUID)
        after_source = reopened.get_source_registration(WORKFLOW_A_UUID)
        after_authoring = reopened.get_authoring_record(WORKFLOW_A_UUID)
    finally:
        reopened.close()

    assert after_workflow == before_workflow
    assert after_source == before_source
    assert after_authoring == before_authoring
    assert (after_workflow["create_time"], after_workflow["update_time"]) == (
        before_workflow["create_time"],
        before_workflow["update_time"],
    )
    assert (after_source["create_time"], after_source["update_time"]) == (
        before_source["create_time"],
        before_source["update_time"],
    )
    assert after_authoring["update_time"] == before_authoring["update_time"]


@pytest.mark.parametrize(
    "package_root",
    (
        r"C:\workspace\alpha_lab",
        r"\\server\share\alpha_lab",
    ),
    ids=("windows-drive", "windows-unc"),
)
def test_canonical_windows_package_roots_install_and_replay_verbatim(
    tmp_path: Path,
    package_root: str,
) -> None:
    """规范 Windows 包根应安装、原值持久化并在重启后幂等重放。

    参数：``tmp_path`` 提供真实 SQLite 文件；``package_root`` 分别覆盖驱动器与
    UNC 绝对路径。返回：无；两次安装必须复用同一来源身份且不改写路径字符串。
    """

    database_path = tmp_path / "workflow_history.db"
    registration = _registration(package_root=package_root)
    first = WorkflowStore(database_path)
    try:
        first.install_discovered_sources((registration,))
        before = first.get_source_registration(WORKFLOW_A_UUID)
    finally:
        first.close()

    reopened = WorkflowStore(database_path)
    try:
        installed = reopened.install_discovered_sources((registration,))
        after = reopened.get_source_registration(WORKFLOW_A_UUID)
    finally:
        reopened.close()

    assert before["package_root"] == package_root
    assert after == before
    assert installed == [after]


@pytest.mark.parametrize(
    "package_root",
    (
        r"C:workspace\alpha_lab",
        r"C:\workspace\alpha_lab\..\other",
        r"C:\workspace/alpha_lab",
        r"\workspace\alpha_lab",
        "C:\\",
        r"\\server\share",
        "C:\\workspace\\alpha_lab\\",
        r"\\?\C:\workspace\alpha_lab",
        r"\\.\C:\workspace\alpha_lab",
        r"C:\workspace\alpha_lab:stream",
        "C:\\workspace\\alpha_lab.",
    ),
    ids=(
        "drive-relative",
        "parent-traversal",
        "mixed-separators",
        "root-without-drive",
        "drive-root",
        "unc-share-root",
        "trailing-separator-alias",
        "extended-device-namespace",
        "device-namespace",
        "alternate-data-stream",
        "trailing-dot-alias",
    ),
)
def test_noncanonical_windows_package_roots_are_rejected_without_writes(
    workflow_store: WorkflowStore,
    package_root: str,
) -> None:
    """Windows 相对路径、穿越、根目录与需改写别名必须关闭式拒绝。"""

    with pytest.raises(StoreConflict):
        workflow_store.install_discovered_sources(
            (_registration(package_root=package_root),)
        )

    _assert_absent(workflow_store, WORKFLOW_A_UUID)


def test_discovered_install_rejects_all_c0_and_del_path_characters_without_writes(
    workflow_store: WorkflowStore,
) -> None:
    """包根与源码路径中的全部 C0/DEL 控制字符必须关闭式拒绝。

    参数：``workflow_store`` 是隔离的真实 SQLite 工作流权威（Workflow
    Authority）。返回：无；逐项覆盖 U+0000..U+001F 与 U+007F，包含换行、制表和
    DEL，并在每次失败后证明定义、来源和空创作事实均为零部分写入。异常：任一
    控制字符被接受时断言失败，定位到对应字段和码点。
    """

    # ``control_characters`` 是词法身份必须拒绝的完整 C0 与 DEL 闭集。
    control_characters = tuple(chr(code) for code in (*range(0x20), 0x7F))
    # ``invalid_fields`` 是必须使用同一控制字符拒绝规则的两个持久路径字段。
    invalid_fields = ("package_root", "relative_path")
    for invalid_field in invalid_fields:
        for control_character in control_characters:
            # ``registration`` 是只在一个路径字段含当前控制码的不可信注册行。
            registration = _registration()
            registration[invalid_field] = (
                f"/workspace/alpha{control_character}lab"
                if invalid_field == "package_root"
                else f"workflows/de{control_character}mo.py"
            )
            registration["source_uri"] = (
                f"package://{registration['package_id']}/"
                f"{registration['relative_path']}"
            )

            with pytest.raises(StoreConflict):
                workflow_store.install_discovered_sources((registration,))
            _assert_absent(workflow_store, WORKFLOW_A_UUID)


def test_soft_deleted_definition_blocks_whole_batch_without_resurrection(
    workflow_store: WorkflowStore,
) -> None:
    """软删除工作流定义必须关闭式拒绝整批安装且不得复活。

    参数：``workflow_store`` 保存一个已软删除（Soft Deletion）身份。返回：无；
    测试把缺失身份放在被删除身份之前，证明分类完成后才写入并且整批零部分提交。
    """

    workflow_store.create_workflow(
        workflow_uuid=WORKFLOW_B_UUID,
        name="已删除定义",
        tags=[],
        description=None,
        meta_data={},
    )
    workflow_store.delete_workflow(WORKFLOW_B_UUID)
    # ``registrations`` 的首项本可创建，后项软删除必须使两项一起失败。
    registrations = (
        _registration(),
        _registration(
            workflow_uuid=WORKFLOW_B_UUID,
            relative_path="workflows/deleted.py",
        ),
    )

    with pytest.raises(StoreConflict):
        workflow_store.install_discovered_sources(registrations)

    _assert_absent(workflow_store, WORKFLOW_A_UUID)
    with pytest.raises(StoreNotFound):
        workflow_store.get_workflow(WORKFLOW_B_UUID)
    with pytest.raises(StoreNotFound):
        workflow_store.get_source_registration(WORKFLOW_B_UUID)


def test_batch_uuid_path_uri_and_package_root_conflicts_leave_no_facts(
    tmp_path: Path,
) -> None:
    """批内四类来源身份冲突必须在任何工作流骨架写入前失败。

    参数：``tmp_path`` 为四种冲突各提供独立 SQLite。返回：无；测试覆盖工作流 UUID、
    物理路径、来源 URI 与包目录身份冲突，并逐库验证零部分事实。
    """

    # ``conflicting_batches`` 逐项表达一种独立的来源身份冲突，不依赖数据库约束顺序。
    conflicting_batches = {
        "workflow_uuid": (
            _registration(),
            _registration(relative_path="workflows/other.py"),
        ),
        "physical_path": (
            _registration(),
            _registration(
                workflow_uuid=WORKFLOW_B_UUID,
                package_id="beta_lab",
            ),
        ),
        "source_uri": (
            _registration(),
            _registration(
                workflow_uuid=WORKFLOW_B_UUID,
                package_root="/workspace/other-alpha",
            ),
        ),
        "package_root": (
            _registration(),
            _registration(
                workflow_uuid=WORKFLOW_B_UUID,
                package_root="/workspace/other-alpha",
                relative_path="workflows/other.py",
            ),
        ),
    }
    for conflict_name, registrations in conflicting_batches.items():
        # ``store`` 隔离当前冲突，确保上一种失败不会影响下一种断言。
        store = WorkflowStore(tmp_path / f"{conflict_name}.db")
        try:
            with pytest.raises(StoreConflict):
                store.install_discovered_sources(registrations)
            _assert_absent(store, WORKFLOW_A_UUID)
            _assert_absent(store, WORKFLOW_B_UUID)
        finally:
            store.close()


def test_late_sql_failure_rolls_back_earlier_definition_source_and_authoring(
    workflow_store: WorkflowStore,
) -> None:
    """后项 SQLite 写入失败时不得留下前项工作流骨架或来源。

    参数：``workflow_store`` 是真实 SQLite 工作流权威（Workflow Authority）。
    返回：无；测试让第二项来源插入由数据库适配器（Adapter）拒绝，并断言此前
    写入的定义、来源和工作流创作（Authoring）事实全部回滚。
    """

    # ``failure_injector`` 只安装测试触发器，不参与被测事务或读取领域事实。
    failure_injector = sqlite3.connect(workflow_store.path)
    failure_injector.execute(
        """
        CREATE TRIGGER reject_second_source_registration
        BEFORE INSERT ON workflow_source_registration
        WHEN NEW.workflow_uuid = '22222222-2222-4222-8222-222222222222'
        BEGIN
            SELECT RAISE(ABORT, '注入后项来源写入失败');
        END
        """
    )
    failure_injector.commit()
    failure_injector.close()
    # ``registrations`` 使 A 的定义与来源先写入，再在 B 的来源插入处失败。
    registrations = (
        _registration(),
        _registration(
            workflow_uuid=WORKFLOW_B_UUID,
            relative_path="workflows/b.py",
        ),
    )

    with pytest.raises(StoreConflict):
        workflow_store.install_discovered_sources(registrations)

    _assert_absent(workflow_store, WORKFLOW_A_UUID)
    _assert_absent(workflow_store, WORKFLOW_B_UUID)


def test_before_commit_failure_rolls_back_definition_source_and_authoring(
    workflow_store: WorkflowStore,
) -> None:
    """提交前固定目录复核失败必须回滚同事务中的全部新事实。

    参数：``workflow_store`` 是真实 SQLite 工作流权威（Workflow Authority）。
    返回：无；注入的回调模拟固定包根目录发生变化，原异常向上传播且定义、来源与
    工作流创作（Authoring）事实均不可见。
    """

    def reject_changed_root() -> None:
        """模拟提交前发现可编辑包根目录身份已变化。

        参数：无。返回：无；始终抛出 ``RuntimeError`` 以证明事务回滚。
        """

        raise RuntimeError("固定包目录身份已变化")

    # ``before_commit`` 是 SQL 全部完成后、真正提交前的最后安全复核点。
    before_commit: Callable[[], None] = reject_changed_root
    with pytest.raises(RuntimeError, match="固定包目录身份已变化"):
        workflow_store.install_discovered_sources(
            (_registration(),),
            before_commit=before_commit,
        )

    _assert_absent(workflow_store, WORKFLOW_A_UUID)


def test_legacy_registration_cannot_create_missing_workflow_definition(
    workflow_store: WorkflowStore,
) -> None:
    """旧兼容注册入口不得把合法清单行升级为缺失工作流定义。

    参数：``workflow_store`` 是真实 SQLite 工作流权威（Workflow Authority）。
    返回：无；测试证明只有显式发现安装入口能够创建缺失工作流（Workflow）骨架，
    旧 ``register_sources`` 必须关闭式失败（Fail-closed）且零写入。
    """

    # ``registration`` 具有完整、规范的清单身份，但其工作流定义尚不存在。
    registration = _registration()

    with pytest.raises(StoreConflict):
        workflow_store.register_sources((registration,))

    _assert_absent(workflow_store, WORKFLOW_A_UUID)


@pytest.mark.parametrize(
    ("invalid_field", "invalid_value"),
    (
        ("workflow_uuid", "not-a-uuid"),
        ("package_root", "relative/package"),
        ("relative_path", "../outside.py"),
    ),
    ids=("invalid-workflow-uuid", "relative-package-root", "escaping-source-path"),
)
def test_legacy_registration_rejects_untrusted_identity_fields_without_writes(
    workflow_store: WorkflowStore,
    invalid_field: str,
    invalid_value: str,
) -> None:
    """旧兼容入口必须完整验证 UUID、绝对包根与受限源码路径。

    参数：``workflow_store`` 是真实工作流权威（Workflow Authority）；
    ``invalid_field`` 和 ``invalid_value`` 指定一项不可信注册字段。返回：无；测试
    证明活动定义路径也不能绕过验证，失败后定义、来源和工作流创作（Authoring）
    事实保持调用前状态。
    """

    # ``registration`` 先形成完整行，再替换一项不可信字段并同步来源 URI。
    registration = _registration()
    registration[invalid_field] = invalid_value
    registration["source_uri"] = (
        f"package://{registration['package_id']}/{registration['relative_path']}"
    )
    if invalid_field == "workflow_uuid":
        # 非 UUID 没有可复用定义，旧入口也不得把它创建成数据库主键。
        expected_workflow_count = 0
        existing_workflow = None
    else:
        workflow_store.create_workflow(
            workflow_uuid=WORKFLOW_A_UUID,
            name="已有活动定义",
            tags=["人工维护"],
            description="不得被兼容注册修改",
            meta_data={"owner": "operator"},
        )
        expected_workflow_count = 1
        # ``existing_workflow`` 是失败前必须逐字段保持的活动工作流（Workflow）事实。
        existing_workflow = workflow_store.get_workflow(WORKFLOW_A_UUID)

    with pytest.raises(StoreConflict):
        workflow_store.register_sources((registration,))

    assert workflow_store.list_workflows(page=1, page_size=100)["total"] == (
        expected_workflow_count
    )
    assert workflow_store.list_source_registrations() == []
    assert (
        workflow_store.get_authoring_record(registration["workflow_uuid"])[
            "update_time"
        ]
        is None
    )
    if existing_workflow is not None:
        assert workflow_store.get_workflow(WORKFLOW_A_UUID) == existing_workflow


def test_legacy_registration_still_supports_existing_active_workflow(
    workflow_store: WorkflowStore,
) -> None:
    """旧兼容入口可为已存在活动工作流定义注册规范来源。

    参数：``workflow_store`` 是真实 SQLite 工作流权威（Workflow Authority）。
    返回：无；测试冻结兼容范围只包括活动工作流（Workflow），且不修改定义字段。
    """

    workflow_store.create_workflow(
        workflow_uuid=WORKFLOW_A_UUID,
        name="已有活动定义",
        tags=["人工维护"],
        description="兼容注册保留",
        meta_data={"owner": "operator"},
    )
    # ``before`` 是兼容注册前的活动工作流（Workflow）权威事实。
    before = workflow_store.get_workflow(WORKFLOW_A_UUID)

    registered = workflow_store.register_sources((_registration(),))

    assert registered == [workflow_store.get_source_registration(WORKFLOW_A_UUID)]
    assert workflow_store.get_workflow(WORKFLOW_A_UUID) == before
    assert workflow_store.get_authoring_record(WORKFLOW_A_UUID)["update_time"]
