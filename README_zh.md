# Uni-Lab-OS

[English](README.md) | **中文** | [开源版开发者指南](https://leap-lab.bohrium.com/docs/dev-guide)

[![GitHub Stars](https://img.shields.io/github/stars/deepmodeling/Uni-Lab-OS)](https://github.com/deepmodeling/Uni-Lab-OS/stargazers)
[![许可证 GPL-3.0](https://img.shields.io/badge/license-GPL--3.0-blue.svg)](LICENSE)

Uni-Lab-OS 是一个开源实验室自动化框架。它通过独立设备包接入实验仪器，在工站上执行工作流，并提供浏览器控制台来管理设备、工作流、任务和库存。开源版调度器一次运行一个工作流任务。

## 快速开始

按以下步骤从源码安装 Uni-Lab-OS，然后以 dry-run 模式启动示例设备包。更多说明见[安装指南](https://leap-lab.bohrium.com/docs/installation)。

### 1. 配置环境并安装源码

预留至少 1 GB 可用磁盘空间。运行 `git --version` 和 `mamba --version` 确认命令可用。如果没有 Mamba，请按平台安装 [Miniforge](https://github.com/conda-forge/miniforge/releases/latest)，然后重新打开终端。`unilabos-env` 提供 ROS 2 和基础 Python 依赖。

```bash
mamba create -n unilab python=3.11.14
mamba activate unilab
mamba install uni-lab::unilabos-env -c robostack-staging -c conda-forge
```

克隆仓库并进入仓库目录。已有源码时，直接进入当前仓库根目录并跳过克隆。

```bash
git clone https://github.com/deepmodeling/Uni-Lab-OS.git
cd Uni-Lab-OS
```

保持 `unilab` 环境处于激活状态，在仓库根目录运行安装脚本。脚本会下载并校验配套的浏览器控制台，然后安装 Uni-Lab-OS 及其 Python 依赖。

```bash
python scripts/install_public.py
```

### 2. 验证安装

使用新终端时，先运行 `conda activate unilab`。确认 Python 路径指向 `unilab` 环境，并检查 CLI 能否正常显示帮助。

```bash
python -c "import sys; print(sys.executable)"
python -c "import unilabos; print(unilabos.__version__)"
unilab --help
unilab workspace start --help
unilab package inspect --help
```

### 3. 生成并检查示例设备包

在准备存放设备包的目录中创建示例工作区，目标目录必须尚未存在。

```bash
unilab workspace init --output ./sample-lab
```

安装设备包及其开发依赖，运行测试并检查设备包。

```bash
cd ./sample-lab
export DEVICE_PACKAGE_ROOT="$(pwd)"
python -m pip install -e '.[dev]'
python -m pytest -q
unilab package inspect --path . --out dist/inspect
```

### 4. 以 dry-run 模式启动

首次启动使用示例设备包自带的 dry-run 启动图，验证工站启动、设备包发现和工作流加载，全程无需连接实验设备。

```bash
unilab workspace start \
  --workspace "$DEVICE_PACKAGE_ROOT" \
  --component all \
  --graph "$DEVICE_PACKAGE_ROOT/deployment/graphs/dry-run.json" \
  --runtime-mode dry-run \
  --json

unilab workspace status \
  --workspace "$DEVICE_PACKAGE_ROOT" \
  --json
```

工作区启动后，在浏览器中打开状态输出里 `components.backend.address` 对应的地址，即可进入控制台。

如果启动失败，查看后端日志。

```bash
unilab workspace logs \
  --workspace "$DEVICE_PACKAGE_ROOT" \
  --component backend \
  --json
```

体验结束后，停止工作区。

```bash
unilab workspace stop \
  --workspace "$DEVICE_PACKAGE_ROOT" \
  --component all \
  --json
```

如需将示例改成自己的设备包，请参阅[创建设备包](https://leap-lab.bohrium.com/docs/workspace)。

## 开发与贡献

[开源版开发者指南](https://leap-lab.bohrium.com/docs/dev-guide)涵盖设备包、工站配置、工作流编写和调试。

编写新驱动时，可以从 `unilab workspace init` 生成的设备包骨架开始，也可以参考下列独立设备包。

| 示例 | 演示内容 |
| --- | --- |
| [LabDeviceTemplate](https://github.com/Xuwznln/LabDeviceTemplate) | 独立设备包模板 |
| [LabDeviceLanDemo](https://github.com/Xuwznln/LabDeviceLanDemo) | 用 `@subscribe` 跨设备订阅，用 `call_device_action` 远程调用设备动作 |
| [LabDeviceWorkstationDemo](https://github.com/Xuwznln/LabDeviceWorkstationDemo) | 通过 `hardware_interface` 让多个设备共享串口或 Modbus 连接 |

欢迎通过 [GitHub Issues](https://github.com/deepmodeling/Uni-Lab-OS/issues)反馈问题或提出改进建议。

## 引用

如果您在学术研究中使用 [Uni-Lab-OS](https://arxiv.org/abs/2512.21766)，请引用：

```bibtex
@article{gao2025unilabos,
    title = {UniLabOS: An AI-Native Operating System for Autonomous Laboratories},
    doi = {10.48550/arXiv.2512.21766},
    publisher = {arXiv},
    author = {Gao, Jing and Chang, Junhan and Que, Haohui and Xiong, Yanfei and
              Zhang, Shixiang and Qi, Xianwei and Liu, Zhen and Wang, Jun-Jie and
              Ding, Qianjun and Li, Xinyu and Pan, Ziwei and Xie, Qiming and
              Yan, Zhuang and Yan, Junchi and Zhang, Linfeng},
    year = {2025}
}
```

## 许可证

本仓库的框架按 GPL-3.0 发布，独立安装的设备包遵循各自的许可证。详见 [LICENSE](LICENSE) 和 [NOTICE](NOTICE)。
