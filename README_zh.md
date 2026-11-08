<div align="center">
  <img src="docs/logo.png" alt="Uni-Lab Logo" width="200"/>
</div>

# Uni-Lab-OS

[English](README.md) | **中文**

[![GitHub Stars](https://img.shields.io/github/stars/deepmodeling/Uni-Lab-OS.svg)](https://github.com/deepmodeling/Uni-Lab-OS/stargazers)
[![GitHub Forks](https://img.shields.io/github/forks/deepmodeling/Uni-Lab-OS.svg)](https://github.com/deepmodeling/Uni-Lab-OS/network/members)
[![GitHub Issues](https://img.shields.io/github/issues/deepmodeling/Uni-Lab-OS.svg)](https://github.com/deepmodeling/Uni-Lab-OS/issues)
[![GitHub License](https://img.shields.io/github/license/deepmodeling/Uni-Lab-OS.svg)](https://github.com/deepmodeling/Uni-Lab-OS/blob/main/LICENSE)

Uni-Lab-OS 是一个开源的实验室自动化平台，用统一方式接入实验设备，并描述设备、物料及其协同执行的实验工作流。

## 平台能力

- 为不同实验设备提供一致的接入接口。
- 编排设备动作与物料操作，执行实验工作流。
- 支持本地部署和分布式实验室部署。
- 设备集成通过外部设备包独立开发和许可。

## v1.0 串行 Task 版本

公开版 v1.0 按 FIFO（先进先出）顺序推进 Task。当前 Task 内的作业在依赖和资源允许时可以并行。设备接入、物料管理和工作流协作仍由平台负责。调度配置、旧参数迁移和验收细节见[串行 Task 发行说明](docs/scheduler/release.md)。

## 快速开始

环境要求：Python 3.11 或更高版本，以及 Rust 稳定版工具链。完整 ROS 2 运行环境所需依赖见[安装指南](docs/user_guide/installation.md)。

```bash
git clone --branch feat/schedule https://github.com/deepmodeling/Uni-Lab-OS.git
cd Uni-Lab-OS
python -m venv .venv
```

激活虚拟环境后，安装源码并构建原生调度库：

```powershell
.\.venv\Scripts\Activate.ps1
python -m pip install -e .
python native/scheduler-queue/build.py --destination unilabos/scheduling/queue_native
unilab --help
```

Linux 和 macOS 使用 `source .venv/bin/activate` 激活环境，其余 Python 命令相同。

创建并运行一个无硬件 mock 设备包和首个 Task，请按[外部设备包指南](docs/developer_guide/external_device_packages.md)操作。

可选：使用 MCP 命令时，安装 `python -m pip install -e ".[mcp]"`。

## 文档

- [文档索引](docs/index.md)
- [源码安装](docs/user_guide/installation.md)
- [外部设备包](docs/developer_guide/external_device_packages.md)
- [串行 Task 调度器](docs/scheduler/release.md)

公开调度器 workflow 已配置为在 Linux、macOS 和 Windows 上使用 Python 3.11 构建原生库并运行 `tests/scheduling`。

## 引用

如果在学术研究中使用 [Uni-Lab-OS](https://arxiv.org/abs/2512.21766)，请引用：

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

本仓库代码适用 GPL-3.0；详见 [LICENSE](LICENSE) 和 [NOTICE](NOTICE)。外部设备包独立发布，并各自声明许可证。

## 联系

- GitHub Issues: <https://github.com/deepmodeling/Uni-Lab-OS/issues>
