<div align="center">
  <img src="docs/logo.png" alt="Uni-Lab Logo" width="200"/>
</div>

# Uni-Lab-OS

<!-- Language switcher -->

[English](README.md) | **中文**

[![GitHub Stars](https://img.shields.io/github/stars/dptech-corp/Uni-Lab-OS.svg)](https://github.com/deepmodeling/Uni-Lab-OS/stargazers)
[![GitHub Forks](https://img.shields.io/github/forks/dptech-corp/Uni-Lab-OS.svg)](https://github.com/deepmodeling/Uni-Lab-OS/network/members)
[![GitHub Issues](https://img.shields.io/github/issues/dptech-corp/Uni-Lab-OS.svg)](https://github.com/deepmodeling/Uni-Lab-OS/issues)
[![GitHub License](https://img.shields.io/github/license/dptech-corp/Uni-Lab-OS.svg)](https://github.com/deepmodeling/Uni-Lab-OS/blob/main/LICENSE)

Uni-Lab-OS 是一个用于实验室自动化的综合平台，旨在连接和控制各种实验设备，实现实验流程的自动化和标准化。

## 核心特点

- 多设备集成管理
- 自动化实验流程
- 云端连接能力
- 灵活的配置系统
- 支持多种实验协议

## 文档

详细文档可在以下位置找到:

- [在线文档](https://deepmodeling.github.io/Uni-Lab-OS/)

## 快速开始

### 1. 配置 Conda 环境

Uni-Lab-OS 建议使用 `mamba` 管理环境。根据您的需求选择合适的安装包：

| 安装包 | 适用场景 | 包含内容 |
|--------|----------|----------|
| `unilabos` | **推荐大多数用户** | 完整安装包，开箱即用 |
| `unilabos-env` | 开发者（可编辑安装） | 仅环境依赖，通过 pip 安装 unilabos |
| `unilabos-full` | 仿真/可视化 | unilabos + ROS2 桌面版 + Gazebo + MoveIt |

```bash
# 创建新环境
mamba create -n unilab python=3.11.14
mamba activate unilab

# 方案 A：标准安装（推荐大多数用户）
mamba install uni-lab::unilabos -c robostack-staging -c conda-forge

# 方案 B：开发者环境（可编辑模式开发）
mamba install uni-lab::unilabos-env -c robostack-staging -c conda-forge
# 然后安装 unilabos 和依赖：
git clone https://github.com/deepmodeling/Uni-Lab-OS.git && cd Uni-Lab-OS
pip install -e .
uv pip install -r unilabos/utils/requirements.txt

# 方案 C：完整安装（仿真/可视化）
mamba install uni-lab::unilabos-full -c robostack-staging -c conda-forge
```

**如何选择？**
- **unilabos**：标准安装，适用于生产部署和日常使用（推荐）
- **unilabos-env**：开发者使用，支持 `pip install -e .` 可编辑模式，可修改源代码
- **unilabos-full**：需要仿真（Gazebo）、可视化（rviz2）或 Jupyter Notebook

### 2. 克隆仓库（可选，供开发者使用）

```bash
# 克隆仓库（仅开发或查看示例时需要）
git clone https://github.com/deepmodeling/Uni-Lab-OS.git
cd Uni-Lab-OS
```

3. 启动 Uni-Lab 系统

请见[文档-启动样例](https://deepmodeling.github.io/Uni-Lab-OS/boot_examples/index.html)

4. 最佳实践

请见[最佳实践指南](https://deepmodeling.github.io/Uni-Lab-OS/user_guide/best_practice.html)

## 消息格式

Uni-Lab-OS 使用预构建的 `unilabos_msgs` 进行系统通信。您可以在 [GitHub Releases](https://github.com/deepmodeling/Uni-Lab-OS/releases) 页面找到已构建的版本。

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

本项目采用双许可证结构：

- **主框架**：GPL-3.0 - 详见 [LICENSE](LICENSE)
- **设备驱动** (`unilabos/devices/`)：深势科技专有许可证

完整许可证说明请参阅 [NOTICE](NOTICE)。

## 项目统计

### Stars 趋势

<a href="https://star-history.com/#dptech-corp/Uni-Lab-OS&Date">
  <img src="https://api.star-history.com/svg?repos=dptech-corp/Uni-Lab-OS&type=Date" alt="Star History Chart" width="600">
</a>

## 联系我们

- GitHub Issues: [https://github.com/deepmodeling/Uni-Lab-OS/issues](https://github.com/deepmodeling/Uni-Lab-OS/issues)
