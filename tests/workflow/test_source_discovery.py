"""工作流源码（Workflow Source）显式发现的公开合同测试。"""

from pathlib import Path

import pytest

from unilabos.workflow.source_discovery import (
    SourceDeclarationError,
    discover_editable_sources,
)

WORKFLOW_UUID = "11111111-1111-4111-8111-111111111111"


def _write_editable_package(
    selected_root: Path,
    *,
    package_id: str = "demo_package",
    workflow_uuid: str = WORKFLOW_UUID,
    entries: tuple[tuple[str, str], ...] | None = None,
    create_sources: bool = True,
) -> Path:
    """创建一个最小可编辑包（Editable Package）。

    参数：
    - ``selected_root``：启动配置显式授权、包含 ``package.yaml`` 的目录。
    - ``package_id``：声明中的稳定包身份，同时也是 Python 包目录名。
    - ``workflow_uuid``：已有工作流（Workflow）的稳定身份。
    - ``entries``：可选的工作流 UUID 与完整包相对源码路径集合。
    - ``create_sources``：是否创建声明指向的普通 UTF-8 Python 文件。

    返回：实际保存 Python 工作流源码（Workflow Source）的文件路径。
    """

    declared_entries = entries or ((workflow_uuid, f"{package_id}/workflows/demo.py"),)
    package_root = selected_root / package_id
    package_root.mkdir(parents=True)
    if create_sources:
        for _declared_workflow_uuid, declared_source in declared_entries:
            source_parts = Path(declared_source).parts
            if len(source_parts) == 3 and source_parts[0] == package_id:
                source_path = selected_root.joinpath(*source_parts)
                source_path.parent.mkdir(parents=True, exist_ok=True)
                source_path.write_text(
                    "@workflow\ndef demo():\n    return None\n",
                    encoding="utf-8",
                )
    default_source = package_root / "workflows" / "demo.py"
    workflow_lines: list[str] = []
    for declared_workflow_uuid, declared_source in declared_entries:
        # 每项声明同时冻结工作流身份与规范包相对源码身份。
        workflow_lines.extend(
            (
                f"  - workflow_uuid: {declared_workflow_uuid}",
                f"    source: {declared_source}",
            )
        )
    selected_root.joinpath("package.yaml").write_text(
        "\n".join(
            (
                "package:",
                f"  name: {package_id}",
                "workflows:",
                *workflow_lines,
                "",
            )
        ),
        encoding="utf-8",
    )
    return default_source


def test_discovery_returns_only_sources_from_explicit_authorized_roots(
    tmp_path: Path,
) -> None:
    """证明源码发现只读取显式授权目录，并返回规范稳定身份。

    参数：``tmp_path`` 隔离已授权目录和应忽略的相邻目录。
    返回：无；测试断言发现结果不含未授权包。
    """

    selected_root = tmp_path / "selected"
    ignored_root = tmp_path / "ignored"
    selected_source = _write_editable_package(selected_root)
    _write_editable_package(
        ignored_root,
        package_id="ignored_package",
        workflow_uuid="22222222-2222-4222-8222-222222222222",
    )

    # 发现计划是本轮公开结果；它只承载显式声明，不获得应用工作流的权威。
    plan = discover_editable_sources((selected_root,))

    assert len(plan.registrations) == 1
    registration = plan.registrations[0]
    assert (
        registration.workflow_uuid,
        registration.package_id,
        registration.package_root,
        registration.relative_path,
        registration.source_uri,
        selected_source.is_file(),
    ) == (
        WORKFLOW_UUID,
        "demo_package",
        selected_root / "demo_package",
        "workflows/demo.py",
        "package://demo_package/workflows/demo.py",
        True,
    )


@pytest.mark.parametrize(
    "declared_source",
    (
        "/tmp/demo.py",
        "../demo_package/workflows/demo.py",
        "demo_package/../workflows/demo.py",
        r"demo_package\workflows\demo.py",
        "other_package/workflows/demo.py",
        "demo_package/workflows/nested/demo.py",
        "demo_package/demo.py",
        "demo_package/workflows/demo.txt",
    ),
)
def test_discovery_rejects_paths_outside_exact_package_workflow_shape(
    tmp_path: Path,
    declared_source: str,
) -> None:
    """证明源码路径只能是当前包下的一层 ``workflows/*.py``。

    参数：``tmp_path`` 是隔离授权目录；``declared_source`` 是待拒绝路径。
    返回：无；测试断言路径错误稳定归类为工作流源码声明错误。
    """

    selected_root = tmp_path / "selected"
    _write_editable_package(
        selected_root,
        entries=((WORKFLOW_UUID, declared_source),),
        create_sources=False,
    )

    with pytest.raises(SourceDeclarationError) as caught:
        discover_editable_sources((selected_root,))

    assert caught.value.code == "invalid_workflow_source"


@pytest.mark.parametrize(
    "entries",
    (
        (
            (WORKFLOW_UUID, "demo_package/workflows/first.py"),
            (WORKFLOW_UUID, "demo_package/workflows/second.py"),
        ),
        (
            (WORKFLOW_UUID, "demo_package/workflows/demo.py"),
            (
                "22222222-2222-4222-8222-222222222222",
                "demo_package/workflows/demo.py",
            ),
        ),
    ),
)
def test_discovery_rejects_duplicate_workflow_or_source_identity(
    tmp_path: Path,
    entries: tuple[tuple[str, str], ...],
) -> None:
    """证明同一发现计划中的工作流身份和物理源码身份都必须唯一。

    参数：``tmp_path`` 是隔离授权目录；``entries`` 含一类重复身份。
    返回：无；测试断言重复身份不会形成部分发现计划。
    """

    selected_root = tmp_path / "selected"
    _write_editable_package(
        selected_root,
        entries=entries,
        create_sources=False,
    )

    with pytest.raises(SourceDeclarationError) as caught:
        discover_editable_sources((selected_root,))

    assert caught.value.code == "duplicate_workflow_source"


def test_discovery_allows_missing_declared_source_without_creating_it(
    tmp_path: Path,
) -> None:
    """证明缺失源码是合法草稿状态，发现过程绝不替用户创建文件。

    参数：``tmp_path`` 提供只含声明、不含 Python 文件的授权目录。
    返回：无；测试断言来源身份存在而物理文件仍缺失。
    """

    selected_root = tmp_path / "selected"
    source_path = _write_editable_package(selected_root, create_sources=False)

    plan = discover_editable_sources((selected_root,))

    assert len(plan.registrations) == 1
    assert source_path.exists() is False


def test_discovery_rejects_duplicate_workflow_identity_across_explicit_packages(
    tmp_path: Path,
) -> None:
    """证明多个显式包不能共同声明同一个工作流（Workflow）身份。

    参数：``tmp_path`` 隔离两个分别有效但身份冲突的显式包。
    返回：无；测试断言完整发现计划失败关闭。
    """

    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    _write_editable_package(first_root, package_id="first_package")
    _write_editable_package(second_root, package_id="second_package")

    with pytest.raises(SourceDeclarationError) as caught:
        discover_editable_sources((first_root, second_root))

    assert caught.value.code == "duplicate_workflow_source"
