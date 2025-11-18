# 实例：物料教程（Resource）

> **文档类型**：物料系统完整教程  
> **适用场景**：物料格式转换、多系统物料对接、资源结构理解  
> **前置知识**：Python 基础 | JSON 数据结构

本教程面向 Uni-Lab-OS 的开发者，讲解"物料"的核心概念、3种物料格式（UniLab、PyLabRobot、奔耀Bioyond）及其相互转换方法，并说明4种 children 结构表现形式及使用场景。

---

## 1. 物料是什么

- **物料（Resource）**：指实验工作站中的实体对象，包括设备（device）、操作甲板 （deck）、试剂、实验耗材，也包括设备上承载的具体物料或者包含的容器（如container/plate/well/瓶/孔/片等）。
- **物料基本信息**（以 UniLab list格式为例）：

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

## 2. 3种物料格式概览(UniLab、PyLabRobot、奔耀Bioyond)

### 2.1 UniLab 物料格式（云端/项目内通用）

- 结构特征：顶层通常是 `nodes` 列表；每个节点是扁平字典，`children` 是子节点 `id` 列表；`parent` 为父节点 `id` 或 `null`。
- 用途：
  - 云端数据存储、前端可视化、与图结构算法互操作
  - 在上传/下载/部署配置时作为标准交换格式

示例片段（UniLab 物料格式）：

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
### 2.4 3种物料格式关键字段对应(UniLab、PyLabRobot、奔耀Bioyond)

| 含义 | UniLab | PyLabRobot (PLR) | 奔耀 Bioyond |
| - | - | - | - |
| 节点唯一名 | `id`  | `name` | `name` |
| 父节点引用 | `parent` | `parent_name` | `locations` 坐标（无直接父名，需映射坐标下的物料） |
| 子节点集合 | `children`（id 列表或对象列表，视结构而定） | `children`（对象列表） | `detail`（明细，非严格树结构，需要自定义映射） |
| 类型（抽象类别） | `type`（device/container/plate/deck/…） | `category`（plate/well/…），以及类名 `type` | `typeName`（厂商自定义，如“液”、“加样头(大)”） |
| 运行/业务数据 | `data` | 通过 `serialize_all_state()`/`load_all_state()` 管理的状态 | `quantity`、`lockQuantity` 等业务数值 |
| 固有配置 | `config`（size_x/size_y/size_z/model/ordering…） | 资源字典中的同名键（反序列化时按构造签名取用） | 厂商自定义字段（需映射入 PLR/UniLab 的 `config` 或 `data`） |
| 空间位置 | `position`（x/y/z） | `location`（Coordinate） + `rotation`（Rotation） | `locations`（whName、x/y/z），不含旋转 |
| 条码/标识 | `config.barcode`（可选） | 常放在配置键中（如 `barcode`） | `barCode` |
| 数量单位 | 无固定键，通常在 `data` | 无固定键，通常在配置或状态中 | `unit` |
| 物料编码 | 通常在 `config` 或 `data` 自定义 | 通常在配置中自定义 | `code` |

说明：
- Bioyond 不提供显式的树形父子关系，通常通过 `locations` 将物料落位到某仓位/坐标。用 `detail` 表示子级明细。

---

## 3. children 的四种结构表示

- **list（扁平列表）**：每个节点是扁平字典，`children` 为子节点 `id` 数组。示例：UniLab `nodes` 中的单个节点。

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

这些结构之间可使用 `graphio.py` 中的工具函数互转（见下一节）。

---

## 4. 转换函数及调用

核心代码文件：`unilabos/resources/graphio.py`

### 4.1 结构互转（list/dict/tree/nestdict）

代码引用：

```217:239:unilabos/resources/graphio.py
def dict_to_tree(nodes: dict, devices_only: bool = False) -> list[dict]:
    # ... 由扁平 dict（id->node）生成树（children 为对象列表）
```

```241:267:unilabos/resources/graphio.py
def dict_to_nested_dict(nodes: dict, devices_only: bool = False) -> dict:
    # ... 由扁平 dict 生成嵌套字典（children 为 {id:node}）
```

```270:273:unilabos/resources/graphio.py
def list_to_nested_dict(nodes: list[dict]) -> dict:
    # ... 由扁平列表（children 为 id 列表）转嵌套字典
```

```275:286:unilabos/resources/graphio.py
def tree_to_list(tree: list[dict]) -> list[dict]:
    # ... 由树形列表转回扁平列表（children 还原为 id 列表）
```

```289:337:unilabos/resources/graphio.py
def nested_dict_to_list(nested_dict: dict) -> list[dict]:
    # ... 由嵌套字典转回扁平列表
```

常见路径：

- UniLab 扁平列表 → 树：`dict_to_tree({r["id"]: r for r in resources})`
- 树 → UniLab 扁平列表：`tree_to_list(resources_tree)`
- 扁平列表 ↔ 嵌套字典：`list_to_nested_dict` / `nested_dict_to_list`

### 4.2 UniLab ↔ PyLabRobot（PLR）

高层封装：

```339:368:unilabos/resources/graphio.py
def convert_resources_to_type(resources_list: list[dict], resource_type: Union[type, list[type]], *, plr_model: bool = False):
    # UniLab -> (NestedDict or PLR)
```

```371:395:unilabos/resources/graphio.py
def convert_resources_from_type(resources_list, resource_type: Union[type, list[type]], *, is_plr: bool = False):
    # (NestedDict or PLR) -> UniLab 扁平列表
```

底层转换：

```398:441:unilabos/resources/graphio.py
def resource_ulab_to_plr(resource: dict, plr_model=False) -> "ResourcePLR":
    # UniLab 单节点(树根) -> PLR Resource 对象
```

```443:481:unilabos/resources/graphio.py
def resource_plr_to_ulab(resource_plr: "ResourcePLR", parent_name: str = None, with_children=True):
    # PLR Resource -> UniLab 单节点(dict)
```

示例：

```python
from unilabos.resources.graphio import convert_resources_to_type, convert_resources_from_type
from pylabrobot.resources.resource import Resource as ResourcePLR

# UniLab 扁平列表 -> PLR 根资源对象
plr_root = convert_resources_to_type(resources_list=ulab_list, resource_type=ResourcePLR)

# PLR 资源对象 -> UniLab 扁平列表（用于保存/上传）
ulab_flat = convert_resources_from_type(resources_list=plr_root, resource_type=ResourcePLR)
```

可选项：

- `plr_model=True`：保留 `model` 字段（默认会移除）。
- `with_children=False`：`resource_plr_to_ulab` 仅转换当前节点。

### 4.3 奔耀（Bioyond）→ PLR（及进一步到 UniLab）

转换入口：

```483:527:unilabos/resources/graphio.py
def resource_bioyond_to_plr(bioyond_materials: list[dict], type_mapping: dict = {}, deck: Any = None) -> list[dict]:
    # Bioyond 列表 -> PLR 资源列表，并可根据 deck.warehouses 将资源落位
```

使用示例：

```python
import json
from unilabos.resources.graphio import resource_bioyond_to_plr, convert_resources_from_type
from pylabrobot.resources.resource import Resource as ResourcePLR

resp = json.load(open("unilabos/devices/workstation/bioyond_cell/bioyond_test_yibin.json", encoding="utf-8"))
materials = resp["data"]

# 将第三方类型name映射到 PLR 资源类名（需根据现场定义）
type_mapping = {
    "液": "RegularContainer",
    "加样头(大)": "RegularContainer"
}

plr_list = resource_bioyond_to_plr(materials, type_mapping=type_mapping, deck=None)

# 如需上传云端（UniLab 扁平格式）：
ulab_flat = convert_resources_from_type(plr_list, [ResourcePLR])
```

说明：

- `type_mapping` 必须由开发者根据设备/物料种类人工维护。
- 如传入 `deck`，且 `deck.warehouses` 命名与 `whName` 对应，可将物料安放到仓库坐标（x/y/z）。

---

## 5. 何时使用哪种格式

- **云端/持久化**：使用 UniLab 物料格式（扁平 `nodes` 列表，children 为 id 列表）。便于版本化、可视化与网络传输。
- **实验工作流执行**：使用 PyLabRobot（PLR）格式。PLR 运行时依赖严格的树形资源结构与对象 API。
- **第三方设备/系统（Bioyond）输入**：保持来源格式不变，使用 `resource_bioyond_to_plr` + 人工 `type_mapping` 将其转换为 PLR（必要时再转 UniLab）。

---

## 6. 常见问题与注意事项

- **children 形态不一致**：不同函数期望不同 children 形态，注意在进入转换前先用“结构互转”工具函数标准化形态。
- **devices_only**：`dict_to_tree/dict_to_nested_dict` 支持仅保留 `type == device` 的节点。
- **模型/类型字段**：PLR 对象序列化参数有所差异，`resource_ulab_to_plr` 内部会根据构造签名移除不兼容字段（如 `category`）。
- **驱动初始化**：`initialize_resource(s)` 支持从注册表/类路径创建 PLR/UniLab 资源或列表。

参考代码：

```530:577:unilabos/resources/graphio.py
def initialize_resource(resource_config: dict, resource_type: Any = None) -> Union[list[dict], ResourcePLR]:
    # 从注册类/模块反射创建资源，或将 UniLab 字典包装为列表
```

```580:597:unilabos/resources/graphio.py
def initialize_resources(resources_config) -> list[dict]:
    # 批量初始化
```




