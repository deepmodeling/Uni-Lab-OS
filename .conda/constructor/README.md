# Uni-Lab-OS Constructor 安装器

该目录使用 [conda constructor](https://conda.github.io/constructor/) 生成包含
Python、Conda、ROS 2 运行时和 Uni-Lab-OS 的离线安装器。默认安装生产包
`unilabos`；需要完整桌面、MoveIt、仿真和 Notebook 工具时可显式选择
`unilabos-full`。

## 启动器私有 Runtime

相邻 `lab-os-installer` 使用精简环境：Constructor 仅交付 `unilabos-env`（Python、ROS 等基础依赖），OS 业务代码和依赖通过当前源码构建的 pip wheel 及依赖锁安装。Windows 固定 OpenBLAS，避免解算成 MKL。WebView2 离线安装器由 Installer 单独内置。

Windows 启动器请使用 Constructor 3.17.3 生成安装器、`lockfile.base.txt` 和 `info.json`，将后者配置为 Installer 的 `constructorInfo`。路径审计要求其中的安装器 SHA-256、包 URL/MD5 清单及最长相对路径统计与实际输入匹配；缺少元数据的旧安装器会在开发构建时被拒绝。通用生产包的构建步骤不受此启动器要求影响。

启动器按未开启系统长路径支持的基线构建：支持的最终安装目录最多 26 个 UTF-16 字符，文件及目录的绝对路径预算分别为 259/247，Constructor 最长相对路径（含解压缓存）不得超过 209；超过预算须在开发阶段精简包内路径或依赖。不要把开启最终用户的系统开关作为修复方法。

在 Windows PowerShell 中，先按下文准备 Constructor 构建工具，再从 OS 仓库执行：

```powershell
$env:UNILABOS_DESKTOP_RUNTIME = '1'
$env:UNILABOS_INSTALLER_PACKAGE = 'unilabos-env'
$env:UNILABOS_INSTALLER_VERSION = '0.11.4'
$env:CONSTRUCTOR_CONDA_EXE = "$env:CONDA_PREFIX/standalone_conda/conda.exe"
constructor .conda/constructor --platform win-64 `
  --conda-exe $env:CONSTRUCTOR_CONDA_EXE `
  --output-dir ../lab-os-installer/runtime-cache/windows-x64
```

`UNILABOS_DESKTOP_RUNTIME=1` 自动禁用私有环境中的 conda CLI，加入 pip、NumPy 1.26.4 和 Windows OpenBLAS 约束，默认选择 `unilabos-env`。本地构建环境包时还可通过 `UNILABOS_INSTALLER_CHANNEL` 与 `UNILABOS_INSTALLER_BUILD` 精确选择该产物。退出当前构建终端或清除这些环境变量后再构建通用生产安装器。

将生成的 `.exe` 和 `lockfile.base.txt` 配入 Installer 的 `runtimeInstaller`、`condaLock`；Constructor 不是开发 Runtime 目录，不能用复制环境代替安装。切换锁文件时请使用新输出目录，后续通过 Installer 根目录 `start.ps1` 构建、复用和封装。构建与封装均拒绝完整 `unilabos` Conda 包和 MKL。

## 平台映射

Constructor 的 `--platform` 必须使用 Conda subdir，而不能只写操作系统族名：

| 输入名 | 实际 Conda subdir | 原生构建系统 | 输出 |
| --- | --- | --- | --- |
| `linux`、`linux-64` | `linux-64` | Linux x86_64 | `.sh` |
| `osx`、`osx-64` | `osx-64` | macOS Intel | `.sh` |
| `osx-arm64` | `osx-arm64` | macOS Apple Silicon | `.sh` |
| `win-64` | `win-64` | Windows x86_64 | `.exe` |

`linux` 和 `osx` 是 GitHub Actions 手动输入接受的便捷别名，不是额外的
Conda subdir。Constructor 只能在目标原生系统上生成可发布安装器，因此 CI 使用
四种原生 runner。`--render` 可跨平台检查 selector；`--dry-run` 和正式构建都应在
目标原生系统执行，以便获得正确的 `__osx`、`__glibc` 等虚拟包。

## 本机构建

先安装固定版本的构建工具。Linux 和 macOS 使用 micromamba；Windows 必须使用
Constructor 支持的 conda-standalone：

```bash
# Linux / macOS
conda create -n constructor-build -c conda-forge constructor=3.16.1 micromamba=2.8.1
conda activate constructor-build
export CONSTRUCTOR_CONDA_EXE="$(command -v micromamba)"

# Windows（在 Miniforge Bash 中）
conda create -n constructor-build -c conda-forge constructor=3.16.1 conda-standalone=26.3.2.post1
conda activate constructor-build
export CONSTRUCTOR_CONDA_EXE="$CONDA_PREFIX/standalone_conda/conda.exe"
```

Constructor 仍然生成 Conda Runtime；上述执行器仅用于离线安装阶段。Unix 使用
micromamba 避开 Conda channel 中连字符/下划线过渡元包的重复规范问题；Constructor
3.16.1 不支持用 micromamba 生成 Windows 安装器，因此 Windows 固定到已验证可处理同一
离线依赖集的 conda-standalone 26.3.2.post1。安装完成后的 Runtime 仍包含正式的
`conda` 命令。

从仓库根目录构建当前平台（版本默认与当前配置中的 `0.11.4` 一致）：

```bash
export UNILABOS_INSTALLER_VERSION=0.11.4
export UNILABOS_INSTALLER_PACKAGE=unilabos
constructor .conda/constructor \
  --platform linux-64 \
  --conda-exe "$CONSTRUCTOR_CONDA_EXE" \
  --output-dir dist/constructor
```

只验证 selector、Schema 和依赖求解，不生成安装器：

```bash
constructor .conda/constructor --platform linux-64 --render
constructor .conda/constructor --platform linux-64 \
  --conda-exe "$CONSTRUCTOR_CONDA_EXE" \
  --dry-run
```

完整版构建时设置：

```bash
export UNILABOS_INSTALLER_PACKAGE=unilabos-full
```

GitHub Actions 的 `platforms` 输入接受逗号分隔的平台或别名，例如
`win-64,linux,osx-arm64,osx`。CI 会先进行原生依赖求解，再构建、静默安装并验证
`unilabos`、`rclpy`、`unilab --help` 和 `unilab-supervisor --help`，最后上传安装器及
hash、lockfile 和包清单。

统一桌面打包必须包含当前 checkout，而不是碰巧同版本的远端包。根仓库的一键命令会先
用 `rattler-build` 生成本地 channel，再通过 `UNILABOS_INSTALLER_CHANNEL=file:///...`
将该 channel 放到 Constructor channel 列表首位。单独调试此流程时也可显式设置该变量。

在包含 OS 与前端 submodule 的 Uni-Lab-Core 根目录运行：

```bash
pnpm package:unified --platform linux-64
```

该命令依次构建三个 pip-only 依赖的本地 Conda 包、当前 OS 源码包、Constructor 私有
Runtime 以及 Electron 安装包。最终用户只安装 Electron 成品；首次启动 Edge 时由桌面端
校验并安装私有 Runtime，不需要 Git、Conda 或 Uni-Lab-OS 源码。
