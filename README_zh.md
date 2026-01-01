<div align="center">
  <img src="docs/logo.png" alt="Uni-Lab Logo" width="200"/>
</div>

# Uni-Lab-OS

<!-- Language switcher -->

[English](README.md) | **中文**

[![GitHub Stars](https://img.shields.io/github/stars/dptech-corp/Uni-Lab-OS.svg)](https://github.com/dptech-corp/Uni-Lab-OS/stargazers)
[![GitHub Forks](https://img.shields.io/github/forks/dptech-corp/Uni-Lab-OS.svg)](https://github.com/dptech-corp/Uni-Lab-OS/network/members)
[![GitHub Issues](https://img.shields.io/github/issues/dptech-corp/Uni-Lab-OS.svg)](https://github.com/dptech-corp/Uni-Lab-OS/issues)
[![GitHub License](https://img.shields.io/github/license/dptech-corp/Uni-Lab-OS.svg)](https://github.com/dptech-corp/Uni-Lab-OS/blob/main/LICENSE)

Uni-Lab-OS 是一个用于实验室自动化的综合平台，旨在连接和控制各种实验设备，实现实验流程的自动化和标准化。

## 🏆 比赛

欢迎参加[有机化学合成智能实验大赛](https://bohrium.dp.tech/competitions/1451645258)，使用 Uni-Lab-OS 探索自动化合成！

## 核心特点

- 多设备集成管理
- 自动化实验流程
- 云端连接能力
- 灵活的配置系统
- 支持多种实验协议

## 文档

详细文档可在以下位置找到:

- [在线文档](https://xuwznln.github.io/Uni-Lab-OS-Doc/)

## 快速开始

1. 配置 Conda 环境

Uni-Lab-OS 建议使用 `mamba` 管理环境。根据您的操作系统选择适当的环境文件:

```bash
# 创建新环境
mamba create -n unilab python=3.11.11
mamba activate unilab
mamba install -n unilab uni-lab::unilabos -c robostack-staging -c conda-forge
```

2. 安装开发版 Uni-Lab-OS:

```bash
# 克隆仓库
git clone https://github.com/dptech-corp/Uni-Lab-OS.git
cd Uni-Lab-OS

# 安装 Uni-Lab-OS
pip install .
```

3. 启动 Uni-Lab 系统:

请见[文档-启动样例](https://xuwznln.github.io/Uni-Lab-OS-Doc/boot_examples/index.html)

## 消息格式

Uni-Lab-OS 使用预构建的 `unilabos_msgs` 进行系统通信。您可以在 [GitHub Releases](https://github.com/dptech-corp/Uni-Lab-OS/releases) 页面找到已构建的版本。

## 许可证

此项目采用 GPL-3.0 许可 - 详情请参阅 [LICENSE](LICENSE) 文件。

## 项目统计

### Stars 趋势

<a href="https://star-history.com/#dptech-corp/Uni-Lab-OS&Date">
  <img src="https://api.star-history.com/svg?repos=dptech-corp/Uni-Lab-OS&type=Date" alt="Star History Chart" width="600">
</a>

## 联系我们

- GitHub Issues: [https://github.com/dptech-corp/Uni-Lab-OS/issues](https://github.com/dptech-corp/Uni-Lab-OS/issues)
