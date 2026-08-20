# UniLabOS 资源注册架构详解

> **目标受众**: 主要开发 `unilabos/registry/devices` 抽象层的开发者  
> **最后更新**: 2026-01-11  
> **维护者**: Uni-Lab-OS 开发团队

本文档详细说明 UniLabOS 资源注册系统的架构、资源的完整生命周期，以及如何实现动态物料位置追踪。

---

## 📚 目录

- [核心概念](#核心概念)
- [三层架构详解](#三层架构详解)
- [资源注册机制](#资源注册机制)
- [物料生命周期管理](#物料生命周期管理)
- [动态物料位置追踪](#动态物料位置追踪)
- [实战案例](#实战案例)
- [常见问题排查](#常见问题排查)

---

## 核心概念

### 1. Resources vs Registry

UniLabOS 采用**声明式注册**模式，将资源的**定义**（Python）与**注册信息**（YAML）分离：

```
┌──────────────────────────────────────────────────────────┐
│          unilabos/resources (Python 实现)                 │
│  - 定义资源的物理属性、行为和创建逻辑                       │
│  - 例如: 瓶子的尺寸、容量、材质                            │
├──────────────────────────────────────────────────────────┤
│       unilabos/registry/resources (YAML 注册表)           │
│  - 声明哪些资源可以被前端使用                              │
│  - 定义资源的分类、图标、初始化参数                         │
└──────────────────────────────────────────────────────────┘
```

**为什么要分离？**

1. **解耦**: Python 代码可以定义无限多的资源类，但只有在 YAML 中注册的才能被前端识别
2. **灵活性**: 无需修改 Python 代码，只需修改 YAML 就能添加/移除可用资源
3. **可扩展性**: 第三方开发者可以通过 YAML 注册自己的资源，无需修改核心代码

---

## 三层架构详解

UniLabOS 资源系统采用**三层架构**，实现从前端UI到底层硬件的完整映射：

### 架构图

```
┌─────────────────────────────────────────────────────┐
│  第1层: YAML 注册表 (registry/resources)             │
│  - 告诉系统"哪些资源可用"                             │
│  - 前端通过此层获取可用资源列表                        │
│  - 文件: YB_bottle.yaml, YB_bottle_carriers.yaml    │
├─────────────────────────────────────────────────────┤
│  第2层: Python 实现 (resources/presets/bioyond)      │
│  - 定义资源的具体属性和行为                           │
│  - 创建资源实例的工厂函数                             │
│  - 文件: YB_bottles.py, YB_bottle_carriers.py       │
├─────────────────────────────────────────────────────┤
│  第3层: Hardware/API 集成 (devices/workstation)      │
│  - 连接 Bioyond 系统 API                            │
│  - 同步物料位置和状态                                │
│  - 文件: station.py, bioyond_rpc.py, config.py     │
└─────────────────────────────────────────────────────┘
```

### 第1层: YAML 注册表

#### YB_bottle.yaml - 单个瓶子注册

```yaml
YB_5ml_fenyeping:
  category:
  - yb3              # 系统分类
  - YB_bottle        # 资源类型
  class:
    module: unilabos.resources.presets.bioyond.YB_bottles:YB_5ml_fenyeping  # Python 函数路径
    type: pylabrobot  # 框架类型
  description: YB_5ml_fenyeping  # 前端显示名称
  handles: []
  icon: ''           # 图标路径
  init_param_schema: {}  # 初始化参数 schema
  registry_type: resource
  version: 1.0.0
```

**作用:**
- 前端通过读取此文件知道有一个叫 "YB_5ml_fenyeping" 的资源
- 用户拖拽时，系统会调用 `YB_bottles:YB_5ml_fenyeping()` 创建实例

#### YB_bottle_carriers.yaml - 载架（容器）注册

```yaml
YB_5ml_fenyepingban:
  category:
  - yb3
  - YB_bottle_carriers
  class:
    module: unilabos.resources.presets.bioyond.YB_bottle_carriers:YB_5ml_fenyepingban
    type: pylabrobot
  description: YB_5ml_fenyepingban  # 5ml分液瓶板
```

**作用:**  
- 载架是容器，里面可以放多个瓶子
- 例如: `YB_5ml_fenyepingban` 是一个 4x2 布局的板，可以放 8 个 5ml 瓶子

### 第2层: Python 实现

#### YB_bottles.py - 瓶子工厂函数

```python
def YB_5ml_fenyeping(
    name: str,
    diameter: float = 20.0,    # 直径 (mm)
    height: float = 50.0,      # 高度 (mm)
    max_volume: float = 5000.0,  # 最大容量 (μL)
    barcode: str = None,
) -> Bottle:
    \"\"\"创建5ml分液瓶\"\"\"
    return Bottle(
        name=name,
        diameter=diameter,
        height=height,
        max_volume=max_volume,
        barcode=barcode,
        model="YB_5ml_fenyeping",  # ⭐ 与 YAML 中的名称对应
    )
```

**关键点:**
- 函数名 `YB_5ml_fenyeping` 必须与 YAML 中的 `module` 路径末尾一致
- 返回一个 `Bottle` 对象（PyLabRobot 资源类型）
- `model` 字段用于在 Bioyond 系统中识别资源类型

**详细文档请参考完整版 README**

---

## 相关文件索引

### 核心文件

| 文件 | 功能 | 路径 |
|------|------|------|
| `YB_bottle.yaml` | 瓶子注册表 | `unilabos/registry/resources/bioyond/` |
| `YB_bottle_carriers.yaml` | 载架注册表 | `unilabos/registry/resources/bioyond/` |
| `deck.yaml` | Deck注册表 | `unilabos/registry/resources/bioyond/` |
| `YB_bottles.py` | 瓶子实现 | `unilabos/resources/presets/bioyond/` |
| `YB_bottle_carriers.py` | 载架实现 | `unilabos/resources/presets/bioyond/` |
| `YB_warehouses.py` | 仓库实现 | `unilabos/resources/presets/bioyond/` |
| `decks.py` | Deck布局 | `unilabos/resources/presets/bioyond/` |
| `station.py` | 物料同步 | `unilabos/devices/workstation/bioyond_studio/` |
| `config.py` | UUID映射 | `unilabos/devices/workstation/bioyond_studio/` |

### 仓库相关文档

- [README_WAREHOUSE.md](../../../resources/presets/bioyond/README_WAREHOUSE.md) - 仓库系统开发指南

---

**维护者:** Uni-Lab-OS 开发团队  
**最后更新:** 2026-01-11
