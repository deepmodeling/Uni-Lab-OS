# 资源高级参考

本文件是 SKILL.md 的补充，包含类继承体系、序列化/反序列化、Bioyond 物料同步、非瓶类资源和仓库工厂模式。Agent 在需要实现这些功能时按需阅读。

---

## 1. 类继承体系

```
PyLabRobot
├── Resource (PLR 基类)
│   ├── Well
│   │   └── Bottle (unilabos)        → 瓶/小瓶/烧杯/反应器
│   ├── Deck
│   │   └── 自定义 Deck 类 (unilabos) → 工作站台面
│   ├── ResourceHolder               → 槽位占位符
│   └── Container
│       └── Battery (unilabos)       → 组装好的电池
│
├── ItemizedCarrier (unilabos, 继承 Resource)
│   ├── BottleCarrier (unilabos)     → 瓶载架
│   └── WareHouse (unilabos)         → 堆栈仓库
│
├── ItemizedResource (PLR)
│   └── MagazineHolder (unilabos)    → 子弹夹载架
│
└── ResourceStack (PLR)
    └── Magazine (unilabos)          → 子弹夹洞位
```

### Bottle 类细节

```python
class Bottle(Well):
    def __init__(self, name, diameter, height, max_volume,
                 size_x=0.0, size_y=0.0, size_z=0.0,
                 barcode=None, category="container", model=None, **kwargs):
        super().__init__(
            name=name,
            size_x=diameter,    # PLR 用 diameter 作为 size_x/size_y
            size_y=diameter,
            size_z=height,      # PLR 用 height 作为 size_z
            max_volume=max_volume,
            category=category,
            model=model,
            bottom_type="flat",
            cross_section_type="circle"
        )
```

注意 `size_x = size_y = diameter`，`size_z = height`。

### ItemizedCarrier 核心方法

| 方法 | 说明 |
|------|------|
| `__getitem__(identifier)` | 通过索引或 Excel 标识（如 `"A01"`）访问槽位 |
| `__setitem__(identifier, resource)` | 向槽位放入资源 |
| `get_child_identifier(child)` | 获取子资源的标识符 |
| `capacity` | 总槽位数 |
| `sites` | 所有槽位字典 |

---

## 2. 序列化与反序列化

### PLR ↔ UniLab 转换

| 函数 | 位置 | 方向 |
|------|------|------|
| `ResourceTreeSet.from_plr_resources(resources)` | `resource_tracker.py` | PLR → UniLab |
| `ResourceTreeSet.to_plr_resources()` | `resource_tracker.py` | UniLab → PLR |

### `from_plr_resources` 流程

```
PLR Resource
    ↓ build_uuid_mapping (递归生成 UUID)
    ↓ resource.serialize() → dict
    ↓ resource.serialize_all_state() → states
    ↓ resource_plr_inner (递归构建 ResourceDictInstance)
ResourceTreeSet
```

关键：每个 PLR 资源通过 `unilabos_uuid` 属性携带 UUID，`unilabos_extra` 携带扩展数据（如 `class` 名）。

### `to_plr_resources` 流程

```
ResourceTreeSet
    ↓ collect_node_data (收集 UUID、状态、扩展数据)
    ↓ node_to_plr_dict (转为 PLR 字典格式)
    ↓ find_subclass(type_name, PLRResource) (查找 PLR 子类)
    ↓ sub_cls.deserialize(plr_dict) (反序列化)
    ↓ loop_set_uuid, loop_set_extra (递归设置 UUID 和扩展)
PLR Resource
```

### Bottle 序列化

```python
class Bottle(Well):
    def serialize(self) -> dict:
        data = super().serialize()
        return {**data, "diameter": self.diameter, "height": self.height}

    @classmethod
    def deserialize(cls, data: dict, allow_marshal=False):
        barcode_data = data.pop("barcode", None)
        instance = super().deserialize(data, allow_marshal=allow_marshal)
        if barcode_data and isinstance(barcode_data, str):
            instance.barcode = barcode_data
        return instance
```

---

## 3. Bioyond 物料同步

### 双向转换函数

| 函数 | 位置 | 方向 |
|------|------|------|
| `resource_bioyond_to_plr(materials, type_mapping, deck)` | `graphio.py` | Bioyond → PLR |
| `resource_plr_to_bioyond(resources, type_mapping, warehouse_mapping)` | `graphio.py` | PLR → Bioyond |

### `resource_bioyond_to_plr` 流程

```
Bioyond 物料列表
    ↓ reverse_type_mapping: {typeName → (model, UUID)}
    ↓ 对每个物料:
       typeName → 查映射 → model (如 "BIOYOND_PolymerStation_Reactor")
       initialize_resource({"name": unique_name, "class": model})
    ↓ 设置 unilabos_extra (material_bioyond_id, material_bioyond_name 等)
    ↓ 处理 detail (子物料/坐标)
    ↓ 按 locationName 放入 deck.warehouses 对应槽位
PLR 资源列表
```

### `resource_plr_to_bioyond` 流程

```
PLR 资源列表
    ↓ 遍历每个资源:
       载架(capacity > 1): 生成 details 子物料 + 坐标
       单瓶: 直接映射
    ↓ type_mapping 查找 typeId
    ↓ warehouse_mapping 查找位置 UUID
    ↓ 组装 Bioyond 格式 (name, typeName, typeId, quantity, Parameters, locations)
Bioyond 物料列表
```

### BioyondResourceSynchronizer

工作站通过 `ResourceSynchronizer` 自动同步物料：

```python
class BioyondResourceSynchronizer(ResourceSynchronizer):
    def sync_from_external(self) -> bool:
        all_data = []
        all_data.extend(api_client.stock_material('{"typeMode": 0}'))  # 耗材
        all_data.extend(api_client.stock_material('{"typeMode": 1}'))  # 样品
        all_data.extend(api_client.stock_material('{"typeMode": 2}'))  # 试剂
        unilab_resources = resource_bioyond_to_plr(
            all_data,
            type_mapping=self.workstation.bioyond_config["material_type_mappings"],
            deck=self.workstation.deck
        )
        # 更新 deck 上的资源
```

---

## 4. 非瓶类资源

### ElectrodeSheet（极片）

路径：`unilabos/resources/battery/electrode_sheet.py`

```python
class ElectrodeSheet(ResourcePLR):
    """片状材料（极片、隔膜、弹片、垫片等）"""
    _unilabos_state = {
        "diameter": 0.0,
        "thickness": 0.0,
        "mass": 0.0,
        "material_type": "",
        "color": "",
        "info": "",
    }
```

工厂函数：`PositiveCan`, `PositiveElectrode`, `NegativeCan`, `NegativeElectrode`, `SpringWasher`, `FlatWasher`, `AluminumFoil`

### Battery（电池）

```python
class Battery(Container):
    """组装好的电池"""
    _unilabos_state = {
        "color": "",
        "electrolyte_name": "",
        "open_circuit_voltage": 0.0,
    }
```

### Magazine / MagazineHolder（子弹夹）

```python
class Magazine(ResourceStack):
    """子弹夹洞位，可堆叠 ElectrodeSheet"""
    # direction, max_sheets

class MagazineHolder(ItemizedResource):
    """多洞位子弹夹"""
    # hole_diameter, hole_depth, max_sheets_per_hole
```

工厂函数 `magazine_factory()` 用 `create_homogeneous_resources` 生成洞位，可选预填 `ElectrodeSheet` 或 `Battery`。

---

## 5. 仓库工厂模式参考

### 实际 warehouse 工厂函数示例

```python
# 行优先 4x4 仓库
def bioyond_warehouse_1x4x4(name: str) -> WareHouse:
    return warehouse_factory(
        name=name,
        num_items_x=4, num_items_y=4, num_items_z=1,
        dx=10.0, dy=10.0, dz=10.0,
        item_dx=147.0, item_dy=106.0, item_dz=130.0,
        layout="row-major",  # A01,A02,A03,A04, B01,...
    )

# 右侧 4x4 仓库（列名偏移）
def bioyond_warehouse_1x4x4_right(name: str) -> WareHouse:
    return warehouse_factory(
        name=name,
        num_items_x=4, num_items_y=4, num_items_z=1,
        dx=10.0, dy=10.0, dz=10.0,
        item_dx=147.0, item_dy=106.0, item_dz=130.0,
        col_offset=4,      # A05,A06,A07,A08
        layout="row-major",
    )

# 竖向仓库（站内试剂存放）
def bioyond_warehouse_reagent_storage(name: str) -> WareHouse:
    return warehouse_factory(
        name=name,
        num_items_x=1, num_items_y=2, num_items_z=1,
        dx=10.0, dy=10.0, dz=10.0,
        item_dx=147.0, item_dy=106.0, item_dz=130.0,
        layout="vertical-col-major",
    )

# 行偏移（F 行开始）
def bioyond_warehouse_5x3x1(name: str, row_offset: int = 0) -> WareHouse:
    return warehouse_factory(
        name=name,
        num_items_x=3, num_items_y=5, num_items_z=1,
        dx=10.0, dy=10.0, dz=10.0,
        item_dx=159.0, item_dy=183.0, item_dz=130.0,
        row_offset=row_offset,  # 0→A行起，5→F行起
        layout="row-major",
    )
```

### layout 类型说明

| layout | 命名顺序 | 适用场景 |
|--------|---------|---------|
| `col-major` (默认) | A01,B01,C01,D01, A02,B02,... | 列优先，标准堆栈 |
| `row-major` | A01,A02,A03,A04, B01,B02,... | 行优先，Bioyond 前端展示 |
| `vertical-col-major` | 竖向排列，标签从底部开始 | 竖向仓库（试剂存放、测密度） |

---

## 6. 关键路径

| 内容 | 路径 |
|------|------|
| Bottle/Carrier 基类 | `unilabos/resources/itemized_carrier.py` |
| WareHouse 类 + 工厂 | `unilabos/resources/warehouse.py` |
| ResourceTreeSet 转换 | `unilabos/resources/resource_tracker.py` |
| Bioyond 物料转换 | `unilabos/resources/graphio.py` |
| Bioyond 仓库定义 | `unilabos/resources/bioyond/warehouses.py` |
| 电池资源 | `unilabos/resources/battery/` |
| PLR 注册 | `unilabos/resources/plr_additional_res_reg.py` |
