# szlab 本地调试 UI 新动作接入教程

本教程使用 `tests/szlab/example/` 接入一个本地调试动作。动作实现、UI 配置、运行配置、OPC 节点表都在 `example/` 目录内。

## 1. 示例目录

```text
tests/szlab/example/
├── README.md
├── __init__.py
├── ai4c_actions.py
├── ai4c_preset.json
├── ai4c_runtime.json
└── ai4c_sim_updated.csv
```

文件用途：

- `ai4c_actions.py`：动作实现。
- `ai4c_preset.json`：UI 动作面板、默认运行配置、设备图模板。
- `ai4c_runtime.json`：设备工厂、设备路由、OPC 快照变量。
- `ai4c_sim_updated.csv`：OPC UA 节点表。
- `README.md`：示例说明。

## 2. 启动示例 UI

首次使用或前端代码更新后，需要先安装依赖并构建前端资源：

```bash
cd unilabos_local_ui
npm install
npm run build
cd ..
```

如果 `unilabos_local_ui/dist/` 已经是最新的，可以跳过这一步。

在仓库根目录执行：

```bash
PYTHONPATH=. python -m scripts.run_workflow_local \
  --ui \
  --port 8014 \
  --preset example/ai4c_preset.json
```

页面操作：

1. 点击 `运行配置`。
2. 填写 OPC UA URL。
3. CSV 填写 `ai4c_sim_updated.csv`。
4. 勾选 `禁用 OPC UA 订阅`。
5. 从左侧添加动作节点。
6. 连接节点。
7. 点击 `校验流程`。
8. 点击 `运行`。

## 3. 本教程新增的动作

新增方法名：

`place_well_plate_to_new_station`

动作逻辑：

1. 读取 `New_Station_Occupied`。
2. 值为 `true` 时返回失败。
3. 值为 `false` 时写入机械臂目标位置、取放料代码、动作代码、触发信号。
4. 等待 `Robotic_Arm_Action_Complete` 变为 `true`。
5. 复位 `Robotic_Arm_Action_Trigger`。
6. 等待 `Robotic_Arm_Action_Complete` 变为 `false`。
7. 返回成功。

## 4. 编写动作实现

打开：

`tests/szlab/example/ai4c_actions.py`

在 `RoboticArmTargetPosition` 中加入新工位枚举：

```python
class RoboticArmTargetPosition(IntEnum):
    PIPETTING_STATION = 3
    NEW_STATION = 4
    PLATE_LOADING_RACK = 6
```

在 `ExampleAI4CActions` 中加入动作方法：

```python
def place_well_plate_to_new_station(self) -> dict[str, Any]:
    if bool(self._read_plc_variable("New_Station_Occupied")):
        return {"success": False, "message": "新工位已有孔板"}

    return self._run_robot_arm_action(
        target_position=RoboticArmTargetPosition.NEW_STATION,
        pick_place_code=1,
        arm_action=RoboticArmAction.PLACE,
        success_message="将孔板放置到新工位完成",
    )
```

动作方法规则：

- 方法名等于 UI preset 中的 `method`。
- 成功返回 `{"success": True, "message": "..."}`。
- 失败返回 `{"success": False, "message": "..."}`。
- 读 OPC 变量使用 `_read_plc_variable(node_name)`。
- 写 OPC 变量使用 `_write_plc_variable(node_name, value)`。
- 等待布尔变量使用 `_wait_plc_bool(node_name, expected)`。
- 触发标准机械臂动作使用 `_run_robot_arm_action(...)`。

## 5. 添加 UI 动作卡片

打开：

`tests/szlab/example/ai4c_preset.json`

在 `actions` 数组中加入：

```json
{
  "method": "place_well_plate_to_new_station",
  "label": "放到新工位",
  "description": "教学示例：机械臂将孔板放置到新工位",
  "params": []
}
```

字段说明：

- `method`：动作方法名。
- `label`：左侧动作卡片标题。
- `description`：左侧动作卡片说明。
- `params`：参数列表。

## 6. 配置 OPC 快照变量

打开：

`tests/szlab/example/ai4c_runtime.json`

在 `opc_snapshot.action_variables` 中加入：

```json
"place_well_plate_to_new_station": ["New_Station_Occupied"]
```

完整片段：

```json
"opc_snapshot": {
  "common_variables": [
    "Robotic_Arm_Idle",
    "Robotic_Arm_Action_Complete",
    "Robotic_Arm_Target_Position_Code",
    "Robotic_Arm_Target_Pick_Place_Code",
    "Robotic_Arm_Action_Code",
    "Robotic_Arm_Action_Trigger"
  ],
  "action_variables": {
    "place_well_plate_to_pipetting_station": ["Pipetting_Station_Occupied"],
    "place_well_plate_to_new_station": ["New_Station_Occupied"]
  },
  "param_variables": {
    "pick_well_plate_from_loading_rack": [
      {
        "param": "position",
        "template": "Well_Plate_Loading_Rack_InPut[{position_minus_1}]"
      }
    ]
  }
}
```

采样规则：

- `common_variables`：每个动作运行前后都会采样。
- `action_variables`：指定动作运行前后额外采样。
- `param_variables`：指定动作按参数生成变量名。
- `{position_minus_1}`：`position` 参数减一后的值。

## 7. 添加 OPC 节点表行

打开：

`tests/szlab/example/ai4c_sim_updated.csv`

加入：

```csv
新工位占位,New_Station_Occupied,VARIABLE,BOOLEAN,Chinese,ns=4;s=UniLab|新工位占位
```

字段说明：

- `Name`：中文节点名。
- `EnglishName`：动作代码和 runtime 配置使用的变量名。
- `NodeType`：节点类型。
- `DataType`：变量类型。
- `NodeLanguage`：节点语言。
- `NodeId`：OPC UA NodeId。

## 8. 确认运行目标类

打开：

`tests/szlab/example/ai4c_runtime.json`

确认 `target_class`：

```json
"target_class": "tests.szlab.example.ai4c_actions.ExampleAI4CActions"
```

确认 `direct_plc_command_method`：

```json
"direct_plc_command_method": "_call_plc_command"
```

运行时行为：

1. runner 创建 `AI4CPLCDevice`。
2. runner 创建 `ExampleAI4CActions`。
3. runner 将 `ExampleAI4CActions._call_plc_command` 连接到 PLC 设备。
4. 动作方法通过 `_read_plc_variable()` 和 `_write_plc_variable()` 访问 PLC。

## 9. 运行新动作

执行：

```bash
PYTHONPATH=. python -m scripts.run_workflow_local \
  --ui \
  --port 8014 \
  --preset example/ai4c_preset.json
```

页面操作：

1. 点击 `运行配置`。
2. 填写 OPC UA URL。
3. 点击 `完成`。
4. 添加 `放到新工位` 节点。
5. 点击 `校验流程`。
6. 点击 `运行`。

验收项：

- 画布节点显示运行状态。
- 右侧 `运行日志` 出现 workflow 日志。
- 右侧节点 Tab 出现 `放到新工位`。
- 画布下方 `OPC 变量变化` 表格出现 `New_Station_Occupied`。
- 表格显示 `Workflow Node`、`NodeID`、`Name`、`Value Begin`、`Value End`。

## 10. 带参数动作示例

动作方法：

```python
def pick_well_plate_from_new_rack(self, position: int = 1) -> dict[str, Any]:
    if position < MIN_RACK_POSITION or position > MAX_RACK_POSITION:
        return {"success": False, "message": "新料架位置错误"}

    occupied = self._read_plc_variable(f"New_Rack_InPut[{position - 1}]")
    if not bool(occupied):
        return {"success": False, "message": f"新料架位置{position}没有孔板"}

    return self._run_robot_arm_action(
        target_position=RoboticArmTargetPosition.PLATE_LOADING_RACK,
        pick_place_code=position,
        arm_action=RoboticArmAction.PICK,
        success_message="从新料架抓取孔板完成",
    )
```

UI preset：

```json
{
  "method": "pick_well_plate_from_new_rack",
  "label": "从新料架取孔板",
  "description": "选择新料架 1-8 号位",
  "params": [
    {
      "name": "position",
      "label": "料架位置",
      "type": "integer",
      "min": 1,
      "max": 8,
      "default": 1
    }
  ]
}
```

runtime config：

```json
"param_variables": {
  "pick_well_plate_from_new_rack": [
    {
      "param": "position",
      "template": "New_Rack_InPut[{position_minus_1}]"
    }
  ]
}
```

CSV：

```csv
新料架_InPut[0],New_Rack_InPut[0],VARIABLE,BOOLEAN,Chinese,ns=4;s=UniLab|新料架_InPut[0]
```

## 11. 已有正式 `@device` 后的命令行测试

如果设备已经按 `add-device` 流程写成正式 `@device` 类，就不需要再把动作手写到 `tests/szlab/example/`。本地测试时应让 preset 从 registry/AST 读取设备和 action。

### 11.1 先确认设备类可导入

在仓库根目录执行：

```bash
PYTHONPATH=. python - <<'PY'
from unilabos.devices.workstation.szlab_mixer.stirrer import SzlabMixerStirrerDevice
from unilabos.devices.workstation.szlab_mixer.pump import SzlabMixerPumpDevice

print(SzlabMixerStirrerDevice.__name__)
print(SzlabMixerPumpDevice.__name__)
PY
```

这一步只验证 Python import，不会连接硬件。失败通常说明模块路径、依赖或类名不对。

### 11.2 用 registry 驱动的 UI preset 测试

preset 需要包含：

```json
{
  "actions_source": "registry",
  "target_device_ids": ["szlab_mixer_stirrer", "szlab_mixer_pump"],
  "runtime_config": "runtime_configs/szlab_mixer_runtime.json",
  "path_roots": [
    "tests/szlab",
    "unilabos/devices/workstation/szlab_mixer"
  ]
}
```

runtime config 需要把设备实例 id 映射到正式设备类：

```json
{
  "device_factory": {
    "devices": {
      "szlab_mixer_stirrer": "unilabos.devices.workstation.szlab_mixer.stirrer.SzlabMixerStirrerDevice",
      "szlab_mixer_pump": "unilabos.devices.workstation.szlab_mixer.pump.SzlabMixerPumpDevice"
    }
  }
}
```

启动 UI：

```bash
PYTHONPATH=. python -m scripts.run_workflow_local \
  --ui \
  --port 8014 \
  --preset szlab_mixer
```

这种方式适合第一次联调：左侧动作卡片来自 `@device` / `@action`，运行时实例来自 `runtime_config.device_factory.devices`，设备参数来自 preset 里的 `device_graph.nodes[].config`。

如果是 AI4C 正式 preset：

```bash
PYTHONPATH=. python -m scripts.run_workflow_local \
  --ui \
  --port 8014 \
  --preset ai4c
```

注意不要再用 `--preset example/ai4c_preset.json`，那个是教学示例 preset，动作列表是手写的。

### 11.3 不打开 UI，直接跑 workflow JSON

已有 workflow JSON 和设备图 JSON 时，可以直接从命令行执行：

```bash
PYTHONPATH=. python -m scripts.run_workflow_local \
  --workflow path/to/workflow.json \
  --graph path/to/device_graph.json \
  --runtime-config tests/szlab/runtime_configs/szlab_mixer_runtime.json \
  --url opc.tcp://jdht1471820.bohrium.tech:50001 \
  --timeout 300 \
  --log-file /tmp/szlab_mixer_run.log
```

直接执行模式的要求：

- `workflow.json` 的节点 `device_name` 必须能路由到 runtime config 中的设备实例 id，例如 `szlab_mixer_pump`。
- `device_graph.json` 的节点 `id` 要和 runtime config 的 `devices` key 一致。
- `device_graph.json` 的节点 `class` 要和 `@device(id=...)` 一致。
- OPC UA 设备默认不需要传 `--csv`；仅在需要覆盖 graph config 的节点表时再传。
- `--url` 会覆盖设备图里的 OPC UA 地址，便于同一套 graph 在不同环境测试。

AI4C 这类 `PLC + target device` 的旧 runtime 仍可用：

```bash
PYTHONPATH=. python -m scripts.run_workflow_local \
  --workflow tests/szlab/robot.json \
  --graph tests/szlab/AI4C.json \
  --runtime-config tests/szlab/runtime_configs/ai4c_runtime.json \
  --url opc.tcp://jdht1471820.bohrium.tech:50001 \
  --no-subscription \
  --timeout 60 \
  --log-file /tmp/ai4c_run.log
```

### 11.4 常用验证命令

只验证 szlab 本地测试逻辑：

```bash
PYTHONPATH=. pytest tests/szlab
```

只验证 registry-driven preset 是否能扫描到正式设备动作：

```bash
PYTHONPATH=. pytest tests/szlab/test_szlab_mixer_devices.py
```

前端代码更新后重新构建：

```bash
cd unilabos_local_ui
npm install
npm run build
```

## 12. 验证命令

后端测试：

```bash
PYTHONPATH=. pytest tests/szlab/test_workflow_ui.py
```

前端构建：

```bash
cd unilabos_local_ui
npm run build
```

动作类导入：

```bash
PYTHONPATH=. python - <<'PY'
from tests.szlab.example.ai4c_actions import ExampleAI4CActions

actions = ExampleAI4CActions()
assert hasattr(actions, "pick_well_plate_from_loading_rack")
assert hasattr(actions, "place_well_plate_to_pipetting_station")
print("example action class ok")
PY
```

## 13. 同步到正式配置

教学示例调试完成后执行：

1. 将 `tests/szlab/example/ai4c_actions.py` 中的动作方法同步到正式设备类。
2. 将 `tests/szlab/example/ai4c_preset.json` 中的 action 同步到 `tests/szlab/presets/ai4c.json`。
3. 将 `tests/szlab/example/ai4c_runtime.json` 中的 OPC 快照配置同步到 `tests/szlab/runtime_configs/ai4c_runtime.json`。
4. 将新增 OPC 节点同步到正式 CSV。
5. 启动正式 UI：

```bash
PYTHONPATH=. python -m scripts.run_workflow_local --ui --port 8014 --preset ai4c
```

## 14. 排查表

| 现象 | 检查项 |
| --- | --- |
| 左侧没有动作卡片 | `example/ai4c_preset.json` 的 `actions` 中存在对应 `method` |
| 校验流程提示不支持动作 | `actions[].method` 拼写正确 |
| 运行时报不存在动作方法 | `ExampleAI4CActions` 中存在同名方法 |
| 运行时报 PLC bridge 未初始化 | `ai4c_runtime.json` 的 `direct_plc_command_method` 为 `_call_plc_command` |
| OPC 表格没有新变量 | `ai4c_runtime.json` 的 `action_variables` 或 `param_variables` 包含变量 |
| OPC 读取失败 | `ai4c_sim_updated.csv` 中存在对应 `EnglishName` |
| 页面显示旧代码 | 重启 `scripts/run_workflow_local.py` UI 进程 |
