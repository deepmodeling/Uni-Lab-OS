"""校验调度决策内核独立套件的语句与分支覆盖率。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def percentage(covered: int, total: int) -> float:
    """返回覆盖百分比；没有可执行项时按完整覆盖处理。"""

    return 100.0 if total == 0 else covered * 100.0 / total


def main() -> int:
    """读取 coverage JSON，分别执行语句和分支阈值门禁。"""

    parser = argparse.ArgumentParser()
    parser.add_argument("report", type=Path)
    parser.add_argument("--statements", type=float, default=85.0)
    parser.add_argument("--branches", type=float, default=75.0)
    args = parser.parse_args()
    payload: dict[str, Any] = json.loads(args.report.read_text(encoding="utf-8"))
    totals = payload["totals"]
    statements = percentage(
        int(totals["covered_lines"]),
        int(totals["num_statements"]),
    )
    branches = percentage(
        int(totals["covered_branches"]),
        int(totals["num_branches"]),
    )
    print(
        "scheduler core coverage: "
        f"statements={statements:.2f}% (required {args.statements:.2f}%), "
        f"branches={branches:.2f}% (required {args.branches:.2f}%)"
    )
    return int(statements < args.statements or branches < args.branches)


if __name__ == "__main__":
    raise SystemExit(main())
