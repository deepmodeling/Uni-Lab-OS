---
name: edit-experiment-graph
description: Guide for creating and editing experiment graph files in Uni-Lab-OS (创建/编辑实验组态图). Covers node types, link types, parent-child relationships, deck configuration, and common graph patterns. Use when the user wants to create a graph file, edit an experiment configuration, or mentions 图文件/graph/组态/拓扑/实验图/experiment JSON.
---

# 创建/编辑实验组态图

实验图（Graph File）定义设备拓扑、物理连接和物料配置。系统启动时加载图文件，初始化所有设备和连接关系。

路径：`unilabos/test/experiments/<name>.json`

> 图文件中的 `class` 字段对应设备/资源的注册表 ID。使用 `@device(id=...)` 或 `@resource(id=...)` 装饰器注册的设备/资源，`class` 填写装饰器中指定的 `id`。

---

## JSON 顶层结构

```json
{
    "nodes": [],
    "links": []
}
```

---

## 节点定义

### 字段说明

| 字段 | 类型 | 必需 | 说明 |
|------|------|------|------|
| `id` | string | **是** | 节点唯一标识 |
| `template_name` | string | **是** | `@device(id=...)` 中的 id，或 YAML 注册表 key（旧图写 `class`，读取时自动回填） |
| `type` | string | 否 | `"device"` / `"deck"` / `"container"` |
| `name` | string | 否 | 显示名称 |
| `parent` | string\|null | 否 | 父节点 ID；层级关系只由它表达，不写 `children` |
| `config` | object | 否 | 按 `__init__` 参数名平铺传入；构造函数声明了 `config` 参数时它收到整份配置，其他具名参数仍按名取值 |
| `data` | object | 否 | 初始运行状态 |

> 子节点顺序即其在 `nodes` 数组中的出现顺序；旧图里的 `children` 列表读取时会被忽略并在入库时剥离。

### 设备节点

```json
{
    "id": "my_device",
    "name": "我的设备",
    "type": "device",
    "template_name": "my_device_id",
    "config": {"port": "/dev/ttyUSB0", "baudrate": 115200},
    "data": {"status": "Idle"}
}
```

### 容器节点

```json
{
    "id": "flask_DMF",
    "name": "DMF试剂瓶",
    "type": "container",
    "parent": "my_station",
    "config": {"max_volume": 1000.0},
    "data": {"liquid": [{"liquid_type": "DMF", "liquid_volume": 800.0}]}
}
```

---

## 连接定义

| 字段 | 类型 | 说明 |
|------|------|------|
| `source` | string | 源节点 ID |
| `target` | string | 目标节点 ID |
| `type` | string | `"physical"` / `"fluid"` / `"communication"` |
| `port` | object | 端口映射 |

---

## 常见图模式

### 模式 A：单设备调试

```json
{
    "nodes": [
        {"id": "my_device", "template_name": "my_device_id", "type": "device",
         "config": {"port": "/dev/ttyUSB0"}}
    ],
    "links": []
}
```

### 模式 B：Protocol 工作站

```json
{
    "nodes": [
        {"id": "station", "template_name": "workstation", "type": "device",
         "config": {"protocol_type": ["PumpTransferProtocol"]}},
        {"id": "pump", "template_name": "virtual_transfer_pump", "parent": "station"},
        {"id": "valve", "template_name": "virtual_multiway_valve", "parent": "station"},
        {"id": "flask", "type": "container", "parent": "station"}
    ],
    "links": [
        {"source": "pump", "target": "valve", "type": "fluid",
         "port": {"pump": "transferpump", "valve": "transferpump"}}
    ]
}
```

### 模式 C：工作站 + Deck

```json
{
    "nodes": [
        {"id": "my_station", "template_name": "my_workstation",
         "deck": {"data": {"_resource_child_name": "my_deck",
                           "_resource_type": "unilabos.resources.module:MyDeck"}}},
        {"id": "my_deck", "template_name": "MyDeck", "parent": "my_station",
         "type": "deck", "config": {"type": "MyDeck", "setup": true}}
    ]
}
```

---

## 父子关系规则

- 层级只由子节点的 `parent`（或 `parent_uuid`）表达，父节点不写 `children`
- 子设备的 `parent` 必须指向工作站节点的 `id`
- 需要固定子设备初始化顺序时，按顺序排列 `nodes` 数组（如通信设备放在其他子设备之前）
- Deck 节点的 `_resource_child_name` 必须与 Deck 节点 `id` 一致

---

## 验证

```bash
unilab -g unilabos/test/experiments/<name>.json
```

---

## 常见错误

| 错误 | 修复 |
|------|------|
| `template_name` 找不到 | 确认 `@device(id=...)` / `@resource(id=...)` 中的 id 或 YAML key |
| 子节点没挂到父节点下 | 检查子节点 `parent` 是否等于父节点 `id`（`children` 不会被读取） |
| `_resource_child_name` 不匹配 | 必须与 Deck 节点 `id` 一致 |

详见 [reference.md](reference.md)：ResourceDict schema、Pose 标准化、Handle 验证（图文件只支持 node-link JSON，GraphML 已不再支持）。
