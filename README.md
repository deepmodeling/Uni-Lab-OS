<div align="center">
  <img src="docs/logo.png" alt="Uni-Lab Logo" width="200"/>
</div>

# Uni-Lab-OS

**English** | [中文](README_zh.md)

[![GitHub Stars](https://img.shields.io/github/stars/deepmodeling/Uni-Lab-OS.svg)](https://github.com/deepmodeling/Uni-Lab-OS/stargazers)
[![GitHub Forks](https://img.shields.io/github/forks/deepmodeling/Uni-Lab-OS.svg)](https://github.com/deepmodeling/Uni-Lab-OS/network/members)
[![GitHub Issues](https://img.shields.io/github/issues/deepmodeling/Uni-Lab-OS.svg)](https://github.com/deepmodeling/Uni-Lab-OS/issues)
[![GitHub License](https://img.shields.io/github/license/deepmodeling/Uni-Lab-OS.svg)](https://github.com/deepmodeling/Uni-Lab-OS/blob/main/LICENSE)

Uni-Lab-OS is an open platform for laboratory automation. It connects experimental equipment and provides a common place to describe devices, materials, and the workflows that coordinate them.

## What Uni-Lab-OS provides

- A consistent interface for integrating different laboratory devices.
- Workflow execution across device actions and material handling.
- Local and distributed deployment options for laboratory systems.
- External device packages so integrations can be developed and licensed independently.

## v1.0 serial Task edition

The public v1.0 edition advances Tasks in first-in, first-out (FIFO) order. Jobs within the active Task can run in parallel when their dependencies and resources allow. Device integration, materials, and workflow coordination remain platform responsibilities. See the [serial Task release note](docs/scheduler/release.md) for scheduler configuration, migration, and verification details.

## Quick Start

Requirements: Python 3.11 or newer and a stable Rust toolchain. For the full ROS 2 runtime, see the [installation guide](docs/user_guide/installation.md) for ROS dependencies.

```bash
git clone --branch feat/schedule https://github.com/deepmodeling/Uni-Lab-OS.git
cd Uni-Lab-OS
python -m venv .venv
```

Activate the environment, then install the source and build the native scheduler library:

```powershell
.\.venv\Scripts\Activate.ps1
python -m pip install -e .
python native/scheduler-queue/build.py --destination unilabos/scheduling/queue_native
unilab --help
```

On Linux and macOS, activate with `source .venv/bin/activate` and run the same Python commands.

To create and run a no-hardware mock device package and its first Task, follow the [external device package guide](docs/developer_guide/external_device_packages.md).

Optional: install the MCP command with `python -m pip install -e ".[mcp]"`.

## Documentation

- [Documentation index](docs/index.md)
- [Source installation](docs/user_guide/installation.md)
- [External device packages](docs/developer_guide/external_device_packages.md)
- [Serial Task scheduler](docs/scheduler/release.md)

The public scheduler workflow is configured to build the native library and run `tests/scheduling` on Linux, macOS, and Windows with Python 3.11.

## Citation

If you use [Uni-Lab-OS](https://arxiv.org/abs/2512.21766) in academic research, please cite:

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

## License

Code in this repository is GPL-3.0; see [LICENSE](LICENSE) and [NOTICE](NOTICE). External device packages are distributed separately and carry their own licenses.

## Contact

- GitHub Issues: <https://github.com/deepmodeling/Uni-Lab-OS/issues>
