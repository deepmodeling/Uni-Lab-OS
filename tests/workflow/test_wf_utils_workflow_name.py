"""``unilabos.workflow.wf_utils.upload_workflow`` 工作流名称 fallback 链单元测试。

对应需求：上传工作流时，**优先取 metadata.workflow_name**；缺失时再回退到顶层
``workflow_name``（旧 node-link 形态遗留字段）；最后才回退到文件名（去 ``.json`` 后缀）。
CLI 显式 ``-n/--workflow_name`` 永远最优先。

本测试只校验「**名称 fallback 链 + tags fallback 链**」的纯逻辑路径，
不实际访问 HTTP / 后端；通过 monkeypatch 把 ``http_client.workflow_import``
桩成可观察的捕获函数。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest


# 让 import 走 Uni-Lab-OS 包根
ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "unilabos"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture
def stub_upload(monkeypatch, tmp_path):
    """Monkeypatch ``http_client.workflow_import`` + ``_convert_to_node_link``，
    返回 (helper, captured) 二元组：

    - ``helper(workflow_data, **upload_kwargs)`` 写入 tmp_path/wf.json
      并调用 ``upload_workflow``；
    - ``captured`` 是 dict，记录 ``workflow_import`` 实际收到的 kwargs，
      以及 ``_convert_to_node_link`` 是否被调过。

    本测试不依赖真实 ``unilabos.app.web``（其级联依赖含 ``fastapi`` 等重型
    package，本地 dev venv 不必装）。通过在 sys.modules 注入空壳 module 拦截
    delayed import。
    """
    import types

    captured: Dict[str, Any] = {"workflow_import_kwargs": None, "converted": False}

    def fake_workflow_import(**kwargs):  # noqa: ANN003
        captured["workflow_import_kwargs"] = kwargs
        return {"code": 0, "data": {"uuid": "fake-uuid", "name": kwargs.get("name")}}

    # 关键：在 wf_utils 触发 `from unilabos.app.web import http_client` 之前
    # 用空壳 module 占位（避免触发真实 web 包的 fastapi 依赖链）。
    fake_http_client = types.ModuleType("unilabos.app.web.http_client")
    fake_http_client.workflow_import = fake_workflow_import  # type: ignore[attr-defined]
    fake_web_pkg = types.ModuleType("unilabos.app.web")
    fake_web_pkg.http_client = fake_http_client  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "unilabos.app.web", fake_web_pkg)
    monkeypatch.setitem(sys.modules, "unilabos.app.web.http_client", fake_http_client)

    from unilabos.workflow import wf_utils

    # _convert_to_node_link 走真实路径会拉重型依赖，这里桩为 node-link 直返回
    def fake_convert_to_node_link(workflow_file, workflow_data, *, target_device="prcxi", target_model=None):
        captured["converted"] = True
        # 返回最小合法 node-link 形态（不带 metadata，模拟当前行为）
        return {"nodes": [], "edges": [], "workflow_uuid": ""}

    monkeypatch.setattr(wf_utils, "_convert_to_node_link", fake_convert_to_node_link)

    def helper(workflow_data: Dict[str, Any], **upload_kwargs: Any) -> Dict[str, Any]:
        wf_path = tmp_path / "transfer_actions_sample.json"
        wf_path.write_text(json.dumps(workflow_data, ensure_ascii=False), encoding="utf-8")
        return wf_utils.upload_workflow(str(wf_path), **upload_kwargs)

    return helper, captured


# ==================== workflow_name fallback 链 ====================


def test_metadata_workflow_name_wins_over_filename(stub_upload):
    """P5 主路径：transfer_actions JSON 含 metadata.workflow_name → 优先于文件名。"""
    helper, captured = stub_upload
    data = {
        "metadata": {"workflow_name": "PCR Prep with Categories", "tags": []},
        "workflow": [],
        "reagent": {},
    }
    helper(data)
    kwargs = captured["workflow_import_kwargs"]
    assert kwargs is not None and captured["converted"] is True
    assert kwargs["name"] == "PCR Prep with Categories"
    assert kwargs["workflow_name"] == "PCR Prep with Categories"


def test_cli_workflow_name_overrides_metadata(stub_upload):
    """CLI 显式 -n/--workflow_name 永远最优先。"""
    helper, captured = stub_upload
    data = {
        "metadata": {"workflow_name": "Metadata Wins By Default"},
        "workflow": [],
        "reagent": {},
    }
    helper(data, workflow_name="CLI Override Name")
    kwargs = captured["workflow_import_kwargs"]
    assert kwargs["name"] == "CLI Override Name"
    assert kwargs["workflow_name"] == "CLI Override Name"


def test_filename_used_when_no_metadata_and_no_legacy(stub_upload):
    """P5 之前的旧文件、且无顶层 workflow_name → 回退到去 .json 后缀的文件名。"""
    helper, captured = stub_upload
    data = {"workflow": [], "reagent": {}}  # 既无 metadata，也无 workflow_name
    helper(data)
    kwargs = captured["workflow_import_kwargs"]
    # 文件名由 fixture 固定为 transfer_actions_sample.json
    assert kwargs["name"] == "transfer_actions_sample"
    assert kwargs["workflow_name"] == "transfer_actions_sample"


def test_metadata_empty_string_falls_back_to_filename(stub_upload):
    """metadata.workflow_name 为空字符串（而非缺失）也应回退到文件名。"""
    helper, captured = stub_upload
    data = {
        "metadata": {"workflow_name": "   "},  # whitespace-only
        "workflow": [],
        "reagent": {},
    }
    helper(data)
    kwargs = captured["workflow_import_kwargs"]
    assert kwargs["name"] == "transfer_actions_sample"


def test_legacy_top_level_workflow_name_used_when_metadata_missing(stub_upload, monkeypatch):
    """旧 node-link 文件（已是 nodes/edges 形态）顶层 workflow_name → 应被使用。

    覆盖路径：``_is_node_link_format`` 直接命中 → 不走转换 → workflow_data 保留顶层
    workflow_name；``orig_metadata`` 为空时 fallback 到该字段。
    """
    helper, captured = stub_upload
    data = {
        "nodes": [],
        "edges": [],
        "workflow_name": "Legacy Top Name",
    }
    helper(data)
    kwargs = captured["workflow_import_kwargs"]
    assert captured["converted"] is False, "node-link 输入不应触发转换"
    assert kwargs["name"] == "Legacy Top Name"
    assert kwargs["workflow_name"] == "Legacy Top Name"


# ==================== tags fallback 链 ====================


def test_metadata_tags_used_when_cli_tags_missing(stub_upload):
    """P5 主路径：metadata.tags 在 CLI 未传 tags 时被使用。"""
    helper, captured = stub_upload
    data = {
        "metadata": {"workflow_name": "X", "tags": ["Opentrons", "PCR"]},
        "workflow": [],
        "reagent": {},
    }
    helper(data)
    kwargs = captured["workflow_import_kwargs"]
    assert kwargs["tags"] == ["Opentrons", "PCR"]


def test_cli_tags_override_metadata_tags(stub_upload):
    """CLI 显式 --tags 优先于 metadata.tags。"""
    helper, captured = stub_upload
    data = {
        "metadata": {"workflow_name": "X", "tags": ["Opentrons", "PCR"]},
        "workflow": [],
        "reagent": {},
    }
    helper(data, tags=["CLI", "Wins"])
    kwargs = captured["workflow_import_kwargs"]
    assert kwargs["tags"] == ["CLI", "Wins"]
