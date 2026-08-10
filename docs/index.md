# Uni-Lab-OS 项目文档

Uni-Lab-OS 是一个开源的实验室自动化操作系统，提供统一的设备接口、工作流管理和分布式部署能力。

当前支持的运行时基线为 Python 3.12.13（`cp312`）、ROS 2 Jazzy 和 NumPy 2。
安装或从旧环境迁移前，请先阅读[运行时与 ABI 基线](user_guide/runtime_baseline.md)。

```{toctree}
:maxdepth: 3

intro.md
```

## 开发者指南

```{toctree}
:maxdepth: 2

developer_guide/http_api.md
developer_guide/networking_overview.md
developer_guide/add_device.md
developer_guide/add_action.md
developer_guide/add_registry.md
developer_guide/add_yaml.md
developer_guide/action_includes.md
```
