"""扫描源码、文档和生成产物，阻止混淆工作流修订与合同发布序号。"""

import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
FORBIDDEN = re.compile(
    r"工作流" + r"(?:的)?版本|\bworkflow[_ .-]?versions?\b"
    r"|workflow\s*\[\s*['\"]version['\"]\s*\]"
    r"|contract\.workflow_revision\s*\|\|\s*contract\.version"
    r"|发布形成稳定版本|发布后形成稳定版本|发布稳定版本|流程版本"
    r"|子操作最新版本|child (?:合同/版本|contract/version)"
    r"|版本 1 工作流|Workflow）版本 1",
    re.IGNORECASE,
)
WORKFLOW_ONLY = re.compile(
    r"latest_version\b|发布新版本|发布版本|冻结版本|固定版本|候选版本",
)

# 本次只清理术语，不修改已有业务行为。以下精确文本属于保留的运行时
# 兼容行为、既有局部变量名或测试数据；限制出现次数，避免豁免整份文件。
LEGACY_RUNTIME_TEXT: dict[str, tuple[tuple[str, int], ...]] = {
    "frontend/src/pages/OperationsPage.tsx": (
        ("contract.workflow_revision || contract.version ||", 2),
    ),
    "frontend/src/pages/WorkflowsPage.tsx": (
        ("contract.workflow_revision || contract.version ||", 1),
    ),
    "tests/scheduler_core/test_resource_lock_plan_execution.py": (
        ('"workflow-version-downgrade"', 1),
    ),
    "tests/workflow/fixtures/control_flow_import_payload.json": (
        ("引用已发布实验操作；兼容的新发布版本由 OS 自动替换。", 2),
    ),
    "tests/workflow/test_domain_workflow_source_sync.py": (
        ('description="第二个不可变发布版本"', 1),
    ),
    "unilabos/workflow/authoring_candidate_hash.py": (
        ('"候选版本（Candidate）正文必须是对象"', 1),
        ('"候选版本（Candidate）正文不能按规范计算哈希"', 1),
    ),
    "unilabos/workflow/composite_invocation.py": (
        ("引用已发布实验操作；兼容的新发布版本由 OS 自动替换。", 1),
    ),
    "unilabos/workflow/published_contract.py": (
        ("latest_version", 2),
    ),
    "unilabos/workflow/store.py": (
        ('"候选版本（Candidate）持久包缺少应用事实"', 1),
        ('"候选版本（Candidate）持久包应用事实类型无效"', 1),
    ),
}


@pytest.mark.parametrize("text", [
    "工作流" + "版本", "workflow" + "_version", "workflow" + ".version",
    "workflow['version']", "contract.workflow_revision || contract.version || 1",
])
def test_rejects_ambiguous_workflow_terms(text: str) -> None:
    assert FORBIDDEN.search(text)


@pytest.mark.parametrize("text", [
    "workflow_revision", "工作流修订", "contract_uuid", "contract_digest",
    "published_workflow_contract.version 表示合同发布序号",
    "输入合同 schema version 1", "设备包发布新版本",
])
def test_allows_revision_contract_sequence_and_schema_terms(text: str) -> None:
    assert not FORBIDDEN.search(text)


def test_repository_workflow_terminology() -> None:
    """实际扫描已跟踪文本；报告路径和行号以便修正源文件及其生成副本。"""
    paths = subprocess.check_output(
        ["git", "ls-files", "-z", "--", "AGENTS.md", "CLAUDE.md", "CONTEXT.md", "unilabos", "frontend/src", "docs", "plan", "tests"],
        cwd=ROOT,
    ).decode().split("\0")
    failures: list[str] = []
    for name in paths:
        path = ROOT / name
        if not name or not path.is_file() or path.suffix not in {
            ".py", ".ts", ".tsx", ".md", ".html", ".txt", ".json", ".js",
        }:
            continue
        if path == Path(__file__).resolve():
            continue
        source = path.read_text(encoding="utf-8", errors="replace")
        for literal, count in LEGACY_RUNTIME_TEXT.get(name, ()):
            assert source.count(literal) == count, (
                f"{name}: 运行时兼容文本数量变化，请核对并更新精确豁免"
            )
            source = source.replace(literal, " " * len(literal))
        for number, line in enumerate(source.splitlines(), 1):
            workflow_source = name.startswith(("unilabos/workflow/", "tests/workflow/"))
            if FORBIDDEN.search(line) or (workflow_source and WORKFLOW_ONLY.search(line)):
                failures.append(f"{name}:{number}: {line[:180]}")
    assert not failures, "\n".join(failures)
