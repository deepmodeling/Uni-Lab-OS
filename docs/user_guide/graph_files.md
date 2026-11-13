# 设备图文件

设备图描述实验室中的设备、资源层级和连接关系。节点使用
`unilabos.resources.objects.resource.ResourceDict` 作为规范快照模型，
`unilabos.resources.resource_tracker.ResourceDictInstance` 提供运行时树行为。

## 支持的文件格式

设备图只支持 node-link JSON。JSON 由 Python 标准库严格解析，因此不能包含注释或
尾随逗号。GraphML 已不再支持：`unilab -g foo.graphml` 会直接拒绝启动，请先把图转换
为下文的 node-link JSON（顶层 `nodes` + `links`，层级用 `parent` / `parent_uuid`
表达）。

启动时通过 `-g` 指定设备图文件，或已登记到本机 Graph Authority 的图名 / uuid：

```bash
unilab -g path/to/device_graph.json
unilab -g device_graph          # 复用已登记的图
```

## JSON 结构

JSON 顶层包含 `nodes` 和可选的 `links`：

```json
{
  "nodes": [
    {
      "id": "workstation_1",
      "uuid": "79a4cbaa-f053-4f0f-b186-9dca9f5fbd58",
      "name": "主工作站",
      "type": "device",
      "class": "workstation",
      "config": {},
      "data": {},
      "extra": {},
      "pose": {
        "position": {"x": 0, "y": 0, "z": 0}
      }
    },
    {
      "id": "reactor_1",
      "uuid": "f2d9082c-c4d9-46d9-bb92-4dc8614158c0",
      "name": "反应器",
      "type": "device",
      "class": "reactor",
      "parent": "workstation_1",
      "parent_uuid": "79a4cbaa-f053-4f0f-b186-9dca9f5fbd58",
      "config": {},
      "data": {},
      "extra": {},
      "pose": {
        "position": {"x": 120, "y": 0, "z": 0}
      }
    }
  ],
  "links": []
}
```

### 节点字段

| 字段 | 说明 |
| --- | --- |
| `id` | 图内唯一的稳定标识。缺省时使用 `name` |
| `uuid` | 微后端分配的资源 UUID；严格导入时必须提供 |
| `name` | 资源名称。缺省时使用 `id` |
| `display_name` | 面向界面的显示名称 |
| `type` | 节点类型；图导入缺省值为 `device` |
| `class` | Registry 中的设备或资源类名 |
| `parent` | 父节点的 `id` |
| `parent_uuid` | 父节点 UUID；同时提供时优先按 UUID 建立关系 |
| `config` | 驱动构造参数和设备配置 |
| `data` | 运行时状态数据 |
| `extra` | Uni-Lab-OS 通信与转换元数据 |
| `pose` | 相对父资源的位置、几何尺寸和可视化布局 |
| `schema` | 资源 schema |
| `model` | 资源模型信息 |
| `icon` | 资源图标 |
| `meta_data` | 规范化业务元数据 |
| `template_name` | 资源模板名称 |
| `resource_template_uuid` | 微后端资源模板 UUID |
| `sites` | 载架 Site 实例 |

`-g` 本地文件中没有 `uuid` 的草稿节点由 Graph Authority 登记时发号（按图 uuid +
节点 `id` 稳定派生，重复导入结果一致）；已有 `uuid` 的节点沿用该身份。传输输入也可
通过 `data.unilabos_uuid` 携带同一 UUID；若两者同时存在，则值必须一致。装配运行时
结构时每个节点都必须带 UUID，不会随机生成。

`children` 是资源树的递归容器，不属于 `ResourceDict` 领域字段。平铺 JSON 图应在
子节点上使用 `parent_uuid`，必要时同时提供便于阅读的 `parent`。

### host_node

host node 是运行时内置的服务设备，按 `class: host_node` 判别，且全图只能有
一个；动作定义始终来自内置 Registry 条目。设备图可以省略它：Host 启动时会
自动创建（ROS2 经通用设备管线初始化 `HostServices`，HostLink 在本地运行时
注册同一服务）。

设备图也可以显式声明 host node 节点（`class` 填 `host_node`），并且与其他
节点一样必须携带 `uuid`。声明后运行时复用该身份而不重复创建，图导出也会
保留该节点。实例 `id` 支持通过 `--host_node_id` 重命名（如
`host_node_8523`）；图中 `id` 与配置不一致时以配置为准。

### Pose

根级 `position` 字段无效；资源相对父节点的位置写入 `pose.position`：

```json
{
  "pose": {
    "size": {"width": 127.76, "height": 85.48, "depth": 10},
    "scale": {"x": 1, "y": 1, "z": 1},
    "layout": "x-y",
    "position": {"x": 100, "y": 200, "z": 0},
    "position3d": {"x": 100, "y": 200, "z": 0},
    "rotation": {"x": 0, "y": 0, "z": 0},
    "cross_section_type": "rectangle"
  }
}
```

嵌套的向量必须完整包含 `x`、`y`、`z`。可用布局为 `2d`、`x-y`、`z-y`
和 `x-z`；截面类型可用 `rectangle`、`circle` 和 `rounded_rectangle`。

节点顶层的 `x`、`y`、`z` 会被规范化到 `pose.position`。若这些坐标与已有
`pose.position` 冲突，导入会失败。

### Links

连接使用节点 `id` 或 UUID 标识端点：

```json
{
  "source": "pump_1",
  "target": "reactor_1",
  "type": "fluid",
  "port": {
    "pump_1": "out",
    "reactor_1": "in"
  }
}
```

加载器会补充 `source_uuid` 和 `target_uuid`。`communication` 连接用于串口、
I/O 和 PLC 等通信适配器；物理流路使用 `fluid`。

## 规范化与校验

`-g <文件>.json` 的读取链路：

1. 旧格式图（`class` 无 `template_name`、根级 `position`、`children` 列表、
   `edges` 键等）在读取边界由 `server/backend/legacy_adaptor/legacy/graph.py`
   一次性转换为当前契约，并打印 WARNING。旧液体字段一并转成内容物：dev 时代容器的
   `data.liquid=[{"liquid_type": ..., "liquid_volume": ...}]` 在节点还没有
   `liquids` / `substances` 时转为 `substances`（单位 `ul`，与 `max_volume` 同单位），
   `data.liquid` 原样保留给协议编译器读取。
2. Graph Authority（`server/services/materials/graph.py`）登记该图：为草稿节点和
   Site 发号、实例化设备模板 Site、拒绝身份冲突；权威已有同名图时以权威为准，只补
   文件新增的节点和连线。
3. `load_graph_payload()` 把权威 payload 装配成运行时结构：
   - 校验节点 UUID；为缺失的 `type`（`device`）、`name`、`config` 等填充默认值；
     顶层 `x/y/z` 折叠进 `pose.position`，根级 `position` 拒绝；
   - 按 `parent_uuid` 或 `parent` 建立 `ResourceTreeSet`，并校验 UUID 唯一性；
   - 补齐连接端点的 id / UUID，把 `port` 规范成 `{端点 id: 端口}`；
   - 生成协议编译器使用的 NetworkX 物理拓扑图，并按 `communication` 连接把串口 /
     I/O 端口写进设备 `config`；
   - 为设备树补充本机 `machine_name`，连接转换为 `sourceHandle` / `targetHandle`。

节点中的未知根字段会移入 `config`，但图文件应直接把驱动参数写在 `config`
中，以便模型审查和前端编辑。

## Python 加载接口

```python
import json

from unilabos.server.services.materials.graph import load_graph_payload

with open("device_graph.json", encoding="utf-8") as stream:
    payload = json.load(stream)
loaded = load_graph_payload(payload, machine_name="lab-pc")
loaded.resource_tree_set     # ResourceTreeSet
loaded.links                 # backend 形态的连接列表
loaded.physical_setup_graph  # NetworkX 物理拓扑图
```

`load_graph_payload` 只接受当前契约且每个节点带 UUID 的 payload（即 Graph
Authority 存储的形态）；旧格式图请先经 `unilab graph upload` 或 `-g` 登记。

## 示例与排查

设备图示例位于 `unilabos/test/experiments/`。修改示例时应保留已有 UUID；
需要创建新实例时，应先通过微后端创建或严格导入流程分配 UUID。

可使用完整 Registry 检查设备类和动作定义：

```bash
unilab --check_mode --complete_registry --skip_env_check -g path/to/device_graph.json
```

导入失败时优先检查：

- JSON 是否为严格语法；
- 每个节点是否具有唯一 UUID；
- `parent_uuid` 是否指向同一图中的节点；
- `pose` 的嵌套向量是否完整；
- `class` 是否存在于当前 Registry；
- 驱动构造参数是否位于 `config`。

源码参考：

- `unilabos/resources/objects/resource.py`
- `unilabos/resources/objects/pose.py`
- `unilabos/resources/resource_tracker.py`
- `unilabos/server/services/materials/graph.py`
- `unilabos/server/backend/legacy_adaptor/legacy/graph.py`
