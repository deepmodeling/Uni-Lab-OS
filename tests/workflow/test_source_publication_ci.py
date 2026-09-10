"""工作流源码（Workflow Source）平台安全 CI 配置合同。"""

from pathlib import Path


def test_windows_ci_installs_pytest_before_running_source_safety_suite() -> None:
    """Windows CI 必须显式安装 pytest 并执行完整源码安全测试文件。

    参数：无。返回：无；配置缺少测试依赖或任一安全测试文件时失败，避免真实
    Windows 门禁只存在于仓库却从未执行。
    """

    workflow = Path(".github/workflows/ci-check.yml").read_text(encoding="utf-8")
    install_section, safety_section = workflow.split(
        "- name: Run Windows workflow source safety contracts",
        maxsplit=1,
    )

    assert "uv pip install pytest" in install_section
    assert "tests/workflow/test_source_publication_windows.py" in safety_section
    assert "tests/workflow/test_source_workspace_security.py" in safety_section
