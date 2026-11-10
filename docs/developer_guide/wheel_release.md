# 配套 wheel 发布约定

默认发行使用 Python 3.14 / NumPy 2 / HostLink，不需要 ROS。下游修正版不上传官方 PyPI，
而是与 UniLabOS 同批作为发行附件提供。只附带一个 wheel 不会改变 pip 的查找规则；
安装入口必须显式提供 `--find-links`，不能把解决依赖的责任推迟到启动设备时。

## 源码与版本

- `recipes/wheels.toml` 固定下载地址、SHA256、版本、补丁和 NOTICE。
- Opentrons wheel 使用 `recipes/opentrons-shared-data/numpy2.patch`，与 Conda 完全共用；
  包版本为 `9.1.0+unilabos.np2.1`，保留原许可证、修改说明和物料 JSON 数据。
- PLR 固定提交 `6285d662effa972be97a781ba7493553f2ba94ee`，与 Conda 共用版本和依赖补丁；
  Python 分发版本 `0.2.2+unilabos.6285d662effa`，避免与上游 `0.2.2` 混淆。
- PLR 自身必须无条件声明 `opentrons-shared-data==9.1.0+unilabos.np2.1`；
  Conda 的 PLR 配方也声明同一前置包。不是调整 requirements 的行顺序，也不安装完整的 `opentrons` SDK。
- UniLabOS 的 `Requires-Dist` 与包内 `unilabos/utils/requirements.txt` 一致。
  不使用源码直链依赖：`--no-index` 不会阻止直接 URL 的下载。

## 构建入口

仅准备开发/CI 配套依赖：

```bash
python scripts/build_wheel_release.py --dependencies-only --output-dir dist/companion
python -m pip install --find-links dist/companion/wheelhouse -e ".[full]"
```

在当前目标平台构建完整包：

```bash
python scripts/build_wheel_release.py --output-dir dist/wheel-release
# 未提交工作区只能生成明确标记的本地测试包
python scripts/build_wheel_release.py --local-test --output-dir dist/wheel-local-test
```

构建机需要 Python 3.14、pip、Git 和网络；目标机只需 Python 3.14。
脚本要求新的或空的输出目录，不会覆盖旧发行包。源码缓存按 SHA256 命名，每次复核哈希。
正式构建拒绝相关未提交修改，主包从当前 HEAD 的 Git 归档构建，避免夹带本地 build 缓存。

完整包包含主包、两个配套 wheel 和全部默认间接依赖，以及：

- `install_wheel_release.py`：标准库安装入口，新建 venv，拒绝覆盖现有目录。
- `requirements.lock`：实际 wheel 的精确版本和 SHA256，安装启用 `--require-hashes`。
- `manifest.json`：源码 SHA、平台、Python 版本、校验结果及文件哈希。
- `wheel-sources.toml`、`sources/`：固定源清单、实际补丁和修改说明。
- `verify_installation.py`：只读检查依赖、无 ROS、微后端、CLI 和实际物料创建。

所有 JSON、锁文件、ZIP、wheel 都在本地输出或 CI artifact 中生成，不提交到 docs 或源码。

## 验收与发布

构建必须在源码目录外分别创建全新 venv：只安装 PLR、只安装 UniLabOS、使用完整锁定清单安装。
前两项不得提前安装 Opentrons 或手动补依赖，以免全量清单掩盖缺失的依赖声明。
三种方式均从完整 wheelhouse 离线解析，运行 `pip check`、
无 ROS 导入、CLI 和 `corning_96_wellplate_360ul_flat` 实际创建；96 孔数量、NumPy 2
积分接口、修正版依赖版本都必须正确。仅 PLR 环境使用验证脚本的 `--materials-only`，不检查 UniLabOS 的 CLI。
只有全部通过才标记 `verified`、记录 `verified_install_roots` 并生成最终 ZIP 和外部 SHA256 文件。

`wheel-release.yml` 覆盖 Windows x64、Linux x64、macOS Intel/Apple Silicon。
PR/dev/手动运行只生成 Actions artifacts；正式 `v<版本>` Release 校验标签后，
必须等四个平台都通过，才把 ZIP 和校验文件附到该 Release，不覆盖已有附件，不上传 PyPI。
本地验证不等于四个平台都已通过；发布时仍须检查完整 CI 矩阵。

开发环境添加测试或硬件 SDK 时仍按 extras 安装。离线包只保证默认核心依赖，
不包含所有硬件 SDK、Python 解释器或 ROS。需要打包解释器时使用既有 Conda-Pack 发行入口。
