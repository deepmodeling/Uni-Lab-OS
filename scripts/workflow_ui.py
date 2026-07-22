"""szlab 本地 workflow 调试界面。"""

from __future__ import annotations
from scripts.workflow_timing import WorkflowTimingRecorder
from scripts.run_workflow_local import (
    ROBOT_ARM_DEVICE_ID,
    RuntimeConfig,
    WorkflowLogger,
    WorkflowNode,
    bind_opc_wait_logger,
    build_execution_order,
    build_snapshot_diff_detail,
    collect_snapshot_variables,
    create_local_devices,
    format_snapshot_detail,
    ignore_opcua_token_time_drift,
    iter_opc_wait_logs,
    load_workflow_nodes,
    load_runtime_config,
    method_name_from_template,
    route_node_device,
    run_nodes,
    snapshot_opc_state,
)
from unilabos.registry.ast_registry_scanner import scan_directory
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response, StreamingResponse
from fastapi import FastAPI, HTTPException

import argparse
import asyncio
import csv
import io
import json
import os
import re
import sys
import tempfile
import threading
import time
import uuid
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


SZLAB_DIR = REPO_ROOT / "tests" / "szlab_poly_studio"
PRESET_DIR = SZLAB_DIR / "presets"
FRONTEND_DIR = REPO_ROOT / "unilabos_local_ui"
FRONTEND_DIST_DIR = FRONTEND_DIR / "dist"
FRONTEND_INDEX_FILE = FRONTEND_DIST_DIR / "index.html"
GENERATED_GRAPH_SENTINEL = "__generated__"


@dataclass(frozen=True)
class ActionSpec:
    method: str
    label: str
    description: str
    params: list[dict[str, Any]] = field(default_factory=list)
    device_id: str | None = None

    @property
    def needs_position(self) -> bool:
        return any(param.get("name") == "position" for param in self.params)


@dataclass(frozen=True)
class WorkflowPreset:
    id: str
    title: str
    target_device_id: str
    target_device_ids: list[str]
    runtime_config: str | None
    default_workflow_name: str
    default_config: dict[str, Any]
    debug_config: dict[str, Any]
    path_roots: list[str]
    device_graph: dict[str, Any]
    actions: dict[str, ActionSpec]
    base_dir: Path = SZLAB_DIR


def load_preset(name: str = "ai4c") -> WorkflowPreset:
    candidate = Path(name)
    if candidate.suffix == ".json" or candidate.exists():
        preset_path = candidate if candidate.is_absolute() else SZLAB_DIR / candidate
    else:
        preset_path = PRESET_DIR / f"{name}.json"
    data = json.loads(preset_path.read_text(encoding="utf-8"))
    target_device_id = data.get("target_device_id", ROBOT_ARM_DEVICE_ID)
    target_device_ids = list(data.get("target_device_ids") or [target_device_id])
    registry_device_ids = list(data.get("registry_device_ids") or target_device_ids)
    action_device_id_aliases = dict(data.get("action_device_id_aliases") or {})
    path_roots = data.get("path_roots", ["tests/szlab_poly_studio"])
    if data.get("actions_source") == "registry":
        actions = _load_registry_actions(registry_device_ids, path_roots, preset_path.parent)
        if action_device_id_aliases:
            actions = {
                method: replace(action, device_id=action_device_id_aliases.get(
                    action.device_id or "", action.device_id))
                for method, action in actions.items()
            }
        hidden_actions = set(data.get("hidden_actions") or [])
        if hidden_actions:
            actions = {
                method: action
                for method, action in actions.items()
                if method not in hidden_actions
            }
    else:
        actions = {
            item["method"]: ActionSpec(
                method=item["method"],
                label=item.get("label", item["method"]),
                description=item.get("description", ""),
                params=item.get("params", []),
                device_id=item.get("device_id") or target_device_id,
            )
            for item in data.get("actions", [])
        }
    return WorkflowPreset(
        id=data["id"],
        title=data.get("title", "szlab 本地调试工具"),
        target_device_id=target_device_id,
        target_device_ids=target_device_ids,
        runtime_config=data.get("runtime_config"),
        default_workflow_name=data.get("default_workflow_name", "szlab_canvas_workflow"),
        default_config=data.get("default_config", {}),
        debug_config=data.get("debug_config", {}),
        path_roots=path_roots,
        device_graph=data.get("device_graph", {"nodes": [], "links": []}),
        actions=actions,
        base_dir=preset_path.parent,
    )


def _load_registry_actions(device_ids: list[str], path_roots: list[str], base_dir: Path) -> dict[str, ActionSpec]:
    repo_root = REPO_ROOT
    pending = set(device_ids)
    actions_by_device: dict[str, dict[str, ActionSpec]] = {}
    with ThreadPoolExecutor(max_workers=4, thread_name_prefix="SzlabRegistryScan") as executor:
        for root in path_roots:
            root_path = _resolve_registry_scan_root(root, base_dir, repo_root)
            if not root_path.exists():
                continue
            scan_result = scan_directory(root_path, python_path=repo_root, executor=executor)
            for device_id in list(pending):
                device_meta = scan_result.get("devices", {}).get(device_id)
                if device_meta:
                    actions_by_device[device_id] = _actions_from_ast_device_meta(device_id, device_meta)
                    pending.remove(device_id)
            if not pending:
                return {
                    method: action
                    for device_id in device_ids
                    for method, action in actions_by_device.get(device_id, {}).items()
                }
    raise ValueError(f"无法从 registry AST 扫描找到设备动作: {sorted(pending)}")


def _resolve_registry_scan_root(root: str, base_dir: Path, repo_root: Path) -> Path:
    candidate = Path(root)
    if candidate.is_absolute():
        return candidate
    repo_candidate = repo_root / candidate
    if repo_candidate.exists():
        return repo_candidate
    return base_dir / candidate


def _actions_from_ast_device_meta(device_id: str, device_meta: dict[str, Any]) -> dict[str, ActionSpec]:
    actions: dict[str, ActionSpec] = {}
    for method, method_info in device_meta.get("actions", {}).items():
        action_args = method_info.get("action_args") or {}
        description = action_args.get("description") or method
        actions[method] = ActionSpec(
            method=method,
            label=description,
            description=description,
            params=_params_from_ast_action(method, method_info),
            device_id=device_id,
        )
    for method, method_info in device_meta.get("auto_methods", {}).items():
        actions.setdefault(
            method,
            ActionSpec(
                method=method,
                label=method,
                description=method_info.get("docstring") or "",
                params=_params_from_ast_action(method, method_info),
                device_id=device_id,
            ),
        )
    return actions


_PRODUCT_TYPE_OPTIONS = [
    {"value": 1, "label": "烧杯"},
    {"value": 2, "label": "250 mL 样品瓶"},
    {"value": 3, "label": "500 mL 样品瓶"},
]
_S01_PRODUCT_TYPE_OPTIONS = [
    {"value": 1, "label": "TIP"},
    {"value": 2, "label": "烧杯"},
    {"value": 3, "label": "250 mL 样品瓶"},
    {"value": 4, "label": "500 mL 样品瓶"},
    {"value": 5, "label": "100 mL 液体瓶"},
    {"value": 6, "label": "固体粉末"},
]
_S072_PRODUCT_TYPE_OPTIONS = [
    {"value": 1, "label": "固体粉末"},
    {"value": 2, "label": "烧杯"},
]
_S08_PRODUCT_TYPE_OPTIONS = [
    {"value": 1, "label": "250 mL 样品瓶"},
    {"value": 2, "label": "500 mL 样品瓶"},
    {"value": 3, "label": "100 mL 液体瓶"},
]
_PARAM_HELP_BY_NAME: dict[str, dict[str, Any]] = {
    "sample_id": {"label": "样品 ID", "description": "用于追踪物料、照片和实验结果的样品标识。"},
    "position": {"label": "位置", "description": "目标工位或仓位编号；可用范围取决于当前动作。"},
    "timeout": {"label": "超时时间", "description": "等待 PLC 工艺完成的最长时间。", "unit": "s"},
    "超时时间": {"description": "等待 S08 开关盖工艺完成的最长时间。", "unit": "s"},
    "duration": {"label": "持续时间", "description": "工艺持续运行时间。", "unit": "s"},
    "speed": {"label": "搅拌速度", "description": "S04 磁力搅拌转速设定。", "unit": "rpm"},
    "temperature": {"label": "目标温度", "description": "S04 磁搅目标温度。", "unit": "°C"},
    "safe_temperature": {"label": "安全温度", "description": "S04 超温保护阈值。", "unit": "°C"},
    "reset": {"label": "仅复位", "description": "开启后只恢复 PLC 参数初始值，不启动加工。"},
    "photo_path": {"label": "照片路径", "description": "预留的照片输出路径；当前由设备侧生成实际照片地址。"},
    "inspection_result": {"label": "检测结果", "description": "预留的算法检测结果；当前以 PLC 拍照结果为准。"},
    "require_material": {"label": "要求有料", "description": "兼容参数；实机拍照动作始终检查拍照位有料。"},
    "volume": {"label": "输送体积", "description": "S06 单次管路输送量，使用 PLC 原始体积单位。", "unit": "PLC raw"},
    "volume_pump_1": {"label": "1号泵加液量", "description": "S06 1号泵本次工艺的加液设定量。", "unit": "PLC raw"},
    "volume_pump_2": {"label": "2号泵加液量", "description": "S06 2号泵本次工艺的加液设定量。", "unit": "PLC raw"},
    "direction": {
        "label": "输送方向",
        "description": "液体输送方向。",
        "options": [{"value": "aspirate", "label": "吸液"}, {"value": "dispense", "label": "排液"}],
    },
    "pipeline": {
        "label": "管路",
        "description": "选择执行动作的 S06 管路。",
        "options": [
            {"value": "aspirate", "label": "吸液管路"},
            {"value": "dispense", "label": "排液管路"},
            {"value": "air", "label": "空气管路"},
        ],
    },
    "skip_level_check": {"label": "跳过液位检查", "description": "仅调试使用；开启后不执行前置液位检查。"},
    "beaker_true_means_present": {"label": "烧杯信号极性", "description": "开启表示传感器 True 代表烧杯在位。"},
    "coarse_position": {"label": "粗注粉粉罐位", "description": "参与粗注粉的 S07 粉罐位置，范围 1–10。"},
    "fine_position": {"label": "精注粉粉罐位", "description": "参与精注粉的 S07 粉罐位置，范围 1–10。"},
    "target_weight": {"label": "目标注粉重量", "description": "S07 本次注粉的目标重量。", "unit": "g（待 PLC 确认）"},
    "params_json": {"label": "配方文件", "description": "粗/精注粉参数 JSON 路径；留空使用设备默认文件。"},
    "recipe_name": {"label": "配方名称", "description": "注粉参数 JSON 中选用的配方键名。"},
    "工艺选择": {
        "description": "S08 开关盖工艺编号。",
        "options": [
            {"value": 1, "label": "开启 500 mL 样品瓶"},
            {"value": 2, "label": "关闭 500 mL 样品瓶"},
            {"value": 3, "label": "开启 250 mL 样品瓶"},
            {"value": 4, "label": "关闭 250 mL 样品瓶"},
            {"value": 5, "label": "开启 100 mL 液体瓶"},
            {"value": 6, "label": "关闭 100 mL 液体瓶"},
        ],
    },
    "样品ID": {"description": "S08 处理的样品 ID 数组，用于动作追踪。"},
    "瓶盖暂存位": {"description": "S08 瓶盖暂存位置编号。"},
    "home_position": {"label": "原点编号", "description": "需要检查的 S09 原点信号编号。"},
    "take_tip_box_index": {"label": "取 TIP 盒", "description": "S09 取新 TIP 的盒位编号，通常为 1。"},
    "release_tip_box_index": {"label": "废 TIP 盒", "description": "S09 释放已用 TIP 的盒位编号，通常为 2。"},
    "tip_index": {"label": "TIP 编号", "description": "当前 TIP 盒内使用的 TIP 位置编号。"},
    "liquid_bottle_index": {"label": "液体瓶编号", "description": "S09 液体试剂瓶工位编号，范围 1–5。"},
    "station": {"label": "烧杯工位", "description": "S09 承接加液的烧杯工位编号。"},
    "aspirate_volume": {"label": "吸液体积", "description": "吸取体积；实际单位由“体积单位”决定。"},
    "dispense_volume": {"label": "放液体积", "description": "排出体积；实际单位由“体积单位”决定。"},
    "volume_unit": {
        "label": "体积单位",
        "description": "raw=0.1 µL/单位，也可直接选择 µL 或 mL。",
        "options": [
            {"value": "raw", "label": "PLC raw（0.1 µL）"},
            {"value": "ul", "label": "µL"},
            {"value": "ml", "label": "mL"},
        ],
    },
    "liquid_steps": {"label": "移液步骤", "description": "S09 批量移液步骤数组；每项包含取 TIP、吸液和放液参数。"},
    "release_after": {"label": "结束后释放", "description": "流程完成后是否释放样品与工站绑定。"},
    "bottle": {"label": "液体瓶编号", "description": "S09 液体试剂瓶编号，范围 1–5。"},
    "remaining_volume": {"label": "剩余液量", "description": "液体瓶当前或初始化剩余体积。", "unit": "mL"},
    "require_stable": {"label": "要求稳定", "description": "开启后仅在 S09 天平稳定信号有效时返回读数。"},
}
_METHOD_PARAM_HELP: dict[tuple[str, str], dict[str, Any]] = {
    **{
        (method, "product_type"): {
            "label": "产品类型",
            "description": "1=烧杯，2=250 mL 样品瓶，3=500 mL 样品瓶。",
            "options": _PRODUCT_TYPE_OPTIONS,
        }
        for method in (
            "submit_place_to_s03",
            "submit_pick_from_s03",
            "submit_place_to_s11",
            "submit_pick_from_s11",
        )
    },
    ("submit_pick_from_s01", "product_type"): {
        "label": "S01 出入料产品",
        "description": "1=TIP，2=烧杯，3=250 mL 样品瓶，4=500 mL 样品瓶，5=100 mL 液体瓶，6=固体粉末。",
        "options": _S01_PRODUCT_TYPE_OPTIONS,
    },
    ("submit_place_to_s072", "product_type"): {
        "label": "S072 产品代码",
        "description": "1=固体粉末，2=烧杯。",
        "options": _S072_PRODUCT_TYPE_OPTIONS,
    },
    ("submit_pick_from_s072", "product_type"): {
        "label": "S072 产品代码",
        "description": "1=固体粉末，2=烧杯。",
        "options": _S072_PRODUCT_TYPE_OPTIONS,
    },
    **{
        (method, "product_type"): {
            "label": "S09 产品类型",
            "description": "1=TIP盒，2=液体试剂瓶，3=烧杯。",
            "options": [
                {"value": 1, "label": "TIP 盒"},
                {"value": 2, "label": "液体试剂瓶"},
                {"value": 3, "label": "烧杯"},
            ],
        }
        for method in ("submit_place_to_s09", "submit_pick_from_s09")
    },
    ("submit_place_to_s08", "product_type"): {
        "label": "瓶型",
        "description": "1=250 mL 样品瓶，2=500 mL 样品瓶，3=100 mL 液体瓶。",
        "options": _S08_PRODUCT_TYPE_OPTIONS,
    },
    ("submit_pick_from_s08", "product_type"): {
        "label": "瓶型",
        "description": "1=250 mL 样品瓶，2=500 mL 样品瓶，3=100 mL 液体瓶。",
        "options": _S08_PRODUCT_TYPE_OPTIONS,
    },
    ("submit_pour_from_s08", "product_type"): {
        "label": "倒料瓶型",
        "description": "1=250 mL 样品瓶，2=500 mL 样品瓶。",
        "options": [
            {"value": 1, "label": "250 mL 样品瓶"},
            {"value": 2, "label": "500 mL 样品瓶"},
        ],
    },
    ("run_stirring", "mode"): {
        "label": "工艺模式",
        "description": "1=仅搅拌，2=仅加热，3=搅拌并加热。",
        "options": [
            {"value": 1, "label": "搅拌"},
            {"value": 2, "label": "加热"},
            {"value": 3, "label": "搅拌 + 加热"},
        ],
    },
    ("run_solvent_addition", "process"): {
        "label": "S06 工艺",
        "description": "1=仅1号泵，2=仅2号泵，3=两路泵均执行。",
        "options": [
            {"value": 1, "label": "1号泵"},
            {"value": 2, "label": "2号泵"},
            {"value": 3, "label": "1号泵 + 2号泵"},
        ],
    },
    ("submit_pick_from_s01", "position"): {"description": "S01 上料过渡仓取料位置，范围 1–6。"},
    ("submit_place_to_s02", "position"): {"description": "S02 TIP 盒放料位，范围 1–6。"},
    ("submit_pick_from_s02", "position"): {"description": "S02 TIP 盒取料位，范围 1–6。"},
    ("submit_place_to_s03", "position"): {"description": "S03 空容器仓位，范围 1–18；前端使用“行-列”格式，例如 1-1。"},
    ("submit_pick_from_s03", "position"): {"description": "S03 空容器仓位，范围 1–18；前端使用“行-列”格式，例如 1-1。"},
    ("submit_place_to_s04", "position"): {"description": "S04 磁搅工位编号，范围 1–6。"},
    ("submit_pick_from_s04", "position"): {"description": "S04 磁搅工位编号，范围 1–6。"},
    ("submit_place_to_s071", "position"): {
        "description": "S071 粉罐仓位，PLC 编号范围 1–6；前端使用“行-列”格式，填 auto 时自动选择空位。"
    },
    ("submit_pick_from_s071", "position"): {"description": "S071 粉罐仓位，PLC 编号范围 1–6；前端使用“行-列”格式，例如 1-1。"},
    ("submit_place_to_s072", "position"): {"description": "兼容参数；S072 产品类型由 S072取放料产品 决定。"},
    ("submit_pick_from_s072", "position"): {"description": "兼容参数；S072 产品类型由 S072取放料产品 决定。"},
    ("submit_place_to_s08", "position"): {"description": "S08 开关盖工位：1=样品瓶，2=100 mL 液体瓶。"},
    ("submit_pick_from_s08", "position"): {"description": "S08 开关盖工位：1=样品瓶，2=100 mL 液体瓶。"},
    ("submit_place_to_s10", "position"): {"description": "S10 液体试剂瓶仓位，范围 1–20。"},
    ("submit_pick_from_s10", "position"): {"description": "S10 液体试剂瓶仓位，范围 1–20。"},
    ("submit_place_to_s11", "position"): {"description": "S11 成品仓位，范围 1–18；前端使用“行-列”格式，例如 1-1。"},
    ("submit_pick_from_s11", "position"): {"description": "S11 成品仓位，范围 1–18；前端使用“行-列”格式，例如 1-1。"},
    ("rotate_powder_cartridge_to_feed", "position"): {
        "description": "旋转到 S07 上料位的粉罐位置，范围 1–10。"
    },
}

_ROBOT_TASK_NUMBERS = {
    "submit_pick_from_s01": 1,
    "submit_place_to_s02": 3,
    "submit_pick_from_s02": 4,
    "submit_place_to_s03": 5,
    "submit_pick_from_s03": 6,
    "submit_place_to_s04": 7,
    "submit_pick_from_s04": 8,
    "submit_place_to_s071": 13,
    "submit_pick_from_s071": 14,
    "submit_place_to_s072": 15,
    "submit_pick_from_s072": 16,
    "submit_place_to_s08": 17,
    "submit_pick_from_s08": 18,
    "submit_place_to_s09": 19,
    "submit_pick_from_s09": 20,
    "submit_place_to_s10": 21,
    "submit_pick_from_s10": 22,
    "submit_place_to_s11": 23,
    "submit_pick_from_s11": 24,
    "submit_pour_from_s08": 25,
}
_ROBOT_PARAM_PLC_VARIABLES = {
    ("submit_pick_from_s01", "product_type"): "S01出入料产品",
    ("submit_pick_from_s01", "position"): "S01取放料编号",
    **{
        (method, "position"): variable
        for method, variable in (
            ("submit_place_to_s02", "S02取放料编号"),
            ("submit_pick_from_s02", "S02取放料编号"),
            ("submit_place_to_s03", "S03取放料编号"),
            ("submit_pick_from_s03", "S03取放料编号"),
            ("submit_place_to_s04", "S04取放料编号"),
            ("submit_pick_from_s04", "S04取放料编号"),
            ("submit_place_to_s071", "S071取放料编号"),
            ("submit_pick_from_s071", "S071取放料编号"),
            ("submit_place_to_s08", "S08取放料编号"),
            ("submit_pick_from_s08", "S08取放料编号"),
            ("submit_place_to_s09", "S09取放料编号"),
            ("submit_pick_from_s09", "S09取放料编号"),
            ("submit_place_to_s10", "S10取放料编号"),
            ("submit_pick_from_s10", "S10取放料编号"),
            ("submit_place_to_s11", "S11取放料编号"),
            ("submit_pick_from_s11", "S11取放料编号"),
        )
    },
    **{
        (method, "product_type"): variable
        for method, variable in (
            ("submit_place_to_s03", "S03取放料产品"),
            ("submit_pick_from_s03", "S03取放料产品"),
            ("submit_place_to_s072", "S072取放料产品"),
            ("submit_pick_from_s072", "S072取放料产品"),
            ("submit_place_to_s08", "S08取放料产品"),
            ("submit_pick_from_s08", "S08取放料产品"),
            ("submit_place_to_s09", "S09取放料产品"),
            ("submit_pick_from_s09", "S09取放料产品"),
            ("submit_place_to_s11", "S11取放料产品"),
            ("submit_pick_from_s11", "S11取放料产品"),
            ("submit_pour_from_s08", "S08倒料产品选择"),
        )
    },
}


def _robot_parameter_context(method: str, name: str) -> str:
    task_number = _ROBOT_TASK_NUMBERS.get(method)
    if task_number is None:
        return ""
    parts: list[str] = []
    plc_variable = _ROBOT_PARAM_PLC_VARIABLES.get((method, name))
    if plc_variable:
        parts.append(f"对应 PLC 变量：{plc_variable}")
    parts.append(f"机器人任务号：{task_number}")
    return "；".join(parts) + "。"


def _docstring_param_help(docstring: str | None) -> dict[str, dict[str, str]]:
    help_by_name: dict[str, dict[str, str]] = {}
    for line in str(docstring or "").splitlines():
        match = re.match(r"\s*([^\s:\[]+)(?:\[([^\]]+)\])?\s*:\s*(.+)", line)
        if match:
            name, label, description = match.groups()
            help_by_name[name] = {"description": description.strip()}
            if label:
                help_by_name[name]["label"] = label.strip()
    return help_by_name


def _inferred_parameter_help(name: str) -> dict[str, Any]:
    if re.fullmatch(r"S09液体瓶[1-5]剩余液量", name):
        return {
            "label": name,
            "description": "可选：覆盖该 S09 液体瓶执行前的剩余液量；留空则读取 PLC 当前值。",
            "unit": "mL",
        }
    return {}


def _params_from_ast_action(method: str, method_info: dict[str, Any]) -> list[dict[str, Any]]:
    action_args = method_info.get("action_args") or {}
    handles = action_args.get("handles") or []
    doc_help = _docstring_param_help(method_info.get("docstring"))
    params = []
    for param in method_info.get("params", []):
        name = param.get("name")
        if not name:
            continue
        handle = _find_action_handle_for_param(handles, name)
        item = {
            "name": name,
            "label": (handle or {}).get("label") or name,
            "type": _json_type_from_python_type(param.get("type")),
        }
        for key, value in _PARAM_HELP_BY_NAME.get(name, {}).items():
            item.setdefault(key, value)
        for key, value in _inferred_parameter_help(name).items():
            item.setdefault(key, value)
        item.update(doc_help.get(name, {}))
        item.update(_METHOD_PARAM_HELP.get((method, name), {}))
        description = (handle or {}).get("description") or item.get("description")
        if description:
            item["description"] = description
            item.update(_range_from_description(description))
        else:
            item["description"] = f"{method} 动作参数 {name}；请按设备工艺定义填写。"
        robot_context = _robot_parameter_context(method, name)
        if robot_context:
            item["description"] = f"{item['description'].rstrip('。')}；{robot_context}"
        if not param.get("required", False) and "default" in param:
            default = param.get("default")
            if isinstance(default, dict) and "_call" in default:
                default = item.get("options", [{}])[0].get("value", 1)
            item["default"] = default
        params.append(item)
    return params


def _range_from_description(description: str) -> dict[str, int]:
    match = re.search(r"范围\s*[\[（(]?\s*(-?\d+)\s*[-~到,，]\s*(-?\d+)", description)
    if not match:
        return {}
    return {"min": int(match.group(1)), "max": int(match.group(2))}


def _find_action_handle_for_param(handles: Any, param_name: str) -> dict[str, Any] | None:
    if isinstance(handles, dict):
        handles = handles.values()
    if not isinstance(handles, list):
        return None
    for handle in handles:
        if isinstance(handle, dict) and handle.get("data_key") == param_name:
            return handle
    return None


def _json_type_from_python_type(python_type: str | None) -> str:
    type_name = str(python_type or "string")
    return {
        "int": "integer",
        "float": "number",
        "bool": "boolean",
        "str": "string",
    }.get(type_name, "string")


DEFAULT_PRESET = load_preset("ai4c")
SUPPORTED_ACTIONS = DEFAULT_PRESET.actions


@dataclass
class LogEvent:
    sequence: int
    message: str
    level: str = "info"
    category: str = "workflow"
    scope: str = "workflow"
    node_id: str | None = None
    detail: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "sequence": self.sequence,
            "message": self.message,
            "level": self.level,
            "category": self.category,
            "scope": self.scope,
            "node_id": self.node_id,
            "detail": self.detail,
        }


@dataclass
class RunRecord:
    run_id: str
    status: str = "pending"
    logs: list[str] = field(default_factory=list)
    log_events: list[LogEvent] = field(default_factory=list)
    result: list[dict[str, Any]] | None = None
    error: str | None = None
    node_statuses: dict[str, str] = field(default_factory=dict)
    cancel_requested: bool = False
    devices: dict[str, Any] = field(default_factory=dict)
    timing_report_path: str | None = None
    timing_summary_path: str | None = None

    def append_log(
        self,
        message: str,
        *,
        node_id: str | None = None,
        level: str = "info",
        category: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        self.logs.append(message)
        scope = "node" if node_id else "workflow"
        self.log_events.append(
            LogEvent(
                sequence=len(self.log_events) + 1,
                message=message,
                level=level,
                category=category or _infer_log_category(message, scope=scope, detail=detail),
                scope=scope,
                node_id=node_id,
                detail=detail,
            )
        )


def _infer_log_category(message: str, *, scope: str, detail: dict[str, Any] | None) -> str:
    detail_type = detail.get("type") if isinstance(detail, dict) else None
    if detail_type == "opc_wait":
        return "opc_wait"
    if "OPC" in message:
        if "采样" in message:
            return "opc_sample"
        if "变化" in message:
            return "opc_change"
        if "等待" in message:
            return "opc_wait"
        return "opc"
    if message.startswith("动作结果"):
        return "action_result"
    if "执行失败" in message or "节点执行失败" in message:
        return "error"
    if scope == "node":
        return "node"
    if "连接" in message or "设备图" in message or "CSV" in message:
        return "setup"
    return "workflow"


def _run_node_with_live_opc_sampling(
    node: WorkflowNode,
    devices: dict[str, Any],
    *,
    logger: WorkflowLogger,
    runtime_config: RuntimeConfig,
    sample_interval: float = 0.5,
) -> list[dict[str, Any]]:
    device_name = route_node_device(node, runtime_config)
    device = devices.get(device_name)
    if device is None:
        raise KeyError(f"未创建本地设备实例: {device_name}")

    method_name = method_name_from_template(node.name)
    snapshot_variables = collect_snapshot_variables(method_name, node.param, runtime_config)
    default_plc = devices.get(runtime_config.device_factory.plc_device_id)
    snapshot_client = default_plc or (device if hasattr(device, "get_variables") else None)
    if not snapshot_variables or snapshot_client is None or not hasattr(snapshot_client, "get_variables"):
        return run_nodes([node], devices, logger=logger, runtime_config=runtime_config)

    if not hasattr(device, method_name):
        raise AttributeError(f"{device_name} 不存在动作方法: {method_name}")
    before = snapshot_opc_state(snapshot_client, snapshot_variables) if snapshot_client is not None else {}

    logger.log(
        f"[1/1] {device_name}.{method_name}({node.param})",
        detail={"device_name": device_name, "method": method_name, "param": node.param},
    )
    if before:
        logger.log(
            f"OPC状态采样: {len(before)} 个变量",
            detail={"before": format_snapshot_detail(before, snapshot_client)},
        )

    stop_sampling = threading.Event()
    last_snapshot = dict(before)
    sampling_errors: list[Exception] = []
    skip_parallel_sampling = bool(runtime_config.device_factory.devices) and snapshot_client is device

    def sample_live_changes() -> None:
        nonlocal last_snapshot
        while not stop_sampling.wait(sample_interval):
            current = snapshot_opc_state(snapshot_client, snapshot_variables)
            diff_detail = build_snapshot_diff_detail(last_snapshot, current, plc=snapshot_client)
            last_snapshot = current
            if diff_detail["changes"]:
                logger.log(
                    f"OPC实时变化: {len(diff_detail['changes'])}/{len(current)} 个变量变化",
                    detail=diff_detail,
                )

    sampler: threading.Thread | None = None
    if snapshot_client is not None and snapshot_variables and not skip_parallel_sampling:
        sampler = threading.Thread(target=sample_live_changes, name="SzlabLiveOpcSampler", daemon=True)
        sampler.start()

    unbind_wait_logger = bind_opc_wait_logger(logger, default_plc, device, snapshot_client)
    try:
        result = getattr(device, method_name)(**node.param)
    finally:
        unbind_wait_logger()
        stop_sampling.set()
        if sampler is not None:
            sampler.join(timeout=max(sample_interval * 2, 0.1))

    after = snapshot_opc_state(snapshot_client, snapshot_variables) if snapshot_client is not None else {}
    if after:
        final_live_diff = build_snapshot_diff_detail(last_snapshot, after, plc=snapshot_client)
        if sampler is not None and final_live_diff["changes"]:
            logger.log(
                f"OPC实时变化: {len(final_live_diff['changes'])}/{len(after)} 个变量变化",
                detail=final_live_diff,
            )
        diff_detail = build_snapshot_diff_detail(before, after, plc=snapshot_client)
        logger.log(
            f"OPC状态变化: {len(diff_detail['changes'])}/{len(before)} 个变量变化",
            detail=diff_detail,
        )
    if sampling_errors:
        logger.log(f"OPC实时采样异常: {sampling_errors[-1]}", level="warning")
    for wait_log in iter_opc_wait_logs(default_plc, device, snapshot_client):
        logger.log(wait_log["message"], detail=wait_log.get("detail"))
    logger.log(f"动作结果: {result}", detail={"result": result})

    output = {
        "uuid": node.uuid,
        "device_name": device_name,
        "method": method_name,
        "param": node.param,
        "opc_before": before,
        "opc_after": after,
        "result": result,
    }
    if isinstance(result, dict) and result.get("success") is False:
        raise RuntimeError(f"动作失败: {device_name}.{method_name}: {result}")
    return [output]


class WorkflowRunManager:
    def __init__(
        self,
        preset: WorkflowPreset,
        runtime_config: RuntimeConfig,
        *,
        timing_enabled: bool = False,
    ) -> None:
        self._preset = preset
        self._runtime_config = runtime_config
        self._timing_enabled = timing_enabled
        self._lock = threading.RLock()
        self._sensor_event_condition = threading.Condition(self._lock)
        self._records: dict[str, RunRecord] = {}
        self._active_run_id: str | None = None
        self._cached_device_key: tuple[Any, ...] | None = None
        self._cached_devices: dict[str, Any] = {}
        self._stack_status_cache: tuple[float, dict[str, Any]] | None = None
        self._sensor_arrays_cache: tuple[float, dict[str, Any]] | None = None
        self._sensor_event_version = 0
        self._sensor_event_plc: Any = None

    def start(self, payload: dict[str, Any]) -> RunRecord:
        with self._lock:
            if self._active_run_id:
                active = self._records.get(self._active_run_id)
                if active and active.status in {"pending", "preparing", "running", "cancelling"}:
                    raise RuntimeError("已有 workflow 正在运行，请等待结束后再启动")

            run_id = uuid.uuid4().hex
            record = RunRecord(run_id=run_id)
            record.append_log("已创建运行任务，等待后台启动...")
            self._records[run_id] = record
            self._active_run_id = run_id

        thread = threading.Thread(
            target=self._run_payload,
            args=(run_id, payload),
            daemon=True,
            name=f"szlab-workflow-{run_id[:8]}",
        )
        thread.start()
        return record

    def get(self, run_id: str) -> RunRecord | None:
        with self._lock:
            return self._records.get(run_id)

    def cancel(self, run_id: str) -> RunRecord:
        with self._lock:
            record = self._records.get(run_id)
            if record is None:
                raise KeyError("运行记录不存在")
            if record.status in {"completed", "failed", "cancelled"}:
                return record

            record.cancel_requested = True
            record.status = "cancelling"
            for node_id, node_status in list(record.node_statuses.items()):
                if node_status in {"idle", "preparing", "running"}:
                    record.node_statuses[node_id] = "cancelled"
            record.append_log("收到终止请求，正在停止当前 workflow...")
            devices = record.devices

        self._disconnect_cached_devices(devices, record.append_log)
        return record

    def shutdown(self) -> None:
        self._disconnect_cached_devices()

    def get_live_devices(self) -> dict[str, Any]:
        with self._lock:
            if self._cached_devices:
                return self._cached_devices

        default_config = self._preset.default_config
        csv_value = str(default_config.get("csv") or "").strip()
        csv_path = _resolve_ui_path(csv_value, self._preset) if csv_value else None
        timeout = float(default_config.get("timeout") or 300.0)
        no_subscription = bool(default_config.get("no_subscription", True))
        graph_value = str(default_config.get("graph") or GENERATED_GRAPH_SENTINEL).strip()
        opcua_url = str(default_config.get("url") or "").strip()

        if graph_value == GENERATED_GRAPH_SENTINEL:
            if not opcua_url:
                raise ValueError("生成设备图需要填写 OPC UA URL，或指定已有 graph JSON")
            generated_graph = build_local_device_graph(
                opcua_url=opcua_url,
                csv_path=str(csv_path or csv_value or default_config.get("csv") or ""),
                use_subscription=not no_subscription,
                preset=self._preset,
            )
            graph_file = _write_temp_json(generated_graph)
        else:
            graph_file = _resolve_ui_path(graph_value, self._preset)

        device_key = (
            self._preset.id,
            graph_value,
            opcua_url,
            str(csv_path or ""),
            no_subscription,
            timeout,
        )
        return self._get_or_create_devices(
            device_key,
            {
                "graph_file": graph_file,
                "opcua_url": opcua_url or None,
                "csv_path": csv_path,
                "use_subscription": False if no_subscription else None,
                "plc_action_timeout": timeout,
                "runtime_config": self._runtime_config,
            },
            lambda message: None,
        )

    def ensure_sensor_event_subscription(self) -> None:
        devices = self.get_live_devices()
        plc_device_id = self._runtime_config.device_factory.plc_device_id or "szlab_poly_plc"
        plc = devices.get(plc_device_id) or devices.get("szlab_poly_plc")
        if plc is None or not hasattr(plc, "start_sensor_array_subscription"):
            raise RuntimeError("当前设备图中的 PLC 不支持传感器变化订阅")
        with self._lock:
            if self._sensor_event_plc is plc:
                return
        plc.start_sensor_array_subscription(self._on_sensor_array_change)
        with self._lock:
            self._sensor_event_plc = plc

    def _on_sensor_array_change(self, group_index: int, values: list[bool]) -> None:
        del group_index, values
        with self._sensor_event_condition:
            self._stack_status_cache = None
            self._sensor_arrays_cache = None
            self._sensor_event_version += 1
            self._sensor_event_condition.notify_all()

    def sensor_event_version(self) -> int:
        with self._lock:
            return self._sensor_event_version

    def wait_for_sensor_change(self, version: int, timeout: float = 15.0) -> int:
        with self._sensor_event_condition:
            self._sensor_event_condition.wait_for(
                lambda: self._sensor_event_version != version,
                timeout=timeout,
            )
            return self._sensor_event_version

    def _get_or_create_devices(
        self,
        device_key: tuple[Any, ...],
        create_kwargs: dict[str, Any],
        log: Any,
    ) -> dict[str, Any]:
        with self._lock:
            if self._cached_devices and self._cached_device_key == device_key:
                log("复用已连接的 OPC UA 设备，跳过重新连接和节点加载")
                return self._cached_devices
            previous_devices = self._cached_devices
            self._cached_devices = {}
            self._cached_device_key = None
            self._stack_status_cache = None
            self._sensor_arrays_cache = None
            self._sensor_event_plc = None

        if previous_devices:
            _disconnect_devices(previous_devices, log)

        devices = create_local_devices(**create_kwargs)
        with self._lock:
            self._cached_devices = devices
            self._cached_device_key = device_key
            self._stack_status_cache = None
            self._sensor_arrays_cache = None
        return devices

    def _disconnect_cached_devices(self, devices: dict[str, Any] | None = None, log: Any = None) -> None:
        with self._lock:
            target_devices = devices or self._cached_devices
            if not target_devices or target_devices is not self._cached_devices:
                return
            self._cached_devices = {}
            self._cached_device_key = None
            self._stack_status_cache = None
            self._sensor_arrays_cache = None
            self._sensor_event_plc = None

        _disconnect_devices(target_devices, log)

    def get_stack_status(self) -> dict[str, Any]:
        now = time.monotonic()
        with self._lock:
            if self._stack_status_cache and now - self._stack_status_cache[0] < 2.0:
                return self._stack_status_cache[1]

        devices = self.get_live_devices()
        plc_device_id = self._runtime_config.device_factory.plc_device_id or "szlab_poly_plc"
        plc = devices.get(plc_device_id) or devices.get("szlab_poly_plc")
        if plc is None or not hasattr(plc, "get_stack_status"):
            status = {
                "success": False,
                "schema": "szlab_poly_studio.stack_status.v1",
                "message": "当前设备图中没有可读取堆栈状态的 PLC 设备",
                "stacks": {},
            }
        else:
            group_names = self._preset.default_config.get("stack_status_groups")
            status = plc.get_stack_status(group_names=group_names)

        with self._lock:
            self._stack_status_cache = (time.monotonic(), status)
        return status

    def get_sensor_arrays(self) -> dict[str, Any]:
        now = time.monotonic()
        with self._lock:
            if self._sensor_arrays_cache and now - self._sensor_arrays_cache[0] < 1.0:
                return self._sensor_arrays_cache[1]

        devices = self.get_live_devices()
        plc_device_id = self._runtime_config.device_factory.plc_device_id or "szlab_poly_plc"
        plc = devices.get(plc_device_id) or devices.get("szlab_poly_plc")
        if plc is None or not hasattr(plc, "get_sensor_arrays"):
            status = {
                "success": False,
                "schema": "szlab_poly_studio.sensor_arrays.v1",
                "message": "当前设备图中没有可读取实机传感器数组的 PLC 设备",
                "groups": [],
            }
        else:
            status = plc.get_sensor_arrays()

        with self._lock:
            self._sensor_arrays_cache = (time.monotonic(), status)
        return status

    def _run_payload(self, run_id: str, payload: dict[str, Any]) -> None:
        record = self.get(run_id)
        if record is None:
            return

        workflow_path: Path | None = None
        graph_path: Path | None = None
        devices: dict[str, Any] = {}
        timing_recorder: WorkflowTimingRecorder | None = None
        with self._lock:
            record.status = "preparing"
        record.append_log("后台任务已启动，准备解析 workflow...")

        try:
            workflow = payload.get("workflow")
            if not isinstance(workflow, dict):
                raise ValueError("缺少 workflow JSON")
            if self._timing_enabled:
                timing_recorder = WorkflowTimingRecorder(
                    run_id=run_id,
                    workflow_name=str(workflow.get("name") or "local_workflow"),
                    output_dir=REPO_ROOT / "workflow_timings",
                )
            record.node_statuses = {
                str(node.get("uuid")): "preparing"
                for node in workflow.get("nodes", [])
                if node.get("uuid")
            }

            workflow_path = _write_temp_workflow(workflow)
            nodes, edges = load_workflow_nodes(workflow_path)
            ordered_nodes = build_execution_order(nodes, edges)
            record.append_log(f"workflow 解析完成，共 {len(ordered_nodes)} 个待执行节点")
            if record.cancel_requested:
                raise WorkflowCancelled("workflow 已终止")
            default_config = self._preset.default_config
            csv_value = str(payload.get("csv") or default_config.get("csv") or "").strip()
            csv_path = _resolve_ui_path(csv_value, self._preset) if csv_value else None
            timeout = float(payload.get("timeout") or default_config.get("timeout") or 300.0)
            write_allowed_timeout = float(
                payload.get("write_allowed_timeout")
                or default_config.get("write_allowed_timeout")
                or 5.0
            )
            no_subscription = bool(payload.get("no_subscription", default_config.get("no_subscription", True)))
            graph_value = str(payload.get("graph") or default_config.get("graph") or GENERATED_GRAPH_SENTINEL).strip()
            opcua_url = str(payload.get("url") or default_config.get("url") or "").strip()

            if graph_value == GENERATED_GRAPH_SENTINEL:
                if not opcua_url:
                    raise ValueError("生成设备图需要填写 OPC UA URL，或指定已有 graph JSON")
                generated_graph = build_local_device_graph(
                    opcua_url=opcua_url,
                    csv_path=str(csv_path or csv_value or default_config.get("csv") or ""),
                    timeout=timeout,
                    write_allowed_timeout=write_allowed_timeout,
                    use_subscription=not no_subscription,
                    preset=self._preset,
                )
                graph_path = _write_temp_json(generated_graph)
                graph_file = graph_path
            else:
                graph_file = _resolve_ui_path(graph_value, self._preset)

            record.append_log(f"加载设备图: {graph_file}")
            if csv_path is not None:
                record.append_log(f"使用 CSV: {csv_path}")

            record.append_log("正在连接 OPC UA 并加载设备节点，这一步可能需要一些时间...")
            device_key = (
                self._preset.id,
                graph_value,
                opcua_url,
                str(csv_path or ""),
                no_subscription,
                timeout,
            )
            devices = self._get_or_create_devices(
                device_key,
                {
                    "graph_file": graph_file,
                    "opcua_url": opcua_url or None,
                    "csv_path": csv_path,
                    "use_subscription": False if no_subscription else None,
                    "plc_action_timeout": timeout,
                    "runtime_config": self._runtime_config,
                },
                record.append_log,
            )
            record.devices = devices
            if record.cancel_requested:
                raise WorkflowCancelled("workflow 已终止")
            record.append_log("设备连接完成，开始执行 workflow")
            if timing_recorder is not None:
                timing_recorder.mark_execution_started()
            with self._lock:
                record.status = "running"
            results: list[dict[str, Any]] = []
            for node_index, node in enumerate(ordered_nodes, start=1):
                if record.cancel_requested:
                    raise WorkflowCancelled("workflow 已终止")
                record.node_statuses[node.uuid] = "running"
                node_method = node.name.removeprefix("auto-")
                device_name = route_node_device(node, self._runtime_config)
                if timing_recorder is not None:
                    timing_recorder.start_step(
                        index=node_index,
                        total=len(ordered_nodes),
                        node_id=node.uuid,
                        device_name=device_name,
                        method=node_method,
                        params=node.param,
                    )
                record.append_log(
                    f"开始执行节点 {node.uuid}: {node_method}",
                    node_id=node.uuid,
                    detail={"method": node_method, "params": node.param},
                )

                def append_node_log(
                    message: str,
                    *,
                    level: str = "info",
                    detail: dict[str, Any] | None = None,
                    node_id: str = node.uuid,
                ) -> None:
                    record.append_log(message, node_id=node_id, level=level, detail=detail)
                    if timing_recorder is not None:
                        timing_recorder.observe_log(message, detail)

                logger = WorkflowLogger(writer=append_node_log)
                try:
                    node_results = _run_node_with_live_opc_sampling(
                        node,
                        devices,
                        logger=logger,
                        runtime_config=self._runtime_config,
                    )
                    results.extend(node_results)
                except Exception as exc:
                    if timing_recorder is not None:
                        timing_recorder.finish_step(error=str(exc))
                    record.node_statuses[node.uuid] = "failed"
                    record.append_log(f"节点执行失败: {exc}", node_id=node.uuid, level="error")
                    raise
                if timing_recorder is not None:
                    timing_recorder.finish_step(result=node_results)
                record.node_statuses[node.uuid] = "success"
                record.append_log(f"节点执行完成 {node.uuid}", node_id=node.uuid)
                if record.cancel_requested:
                    raise WorkflowCancelled("workflow 已终止")
            record.result = results
            record.append_log(f"本地 workflow 执行完成，共 {len(record.result)} 个节点")
            with self._lock:
                record.status = "completed"
        except WorkflowCancelled as exc:
            record.error = str(exc)
            record.append_log(str(exc))
            with self._lock:
                record.status = "cancelled"
        except Exception as exc:
            record.error = str(exc)
            record.append_log(f"执行失败: {exc}")
            with self._lock:
                record.status = "failed"
        finally:
            if timing_recorder is not None:
                try:
                    report_path = timing_recorder.finish(status=record.status, error=record.error)
                    record.timing_report_path = str(report_path)
                    record.append_log(f"排程计时报告已保存: {report_path}")
                    if timing_recorder.summary_path is not None:
                        record.timing_summary_path = str(timing_recorder.summary_path)
                        record.append_log(f"排程时间轴已保存: {timing_recorder.summary_path}")
                except Exception as exc:
                    record.append_log(f"排程计时报告保存失败: {exc}", level="warning")
            if record.cancel_requested:
                self._disconnect_cached_devices(devices)
            record.devices = {}
            if workflow_path is not None:
                workflow_path.unlink(missing_ok=True)
            if graph_path is not None:
                graph_path.unlink(missing_ok=True)
            with self._lock:
                if self._active_run_id == run_id:
                    self._active_run_id = None


class WorkflowCancelled(RuntimeError):
    pass


def build_linear_workflow(
    steps: list[dict[str, Any]],
    name: str = "szlab_local_workflow",
    preset: WorkflowPreset = DEFAULT_PRESET,
) -> dict[str, Any]:
    """将前端线性步骤转换为 UniLab workflow JSON。"""
    if not steps:
        raise ValueError("至少需要一个 workflow 步骤")

    nodes = []
    for index, step in enumerate(steps, start=1):
        method = str(step.get("method", "")).strip()
        if method not in preset.actions:
            raise ValueError(f"不支持的动作: {method}")

        spec = preset.actions[method]
        params = _build_action_params(spec, dict(step.get("params") or step.get("param") or {}))

        nodes.append(
            {
                "uuid": f"step_{index:03d}_{method}",
                "name": f"auto-{method}",
                "device_name": spec.device_id or preset.target_device_id,
                "param": params,
            }
        )

    edges = [
        {
            "source_node_uuid": nodes[index]["uuid"],
            "target_node_uuid": nodes[index + 1]["uuid"],
        }
        for index in range(len(nodes) - 1)
    ]
    return {"name": name or preset.default_workflow_name, "nodes": nodes, "edges": edges}


def build_graph_workflow(
    flow_nodes: list[dict[str, Any]],
    flow_edges: list[dict[str, Any]],
    name: str = "szlab_canvas_workflow",
    preset: WorkflowPreset = DEFAULT_PRESET,
) -> dict[str, Any]:
    """将 React Flow 画板节点和边转换为 UniLab workflow JSON。"""
    if not flow_nodes:
        raise ValueError("至少需要一个 workflow 节点")

    nodes_by_id: dict[str, dict[str, Any]] = {}
    original_index: dict[str, int] = {}
    for index, flow_node in enumerate(flow_nodes):
        node_id = str(flow_node.get("id", "")).strip()
        if not node_id:
            raise ValueError("workflow 节点缺少 id")
        if node_id in nodes_by_id:
            raise ValueError(f"workflow 节点 id 重复: {node_id}")
        nodes_by_id[node_id] = flow_node
        original_index[node_id] = index

    outgoing: dict[str, list[str]] = {node_id: [] for node_id in nodes_by_id}
    incoming_count: dict[str, int] = {node_id: 0 for node_id in nodes_by_id}
    workflow_edges: list[dict[str, str]] = []
    for edge in flow_edges:
        source = str(edge.get("source", "")).strip()
        target = str(edge.get("target", "")).strip()
        if source not in nodes_by_id or target not in nodes_by_id:
            raise ValueError(f"连线引用了不存在的节点: {source} -> {target}")
        outgoing[source].append(target)
        incoming_count[target] += 1
        workflow_edges.append({"source_node_uuid": source, "target_node_uuid": target})

    ready = sorted(
        [node_id for node_id, count in incoming_count.items() if count == 0],
        key=lambda node_id: original_index[node_id],
    )
    ordered_ids: list[str] = []
    while ready:
        current = ready.pop(0)
        ordered_ids.append(current)
        for target in sorted(outgoing[current], key=lambda node_id: original_index[node_id]):
            incoming_count[target] -= 1
            if incoming_count[target] == 0:
                ready.append(target)
        ready.sort(key=lambda node_id: original_index[node_id])

    if len(ordered_ids) != len(nodes_by_id):
        raise ValueError("workflow 不能包含环，请删除形成循环依赖的连线")

    workflow_nodes = [_build_workflow_node_from_flow_node(nodes_by_id[node_id], preset) for node_id in ordered_ids]
    return {"name": name or preset.default_workflow_name, "nodes": workflow_nodes, "edges": workflow_edges}


def build_local_device_graph(
    opcua_url: str,
    csv_path: str = "",
    timeout: float | int | None = None,
    write_allowed_timeout: float | int | None = None,
    use_subscription: bool = True,
    preset: WorkflowPreset = DEFAULT_PRESET,
) -> dict[str, Any]:
    """根据页面运行配置和 preset 生成本地设备图。"""
    if not opcua_url:
        raise ValueError("缺少 OPC UA URL")

    graph = _render_template_value(
        preset.device_graph,
        {
            "opcua_url": opcua_url,
            "csv_path": csv_path,
            "timeout": timeout if timeout is not None else preset.default_config.get("timeout", 300),
            "write_allowed_timeout": (
                write_allowed_timeout
                if write_allowed_timeout is not None
                else preset.default_config.get("write_allowed_timeout", 5.0)
            ),
            "use_subscription": use_subscription,
        },
    )
    if not csv_path:
        for node in graph.get("nodes", []):
            config = node.get("config")
            if isinstance(config, dict):
                config.pop("csv_path", None)
    else:
        for node in graph.get("nodes", []):
            config = node.get("config")
            if isinstance(config, dict) and config.get("url") == opcua_url:
                config["csv_path"] = csv_path
    return graph


def _load_preset_runtime_config(preset: WorkflowPreset) -> RuntimeConfig:
    if preset.runtime_config:
        return load_runtime_config(_resolve_ui_path(preset.runtime_config, preset))
    return load_runtime_config()


def _runtime_supported_actions(preset: WorkflowPreset, runtime_config: RuntimeConfig) -> dict[str, ActionSpec]:
    device_ids = set(runtime_config.device_factory.devices)
    if not device_ids:
        return preset.actions
    return {
        method: action
        for method, action in preset.actions.items()
        if (action.device_id or preset.target_device_id) in device_ids
    }


def _preset_for_runtime(preset: WorkflowPreset, runtime_config: RuntimeConfig) -> WorkflowPreset:
    actions = _runtime_supported_actions(preset, runtime_config)
    if actions is preset.actions:
        return preset
    return WorkflowPreset(
        id=preset.id,
        title=preset.title,
        target_device_id=preset.target_device_id,
        target_device_ids=preset.target_device_ids,
        runtime_config=preset.runtime_config,
        default_workflow_name=preset.default_workflow_name,
        default_config=preset.default_config,
        debug_config=preset.debug_config,
        path_roots=preset.path_roots,
        device_graph=preset.device_graph,
        actions=actions,
        base_dir=preset.base_dir,
    )


def create_app(
    preset_name: str = "ai4c",
    runtime_config: RuntimeConfig | None = None,
    *,
    timing_enabled: bool = False,
) -> FastAPI:
    preset = load_preset(preset_name)
    runtime_config = runtime_config or _load_preset_runtime_config(preset)
    active_preset = _preset_for_runtime(preset, runtime_config)
    app = FastAPI(title="szlab Workflow Debugger")
    manager = WorkflowRunManager(active_preset, runtime_config, timing_enabled=timing_enabled)
    _register_shutdown_handler(app, manager.shutdown)

    assets_dir = FRONTEND_DIST_DIR / "assets"
    if assets_dir.exists():
        app.mount("/assets", StaticFiles(directory=assets_dir), name="szlab_workflow_assets")

    @app.get("/", response_class=HTMLResponse)
    async def index() -> Response:
        return _frontend_entry_response()

    @app.get("/api/actions", response_class=JSONResponse)
    async def list_actions() -> dict[str, Any]:
        return {"actions": [_action_to_dict(action, runtime_config) for action in active_preset.actions.values()]}

    @app.get("/api/preset", response_class=JSONResponse)
    async def get_preset() -> dict[str, Any]:
        return {
            "id": preset.id,
            "title": preset.title,
            "runtime_config": preset.runtime_config,
            "default_workflow_name": active_preset.default_workflow_name,
            "default_config": active_preset.default_config,
            "actions": [_action_to_dict(action, runtime_config) for action in active_preset.actions.values()],
        }

    @app.get("/api/csv-variables", response_class=JSONResponse)
    async def csv_variables(csv_path: str = "") -> dict[str, Any]:
        value = csv_path.strip() or str(active_preset.default_config.get("csv") or "").strip()
        path = _resolve_ui_path(value, active_preset) if value else None
        if path is None or not path.exists():
            return {"variables": []}
        raw = path.read_bytes()
        text = ""
        for encoding in ("utf-8-sig", "utf-16", "gb18030"):
            try:
                text = raw.decode(encoding)
                break
            except UnicodeDecodeError:
                continue
        if not text:
            raise HTTPException(status_code=400, detail=f"无法识别 CSV 文件编码: {path.name}")
        sample = text[:4096]
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",\t")
        except csv.Error:
            dialect = csv.excel
        rows = csv.DictReader(io.StringIO(text), dialect=dialect)
        variables = [
            {
                "name": (row.get("变量名") or "").strip(),
                "data_type": (row.get("数据类型") or "STRING").strip(),
                "initial_value": (row.get("初始值") or "").strip(),
                "comment": (row.get("注释") or "").strip(),
            }
            for row in rows
            if (row.get("变量名") or "").strip() and (row.get("数据类型") or "").strip()
        ]
        return {"variables": variables}

    @app.get("/api/stack-status", response_class=JSONResponse)
    async def get_stack_status() -> dict[str, Any]:
        try:
            return manager.get_stack_status()
        except Exception as exc:
            return {
                "success": False,
                "schema": "szlab_poly_studio.stack_status.v1",
                "message": str(exc),
                "stacks": {},
            }

    @app.get("/api/sensor-arrays", response_class=JSONResponse)
    async def get_sensor_arrays() -> dict[str, Any]:
        try:
            return manager.get_sensor_arrays()
        except Exception as exc:
            return {
                "success": False,
                "schema": "szlab_poly_studio.sensor_arrays.v1",
                "message": str(exc),
                "groups": [],
            }

    @app.get("/api/sensor-events")
    async def stream_sensor_events() -> StreamingResponse:
        async def event_stream():
            version: int | None = None
            while True:
                try:
                    await asyncio.to_thread(manager.ensure_sensor_event_subscription)
                    current_version = manager.sensor_event_version()
                    if version is None:
                        version = current_version
                        yield f"event: sensor-change\ndata: {json.dumps({'version': version})}\n\n"

                    next_version = await asyncio.to_thread(
                        manager.wait_for_sensor_change,
                        version,
                        15.0,
                    )
                    if next_version == version:
                        yield ": keepalive\n\n"
                        continue
                    version = next_version
                    yield f"event: sensor-change\ndata: {json.dumps({'version': version})}\n\n"
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    payload = json.dumps({"message": str(exc)}, ensure_ascii=False)
                    yield f"event: subscription-error\ndata: {payload}\n\n"
                    await asyncio.sleep(5.0)

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    @app.post("/api/workflow/build", response_class=JSONResponse)
    async def build_workflow(payload: dict[str, Any]) -> dict[str, Any]:
        try:
            return build_linear_workflow(
                payload.get("steps") or [],
                name=payload.get("name") or active_preset.default_workflow_name,
                preset=active_preset,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @app.post("/api/workflow/build-graph", response_class=JSONResponse)
    async def build_graph(payload: dict[str, Any]) -> dict[str, Any]:
        try:
            return build_graph_workflow(
                flow_nodes=payload.get("nodes") or [],
                flow_edges=payload.get("edges") or [],
                name=payload.get("name") or active_preset.default_workflow_name,
                preset=active_preset,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @app.post("/api/run", response_class=JSONResponse)
    async def run(payload: dict[str, Any]) -> dict[str, Any]:
        try:
            record = manager.start(payload)
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        return _record_to_dict(record)

    @app.get("/api/run/{run_id}", response_class=JSONResponse)
    async def get_run(run_id: str) -> dict[str, Any]:
        record = manager.get(run_id)
        if record is None:
            raise HTTPException(status_code=404, detail="运行记录不存在")
        return _record_to_dict(record)

    @app.post("/api/run/{run_id}/cancel", response_class=JSONResponse)
    async def cancel_run(run_id: str) -> dict[str, Any]:
        try:
            record = manager.cancel(run_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="运行记录不存在")
        return _record_to_dict(record)

    @app.get("/{path:path}", response_class=HTMLResponse)
    async def spa_fallback(path: str) -> Response:
        if path.startswith("api/"):
            raise HTTPException(status_code=404, detail="接口不存在")
        return _frontend_entry_response()

    return app


def _register_shutdown_handler(app: FastAPI, handler: Any) -> None:
    if hasattr(app, "add_event_handler"):
        app.add_event_handler("shutdown", handler)
        return
    if hasattr(app, "on_event"):
        app.on_event("shutdown")(handler)
        return
    raise RuntimeError("当前 FastAPI 版本不支持注册 shutdown 事件")


def start_ui(
    host: str = "127.0.0.1",
    port: int = 8014,
    open_browser: bool = True,
    preset_name: str = "ai4c",
    runtime_config: RuntimeConfig | None = None,
    timing_enabled: bool = False,
) -> None:
    import uvicorn

    url = f"http://{host if host != '0.0.0.0' else 'localhost'}:{port}/"
    if open_browser:
        webbrowser.open(url)
    uvicorn.run(
        create_app(
            preset_name=preset_name,
            runtime_config=runtime_config,
            timing_enabled=timing_enabled,
        ),
        host=host,
        port=port,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run szlab workflow runner service.")
    parser.add_argument("--host", default="0.0.0.0", help="服务监听地址")
    parser.add_argument("--port", type=int, default=8000, help="服务监听端口")
    parser.add_argument("--preset", default="ai4c", help="服务使用的 workflow preset 名称或 JSON 路径")
    parser.add_argument("--runtime-config", type=Path, default=None, help="覆盖 preset 中的运行配置 JSON")
    parser.add_argument("--open-browser", action="store_true", help="服务启动后自动打开浏览器")
    parser.add_argument("--debug", action="store_true", help="启用 preset.debug_config 中定义的调试环境变量")
    parser.add_argument("--timing", action="store_true", help="临时记录 workflow 排程耗时")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    start_ui(
        host=args.host,
        port=args.port,
        open_browser=args.open_browser,
        preset_name=args.preset,
        runtime_config=load_runtime_config(args.runtime_config) if args.runtime_config else None,
        timing_enabled=args.timing,
    )
    return 0


def _build_action_params(spec: ActionSpec, raw_params: dict[str, Any]) -> dict[str, Any]:
    params: dict[str, Any] = {}
    for param_spec in spec.params:
        name = str(param_spec.get("name", "")).strip()
        if not name:
            continue
        value = raw_params.get(name, param_spec.get("default"))
        if param_spec.get("type") == "integer":
            try:
                value = int(value)
            except (TypeError, ValueError) as exc:
                raise ValueError(_range_message(name, param_spec)) from exc
            minimum = param_spec.get("min")
            maximum = param_spec.get("max")
            if minimum is not None and value < int(minimum):
                raise ValueError(_range_message(name, param_spec))
            if maximum is not None and value > int(maximum):
                raise ValueError(_range_message(name, param_spec))
        params[name] = value
    return params


def _range_message(name: str, param_spec: dict[str, Any]) -> str:
    minimum = param_spec.get("min")
    maximum = param_spec.get("max")
    if minimum is not None and maximum is not None:
        return f"{name} 必须在 {minimum}-{maximum} 范围内"
    return f"{name} 参数无效"


def _build_workflow_node_from_flow_node(flow_node: dict[str, Any], preset: WorkflowPreset) -> dict[str, Any]:
    node_id = str(flow_node.get("id", "")).strip()
    data = flow_node.get("data") or {}
    method = str(data.get("method", "")).strip()
    if method not in preset.actions:
        raise ValueError(f"不支持的动作: {method}")

    spec = preset.actions[method]
    params = _build_action_params(spec, dict(data.get("params") or data.get("param") or {}))

    return {
        "uuid": node_id,
        "name": f"auto-{method}",
        "device_name": data.get("device_id") or spec.device_id or preset.target_device_id,
        "param": params,
    }


def _resolve_ui_path(path: str | Path, preset: WorkflowPreset = DEFAULT_PRESET) -> Path:
    candidate = Path(path)
    if candidate.is_absolute():
        return candidate

    root_candidates = [preset.base_dir / candidate]
    repo_root = REPO_ROOT
    for root in preset.path_roots:
        root_path = Path(root)
        if not root_path.is_absolute():
            root_path = repo_root / root_path
        root_candidates.append(root_path / candidate)

    for root_candidate in root_candidates:
        if root_candidate.exists():
            return root_candidate

    return root_candidates[0] if root_candidates else SZLAB_DIR / candidate


def _write_temp_workflow(workflow: dict[str, Any]) -> Path:
    return _write_temp_json(workflow)


def _write_temp_json(data: dict[str, Any]) -> Path:
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".json", delete=False) as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
        return Path(handle.name)


def _render_template_value(value: Any, replacements: dict[str, Any]) -> Any:
    if isinstance(value, str) and value.startswith("${") and value.endswith("}"):
        key = value[2:-1]
        return replacements.get(key, value)
    if isinstance(value, list):
        return [_render_template_value(item, replacements) for item in value]
    if isinstance(value, dict):
        return {key: _render_template_value(item, replacements) for key, item in value.items()}
    return value


def _disconnect_devices(devices: dict[str, Any], log: Any | None = None) -> None:
    for device_id, device in devices.items():
        if not hasattr(device, "disconnect"):
            continue
        try:
            device.disconnect()
            if log:
                log(f"已断开设备 {device_id} 连接，用于中断等待中的通信操作")
        except Exception as exc:
            if log:
                log(f"断开设备 {device_id} 连接时出错: {exc}")


def _action_to_dict(action: ActionSpec, runtime_config: RuntimeConfig | None = None) -> dict[str, Any]:
    data = {
        "method": action.method,
        "label": action.label,
        "description": action.description,
        "needs_position": action.needs_position,
        "params": action.params,
        "device_id": action.device_id,
    }
    if runtime_config is not None:
        data["opc_variables"] = _collect_action_level_opc_variables(action.method, runtime_config)
    return data


def _collect_action_level_opc_variables(method: str, runtime_config: RuntimeConfig) -> list[str]:
    snapshot_config = runtime_config.opc_snapshot
    variables = list(snapshot_config.common_variables)
    variables.extend(snapshot_config.action_variables.get(method, []))
    return list(dict.fromkeys(variables))


def _record_to_dict(record: RunRecord) -> dict[str, Any]:
    return {
        "run_id": record.run_id,
        "status": record.status,
        "logs": record.logs,
        "log_events": [event.to_dict() for event in record.log_events],
        "result": record.result,
        "error": record.error,
        "node_statuses": record.node_statuses,
        "timing_report_path": record.timing_report_path,
        "timing_summary_path": record.timing_summary_path,
    }


def _frontend_entry_response() -> Response:
    if FRONTEND_INDEX_FILE.exists():
        return FileResponse(FRONTEND_INDEX_FILE)

    return HTMLResponse(
        """
        <!DOCTYPE html>
        <html lang="zh-CN">
        <head>
            <meta charset="UTF-8">
            <title>szlab 流程图画板未构建</title>
            <style>
                body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: #f6f7f9; }
                main {
                    max-width: 760px;
                    margin: 80px auto;
                    background: white;
                    border-radius: 16px;
                    padding: 28px;
                    box-shadow: 0 10px 28px rgba(15, 23, 42, 0.08);
                }
                code { background: #f1f5f9; border-radius: 6px; padding: 2px 5px; }
                pre { background: #111827; color: #e5e7eb; border-radius: 12px; padding: 14px; overflow: auto; }
            </style>
        </head>
        <body>
            <main>
                <h1>szlab 流程图画板未构建</h1>
                <p>请先构建 Node.js 前端，或在开发时启动 Vite dev server。</p>
                <pre>cd unilabos_local_ui
npm install
npm run build</pre>
                <p>
                    后端 API 已可用：
                    <code>/api/actions</code>、<code>/api/workflow/build-graph</code>、<code>/api/run</code>。
                </p>
            </main>
        </body>
        </html>
        """,
        status_code=503,
    )


def apply_preset_debug_config(preset_name: str) -> dict[str, str]:
    preset = load_preset(preset_name)
    applied: dict[str, str] = {}
    skip_variables = preset.debug_config.get("skip_robot_precheck_variables", [])
    if isinstance(skip_variables, list):
        variable_names = [str(name).strip() for name in skip_variables if str(name).strip()]
        if variable_names:
            value = ",".join(variable_names)
            os.environ["SKIP_ROBOT_PRECHECK_VARIABLES"] = value
            applied["SKIP_ROBOT_PRECHECK_VARIABLES"] = value

    env_values = preset.debug_config.get("env", {})
    if isinstance(env_values, dict):
        for name, value in env_values.items():
            if not name:
                continue
            text_value = str(value)
            os.environ[str(name)] = text_value
            applied[str(name)] = text_value
    return applied


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Uni-Lab 本地 workflow 调试界面")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址")
    parser.add_argument("--port", type=int, default=8014, help="监听端口")
    parser.add_argument("--preset", default="ai4c", help="preset 名称，Docker 默认使用 szlab_mixer")
    parser.add_argument("--no-browser", action="store_true", help="启动时不自动打开浏览器")
    parser.add_argument("--runtime-config", type=Path, default=None, help="覆盖 preset 的 runtime config")
    parser.add_argument("--debug", action="store_true", help="启用 preset.debug_config 中定义的调试环境变量")
    parser.add_argument("--timing", action="store_true", help="临时记录 workflow 排程耗时")
    args = parser.parse_args()

    runtime_config = load_runtime_config(args.runtime_config) if args.runtime_config else None
    ignore_opcua_token_time_drift()
    if args.debug:
        apply_preset_debug_config(args.preset)
    start_ui(
        host=args.host,
        port=args.port,
        open_browser=not args.no_browser,
        preset_name=args.preset,
        runtime_config=runtime_config,
        timing_enabled=args.timing,
    )


if __name__ == "__main__":
    main()
