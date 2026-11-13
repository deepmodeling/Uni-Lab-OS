# Uni-Lab-OS 项目文档

Uni-Lab-OS 是一个开源的实验室自动化操作系统，提供统一的设备接口、工作流管理和分布式部署能力。

当前支持的运行时基线为 Python 3.14.10（`cp314`）、ROS 2 Jazzy 和 NumPy 2。
安装或从旧环境迁移前，请先阅读[运行时与 ABI 基线](user_guide/runtime_baseline.md)。

```{toctree}
:maxdepth: 3

intro.md
```

## 开发者指南

```{toctree}
:maxdepth: 2

developer_guide/http_api.md
developer_guide/interfaces/index.md
developer_guide/api_v1_migration.md
developer_guide/api_v1_route_map.md
developer_guide/mcp.md
developer_guide/mcp_guide.md
developer_guide/mcp_demo_validation_20260913.md
developer_guide/readme_demo_tests.md
developer_guide/networking_overview.md
developer_guide/add_device.md
developer_guide/add_action.md
developer_guide/add_registry.md
developer_guide/add_yaml.md
developer_guide/action_includes.md
```

## 架构决策

```{toctree}
:maxdepth: 1

adr/README.md
adr/0001-compile-resource-occupancy-intervals.md
adr/0002-task-level-edit-boundary.md
adr/0003-task-scoped-pause.md
adr/0005-runtime-restart-fails-station-tasks.md
adr/0006-frontend-event-subscription.md
adr/0007-host-restart-reconciles-instead-of-failing.md
adr/0008-timeout-settles-attempt-but-keeps-locks.md
```
