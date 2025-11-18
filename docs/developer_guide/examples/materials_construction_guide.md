# 实例：物料构建指南

> **文档类型**：物料系统实战指南  
> **适用场景**：工作站物料系统构建、Deck/Warehouse/Carrier/Bottle 配置  
> **前置知识**：PyLabRobot 基础 | 资源管理概念

## 概述

在UniLab-OS系统中，任何工作站中所需要用到的物料主要包括四个核心组件：

1. **桌子（Deck）** - 工作台面，定义整个工作空间的布局
2. **堆栈（Warehouse）** - 存储区域，用于放置载具和物料
3. **载具（Carriers）** - 承载瓶子等物料的容器架
4. **瓶子（Bottles）** - 实际的物料容器

本文档以BioYond工作站为例，详细说明如何构建这些物料组件。

## 文件结构

物料定义文件位于 `unilabos/resources/` 文件夹中：

```
unilabos/resources/bioyond/
├── decks.py              # 桌子定义
├── YB_warehouses.py      # 堆栈定义
├── YB_bottle_carriers.py # 载具定义
└── YB_bottles.py         # 瓶子定义
```

对应的注册表文件位于 `unilabos/registry/resources/bioyond/` 文件夹中：

```
unilabos/registry/resources/bioyond/
├── deck.yaml               # 桌子注册表
├── YB_bottle_carriers.yaml # 载具注册表
└── YB_bottle.yaml          # 瓶子注册表
```

## 1. 桌子（Deck）构建

桌子是整个工作站的基础，定义了工作空间的尺寸和各个组件的位置。

### 代码示例 (decks.py)

```python
from pylabrobot.resources import Coordinate, Deck
from unilabos.resources.bioyond.YB_warehouses import (
    bioyond_warehouse_2x2x1,
    bioyond_warehouse_3x5x1,
    bioyond_warehouse_20x1x1,
    bioyond_warehouse_3x3x1,
    bioyond_warehouse_10x1x1
)

class BIOYOND_YB_Deck(Deck):
    def __init__(
        self, 
        name: str = "YB_Deck",
        size_x: float = 4150,      # 桌子X方向尺寸 (mm)
        size_y: float = 1400.0,    # 桌子Y方向尺寸 (mm)
        size_z: float = 2670.0,    # 桌子Z方向尺寸 (mm)
        category: str = "deck",
        setup: bool = False
    ) -> None:
        super().__init__(name=name, size_x=4150.0, size_y=1400.0, size_z=2670.0)
        if setup:
            self.setup() # 当在工作站配置中setup为True时，自动创建并放置所有预定义的堆栈

    def setup(self) -> None:
        # 定义桌子上的各个仓库区域
        self.warehouses = {
            "自动堆栈-左": bioyond_warehouse_2x2x1("自动堆栈-左"),
            "自动堆栈-右": bioyond_warehouse_2x2x1("自动堆栈-右"),
            "手动堆栈-左": bioyond_warehouse_3x5x1("手动堆栈-左"),
            "手动堆栈-右": bioyond_warehouse_3x5x1("手动堆栈-右"),
            "粉末加样头堆栈": bioyond_warehouse_20x1x1("粉末加样头堆栈"),
            "配液站内试剂仓库": bioyond_warehouse_3x3x1("配液站内试剂仓库"),
            "试剂替换仓库": bioyond_warehouse_10x1x1("试剂替换仓库"),
        }
  
        # 定义各个仓库在桌子上的坐标位置
        self.warehouse_locations = {
            "自动堆栈-左": Coordinate(-100.3, 171.5, 0.0),
            "自动堆栈-右": Coordinate(3960.1, 155.9, 0.0),
            "手动堆栈-左": Coordinate(-213.3, 804.4, 0.0),
            "手动堆栈-右": Coordinate(3960.1, 807.6, 0.0),
            "粉末加样头堆栈": Coordinate(415.0, 1301.0, 0.0),
            "配液站内试剂仓库": Coordinate(2162.0, 437.0, 0.0),
            "试剂替换仓库": Coordinate(1173.0, 802.0, 0.0),
        }

        # 将仓库分配到桌子的指定位置
        for warehouse_name, warehouse in self.warehouses.items():
            self.assign_child_resource(warehouse, location=self.warehouse_locations[warehouse_name])
```

### 在工作站配置中的使用

当在工作站配置文件中定义桌子时，可以通过`setup`参数控制是否自动建立所有堆栈：

```json
{
  "id": "YB_Bioyond_Deck",
  "name": "YB_Bioyond_Deck", 
  "children": [],
  "parent": "bioyond_cell_workstation",
  "type": "deck",
  "class": "BIOYOND_YB_Deck",
  "config": {
    "type": "BIOYOND_YB_Deck",
    "setup": true
  },
  "data": {}
}
```

**重要说明**：
- 当 `"setup": true` 时，系统会自动调用桌子的 `setup()` 方法
- 这将创建并放置所有预定义的堆栈到桌子上的指定位置
- 如果 `"setup": false` 或省略该参数，则只创建空桌子，需要手动添加堆栈

### 关键要点注释

- `size_x`, `size_y`, `size_z`: 定义桌子的物理尺寸
- `warehouses`: 字典类型，包含桌子上所有的仓库区域
- `warehouse_locations`: 定义每个仓库在桌子坐标系中的位置
- `assign_child_resource()`: 将仓库资源分配到桌子的指定位置
- `setup()`: 可选的自动设置方法，初始化时可调用

## 2. 堆栈（Warehouse）构建

堆栈定义了存储区域的规格和布局，用于放置载具。

### 代码示例 (YB_warehouses.py)

```python
from unilabos.resources.warehouse import WareHouse, YB_warehouse_factory

def bioyond_warehouse_1x4x4(name: str) -> WareHouse:
    """创建BioYond 1x4x4仓库
  
    Args:
        name: 仓库名称
  
    Returns:
        WareHouse: 仓库对象
    """
    return YB_warehouse_factory(
        name=name,
        num_items_x=1,      # X方向位置数量
        num_items_y=4,      # Y方向位置数量  
        num_items_z=4,      # Z方向位置数量（层数）
        dx=10.0,            # X方向起始偏移
        dy=10.0,            # Y方向起始偏移
        dz=10.0,            # Z方向起始偏移
        item_dx=137.0,      # X方向间距
        item_dy=96.0,       # Y方向间距
        item_dz=120.0,      # Z方向间距（层高）
        category="warehouse",
    )

def bioyond_warehouse_2x2x1(name: str) -> WareHouse:
    """创建BioYond 2x2x1仓库（自动堆栈）"""
    return YB_warehouse_factory(
        name=name,
        num_items_x=2,
        num_items_y=2,
        num_items_z=1,      # 单层
        dx=10.0,
        dy=10.0,
        dz=10.0,
        item_dx=137.0,
        item_dy=96.0,
        item_dz=120.0,
        category="YB_warehouse",
    )
```

### 关键要点注释

- `num_items_x/y/z`: 定义仓库在各个方向的位置数量
- `dx/dy/dz`: 第一个位置的起始偏移坐标
- `item_dx/dy/dz`: 相邻位置之间的间距
- `category`: 仓库类别，用于分类管理
- `YB_warehouse_factory`: 统一的仓库创建工厂函数

## 3. 载具（Carriers）构建

载具是承载瓶子的容器架，定义了瓶子的排列方式和位置。

### 代码示例 (YB_bottle_carriers.py)

```python
from pylabrobot.resources import create_homogeneous_resources, Coordinate, ResourceHolder, create_ordered_items_2d
from unilabos.resources.itemized_carrier import Bottle, BottleCarrier
from unilabos.resources.bioyond.YB_bottles import YB_pei_ye_xiao_Bottle

def YB_peiyepingxiaoban(name: str) -> BottleCarrier:
    """配液瓶(小)板 - 4x2布局，8个位置
  
    Args:
        name: 载具名称
  
    Returns:
        BottleCarrier: 载具对象，包含8个配液瓶位置
    """
  
    # 载具物理尺寸 (mm)
    carrier_size_x = 127.8
    carrier_size_y = 85.5
    carrier_size_z = 65.0

    # 瓶位参数
    bottle_diameter = 35.0      # 瓶子直径
    bottle_spacing_x = 42.0     # X方向瓶子间距
    bottle_spacing_y = 35.0     # Y方向瓶子间距

    # 计算起始位置 (居中排列)
    start_x = (carrier_size_x - (4 - 1) * bottle_spacing_x - bottle_diameter) / 2
    start_y = (carrier_size_y - (2 - 1) * bottle_spacing_y - bottle_diameter) / 2

    # 创建瓶位布局：4列x2行
    sites = create_ordered_items_2d(
        klass=ResourceHolder,
        num_items_x=4,          # 4列
        num_items_y=2,          # 2行
        dx=start_x,
        dy=start_y,
        dz=5.0,                 # 瓶子底部高度
        item_dx=bottle_spacing_x,
        item_dy=bottle_spacing_y,
        size_x=bottle_diameter,
        size_y=bottle_diameter,
        size_z=carrier_size_z,
    )
  
    # 为每个瓶位设置名称
    for k, v in sites.items():
        v.name = f"{name}_{v.name}"

    # 创建载具对象
    carrier = BottleCarrier(
        name=name,
        size_x=carrier_size_x,
        size_y=carrier_size_y,
        size_z=carrier_size_z,
        sites=sites,
        model="YB_peiyepingxiaoban",
    )
  
    # 设置载具布局参数
    carrier.num_items_x = 4
    carrier.num_items_y = 2
    carrier.num_items_z = 1
  
    # 定义瓶子排列顺序
    ordering = ["A1", "A2", "A3", "A4", "B1", "B2", "B3", "B4"]
  
    # 为每个位置创建瓶子实例
    for i in range(8):
        carrier[i] = YB_pei_ye_xiao_Bottle(f"{name}_bottle_{ordering[i]}")
  
    return carrier
```

### 关键要点注释

- `carrier_size_x/y/z`: 载具的物理尺寸
- `bottle_diameter`: 瓶子的直径，用于计算瓶位大小
- `bottle_spacing_x/y`: 瓶子之间的间距
- `create_ordered_items_2d`: 创建二维排列的瓶位
- `sites`: 瓶位字典，存储所有瓶子位置信息
- `ordering`: 定义瓶位的命名规则（如A1, A2, B1等）

## 4. 瓶子（Bottles）构建

瓶子是最终的物料容器，定义了容器的物理属性。

### 代码示例 (YB_bottles.py)

```python
from unilabos.resources.itemized_carrier import Bottle

def YB_pei_ye_xiao_Bottle(
    name: str,
    diameter: float = 35.0,         # 瓶子直径 (mm)
    height: float = 60.0,           # 瓶子高度 (mm)
    max_volume: float = 30000.0,    # 最大容量 (μL) - 30mL
    barcode: str = None,            # 条码
) -> Bottle:
    """创建配液瓶(小)
  
    Args:
        name: 瓶子名称
        diameter: 瓶子直径
        height: 瓶子高度
        max_volume: 最大容量（微升）
        barcode: 条码标识
  
    Returns:
        Bottle: 瓶子对象
    """
    return Bottle(
        name=name,
        diameter=diameter,
        height=height,
        max_volume=max_volume,
        barcode=barcode,
        model="YB_pei_ye_xiao_Bottle",    
    )

def YB_ye_Bottle(
    name: str,
    diameter: float = 40.0,
    height: float = 70.0,
    max_volume: float = 50000.0,    # 最大容量
    barcode: str = None,
) -> Bottle:
    """创建液体瓶"""
    return Bottle(
        name=name,
        diameter=diameter,
        height=height,
        max_volume=max_volume,
        barcode=barcode,
        model="YB_ye_Bottle",
    )
```

### 关键要点注释

- `diameter`: 瓶子直径，影响瓶位大小计算
- `height`: 瓶子高度，用于碰撞检测和移液计算
- `max_volume`: 最大容量，单位为微升（μL）
- `barcode`: 条码标识，用于瓶子追踪
- `model`: 型号标识，用于区分不同类型的瓶子

## 5. 注册表配置

创建完物料定义后，需要在注册表中注册这些物料，使系统能够识别和使用它们。

在 `unilabos/registry/resources/bioyond/` 目录下创建：

- `deck.yaml` - 桌子注册表
- `YB_bottle_carriers.yaml` - 载具注册表
- `YB_bottle.yaml` - 瓶子注册表

### 5.1 桌子注册表 (deck.yaml)

```yaml
BIOYOND_YB_Deck:
  category:
    - deck                          # 前端显示的分类存放
  class:
    module: unilabos.resources.bioyond.decks:BIOYOND_YB_Deck  # 定义桌子的类的路径
    type: pylabrobot              
  description: BIOYOND_YB_Deck     # 描述信息
  handles: []                     
  icon: 配液站.webp               # 图标文件
  init_param_schema: {}         
  registry_type: resource         # 注册类型
  version: 1.0.0                  # 版本号
```

### 5.2 载具注册表 (YB_bottle_carriers.yaml)

```yaml
YB_peiyepingxiaoban:
  category:
    - yb3                          
    - YB_bottle_carriers          
  class:
    module: unilabos.resources.bioyond.YB_bottle_carriers:YB_peiyepingxiaoban
    type: pylabrobot
  description: YB_peiyepingxiaoban  
  handles: []
  icon: ''                      
  init_param_schema: {}
  registry_type: resource
  version: 1.0.0
```

### 5.3 瓶子注册表 (YB_bottle.yaml)

```yaml
YB_pei_ye_xiao_Bottle:
  category:
    - yb3                          
    - YB_bottle                 
  class:
    module: unilabos.resources.bioyond.YB_bottles:YB_pei_ye_xiao_Bottle
    type: pylabrobot
  description: YB_pei_ye_xiao_Bottle
  handles: []
  icon: ''
  init_param_schema: {}
  registry_type: resource
  version: 1.0.0
```

### 注册表关键要点注释

- `category`: 物料分类，用于在云端（网页界面）中的分类中显示
- `module`: Python模块路径，格式为 `模块路径:类名`
- `type`: 框架类型，通常为 `pylabrobot`（默认即可）
- `description`: 描述信息，显示在用户界面中
- `icon`: （名称唯一自动匹配后端上传的图标文件名，显示在云端）
- `registry_type`: 固定为 `resource`
- `version`: 版本号，用于版本管理
