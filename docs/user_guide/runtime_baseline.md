# 运行时与 ABI 基线

本页记录当前 `dev` 分支及正式 Conda 包使用的统一运行时基线。安装、开发、CI
和设备包应以此处为准，避免混用不同 ROS 发行版或 Python ABI。

## 当前支持矩阵

| 组件 | 当前基线 | 说明 |
| --- | --- | --- |
| Python | 3.12.13 | 固定 `cp312` ABI；当前包要求 `>=3.12,<3.13` |
| ROS 2 | Jazzy | 使用 `robostack-jazzy` 频道 |
| ROS 2 distro mutex | `0.15.*` | 构建字符串应为 `jazzy_*` |
| NumPy | `>=2,<3` | 与 Jazzy/cp312 包保持同一 ABI 组合 |
| UniLabOS messages | `ros-jazzy-unilabos-msgs` | 从 `uni-lab` 频道安装 |

Python 3.11 与 ROS 2 Humble 不再是当前二进制包的支持组合。已有旧环境不要原地
混装或切换 `robostack-humble`/`robostack-jazzy` 频道；请新建环境，避免 Conda
求解出 ABI 不一致的 ROS、NumPy 或扩展模块。

## 推荐安装

```bash
mamba create -n unilab python=3.12.13
mamba activate unilab
mamba install uni-lab::unilabos -c uni-lab -c conda-forge -c robostack-jazzy
```

开发者使用环境包后再安装源码：

```bash
mamba install uni-lab::unilabos-env -c uni-lab -c conda-forge -c robostack-jazzy
uv pip install -r unilabos/utils/requirements.txt
pip install -e .
```

## 从旧环境迁移

1. 保留旧环境用于复现实验，不要在其中直接升级 ROS 发行版。
2. 按上面的命令创建新的 Python 3.12.13/Jazzy 环境。
3. 重新安装设备驱动及其 Python 依赖，不要复制旧环境的 `site-packages`。
4. 验证实际安装版本：

```bash
python -c "import sys, numpy; print(sys.version); print(numpy.__version__)"
conda list | grep -E "ros2-distro-mutex|ros-jazzy|unilabos"
```

Windows 可将最后一条命令改为：

```powershell
conda list | findstr /I "ros2-distro-mutex ros-jazzy unilabos"
```

## 历史兼容文件

仓库中的 `recipes/ros-humble-unilabos-msgs/` 仅用于维护历史 Humble 消息包，
不是当前安装入口。新的 Jazzy 消息包由 `recipes/msgs/` 构建；用户环境应安装
`ros-jazzy-unilabos-msgs`。
