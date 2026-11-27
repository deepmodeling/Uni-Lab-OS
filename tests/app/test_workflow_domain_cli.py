from __future__ import annotations

import sys

from unilabos.app.main import parse_args


def test_workflow_domain_cli_exposes_agent_operations(monkeypatch) -> None:
    cases = [
        (["workflow", "list", "--json"], "list"),
        (["workflow", "inspect", "task-1", "--kind", "task"], "inspect"),
        (["workflow", "run", "workflow-1", "--follow", "--jsonl"], "run"),
        (
            [
                "workflow",
                "debug",
                "workflow-1",
                "--start-node",
                "node-1",
                "--breakpoint",
                "node-2",
            ],
            "debug",
        ),
        (["workflow", "watch", "task-1", "--after", "8", "--jsonl"], "watch"),
        (["workflow", "command", "task-1", "continue", "--hold", "hold-1"], "command"),
        (
            ["workflow", "authoring", "workflow-1", "--after-revision", "9"],
            "authoring",
        ),
    ]
    for argv, action in cases:
        monkeypatch.setattr(sys, "argv", ["unilab", *argv])
        parsed = parse_args().parse_args()
        assert parsed.command == "workflow"
        assert parsed.workflow_command == action
