"""szlab 本地 workflow 调试界面。"""

from __future__ import annotations

import argparse
import asyncio
import csv
import errno
import hashlib
import io
import json
import logging
import os
import re
import tempfile
import threading
import time
import traceback
import uuid
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from fastapi import FastAPI, HTTPException, Request as FastAPIRequest
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    Response,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles

from unilabos.registry.ast_registry_scanner import scan_directory
from unilabos.devices.workstation.szlab_poly_studio.s09_pipetting_station.pipetting_station import (
    DEFAULT_TIP_REUSE_STATE_PATH,
)
from scripts.opc_simulator_process_manager import (
    InvalidSimulatorConfig,
    InvalidSimulatorRevision,
    OpcSimulatorProcessManager,
    SimulatorAlreadyRunning,
    SimulatorConfigNotFound,
    SimulatorInputError,
    SimulatorRevisionConflict,
    SimulatorRunIdentityConflict,
    SimulatorSpawnError,
    SimulatorStorageError,
    SimulatorStorageFull,
    StopTimeout,
    UnsafeSimulatorUrlConfirmationRequired,
)
from scripts.task_action_log_store import TaskActionLogStore
from scripts.task_action_result import ActionReturnedFailure, find_action_failure
from unilabos.devices.workstation.szlab_poly_studio.error_codes import (
    enrich_mixing_failure,
    enrich_with_plc_alarm,
    plc_alarm_for_address,
    read_active_plc_alarms,
)
from scripts.run_history_store import RunHistoryStore
from scripts.task_execution_coordinator import (
    TaskApiConflict,
    TaskExecutionCoordinator,
    validate_task_dispatch_preflight,
    workflow_nodes_from_payload,
)
from scripts.szlab_task_opc_simulator import DEFAULT_URL as DEFAULT_OPC_SIMULATOR_URL
from scripts.opc_simulator_profiles import (
    MAX_JSON_BYTES,
    ProfileLimitError,
    ProfileValidationError,
    RevisionConflict,
    generate_opc_simulator_draft,
    list_profile_files,
    migrate_legacy_opc_profiles,
    read_profile,
    resolve_profile_path,
    save_profile,
    validate_profile,
)
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
    iter_action_logs,
    iter_opc_wait_logs,
    log_opc_snapshot_failures,
    log_opc_snapshot_recovery,
    load_workflow_nodes,
    load_runtime_config,
    node_method,
    opc_snapshot_failures,
    route_node_device,
    run_nodes,
    snapshot_opc_state,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
SZLAB_DIR = REPO_ROOT / "tests" / "szlab_poly_studio"
PRESET_DIR = SZLAB_DIR / "presets"
FRONTEND_DIR = REPO_ROOT / "unilabos_local_ui"
FRONTEND_DIST_DIR = FRONTEND_DIR / "dist"
FRONTEND_INDEX_FILE = FRONTEND_DIST_DIR / "index.html"
OPC_SIMULATOR_CONFIG_DIR = REPO_ROOT / "task-orchestration" / "configs"
OPC_SIMULATOR_REFERENCE_DIR = Path(__file__).with_name("config")
OPC_SIMULATOR_REFERENCE_PROFILE = "szlab_task_opc_simulator.json"
OPC_SIMULATOR_PROFILE_SPEC_PATH = (
    REPO_ROOT / "docs" / "developer_guide" / "opc_simulator_profile_v2.md"
)
GENERATED_GRAPH_SENTINEL = "__generated__"
_LOGGER = logging.getLogger(__name__)


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
        actions = _load_registry_actions(
            registry_device_ids, path_roots, preset_path.parent
        )
        if action_device_id_aliases:
            actions = {
                method: replace(
                    action,
                    device_id=action_device_id_aliases.get(
                        action.device_id or "", action.device_id
                    ),
                )
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
        default_workflow_name=data.get(
            "default_workflow_name", "szlab_canvas_workflow"
        ),
        default_config=data.get("default_config", {}),
        debug_config=data.get("debug_config", {}),
        path_roots=path_roots,
        device_graph=data.get("device_graph", {"nodes": [], "links": []}),
        actions=actions,
        base_dir=preset_path.parent,
    )


def _load_registry_actions(
    device_ids: list[str], path_roots: list[str], base_dir: Path
) -> dict[str, ActionSpec]:
    repo_root = REPO_ROOT
    pending = set(device_ids)
    actions_by_device: dict[str, dict[str, ActionSpec]] = {}
    with ThreadPoolExecutor(
        max_workers=4, thread_name_prefix="SzlabRegistryScan"
    ) as executor:
        for root in path_roots:
            root_path = _resolve_registry_scan_root(root, base_dir, repo_root)
            if not root_path.exists():
                continue
            scan_result = scan_directory(
                root_path, python_path=repo_root, executor=executor
            )
            for device_id in list(pending):
                device_meta = scan_result.get("devices", {}).get(device_id)
                if device_meta:
                    actions_by_device[device_id] = _actions_from_ast_device_meta(
                        device_id, device_meta
                    )
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


def _actions_from_ast_device_meta(
    device_id: str, device_meta: dict[str, Any]
) -> dict[str, ActionSpec]:
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
    "coarse_position": {"label": "粗注粉粉罐位", "description": "参与粗注粉的 S07 粉罐位置，范围 1–10；选择 0 时跳过粗注粉。"},
    "fine_position": {"label": "精注粉粉罐位", "description": "参与精注粉的 S07 粉罐位置，范围 1–10；选择 0 时跳过精注粉。"},
    "target_weight": {"label": "目标注粉重量", "description": "S07 本次注粉的目标重量。", "unit": "g（待 PLC 确认）"},
    "params_json": {"label": "配方文件", "description": "粗/精注粉参数 JSON 路径；留空使用设备默认文件。"},
    "recipe_name": {"label": "加粉策略", "description": "从默认注粉参数 JSON 中选择的策略名称，例如 default、salt 或 salt2。"},
    "powder_count": {"label": "固体粉末种类数", "description": "同一样品需要依次加入的固体粉末数量。"},
    "powder_additions": {"label": "各粉末参数", "description": "每种粉末的粉罐位、单独目标重量和加粉策略。"},
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
    "liquid_station_index": {
        "label": "加液体工位编号",
        "description": "本次使用的 S09 液体工位编号，范围 1–5；TIP 按溶剂批次与工位组合绑定。",
    },
    "solvent_batch_id": {
        "label": "溶剂标识",
        "description": "用于复用加液 TIP；相同溶剂标识和相同工位复用原 TIP，标识或工位任一变化都会分配新 TIP。",
    },
    "used_tip_count": {
        "label": "已使用 TIP 数量",
        "description": "仅库存初始化时使用；从 TIP 1 起将指定数量标记为不可自动分配。",
    },
    "known_bindings": {
        "label": "已知溶剂与 TIP 绑定",
        "description": "仅库存恢复时使用，格式为“批次与工位组合键”到 TIP 编号的 JSON 对象。",
    },
    "station": {"label": "加液体工位", "description": "S09 加液体工位编号，范围 1–5。"},
    "density_volume": {
        "label": "测密度体积",
        "description": "工艺 9 使用的烧杯取样体积；该工艺一次完成抽排液，PLC 返回的两组天平数据均为该体积液体的净重。",
    },
    "density_measurement_count": {
        "label": "测密度次数",
        "description": "PLC 连续测密度次数，范围 1-10；每次结果写入对应的抽液/放液天平读数数组。",
    },
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
    "liquid_count": {"label": "液体种类数", "description": "本次加液动作需要依次加入的液体数量。"},
    "liquid_additions": {"label": "各液体参数", "description": "每种液体的工位、溶剂标识和独立加液体积。"},
    "initialize_tip_inventory": {"label": "执行前初始化 TIP 库存", "description": "会覆盖当前 TIP 使用和绑定记录；仅在确认盒1状态后启用。"},
    "initial_used_tip_count": {"label": "初始化时已使用 TIP 数量", "description": "初始化后从 TIP 1 开始标记为不可用的数量。"},
    "release_after": {"label": "结束后释放", "description": "流程完成后是否释放样品与工站绑定。"},
    "bottle": {"label": "液体瓶编号", "description": "S09 液体试剂瓶编号，范围 1–5。"},
    "remaining_volume": {"label": "剩余液量", "description": "液体瓶当前或初始化剩余体积。", "unit": "mL"},
    "require_stable": {"label": "要求稳定", "description": "开启后仅在 S09 天平稳定信号有效时返回读数。"},
}
_METHOD_PARAM_HELP: dict[tuple[str, str], dict[str, Any]] = {
    ("add_liquid_with_reusable_tip", "volume"): {
        "label": "加液体积",
        "description": "S09 从所选加液体工位吸取并排入烧杯的体积；实际单位由“体积单位”决定。",
    },
    ("measure_density", "density_measurement_count"): {
        "label": "测密度次数",
        "description": "PLC 连续测密度次数，范围 1-10；每次结果写入对应的抽液/放液天平读数数组。",
    },
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
            "description": "1=TIP盒，2=液体试剂瓶，3=烧杯，4=测密度烧杯。",
            "options": [
                {"value": 1, "label": "TIP 盒"},
                {"value": 2, "label": "液体试剂瓶"},
                {"value": 3, "label": "烧杯"},
                {"value": 4, "label": "测密度烧杯"},
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
    ("submit_place_to_s04", "position"): {"description": "S04 磁搅工位编号，范围 1–4。"},
    ("submit_pick_from_s04", "position"): {"description": "S04 磁搅工位编号，范围 1–4。"},
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


def _find_action_handle_for_param(
    handles: Any, param_name: str
) -> dict[str, Any] | None:
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
    live_statuses: dict[str, dict[str, dict[str, Any]]] = field(default_factory=dict)
    live_log_indexes: dict[tuple[str, str], int] = field(default_factory=dict)
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
                category=category
                or _infer_log_category(message, scope=scope, detail=detail),
                scope=scope,
                node_id=node_id,
                detail=detail,
            )
        )

    def update_live_status(
        self,
        node_id: str,
        key: str,
        payload: dict[str, Any],
    ) -> None:
        self.live_statuses.setdefault(node_id, {})[key] = dict(payload)

    def clear_live_status(self, node_id: str, key: str | None = None) -> None:
        if key is None:
            self.live_statuses.pop(node_id, None)
            return
        statuses = self.live_statuses.get(node_id)
        if statuses is None:
            return
        statuses.pop(key, None)
        if not statuses:
            self.live_statuses.pop(node_id, None)

    def update_live_log(
        self,
        node_id: str,
        key: str,
        message: str,
        *,
        level: str = "info",
    ) -> None:
        live_key = (node_id, key)
        index = self.live_log_indexes.get(live_key)
        if index is None:
            self.append_log(
                message,
                node_id=node_id,
                level=level,
            )
            self.live_log_indexes[live_key] = len(self.log_events) - 1
            return
        self.logs[index] = message
        event = self.log_events[index]
        event.message = message
        event.level = level


def _infer_log_category(
    message: str, *, scope: str, detail: dict[str, Any] | None
) -> str:
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


def _task_execution_log_contract(
    message: str,
    *,
    level: str,
    detail: dict[str, Any] | None,
    category: str = "",
    code: str = "",
    phase: str = "",
) -> dict[str, str]:
    """把现有 Workflow 日志兼容转换为统一的 Task 执行日志协议。"""
    inferred = _infer_log_category(message, scope="node", detail=detail)
    inferred_category = {
        "opc": "opc",
        "opc_sample": "opc",
        "opc_change": "opc",
        "opc_wait": "opc",
        "action_result": "result",
    }.get(inferred, "action")
    normalized_level = str(level or "info").strip().lower()
    legacy_error = not category and re.search(
        r"失败|错误|error|failed", message, flags=re.IGNORECASE
    )
    if legacy_error and normalized_level not in {"error", "critical"}:
        normalized_level = "error"
    detail_type = detail.get("type") if isinstance(detail, dict) else None
    has_result = isinstance(detail, dict) and "result" in detail
    result_failure = find_action_failure(detail["result"]) if has_result else None
    opc_wait_failure = False
    if isinstance(detail, dict):
        opc_wait_failure = (
            detail_type == "opc_wait"
            and detail.get("phase") == "finish"
            and detail.get("success") is False
        )
    if result_failure is not None and not category:
        inferred_category = "result"
        normalized_level = "error"
    if opc_wait_failure and not category:
        inferred_category = "opc"
        normalized_level = "error"
    if result_failure is not None:
        inferred_code = "action_returned_failure"
    elif opc_wait_failure:
        inferred_code = (
            "opc_wait_read_failed" if detail.get("error") else "opc_wait_failed"
        )
    elif isinstance(detail_type, str) and detail_type.strip():
        inferred_code = str(detail_type)
    else:
        inferred_code = {
            "action_result": "action_result",
            "error": "action_log_error",
            "node": "action_log",
        }.get(inferred, inferred)
    detail_phase = detail.get("phase") if isinstance(detail, dict) else None
    return {
        "category": str(category or inferred_category).strip().lower(),
        "level": normalized_level,
        "code": str(code or inferred_code).strip(),
        "phase": str(phase or detail_phase or "executing").strip(),
    }


def _run_node_with_live_opc_sampling(
    node: WorkflowNode,
    devices: dict[str, Any],
    *,
    action_callable: Callable[..., Any] | None = None,
    logger: WorkflowLogger,
    runtime_config: RuntimeConfig,
    sample_interval: float = 0.5,
) -> list[dict[str, Any]]:
    bound_action_provided = action_callable is not None
    device_name = (
        node.device_name
        if bound_action_provided
        else route_node_device(node, runtime_config)
    )
    device = devices.get(device_name)
    if device is None:
        raise KeyError(f"未创建本地设备实例: {device_name}")

    method_name = node_method(node)
    snapshot_variables = collect_snapshot_variables(
        method_name, node.param, runtime_config
    )
    default_plc = devices.get(runtime_config.device_factory.plc_device_id)
    snapshot_client = default_plc or (
        device if hasattr(device, "get_variables") else None
    )
    if (
        not bound_action_provided
        and (
            not snapshot_variables
            or snapshot_client is None
            or not hasattr(snapshot_client, "get_variables")
        )
    ):
        return run_nodes(
            [node],
            devices,
            logger=logger,
            runtime_config=runtime_config,
        )
    if action_callable is None:
        action_callable = getattr(device, method_name, None)
    if not callable(action_callable):
        raise AttributeError(f"{device_name} 不存在动作方法: {method_name}")
    before = (
        snapshot_opc_state(snapshot_client, snapshot_variables)
        if (
            snapshot_client is not None
            and snapshot_variables
            and hasattr(snapshot_client, "get_variables")
        )
        else {}
    )

    logger.log(
        f"[1/1] {device_name}.{method_name}({node.param})",
        detail={"device_name": device_name, "method": method_name, "param": node.param},
    )
    if before:
        logger.log(
            f"OPC状态采样: {len(before)} 个变量",
            detail={"before": format_snapshot_detail(before, snapshot_client)},
        )
    active_snapshot_failures = log_opc_snapshot_failures(
        logger,
        before,
        snapshot_variables,
        phase="sampling_before",
        plc=snapshot_client,
    )

    stop_sampling = threading.Event()
    last_snapshot = dict(before)
    active_live_failure_signature = tuple(
        (str(item.get("name") or ""), str(item.get("error") or ""))
        for item in active_snapshot_failures
    )
    skip_parallel_sampling = (
        bool(runtime_config.device_factory.devices) and snapshot_client is device
    )

    def sample_live_changes() -> None:
        nonlocal active_live_failure_signature, active_snapshot_failures, last_snapshot
        while not stop_sampling.wait(sample_interval):
            current = snapshot_opc_state(snapshot_client, snapshot_variables)
            failures = opc_snapshot_failures(
                current,
                snapshot_variables,
                plc=snapshot_client,
            )
            failure_signature = tuple(
                (str(item.get("name") or ""), str(item.get("error") or ""))
                for item in failures
            )
            if failure_signature and failure_signature != active_live_failure_signature:
                log_opc_snapshot_failures(
                    logger,
                    current,
                    snapshot_variables,
                    phase="sampling_live",
                    plc=snapshot_client,
                )
            elif active_snapshot_failures and not failure_signature:
                log_opc_snapshot_recovery(
                    logger,
                    active_snapshot_failures,
                    phase="sampling_live",
                )
            active_snapshot_failures = failures
            active_live_failure_signature = failure_signature
            diff_detail = build_snapshot_diff_detail(
                last_snapshot, current, plc=snapshot_client
            )
            last_snapshot = current
            if diff_detail["changes"]:
                logger.log(
                    f"OPC实时变化: {len(diff_detail['changes'])}/{len(current)} 个变量变化",
                    detail=diff_detail,
                )

    sampler: threading.Thread | None = None
    if (
        snapshot_client is not None
        and snapshot_variables
        and not skip_parallel_sampling
    ):
        sampler = threading.Thread(
            target=sample_live_changes, name="SzlabLiveOpcSampler", daemon=True
        )
        sampler.start()

    unbind_wait_logger = bind_opc_wait_logger(
        logger, default_plc, device, snapshot_client
    )
    clear_wait_alarm = getattr(default_plc, "clear_last_wait_alarm", None)
    if callable(clear_wait_alarm):
        clear_wait_alarm()
    try:
        result = action_callable(**node.param)
    finally:
        unbind_wait_logger()
        stop_sampling.set()
        if sampler is not None:
            sampler.join(timeout=max(sample_interval * 2, 0.1))

    after = (
        snapshot_opc_state(snapshot_client, snapshot_variables)
        if snapshot_client is not None
        else {}
    )
    if after:
        final_live_diff = build_snapshot_diff_detail(
            last_snapshot, after, plc=snapshot_client
        )
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
    after_failures = log_opc_snapshot_failures(
        logger,
        after,
        snapshot_variables,
        phase="sampling_after",
        plc=snapshot_client,
    )
    if active_snapshot_failures and not after_failures:
        log_opc_snapshot_recovery(
            logger,
            active_snapshot_failures,
            phase="sampling_after",
        )
    for action_log in iter_action_logs(result):
        logger.log(
            action_log["message"],
            detail={"action_log": action_log.get("detail")},
        )
    for wait_log in iter_opc_wait_logs(default_plc, device, snapshot_client):
        logger.log(
            wait_log["message"],
            level=wait_log.get("level", "info"),
            detail=wait_log.get("detail"),
        )
    failure = find_action_failure(result)
    reported_failure = failure
    if failure is not None:
        enriched_failure = enrich_mixing_failure(
            failure,
            device_id=device_name,
        )
        get_wait_alarm = getattr(default_plc, "get_last_wait_alarm", None)
        wait_alarm_data = get_wait_alarm(clear=True) if callable(get_wait_alarm) else None
        wait_alarm = (
            plc_alarm_for_address(str(wait_alarm_data.get("plc_address") or ""))
            if isinstance(wait_alarm_data, dict)
            else None
        )
        active_alarms = [] if wait_alarm is not None else read_active_plc_alarms(
            default_plc,
            stations=(
                {str(enriched_failure["station"])}
                if enriched_failure and enriched_failure.get("station")
                else None
            ),
        )
        if wait_alarm is not None:
            reported_failure = enrich_with_plc_alarm(
                enriched_failure or failure,
                alarm=wait_alarm,
            )
        elif active_alarms:
            reported_failure = enrich_with_plc_alarm(
                enriched_failure or failure,
                alarm=active_alarms[0],
            )
        elif enriched_failure is not None:
            reported_failure = enriched_failure
        if result is failure:
            result = reported_failure
    if isinstance(result, dict):
        status_text = "成功" if failure is None else "失败"
        summary = f"动作结果：{status_text}"
        display_message = result.get("display_message")
        if display_message:
            logger.log(str(display_message))
        elif result.get("message"):
            summary = f"{summary} · {result['message']}"
    elif failure is not None:
        summary = f"动作结果：失败 · {result}"
    else:
        summary = f"动作结果：{result}"
    result_detail = {"result": result}
    if reported_failure is not None and result is not reported_failure:
        result_detail["reported_failure"] = reported_failure
    logger.log(summary, detail=result_detail)

    output = {
        "uuid": node.uuid,
        "device_name": device_name,
        "method": method_name,
        "param": node.param,
        "opc_before": before,
        "opc_after": after,
        "result": result,
    }
    if reported_failure is not None:
        raise ActionReturnedFailure(
            device_id=device_name,
            action_name=method_name,
            failure=reported_failure,
        )
    return [output]


def _task_workflow_fingerprint(payload: dict[str, Any]) -> str:
    """为预检使用的 workflow 快照生成稳定指纹。"""
    workflow = payload.get("data", payload)
    try:
        canonical = json.dumps(
            workflow,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("workflow 内容无法生成稳定指纹") from exc
    return f"sha256:{hashlib.sha256(canonical).hexdigest()}"


class WorkflowRunManager:
    def __init__(
        self,
        preset: WorkflowPreset,
        runtime_config: RuntimeConfig,
        *,
        timing_enabled: bool = False,
        run_history_store: RunHistoryStore | None = None,
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
        self._task_snapshot_publisher = TaskOrchestrationSnapshotPublisher()
        self._task_action_log_store = TaskActionLogStore()
        self._active_scheduler_errors: dict[
            str, dict[tuple[str, str, str, str, str], dict[str, Any]]
        ] = {}
        self._active_opc_errors: dict[tuple[str, str], dict[str, Any]] = {}
        self._run_history_store = run_history_store or RunHistoryStore()
        self._task_execution_coordinator = TaskExecutionCoordinator(
            task_client=self._task_snapshot_publisher,
            node_runner=self._run_task_action_node,
            device_provider=self._task_execution_devices,
        )

    def _append_task_action_log(
        self,
        context: dict[str, Any],
        message: str,
        *,
        level: str = "info",
        detail: dict[str, Any] | None = None,
        category: str = "",
        code: str = "",
        phase: str = "",
        record_incident: bool = True,
    ) -> None:
        contract = _task_execution_log_contract(
            message,
            level=level,
            detail=detail,
            category=category,
            code=code,
            phase=phase,
        )
        try:
            self._task_action_log_store.append(
                workflow_path=str(context.get("workflow_path") or ""),
                instance_id=str(context.get("instance_id") or ""),
                node_id=str(context.get("node_id") or ""),
                execution_id=str(context.get("execution_id") or ""),
                sample_id=str(context.get("sample_id") or ""),
                template_id=str(context.get("template_id") or ""),
                device_id=str(context.get("device_id") or ""),
                action_name=str(context.get("action_name") or ""),
                category=contract["category"],
                level=contract["level"],
                code=contract["code"],
                phase=contract["phase"],
                message=message,
                detail=detail,
            )
        except Exception:
            # 日志收集失败不得影响 Action 执行
            pass
        try:
            self._run_history_store.append_event(
                workflow_path=str(context.get("workflow_path") or ""),
                instance_id=str(context.get("instance_id") or ""),
                node_id=str(context.get("node_id") or ""),
                execution_id=str(context.get("execution_id") or ""),
                sample_id=str(context.get("sample_id") or ""),
                level=contract["level"],
                message=message,
                detail=detail,
            )
        except Exception:
            # 持久化失败不得影响 Action 执行
            _LOGGER.exception("Task Action 日志持久化失败")
        normalized_level = contract["level"]
        if record_incident and (
            normalized_level in {"warning", "error", "critical"} or any(
                keyword in message for keyword in ("报警", "告警")
            )
        ):
            try:
                error_category = {
                    "schedule": "scheduler_error",
                    "opc": "opc_error",
                }.get(contract["category"], "action_error")
                self._run_history_store.record_incident(
                    category=(
                        error_category
                        if normalized_level in {"error", "critical"}
                        else "action_alarm"
                    ),
                    code=contract["code"] or "action_log_alarm",
                    severity=(
                        normalized_level
                        if normalized_level in {"warning", "error", "critical"}
                        else "warning"
                    ),
                    message=message,
                    workflow_path=str(context.get("workflow_path") or ""),
                    instance_id=str(context.get("instance_id") or ""),
                    sample_id=str(context.get("sample_id") or ""),
                    node_id=str(context.get("node_id") or ""),
                    execution_id=str(context.get("execution_id") or ""),
                    device_id=str(context.get("device_id") or ""),
                    action_name=str(context.get("action_name") or ""),
                    phase=contract["phase"] or "动作执行中",
                    detail=detail,
                )
            except Exception:
                pass

    def _run_task_action_node(
        self,
        node: WorkflowNode,
        devices: dict[str, Any],
        action_callable: Callable[..., Any],
        context: dict[str, Any],
    ) -> list[dict[str, Any]]:
        execution_id = str(context.get("execution_id") or "")
        try:
            self._run_history_store.record_action_start(
                workflow_path=str(context.get("workflow_path") or ""),
                instance_id=str(context.get("instance_id") or ""),
                node_id=str(context.get("node_id") or node.uuid),
                execution_id=execution_id,
                sample_id=str(context.get("sample_id") or ""),
                device_id=node.device_name,
                action_name=node_method(node),
                params=node.param,
            )
        except Exception:
            # 台账不得改变原有动作执行路径
            _LOGGER.exception("Task Action 开始记录持久化失败")

        def writer(message: str, *, level: str = "info", detail: dict[str, Any] | None = None) -> None:
            self._append_task_action_log(
                context,
                message,
                level=level,
                detail=detail,
            )

        logger = WorkflowLogger(writer=writer)
        try:
            result = _run_node_with_live_opc_sampling(
                node,
                devices,
                action_callable=action_callable,
                logger=logger,
                runtime_config=self._runtime_config,
            )
        except Exception as exc:
            returned_failure = isinstance(exc, ActionReturnedFailure)
            traceback_text = traceback.format_exc()
            if not returned_failure:
                exception_type = type(exc).__name__
                exception_text = str(exc).strip()
                self._append_task_action_log(
                    context,
                    f"Action 执行失败：{exception_type}"
                    + (f"：{exception_text}" if exception_text else ""),
                    level="error",
                    category="action",
                    code="action_exception",
                    phase="executing",
                    detail={
                        "type": "action_exception",
                        "exception_type": exception_type,
                        "message": exception_text,
                        "traceback": traceback_text,
                    },
                    record_incident=False,
                )
            try:
                self._run_history_store.record_action_finish(
                    execution_id=execution_id,
                    status="failed",
                    error={
                        "type": type(exc).__name__,
                        "message": str(exc),
                        "traceback": traceback_text,
                    },
                )
            except Exception:
                pass
            if not returned_failure:
                try:
                    self._run_history_store.record_incident(
                        category="action_error",
                        code="action_failed",
                        severity="error",
                        message=str(exc),
                        execution_id=execution_id,
                        phase="动作执行中",
                        detail={
                            "type": type(exc).__name__,
                            "traceback": traceback_text,
                        },
                    )
                except Exception:
                    pass
            raise
        try:
            self._run_history_store.record_action_finish(
                execution_id=execution_id,
                status="completed",
                result=result,
            )
        except Exception:
            pass
        return result

    def _publish_task_scheduler_errors(
        self,
        *,
        workflow_path: str,
        diagnostics: Any,
    ) -> None:
        """把新出现的调度错误发布到统一日志，并抑制连续 tick 重复。"""
        items = diagnostics if isinstance(diagnostics, list) else []
        current_items: dict[
            tuple[str, str, str, str, str], dict[str, Any]
        ] = {}
        for item in items:
            if not isinstance(item, dict):
                continue
            level = str(item.get("severity") or "warning").strip().lower()
            if level not in {"error", "critical"}:
                continue
            key = (
                str(item.get("instance_id") or ""),
                str(item.get("node_id") or ""),
                str(item.get("execution_id") or ""),
                str(item.get("code") or "scheduler_error"),
                str(item.get("message") or ""),
            )
            current_items.setdefault(key, item)

        with self._lock:
            previous_items = self._active_scheduler_errors.get(
                workflow_path, {}
            )
            new_keys = current_items.keys() - previous_items.keys()
            recovered_keys = previous_items.keys() - current_items.keys()
            if current_items:
                self._active_scheduler_errors[workflow_path] = current_items
            else:
                self._active_scheduler_errors.pop(workflow_path, None)

        for key in recovered_keys:
            item = previous_items[key]
            original_code = str(item.get("code") or "scheduler_error")
            original_message = str(item.get("message") or original_code)
            self._append_task_action_log(
                {
                    "workflow_path": workflow_path,
                    "instance_id": str(item.get("instance_id") or ""),
                    "sample_id": str(item.get("sample_id") or ""),
                    "template_id": str(item.get("template_id") or ""),
                    "node_id": str(item.get("node_id") or ""),
                    "execution_id": str(item.get("execution_id") or ""),
                    "device_id": str(item.get("device_id") or ""),
                    "action_name": str(item.get("action_name") or ""),
                },
                f"调度错误已恢复：{original_message}",
                level="info",
                category="schedule",
                code="scheduler_error_recovered",
                phase="recovered",
                detail={
                    "type": "scheduler_error_recovered",
                    "original_code": original_code,
                    "original_message": original_message,
                    "original_phase": str(item.get("phase") or "dispatching"),
                    "diagnostic_category": str(item.get("category") or ""),
                },
                record_incident=False,
            )

        for key, item in current_items.items():
            if key not in new_keys:
                continue
            code = str(item.get("code") or "scheduler_error")
            message = str(item.get("message") or code)
            self._append_task_action_log(
                {
                    "workflow_path": workflow_path,
                    "instance_id": str(item.get("instance_id") or ""),
                    "sample_id": str(item.get("sample_id") or ""),
                    "template_id": str(item.get("template_id") or ""),
                    "node_id": str(item.get("node_id") or ""),
                    "execution_id": str(item.get("execution_id") or ""),
                    "device_id": str(item.get("device_id") or ""),
                    "action_name": str(item.get("action_name") or ""),
                },
                message,
                level=str(item.get("severity") or "error"),
                category="schedule",
                code=code,
                phase=str(item.get("phase") or "dispatching"),
                detail={
                    "type": "scheduler_diagnostic",
                    "diagnostic_category": str(item.get("category") or ""),
                    "immediate": bool(item.get("immediate")),
                    "diagnostic": (
                        item.get("detail")
                        if isinstance(item.get("detail"), dict)
                        else {}
                    ),
                },
                record_incident=False,
            )

    def _update_task_opc_error(
        self,
        *,
        workflow_path: str,
        operation: str,
        code: str = "",
        message: str = "",
        phase: str = "polling",
        device_id: str = "",
        detail: dict[str, Any] | None = None,
    ) -> None:
        """发布 OPC 错误状态，并在连续失败结束时补一条恢复日志。"""
        state_key = (workflow_path, operation)
        error_key = (str(code or ""), str(message or ""))
        with self._lock:
            previous = self._active_opc_errors.get(state_key)
            if not code:
                self._active_opc_errors.pop(state_key, None)
            elif previous and previous.get("error_key") == error_key:
                return
            else:
                self._active_opc_errors[state_key] = {
                    "error_key": error_key,
                    "code": code,
                    "message": message or code,
                    "phase": phase,
                    "device_id": device_id,
                    "detail": dict(detail or {}),
                }
        if not code:
            if previous is None:
                return
            operation_label = {
                "connect": "连接",
                "registration": "注册与初始采样",
                "poll": "轮询采样",
            }.get(operation, operation)
            original_code = str(previous.get("code") or "opc_error")
            original_message = str(previous.get("message") or original_code)
            self._append_task_action_log(
                {
                    "workflow_path": workflow_path,
                    "device_id": str(previous.get("device_id") or device_id),
                },
                f"OPC {operation_label}已恢复：{original_message}",
                level="info",
                category="opc",
                code="opc_error_recovered",
                phase="recovered",
                detail={
                    "type": "opc_error_recovered",
                    "operation": operation,
                    "original_code": original_code,
                    "original_message": original_message,
                    "original_phase": str(previous.get("phase") or phase),
                    "original_detail": previous.get("detail") or {},
                },
                record_incident=False,
            )
            return
        self._append_task_action_log(
            {
                "workflow_path": workflow_path,
                "device_id": device_id,
            },
            message or code,
            level="error",
            category="opc",
            code=code,
            phase=phase,
            detail={
                "type": code,
                "operation": operation,
                **(detail or {}),
            },
        )

    def list_task_action_logs(
        self,
        *,
        workflow_path: str,
        after_seq: int = 0,
        instance_id: str | None = None,
    ) -> dict[str, Any]:
        return self._task_action_log_store.list_since(
            workflow_path,
            after_seq=after_seq,
            instance_id=instance_id,
        )

    def append_task_execution_timings(
        self,
        *,
        workflow_path: str,
        entries: list[dict[str, Any]],
    ) -> int:
        """持久化前端排程循环的分段耗时。"""
        return self._run_history_store.append_scheduler_timings(
            workflow_path=workflow_path,
            entries=entries,
        )

    def list_run_history(self, *, limit: int = 50) -> dict[str, Any]:
        return self._run_history_store.list_runs(limit=limit)

    def get_timing_ledger(self, *, run_id: str | None = None) -> dict[str, Any]:
        return self._run_history_store.timing_ledger(run_id)

    def get_station_ledger(
        self,
        *,
        run_id: str | None = None,
        station: str | None = None,
        limit: int = 1000,
    ) -> dict[str, Any]:
        return self._run_history_store.station_ledger(
            run_id,
            station=station,
            limit=limit,
        )

    def get_incident_ledger(
        self,
        *,
        run_id: str | None = None,
        sample_id: str | None = None,
        status: str | None = None,
        limit: int = 1000,
    ) -> dict[str, Any]:
        return self._run_history_store.incident_ledger(
            run_id,
            sample_id=sample_id,
            status=status,
            limit=limit,
        )

    def export_run_history(
        self,
        *,
        ledger: str,
        run_id: str | None = None,
        file_format: str = "json",
    ) -> Path:
        return self._run_history_store.export_ledger(
            ledger=ledger,
            run_id=run_id,
            file_format=file_format,
        )

    def start(self, payload: dict[str, Any]) -> RunRecord:
        with self._lock:
            if self._active_run_id:
                active = self._records.get(self._active_run_id)
                if active and active.status in {
                    "pending",
                    "preparing",
                    "running",
                    "cancelling",
                }:
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

    def shutdown(self) -> dict[str, Any]:
        shutdown_result = self._task_execution_coordinator.shutdown()
        self._disconnect_cached_devices()
        self._run_history_store.close()
        return shutdown_result

    def _task_execution_devices(self) -> dict[str, Any]:
        """只返回 Task 页面已经连接的设备，避免 tick 隐式重连。"""
        with self._lock:
            return dict(self._cached_devices)

    def preflight_task_dispatch(
        self,
        *,
        workflow_path: str,
        expected_version: int,
        workflow_payload: dict[str, Any],
    ) -> dict[str, Any]:
        """使用当前工作区和 workflow 快照检查 Task 是否允许派发。"""
        if type(expected_version) is not int or expected_version < 0:
            raise ValueError("expected_version 必须是非负整数")
        result, _ = self._evaluate_task_dispatch_preflight(
            workflow_path=workflow_path,
            workflow_payload=workflow_payload,
            expected_version=expected_version,
        )
        return result

    def _evaluate_task_dispatch_preflight(
        self,
        *,
        workflow_path: str,
        workflow_payload: dict[str, Any],
        expected_version: int | None = None,
    ) -> tuple[dict[str, Any], list[WorkflowNode]]:
        """基于服务端最新工作区执行预检，并返回本次解析的节点快照。"""
        workflow_nodes = workflow_nodes_from_payload(workflow_payload)
        workflow_fingerprint = _task_workflow_fingerprint(workflow_payload)
        response = self._task_snapshot_publisher.get_workspace(
            workflow_path=workflow_path
        )
        actual_version = response.get("version")
        if type(actual_version) is not int:
            raise RuntimeError("Task 排程服务未返回有效工作区版本")
        if expected_version is not None and actual_version != expected_version:
            raise TaskApiConflict(
                "version_conflict",
                (
                    "Task 工作区版本已变化: "
                    f"expected={expected_version}, actual={actual_version}"
                ),
            )
        workspace = response.get("workspace")
        if not isinstance(workspace, dict):
            raise RuntimeError("Task 排程服务未返回有效工作区")
        result = validate_task_dispatch_preflight(
            workspace,
            workflow_nodes,
            self._task_execution_devices(),
        )
        return (
            {
                **result.as_dict(),
                "workspace_version": actual_version,
                "workflow_fingerprint": workflow_fingerprint,
            },
            workflow_nodes,
        )

    def run_task_execution_cycle(
        self,
        *,
        workflow_path: str,
        workflow_payload: dict[str, Any] | None = None,
        harvest_only: bool = False,
    ) -> dict[str, Any]:
        """解析当前 workflow，并推进一次非阻塞动作协调周期。"""
        if type(harvest_only) is not bool:
            raise TypeError("harvest_only 必须为 bool")
        if not harvest_only and not isinstance(workflow_payload, dict):
            raise ValueError("缺少当前 workflow JSON")
        preflight_result: dict[str, Any] | None = None
        if harvest_only:
            workflow_nodes: list[WorkflowNode] = []
        else:
            preflight_result, workflow_nodes = (
                self._evaluate_task_dispatch_preflight(
                    workflow_path=workflow_path,
                    workflow_payload=workflow_payload,
                )
            )
            if not preflight_result.get("valid"):
                # 已在途动作仍需正常收割终态，但预检失败后绝不认领新动作。
                stats = self._task_execution_coordinator.cycle(
                    workflow_path=workflow_path,
                    workflow_nodes=workflow_nodes,
                    harvest_only=True,
                )
                errors = preflight_result.get("errors")
                issues = errors if isinstance(errors, list) else []
                diagnostics: list[dict[str, Any]] = []
                for issue in issues:
                    if not isinstance(issue, dict):
                        continue
                    diagnostic = dict(issue)
                    instance_ids = issue.get("instance_ids")
                    resolved_instance_ids = (
                        [str(item) for item in instance_ids if str(item)]
                        if isinstance(instance_ids, list)
                        else []
                    )
                    if resolved_instance_ids:
                        diagnostic["instance_id"] = resolved_instance_ids[0]
                    diagnostic["immediate"] = True
                    diagnostic["detail"] = {
                        **(
                            issue.get("detail")
                            if isinstance(issue.get("detail"), dict)
                            else {}
                        ),
                        "instance_ids": resolved_instance_ids,
                    }
                    diagnostics.append(diagnostic)
                existing_diagnostics = stats.get("diagnostics")
                stats["diagnostics"] = [
                    *(
                        existing_diagnostics
                        if isinstance(existing_diagnostics, list)
                        else []
                    ),
                    *diagnostics,
                ]
                first_message = next(
                    (
                        str(item.get("message") or "").strip()
                        for item in issues
                        if isinstance(item, dict)
                        and str(item.get("message") or "").strip()
                    ),
                    "存在派发阻断项",
                )
                stats["success"] = False
                stats["code"] = "task_dispatch_preflight_failed"
                stats["message"] = f"派发预检未通过：{first_message}"
                stats["preflight"] = preflight_result
                self._publish_task_scheduler_errors(
                    workflow_path=workflow_path,
                    diagnostics=stats["diagnostics"],
                )
                return stats
        try:
            stats = self._task_execution_coordinator.cycle(
                workflow_path=workflow_path,
                workflow_nodes=workflow_nodes,
                harvest_only=harvest_only,
            )
        except Exception as exc:
            traceback_text = traceback.format_exc()
            self._publish_task_scheduler_errors(
                workflow_path=workflow_path,
                diagnostics=[
                    {
                        "category": "scheduler_error",
                        "code": "execution_cycle_failed",
                        "severity": "error",
                        "immediate": True,
                        "phase": "scheduling",
                        "message": (
                            f"调度循环失败：{type(exc).__name__}"
                            + (f"：{exc}" if str(exc) else "")
                        ),
                        "detail": {
                            "exception_type": type(exc).__name__,
                            "message": str(exc),
                            "traceback": traceback_text,
                        },
                    }
                ],
            )
            try:
                self._run_history_store.record_incident(
                    category="scheduler_error",
                    code="execution_cycle_failed",
                    severity="error",
                    message=str(exc),
                    workflow_path=workflow_path,
                    phase="排程循环",
                    detail={
                        "type": type(exc).__name__,
                        "traceback": traceback_text,
                    },
                )
            except Exception:
                pass
            raise
        self._publish_task_scheduler_errors(
            workflow_path=workflow_path,
            diagnostics=stats.get("diagnostics"),
        )
        if not harvest_only:
            try:
                self._run_history_store.observe_scheduler_cycle(
                    workflow_path=workflow_path,
                    diagnostics=stats.get("diagnostics"),
                    cycle_stats={
                        key: value
                        for key, value in stats.items()
                        if key != "diagnostics"
                    },
                )
            except Exception:
                pass
        return stats

    def get_live_devices(self) -> dict[str, Any]:
        with self._lock:
            if self._cached_devices:
                return self._cached_devices

        default_config = self._preset.default_config
        csv_value = str(default_config.get("csv") or "").strip()
        csv_path = _resolve_ui_path(csv_value, self._preset) if csv_value else None
        no_subscription = bool(default_config.get("no_subscription", True))
        graph_value = str(
            default_config.get("graph") or GENERATED_GRAPH_SENTINEL
        ).strip()
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
        )
        return self._get_or_create_devices(
            device_key,
            {
                "graph_file": graph_file,
                "opcua_url": opcua_url or None,
                "csv_path": csv_path,
                "use_subscription": False if no_subscription else None,
                "runtime_config": self._runtime_config,
            },
            lambda message: None,
        )

    def ensure_sensor_event_subscription(self) -> None:
        devices = self.get_live_devices()
        plc_device_id = (
            self._runtime_config.device_factory.plc_device_id or "szlab_poly_plc"
        )
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

    def connect_task_opc(
        self,
        *,
        opcua_url: str,
        workflow_path: str,
    ) -> dict[str, Any]:
        """通过既有 PLC 设备工厂连接 Task 页面请求的 OPC runtime。"""
        normalized_url = opcua_url.strip()
        if not normalized_url:
            raise ValueError("请填写 OPC UA URL")
        default_config = self._preset.default_config
        csv_value = str(default_config.get("csv") or "").strip()
        csv_path = _resolve_ui_path(csv_value, self._preset) if csv_value else None
        no_subscription = bool(default_config.get("no_subscription", True))
        graph_value = str(
            default_config.get("graph") or GENERATED_GRAPH_SENTINEL
        ).strip()
        if graph_value == GENERATED_GRAPH_SENTINEL:
            graph_file = _write_temp_json(
                build_local_device_graph(
                    opcua_url=normalized_url,
                    csv_path=str(csv_path or csv_value),
                    use_subscription=not no_subscription,
                    preset=self._preset,
                )
            )
        else:
            graph_file = _resolve_ui_path(graph_value, self._preset)
        device_key = (
            self._preset.id,
            graph_value,
            normalized_url,
            str(csv_path or ""),
            no_subscription,
        )
        try:
            devices = self._get_or_create_devices(
                device_key,
                {
                    "graph_file": graph_file,
                    "opcua_url": normalized_url,
                    "csv_path": csv_path,
                    "use_subscription": False if no_subscription else None,
                    "runtime_config": self._runtime_config,
                },
                lambda message: None,
            )
        except Exception as exc:
            self._update_task_opc_error(
                workflow_path=workflow_path,
                operation="connect",
                code="opc_connection_failed",
                message=_task_opc_connect_error_message(
                    exc, opcua_url=normalized_url
                ),
                phase="connecting",
                device_id=(
                    self._runtime_config.device_factory.plc_device_id
                    or "szlab_poly_plc"
                ),
                detail={
                    "exception_type": type(exc).__name__,
                    "message": str(exc),
                },
            )
            raise
        self._update_task_opc_error(
            workflow_path=workflow_path,
            operation="connect",
        )
        return devices

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

    def _disconnect_cached_devices(
        self, devices: dict[str, Any] | None = None, log: Any = None
    ) -> None:
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

    def get_stack_status(
        self,
        *,
        task_workspace_path: str | None = None,
        task_workspace_version: int | None = None,
    ) -> dict[str, Any]:
        now = time.monotonic()
        with self._lock:
            if (
                task_workspace_path is None
                and self._stack_status_cache
                and now - self._stack_status_cache[0] < 2.0
            ):
                return self._stack_status_cache[1]

        plc_device_id = (
            self._runtime_config.device_factory.plc_device_id or "szlab_poly_plc"
        )
        with self._lock:
            devices = dict(self._cached_devices)
        if not devices and task_workspace_path is None:
            devices = self.get_live_devices()
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
        registered_variables = []
        registered = getattr(plc, "registered_variables", None)
        if callable(registered):
            registered_variables = list(registered())
        registered_aliases = self._registered_plc_variable_aliases(plc)
        status["plc"] = {
            "device_id": plc_device_id,
            "url": getattr(plc, "url", None),
            "connected": bool(getattr(plc, "client", None)),
            "registered_variables": registered_variables,
            "variable_aliases": registered_aliases,
        }
        with self._lock:
            self._stack_status_cache = (time.monotonic(), status)
        return status

    def get_sensor_arrays(self) -> dict[str, Any]:
        now = time.monotonic()
        with self._lock:
            if self._sensor_arrays_cache and now - self._sensor_arrays_cache[0] < 1.0:
                return self._sensor_arrays_cache[1]

        devices = self.get_live_devices()
        plc_device_id = (
            self._runtime_config.device_factory.plc_device_id or "szlab_poly_plc"
        )
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

    def _publish_registered_plc_snapshot(
        self,
        devices: dict[str, Any],
        *,
        workflow_path: str,
        read_snapshot: bool = False,
    ) -> dict[str, Any]:
        """分发 PLC 注册表；仅在调度前读取模板条件引用的变量。"""
        plc_device_id = (
            self._runtime_config.device_factory.plc_device_id or "szlab_poly_plc"
        )

        def finish(
            result: dict[str, Any],
            *,
            code: str = "",
            detail: dict[str, Any] | None = None,
        ) -> dict[str, Any]:
            self._update_task_opc_error(
                workflow_path=workflow_path,
                operation="registration",
                code=code,
                message=str(result.get("message") or code),
                phase="registering",
                device_id=plc_device_id,
                detail=detail,
            )
            return result

        plc = devices.get(plc_device_id) or devices.get("szlab_poly_plc")
        registered = getattr(plc, "registered_variables", None)
        get_variables = getattr(plc, "get_variables", None)
        if not callable(registered):
            return finish(
                {
                    "distributed": False,
                    "message": "当前运行时未提供可分发的 PLC 注册变量",
                },
                code="opc_registration_unsupported",
            )
        try:
            variable_names = list(registered())
        except Exception as exc:
            return finish(
                {"distributed": False, "message": str(exc)},
                code="opc_registration_failed",
                detail={
                    "exception_type": type(exc).__name__,
                    "message": str(exc),
                },
            )
        variable_aliases = self._registered_plc_variable_aliases(plc)
        if not variable_names:
            return finish(
                {"distributed": False, "message": "PLC 尚未注册任何变量"},
                code="opc_registration_empty",
            )
        try:
            registration = self._task_snapshot_publisher.register(
                workflow_path=workflow_path,
                plc_device_id=plc_device_id,
                runtime_url=str(getattr(plc, "url", "") or ""),
                registered_variables=variable_names,
                variable_aliases=variable_aliases,
            )
        except Exception as exc:
            message = str(exc).strip() or type(exc).__name__
            return finish(
                {"distributed": False, "message": message},
                code="opc_registration_failed",
                detail={
                    "exception_type": type(exc).__name__,
                    "message": str(exc),
                },
            )
        if not read_snapshot:
            return finish({"distributed": True, "variable_count": 0})
        if not callable(get_variables):
            return finish(
                {
                    "distributed": False,
                    "message": "当前运行时未提供 PLC 变量读取接口",
                },
                code="opc_sampling_unsupported",
            )
        condition_variables = self._template_condition_variables(
            registration, plc_device_id, variable_names, variable_aliases
        )
        if not condition_variables:
            return finish({"distributed": True, "variable_count": 0})
        try:
            snapshot = get_variables(condition_variables, use_cache=False)
        except Exception as exc:
            message = str(exc).strip() or type(exc).__name__
            return finish(
                {"distributed": False, "message": message},
                code="opc_condition_snapshot_failed",
                detail={
                    "variables": condition_variables,
                    "exception_type": type(exc).__name__,
                    "message": str(exc),
                },
            )
        failures = opc_snapshot_failures(
            snapshot,
            condition_variables,
            plc=plc,
        )
        values = extract_registered_opc_values(snapshot)
        if not values:
            return finish(
                {
                    "distributed": False,
                    "message": "模板条件变量当前均无法读取",
                },
                code="opc_condition_snapshot_failed",
                detail={"failures": failures},
            )
        try:
            self._task_snapshot_publisher.publish_snapshot(
                workflow_path=workflow_path,
                expected_version=int(registration["version"]),
                plc_device_id=plc_device_id,
                values=values,
            )
        except Exception as exc:
            message = str(exc).strip() or type(exc).__name__
            return finish(
                {"distributed": False, "message": message},
                code="opc_snapshot_publish_failed",
                detail={
                    "exception_type": type(exc).__name__,
                    "message": str(exc),
                },
            )
        result = {"distributed": True, "variable_count": len(values)}
        if failures:
            result["message"] = "部分模板条件变量无法读取"
            return finish(
                result,
                code="opc_condition_snapshot_partial",
                detail={"failures": failures},
            )
        return finish(result)

    def poll_task_opc(self, *, workflow_path: str) -> dict[str, Any]:
        """按当前非终态 Task 的条件读取 PLC，并分发最小快照。"""
        with self._lock:
            devices = dict(self._cached_devices)
        plc_device_id = (
            self._runtime_config.device_factory.plc_device_id or "szlab_poly_plc"
        )

        def finish(
            result: dict[str, Any],
            *,
            code: str = "",
            detail: dict[str, Any] | None = None,
        ) -> dict[str, Any]:
            self._update_task_opc_error(
                workflow_path=workflow_path,
                operation="poll",
                code=code,
                message=str(result.get("message") or code),
                phase="polling",
                device_id=plc_device_id,
                detail=detail,
            )
            return result

        plc = devices.get(plc_device_id) or devices.get("szlab_poly_plc")
        if plc is None or not bool(getattr(plc, "client", None)):
            return finish(
                {
                    "success": False,
                    "active": False,
                    "message": "Task OPC 尚未连接",
                },
                code="opc_not_connected",
            )
        registered = getattr(plc, "registered_variables", None)
        get_variables = getattr(plc, "get_variables", None)
        if not callable(registered) or not callable(get_variables):
            return finish(
                {
                    "success": False,
                    "active": False,
                    "message": "当前 PLC 不支持按需变量采样",
                },
                code="opc_sampling_unsupported",
            )
        try:
            variable_names = list(registered())
            workspace_response = self._task_snapshot_publisher.get_workspace(
                workflow_path=workflow_path
            )
        except Exception as exc:
            message = str(exc).strip() or type(exc).__name__
            return finish(
                {"success": False, "active": False, "message": message},
                code="opc_poll_failed",
                detail={
                    "exception_type": type(exc).__name__,
                    "message": str(exc),
                },
            )
        workspace = workspace_response.get("workspace")
        if not isinstance(workspace, dict):
            return finish(
                {
                    "success": False,
                    "active": False,
                    "message": "Task 工作区内容无效",
                },
                code="opc_workspace_invalid",
            )
        active = any(
            isinstance(instance, dict)
            and instance.get("status") not in {"completed", "failed", "cancelled"}
            for instance in workspace.get("task_instances", [])
        )
        if not active:
            return finish(
                {"success": True, "active": False, "variable_count": 0}
            )
        condition_variables = self._active_task_condition_variables(
            workspace,
            plc_device_id,
            variable_names,
            self._registered_plc_variable_aliases(plc),
        )
        input_variables = self._active_task_condition_variables(
            workspace,
            plc_device_id,
            variable_names,
            self._registered_plc_variable_aliases(plc),
            include_running_outputs=False,
        )
        if not condition_variables:
            return finish(
                {"success": True, "active": True, "variable_count": 0}
            )
        try:
            snapshot = get_variables(condition_variables, use_cache=False)
            failures = opc_snapshot_failures(
                snapshot,
                condition_variables,
                plc=plc,
            )
            values = extract_registered_opc_values(snapshot)
        except Exception as exc:
            message = str(exc).strip() or type(exc).__name__
            detail = {
                "variables": condition_variables,
                "exception_type": type(exc).__name__,
                "message": str(exc),
            }
            if input_variables:
                return finish(
                    {"success": False, "active": True, "message": message},
                    code="opc_input_snapshot_failed",
                    detail=detail,
                )
            return finish(
                {
                    "success": True,
                    "active": True,
                    "variable_count": 0,
                    "message": f"Task 输出状态采样失败：{message}",
                },
                code="opc_output_snapshot_failed",
                detail=detail,
            )
        if any(variable not in values for variable in input_variables):
            return finish(
                {
                    "success": False,
                    "active": True,
                    "message": "当前 Task 条件变量均无法读取",
                },
                code="opc_input_snapshot_failed",
                detail={"failures": failures},
            )
        output_variables = [
            variable
            for variable in condition_variables
            if variable not in input_variables
        ]
        missing_output = any(
            variable not in values for variable in output_variables
        )
        if not values:
            return finish(
                {
                    "success": True,
                    "active": True,
                    "variable_count": 0,
                    "message": "当前 Task 输出状态变量均无法读取",
                },
                code="opc_output_snapshot_failed",
                detail={"failures": failures},
            )
        try:
            self._task_snapshot_publisher.publish_snapshot(
                workflow_path=workflow_path,
                expected_version=int(workspace_response["version"]),
                plc_device_id=plc_device_id,
                values=values,
            )
        except Exception as exc:
            message = str(exc).strip() or type(exc).__name__
            return finish(
                {"success": False, "active": True, "message": message},
                code="opc_snapshot_publish_failed",
                detail={
                    "exception_type": type(exc).__name__,
                    "message": str(exc),
                },
            )
        result = {"success": True, "active": True, "variable_count": len(values)}
        if missing_output:
            result["message"] = "部分 Task 输出状态变量无法读取"
            return finish(
                result,
                code="opc_output_snapshot_partial",
                detail={"failures": failures},
            )
        return finish(result)

    @staticmethod
    def _template_condition_variables(
        registration: dict[str, Any],
        plc_device_id: str,
        registered_variables: list[str],
        variable_aliases: dict[str, str],
    ) -> list[str]:
        """提取当前模板输入/输出条件需要的已注册 PLC 变量。"""
        workspace = (
            registration.get("workspace") if isinstance(registration, dict) else None
        )
        templates = (
            workspace.get("templates", []) if isinstance(workspace, dict) else []
        )
        registered = set(registered_variables)
        required: list[str] = []
        for template in templates:
            if not isinstance(template, dict):
                continue
            for key in ("input_triggers", "output_triggers"):
                for trigger in template.get(key, []):
                    config = (
                        trigger.get("config", {}) if isinstance(trigger, dict) else {}
                    )
                    if config.get("plc_device_id") != plc_device_id:
                        continue
                    variable = variable_aliases.get(
                        str(config.get("variable", "")), str(config.get("variable", ""))
                    )
                    if variable in registered and variable not in required:
                        required.append(variable)
        return required

    @staticmethod
    def _active_task_condition_variables(
        workspace: dict[str, Any],
        plc_device_id: str,
        registered_variables: list[str],
        variable_aliases: dict[str, str],
        *,
        include_running_outputs: bool = True,
    ) -> list[str]:
        """提取非终态 Task 当前需要采样的 PLC 条件变量。"""
        templates = {
            str(template.get("id")): template
            for template in workspace.get("templates", [])
            if isinstance(template, dict)
        }
        registered = set(registered_variables)
        required: list[str] = []
        for instance in workspace.get("task_instances", []):
            if not isinstance(instance, dict):
                continue
            status = instance.get("status")
            trigger_key = (
                "input_triggers"
                if status in {"waiting", "pending"}
                else "output_triggers"
                if status == "running" and include_running_outputs
                else None
            )
            template = templates.get(str(instance.get("template_id")))
            if trigger_key is None or template is None:
                continue
            for trigger in template.get(trigger_key, []):
                config = trigger.get("config", {}) if isinstance(trigger, dict) else {}
                if config.get("plc_device_id") != plc_device_id:
                    continue
                configured_name = str(config.get("variable", ""))
                variable = variable_aliases.get(configured_name, configured_name)
                if variable in registered and variable not in required:
                    required.append(variable)
        return required

    @staticmethod
    def _registered_plc_variable_aliases(plc: Any) -> dict[str, str]:
        """仅公开已注册真实节点名对应的动作/界面别名。"""
        aliases = getattr(plc, "registered_variable_aliases", None)
        if callable(aliases):
            return {
                str(alias): str(canonical)
                for alias, canonical in dict(aliases()).items()
            }
        canonical_names = set(getattr(plc, "registered_variables", lambda: [])())
        return {
            str(alias): str(canonical)
            for alias, canonical in (getattr(plc, "_name_mapping", {}) or {}).items()
            if canonical in canonical_names
        }

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
                str(node.get("workflow_node_id") or node.get("uuid")): "preparing"
                for node in workflow.get("nodes", [])
                if isinstance(node, dict)
                and (node.get("workflow_node_id") or node.get("uuid"))
            }

            workflow_path = _write_temp_workflow(workflow)
            nodes, edges = load_workflow_nodes(workflow_path)
            ordered_nodes = build_execution_order(nodes, edges)
            record.append_log(
                f"workflow 解析完成，共 {len(ordered_nodes)} 个待执行节点"
            )
            if record.cancel_requested:
                raise WorkflowCancelled("workflow 已终止")
            default_config = self._preset.default_config
            csv_value = str(
                payload.get("csv") or default_config.get("csv") or ""
            ).strip()
            csv_path = _resolve_ui_path(csv_value, self._preset) if csv_value else None
            no_subscription = bool(payload.get("no_subscription", default_config.get("no_subscription", True)))
            graph_value = str(payload.get("graph") or default_config.get("graph") or GENERATED_GRAPH_SENTINEL).strip()
            opcua_url = str(payload.get("url") or default_config.get("url") or "").strip()

            if graph_value == GENERATED_GRAPH_SENTINEL:
                if not opcua_url:
                    raise ValueError(
                        "生成设备图需要填写 OPC UA URL，或指定已有 graph JSON"
                    )
                generated_graph = build_local_device_graph(
                    opcua_url=opcua_url,
                    csv_path=str(csv_path or csv_value or default_config.get("csv") or ""),
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

            record.append_log(
                "正在连接 OPC UA 并加载设备节点，这一步可能需要一些时间..."
            )
            device_key = (
                self._preset.id,
                graph_value,
                opcua_url,
                str(csv_path or ""),
                no_subscription,
            )
            devices = self._get_or_create_devices(
                device_key,
                {
                    "graph_file": graph_file,
                    "opcua_url": opcua_url or None,
                    "csv_path": csv_path,
                    "use_subscription": False if no_subscription else None,
                    "runtime_config": self._runtime_config,
                },
                record.append_log,
            )
            record.devices = devices
            task_workspace_path = str(
                payload.get("task_workspace_path")
                or f"{workflow.get('name', 'workflow')}.json"
            )
            distribution = self._publish_registered_plc_snapshot(
                devices,
                workflow_path=task_workspace_path,
                read_snapshot=True,
            )
            record.append_log(
                "已向 Task 排程分发 PLC 注册变量"
                if distribution.get("distributed")
                else f"PLC 变量未分发到 Task 排程：{distribution.get('message', '未知原因')}",
                level="info" if distribution.get("distributed") else "warning",
                category="opc",
                detail=distribution,
            )
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
                method_name = node_method(node)
                device_name = route_node_device(node, self._runtime_config)
                execution_id = f"workflow:{run_id}:{node_index}:{node.uuid}"
                history_context = {
                    "workflow_path": task_workspace_path,
                    "instance_id": run_id,
                    "sample_id": str(payload.get("sample_id") or ""),
                    "node_id": node.uuid,
                    "execution_id": execution_id,
                }
                try:
                    self._run_history_store.record_action_start(
                        workflow_path=history_context["workflow_path"],
                        instance_id=history_context["instance_id"],
                        sample_id=history_context["sample_id"],
                        node_id=history_context["node_id"],
                        execution_id=history_context["execution_id"],
                        device_id=device_name,
                        action_name=method_name,
                        params=node.param,
                    )
                except Exception:
                    # 持久化失败不得影响普通 Workflow 的设备动作。
                    _LOGGER.exception("Workflow 节点开始记录持久化失败")
                if timing_recorder is not None:
                    timing_recorder.start_step(
                        index=node_index,
                        total=len(ordered_nodes),
                        node_id=node.uuid,
                        device_name=device_name,
                        method=method_name,
                        params=node.param,
                    )
                record.append_log(
                    f"开始执行节点 {node.uuid}: {method_name}",
                    node_id=node.uuid,
                    detail={"method": method_name, "params": node.param},
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
                    try:
                        self._run_history_store.append_event(
                            **history_context,
                            level=level,
                            message=message,
                            detail=detail,
                        )
                    except Exception:
                        # 持久化失败不得中断节点日志或设备动作。
                        _LOGGER.exception("Workflow 节点日志持久化失败")

                logger = WorkflowLogger(writer=append_node_log)
                device = devices.get(device_name)
                balance_status_setter = (
                    getattr(device, "set_balance_status_callback", None)
                    if method_name == "dose_powder"
                    else None
                )
                if callable(balance_status_setter):

                    def update_balance_status(
                        payload: dict[str, Any],
                        node_id: str = node.uuid,
                    ) -> None:
                        value = payload.get("value")
                        value_text = f"{float(value):.3f} g" if isinstance(value, (int, float)) else "-- g"
                        state = str(payload.get("state") or "ok")
                        if state == "error":
                            suffix = f"（{payload.get('message') or '读取暂时失败'}）"
                            level = "warning"
                        elif state == "final":
                            suffix = "（最终读数）"
                            level = "info"
                        else:
                            suffix = "（每 2 秒刷新）"
                            level = "info"
                        with self._lock:
                            record.update_live_status(node_id, "s07_balance", payload)
                            record.update_live_log(
                                node_id,
                                "s07_balance",
                                f"S07 实时天平：{value_text}{suffix}",
                                level=level,
                            )

                    balance_status_setter(update_balance_status)
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
                    try:
                        error_detail = {
                            "type": type(exc).__name__,
                            "message": str(exc),
                            "traceback": traceback.format_exc(),
                        }
                        self._run_history_store.record_action_finish(
                            execution_id=execution_id,
                            status="failed",
                            error=error_detail,
                        )
                        self._run_history_store.record_incident(
                            category="action_error",
                            code="workflow_action_failed",
                            severity="error",
                            message=str(exc),
                            **history_context,
                            device_id=device_name,
                            action_name=method_name,
                            phase="普通 Workflow 执行",
                            detail=error_detail,
                        )
                    except Exception:
                        pass
                    record.node_statuses[node.uuid] = "failed"
                    record.append_log(
                        f"节点执行失败: {exc}", node_id=node.uuid, level="error"
                    )
                    raise
                finally:
                    if callable(balance_status_setter):
                        balance_status_setter(None)
                        with self._lock:
                            record.clear_live_status(node.uuid, "s07_balance")
                if timing_recorder is not None:
                    timing_recorder.finish_step(result=node_results)
                try:
                    self._run_history_store.record_action_finish(
                        execution_id=execution_id,
                        status="completed",
                        result=node_results,
                    )
                except Exception:
                    pass
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


def extract_registered_opc_values(snapshot: dict[str, Any]) -> dict[str, Any]:
    """只分发 PLC 成功读取到的原始值，避免将客户端状态对象传给排程服务。"""
    values: dict[str, Any] = {}
    for name, item in snapshot.items():
        if isinstance(item, dict):
            if item.get("success") is False or "value" not in item:
                continue
            value = item["value"]
        else:
            value = item
        if isinstance(value, (str, int, float, bool)) or value is None:
            values[name] = value
    return values


def _task_opc_connect_error_message(exc: BaseException, *, opcua_url: str = "") -> str:
    """将 OPC 连接异常转成可读错误，避免 TimeoutError 等空消息误导前端。"""
    message = str(exc).strip()
    if message:
        return message
    if isinstance(exc, TimeoutError):
        target = opcua_url or "目标 PLC"
        return f"PLC 连接超时：无法在限定时间内连接 {target}，请检查 IP/端口、VPN 与 OPC UA 服务是否已启动"
    if isinstance(exc, ConnectionRefusedError):
        return f"PLC 拒绝连接：{opcua_url or '目标地址'} 无 OPC UA 服务监听"
    if isinstance(exc, ModuleNotFoundError):
        return "缺少 pylabrobot 依赖，请在 unilab/mamba 环境中启动 workflow_ui"
    return f"PLC 连接失败：{exc.__class__.__name__}"


class TaskOrchestrationSnapshotPublisher:
    """将已由 PLC 注册的变量快照推送给 Task 排程服务。"""

    def __init__(
        self,
        base_url: str | None = None,
        sender: Any | None = None,
    ) -> None:
        self._base_url = (
            base_url
            or os.getenv("TASK_ORCHESTRATION_API_URL", "http://127.0.0.1:8091/api/v1")
        ).rstrip("/")
        self._sender = sender or self._send
        self._sequence = 0

    def publish(
        self,
        *,
        workflow_path: str,
        expected_version: int | None = None,
        plc_device_id: str,
        runtime_url: str,
        registered_variables: list[str],
        variable_aliases: dict[str, str] | None = None,
        values: dict[str, Any],
    ) -> None:
        """兼容旧调用：先注册，再按注册结果版本推送快照。"""
        registration = self.register(
            workflow_path=workflow_path,
            plc_device_id=plc_device_id,
            runtime_url=runtime_url,
            registered_variables=registered_variables,
            variable_aliases=variable_aliases,
        )
        self.publish_snapshot(
            workflow_path=workflow_path,
            expected_version=int(registration["version"]),
            plc_device_id=plc_device_id,
            values=values,
        )

    def register(
        self,
        *,
        workflow_path: str,
        plc_device_id: str,
        runtime_url: str,
        registered_variables: list[str],
        variable_aliases: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        if not runtime_url:
            raise RuntimeError("PLC runtime 未提供 OPC UA URL，无法分发变量注册表")
        if not registered_variables:
            raise RuntimeError("PLC runtime 未注册变量，无法分发快照")
        registration = self._sender(
            f"{self._base_url}/opc/registrations",
            {
                "workflow_path": workflow_path,
                "registration": {
                    "plc_device_id": plc_device_id,
                    "runtime_url": runtime_url,
                    "variables": registered_variables,
                    "aliases": variable_aliases or {},
                },
            },
        )
        if not isinstance(registration, dict) or not isinstance(
            registration.get("version"), int
        ):
            raise RuntimeError("Task 排程服务未返回 PLC 注册后的工作区版本")
        return registration

    def get_workspace(self, *, workflow_path: str) -> dict[str, Any]:
        """读取当前 Task 工作区，不重复写入 PLC 注册表。"""
        url = (
            f"{self._base_url}/workspaces?{urlencode({'workflow_path': workflow_path})}"
        )
        with urlopen(url, timeout=3) as response:
            if response.status >= 400:
                raise RuntimeError(
                    f"Task 排程服务拒绝读取工作区（HTTP {response.status}）"
                )
            workspace = json.loads(response.read().decode("utf-8"))
        if not isinstance(workspace, dict) or not isinstance(
            workspace.get("version"), int
        ):
            raise RuntimeError("Task 排程服务未返回有效工作区")
        return workspace

    def claim_action(self, **payload: Any) -> dict[str, Any]:
        """原子认领当前游标节点。"""
        return self._action_request("claim", payload)

    def succeed_action(self, **payload: Any) -> dict[str, Any]:
        """上报动作成功及资源释放计划。"""
        return self._action_request("succeed", payload)

    def fail_action(self, **payload: Any) -> dict[str, Any]:
        """上报动作失败，由服务端暂停整个工作区。"""
        return self._action_request("fail", payload)

    def _action_request(
        self,
        action: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        try:
            response = self._sender(
                f"{self._base_url}/actions:{action}",
                payload,
            )
        except HTTPError as exc:
            if exc.code != 409:
                raise
            body = exc.read().decode("utf-8", errors="replace")
            try:
                detail = json.loads(body).get("detail")
            except json.JSONDecodeError:
                detail = body
            if isinstance(detail, dict):
                code = str(detail.get("code") or "version_conflict")
                message = str(detail.get("message") or detail)
            else:
                code = "version_conflict"
                message = str(detail)
            raise TaskApiConflict(code, message) from exc
        if not isinstance(response, dict) or not isinstance(
            response.get("version"), int
        ):
            raise RuntimeError("Task 排程服务未返回动作更新后的工作区版本")
        return response

    def publish_snapshot(
        self,
        *,
        workflow_path: str,
        expected_version: int,
        plc_device_id: str,
        values: dict[str, Any],
    ) -> None:
        current_version = expected_version
        items = list(values.items())
        for start in range(0, len(items), 64):
            self._sequence += 1
            response = self._sender(
                f"{self._base_url}/opc/snapshots",
                {
                    "workflow_path": workflow_path,
                    "expected_version": current_version,
                    "plc_device_id": plc_device_id,
                    "sequence": self._sequence,
                    "values": dict(items[start: start + 64]),
                },
            )
            if isinstance(response, dict) and isinstance(response.get("version"), int):
                current_version = response["version"]
            else:
                current_version += 1

    @staticmethod
    def _send(url: str, payload: dict[str, Any]) -> dict[str, Any]:
        request = Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urlopen(request, timeout=3) as response:
            if response.status >= 400:
                raise RuntimeError(
                    f"Task 排程服务拒绝 PLC 快照（HTTP {response.status}）"
                )
            return json.loads(response.read().decode("utf-8"))


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
        params = _build_action_params(
            spec, dict(step.get("params") or step.get("param") or {})
        )

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
    return {
        "name": name or preset.default_workflow_name,
        "nodes": nodes,
        "edges": edges,
    }


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
        for target in sorted(
            outgoing[current], key=lambda node_id: original_index[node_id]
        ):
            incoming_count[target] -= 1
            if incoming_count[target] == 0:
                ready.append(target)
        ready.sort(key=lambda node_id: original_index[node_id])

    if len(ordered_ids) != len(nodes_by_id):
        raise ValueError("workflow 不能包含环，请删除形成循环依赖的连线")

    workflow_nodes = [
        _build_workflow_node_from_flow_node(nodes_by_id[node_id], preset)
        for node_id in ordered_ids
    ]
    return {
        "name": name or preset.default_workflow_name,
        "nodes": workflow_nodes,
        "edges": workflow_edges,
    }


def build_local_device_graph(
    opcua_url: str,
    csv_path: str = "",
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


def _runtime_supported_actions(
    preset: WorkflowPreset, runtime_config: RuntimeConfig
) -> dict[str, ActionSpec]:
    device_ids = set(runtime_config.device_factory.devices)
    if not device_ids:
        return preset.actions
    return {
        method: action
        for method, action in preset.actions.items()
        if (action.device_id or preset.target_device_id) in device_ids
    }


def _preset_for_runtime(
    preset: WorkflowPreset, runtime_config: RuntimeConfig
) -> WorkflowPreset:
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


def _validate_json_nesting(raw: bytes, max_depth: int = 100) -> None:
    """扫描原始 JSON bytes；字符串及转义中的括号不计入嵌套。"""
    depth = 0
    in_string = False
    escaped = False
    for byte in raw:
        if in_string:
            if escaped:
                escaped = False
            elif byte == 0x5C:  # backslash
                escaped = True
            elif byte == 0x22:  # quote
                in_string = False
            continue
        if byte == 0x22:
            in_string = True
        elif byte in (0x7B, 0x5B):  # { [
            depth += 1
            if depth > max_depth:
                raise HTTPException(status_code=413, detail="请求 JSON 嵌套超过 100 层")
        elif byte in (0x7D, 0x5D) and depth > 0:  # } ]
            depth -= 1


async def _read_bounded_json(request: FastAPIRequest) -> dict[str, Any]:
    """单次流式读取 JSON，避免 Starlette 预先缓存无界 body。"""
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > MAX_JSON_BYTES:
            raise HTTPException(status_code=413, detail="请求 body 超过 2 MiB")
        body.extend(chunk)
    raw = bytes(body)
    _validate_json_nesting(raw)
    try:
        payload = await asyncio.to_thread(json.loads, raw)
    except RecursionError as exc:
        raise HTTPException(status_code=400, detail="请求 JSON 嵌套过深") from exc
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=400, detail="请求 JSON 无效") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="请求 JSON 必须是对象")
    return payload


def _profile_http_exception(
    exc: BaseException,
    *,
    missing_status: int = 500,
) -> HTTPException:
    """稳定映射 profile API 错误，不暴露路径或系统异常文本。"""
    if isinstance(exc, ProfileLimitError):
        return HTTPException(
            status_code=422,
            detail={"validation_errors": exc.validation_errors},
        )
    if isinstance(exc, ProfileValidationError):
        return HTTPException(status_code=422, detail=exc.result)
    if isinstance(exc, RevisionConflict):
        return HTTPException(status_code=409, detail="profile revision 冲突")
    if isinstance(exc, FileNotFoundError):
        return HTTPException(status_code=missing_status, detail="模拟器配置不存在")
    if isinstance(exc, json.JSONDecodeError):
        return HTTPException(status_code=400, detail="profile JSON 无效")
    if isinstance(exc, ValueError):
        return HTTPException(status_code=400, detail=str(exc))
    if isinstance(exc, OSError):
        if exc.errno in (errno.ENOSPC, errno.EDQUOT):
            return HTTPException(status_code=507, detail="profile 存储空间不足")
        return HTTPException(status_code=500, detail="profile 存储操作失败")
    return HTTPException(status_code=500, detail="profile 操作失败")


def create_app(
    preset_name: str = "ai4c",
    runtime_config: RuntimeConfig | None = None,
    *,
    timing_enabled: bool = False,
    opc_simulator_manager: OpcSimulatorProcessManager | None = None,
    run_history_store: RunHistoryStore | None = None,
) -> FastAPI:
    preset = load_preset(preset_name)
    runtime_config = runtime_config or _load_preset_runtime_config(preset)
    active_preset = _preset_for_runtime(preset, runtime_config)
    app = FastAPI(title="szlab Workflow Debugger")
    manager = WorkflowRunManager(
        active_preset,
        runtime_config,
        timing_enabled=timing_enabled,
        run_history_store=run_history_store,
    )
    opc_simulator_manager = opc_simulator_manager or OpcSimulatorProcessManager(
        config_dir=OPC_SIMULATOR_CONFIG_DIR
    )
    OPC_SIMULATOR_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    migrate_legacy_opc_profiles(
        OPC_SIMULATOR_CONFIG_DIR,
        OPC_SIMULATOR_REFERENCE_DIR,
        reference_profile=OPC_SIMULATOR_REFERENCE_PROFILE,
    )
    _register_shutdown_handler(app, manager.shutdown)

    async def shutdown_opc_simulator() -> None:
        await asyncio.to_thread(opc_simulator_manager.shutdown)

    _register_shutdown_handler(app, shutdown_opc_simulator)

    assets_dir = FRONTEND_DIST_DIR / "assets"
    if assets_dir.exists():
        app.mount(
            "/assets", StaticFiles(directory=assets_dir), name="szlab_workflow_assets"
        )

    @app.get("/", response_class=HTMLResponse)
    async def index() -> Response:
        return _frontend_entry_response()

    @app.get("/api/actions", response_class=JSONResponse)
    async def list_actions() -> dict[str, Any]:
        return {
            "actions": [
                _action_to_dict(action, runtime_config)
                for action in active_preset.actions.values()
            ]
        }

    @app.get("/api/preset", response_class=JSONResponse)
    async def get_preset() -> dict[str, Any]:
        return {
            "id": preset.id,
            "title": preset.title,
            "runtime_config": preset.runtime_config,
            "default_workflow_name": active_preset.default_workflow_name,
            "default_config": active_preset.default_config,
            "actions": [
                _action_to_dict(action, runtime_config)
                for action in active_preset.actions.values()
            ],
        }

    @app.get("/api/s09-tip-status", response_class=JSONResponse)
    async def get_s09_tip_status() -> dict[str, Any]:
        if not DEFAULT_TIP_REUSE_STATE_PATH.exists():
            return {"initialized": False, "last_operation": None, "tips": {}, "solvents": {}}
        try:
            state = json.loads(DEFAULT_TIP_REUSE_STATE_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise HTTPException(status_code=500, detail="读取 S09 TIP 状态失败") from exc
        return state

    @app.get("/api/csv-variables", response_class=JSONResponse)
    async def csv_variables(csv_path: str = "") -> dict[str, Any]:
        value = (
            csv_path.strip()
            or str(active_preset.default_config.get("csv") or "").strip()
        )
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
            raise HTTPException(
                status_code=400, detail=f"无法识别 CSV 文件编码: {path.name}"
            )
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
    async def get_stack_status(
        task_workspace_path: str | None = None,
        task_workspace_version: int | None = None,
    ) -> dict[str, Any]:
        try:
            return manager.get_stack_status(
                task_workspace_path=task_workspace_path,
                task_workspace_version=task_workspace_version,
            )
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

    @app.post("/api/task-opc/connect", response_class=JSONResponse)
    async def connect_task_opc(payload: dict[str, Any]) -> dict[str, Any]:
        opcua_url = str(payload.get("url") or "").strip()
        workspace_path = str(payload.get("task_workspace_path") or "").strip()
        if not opcua_url:
            return {
                "success": False,
                "message": "请填写 OPC UA URL",
                "plc": {
                    "device_id": runtime_config.device_factory.plc_device_id
                    or "szlab_poly_plc",
                    "connected": False,
                    "registered_variables": [],
                },
            }
        if not workspace_path:
            return {
                "success": False,
                "message": "缺少当前 workflow 路径",
                "plc": {
                    "device_id": runtime_config.device_factory.plc_device_id
                    or "szlab_poly_plc",
                    "connected": False,
                    "registered_variables": [],
                },
            }
        plc_device_id = runtime_config.device_factory.plc_device_id or "szlab_poly_plc"
        try:
            devices = manager.connect_task_opc(
                opcua_url=opcua_url,
                workflow_path=workspace_path,
            )
            plc = devices.get(plc_device_id) or devices.get("szlab_poly_plc")
            if plc is None:
                raise RuntimeError("当前运行时未创建 SZLabPolyPLCDevice")
            registered = getattr(plc, "registered_variables", None)
            registered_variables = list(registered()) if callable(registered) else []
            registered_aliases = manager._registered_plc_variable_aliases(plc)
            distribution = manager._publish_registered_plc_snapshot(
                devices,
                workflow_path=workspace_path,
                read_snapshot=False,
            )
            return {
                "success": True,
                "plc": {
                    "device_id": plc_device_id,
                    "url": getattr(plc, "url", opcua_url),
                    "connected": bool(getattr(plc, "client", None)),
                    "registered_variables": registered_variables,
                    "variable_aliases": registered_aliases,
                },
                "task_orchestration": distribution,
            }
        except Exception as exc:
            error_message = _task_opc_connect_error_message(
                exc, opcua_url=opcua_url
            )
            manager._update_task_opc_error(
                workflow_path=workspace_path,
                operation="connect",
                code="opc_connection_failed",
                message=error_message,
                phase="connecting",
                device_id=plc_device_id,
                detail={
                    "exception_type": type(exc).__name__,
                    "message": str(exc),
                },
            )
            return {
                "success": False,
                "message": error_message,
                "plc": {
                    "device_id": plc_device_id,
                    "connected": False,
                    "registered_variables": [],
                },
            }

    @app.post("/api/task-opc/poll", response_class=JSONResponse)
    async def poll_task_opc(payload: dict[str, Any]) -> dict[str, Any]:
        workspace_path = str(payload.get("task_workspace_path") or "").strip()
        if not workspace_path:
            return {
                "success": False,
                "active": False,
                "message": "缺少当前 workflow 路径",
            }
        try:
            return manager.poll_task_opc(workflow_path=workspace_path)
        except Exception as exc:
            error_message = str(exc).strip() or type(exc).__name__
            manager._update_task_opc_error(
                workflow_path=workspace_path,
                operation="poll",
                code="opc_poll_failed",
                message=error_message,
                phase="polling",
                device_id=(
                    runtime_config.device_factory.plc_device_id
                    or "szlab_poly_plc"
                ),
                detail={
                    "exception_type": type(exc).__name__,
                    "message": str(exc),
                },
            )
            return {
                "success": False,
                "active": False,
                "message": error_message,
            }

    @app.post("/api/task-execution/preflight", response_class=JSONResponse)
    async def preflight_task_dispatch(
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        workspace_path = str(payload.get("task_workspace_path") or "").strip()
        workflow_payload = payload.get("workflow")
        expected_version = payload.get("expected_version")
        if not workspace_path:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "preflight_request_invalid",
                    "message": "缺少当前 workflow 路径",
                },
            )
        if type(expected_version) is not int or expected_version < 0:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "preflight_request_invalid",
                    "message": "expected_version 必须是非负整数",
                },
            )
        if not isinstance(workflow_payload, dict):
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "preflight_request_invalid",
                    "message": "缺少当前 workflow JSON",
                },
            )
        try:
            result = await asyncio.to_thread(
                manager.preflight_task_dispatch,
                workflow_path=workspace_path,
                expected_version=expected_version,
                workflow_payload=workflow_payload,
            )
        except TaskApiConflict as exc:
            raise HTTPException(
                status_code=409,
                detail={"code": exc.code, "message": str(exc)},
            ) from exc
        except ValueError as exc:
            raise HTTPException(
                status_code=422,
                detail={"code": "workflow_invalid", "message": str(exc)},
            ) from exc
        except Exception as exc:
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "preflight_failed",
                    "message": str(exc).strip() or type(exc).__name__,
                },
            ) from exc
        if not result.get("valid"):
            raise HTTPException(status_code=422, detail=result)
        return result

    @app.post("/api/task-execution/tick", response_class=JSONResponse)
    async def run_task_execution_tick(
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        workspace_path = str(payload.get("task_workspace_path") or "").strip()
        workflow_payload = payload.get("workflow")
        harvest_only = payload.get("harvest_only", False)
        if not workspace_path:
            return {
                "success": False,
                "message": "缺少当前 workflow 路径",
                "active": 0,
                "in_flight": 0,
                "claimed": 0,
                "completed": 0,
                "failed": 0,
            }
        if type(harvest_only) is not bool:
            return {
                "success": False,
                "message": "harvest_only 必须为 bool",
                "active": 0,
                "in_flight": 0,
                "claimed": 0,
                "completed": 0,
                "failed": 0,
            }
        if not harvest_only and not isinstance(workflow_payload, dict):
            return {
                "success": False,
                "message": "缺少当前 workflow JSON",
                "active": 0,
                "in_flight": 0,
                "claimed": 0,
                "completed": 0,
                "failed": 0,
            }
        try:
            return await asyncio.to_thread(
                manager.run_task_execution_cycle,
                workflow_path=workspace_path,
                workflow_payload=workflow_payload,
                harvest_only=harvest_only,
            )
        except Exception as exc:
            return {
                "success": False,
                "message": str(exc),
                "active": 0,
                "in_flight": 0,
                "claimed": 0,
                "completed": 0,
                "failed": 0,
            }

    @app.post("/api/task-execution/timings", response_class=JSONResponse)
    async def append_task_execution_timings(
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        workflow_path = str(payload.get("task_workspace_path") or "").strip()
        entries = payload.get("entries")
        if not workflow_path:
            return {"success": False, "message": "缺少当前 workflow 路径", "written": 0}
        if not isinstance(entries, list) or not entries or len(entries) > 200:
            return {
                "success": False,
                "message": "entries 必须是包含 1-200 项的数组",
                "written": 0,
            }
        try:
            written = await asyncio.to_thread(
                manager.append_task_execution_timings,
                workflow_path=workflow_path,
                entries=entries,
            )
            return {"success": True, "written": written}
        except Exception as exc:
            return {"success": False, "message": str(exc), "written": 0}

    @app.get("/api/task-execution/logs", response_class=JSONResponse)
    async def get_task_execution_logs(
        task_workspace_path: str = "",
        after_seq: int = 0,
        instance_id: str = "",
    ) -> dict[str, Any]:
        workflow_path = str(task_workspace_path or "").strip()
        if not workflow_path:
            return {
                "success": False,
                "message": "缺少当前 workflow 路径",
                "latest_seq": 0,
                "next_after_seq": max(0, int(after_seq)),
                "has_more": False,
                "entries": [],
            }
        try:
            payload = manager.list_task_action_logs(
                workflow_path=workflow_path,
                after_seq=max(0, int(after_seq)),
                instance_id=str(instance_id).strip() or None,
            )
            return {"success": True, **payload}
        except Exception as exc:
            return {
                "success": False,
                "message": str(exc),
                "latest_seq": 0,
                "next_after_seq": max(0, int(after_seq)),
                "has_more": False,
                "entries": [],
            }

    @app.get("/api/run-history/runs", response_class=JSONResponse)
    async def list_run_history(limit: int = 50) -> dict[str, Any]:
        try:
            return {
                "success": True,
                **await asyncio.to_thread(
                    manager.list_run_history,
                    limit=max(1, min(int(limit), 500)),
                ),
            }
        except Exception as exc:
            return {"success": False, "message": str(exc), "runs": []}

    @app.get("/api/run-history/timing", response_class=JSONResponse)
    async def get_timing_ledger(run_id: str = "") -> dict[str, Any]:
        try:
            return {
                "success": True,
                **await asyncio.to_thread(
                    manager.get_timing_ledger,
                    run_id=str(run_id).strip() or None,
                ),
            }
        except Exception as exc:
            return {"success": False, "message": str(exc)}

    @app.get("/api/run-history/stations", response_class=JSONResponse)
    async def get_station_ledger(
        run_id: str = "",
        station: str = "",
        limit: int = 1000,
    ) -> dict[str, Any]:
        try:
            return {
                "success": True,
                **await asyncio.to_thread(
                    manager.get_station_ledger,
                    run_id=str(run_id).strip() or None,
                    station=str(station).strip() or None,
                    limit=max(1, min(int(limit), 10000)),
                ),
            }
        except Exception as exc:
            return {"success": False, "message": str(exc), "stations": []}

    @app.get("/api/run-history/incidents", response_class=JSONResponse)
    async def get_incident_ledger(
        run_id: str = "",
        sample_id: str = "",
        status: str = "",
        limit: int = 1000,
    ) -> dict[str, Any]:
        try:
            return {
                "success": True,
                **await asyncio.to_thread(
                    manager.get_incident_ledger,
                    run_id=str(run_id).strip() or None,
                    sample_id=str(sample_id).strip() or None,
                    status=str(status).strip() or None,
                    limit=max(1, min(int(limit), 10000)),
                ),
            }
        except Exception as exc:
            return {"success": False, "message": str(exc), "incidents": []}

    @app.get("/api/run-history/export", response_class=FileResponse)
    async def export_run_history(
        ledger: str = "timing",
        run_id: str = "",
        format: str = "json",
    ) -> FileResponse:
        try:
            export_path = await asyncio.to_thread(
                manager.export_run_history,
                ledger=str(ledger).strip(),
                run_id=str(run_id).strip() or None,
                file_format=str(format).strip(),
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except OSError as exc:
            raise HTTPException(status_code=500, detail="台账导出失败") from exc
        return FileResponse(
            export_path,
            filename=export_path.name,
            media_type=(
                "application/json"
                if export_path.suffix == ".json"
                else "text/csv; charset=utf-8"
            ),
        )

    @app.post("/api/opc-simulator/profiles:generate", response_class=JSONResponse)
    async def generate_opc_simulator_profile(
        request: FastAPIRequest,
    ) -> dict[str, Any]:
        payload = await _read_bounded_json(request)
        allowed_fields = {
            "workflow",
            "templates",
            "scheduled_template_ids",
            "action_catalog",
            "variable_catalog",
            "name",
            "file_name",
            "opc_url",
        }
        unknown_fields = set(payload) - allowed_fields
        if unknown_fields:
            raise HTTPException(
                status_code=400,
                detail=f"生成请求含未知字段: {sorted(unknown_fields)[0]}",
            )
        if "action_catalog" not in payload:
            raise HTTPException(status_code=400, detail="生成请求缺少 action_catalog")
        file_name = payload.get("file_name") or "opc-simulator-profile.json"
        try:
            resolve_profile_path(file_name, OPC_SIMULATOR_CONFIG_DIR)
            generated = await asyncio.to_thread(
                generate_opc_simulator_draft,
                payload.get("workflow"),
                payload.get("templates"),
                payload.get("scheduled_template_ids"),
                payload.get("name"),
                payload.get("opc_url") or DEFAULT_OPC_SIMULATOR_URL,
                action_catalog=payload.get("action_catalog"),
                variable_catalog=payload.get("variable_catalog", []),
            )
            result = await asyncio.to_thread(
                validate_profile,
                generated["profile"],
                file_name,
            )
        except Exception as exc:
            raise _profile_http_exception(exc) from exc
        result["validation_errors"] = list(
            dict.fromkeys(
                [
                    *generated["validation_errors"],
                    *result["validation_errors"],
                ]
            )
        )
        return result

    @app.post("/api/opc-simulator/profiles:validate", response_class=JSONResponse)
    async def validate_opc_simulator_profile(
        request: FastAPIRequest,
    ) -> dict[str, Any]:
        payload = await _read_bounded_json(request)
        if set(payload) != {"profile", "file_name"}:
            raise HTTPException(
                status_code=400,
                detail="校验请求必须且只能包含 profile、file_name",
            )
        profile = payload["profile"]
        file_name = payload["file_name"]
        try:
            resolve_profile_path(file_name, OPC_SIMULATOR_CONFIG_DIR)
            return await asyncio.to_thread(validate_profile, profile, file_name)
        except Exception as exc:
            raise _profile_http_exception(exc) from exc

    @app.put(
        "/api/opc-simulator/profiles/{file_name}",
        response_class=JSONResponse,
    )
    async def save_opc_simulator_profile(
        file_name: str,
        request: FastAPIRequest,
    ) -> dict[str, Any]:
        payload = await _read_bounded_json(request)
        if set(payload) != {"profile"}:
            raise HTTPException(
                status_code=400,
                detail="保存请求必须且只能包含 profile",
            )
        try:
            return await asyncio.to_thread(
                save_profile,
                file_name,
                payload["profile"],
                OPC_SIMULATOR_CONFIG_DIR,
                expected_revision=request.headers.get("if-match"),
            )
        except Exception as exc:
            raise _profile_http_exception(exc) from exc

    @app.get(
        "/api/opc-simulator/profile-spec",
        response_class=JSONResponse,
    )
    async def read_opc_simulator_profile_spec() -> dict[str, Any]:
        """返回 OPC profile v2 生成规范 Markdown，供前端展示与大模型参照。"""
        path = OPC_SIMULATOR_PROFILE_SPEC_PATH
        if not path.is_file():
            raise HTTPException(status_code=404, detail="OPC profile 生成规范不存在")
        try:
            markdown = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise HTTPException(
                status_code=500,
                detail=f"无法读取 OPC profile 生成规范: {exc}",
            ) from exc
        if len(markdown.encode("utf-8")) > 512 * 1024:
            raise HTTPException(status_code=500, detail="OPC profile 生成规范过大")
        return {
            "schema_version": 2,
            "path": str(path.relative_to(REPO_ROOT)),
            "markdown": markdown,
        }

    @app.get(
        "/api/opc-simulator/profiles/reference/template",
        response_class=JSONResponse,
    )
    async def read_opc_simulator_reference_template() -> dict[str, Any]:
        """返回仓库内置 schema v2 可运行示例，供前端只读对照。"""
        try:
            return await asyncio.to_thread(
                read_profile,
                OPC_SIMULATOR_REFERENCE_PROFILE,
                OPC_SIMULATOR_REFERENCE_DIR,
            )
        except Exception as exc:
            raise _profile_http_exception(exc, missing_status=404) from exc

    @app.get("/api/opc-simulator/profiles", response_class=JSONResponse)
    async def list_opc_simulator_profiles() -> dict[str, Any]:
        """列出 task-orchestration/configs 下已保存的 profile JSON。"""
        try:
            config_dir = str(OPC_SIMULATOR_CONFIG_DIR.relative_to(REPO_ROOT))
        except ValueError:
            config_dir = str(OPC_SIMULATOR_CONFIG_DIR)
        try:
            await asyncio.to_thread(
                migrate_legacy_opc_profiles,
                OPC_SIMULATOR_CONFIG_DIR,
                OPC_SIMULATOR_REFERENCE_DIR,
                reference_profile=OPC_SIMULATOR_REFERENCE_PROFILE,
            )
            return {
                "config_dir": config_dir,
                "files": await asyncio.to_thread(
                    list_profile_files,
                    OPC_SIMULATOR_CONFIG_DIR,
                ),
            }
        except Exception as exc:
            raise _profile_http_exception(exc) from exc

    @app.get(
        "/api/opc-simulator/profiles/{file_name}",
        response_class=JSONResponse,
    )
    async def read_opc_simulator_profile(file_name: str) -> dict[str, Any]:
        try:
            return await asyncio.to_thread(
                read_profile,
                file_name,
                OPC_SIMULATOR_CONFIG_DIR,
            )
        except Exception as exc:
            raise _profile_http_exception(exc, missing_status=404) from exc

    @app.post("/api/opc-simulator/start", response_class=JSONResponse)
    async def start_opc_simulator(
        request: FastAPIRequest,
    ) -> dict[str, Any]:
        payload = await _read_bounded_json(request)
        allowed_fields = {
            "file_name",
            "expected_revision",
            "allow_unsafe_url",
        }
        if set(payload) - allowed_fields:
            raise HTTPException(status_code=400, detail="模拟器启动请求含未知字段")
        file_name = payload.get("file_name")
        expected_revision = payload.get("expected_revision")
        allow_unsafe_url = payload.get("allow_unsafe_url", False)
        if not isinstance(file_name, str) or not file_name:
            raise HTTPException(status_code=400, detail="file_name 必须是非空字符串")
        if not isinstance(expected_revision, str) or not expected_revision:
            raise HTTPException(
                status_code=400,
                detail="expected_revision 必须是非空字符串",
            )
        if type(allow_unsafe_url) is not bool:
            raise HTTPException(
                status_code=400,
                detail="allow_unsafe_url 必须是严格布尔值",
            )
        try:
            return await asyncio.to_thread(
                opc_simulator_manager.start,
                file_name,
                expected_revision,
                allow_unsafe_url=allow_unsafe_url,
            )
        except InvalidSimulatorRevision as exc:
            raise HTTPException(
                status_code=400,
                detail="模拟器 revision 格式无效",
            ) from exc
        except SimulatorInputError as exc:
            raise HTTPException(status_code=400, detail="模拟器启动参数无效") from exc
        except SimulatorConfigNotFound as exc:
            raise HTTPException(status_code=404, detail="模拟器配置不存在") from exc
        except SimulatorRevisionConflict as exc:
            raise HTTPException(status_code=409, detail="profile revision 冲突") from exc
        except UnsafeSimulatorUrlConfirmationRequired as exc:
            raise HTTPException(
                status_code=422,
                detail="非默认 OPC URL 需明确确认风险",
            ) from exc
        except InvalidSimulatorConfig as exc:
            raise HTTPException(status_code=422, detail="模拟器配置不可运行") from exc
        except SimulatorAlreadyRunning as exc:
            raise HTTPException(
                status_code=409,
                detail="已有 OPC 模拟器正在运行",
            ) from exc
        except SimulatorSpawnError as exc:
            raise HTTPException(status_code=500, detail="启动模拟器进程失败") from exc
        except SimulatorStorageFull as exc:
            raise HTTPException(status_code=507, detail="模拟器存储空间不足") from exc
        except SimulatorStorageError as exc:
            raise HTTPException(status_code=500, detail="模拟器存储操作失败") from exc
        except Exception as exc:
            raise HTTPException(status_code=500, detail="模拟器系统操作失败") from exc

    @app.get("/api/opc-simulator/status", response_class=JSONResponse)
    async def get_opc_simulator_status() -> dict[str, Any]:
        try:
            return opc_simulator_manager.status()
        except Exception as exc:
            raise HTTPException(status_code=500, detail="读取模拟器状态失败") from exc

    @app.post("/api/opc-simulator/stop", response_class=JSONResponse)
    async def stop_opc_simulator(
        request: FastAPIRequest,
    ) -> dict[str, Any]:
        payload = await _read_bounded_json(request)
        if set(payload) - {"expected_run_id"}:
            raise HTTPException(status_code=400, detail="停止请求含未知字段")
        expected_run_id = payload.get("expected_run_id")
        if expected_run_id is not None and (
            not isinstance(expected_run_id, str)
            or re.fullmatch(r"[0-9a-f]{32}", expected_run_id) is None
        ):
            raise HTTPException(
                status_code=400,
                detail="expected_run_id 格式无效",
            )
        try:
            return await asyncio.to_thread(
                opc_simulator_manager.stop,
                expected_run_id=expected_run_id,
            )
        except SimulatorRunIdentityConflict as exc:
            raise HTTPException(
                status_code=409,
                detail="模拟器运行身份冲突",
            ) from exc
        except StopTimeout as exc:
            raise HTTPException(
                status_code=504,
                detail="模拟器停止超时，恢复结果不确定",
            ) from exc
        except Exception as exc:
            raise HTTPException(status_code=500, detail="停止模拟器失败") from exc

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

    run_history_store = RunHistoryStore()
    history_paths = run_history_store.initialize()
    print(f"运行历史数据库已初始化: {history_paths['database_path']}")

    url = f"http://{host if host != '0.0.0.0' else 'localhost'}:{port}/"
    if open_browser:
        webbrowser.open(url)
    uvicorn.run(
        create_app(
            preset_name=preset_name,
            runtime_config=runtime_config,
            timing_enabled=timing_enabled,
            run_history_store=run_history_store,
        ),
        host=host,
        port=port,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Uni-Lab 本地 workflow 调试界面")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址")
    parser.add_argument("--port", type=int, default=8014, help="监听端口")
    parser.add_argument(
        "--preset", default="ai4c", help="preset 名称，Docker 默认使用 szlab_mixer"
    )
    parser.add_argument(
        "--runtime-config",
        type=Path,
        default=None,
        help="覆盖 preset 中的运行配置 JSON",
    )
    parser.add_argument(
        "--no-browser", action="store_true", help="启动时不自动打开浏览器"
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="启用 preset.debug_config 中定义的调试环境变量",
    )
    parser.add_argument(
        "--timing", action="store_true", help="临时记录 workflow 排程耗时"
    )
    return parser


def _build_action_params(
    spec: ActionSpec, raw_params: dict[str, Any]
) -> dict[str, Any]:
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


def _build_workflow_node_from_flow_node(
    flow_node: dict[str, Any], preset: WorkflowPreset
) -> dict[str, Any]:
    node_id = str(flow_node.get("id", "")).strip()
    data = flow_node.get("data") or {}
    method = str(data.get("method", "")).strip()
    if method not in preset.actions:
        raise ValueError(f"不支持的动作: {method}")

    spec = preset.actions[method]
    params = _build_action_params(
        spec, dict(data.get("params") or data.get("param") or {})
    )
    raw_opc_variables = data.get("opc_variables", [])
    if not isinstance(raw_opc_variables, list):
        raise ValueError("workflow 节点 opc_variables 必须是数组")
    if len(raw_opc_variables) > 500:
        raise ValueError("workflow 节点 opc_variables 超过 500 项")
    opc_variables: list[str] = []
    for variable in raw_opc_variables:
        if not isinstance(variable, str) or not variable.strip():
            raise ValueError("workflow 节点 opc_variables 必须是非空字符串数组")
        normalized = variable.strip()
        if normalized not in opc_variables:
            opc_variables.append(normalized)

    node = {
        "workflow_node_id": node_id,
        "device_id": data.get("device_id")
        or spec.device_id
        or preset.target_device_id,
        "method": method,
        "params": params,
        "opc_variables": opc_variables,
    }
    if data.get("execution_disabled") is True or data.get("disabled") is True:
        node["disabled"] = True
    return node


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
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", suffix=".json", delete=False
    ) as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
        return Path(handle.name)


def _render_template_value(value: Any, replacements: dict[str, Any]) -> Any:
    if isinstance(value, str) and value.startswith("${") and value.endswith("}"):
        key = value[2:-1]
        return replacements.get(key, value)
    if isinstance(value, list):
        return [_render_template_value(item, replacements) for item in value]
    if isinstance(value, dict):
        return {
            key: _render_template_value(item, replacements)
            for key, item in value.items()
        }
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


def _action_to_dict(
    action: ActionSpec, runtime_config: RuntimeConfig | None = None
) -> dict[str, Any]:
    data = {
        "method": action.method,
        "label": action.label,
        "description": action.description,
        "needs_position": action.needs_position,
        "params": action.params,
        "device_id": action.device_id,
    }
    if runtime_config is not None:
        data["opc_variables"] = _collect_action_level_opc_variables(
            action.method, runtime_config
        )
    return data


def _collect_action_level_opc_variables(
    method: str, runtime_config: RuntimeConfig
) -> list[str]:
    from scripts.szlab_action_sensor_variables import resolve_robot_action_opc_variables

    snapshot_config = runtime_config.opc_snapshot
    variables = list(snapshot_config.common_variables)
    variables.extend(snapshot_config.action_variables.get(method, []))
    if method.startswith("submit_"):
        variables.extend(resolve_robot_action_opc_variables("szlab_mixer_robot", method))
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
        "live_statuses": {
            node_id: {key: dict(payload) for key, payload in statuses.items()}
            for node_id, statuses in record.live_statuses.items()
        },
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
        variable_names = [
            str(name).strip() for name in skip_variables if str(name).strip()
        ]
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


def main() -> int:
    args = build_parser().parse_args()
    runtime_config = (
        load_runtime_config(args.runtime_config) if args.runtime_config else None
    )
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
    return 0


if __name__ == "__main__":
    main()
