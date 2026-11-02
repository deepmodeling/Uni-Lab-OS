<div align="center">
  <img src="docs/logo.png" alt="Uni-Lab Logo" width="200"/>
</div>

# Uni-Lab-OS

<!-- Language switcher -->

[English](README.md) | **中文**

[![GitHub Stars](https://img.shields.io/github/stars/deepmodeling/Uni-Lab-OS.svg)](https://github.com/deepmodeling/Uni-Lab-OS/stargazers)
[![GitHub Forks](https://img.shields.io/github/forks/deepmodeling/Uni-Lab-OS.svg)](https://github.com/deepmodeling/Uni-Lab-OS/network/members)
[![GitHub Issues](https://img.shields.io/github/issues/deepmodeling/Uni-Lab-OS.svg)](https://github.com/deepmodeling/Uni-Lab-OS/issues)
[![GitHub License](https://img.shields.io/github/license/deepmodeling/Uni-Lab-OS.svg)](https://github.com/deepmodeling/Uni-Lab-OS/blob/v1.0/LICENSE)

**v1.0** 是 Uni-Lab-OS 的公开串行 Task 版本。标签 [`v1.0`](https://github.com/deepmodeling/Uni-Lab-OS/tree/v1.0) 与分支 `feat/schedule` 指向同一根提交。Python 包与原生串行库的版本都是 **1.0.0**。

Uni-Lab-OS 用于连接实验设备并运行实验工作流。本快照的调度器按 FIFO 推进 Task。同一 Task 内，依赖和资源都允许时作业可以并行。暂停保留队首。失败或取消的 Task 必须完成结算，下一 Task 才会前进。设备调用、库存、持久开始许可、人工授权和结果恢复仍由平台负责。

构建、配置和已记录的验收见[串行 Task 说明](docs/scheduler/release.md)。

## 本标签包含什么

| 在本快照中 | 不在本快照中 |
| --- | --- |
| 开源 FIFO 调度器 `unilab.queue`，以及由 `native/scheduler-queue` 构建的 Rust 库 | CP-SAT 源码、商业动态库和商业构建依赖 |
| 共用调度协议、插件宿主和独立 worker | 父提交历史。本提交不继承其他仓库的历史 |
| 驱动该调度器的宿主、工作流、物料与 Edge 代码 | 树内设备驱动。驱动以独立包发布，并使用各自的许可证 |
| 公开验收套件 [`tests/scheduling`](tests/scheduling) | 旧时间规划算法。相关设置不能用来选择引擎 |

默认 `UNILABOS_SCHEDULING_ENGINE=queue`。启动时从 `unilabos/scheduling/queue_native/plugin.json` 加载原生库。运行时不会调用编译器，也不会退回 Python 调度算法。`plugin` 通过 `UNILABOS_SCHEDULING_MANIFEST` 加载其他引擎。

队列配置是固定的：

| 键 | 值 |
| --- | --- |
| `queue_order` | `fifo` |
| `paused_task` | `hold` |
| `failed_task` | `advance_after_settlement` |
| `canceled_task` | `advance_after_settlement` |
| `manual_outside_scope` | `reject` |

公开 CI 为 [`.github/workflows/public-scheduler.yml`](.github/workflows/public-scheduler.yml)。它在 Linux、macOS 和 Windows 上使用 Python 3.11 构建真实动态库，并运行 `tests/scheduling`。

## 环境

- Python 3.11 或更高版本
- Rust 稳定版，用于从本树编译串行库

## 安装

在仓库根目录执行：

```bash
python -m pip install -e . pytest wheel packaging
python native/scheduler-queue/build.py --destination unilabos/scheduling/queue_native
```

可编辑安装会提供命令 `unilab`、`unilab-supervisor` 和 `unilab-mcp`。启动队列引擎前，用第二条命令构建串行库。构建 wheel 时会为当前操作系统和 CPU 编译该库并打进 wheel：

```bash
python -m pip wheel . --no-deps -w dist/public
```

wheel 标记为 `py3-none-<platform>`。动态库不链接 CPython。若库构建在其他目录，把 `UNILABOS_SCHEDULING_MANIFEST` 指到该目录的 `plugin.json`。构建目标目录必须尚不存在。

协议清单和 ABI 见 [`unilabos/scheduling/contract/`](unilabos/scheduling/contract/)。库的说明见 [`native/scheduler-queue/README.md`](native/scheduler-queue/README.md)。

## 检查构建

```bash
cargo test --locked --manifest-path native/scheduler_shared/Cargo.toml
cargo test --locked --manifest-path native/scheduler-queue/Cargo.toml
UNILAB_TEST_CLEAN_INSTALL=1 python -m pytest -q tests/scheduling
```

`tests/scheduling` 使用真实原生库，覆盖协议边界、FIFO、暂停、结算后推进、幂等、持久会话恢复，以及从已安装 wheel 执行 Task。验收记录写在[发行说明](docs/scheduler/release.md)。

## 文档

[docs/index.md](docs/index.md) 是文档集的索引。本页不罗列其中的页面。串行 Task 说明 [docs/scheduler/release.md](docs/scheduler/release.md) 不在这个索引里。

## 目录

| 路径 | 内容 |
| --- | --- |
| [`native/scheduler-queue/`](native/scheduler-queue/) | FIFO 动态库 |
| [`native/scheduler_shared/`](native/scheduler_shared/) | 共用的 Rust 协议与语义检查 |
| [`unilabos/scheduling/`](unilabos/scheduling/) | Python 宿主、队列适配和插件 worker |
| [`tests/scheduling/`](tests/scheduling/) | 公开版调度验收套件 |

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

本快照中的代码为 GPL-3.0。见 [LICENSE](LICENSE) 和 [NOTICE](NOTICE)。

`setup.py` 不发行 `unilabos.devices`。驱动包使用各自的许可证。该目录存在时，`unilabos/devices/` 适用深势科技专有许可证。

## 联系

- GitHub Issues: <https://github.com/deepmodeling/Uni-Lab-OS/issues>
