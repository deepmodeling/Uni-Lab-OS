# 串行 Task 调度

本分支交付公开的串行 Task 调度器，默认加载 `native/scheduler-queue` 构建的
Rust 动态库。同一 Task 内允许满足依赖和资源约束的并行作业；后续 Task 按 FIFO
等待当前 Task 结算。暂停保留队首，失败和取消须完成结算后才推进下一 Task。
平台继续负责设备调用、库存、持久开始许可、人工授权和结果恢复。

## 固定队列策略

默认引擎为 `queue`。公开 v1.0 的策略固定如下：

| 配置项 | 行为 |
| --- | --- |
| `queue_order` | `fifo` |
| `paused_task` | `hold` |
| `failed_task` | `advance_after_settlement` |
| `canceled_task` | `advance_after_settlement` |
| `manual_outside_scope` | `reject` |

## 旧时间规划配置迁移

旧时间规划设置不能选择新调度引擎。非空旧设置可能让启动以 `CONFIG_INVALID` 失败；升级前请清理旧环境变量。

| 旧变量 | v1.0 行为与迁移 |
| --- | --- |
| `UNILABOS_TIME_SCHEDULING_ENABLED` | 不能选择引擎。未设置或留空可启动；只有引擎为 `queue` 时，`false` / `0` 可保留但没有选择作用。其他非空值会导致启动失败。建议删除。 |
| `UNILABOS_TIME_SCHEDULING_SOLVE_SECONDS`、`UNILABOS_TIME_SCHEDULING_MAX_NODES` | 任一非空值都会导致启动失败。队列用户删除；插件用户仅在插件 schema 支持时迁移。 |
| `ULAB_SCHEDULER_AGING_INTERVAL_SECONDS`、`ULAB_ESTIMATE_MODE`、`ULAB_ESTIMATE_DEFAULT_S`、`UNILABOS_TIMING_RULES_FILE` | 任一非空值都会导致启动失败。队列用户删除；插件用户仅在插件 schema 支持时迁移。 |

队列模式使用 `UNILABOS_SCHEDULING_ENGINE=queue`。旧算法参数不能直接映射为队列配置。如果仍需第三方算法，应选择 `UNILABOS_SCHEDULING_ENGINE=plugin`、提供 `UNILABOS_SCHEDULING_MANIFEST`，并将 `UNILABOS_SCHEDULING_CONFIG` 写成该插件 schema 接受的 JSON 对象；不要把旧变量名原样塞进新配置。

## 构建与验证

需要 Python 3.11+ 和 Rust 稳定版工具链。

```bash
python -m pip install -e . pytest wheel packaging
cargo test --locked --manifest-path native/scheduler_shared/Cargo.toml
cargo test --locked --manifest-path native/scheduler-queue/Cargo.toml
python native/scheduler-queue/build.py --destination unilabos/scheduling/queue_native
UNILAB_TEST_CLEAN_INSTALL=1 python -m pytest -q tests/scheduling
```

PowerShell:

```powershell
$env:UNILAB_TEST_CLEAN_INSTALL = "1"
python -m pytest -q tests/scheduling
```

构建 wheel 时会自动生成当前平台的串行库：

```bash
python -m pip wheel . --no-deps -w dist/public
```

默认使用 `UNILABOS_SCHEDULING_ENGINE=queue`，统一插件协议与独立 worker 继续
保留。清单和 ABI 见 `unilabos/scheduling/contract/`。动态库不依赖 CPython。

## 发布范围

发布快照不包含 CP-SAT 的 Rust/Python 实现、商业动态库或商业构建依赖，
也不携带私有开发仓库的提交历史。设备驱动遵循其独立许可，通过外部驱动包提供。
公开 CI 为 `.github/workflows/public-scheduler.yml`，验证串行库的公开协议。

## v1.0 发布说明

本公开快照源于串行 Task 交付代码；Git 分支 `feat/schedule` 与标签 `v1.0` 指向同一根提交，
不继承其他仓库的提交历史。Python 发行包版本为 `1.0.0`，原生串行库版本为 `1.0.0`。

`tests/scheduling` 为公开版调度验收套件，覆盖真实原生库、协议边界、FIFO/暂停/结算、
幂等、持久会话恢复及离开源码目录后的安装运行。历史时间规划测试和商业引擎测试不随本版本发布。
旧时间规划兼容入口不包含算法实现，生产启动通过统一串行引擎装配。

## 发布验收（2026-10-30）

| 环境 | 公开版 Python 套件与术语检查 | Rust 单元测试 |
|---|---:|---:|
| macOS arm64 / Python 3.11 | 191 passed | 3 passed |
| Linux x86_64 / Python 3.11 / Kubernetes | 191 passed | 3 passed |

公开版套件覆盖协议与语义边界、实际串行库、FIFO 与暂停、结算后推进、幂等和恢复、
源码包构建 wheel、安装后实际 Task 执行，以及仅靠声明依赖的空环境安装。
原生实现与开发源快照逐文件核对一致。发行树不含商业引擎源文件或预编译动态库。
Windows 验证由公开 GitHub Actions 矩阵执行，本轮本地验收未覆盖 Windows。
