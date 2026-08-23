# 运行时与 ABI 基线

本页记录当前 `dev` 分支及 Conda 包支持的运行时基线。Python 与 NumPy ABI 在
两个 ROS 2 发行版之间保持一致，但每个 Conda 环境只能选择一个 ROS 发行版。

## 当前支持矩阵

| 组件 | Jazzy | Humble |
| --- | --- | --- |
| Python | 3.12.13（`cp312`） | 3.12.13（`cp312`） |
| NumPy | `>=2,<3` | `>=2,<3` |
| RoboStack channel | `robostack-jazzy` | `robostack-humble` |
| ROS 2 distro mutex | `0.15.*` / `jazzy_*` | `0.9.*` / `humble_*` |
| UniLabOS messages | `ros-jazzy-unilabos-msgs=0.12.0` | `ros-humble-unilabos-msgs=0.12.0` |
| Conda build string | `jazzy_1` | `humble_1` |

Jazzy 是默认和推荐发行版；Humble 作为兼容发行版运行相同的 Python 3.12、NumPy 2
与 microbackend v2 源码。公开通信 backend 只有 `hostlink` 和 `ros2`：HostLink
内部的本地 Python 执行引擎不依赖具体 ROS 发行版，`ros2` backend 会使用当前
环境安装的 Humble 或 Jazzy。

## Backend 选择边界

ROS 发行版兼容性与设备 backend 是两个不同维度。`hostlink` 不启动 rclpy/DDS，
适用于普通 Python 设备驱动，并支持 Action、Service、状态、低中频 JSON Topic、
Workstation 和 sub-device 的初始化与通信。驱动仅借用 ROS message Python 类作为
数据结构时，也可以使用 HostLink，但环境中仍需能导入相应的 message 包。

HostLink 与 ROS2 的通用设备接口保持对齐，但并非 100% 等价。MoveIt、RViz、原生
ROS graph/TF、ROS2 专用高频图像流，以及依赖 QoS、零拷贝或复杂消息图的设备应选择
`ros2` backend。详细启动参数和选择示例见 {doc}`launch`，组网能力边界见
{doc}`../developer_guide/networking_overview`。

不要在同一环境中同时配置 `robostack-humble` 与 `robostack-jazzy`。两个 channel
提供互斥的 `ros2-distro-mutex`，混装或原地切换会导致 ROS 原生扩展 ABI 不一致。

## 推荐安装

### ROS 2 Jazzy（默认）

```bash
mamba create -n unilab-jazzy python=3.12.13
mamba activate unilab-jazzy
mamba install uni-lab::unilabos -c uni-lab -c conda-forge -c robostack-jazzy
```

### ROS 2 Humble（兼容）

```bash
mamba create -n unilab-humble python=3.12.13
mamba activate unilab-humble
mamba install uni-lab::unilabos -c uni-lab -c conda-forge -c robostack-humble
```

开发者可将 `unilabos` 换成 `unilabos-env`，再安装当前源码：

```bash
uv pip install -r unilabos/utils/requirements.txt
pip install -e .
```

包名在两个发行版中保持一致，Conda 根据启用的 RoboStack channel 选择
`jazzy_1` 或 `humble_1` build。自定义消息包名称仍包含发行版，不能交叉安装。

## Windows DLL 加载兼容

UniLabOS 会优先从当前环境的 `ros2-distro-mutex` 元数据识别 ROS 发行版，
不依赖可能尚未由激活脚本设置的 `ROS_DISTRO`：

- Humble 与 Jazzy 仅在实际出现 `DLL load failed` 时，对 rclpy/rpyutils 的加载
  入口应用同一套兼容补丁，并提示重新启动进程。
- 补丁使用原子文件替换，避免修改与环境文件硬链接的 Conda package cache。

## 从旧环境迁移

旧 Humble 环境常见组合是 Python 3.11、NumPy 1、mutex 0.7。它与当前 Humble
兼容线的 cp312/NumPy 2/mutex 0.9 也不兼容，必须新建环境：

1. 保留旧环境用于复现实验，不要原地升级 Python、NumPy 或 distro mutex。
2. 按上面的 Jazzy 或 Humble 命令创建新的 Python 3.12.13 环境。
3. 重新安装设备驱动及其 Python 依赖，不要复制旧环境的 `site-packages`。
4. 验证实际安装版本：

```bash
python -c "import os, sys, numpy; print(os.environ.get('ROS_DISTRO')); print(sys.version); print(numpy.__version__)"
conda list | grep -E "ros2-distro-mutex|ros-(jazzy|humble)|unilabos"
```

Windows 可将最后一条命令改为：

```powershell
conda list | findstr /I "ros2-distro-mutex ros-jazzy ros-humble unilabos"
```

## 构建与验证入口

- `recipes/msgs/`：Jazzy/cp312/NumPy 2 消息包。
- `recipes/msgs-humble/`：Humble/cp312/NumPy 2 消息包。
- `.conda/environment*/`、`.conda/base*/`、`.conda/full*/`：两个发行版的
  `unilabos-env`、`unilabos` 和 `unilabos-full` build 变体。
- CI 在 Humble 与 Jazzy 中分别从源码构建 `unilabos_msgs` 并运行同一套
  backend/ROS 合同测试。
