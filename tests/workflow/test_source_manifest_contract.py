"""可编辑包（Editable Package）manifest 的封闭安全合同。"""

from pathlib import Path

import pytest

from unilabos.workflow.source_discovery import (
    SourceDeclarationError,
    discover_editable_sources,
)


@pytest.mark.parametrize(
    "manifest_text",
    (
        (
            "package: {name: demo}\n"
            "workflows:\n"
            "  - workflow_uuid: 11111111-1111-4111-8111-111111111111\n"
            "    source: demo/workflows/demo.py\n"
            "unexpected: secret-value\n"
        ),
        (
            "package: {name: demo}\n"
            "workflows:\n"
            "  - workflow_uuid: 11111111-1111-4111-8111-111111111111\n"
            "    source: &source demo/workflows/demo.py\n"
            "  - workflow_uuid: 22222222-2222-4222-8222-222222222222\n"
            "    source: *source\n"
        ),
        (
            "package: {name: !!str demo}\n"
            "workflows:\n"
            "  - workflow_uuid: 11111111-1111-4111-8111-111111111111\n"
            "    source: demo/workflows/demo.py\n"
        ),
        (
            "package: {name: demo}\n"
            "workflows:\n"
            "  - workflow_uuid: 11111111-1111-4111-8111-111111111111\n"
            "    source: demo/workflows/demo.py\n"
            "---\n"
            "package: {name: other}\n"
        ),
        (
            "package: {name: demo}\n"
            "package: {name: shadow}\n"
            "workflows:\n"
            "  - workflow_uuid: 11111111-1111-4111-8111-111111111111\n"
            "    source: shadow/workflows/demo.py\n"
        ),
    ),
)
def test_discovery_rejects_non_closed_yaml_without_leaking_content(
    tmp_path: Path,
    manifest_text: str,
) -> None:
    """证明别名、标签、多文档、重复键和额外字段均失败关闭。

    参数：``tmp_path`` 是隔离授权目录；``manifest_text`` 是一项不可信 YAML 样本。
    返回：无；测试断言公开错误稳定且不泄漏声明正文。
    """

    selected_root = tmp_path / "selected"
    selected_root.mkdir()
    selected_root.joinpath("package.yaml").write_text(manifest_text, encoding="utf-8")

    with pytest.raises(SourceDeclarationError) as caught:
        discover_editable_sources((selected_root,))

    assert caught.value.code == "invalid_manifest"
    assert "secret-value" not in str(caught.value)


def test_discovery_limits_workflow_declarations_to_1024(tmp_path: Path) -> None:
    """证明单个 manifest 不能用超量工作流声明耗尽启动资源。

    参数：``tmp_path`` 保存含 1025 项声明的隔离 manifest。
    返回：无；测试断言超限在形成发现计划前失败。
    """

    selected_root = tmp_path / "selected"
    package_root = selected_root / "demo"
    package_root.mkdir(parents=True)
    workflow_lines: list[str] = []
    for index in range(1025):
        # UUID 由固定整数生成，仅用于提供 1025 个互异规范工作流身份。
        workflow_uuid = f"00000000-0000-4000-8000-{index + 1:012d}"
        workflow_lines.extend(
            (
                f"  - workflow_uuid: {workflow_uuid}",
                f"    source: demo/workflows/workflow_{index}.py",
            )
        )
    selected_root.joinpath("package.yaml").write_text(
        "\n".join(("package:", "  name: demo", "workflows:", *workflow_lines)),
        encoding="utf-8",
    )

    with pytest.raises(SourceDeclarationError) as caught:
        discover_editable_sources((selected_root,))

    assert caught.value.code == "invalid_manifest"


@pytest.mark.parametrize(
    ("package_id", "workflow_uuid", "expected_code"),
    (
        ("实验包", "11111111-1111-4111-8111-111111111111", "invalid_package"),
        ("demo", "11111111-1111-4111-8111-11111111111A", "invalid_workflow_source"),
    ),
)
def test_discovery_requires_ascii_package_and_canonical_uuid_identity(
    tmp_path: Path,
    package_id: str,
    workflow_uuid: str,
    expected_code: str,
) -> None:
    """证明包身份只接受 ASCII Python 标识符，UUID 只接受规范小写文本。

    参数：``tmp_path`` 是隔离目录；``package_id`` 和 ``workflow_uuid`` 是待拒绝身份；
    ``expected_code`` 是对应的稳定错误分类。
    返回：无；测试断言身份文本不会被静默规范化。
    """

    selected_root = tmp_path / "selected"
    selected_root.joinpath(package_id).mkdir(parents=True)
    selected_root.joinpath("package.yaml").write_text(
        "package:\n"
        f"  name: {package_id}\n"
        "workflows:\n"
        f"  - workflow_uuid: {workflow_uuid}\n"
        f"    source: {package_id}/workflows/demo.py\n",
        encoding="utf-8",
    )

    with pytest.raises(SourceDeclarationError) as caught:
        discover_editable_sources((selected_root,))

    assert caught.value.code == expected_code
