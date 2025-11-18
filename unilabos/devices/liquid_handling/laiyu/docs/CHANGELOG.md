# 更新日志

本文档记录了 LaiYu_Liquid 模块的所有重要变更。

## [1.0.0] - 2024-01-XX

### 新增功能
- ✅ 完整的液体处理工作站集成
- ✅ RS485 通信协议支持
- ✅ SOPA 气动式移液器驱动
- ✅ XYZ 三轴步进电机控制
- ✅ PyLabRobot 兼容后端
- ✅ 标准化资源管理系统
- ✅ 96孔板、离心管架、枪头架支持
- ✅ RViz 可视化后端
- ✅ 完整的配置管理系统
- ✅ 抽象协议实现
- ✅ 生产级错误处理和日志记录

### 技术特性
- **硬件支持**: SOPA移液器 + XYZ三轴运动平台
- **通信协议**: RS485总线，波特率115200
- **坐标系统**: 机械坐标与工作坐标自动转换
- **安全机制**: 限位保护、紧急停止、错误恢复
- **兼容性**: 完全兼容 PyLabRobot 框架

### 文件结构
```
LaiYu_Liquid/
├── core/
│   └── LaiYu_Liquid.py      # 主模块文件
├── __init__.py              # 模块初始化
├── abstract_protocol.py    # 抽象协议
├── laiyu_liquid_res.py     # 资源管理
├── rviz_backend.py         # RViz后端
├── backend/                # 后端驱动
├── config/                 # 配置文件
├── controllers/           # 控制器
├── docs/                  # 技术文档
└── drivers/               # 底层驱动
```

### 已知问题
- 无

### 依赖要求
- Python 3.8+
- PyLabRobot
- pyserial
- asyncio

---

## 版本说明

### 版本号格式
采用语义化版本控制 (Semantic Versioning): `MAJOR.MINOR.PATCH`

- **MAJOR**: 不兼容的API变更
- **MINOR**: 向后兼容的功能新增
- **PATCH**: 向后兼容的问题修复

### 变更类型
- **新增功能**: 新的功能特性
- **变更**: 现有功能的变更
- **弃用**: 即将移除的功能
- **移除**: 已移除的功能
- **修复**: 问题修复
- **安全**: 安全相关的修复