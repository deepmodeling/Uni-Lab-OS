# 实例：物料教程（Resource）

> **文档类型**：物料系统完整教程  
> **适用场景**：物料格式转换、多系统物料对接、资源结构理解  
> **前置知识**：Python 基础 | JSON 数据结构
> **驱动位置**：本文涉及的厂商驱动与物料预设（Bioyond 工作站及其测试数据）已迁出主仓库，放在外部驱动包中；文中的 `unilabos.devices.*`、`unilabos.resources.presets.{bioyond,battery}` 路径请换成对应驱动包里的模块路径。框架基类 `unilabos.devices.workstation.workstation_base` 仍在主仓库。

本教程面向 Uni-Lab-OS 的开发者，讲解"物料"的核心概念、3种物料格式（Uni-Lab-OS、PyLabRobot、奔耀Bioyond）及其相互转换方法，并说明4种 children 结构表现形式及使用场景。

---

## 1. 物料是什么

- **物料（Resource）**：指实验工作站中的实体对象，包括设备（device）、操作甲板 （deck）、试剂、实验耗材，也包括设备上承载的具体物料或者包含的容器（如container/plate/well/瓶/孔/片等）。
- **物料基本信息**（以 Uni-Lab-OS list格式为例）：

```jsonc
{
    "id": "plate", // 某一类物料的唯一名称
    "name": "50ml瓶装试剂托盘", // 在云端显示的名称
    "sample_id": null, // 同类物料的不同样品
    "children": [
        "50ml试剂瓶"   // 表示托盘上有一个 50ml 试剂瓶
    ],
    "parent": "deck", // 此物料放置在 deck 上
    "type": "plate", // 物料类型
    "class": "plate", // 物料对应的注册/类名
    "position": {
        "x": 0, // 初始放置位置
        "y": 0,
        "z": 0
    },
    "config": { // 固有配置（尺寸、旋转等）
        "size_x": 400.0,
        "size_y": 400.0,
        "size_z": 400.0,
        "rotation": {
            "x": 0,
            "y": 0,
            "z": 0,
            "type": "Rotation"
        }
    },
    "data": { 
        "bottle_number": 1 // 动态数据（可变化）
    }
}
```

## 2. 3种物料格式概览(Uni-Lab-OS、PyLabRobot、奔耀Bioyond)

### 2.1 Uni-Lab-OS 物料格式（云端/项目内通用）

- 结构特征：顶层通常是 `nodes` 列表；每个节点是扁平字典，`children` 是子节点 `id` 列表；`parent` 为父节点 `id` 或 `null`。
- 用途：
  - 云端数据存储、前端可视化、与图结构算法互操作
  - 在上传/下载/部署配置时作为标准交换格式

示例片段（Uni-Lab-OS 物料格式）：

```jsonc
{
  "nodes": [
  
    {
      "id": "a",
      "name": "name_a",
      "sample_id": 1, 
      "type": "deck",
      "class": "deck",
      "parent": null,
      "children": ["b1"],
      "position": {"x": 0, "y": 0, "z": 0},
      "config": {},
      "data": {}
    },
    {
      
      "id": "b1",
      "name": "name_b1",
      "sample_id": 1, 
      "type": "plate",
      "class": "plate",
      "parent": "a1",
      "children": [],
      "position": {"x": 0, "y": 0, "z": 0},
      "config": {},
      "data": {}
    }
  ]
}
```

### 2.2 PyLabRobot（PLR）物料格式（实验流程运行时）

- 结构特征：严格的层级树，`children` 为“子资源字典列表”（每个子节点本身是完整对象）。
- 用途：
  - 实验流程执行与调度，PLR 运行时期望的资源对象格式
  - 通过 `Resource.deserialize/serialize`、`load_all_state/serialize_all_state` 与对象交互

示例片段（PRL 物料格式）：：

```json
{
  "name": "deck",
  "type": "Deck",
  "category": "deck",
  "location": {"x": 0, "y": 0, "z": 0, "type": "Coordinate"},
  "rotation": {"x": 0, "y": 0, "z": 0, "type": "Rotation"},
  "parent_name": null,
  "children": [
    {
      "name": "plate_1",
      "type": "Plate",
      "category": "plate_96",
      "location": {"x": 100, "y": 0, "z": 0, "type": "Coordinate"},
      "rotation": {"x": 0, "y": 0, "z": 0, "type": "Rotation"},
      "parent_name": "deck",
      "children": [
        {
          "name": "A1",
          "type": "Well",
          "category": "well",
          "location": {"x": 0, "y": 0, "z": 0, "type": "Coordinate"},
          "rotation": {"x": 0, "y": 0, "z": 0, "type": "Rotation"},
          "parent_name": "plate_1",
          "children": []
        }
      ]
    }
  ]
}
```


### 2.3 奔耀 Bioyond 物料格式（第三方来源）
一般是厂商自己定义的json格式和字段，信息需要提取和对应。以下为示例说明。

- 结构特征：顶层 `data` 列表，每项包含 `typeName`、`code`、`barCode`、`name`、`quantity`、`unit`、`locations`（仓位 `whName`、`x/y/z`）、`detail`（细粒度内容，如瓶内液体或孔位物料）。
- 用途：
  - 第三方 WMS/设备的物料清单输入
  - 需要自定义映射表将 `typeName` → PLR 类名，对 `locations`/`detail` 进行落位/赋值

示例片段（奔耀Bioyond 物料格式）：


```json
{
  "data": [
    {
      "id": "3a1b5c10-d4f3-01ac-1e64-5b4be2add4b1",
      "typeName": "液",
      "code": "0006-00014",
      "barCode": "",
      "name": "EMC",
      "quantity": 50,
      "lockQuantity": 2.057,
      "unit": "瓶",
      "status": 1,
      "isUse": false,
      "locations": [
        {
          "id": "3a19da43-57b5-5e75-552f-8dbd0ad1075f",
          "whid": "3a19da43-57b4-a2a8-3f52-91dbbeb836db",
          "whName": "配液站内试剂仓库",
          "code": "0003-0003",
          "x": 1,
          "y": 3,
          "z": 1,
          "quantity": 0
        }
      ],
      "detail": [
        {
          "code": "0006-00014-01",
          "name": "EMC-瓶-1",
          "x": 1,
          "y": 3,
          "z": 1,
          "quantity": 500.0
        }
      ]
    }
  ],
  "code": 1,
  "message": "",
  "timestamp": 0
}
```
### 2.4 3种物料格式关键字段对应(Uni-Lab-OS、PyLabRobot、奔耀Bioyond)

| 含义 | Uni-Lab-OS | PyLabRobot (PLR) | 奔耀 Bioyond |
| - | - | - | - |
| 节点唯一名 | `id`  | `name` | `name` |
| 父节点引用 | `parent` | `parent_name` | `locations` 坐标（无直接父名，需映射坐标下的物料） |
| 子节点集合 | `children`（id 列表或对象列表，视结构而定） | `children`（对象列表） | `detail`（明细，非严格树结构，需要自定义映射） |
| 类型（抽象类别） | `type`（device/container/plate/deck/…） | `category`（plate/well/…），以及类名 `type` | `typeName`（厂商自定义，如“液”、“加样头(大)”） |
| 运行/业务数据 | `data` | 通过 `serialize_all_state()`/`load_all_state()` 管理的状态 | `quantity`、`lockQuantity` 等业务数值 |
| 固有配置 | `config`（size_x/size_y/size_z/model/ordering…） | 资源字典中的同名键（反序列化时按构造签名取用） | 厂商自定义字段（需映射入 PLR/Uni-Lab-OS 的 `config` 或 `data`） |
| 空间位置 | `position`（x/y/z） | `location`（Coordinate） + `rotation`（Rotation） | `locations`（whName、x/y/z），不含旋转 |
| 条码/标识 | `config.barcode`（可选） | 常放在配置键中（如 `barcode`） | `barCode` |
| 数量单位 | 无固定键，通常在 `data` | 无固定键，通常在配置或状态中 | `unit` |
| 物料编码 | 通常在 `config` 或 `data` 自定义 | 通常在配置中自定义 | `code` |

说明：
- Bioyond 不提供显式的树形父子关系，通常通过 `locations` 将物料落位到某仓位/坐标。用 `detail` 表示子级明细。

---

## 3. children 的四种结构表示

- **list（扁平列表）**：每个节点是扁平字典，`children` 为子节点 `id` 数组。示例：Uni-Lab-OS `nodes` 中的单个节点。

```json
{
  "nodes": [
    { "id": "root", "parent": null, "children": ["child1"] },
    { "id": "child1", "parent": "root", "children": [] }
  ]
}
```
- **dict（嵌套字典）**：节点的 `children` 是 `{ child_id: child_node_dict }` 字典。

```json
{
  "id": "root",
  "parent": null,
  "children": {
    "child1": { "id": "child1", "parent": "root", "children": {} }
  }
}
```
- **tree（树形列表）**：顶层是 `[root_node, ...]`，每个 `node.children` 是“子节点对象列表”（而非 id 列表）。

```json
[
  {
    "id": "root",
    "parent": null,
    "children": [
      { "id": "child1", "parent": "root", "children": [] }
    ]
  }
]
```
- **nestdict（顶层嵌套字典）**：顶层是 `{root_id: root_node, ...}`，或者根节点自身带 `children: {id: node}` 形态。

```json
{
  "root": {
    "id": "root",
    "parent": null,
    "children": {
      "child1": { "id": "child1", "parent": "root", "children": {} }
    }
  }
}
```

当前运行时只认 `parent` / `parent_uuid` 表达层级，`children` 是派生字段：读取时忽略，
导出时不写。上面几种形态之间的互转统一经 `ResourceTreeSet` 完成（见下一节）。

---

## 4. 转换函数及调用

核心代码文件：`unilabos/resources/resource_tracker.py`（`ResourceTreeSet`）。早期
`graphio.py` 里的互转函数（`dict_to_tree`、`tree_to_list`、`convert_resources_to_type`、
`resource_plr_to_ulab`、`initialize_resource` 等）已删除，统一使用下列 API。

### 4.1 扁平列表 ↔ 资源树

```python
from unilabos.resources.resource_tracker import ResourceTreeSet

# 扁平节点列表（层级用 parent_uuid / parent 表达）-> 资源树集合
tree_set = ResourceTreeSet.from_raw_dict_list(nodes)
tree_set.root_nodes            # 每棵树的根（ResourceDictInstance，.children 为子节点）
tree_set.all_nodes             # 深度优先的全部节点
tree_set.find_by_uuid(uuid)

# 资源树 -> 每棵树一组扁平节点（parent_uuid，无 children），可 JSON 序列化
dumped = tree_set.dump()       # list[list[dict]]
restored = ResourceTreeSet.load(dumped)
```

设备图（node-link JSON）不直接走这里：`unilab -g` 先登记到 Graph Authority，再由
`unilabos.server.services.materials.graph.load_graph_payload` 装配成
`ResourceTreeSet`、backend 连线与协议编译器用的物理拓扑图，详见 [设备图文件说明](../../user_guide/graph_files.md)。

### 4.2 Uni-Lab-OS ↔ PyLabRobot（PLR）

```python
from unilabos.resources.resource_tracker import ResourceTreeSet

# PLR 资源对象 -> 资源树（每个资源须已带权威分配的 unilabos_uuid）
tree_set = ResourceTreeSet.from_plr_resources([plr_root])
ulab_flat = tree_set.dump()[0]

# 资源树 -> PLR 资源对象（按 config.type 查找 PLR 类）
plr_root = tree_set.to_plr_resources()[0]
```

`from_plr_resources(..., known_random_uuid=True)` 只用于尚未登记的草稿 / 测试：它为缺少
UUID 的资源生成临时 UUID，真正的身份仍以微后端创建后返回的为准。

### 4.3 按注册表资源类创建

```python
from unilabos.resources import materials

# 只实例化本地 PLR 草稿（无 uuid）
draft = materials.instantiate_registry_resource("my_plate_class", "plate_1")

# 实例化并在权威登记，返回带 uuid 的权威 PLR 对象
plate = materials.create("my_plate_class", name="plate_1")
```

### 4.4 第三方格式（如奔耀 Bioyond）

第三方物料格式的映射属于外部驱动包：驱动先按自己的 `type_mapping` 构造 PLR 资源树，
再经 `materials.create` / `materials.ensure` 登记到权威，或用
`ResourceTreeSet.from_plr_resources` 得到扁平格式。

---

## 5. 何时使用哪种格式

- **云端/持久化**：使用 Uni-Lab-OS 物料格式（扁平 `nodes` 列表，children 为 id 列表）。便于版本化、可视化与网络传输。
- **实验工作流执行**：使用 PyLabRobot（PLR）格式。PLR 运行时依赖严格的树形资源结构与对象 API。
- **第三方设备/系统（Bioyond）输入**：保持来源格式不变，由驱动包按人工维护的 `type_mapping` 构造 PLR 资源树，再登记到权威（必要时经 `ResourceTreeSet` 转 Uni-Lab-OS）。

---

## 6. 常见问题与注意事项

- **children 形态不一致**：`ResourceTreeSet` 只按 `parent_uuid` / `parent` 建立层级，`children` 列表会被忽略；旧格式图（`children` id 列表、根级 `position` 等）在 `-g` / `unilab graph upload` 读取边界由 legacy 适配层统一转换。
- **UUID**：`from_raw_dict_list` / `load` / `from_plr_resources` 都要求每个节点带权威 UUID（或 `data.unilabos_uuid`），不会随机生成。
- **PLR 类型**：`to_plr_resources` 只按 `config.type` 查找 PLR 类，不回退到 `class` / `template_name`。
- **按注册表创建**：`materials.instantiate_registry_resource` 只支持 `class.type == "pylabrobot"` 的注册表资源类。

参考代码：

- `unilabos/resources/resource_tracker.py`（`ResourceTreeSet`）
- `unilabos/resources/materials.py`（`create`、`instantiate_registry_resource`）
- `unilabos/server/services/materials/graph.py`（`load_graph_payload`）
