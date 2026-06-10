# szlab 本地调试教学示例

这个目录是一套自包含的 AI4C 本地调试 UI 示例。新增动作调试时，可以先复制或修改这里的文件，不需要先改 `tests/szlab/presets/` 或 `tests/szlab/runtime_configs/`。

## 文件说明

- `ai4c_preset.json`：UI 示例 preset，定义左侧动作卡片、默认运行配置和临时设备图。
- `ai4c_runtime.json`：本地运行配置，定义设备工厂、设备路由、OPC 快照变量。
- `ai4c_actions.py`：教学用本地动作类，示例动作可以先写在这里，不需要改生产设备类。
- `ai4c_sim_updated.csv`：示例 OPC UA 节点表，preset 默认会引用这个文件。
- `ai4c_graph.json`：命令行直跑用的最小设备图。
- `ai4c_workflow.json`：命令行直跑用的最小 workflow。

## 启动示例

在仓库根目录执行：

```bash
PYTHONPATH=. python -m scripts.run_workflow_local \
  --ui \
  --port 8014 \
  --preset example/ai4c_preset.json
```

如果只想启动 UI 服务但不自动打开浏览器：

```bash
PYTHONPATH=. python -m scripts.run_workflow_local \
  --ui \
  --no-browser \
  --port 8014 \
  --preset example/ai4c_preset.json
```

如果完全不启动 UI，直接在命令行执行示例 workflow：

```bash
PYTHONPATH=. python -m scripts.run_workflow_local \
  --runtime-config tests/szlab/example/ai4c_runtime.json \
  --graph tests/szlab/example/ai4c_graph.json \
  --workflow tests/szlab/example/ai4c_workflow.json \
  --url opc.tcp://127.0.0.1:49320 \
  --csv tests/szlab/example/ai4c_sim_updated.csv \
  --no-subscription \
  --timeout 60
```

页面打开后：

1. 点击 `运行配置`。
2. 填入 OPC UA URL。
3. 确认 CSV 是 `ai4c_sim_updated.csv`。
4. 从左侧添加动作节点，连线后点击 `运行`。

## 新增动作时改哪里

1. 在 `ai4c_actions.py` 的 `ExampleAI4CActions` 中实现动作。
2. 在 `ai4c_preset.json` 的 `actions` 中添加一个动作卡片。
3. 在 `ai4c_runtime.json` 的 `opc_snapshot` 中配置这个动作需要观察的 OPC 变量。

`ai4c_runtime.json` 中的 `target_class` 指向 `ExampleAI4CActions`：

```json
"target_class": "tests.szlab.example.ai4c_actions.ExampleAI4CActions"
```

如果以后要切回生产设备类，可以把它改成生产类路径。

### 无参数动作

`ai4c_preset.json`：

```json
{
  "method": "place_well_plate_to_new_station",
  "label": "放到新工位",
  "description": "机械臂将孔板放置到新工位",
  "params": []
}
```

`ai4c_runtime.json`：

```json
"action_variables": {
  "place_well_plate_to_new_station": ["New_Station_Occupied"]
}
```

### 带 position 参数动作

`ai4c_preset.json`：

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

`ai4c_runtime.json`：

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

`position=3` 时，`{position_minus_1}` 会自动渲染成 `2`。
