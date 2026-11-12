# 实验图高级参考

本文件是 SKILL.md 的补充，包含 ResourceDict 完整 schema、Handle 验证、Pose 标准化规则和复杂图文件结构（GraphML 已不再支持）。Agent 在需要处理这些场景时按需阅读。

---

## 1. ResourceDict 完整字段

`unilabos/resources/resource_tracker.py` 中定义的节点数据模型：

| 字段 | 类型 | 别名 | 说明 |
|------|------|------|------|
| `id` | `str` | — | 节点唯一标识 |
| `uuid` | `str` | — | 全局唯一标识 |
| `name` | `str` | — | 显示名称 |
| `description` | `str` | — | 描述（默认 `""` ） |
| `resource_schema` | `Dict[str, Any]` | `schema` | 资源 schema |
| `model` | `Dict[str, Any]` | — | 3D 模型信息 |
| `icon` | `str` | — | 图标（默认 `""` ） |
| `parent_uuid` | `Optional[str]` | — | 父节点 UUID |
| `parent` | `Optional[ResourceDict]` | — | 父节点引用（序列化时 exclude） |
| `type` | `Union[Literal["device"], str]` | — | 节点类型 |
| `klass` | `str` | `class` | 注册表类名 |
| `pose` | `ResourceDictPosition` | — | 位姿信息 |
| `config` | `Dict[str, Any]` | — | 配置参数 |
| `data` | `Dict[str, Any]` | — | 运行时数据 |
| `extra` | `Dict[str, Any]` | — | 扩展数据 |

### Pose 完整结构（ResourceDictPosition）

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `size` | `{width, height, depth}` | `{0,0,0}` | 节点尺寸 |
| `scale` | `{x, y, z}` | `{1,1,1}` | 缩放比例 |
| `layout` | `"2d"/"x-y"/"z-y"/"x-z"` | `"x-y"` | 布局方向 |
| `position` | `{x, y, z}` | `{0,0,0}` | 2D 位置 |
| `position3d` | `{x, y, z}` | `{0,0,0}` | 3D 位置 |
| `rotation` | `{x, y, z}` | `{0,0,0}` | 旋转角度 |
| `cross_section_type` | `"rectangle"/"circle"/"rounded_rectangle"` | `"rectangle"` | 横截面形状 |

---

## 2. Position / Pose 标准化规则

图文件中的 `position` 有多种写法，加载时自动标准化。

### 输入格式兼容

```json
// 格式 A: 直接 {x, y, z}（最常用）
"position": {"x": 100, "y": 200, "z": 0}

// 格式 B: 嵌套 position
"position": {"position": {"x": 100, "y": 200, "z": 0}}

// 格式 C: 使用 pose 字段
"pose": {"position": {"x": 100, "y": 200, "z": 0}}

// 格式 D: 顶层 x, y, z（无 position 字段）
"x": 100, "y": 200, "z": 0
```

### 标准化流程

1. **legacy 适配层**（`server/backend/legacy_adaptor/legacy/graph.py`）：`-g` / `unilab graph upload` 读取边界把根级 `position` 折叠进 `pose.position`
2. **Graph Authority `load_graph_payload`**（`server/services/materials/graph.py`）：从节点顶层提取 `x/y/z` 填入 `pose.position`；仍带根级 `position` 的节点拒绝
3. **resource_tracker.py `get_resource_instance_from_dict`**：`pose.size` 从 `config.size_x/size_y/size_z` 自动填充

---

## 3. Handle 验证

启动时系统验证 link 中的 `sourceHandle` / `targetHandle` 是否在注册表的 `handles` 中定义。

```python
# unilabos/app/main.py (约 449-481 行)
source_handler_keys = [
    h["handler_key"] for h in materials[source_node.klass]["handles"]
    if h["io_type"] == "source"
]
target_handler_keys = [
    h["handler_key"] for h in materials[target_node.klass]["handles"]
    if h["io_type"] == "target"
]
if source_handle not in source_handler_keys:
    print_status(f"节点 {source_node.id} 的source端点 {source_handle} 不存在", "error")
    resource_edge_info.pop(...)  # 移除非法 link
```

**Handle 定义在注册表 YAML 中：**

```yaml
my_device:
  handles:
  - handler_key: access
    io_type: target
    data_type: fluid
    side: NORTH
    label: access
```

> 大多数简单设备不定义 handles，此验证仅对有 `sourceHandle`/`targetHandle` 的 link 生效。

---

## 4. GraphML 不再支持

设备图只接受 node-link JSON。`unilab -g foo.graphml` 会直接拒绝启动，并提示把图转换为
node-link JSON：节点 id 用真实设备名，GraphML 里 `父::子` 的层级改写成子节点的
`parent` 字段，顶层 `x/y/z` 可以保留（加载时折叠进 `pose.position`）。

---

## 5. 复杂图文件结构示例

### 外部系统工作站完整 config

以 Bioyond 反应站图文件（随 Bioyond 驱动包发布，原 `reaction_station_bioyond.json`）为例，工作站 `config` 中的关键字段：

```json
{
    "config": {
        "api_key": "DE9BDDA0",
        "api_host": "http://172.21.103.36:45388",

        "workflow_mappings": {
            "scheduler_start": {"workflow": "start", "params": {}},
            "create_order": {"workflow": "create_order", "params": {}}
        },

        "material_type_mappings": {
            "BIOYOND_PolymerStation_Reactor": ["反应器", "type-uuid-here"],
            "BIOYOND_PolymerStation_1BottleCarrier": ["试剂瓶", "type-uuid-here"]
        },

        "warehouse_mapping": {
            "堆栈1左": {
                "uuid": "warehouse-uuid-here",
                "site_uuids": {
                    "A01": "site-uuid-1",
                    "A02": "site-uuid-2"
                }
            }
        },

        "http_service_config": {
            "enabled": true,
            "host": "0.0.0.0",
            "port": 45399,
            "routes": ["/callback/workflow", "/callback/material"]
        },

        "deck": {
            "data": {
                "_resource_child_name": "Bioyond_Deck",
                "_resource_type": "unilabos.resources.bioyond.decks:BIOYOND_PolymerReactionStation_Deck"
            }
        },

        "size_x": 2700.0,
        "size_y": 1080.0,
        "size_z": 2500.0,
        "protocol_type": [],
        "data": {}
    }
}
```

### 子设备 Reactor 节点

```json
{
    "id": "reactor_1",
    "name": "reactor_1",
    "parent": "reaction_station_bioyond",
    "type": "device",
    "class": "bioyond_reactor",
    "position": {"x": 1150, "y": 300, "z": 0},
    "config": {
        "reactor_index": 0,
        "bioyond_workflow_key": "reactor_1"
    },
    "data": {}
}
```

### Deck 节点

```json
{
    "id": "Bioyond_Deck",
    "name": "Bioyond_Deck",
    "parent": "reaction_station_bioyond",
    "type": "deck",
    "class": "BIOYOND_PolymerReactionStation_Deck",
    "position": {"x": 0, "y": 0, "z": 0},
    "config": {
        "type": "BIOYOND_PolymerReactionStation_Deck",
        "setup": true,
        "rotation": {"x": 0, "y": 0, "z": 0, "type": "Rotation"}
    },
    "data": {}
}
```

---

## 6. Link 端口标准化

`server/services/materials/graph.py::_canonicalize_link_ports`（由 `load_graph_payload` 调用）处理 `port` 字段的多种格式：

```python
# 输入: 字符串格式 "(A,B)"
"port": "(pump_1, valve_1)"
# 输出: 字典格式
"port": {"source_id": "pump_1", "target_id": "valve_1"}

# 输入: 已是字典
"port": {"pump_1": "port", "serial_1": "port"}
# 保持不变

# 输入: 无 port 字段
# 自动补充空 port
```

---

## 7. 关键路径

| 内容 | 路径 |
|------|------|
| ResourceDict 模型 | `unilabos/resources/resource_tracker.py` |
| 旧格式图升级 | `unilabos/server/backend/legacy_adaptor/legacy/graph.py` |
| 图登记 + 运行时装配 | `unilabos/server/services/materials/graph.py` (`GraphService.upsert_graph`, `load_graph_payload`) |
| `-g` 启动入口 | `unilabos/app/main.py` (`_load_startup_graph`) |
| Handle 验证 | `unilabos/app/main.py` (449-481 行) |
| 协议工作站图文件 | `unilabos/test/experiments/Protocol_Test_Station/*.json` |
| 虚拟反应器图文件 | `unilabos/test/experiments/mock_reactor.json` |
| 用户文档 | `docs/user_guide/graph_files.md` |
