# Uni-Lab-OS 安装指南

从 **0.12.4** 起，默认安装和运行均使用 **HostLink，不需要 ROS**。Python 要求为
`>=3.14,<3.15`；Windows、Linux、macOS 可使用相同的 Python 入口。

本文的 Conda 命令适用于 0.12.4 及之后的发布。如果 channel 尚未提供该版本，请先使用
下方源码安装；旧的 0.12.2 Conda 包仍包含 ROS，升级源码不会自动卸载旧环境中的 ROS。

## 配套 wheel 发行包（推荐，无须 Conda）

下载对应平台的 `unilabos-<版本>-wheelhouse-<平台>.zip` 并解压。需要自行安装 Python 3.14；
Windows x64、Linux x64、macOS Intel/Apple Silicon 分别提供独立产物，不能混用。
在解压目录执行：

```bash
python install_wheel_release.py
# 可选：指定一个尚不存在的新环境目录
python install_wheel_release.py --prefix /path/to/new-environment
```

入口会校验 SHA256、创建 `.venv`、离线安装全部默认依赖并验证 CLI、微后端和 Opentrons 96 孔板。
已有环境目录不会覆盖；安装不需要 ROS、Git、Conda、编译器或访问包源。执行结束会打印启动命令。
这个包不包含 Python 解释器，需要连同解释器分发时使用下文 Conda-Pack 包。

PLR fork 和 Opentrons NumPy 2 修正版只作为配套文件提供，**不上传官方 PyPI**。
发布到公开 GitHub Release 的附件仍然可公开下载；不等于私有源。
未发布正式 Release 时，可从 [配套 wheel 构建](https://github.com/deepmodeling/Uni-Lab-OS/actions/workflows/wheel-release.yml)
的成功 artifact 获取；`local-test` 文件仅供本地验证，不是正式发布版本。

已有独立 Python 3.14 环境时，也可由 pip 自动解析依赖：

```bash
# 完整离线、精确版本与文件哈希：推荐保留安装入口的默认行为
python -m pip install --no-index --find-links ./wheelhouse --require-hashes -r requirements.lock
# 只指定主包，自动安装所需依赖（不锁定清单中全部间接依赖版本）
python -m pip install --no-index --find-links ./wheelhouse "unilabos==0.12.4"
```

只复制 `unilabos.whl`、不提供配套目录，或直接对公共源执行 `pip install unilabos`，
都不能让 pip 发现未托管在公共源的修正版。不要用上游同名包、`--no-deps` 或降级 NumPy 来绕过它。

## Python 安装

准备一个独立的 Python 3.14 环境，避免复用旧 ROS 环境：

```bash
python -m venv .venv
# Linux / macOS
source .venv/bin/activate
# Windows PowerShell 改用：.venv\Scripts\Activate.ps1

git clone --branch dev https://github.com/deepmodeling/Uni-Lab-OS.git
cd Uni-Lab-OS
python scripts/dev_install.py
unilab --help
python -m unilabos.app.main --disable_browser --port 8002
```

首次运行按提示选择工作目录和配置；不带 `-g` 时以空设备图启动，可从微前端安装设备包。
安装时已声明微后端、调度、物料、HostLink 和 CLI 所需的 Python 依赖，不再需要额外安装
`unilabos_msgs`、执行 `colcon build` 或手动补一个 requirements 清单。

源码安装入口会从固定源码构建配套 wheel，再可编辑安装 UniLabOS。此构建过程需要 Git、
Python 包源和 GitHub；如果已有配套包，用 `python scripts/dev_install.py --wheelhouse <解压目录>/wheelhouse`
可以跳过这一步。构建产物不会上传 PyPI，源码下载以 SHA256 校验。

用户安装 wheel 并提供 `--find-links` 时，也会自动安装其 `Requires-Dist` 依赖，不需要源码仓库。
依赖清单随 wheel 安装在 `unilabos/utils/requirements.txt`，启动检查不读取仓库根目录。
若使用了 `pip install --no-deps`，可显式运行
`python -m unilabos.utils.environment_check --wheelhouse <解压目录>/wheelhouse` 补齐当前包声明的依赖；
仅检查使用 `--no-auto-install`。也可通过 `UNILABOS_WHEELHOUSE` 显式设置该目录。
这只是误用 `--no-deps` 后的恢复入口，正常安装不依靠首次启动时再下载依赖；
恢复入口允许访问普通包源，严格离线安装应使用前面的安装入口/锁定清单。

两个修正版是默认物料依赖：`pylabrobot==0.2.2+unilabos.6285d662effa.ot10` 固定到已验证的 fork 提交，
`opentrons-shared-data==10.1.0+unilabos.np2.1` 与 Conda 共用 NumPy 2 补丁。
普通上游 `opentrons-shared-data` 仍限制 NumPy 1，不能替换修正版。
PLR 自身也声明了这个 Opentrons 数据包前置依赖；从完整配套 `wheelhouse` 只安装 PLR 时，
pip 仍会自动带齐修正版和 NumPy 2，无须手动安排安装顺序，也无须安装完整的 `opentrons` SDK。

安装只分三档，不再拆分独立的 docs/dev/test/drivers 选项：

| 档位 | Python 安装 | 内容 |
| --- | --- | --- |
| 默认 | `python scripts/dev_install.py` | HostLink、微后端、工作流、物料，默认无 ROS |
| ros2 | `python scripts/dev_install.py --extras ros2` | 默认依赖及 ROS Python 辅助依赖；原生 ROS 另装 |
| full | `python scripts/dev_install.py --extras full` | 全部 Python 依赖：内置驱动 SDK、文档、测试、开发工具及 ROS Python 辅助依赖 |

已有配套 wheel 时追加 `--wheelhouse <解压目录>/wheelhouse`。非源码安装使用
`python -m pip install --find-links <解压目录>/wheelhouse "unilabos[full]==0.12.4"`。
默认离线 wheel 包只锁定默认依赖；扩展档位还需联网取得公共依赖，不能宣称整套 full 离线可用。
pip 的 `[ros2]` / `[full]` **不安装原生 ROS、DDS、RViz 或消息 typesupport**，
需要这些组件时使用下方 Conda 对应档位。专有硬件 SDK、系统驱动和许可证仍按设备包要求准备。
`--test_mode` 是硬件模拟开关，与“无 ROS”不是同一概念；普通 Python 驱动可通过 HostLink 操作真实硬件。

## Conda 安装

默认只启用 Uni-Lab 和 conda-forge，不添加 RoboStack channel：

```bash
mamba create -n unilab --override-channels -c uni-lab -c conda-forge "unilabos>=0.12.4"
conda activate unilab
unilab --help
unilab --disable_browser --port 8002
```

| 包 | 用途 | 是否安装 ROS |
| --- | --- | --- |
| `unilabos` | 默认完整的 HostLink / 微后端运行程序 | 否 |
| `unilabos-ros2` | 显式选装 Jazzy 或 Humble 的设备通信扩展 | 是 |
| `unilabos-full` | 完整运行、文档、测试、开发工具及 ROS Desktop / MoveIt | 是 |

默认包的 build 为 `py314_0`。不再发布 `unilabos-env` 中间包；这与用户自行创建的
名为 `unilab` 的 Conda 环境不是同一概念，不会删除或更改已有环境。
ROS 扩展使用另一个包名，避免 Conda 在同名默认包的 ROS / 非 ROS 变体之间自行选择。

只安装 `unilabos` 即会直接带上 PLR fork、`msgcenterpy` 及 NumPy 2 修复版
`opentrons-shared-data=10.1.0+unilabos.np2.1=py314_np2_1`，不要求用户另装环境中间包
或 ROS 扩展。发行自检会实际创建 Opentrons 96 孔板，并检查 NumPy 2 积分接口，不能只验证 import。

## 运行 Host 与 Slave

```bash
# 默认 HostLink；省略 --backend 也一样
unilab -g host.json --machine_name lab-host --port 8002 --hostlink_port 7302

# 另一台机器（或同机另一个进程）
unilab --is_slave -g slave.json --machine_name lab-slave --host_node_ip 192.168.1.10 --hostlink_port 7302

# 单独的调度/物料权威，不启动设备
unilab --role backend --port 8081
```

默认启动会分为微后端权威和 Host 子进程；前端只连接管理端 HTTP 端口，
Slave 连接 HostLink TCP 端口。同机时 Host IP 可用 `127.0.0.1`。
设备归属可以提前登记，但必须完成 `post_init/setup/initialize` 后才会进入可调度设备快照。

无 ROS 安装支持普通设备驱动、Workstation/sub-device、动作与 Service、物料创建及 transfer、
工作流、异常干预、逐步执行、实时日志和 MCP。原生 ROS graph/TF、MoveIt、RViz、
依赖 ROS 消息或 DDS/QoS 的驱动、ROS 高频图像流需要下方的 ROS 扩展。
HTTP/FFmpeg 等非 ROS 图像方案不因此被禁用，但仍需安装自己的依赖。

## 显式选装 ROS2

分别创建环境，不能混用 Jazzy 和 Humble 的 channel：

```bash
# Jazzy
mamba create -n unilab-jazzy --override-channels -c uni-lab -c conda-forge -c robostack-jazzy "unilabos-ros2=0.12.4=jazzy_1"
conda activate unilab-jazzy
unilab --backend ros2 -g graph.json

# Humble：在另一个环境执行
mamba create -n unilab-humble --override-channels -c uni-lab -c conda-forge -c robostack-humble "unilabos-ros2=0.12.4=humble_1"
```

ROS 扩展保持 Python 3.14.10 / NumPy 2 与消息包 0.12.1 的 ABI 基线；
安装扩展后也必须使用 `--backend ros2`，不会偷偷切换默认 backend。
需要 RViz/MoveIt 时再显式选择匹配的 `unilabos-full=0.12.4=jazzy_1` 或 `humble_1`，
这些大型组件的可用性还取决于平台和 RoboStack 包。

自定义 ROS Action/消息参见[添加动作](../developer_guide/add_action.md)。
纯 Python/HostLink 驱动不需要为了增加动作而编译 ROS 消息。

## 完整开发与文档环境

需要连同 ROS 运行与文档一起开发时，创建完整环境后可编辑安装源码：

```bash
mamba create -n unilab-full --override-channels -c uni-lab -c conda-forge -c robostack-jazzy "unilabos-full=0.12.4=jazzy_1"
conda activate unilab-full
python scripts/dev_install.py --extras full
python -m pytest tests/
python -m sphinx -b html -d .local/docs/doctrees docs .local/docs/html
```

`full` 统一包含文档、测试、开发工具和内置驱动的通用 Python SDK，不再拆分额外档位。
Conda 发行同时提供 PDF 字体和各平台的 OPC-UA 依赖；Windows GUI 自动化依赖仅在
Windows 安装，因为对应的 Agilent HPLC 驱动依赖 Windows 应用，并不支持其他操作系统。

正式文档构建设置 `UNILABOS_DOCS_REQUIRE_ROS=1`，缺少 `control_msgs` / `nav2_msgs`
动作定义时直接失败，不静默生成缺页文档。无 ROS 环境只做普通页面预览；若已有另一套 ROS
环境，可设置 `UNILABOS_DOCS_ROS_PREFIX` 指向它，仅复用 `share` 中的消息定义，不混用 Python 包。
生成的动作参考和 HTML 均保留在构建输出目录，不改写 `docs/developer_guide` 或提交生成物。

## 离线包

在 [Conda-Pack 构建](https://github.com/deepmodeling/Uni-Lab-OS/actions/workflows/conda-pack-build.yml)
选择成功的 `unilab-pack-hostlink-<平台>-<源码SHA>` 产物，解压 GitHub artifact ZIP 后，
Windows 运行 `install_unilab.bat`，Linux/macOS 运行 `bash install_unilab.sh`。

包内包含 Python 环境、安装/验证工具和对应源码归档；默认不包含 ROS。
已存在的环境不会被覆盖；需要新名字时用 `install_unilab.bat unilab-hostlink`
或 `bash install_unilab.sh unilab-hostlink`。
自动打包使用上游同一次构建的 Conda 文件，不覆盖安装另一个源码版本，不追踪最新 ROS 消息。
手动构建会先按指定 SHA 构建默认 Conda 包，再安装验证和打包。

## 验证与排错

在源码目录执行（离线包中省略 `scripts/` 前缀）：

```bash
python -m pip check
python scripts/verify_installation.py --assert-no-ros
unilab --help
```

ROS 环境改为 `python scripts/verify_installation.py --backend ros2`，不加 `--assert-no-ros`。
验证默认不会自动安装缺失依赖；缺失或版本不满足时返回非零退出码。

- 找不到 `unilab`：先激活安装时使用的环境，或执行 `python -m unilabos.app.main`。
- 提示缺少 `rclpy`：普通驱动使用默认 HostLink；确实需要 ROS 才安装匹配扩展，不用 pip 猜装 ROS 原生库。
- 想从旧环境去掉 ROS：保留原环境，另建干净环境；不要删除数据库或复制旧的 site-packages。
- 驱动缺少专用 SDK：按设备包的声明安装，不代表默认微后端需要 ROS。
- 网络下载失败：目标机器优先使用配套离线包；源码构建仍需下载固定上游归档，不能改成任意分支。

运行参数见[启动指南](launch.md)，接口与设计见[开发者指南](../developer_guide/interfaces/index.md)。
