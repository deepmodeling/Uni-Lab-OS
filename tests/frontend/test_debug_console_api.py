"""React 调试控制台（frontend/）的 API 契约对账。

控制台的类型来自已提交的 OpenAPI 快照（``frontend/openapi/unilabos-openapi.json``）经
openapi-typescript 生成的 ``frontend/src/api/generated.ts``。后端路由或模型变了而快照没刷新时，
这里失败并提示 ``cd frontend && npm run api:sync``。

刷新需要 node，所以没有 node 时整个模块跳过；生成类型的对账还需要 ``frontend/node_modules``
（``npm ci``），缺失时只跳过那一条。比较快照时忽略只随包版本变化的两个版本字段。
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict

import pytest

from unilabos.server import openapi_export

ROOT = Path(__file__).resolve().parents[2]
FRONTEND = ROOT / "frontend"
SNAPSHOT = FRONTEND / "openapi" / "unilabos-openapi.json"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="没有 node：调试控制台的 API 快照对账跳过")


def _normalized(document: Dict[str, Any]) -> Dict[str, Any]:
    document = json.loads(json.dumps(document))
    info = document.get("info", {})
    info.pop("version", None)
    info.get("x-openlab-protocol", {}).pop("unilabos_version", None)
    return document


def test_snapshot_matches_fresh_export() -> None:
    committed = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    assert _normalized(committed) == _normalized(openapi_export.export_openapi()), (
        "frontend/openapi/unilabos-openapi.json 与当前后端导出不一致：cd frontend && npm run api:sync"
    )


def test_generated_types_match_snapshot() -> None:
    if not (FRONTEND / "node_modules" / "openapi-typescript").is_dir():
        pytest.skip("frontend/node_modules 未安装（cd frontend && npm ci）")
    result = subprocess.run(
        [NODE, "scripts/api-sync.mjs", "--check-types"],
        cwd=FRONTEND,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=300,
    )
    assert result.returncode == 0, result.stdout + result.stderr
