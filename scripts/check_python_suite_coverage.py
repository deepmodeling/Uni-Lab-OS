"""校验无 ROS Python 套件的内核覆盖率，并打印全仓库覆盖率。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

KERNEL_PREFIXES = (
    "unilabos/workflow/",
    "unilabos/registry/",
    "unilabos/package_manager/",
    "unilabos/app/",
    "unilabos/resources/",
    "unilabos/client/",
    "unilabos/hostlink/",
    "unilabos/workspace_host/",
    "unilabos/utils/",
)

# 这些模块在 import 时绑定 ROS。它们留在全仓库报告里，不进入内核门禁。
KERNEL_EXCLUSIONS = (
    "unilabos/app/web/",
    "unilabos/app/ws_client.py",
)


def percentage(covered: int, total: int) -> float:
    """返回覆盖百分比；没有可执行项时按完整覆盖处理。"""

    return 100.0 if total == 0 else covered * 100.0 / total


def normalize(path: str) -> str:
    """把 coverage 记录的路径收成仓库内的正斜杠相对路径。"""

    unified = path.replace("\\", "/")
    marker = "unilabos/"
    index = unified.find(marker)
    if index == -1:
        return unified
    return unified[index:]


def in_kernel(path: str) -> bool:
    """判断一个源文件是否属于重构后仍留在开源内核的覆盖率分母。"""

    relative = normalize(path)
    if any(relative == item or relative.startswith(item) for item in KERNEL_EXCLUSIONS):
        return False
    return any(relative.startswith(prefix) for prefix in KERNEL_PREFIXES)


def summarize(files: dict[str, Any], predicate) -> tuple[int, int, int, int]:
    """按文件摘要累加语句和分支的覆盖数与总数。"""

    covered_lines = 0
    statements = 0
    covered_branches = 0
    branches = 0
    for path, payload in files.items():
        if not predicate(path):
            continue
        summary = payload["summary"]
        covered_lines += int(summary["covered_lines"])
        statements += int(summary["num_statements"])
        covered_branches += int(summary["covered_branches"])
        branches += int(summary["num_branches"])
    return covered_lines, statements, covered_branches, branches


def main() -> int:
    """读取一次 pytest-cov JSON，门禁只比较内核，全仓库数字只打印。"""

    parser = argparse.ArgumentParser()
    parser.add_argument("report", type=Path)
    parser.add_argument("--statements", type=float, required=True)
    parser.add_argument("--branches", type=float, required=True)
    args = parser.parse_args()
    payload: dict[str, Any] = json.loads(args.report.read_text(encoding="utf-8"))
    files: dict[str, Any] = payload["files"]
    kernel_lines, kernel_statements, kernel_covered_branches, kernel_branches = summarize(
        files, in_kernel
    )
    repo_lines, repo_statements, repo_covered_branches, repo_branches = summarize(
        files, lambda _path: True
    )
    kernel_statement_pct = percentage(kernel_lines, kernel_statements)
    kernel_branch_pct = percentage(kernel_covered_branches, kernel_branches)
    print(
        "python suite kernel coverage: "
        f"statements={kernel_statement_pct:.2f}% "
        f"(required {args.statements:.2f}%), "
        f"branches={kernel_branch_pct:.2f}% "
        f"(required {args.branches:.2f}%)"
    )
    print(
        "python suite repository coverage: "
        f"statements={percentage(repo_lines, repo_statements):.2f}%, "
        f"branches={percentage(repo_covered_branches, repo_branches):.2f}%"
    )
    return int(
        kernel_statement_pct < args.statements or kernel_branch_pct < args.branches
    )


if __name__ == "__main__":
    raise SystemExit(main())
