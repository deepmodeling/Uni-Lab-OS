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
      "name": "79a4cbaa-f053-4f0f-b186-9dca9f5fbd58",
      "display_name": "主工作站",
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
      "name": "f2d9082c-c4d9-46d9-bb92-4dc8614158c0",
      "display_name": "反应器",
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
| `id` | 图内唯一、人可读的稳定标识（编译器、调度与设备寻址都用它）。缺省时取 PLR 键 / `display_name` |
| `uuid` | 微后端分配的资源 UUID；严格导入时必须提供 |
| `name` | 物料名，恒等于 `uuid`（见下文「物料命名」）。可以省略 |
| `display_name` | 面向界面的人读标签，可重名、可编辑 |
| `type` | 节点类型；图导入缺省值为 `device` |
| `class` | 设备填 Registry 设备名；物料填 PLR 类名（如 `RegularContainer`），须与模板的类名一致 |
| `parent` | 父节点的 `id` |
| `parent_uuid` | 父节点 UUID；同时提供时优先按 UUID 建立关系 |
| `config` | 驱动构造参数和设备配置；非设备物料的 `config.name` 是 PLR 键 |
| `data` | 运行时状态数据 |
| `extra` | Uni-Lab-OS 通信与转换元数据 |
| `pose` | 相对父资源的位置、几何尺寸和可视化布局 |
| `schema` | 资源 schema |
| `model` | 资源模型信息 |
| `icon` | 资源图标 |
| `meta_data` | 规范化业务元数据 |
| `template_name` | 资源模板名称（Registry 资源名，如 `container`）；设备缺省取 `class` |
| `resource_template_uuid` | 微后端资源模板 UUID |
| `sites` | 载架 Site 实例 |

`-g` 本地文件中没有 `uuid` 的草稿节点由 Graph Authority 登记时发号（按图 uuid +
节点 `id` 稳定派生，重复导入结果一致）；已有 `uuid` 的节点沿用该身份。传输输入也可
通过 `data.unilabos_uuid` 携带同一 UUID；若两者同时存在，则值必须一致。装配运行时
结构时每个节点都必须带 UUID，不会随机生成。

### 物料命名

物料层的 `name` 恒等于 `uuid`（设备节点同样如此），人读标签放 `display_name`。
PyLabRobot 树里用的名字（PLR 键）单独保存在 `config.name`：PLR 名在整棵 PLR 树内唯一、
创建后不可改，驱动和 PLR 自身按它查找（如 `_resource_child_name` 引用的就是 PLR 键）。
设备不是 PLR 资源，`config` 是驱动构造参数，不写 PLR 键。`id` 保持人可读，不从
uuid 派生。

旧图里 `name` 是标签：登记时若 `name` 已是 UUID 形式（大写、花括号、无连字符也算），
就作为该节点的身份复用（节点另带的 `uuid` 与它不一致时拒绝，报 `identity_conflict`）；
否则把它迁到 `display_name`（为空时）和 `config.name`（缺失时），再写成 uuid。导出的
新图里 `name` 就是 uuid，重新导入是幂等的。

Opentrons 板 / 管架里「名字就是 ordering 标识」的孔和管（裸 `"A1"`）用 uuid 作 PLR 键，
父节点 `config.ordering` 同步改成 `{"A1": <uuid>}`；`plate["A1"]`、`get_item("A1")` 照常
可用，同一台面上两块这样的板不再撞名。驱动若用 `get_resource("A1")` 按名找孔需要改用
ordering 标识。

### 物料节点的类与模板

物料节点（`type` 不是 `device`）要同时写清三处：`template_name` 是 Registry 资源名，`class` 与
`config.type` 是该模板实例化出的 PLR 类名。开机时 Host 把图中物料对齐到物料权威，权威按模板校验
`class`，PLR 按 `config.type` 反序列化；`config` 里只能放 PLR 构造参数，其他业务字段放 `data`。
最常见的通用容器写法（与 `/api/v1/graphs/live/payload` 导出的形状一致）：

```json
{
  "id": "flask_DMF",
  "type": "container",
  "class": "RegularContainer",
  "template_name": "container",
  "parent": "workstation_1",
  "config": {"type": "RegularContainer", "max_volume": 1000.0},
  "data": {}
}
```

旧图里容器常写成 `"class": null` 或 `"class": "container"`、没有 `config.type`，`config` 里还可能混着
`filter_type` 这类业务字段。这样的图不用手改：`-g` 与 `unilab graph upload` 读图时按注册表补齐
（见下文「规范化与校验」第 1 步），文件本身不动。不经读图边界、直接交给 Graph Authority 的
payload 不做这一步，权威会按模板拒绝（`class_name ... does not match template 'container'
class_name 'RegularContainer'`）。

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
   `edges` 键、`name` 不是 uuid 等）在读取边界由 `server/backend/legacy_adaptor/legacy/graph.py`
   一次性转换为当前契约，并打印 WARNING。旧液体字段一并转成内容物：dev 时代容器的
   `data.liquid=[{"liquid_type": ..., "liquid_volume": ...}]` 在节点还没有
   `liquids` / `substances` 时转为 `substances`（单位 `ul`，与 `max_volume` 同单位），
   `data.liquid` 原样保留给协议编译器读取。

   缺 PLR 类的旧物料节点（`type` 不是 `device`，且没有 `config.type`；或有 `config.type`、没有
   `template_name`、`class` 却写成模板名）按注册表补齐成上文「物料节点的类与模板」的形状：

   - 模板依次取 `template_name`、`class`、`type`（`class` 为空的 dev 时代容器写
     `"type": "container"`，即同名注册表模板），写回 `template_name`；
   - PLR 类名只从注册表查：该模板注册表条目的 `config_info[0].config.type`（物料权威登记模板时
     用的同一规则；条目没有 `config_info` 时按条目的 `class.module` 实例化一次取类名），写进
     `class`，`config.type` 缺失时一并补上；
   - 补上 `config.type` 的节点，`config` 里该 PLR 类构造器不接受的键（如 `filter_type`）挪进
     `data`；
   - 模板不在注册表、条目不是 PLR 资源类、或节点已有的 `config.type` 与注册表的类不一致时报错，
     消息写明节点 `id` 与模板，不按名字猜类名。

   `-g` 的读图发生在 Host / Slave 进程，查本进程扫描的注册表；`unilab graph upload` 在 CLI
   进程读图，经 `GET /api/v1/registry/status` 与 `GET /api/v1/registry/entries/{模板}` 查上传目标
   调度权威的注册表（Host 上报的目录；权威还没收到过上报时直接报错）。已是当前写法的节点原样
   不动，升级结果再读一次不再变化。
2. Graph Authority（`server/services/materials/graph.py`）登记该图：为草稿节点和
   Site 发号、把 `name` 写成 uuid（按上文规则迁移旧名）、实例化设备模板 Site、拒绝身份冲突；权威已有同名图时以权威为准，只补
   文件新增的节点和连线。
3. `load_graph_payload()` 把权威 payload 装配成运行时结构：
   - 校验节点 UUID；为缺失的 `type`（`device`）、`name`（取 uuid）、`config` 等填充默认值；
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
需要创建新实例时，应先通过微后端创建或严格导入流程分配 UUID。使用内置虚拟设备的示例图
（协议测试站、`mock_devices/`、`mock_protocol/`、`mock_reactor.json`、
`virtual_heating_platform_demo.json` 等）都能在默认的 HostLink backend 下直接启动并
真实执行（不需要 `--test_mode`，默认的模拟时间倍率下秒级完成，见 {doc}`virtual_devices`）；
`tests/devices/test_virtual_devices_hostlink.py` 逐张检查这些图的物料能按模板建出来，并在图里的
设备上执行各协议测试站的代表协议。`comprehensive_protocol/comprehensive_slim.json` 例外：它的根节点模板
`workstation.example` 不在 Registry 中，台面数据也是旧版 PLR 的序列化，仅作历史参考；`-g` 读它时
在读图边界就拒绝启动，错误写明是哪个节点、哪个模板（枪头盒的 `config.type` 与注册表模板的类不一致）。

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
