<div align="center">
  <img src="docs/logo.png" alt="Uni-Lab Logo" width="200"/>
</div>

# Uni-Lab-OS

<!-- Language switcher -->

**English** | [中文](README_zh.md)

[![GitHub Stars](https://img.shields.io/github/stars/dptech-corp/Uni-Lab-OS.svg)](https://github.com/deepmodeling/Uni-Lab-OS/stargazers)
[![GitHub Forks](https://img.shields.io/github/forks/dptech-corp/Uni-Lab-OS.svg)](https://github.com/deepmodeling/Uni-Lab-OS/network/members)
[![GitHub Issues](https://img.shields.io/github/issues/dptech-corp/Uni-Lab-OS.svg)](https://github.com/deepmodeling/Uni-Lab-OS/issues)
[![GitHub License](https://img.shields.io/github/license/dptech-corp/Uni-Lab-OS.svg)](https://github.com/deepmodeling/Uni-Lab-OS/blob/main/LICENSE)

Uni-Lab-OS is a platform for laboratory automation, designed to connect and control various experimental equipment, enabling automation and standardization of experimental workflows.

## Key Features

- Multi-device integration management
- Automated experimental workflows
- Cloud connectivity capabilities
- Flexible configuration system
- Support for multiple experimental protocols

## Documentation

Detailed documentation can be found at:

- [Online Documentation](https://deepmodeling.github.io/Uni-Lab-OS/)

## Supported Runtime

The current binary and development baseline is **Python 3.12.13 (`cp312`) + ROS 2
Jazzy + NumPy 2**, using `robostack-jazzy::ros2-distro-mutex 0.15.*`. Python 3.11
and ROS 2 Humble environments are legacy combinations and should not be upgraded in
place or mixed with the Jazzy channels. See the
[runtime and ABI baseline](docs/user_guide/runtime_baseline.md) for migration and
verification instructions.

## Quick Start

### 1. Setup Conda Environment

Uni-Lab-OS recommends using `mamba` for environment management. Choose the package that fits your needs:

| Package | Use Case | Contents |
|---------|----------|----------|
| `unilabos` | **Recommended for most users** | Complete package, ready to use |
| `unilabos-env` | Developers (editable install) | Environment only, install unilabos via pip |
| `unilabos-full` | Simulation/Visualization | unilabos + ROS2 Desktop + Gazebo + MoveIt |

```bash
# Create new environment
mamba create -n unilab python=3.12.13
mamba activate unilab

# Option A: Standard installation (recommended for most users)
mamba install uni-lab::unilabos -c uni-lab -c conda-forge -c robostack-jazzy

# Option B: For developers (editable mode development)
mamba install uni-lab::unilabos-env -c uni-lab -c conda-forge -c robostack-jazzy
# Then install unilabos and dependencies:
git clone https://github.com/deepmodeling/Uni-Lab-OS.git && cd Uni-Lab-OS
pip install -e .
uv pip install -r unilabos/utils/requirements.txt

# Option C: Full installation (simulation/visualization)
mamba install uni-lab::unilabos-full -c uni-lab -c conda-forge -c robostack-jazzy
```

**When to use which?**
- **unilabos**: Standard installation for production deployment and general usage (recommended)
- **unilabos-env**: For developers who need `pip install -e .` editable mode, modify source code
- **unilabos-full**: For simulation (Gazebo), visualization (rviz2), and Jupyter notebooks

### 2. Clone Repository (Optional, for developers)

```bash
# Clone the repository (only needed for development or examples)
git clone https://github.com/deepmodeling/Uni-Lab-OS.git
cd Uni-Lab-OS
```

3. Start Uni-Lab System

Please refer to [Documentation - Boot Examples](https://deepmodeling.github.io/Uni-Lab-OS/boot_examples/index.html)

4. Best Practice

See [Best Practice Guide](https://deepmodeling.github.io/Uni-Lab-OS/user_guide/best_practice.html)

## Reference Driver Implementations

Two runnable example device packages are maintained as standalone GitHub repositories (generated
from [LabDeviceTemplate](https://github.com/Xuwznln/LabDeviceTemplate)). Clone either one, load it
with `--devices <pkg> --external_devices_only`, and read it when writing your own drivers:

| Example repository | Demonstrates |
|--------------------|--------------|
| [LabDeviceLanDemo](https://github.com/Xuwznln/LabDeviceLanDemo) | Cross-device `@subscribe` + remote `call_device_action` LAN closed loop (hub/sub as two processes) |
| [LabDeviceWorkstationDemo](https://github.com/Xuwznln/LabDeviceWorkstationDemo) | `hardware_interface` proxy — multiple sub-devices share one communication endpoint: shared serial (default IO method names) and Modbus `extra_info` (per-device `slave_id` injection) |

Each repository README ships a step-by-step launch tutorial with verified output. For the underlying
communication-sharing mechanism see [Best Practice Guide §11.5](https://deepmodeling.github.io/Uni-Lab-OS/user_guide/best_practice.html);
to write a new driver from scratch see [Add Device](https://deepmodeling.github.io/Uni-Lab-OS/developer_guide/add_device.html).

## Message Format

Uni-Lab-OS uses pre-built `unilabos_msgs` for system communication. You can find the built versions on the [GitHub Releases](https://github.com/deepmodeling/Uni-Lab-OS/releases) page.

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

This project uses a dual licensing structure:

- **Main Framework**: GPL-3.0 - see [LICENSE](LICENSE)
- **Device Drivers** (`unilabos/devices/`): DP Technology Proprietary License

See [NOTICE](NOTICE) for complete licensing details.

## Project Statistics

### Stars Trend

<a href="https://star-history.com/#dptech-corp/Uni-Lab-OS&Date">
  <img src="https://api.star-history.com/svg?repos=dptech-corp/Uni-Lab-OS&type=Date" alt="Star History Chart" width="600">
</a>

## Contact Us

- GitHub Issues: [https://github.com/deepmodeling/Uni-Lab-OS/issues](https://github.com/deepmodeling/Uni-Lab-OS/issues)
