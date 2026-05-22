---
name: add-resource
description: Guide for adding new resources (materials, bottles, carriers, decks, warehouses) to Uni-Lab-OS (添加新物料/资源). Uses @resource decorator for AST auto-scanning. Covers Bottle, Carrier, Deck, WareHouse definitions. Use when the user wants to add resources, define materials, create a deck layout, add bottles/carriers/plates, or mentions 物料/资源/resource/bottle/carrier/deck/plate/warehouse.
---

# 添加新物料资源

Uni-Lab-OS 的资源体系基于 PyLabRobot，通过扩展实现 Bottle、Carrier、WareHouse、Deck 等实验室物料管理。使用 `@resource` 装饰器注册，AST 自动扫描生成注册表条目。

---

## 资源类型

| 类型 | 基类 | 用途 | 示例 |
|------|------|------|------|
| **Bottle** | `Well` (PyLabRobot) | 单个容器（瓶、小瓶、烧杯、反应器） | 试剂瓶、粉末瓶 |
| **BottleCarrier** | `ItemizedCarrier` | 多槽位载架（放多个 Bottle） | 6 位试剂架、枪头盒 |
| **WareHouse** | `ItemizedCarrier` | 堆栈/仓库（放多个 Carrier） | 4x4 堆栈 |
| **Deck** | `Deck` (PyLabRobot) | 工作站台面（放多个 WareHouse） | 反应站 Deck |

**层级关系：** `Deck` → `WareHouse` → `BottleCarrier` → `Bottle`

WareHouse 本质上和 Site 是同一概念 — 都是定义一组固定的放置位（slot），只不过 WareHouse 多嵌套了一层 Deck。两者都需要开发者根据实际物理尺寸自行计算各 slot 的偏移坐标。

---

## @resource 装饰器

```python
from unilabos.registry.decorators import resource

@resource(
    id="my_resource_id",        # 注册表唯一标识（必填）
    category=["bottles"],       # 分类标签列表（必填）
    description="资源描述",
    icon="",                    # 图标
    version="1.0.0",
    handles=[...],              # 端口列表（InputHandle / OutputHandle）
    model={...},                # 3D 模型配置
    class_type="pylabrobot",    # "python" / "pylabrobot" / "unilabos"
)
```

---

## 创建规范

### 命名规则

1. **`name` 参数作为前缀**：所有工厂函数必须接受 `name: str` 参数，创建子物料时以 `name` 作为前缀，确保实例名在运行时全局唯一
2. **Bottle 命名约定**：试剂瓶-Bottle，烧杯-Beaker，烧瓶-Flask，小瓶-Vial
3. **函数名 = `@resource(id=...)`**：工厂函数名与注册表 id 保持一致

### 子物料命名示例

```python
# Carrier 内部的 sites 用 name 前缀
for k, v in sites.items():
    v.name = f"{name}_{v.name}"     # "堆栈1左_A01", "堆栈1左_B02" ...

# Carrier 中放置 Bottle 时用 name 前缀
carrier[0] = My_Reagent_Bottle(f"{name}_flask_1")    # "堆栈1左_flask_1"
carrier[i] = My_Solid_Vial(f"{name}_vial_{ordering[i]}")  # "堆栈1左_vial_A1"

# create_homogeneous_resources 使用 name_prefix
sites=create_homogeneous_resources(
    klass=ResourceHolder,
    locations=[...],
    name_prefix=name,  # 自动生成 "{name}_0", "{name}_1" ...
)

# Deck setup 中用仓库名称作为 name 传入
self.warehouses = {
    "堆栈1左": my_warehouse_4x4("堆栈1左"),   # WareHouse.name = "堆栈1左"
    "试剂堆栈": my_reagent_stack("试剂堆栈"),  # WareHouse.name = "试剂堆栈"
}
```

### 其他规范

- **max_volume 单位为 μL**：500mL = 500000
- **尺寸单位为 mm**：`diameter`, `height`, `size_x/y/z`, `dx/dy/dz`
- **BottleCarrier 必须设置 `num_items_x/y/z`**：用于前端渲染布局
- **Deck 的 `__init__` 必须接受 `setup=False`**：图文件中 `config.setup=true` 触发 `setup()`
- **按项目分组文件**：同一工作站的资源放在 `unilabos/resources/<project>/` 下
- **`__init__` 必须接受 `serialize()` 输出的所有字段**：`serialize()` 输出会作为 `config` 回传到 `__init__`，因此必须通过显式参数或 `**kwargs` 接受，否则反序列化会报错
- **持久化运行时状态用 `serialize_state()`**：通过 `_unilabos_state` 字典存储可变信息（如物料内容、液体量），只存 JSON 可序列化的基本类型

---

## 资源模板

### Bottle

```python
from unilabos.registry.decorators import resource
from unilabos.resources.itemized_carrier import Bottle


@resource(id="My_Reagent_Bottle", category=["bottles"], description="我的试剂瓶")
def My_Reagent_Bottle(
    name: str,
    diameter: float = 70.0,
    height: float = 120.0,
    max_volume: float = 500000.0,
    barcode: str = None,
) -> Bottle:
    return Bottle(
        name=name,
        diameter=diameter,
        height=height,
        max_volume=max_volume,
        barcode=barcode,
        model="My_Reagent_Bottle",
    )
```

**Bottle 参数：**
- `name`: 实例名称（运行时唯一，由上层 Carrier 以前缀方式传入）
- `diameter`: 瓶体直径 (mm)
- `height`: 瓶体高度 (mm)
- `max_volume`: 最大容积（**μL**，500mL = 500000）
- `barcode`: 条形码（可选）

### BottleCarrier

```python
from pylabrobot.resources import ResourceHolder
from pylabrobot.resources.carrier import create_ordered_items_2d
from unilabos.resources.itemized_carrier import BottleCarrier
from unilabos.registry.decorators import resource


@resource(id="My_6SlotCarrier", category=["bottle_carriers"], description="六槽位载架")
def My_6SlotCarrier(name: str) -> BottleCarrier:
    sites = create_ordered_items_2d(
        klass=ResourceHolder,
        num_items_x=3, num_items_y=2,
        dx=10.0, dy=10.0, dz=5.0,
        item_dx=42.0, item_dy=35.0,
        size_x=20.0, size_y=20.0, size_z=50.0,
    )
    # 子 site 用 name 作为前缀
    for k, v in sites.items():
        v.name = f"{name}_{v.name}"

    carrier = BottleCarrier(
        name=name, size_x=146.0, size_y=80.0, size_z=55.0,
        sites=sites, model="My_6SlotCarrier",
    )
    carrier.num_items_x = 3
    carrier.num_items_y = 2
    carrier.num_items_z = 1

    # 放置 Bottle 时用 name 作为前缀
    ordering = ["A1", "B1", "A2", "B2", "A3", "B3"]
    for i in range(6):
        carrier[i] = My_Reagent_Bottle(f"{name}_vial_{ordering[i]}")
    return carrier
```

### WareHouse / Deck 放置位

WareHouse 和 Site 本质上是同一概念：都是定义一组固定放置位（slot），根据物理尺寸自行批量计算偏移坐标。WareHouse 只是多嵌套了一层 Deck 而已。推荐开发者直接根据实物测量数据计算各 slot 偏移量。

#### WareHouse（使用 warehouse_factory）

```python
from unilabos.resources.warehouse import warehouse_factory
from unilabos.registry.decorators import resource


@resource(id="my_warehouse_4x4", category=["warehouse"], description="4x4 堆栈仓库")
def my_warehouse_4x4(name: str) -> "WareHouse":
    return warehouse_factory(
        name=name,
        num_items_x=4, num_items_y=4, num_items_z=1,
        dx=10.0, dy=10.0, dz=10.0,             # 第一个 slot 的起始偏移
        item_dx=147.0, item_dy=106.0, item_dz=130.0,  # slot 间距
        resource_size_x=127.0, resource_size_y=85.0, resource_size_z=100.0,  # slot 尺寸
        model="my_warehouse_4x4",
        col_offset=0,       # 列标签起始偏移（0 → A01, 4 → A05）
        layout="row-major",  # "row-major" 行优先 / "col-major" 列优先 / "vertical-col-major" 竖向
    )
```

`warehouse_factory` 参数说明：
- `dx/dy/dz`：第一个 slot 相对 WareHouse 原点的偏移（mm）
- `item_dx/item_dy/item_dz`：相邻 slot 间距（mm），需根据实际物理间距测量
- `resource_size_x/y/z`：每个 slot 的可放置区域尺寸
- `layout`：影响 slot 标签和坐标映射
  - `"row-major"`：A01,A02,...,B01,B02,...（行优先，适合横向排列）
  - `"col-major"`：A01,B01,...,A02,B02,...（列优先）
  - `"vertical-col-major"`：竖向排列，y 坐标反向

#### Deck 组装 WareHouse

Deck 通过 `setup()` 将多个 WareHouse 放置到指定坐标：

```python
from pylabrobot.resources import Deck, Coordinate
from unilabos.registry.decorators import resource


@resource(id="MyStation_Deck", category=["deck"], description="我的工作站 Deck")
class MyStation_Deck(Deck):
    def __init__(self, name="MyStation_Deck", size_x=2700.0, size_y=1080.0, size_z=1500.0,
                 category="deck", setup=False, **kwargs) -> None:
        super().__init__(name=name, size_x=size_x, size_y=size_y, size_z=size_z)
        if setup:
            self.setup()

    def setup(self) -> None:
        self.warehouses = {
            "堆栈1左": my_warehouse_4x4("堆栈1左"),
            "堆栈1右": my_warehouse_4x4("堆栈1右"),
        }
        self.warehouse_locations = {
            "堆栈1左": Coordinate(-200.0, 400.0, 0.0),  # 自行测量计算
            "堆栈1右": Coordinate(2350.0, 400.0, 0.0),
        }
        for wh_name, wh in self.warehouses.items():
            self.assign_child_resource(wh, location=self.warehouse_locations[wh_name])
```

#### Site 模式（前端定向放置）

适用于有固定孔位/槽位的设备（如移液站 PRCXI 9300），Deck 通过 `sites` 列表定义前端展示的放置位，前端据此渲染可拖拽的孔位布局：

```python
import collections
from typing import Any, Dict, List, Optional
from pylabrobot.resources import Deck, Resource, Coordinate
from unilabos.registry.decorators import resource


@resource(id="MyLabDeck", category=["deck"], description="带 Site 定向放置的 Deck")
class MyLabDeck(Deck):
    # 根据设备台面实测批量计算各 slot 坐标偏移
    _DEFAULT_SITE_POSITIONS = [
        (0, 0, 0), (138, 0, 0), (276, 0, 0), (414, 0, 0),       # T1-T4
        (0, 96, 0), (138, 96, 0), (276, 96, 0), (414, 96, 0),   # T5-T8
    ]
    _DEFAULT_SITE_SIZE = {"width": 128.0, "height": 86.0, "depth": 0}
    _DEFAULT_CONTENT_TYPE = ["plate", "tip_rack", "tube_rack", "adaptor"]

    def __init__(self, name: str, size_x: float, size_y: float, size_z: float,
                 sites: Optional[List[Dict[str, Any]]] = None, **kwargs):
        super().__init__(size_x, size_y, size_z, name)
        if sites is not None:
            self.sites = [dict(s) for s in sites]
        else:
            self.sites = []
            for i, (x, y, z) in enumerate(self._DEFAULT_SITE_POSITIONS):
                self.sites.append({
                    "label": f"T{i + 1}",          # 前端显示的槽位标签
                    "visible": True,                # 是否在前端可见
                    "position": {"x": x, "y": y, "z": z},  # 槽位物理坐标
                    "size": dict(self._DEFAULT_SITE_SIZE),  # 槽位尺寸
                    "content_type": list(self._DEFAULT_CONTENT_TYPE),  # 允许放入的物料类型
                })
        self._ordering = collections.OrderedDict(
            (site["label"], None) for site in self.sites
        )

    def assign_child_resource(self, resource: Resource,
                              location: Optional[Coordinate] = None,
                              reassign: bool = True,
                              spot: Optional[int] = None):
        idx = spot
        if spot is None:
            for i, site in enumerate(self.sites):
                if site.get("label") == resource.name:
                    idx = i
                    break
        if idx is None:
            for i in range(len(self.sites)):
                if self._get_site_resource(i) is None:
                    idx = i
                    break
        if idx is None:
            raise ValueError(f"No available site for '{resource.name}'")
        loc = Coordinate(**self.sites[idx]["position"])
        super().assign_child_resource(resource, location=loc, reassign=reassign)

    def serialize(self) -> dict:
        data = super().serialize()
        sites_out = []
        for i, site in enumerate(self.sites):
            occupied = self._get_site_resource(i)
            sites_out.append({
                "label": site["label"],
                "visible": site.get("visible", True),
                "occupied_by": occupied.name if occupied else None,
                "position": site["position"],
                "size": site["size"],
                "content_type": site["content_type"],
            })
        data["sites"] = sites_out
        return data
```

**Site 字段说明：**

| 字段 | 类型 | 说明 |
|------|------|------|
| `label` | str | 槽位标签（如 `"T1"`），前端显示名称，也用于匹配 resource.name |
| `visible` | bool | 是否在前端可见 |
| `position` | dict | 物理坐标 `{x, y, z}`（mm），需自行测量计算偏移 |
| `size` | dict | 槽位尺寸 `{width, height, depth}`（mm） |
| `content_type` | list | 允许放入的物料类型，如 `["plate", "tip_rack", "tube_rack", "adaptor"]` |

**参考实现：** `unilabos/devices/liquid_handling/prcxi/prcxi.py` 中的 `PRCXI9300Deck`（4x4 共 16 个 site）。

---

## 文件位置

```
unilabos/resources/
├── <project>/              # 按项目分组
│   ├── bottles.py          # Bottle 工厂函数
│   ├── bottle_carriers.py  # Carrier 工厂函数
│   ├── warehouses.py       # WareHouse 工厂函数
│   └── decks.py            # Deck 类定义
```

---

## 验证

```bash
# 资源可导入
python -c "from unilabos.resources.my_project.bottles import My_Reagent_Bottle; print(My_Reagent_Bottle('test'))"

# 启动测试（AST 自动扫描）
unilab -g <graph>.json
```

仅在以下情况仍需 YAML：第三方库资源（如 pylabrobot 内置资源，无 `@resource` 装饰器）。

---

## 关键路径

| 内容 | 路径 |
|------|------|
| Bottle/Carrier 基类 | `unilabos/resources/itemized_carrier.py` |
| WareHouse 基类 + 工厂 | `unilabos/resources/warehouse.py` |
| PLR 注册 | `unilabos/resources/plr_additional_res_reg.py` |
| 装饰器定义 | `unilabos/registry/decorators.py` |
