# AGENTS.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

Also follow the monorepo-level rules in `../AGENTS.md`.

## Build & Development

```bash
# Install in editable mode (requires mamba env with python 3.11)
pip install -e .
uv pip install -r unilabos/utils/requirements.txt

# Run with a device graph
unilab --graph <graph.json> --config <config.py> --backend ros
unilab --graph <graph.json> --config <config.py> --backend simple  # no ROS2 needed

# Common CLI flags
unilab --app_bridges websocket fastapi    # communication bridges
unilab --test_mode                        # simulate hardware, no real execution
unilab --check_mode                       # CI validation of registry imports
unilab --skip_env_check                   # skip auto-install of dependencies
unilab --visual rviz|web|disable          # visualization mode
unilab --is_slave                         # run as slave node

# Workflow upload subcommand（P6.1 新增 --target_device；P6.1.1 新增 --target_model）
unilab workflow_upload -f <workflow.json> -n <name> --tags tag1 tag2
unilab workflow_upload -f <workflow.json> --target_device prcxi                    # P6.1 默认；同上 P6 行为
unilab workflow_upload -f <workflow.json> --target_device prcxi --target_model 9320  # P6.1.1：型号粒度
unilab workflow_upload -f <workflow.json> --target_device beckman                  # 未来支持，需在 YAML 中声明 target_devices.beckman

# Tests
pytest tests/                              # all tests
pytest tests/resources/test_resourcetreeset.py  # single test file
pytest tests/resources/test_resourcetreeset.py::TestClassName::test_method  # single test
```

## Architecture

### Startup Flow

`unilab` CLI → `unilabos/app/main.py:main()` → loads config → builds registry → reads device graph (JSON/GraphML) → starts backend thread (ROS2/simple) → starts FastAPI web server + WebSocket client.

### Core Layers

**Registry** (`unilabos/registry/`): Singleton `Registry` class discovers and catalogs all device types, resource types, and communication devices from YAML definitions. Device types live in `registry/devices/*.yaml`, resources in `registry/resources/`, comms in `registry/device_comms/`. The registry resolves class paths to actual Python classes via `utils/import_manager.py`.

**Resource Tracking** (`unilabos/resources/resource_tracker.py`): Pydantic-based `ResourceDict` → `ResourceDictInstance` → `ResourceTreeSet` hierarchy. `ResourceTreeSet` is the canonical in-memory representation of all devices and resources, used throughout the system. Graph I/O is in `resources/graphio.py` (reads JSON/GraphML device topology files into `nx.Graph` + `ResourceTreeSet`).

**Device Drivers** (`unilabos/devices/`): 30+ hardware drivers organized by device type (liquid_handling, hplc, balance, arm, etc.). Each driver is a Python class that gets wrapped by `ros/device_node_wrapper.py:ros2_device_node()` to become a ROS2 node with publishers, subscribers, and action servers.

**ROS2 Layer** (`unilabos/ros/`): `device_node_wrapper.py` dynamically wraps any device class into `ROS2DeviceNode` (defined in `ros/nodes/base_device_node.py`). Preset node types in `ros/nodes/presets/` include `host_node`, `controller_node`, `workstation`, `serial_node`, `camera`. Messages use custom `unilabos_msgs` (pre-built, distributed via releases).

**Protocol Compilation** (`unilabos/compile/`): 20+ protocol compilers (add, centrifuge, dissolve, filter, heatchill, stir, pump, etc.) that transform YAML protocol definitions into executable sequences.

**Communication** (`unilabos/device_comms/`): Hardware communication adapters — OPC-UA client, Modbus PLC, RPC, and a universal driver. `app/communication.py` provides a factory pattern for WebSocket client connections to the cloud.

**Web/API** (`unilabos/app/web/`): FastAPI server with REST API (`api.py`), Jinja2 template pages (`pages.py`), and HTTP client for cloud communication (`client.py`). Runs on port 8002 by default.

### Configuration System

- **Config classes** in `unilabos/config/config.py`: `BasicConfig`, `WSConfig`, `HTTPConfig`, `ROSConfig` — all class-level attributes, loaded from Python config files
- Config files are `.py` files with matching class names (see `config/example_config.py`)
- Environment variables override with prefix `UNILABOS_` (e.g., `UNILABOS_BASICCONFIG_PORT=9000`)
- Device topology defined in graph files (JSON with node-link format, or GraphML)

### Key Data Flow

1. Graph file → `graphio.read_node_link_json()` → `(nx.Graph, ResourceTreeSet, resource_links)`
2. `ResourceTreeSet` + `Registry` → `initialize_device.initialize_device_from_dict()` → `ROS2DeviceNode` instances
3. Device nodes communicate via ROS2 topics/actions or direct Python calls (simple backend)
4. Cloud sync via WebSocket (`app/ws_client.py`) and HTTP (`app/web/client.py`)

### Test Data

Example device graphs and experiment configs are in `unilabos/test/experiments/` (not `tests/`). Registry test fixtures in `unilabos/test/registry/`.

### Labware Mapping Table (`labware_mapping.yaml`) — P6 + P6.1 + P6.1.1

Opentrons → 目标仪器（PRCXI / Beckman / Tecan ...）的「槽位重映射 + labware 归类 +
class_name 选择」全部外化到项目根的
[`labware_mapping.yaml`](./labware_mapping.yaml)（与 `pyproject.toml` 同级，最显眼的位置）。
要新增 SKU、新厂商、新型号、或调整 tip 量程档时，**只改 YAML，不改 Python**。

- **YAML 两段顶层语义**（P6.1.1 起 `slot_remap` 已下沉到 `target_devices` 内）：
  - `kinds` — 顺序敏感的 regex；把 labware 字符串归到 `trash / tip_rack / tube_rack / plate`。**全局段**，与目标仪器无关。
  - `target_devices.<name>` — 按目标仪器组织的规则段，内含三个字段：
    - `slot_remap` — 替代历史 `_map_deck_slot`（例：`4 → 13`、`8 → 14`、`12+trash → 16`）。
    - `rules` — 顺序敏感的「`kind + hole_count + volume_min/volume_max` → `class_name`」规则，首个命中胜出。
    - `models.<model_name>` — 可选的型号粒度覆盖（slot_remap / rules）；缺失字段自动继承厂商级。
- **`target_devices` 内段名约定**：
  - `default` — **固定段名**，兜底物料集 + 兜底 `slot_remap`。caller 传入的 `target_device` 在 `target_devices`
    下未声明时，自动 fallback 到此段（loader 单次 warning，下游消费方零感知)。
    **第一版按 prcxi 内容拷贝填充**（值仍是 `PRCXI_*`），但与 prcxi 段在 YAML 中
    各自独立，可独立演进。**`default` 不支持 `models` 子段**——型号粒度差异必须落到具体仪器段。
  - `prcxi` / `beckman` / `tecan` / ... — 具体仪器段（厂商粒度）；caller 显式
    `--target_device <name>` 时命中。可在 `models.<model>` 下声明同厂商不同型号的差异。
- **4 段 fallback 链**（`slot_remap` / `rules` 共用）：
  1. `target_devices.<device>.models.<model>.<field>`（caller 同时传 device + model）
  2. `target_devices.<device>.<field>`（厂商级；步骤 1 缺字段时静默 fallback）
  3. `target_devices.default.<field>`（caller 传未声明 device，或步骤 2 缺字段；打 warning）
  4. `_BUILTIN_DEFAULT.target_devices.default.<field>`（YAML 误删 default 段时的最后兜底）
- **CLI 用法**：
  - P6.1：`unilab workflow_upload -f <workflow.json> --target_device prcxi`
    （`--target_device` snake-case，默认 `prcxi`；未声明的名字自动 fallback 到 `default` 段）。
  - P6.1.1：可加 `--target_model <name>`（snake，可省略，默认 `None`）。
    例：`unilab workflow_upload -f <workflow.json> --target_device prcxi --target_model 9320`。
- **入口代码**：`unilabos/workflow/labware_mapping.py` 暴露 `remap_slot` / `infer_kind` /
  `resolve_target_class` / `reload_mapping`。
  API 签名（P6.1.1）：
  - `remap_slot(raw_slot, object_type="", *, target_device="prcxi", target_model=None)`
  - `resolve_target_class(target_device, kind, hole_count=None, volume=None, *, target_model=None)`
  `workflow/common.py` 中 `_map_deck_slot` / `_infer_reagent_kind` /
  `_apply_tip_rack_class_from_transfer_volumes` / `_apply_target_labware_class_auto_match` /
  `_reconcile_slot_carrier_target_class` 都已转调 YAML 并透传 `target_device` / `target_model`；
  YAML 未命中（孔数 / 体积超出 default 段覆盖范围）时 fallback 到
  `prcxi_labware.get_prcxi_labware_template_specs` 的模板打分匹配，并打 warning 提示「请补到映射表」。
- **`labware_info` 字段重命名**：P6 的 `prcxi_class_name` → P6.1 的 `target_class_name`，
  13 处全部同步刷新；旧 schema（顶层 `vendors` / `slot_remap` 或任一 rule 内 `prcxi_class`）
  会触发 loader warning 并整段 fallback 到 builtin 默认表。
- **测试**：
  - `pytest tests/workflow/test_labware_mapping.py` —— 45 项单元测试（含 P6.1 + P6.1.1 用例：
    `test_remap_slot_model_level_overrides_device_level`、
    `test_remap_slot_model_inherits_device_when_field_missing`、
    `test_legacy_top_level_slot_remap_rejected`、
    `test_default_section_models_subsection_warns` 等）。
  - `pytest tests/workflow/test_build_protocol_graph_target_device.py` —— 6 项集成
    测试（默认 / 显式 prcxi / unknown 段 fallback / per-device tip class / 字段重命名 /
    P6.1.1 model-level slot_remap）。
- **设计文档**：[`product_designs/protocol_convert/06-labware-mapping-table.md`](../product_designs/protocol_convert/06-labware-mapping-table.md)
  （§11.7 = P6.1 多目标仪器选择，§11.8 = P6.1.1 槽位映射按厂商+型号分叉）。

### P2 跨 slot transfer_liquid 合并（v2，已落地）

当一次 phase 中存在「单源吸取 → 跨多个 plate 分发」（典型 `steps/51b9a5.json` 9 plate × 12 well = 108 条 1:1 dispense），Stage 2 + Stage 3 现在能把它折叠成 **1 个 merged set_liquid_from_plate + 1 个 transfer_liquid** 节点。

- **Stage 2**（[`Protocols/protocol_converter/change_to_transfer_group.py`](../Protocols/protocol_converter/change_to_transfer_group.py)）：
  - `_pair_mergeable` 只要求源 slot / tip 量程档 / use_channels 一致；不再要求 `_target_slot` 相同。
  - `_merge_two_transfer_actions` 维护 `_target_slots: list[int]`（与 `_target_wells` 平行，每次 dispense 一条）。
  - `export_transfer_actions` 通过 `_register_target_reagent_key` 统一注册 reagent_key：跨 slot 时按 `_target_slots` 顺序拼出 `action_args.targets: list[str]`（同板退化为 `str`）。
  - 末尾 `pop` 全部 `_` 前缀字段（包括新增的 `_target_slots`）。
- **Stage 3**（[`Uni-Lab-OS/unilabos/workflow/common.py`](unilabos/workflow/common.py)）：
  - 新增 `_emit_merged_set_liquid(...)`：对 `params.targets: list[str]` 的 transfer_liquid 节点，在其上游插入一个 **merged `set_liquid_from_plate`** 跨板聚合器；其 `param.wells` 是按 dispense 顺序通过 cursor 走 `reagent[key].well` 得出的有序跨板 well refs；多入边（每 plate 一条 `create_resource.labware → wells_identifier`），单出边（`output_wells → transfer_liquid.targets_identifier`）。
  - 把 `params["targets"]` 改写为 synthetic str `_merged_targets_<idx>` 并注册 `resource_last_writer`，保证 INPUT_PORT_MAPPING 走 P3 既有的单边路径。
  - `OUTPUT_PORT_MAPPING` 在原始 `step.param.targets` 为 `list[str]` 时为每个 reagent_key 分别注册 transfer_liquid 的下游 writer。
- **PRCXI runtime**（[`prcxi/prcxi.py`](unilabos/devices/liquid_handling/prcxi/prcxi.py)）：`change_slots` 改为遍历所有 source / target 的 parent plate 并按 plate name 去重（跨板 4 个 plate 都能 `update_pipetting_position`）。
- **`liquid_handler_abstract.transfer_liquid`**：**完全不改动**，主循环 `i % num_targets` 与单边 + 单 list 完全兼容。

CLI 行为不变：现有 `unilab workflow_upload -f <workflow.json> ...` 一切照旧；跨 slot 协议自动走 v2 路径。

测试：
- `pytest Protocols/protocol_converter/tests/test_cross_slot_merge.py` — Stage 2 单测 10 项。
- `pytest tests/workflow/test_common_cross_slot_v2.py` — Stage 3 集成测试 6 项。
- `pytest tests/devices/liquid_handling/test_set_liquid_from_plate_cross_plate.py` — device 跨板单测 6 项（pylabrobot 不全时优雅 skip）。

设计文档：[`product_designs/protocol_convert/02-cross-slot-merge.md`](../product_designs/protocol_convert/02-cross-slot-merge.md)（§9 v2 设计 + §11 落地记录）。

## Code Conventions

- Code comments and log messages in simplified Chinese
- Python 3.11+, type hints expected
- Pydantic models for data validation (`resource_tracker.py`)
- Singleton pattern via `@singleton` decorator (`utils/decorator.py`)
- Dynamic class loading via `utils/import_manager.py` — device classes resolved at runtime from registry YAML paths
- CLI argument dashes auto-converted to underscores for consistency

## Licensing

- Framework code: GPL-3.0
- Device drivers (`unilabos/devices/`): DP Technology Proprietary License — do not redistribute
