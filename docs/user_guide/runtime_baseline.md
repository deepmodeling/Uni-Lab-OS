# 运行时与 ABI 基线

当前源码基线为普通 CPython 3.14（`cp314`），保留 GIL；不是自由线程
`cp314t`。Jazzy 使用 mutex `0.20.*`，Humble 使用 `0.11.*`。升级时创建新环境，
并重新构建对应发行版的 `unilabos_msgs`，不要复用旧 cp312 原生扩展。

从 0.12.3 起，默认安装、运行、Conda 发布和离线包都采用无 ROS 的 HostLink。
旧版 0.12.2 的已发布包不覆盖；新版本发布前可从当前源码安装。

## 支持矩阵

| 组件 | 默认 HostLink | 显式 ROS2 Jazzy | 显式 ROS2 Humble |
| --- | --- | --- | --- |
| Python | >=3.14,<3.15（构建解释器 3.14.10） | 3.14.10（cp314） | 3.14.10（cp314） |
| NumPy | >=2,<3 | >=2,<3 | >=2,<3 |
| 入口包 | unilabos | unilabos-ros2，jazzy_1 | unilabos-ros2，humble_1 |
| 核心应用构建 | py314_0 | 同一核心包 | 同一核心包 |
| 环境依赖 | 默认包直接声明 | 默认依赖加 ROS 扩展 | 默认依赖加 ROS 扩展 |
| RoboStack channel | 不需要 | robostack-jazzy | robostack-humble |
| distro mutex | 不需要 | 0.20.* / jazzy_* | 0.11.* / humble_* |
| UniLabOS 消息包 | 不需要 | ros-jazzy-unilabos-msgs 0.12.1 | ros-humble-unilabos-msgs 0.12.1 |
| 启动选择 | 不传 backend 或 --backend hostlink | --backend ros2 | --backend ros2 |

Python 核心应用不再按 ROS 发行版重复打包。ROS2 包只是显式选装扩展，
不会因为安装了 ROS 就自动改变默认 backend。不要在同一环境混合两个 RoboStack channel。

## 能力边界

HostLink 支持普通 Python 驱动的 Action、Service、状态、JSON Topic、
Workstation/sub-device 初始化，以及 Host/Slave 跨进程和跨机器通信。
微后端、CLI 建图、工作流调度、物料同步、HTTP/WS、实时日志和 MCP 不需要 ROS。

以下能力仍应使用 ROS2 环境：

- MoveIt、RViz、TF/原生 ROS graph。
- 原生 ROS 摄像头、高频图像流、DDS QoS 等专用能力。
- 在模块顶层直接导入 rclpy 或 ROS 消息类的驱动。此类驱动不会自动变成纯 Python；
  若希望双 backend 运行，应隔离 ROS 专用部分或使用 backend 无关的数据模型。

硬件 SDK 也不是默认核心依赖：按设备包声明安装；内置驱动可选依赖可用
`python scripts/dev_install.py --extras full` 安装。这不等于所有硬件驱动在裸 Python 中都开箱即用。

## 安装与迁移

完整命令见[安装指南](installation.md)。默认不添加 RoboStack channel：

```bash
mamba create -n unilab --override-channels -c uni-lab -c conda-forge "unilabos>=0.12.3"
mamba activate unilab
unilab --disable-browser
```

需要 ROS2 时另建 Jazzy 或 Humble 环境，安装对应 `unilabos-ros2` 构建，
并在运行时显式传 `--backend ros2`。`unilabos-full` 仍是可选桌面/仿真套件。

保留旧环境以便复现实验，不要直接删除 ROS 包、复制旧 site-packages 或在
Humble/Jazzy 间原地切换。旧 Python 3.11、NumPy 1 消息扩展不能混入 cp314/NumPy 2 环境。

## Windows DLL 兼容

仅 ROS2 后端需要处理 ROS 原生 DLL。发行版优先从当前环境的
`ros2-distro-mutex` 元数据识别，不依赖激活脚本是否设置 `ROS_DISTRO`。
现有 DLL 兼容修补只针对实际加载失败，使用原子替换以免修改 Conda 的硬链接缓存。
默认 HostLink 不加载该 ROS 运行时。

## 构建与验证

- `.conda/base/`、`.conda/environment/`：唯一一套无 ROS 核心和依赖。
- `.conda/msgcenterpy/`、`.conda/pylabrobot/`：固定版本的消息转换和 PLR 分支依赖。
- `.conda/mcp/`：补齐既有 MCP 1.30 最低版本，不通过降低版本绕过缺包。
- `.conda/ros2*/`、`.conda/full*/`：显式 ROS2 扩展和桌面变体。
- `recipes/msgs*/`：独立的 ROS 消息发布；不由普通 HostLink 发布触发。
- `scripts/build_conda_release.py`：默认只构建无 ROS 包；ROS 需指定 `--ros-distros`。
- `scripts/pack_conda_release.py`：离线包只消费同一源码 SHA 的 Conda 产物。
- `recipes/wheels.toml`、`scripts/build_wheel_release.py`：复用 Conda 补丁，生成各平台完整 wheelhouse；
  在仓库外用配套安装入口验证后作为发行附件分发，不上传 PyPI。
- 默认 CI 使用未安装 ROS 的 Python 环境，验证安装、CLI、接口、HostLink 和七个
  完整 demo；保留独立 Jazzy/Humble 检查。

```bash
python -m pip check
python scripts/verify_installation.py --assert-no-ros
python -m pytest -q tests/app/test_no_ros_installation.py tests/e2e/test_readme_demos.py
```

ROS 环境验证用 `python scripts/verify_installation.py --backend ros2`。
无 ROS 检查会确认 rclpy/rosidl/ament 不存在，不会用自动安装掩盖缺失依赖。
