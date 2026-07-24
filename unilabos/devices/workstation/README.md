# 工作站抽象基类物料系统架构说明

## 设计理念

基于用户需求"请你帮我系统思考一下，工作站抽象基类的物料系统基类该如何构建"，我们最终确定了一个**PyLabRobot Deck为中心**的简化架构。

### 核心原则

1. **PyLabRobot为物料管理核心**：使用PyLabRobot的Deck系统作为物料管理的基础，利用其成熟的Resource体系
2. **Graphio转换函数集成**：使用graphio中的`resource_ulab_to_plr`等转换函数实现UniLab与PLR格式的无缝转换
3. **关注点分离**：基类专注核心物料系统，HTTP服务等功能在子类中实现
4. **外部系统集成模式**：通过ResourceSynchronizer抽象类提供外部物料系统对接模式

## 架构组成

### 1. WorkstationBase（基类）
**文件**: `workstation_base.py`

**核心功能**：
- 使用deck_config和children通过`resource_ulab_to_plr`转换为PLR物料self.deck
- 基础的资源查找和管理功能
- 抽象的工作流执行接口
- ResourceSynchronizer集成点

**关键代码**：
```python
def _initialize_material_system(self, deck_config: Dict[str, Any], children_config: Dict[str, Any] = None):
    """初始化基于PLR的物料系统"""
    # 合并deck_config和children
    complete_config = self._merge_deck_and_children_config(deck_config, children_config)
    
    # 使用graphio转换函数转换为PLR资源
    self.deck = resource_ulab_to_plr(complete_config)
```

### 2. ResourceSynchronizer（外部系统集成抽象类）
**定义在**: `workstation_base.py`

**设计目的**：
- 提供外部物料系统（如Bioyond、LIMS等）集成的标准接口
- 双向同步：从外部系统同步到本地deck，以及将本地变更同步到外部系统
- 处理外部系统的变更通知

**核心方法**：
```python
async def sync_from_external(self) -> bool:
    """从外部系统同步物料到本地deck"""
    
async def sync_to_external(self, plr_resource) -> bool:
    """将本地物料同步到外部系统"""
    
async def handle_external_change(self, change_info: Dict[str, Any]) -> bool:
    """处理外部系统的变更通知"""
```

### 3. WorkstationWithHTTP（子类示例）
**文件**: `workstation_with_http_example.py`

**扩展功能**：
- HTTP报送接收服务集成
- 具体工作流实现（液体转移、板洗等）
- Bioyond物料系统同步器示例
- 外部报送处理方法

## 技术栈

### 核心依赖
- **PyLabRobot**: 物料资源管理核心（Deck, Resource, Coordinate）
- **GraphIO转换函数**: UniLab ↔ PLR格式转换
  - `resource_ulab_to_plr`: UniLab格式转PLR格式
  - `resource_plr_to_ulab`: PLR格式转UniLab格式
  - `convert_resources_to_type`: 通用资源类型转换
- **ROS2**: 基础设备节点通信（BaseROS2DeviceNode）

### 可选依赖
- **HTTP服务**: 仅在需要外部报送接收的子类中使用
- **外部系统API**: 根据具体集成需求添加

## 使用示例

### 1. 简单工作站（仅PLR物料系统）

```python
from unilabos.devices.workstation.workstation_base import WorkstationBase

# Deck配置
deck_config = {
    "size_x": 1200.0,
    "size_y": 800.0,
    "size_z": 100.0
}

# 子资源配置
children_config = {
    "source_plate": {
        "name": "source_plate",
        "type": "plate",
        "position": {"x": 100, "y": 100, "z": 10},
        "config": {"size_x": 127.8, "size_y": 85.5, "size_z": 14.4}
    }
}

# 创建工作站
workstation = WorkstationBase(
    device_id="simple_workstation",
    deck_config=deck_config,
    children_config=children_config
)

# 查找资源
plate = workstation.find_resource_by_name("source_plate")
```

### 2. 带HTTP服务的工作站

```python
from unilabos.devices.workstation.workstation_with_http_example import WorkstationWithHTTP

# HTTP服务配置
http_service_config = {
    "enabled": True,
    "host": "127.0.0.1",
    "port": 8081
}

# 创建带HTTP服务的工作站
workstation = WorkstationWithHTTP(
    device_id="http_workstation",
    deck_config=deck_config,
    children_config=children_config,
    http_service_config=http_service_config
)

# 执行工作流
success = workstation.execute_workflow("liquid_transfer", {
    "volume": 100.0,
    "source_wells": ["A1", "A2"],
    "dest_wells": ["B1", "B2"]
})
```

### 3. 外部系统集成

```python
class BioyondResourceSynchronizer(ResourceSynchronizer):
    """Bioyond系统同步器"""
    
    async def sync_from_external(self) -> bool:
        # 从Bioyond API获取物料
        external_materials = await self._fetch_bioyong_materials()
        
        # 转换并添加到本地deck
        for material in external_materials:
            await self._add_material_to_deck(material)
        
        return True
```

## 设计优势

### 1. **简洁性**
- 基类只专注核心物料管理，没有冗余功能
- 使用成熟的PyLabRobot作为物料管理基础

### 2. **可扩展性**
- 通过子类添加HTTP服务、特定工作流等功能
- ResourceSynchronizer模式支持任意外部系统集成

### 3. **标准化**
- PLR Deck提供标准的资源管理接口
- Graphio转换函数确保格式一致性

### 4. **灵活性**
- 可选择性使用HTTP服务和外部系统集成
- 支持不同类型的工作站需求

## 发展历程

1. **初始设计**: 复杂的统一物料系统，包含HTTP服务和多种功能
2. **PyLabRobot集成**: 引入PLR Deck管理，但保留了ResourceTracker复杂性
3. **Graphio转换**: 使用graphio转换函数简化初始化
4. **最终简化**: 专注核心PLR物料系统，HTTP服务移至子类

这个架构体现了"用PyLabRobot Deck来管理物料会更好；但是要做好和外部物料系统的对接"的设计理念，以及"现在我只需要在工作站创建的时候，整体使用deck_config和children，一起通过resource_ulab_to_plr转换为plr物料self.deck即可"的简化要求。
