# Bioyond 仓库系统开发指南

本文档详细说明 Bioyond 仓库（Warehouse）系统的架构、配置和使用方法，帮助开发者快速理解和维护仓库相关代码。

## 📚 目录

- [系统架构](#系统架构)
- [核心概念](#核心概念)
- [三层映射关系](#三层映射关系)
- [warehouse_factory 详解](#warehouse_factory-详解)
- [创建新仓库](#创建新仓库)
- [常见问题](#常见问题)
- [调试技巧](#调试技巧)

---

## 系统架构

Bioyond 仓库系统采用**三层架构**，实现从前端显示到后端 API 的完整映射：

```
┌─────────────────────────────────────────────────────────┐
│  前端显示层 (YB_warehouses.py)                          │
│  - warehouse_factory 自动生成库位网格                   │
│  - 生成库位名称：A01, B02, C03...                       │
│  - 存储在 WareHouse.sites 字典中                        │
└────────────────┬────────────────────────────────────────┘
                 │
                 ▼
┌─────────────────────────────────────────────────────────┐
│  Deck 布局层 (decks.py)                                 │
│  - 定义仓库在 Deck 上的物理位置                         │
│  - 组织多个仓库形成完整布局                             │
└────────────────┬────────────────────────────────────────┘
                 │
                 ▼
┌─────────────────────────────────────────────────────────┐
│  UUID 映射层 (config.py)                                │
│  - 将库位名称映射到 Bioyond 系统 UUID                   │
│  - 用于 API 调用时的物料入库操作                        │
└─────────────────────────────────────────────────────────┘
```

---

## 核心概念

### 仓库（Warehouse）

仓库是一个**三维网格**，用于存放物料。由以下参数定义：

- **num_items_x**: 列数（X 轴）
- **num_items_y**: 行数（Y 轴）  
- **num_items_z**: 层数（Z 轴）

例如：`5行×3列×1层` = 5×3×1 = 15个库位

### 库位（Site）

库位是仓库中的单个存储位置，由**字母行+数字列**命名：

- **字母行**：A, B, C, D, E, F...（对应 Y 轴）
- **数字列**：01, 02, 03, 04...（对应 X 轴或 Z 轴）

示例：`A01`, `B02`, `C03`

### 布局模式（Layout）

控制库位的排序和 Y 坐标计算：

| 模式 | 说明 | 生成顺序 | Y 坐标计算 | 显示效果 |
|------|------|----------|-----------|---------|
| `col-major` | 列优先（默认） | A01, B01, C01, A02... | `dy + (num_y - row - 1) * item_dy` | A 可能在下 |
| `row-major` | 行优先 | A01, A02, A03, B01... | `dy + row * item_dy` | **A 在上** ✓ |

**重要：** 使用 `row-major` 可以避免上下颠倒问题！

---

## 三层映射关系

### 示例：手动传递窗右（A01-E03）

#### 1️⃣ 前端显示层 - [`YB_warehouses.py`](YB_warehouses.py)

```python
def bioyond_warehouse_5x3x1(name: str, row_offset: int = 0) -> WareHouse:
    """创建 5行×3列×1层 仓库"""
    return warehouse_factory(
        name=name,
        num_items_x=3,      # 3列
        num_items_y=5,      # 5行
        num_items_z=1,      # 1层
        row_offset=row_offset,
        layout="row-major",
    )
```

**自动生成的库位：** A01, A02, A03, B01, B02, B03, ..., E01, E02, E03

#### 2️⃣ Deck 布局层 - [`decks.py`](decks.py)

```python
self.warehouses = {
    "手动传递窗右": bioyond_warehouse_5x3x1("手动传递窗右", row_offset=0),
}
self.warehouse_locations = {
    "手动传递窗右": Coordinate(4160.0, 877.0, 0.0),
}
```

**作用：** 
- 创建仓库实例
- 设置在 Deck 上的物理坐标

#### 3️⃣ UUID 映射层 - [`config.py`](../../devices/workstation/bioyond_studio/config.py)

```python
WAREHOUSE_MAPPING = {
    "手动传递窗右": {
        "uuid": "",
        "site_uuids": {
            "A01": "3a19deae-2c7a-36f5-5e41-02c5b66feaea",
            "A02": "3a19deae-2c7a-dc6d-c41e-ef285d946cfe",
            # ... 其他库位
        }
    }
}
```

**作用：**
- 用户拖拽物料到"手动传递窗右"的"A01"位置时
- 系统查找 `WAREHOUSE_MAPPING["手动传递窗右"]["site_uuids"]["A01"]`
- 获取 UUID `"3a19deae-2c7a-36f5-5e41-02c5b66feaea"`
- 调用 Bioyond API 将物料入库到该 UUID 位置

---

## 实际配置案例

### 案例：手动传递窗左/右的完整配置

本案例展示如何为"手动传递窗右"和"手动传递窗左"建立完整的三层映射。

#### 背景需求
- **手动传递窗右**: 需要 A01-E03（5行×3列=15个库位）
- **手动传递窗左**: 需要 F01-J03（5行×3列=15个库位）
- 这两个仓库共享同一个物理堆栈的 UUID（"手动堆栈"）

#### 实施步骤

**1️⃣ 修复前端布局** - [`YB_warehouses.py`](YB_warehouses.py)

```python
# 创建新的 5×3×1 仓库函数（之前是错误的 1×3×3）
def bioyond_warehouse_5x3x1(name: str, row_offset: int = 0) -> WareHouse:
    """创建5行×3列×1层仓库，支持行偏移生成不同字母行"""
    return warehouse_factory(
        name=name,
        num_items_x=3,      # 3列
        num_items_y=5,      # 5行  ← 修正
        num_items_z=1,      # 1层  ← 修正
        row_offset=row_offset,  # ← 支持 F-J 行
        layout="row-major",     # ← 避免上下颠倒
    )
```

**2️⃣ 更新 Deck 配置** - [`decks.py`](decks.py)

```python
from unilabos.resources.presets.bioyond.YB_warehouses import (
    bioyond_warehouse_5x3x1,  # 新增导入
)

class BIOYOND_YB_Deck(Deck):
    def setup(self) -> None:
        self.warehouses = {
            # 修改前: bioyond_warehouse_1x3x3 (错误尺寸)
            # 修改后: bioyond_warehouse_5x3x1 (正确尺寸)
            "手动传递窗右": bioyond_warehouse_5x3x1("手动传递窗右", row_offset=0),  # A01-E03
            "手动传递窗左": bioyond_warehouse_5x3x1("手动传递窗左", row_offset=5),  # F01-J03
        }
```

**3️⃣ 添加 UUID 映射** - [`config.py`](../../devices/workstation/bioyond_studio/config.py)

```python
WAREHOUSE_MAPPING = {
    # 保持原有的"手动堆栈"配置不变（A01-J03共30个库位）
    "手动堆栈": {
        "uuid": "",
        "site_uuids": {
            "A01": "3a19deae-2c7a-36f5-5e41-02c5b66feaea",
            # ... A02-E03 共15个
            "F01": "3a19deae-2c7a-d594-fd6a-0d20de3c7c4a",
            # ... F02-J03 共15个
        }
    },
    
    # [新增] 手动传递窗右 - 复用"手动堆栈"的 A01-E03 UUID
    "手动传递窗右": {
        "uuid": "",
        "site_uuids": {
            "A01": "3a19deae-2c7a-36f5-5e41-02c5b66feaea",  # ← 与手动堆栈A01相同
            "A02": "3a19deae-2c7a-dc6d-c41e-ef285d946cfe",
            "A03": "3a19deae-2c7a-5876-c454-6b7e224ca927",
            "B01": "3a19deae-2c7a-2426-6d71-e9de3cb158b1",
            "B02": "3a19deae-2c7a-79b0-5e44-efaafd1e4cf3",
            "B03": "3a19deae-2c7a-b9eb-f4e3-e308e0cf839a",
            "C01": "3a19deae-2c7a-32bc-768e-556647e292f3",
            "C02": "3a19deae-2c7a-e97a-8484-f5a4599447c4",
            "C03": "3a19deae-2c7a-3056-6504-10dc73fbc276",
            "D01": "3a19deae-2c7a-ffad-875e-8c4cda61d440",
            "D02": "3a19deae-2c7a-61be-601c-b6fb5610499a",
            "D03": "3a19deae-2c7a-c0f7-05a7-e3fe2491e560",
            "E01": "3a19deae-2c7a-a6f4-edd1-b436a7576363",
            "E02": "3a19deae-2c7a-4367-96dd-1ca2186f4910",
            "E03": "3a19deae-2c7a-b163-2219-23df15200311",
        }
    },
    
    # [新增] 手动传递窗左 - 复用"手动堆栈"的 F01-J03 UUID
    "手动传递窗左": {
        "uuid": "",
        "site_uuids": {
            "F01": "3a19deae-2c7a-d594-fd6a-0d20de3c7c4a",  # ← 与手动堆栈F01相同
            "F02": "3a19deae-2c7a-a194-ea63-8b342b8d8679",
            "F03": "3a19deae-2c7a-f7c4-12bd-425799425698",
            "G01": "3a19deae-2c7a-0b56-72f1-8ab86e53b955",
            "G02": "3a19deae-2c7a-204e-95ed-1f1950f28343",
            "G03": "3a19deae-2c7a-392b-62f1-4907c66343f8",
            "H01": "3a19deae-2c7a-5602-e876-d27aca4e3201",
            "H02": "3a19deae-2c7a-f15c-70e0-25b58a8c9702",
            "H03": "3a19deae-2c7a-780b-8965-2e1345f7e834",
            "I01": "3a19deae-2c7a-8849-e172-07de14ede928",
            "I02": "3a19deae-2c7a-4772-a37f-ff99270bafc0",
            "I03": "3a19deae-2c7a-cce7-6e4a-25ea4a2068c4",
            "J01": "3a19deae-2c7a-1848-de92-b5d5ed054cc6",
            "J02": "3a19deae-2c7a-1d45-b4f8-6f866530e205",
            "J03": "3a19deae-2c7a-f237-89d9-8fe19025dee9"
        }
    },
}
```

#### 关键要点

1. **UUID 可以复用**: 三个仓库（手动堆栈、手动传递窗右、手动传递窗左）可以共享相同的物理库位 UUID
2. **库位名称必须匹配**: 前端生成的库位名称（如 F01）必须与 config.py 中的键名完全一致
3. **row_offset 的妙用**: 
   - `row_offset=0` → 生成 A-E 行
   - `row_offset=5` → 生成 F-J 行（跳过前5个字母）

#### 验证结果

配置完成后，拖拽测试：

| 拖拽位置 | 前端库位 | 查找路径 | UUID | 结果 |
|---------|---------|---------|------|------|
| 手动传递窗右/A01 | A01 | `WAREHOUSE_MAPPING["手动传递窗右"]["site_uuids"]["A01"]` | `3a19...eaea` | ✅ 正确入库 |
| 手动传递窗左/F01 | F01 | `WAREHOUSE_MAPPING["手动传递窗左"]["site_uuids"]["F01"]` | `3a19...c4a` | ✅ 正确入库 |
| 手动堆栈/A01 | A01 | `WAREHOUSE_MAPPING["手动堆栈"]["site_uuids"]["A01"]` | `3a19...eaea` | ✅ 仍然正常 |


---

## warehouse_factory 详解

### 函数签名

```python
def warehouse_factory(
    name: str,
    num_items_x: int = 1,      # 列数
    num_items_y: int = 4,      # 行数
    num_items_z: int = 4,      # 层数
    dx: float = 137.0,         # X 起始偏移
    dy: float = 96.0,          # Y 起始偏移
    dz: float = 120.0,         # Z 起始偏移
    item_dx: float = 10.0,     # X 间距
    item_dy: float = 10.0,     # Y 间距
    item_dz: float = 10.0,     # Z 间距
    col_offset: int = 0,       # 列偏移（影响数字）
    row_offset: int = 0,       # 行偏移（影响字母）
    layout: str = "col-major", # 布局模式
) -> WareHouse:
```

### 参数说明

#### 尺寸参数
- **num_items_x, y, z**: 定义仓库的网格尺寸
- **注意**: 当 `num_items_z > 1` 时，Z 轴会被映射为数字列

#### 位置参数
- **dx, dy, dz**: 第一个库位的起始坐标
- **item_dx, dy, dz**: 库位之间的间距

#### 偏移参数
- **col_offset**: 列起始偏移，用于生成 A05-D08 等命名
  ```python
  col_offset=4  # 生成 A05, A06, A07, A08
  ```

- **row_offset**: 行起始偏移，用于生成 F01-J03 等命名
  ```python
  row_offset=5  # 生成 F01, F02, F03（跳过 A-E）
  ```

#### 布局参数
- **layout**: 
  - `"col-major"`: 列优先（默认），可能导致上下颠倒
  - `"row-major"`: 行优先，**推荐使用**，A 显示在上

### 库位生成逻辑

```python
# row-major 模式（推荐）
keys = [f"{LETTERS[j + row_offset]}{i + 1 + col_offset:02d}" 
        for j in range(num_y) 
        for i in range(num_x)]

# 示例：num_y=2, num_x=3, row_offset=0, col_offset=0
# 生成：A01, A02, A03, B01, B02, B03
```

### Y 坐标计算

```python
if layout == "row-major":
    # A 在上（Y 较小）
    y = dy + row * item_dy
else:
    # A 在下（Y 较大）- 不推荐
    y = dy + (num_items_y - row - 1) * item_dy
```

---

## 创建新仓库

### 步骤 1: 在 YB_warehouses.py 中创建函数

```python
def bioyond_warehouse_3x4x1(name: str) -> WareHouse:
    """创建 3行×4列×1层 仓库
    
    布局:
    A01 | A02 | A03 | A04
    B01 | B02 | B03 | B04
    C01 | C02 | C03 | C04
    """
    return warehouse_factory(
        name=name,
        num_items_x=4,      # 4列
        num_items_y=3,      # 3行
        num_items_z=1,      # 1层
        dx=10.0,
        dy=10.0,
        dz=10.0,
        item_dx=137.0,
        item_dy=120.0,
        item_dz=120.0,
        category="warehouse",
        layout="row-major",  # ⭐ 推荐使用
    )
```

### 步骤 2: 在 decks.py 中使用

```python
# 1. 导入函数
from unilabos.resources.presets.bioyond.YB_warehouses import (
    bioyond_warehouse_3x4x1,  # 新增
)

# 2. 在 setup() 中添加
self.warehouses = {
    "我的新仓库": bioyond_warehouse_3x4x1("我的新仓库"),
}
self.warehouse_locations = {
    "我的新仓库": Coordinate(100.0, 200.0, 0.0),
}
```

### 步骤 3: 在 config.py 中配置 UUID（可选）

```python
WAREHOUSE_MAPPING = {
    "我的新仓库": {
        "uuid": "",
        "site_uuids": {
            "A01": "从 Bioyond 系统获取的 UUID",
            "A02": "从 Bioyond 系统获取的 UUID",
            # ... 其他 11 个库位
        }
    }
}
```

**注意：** 如果不需要拖拽入库功能，可跳过此步骤。

---

## 常见问题

### Q1: 为什么库位显示上下颠倒（C 在上，A 在下）？

**原因：** 使用了默认的 `col-major` 布局。

**解决：** 在 `warehouse_factory` 中添加 `layout="row-major"`

```python
return warehouse_factory(
    ...
    layout="row-major",  # ← 添加这行
)
```

### Q2: 我需要 1×3×3 还是 3×3×1？

**判断方法：**
- **1×3×3**: 1列×3行×3**层**（垂直堆叠，有高度）
- **3×3×1**: 3行×3列×1**层**（平面网格）

**推荐：** 大多数情况使用 `X×Y×1`（平面网格）更直观。

### Q3: 如何生成 F01-J03 而非 A01-E03？

**方法：** 使用 `row_offset` 参数

```python
bioyond_warehouse_5x3x1("仓库名", row_offset=5)
# row_offset=5 跳过 A-E，从 F 开始
```

### Q4: 拖拽物料后找不到 UUID 怎么办？

**检查清单：**
1. `config.py` 中是否有该仓库的配置？
2. 仓库名称是否完全匹配？
3. 库位名称（如 A01）是否在 `site_uuids` 中？

**示例错误：**
```python
# decks.py
"手动传递窗右": bioyond_warehouse_5x3x1(...)

# config.py - ❌ 名称不匹配
"手动传递窗": { ... }  # 缺少"右"字
```

### Q5: 库位重叠怎么办？

**原因：** 间距（`item_dx/dy/dz`）太小。

**解决：** 增大间距参数

```python
item_dx=150.0,  # 增大 X 间距
item_dy=130.0,  # 增大 Y 间距
```

---

## 调试技巧

### 1. 查看生成的库位

```python
warehouse = bioyond_warehouse_5x3x1("测试仓库")
print(list(warehouse.sites.keys()))
# 输出：['A01', 'A02', 'A03', 'B01', 'B02', ...]
```

### 2. 检查库位坐标

```python
for name, site in warehouse.sites.items():
    print(f"{name}: {site.location}")
# 输出：
# A01: Coordinate(x=10.0, y=10.0, z=120.0)
# A02: Coordinate(x=147.0, y=10.0, z=120.0)
# ...
```

### 3. 验证 UUID 映射

```python
from unilabos.devices.workstation.bioyond_studio.config import WAREHOUSE_MAPPING

warehouse_name = "手动传递窗右"
location_code = "A01"

if warehouse_name in WAREHOUSE_MAPPING:
    uuid = WAREHOUSE_MAPPING[warehouse_name]["site_uuids"].get(location_code)
    print(f"{warehouse_name}/{location_code} → {uuid}")
else:
    print(f"❌ 未找到仓库: {warehouse_name}")
```

---

## 文件关系图

```
unilabos/
├── resources/
│   ├── warehouse.py              # warehouse_factory 核心实现
│   └── bioyond/
│       ├── YB_warehouses.py      # ⭐ 仓库函数定义
│       ├── decks.py              # ⭐ Deck 布局配置
│       └── README_WAREHOUSE.md   # 📖 本文档
└── devices/
    └── workstation/
        └── bioyond_studio/
            ├── config.py         # ⭐ UUID 映射配置
            └── bioyond_cell/
                └── bioyond_cell_workstation.py  # 业务逻辑
```

---

## 版本历史

- **v1.1** (2026-01-08): 补充实际配置案例
  - 添加"手动传递窗右"和"手动传递窗左"的完整配置示例
  - 展示 UUID 复用的实际应用
  - 说明三个仓库共享物理堆栈的配置方法
  
- **v1.0** (2026-01-07): 初始版本
  - 新增 `row_offset` 参数支持
  - 创建 `bioyond_warehouse_5x3x1` 和 `bioyond_warehouse_2x2x1`
  - 修复多个仓库的上下颠倒问题

---

## 相关资源

- [warehouse.py](../warehouse.py) - 核心工厂函数实现
- [YB_warehouses.py](YB_warehouses.py) - 所有仓库定义
- [decks.py](decks.py) - Deck 布局配置
- [config.py](../../devices/workstation/bioyond_studio/config.py) - UUID 映射

---

**维护者：** Uni-Lab-OS 开发团队  
**最后更新：** 2026-01-07
