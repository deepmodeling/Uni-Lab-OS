# Uni-Lab-OS 安装指南

本指南提供 Uni-Lab-OS 的完整安装说明，涵盖从快速一键安装到完整开发环境配置的所有方式。

## 系统要求

- **操作系统**: Windows 10/11, Linux (Ubuntu 20.04+), macOS (10.15+)
- **内存**: 最小 4GB，推荐 8GB 以上
- **磁盘空间**: 至少 10GB 可用空间
- **网络**: 稳定的互联网连接（用于下载软件包）
- **其他**:
  - 已安装 Conda/Miniconda/Miniforge/Mamba
  - 开发者需要 Git 和基本的 Python 开发知识
  - 自定义 msgs 需要 GitHub 账号

## 安装方式选择

根据您的使用场景，选择合适的安装方式：

| 安装方式               | 适用人群             | 特点                           | 安装时间                     |
| ---------------------- | -------------------- | ------------------------------ | ---------------------------- |
| **方式一：一键安装**   | 实验室用户、快速体验 | 预打包环境，离线可用，无需配置 | 5-10 分钟 (网络良好的情况下) |
| **方式二：手动安装**   | 标准用户、生产环境   | 灵活配置，版本可控             | 10-20 分钟                   |
| **方式三：开发者安装** | 开发者、需要修改源码 | 可编辑模式，支持自定义 msgs    | 20-30 分钟                   |

---

## 方式一：一键安装（推荐新用户）

使用预打包的 conda 环境，最快速的安装方法。

### 前置条件

确保已安装 Conda/Miniconda/Miniforge/Mamba。

### 安装步骤

#### 第一步：下载预打包环境

1. 访问 [GitHub Actions - Conda Pack Build](https://github.com/deepmodeling/Uni-Lab-OS/actions/workflows/conda-pack-build.yml)

2. 选择最新的成功构建记录（绿色勾号 ✓）

3. 在页面底部的 "Artifacts" 部分，下载对应你操作系统的压缩包：
   - Windows: `unilab-pack-win-64-{branch}.zip`
   - macOS (Intel): `unilab-pack-osx-64-{branch}.tar.gz`
   - macOS (Apple Silicon): `unilab-pack-osx-arm64-{branch}.tar.gz`
   - Linux: `unilab-pack-linux-64-{branch}.tar.gz`

#### 第二步：解压并运行安装脚本

**Windows**:

```batch
REM 使用 Windows 资源管理器解压下载的 zip 文件
REM 或使用命令行：
tar -xzf unilab-pack-win-64-dev.zip

REM 进入解压后的目录
cd unilab-pack-win-64-dev

REM 双击运行 install_unilab.bat
REM 或在命令行中执行：
install_unilab.bat
```

**macOS**:

```bash
# 解压下载的压缩包
tar -xzf unilab-pack-osx-arm64-dev.tar.gz

# 进入解压后的目录
cd unilab-pack-osx-arm64-dev

# 运行安装脚本
bash install_unilab.sh
```

**Linux**:

```bash
# 解压下载的压缩包
tar -xzf unilab-pack-linux-64-dev.tar.gz

# 进入解压后的目录
cd unilab-pack-linux-64-dev

# 添加执行权限（如果需要）
chmod +x install_unilab.sh

# 运行安装脚本
./install_unilab.sh
```

#### 第三步：激活环境

```bash
conda activate unilab
```

激活后，您的命令行提示符应该会显示 `(unilab)` 前缀。

---

## 方式二：手动安装（标准用户）

适合生产环境和需要灵活配置的用户。

### 第一步：安装 Mamba 环境管理器

Mamba 是 Conda 的快速替代品，我们强烈推荐使用 Mamba 来管理 Uni-Lab 环境。

#### Windows

下载并安装 Miniforge（包含 Mamba）:

```powershell
# 访问 https://github.com/conda-forge/miniforge/releases
# 下载 Miniforge3-Windows-x86_64.exe
# 运行安装程序

# 也可以使用镜像站 https://mirrors.tuna.tsinghua.edu.cn/github-release/conda-forge/miniforge/LatestRelease/
# 下载 Miniforge3-Windows-x86_64.exe
# 运行安装程序
```

#### Linux/macOS

```bash
# 下载 Miniforge 安装脚本
curl -L -O "https://github.com/conda-forge/miniforge/releases/latest/download/Miniforge3-$(uname)-$(uname -m).sh"

# 运行安装
bash Miniforge3-$(uname)-$(uname -m).sh

# 按照提示完成安装，建议选择 yes 来初始化
```

安装完成后，重新打开终端使 Mamba 生效。

### 第二步：创建 Uni-Lab 环境

使用以下命令创建 Uni-Lab 专用环境：

```bash
mamba create -n unilab python=3.11.11  # 目前ros2组件依赖版本大多为3.11.11
mamba activate unilab
mamba install -n unilab uni-lab::unilabos -c robostack-staging -c conda-forge
```

**参数说明**:

- `-n unilab`: 创建名为 "unilab" 的环境
- `uni-lab::unilabos`: 从 uni-lab channel 安装 unilabos 包
- `-c robostack-staging -c conda-forge`: 添加额外的软件源

**如果遇到网络问题**，可以使用清华镜像源加速下载：

```bash
# 配置清华镜像源
mamba config --add channels https://mirrors.tuna.tsinghua.edu.cn/anaconda/pkgs/main/
mamba config --add channels https://mirrors.tuna.tsinghua.edu.cn/anaconda/pkgs/free/
mamba config --add channels https://mirrors.tuna.tsinghua.edu.cn/anaconda/cloud/conda-forge/

# 然后重新执行安装命令
mamba create -n unilab uni-lab::unilabos -c robostack-staging
```

### 第三步：激活环境

```bash
conda activate unilab
```

---

## 方式三：开发者安装

适用于需要修改 Uni-Lab 源代码或开发新设备驱动的开发者。

### 前置条件

- 已安装 Git
- 已安装 Mamba/Conda
- 有 GitHub 账号（如需自定义 msgs）
- 基本的 Python 开发知识

### 第一步：克隆仓库

```bash
git clone https://github.com/deepmodeling/Uni-Lab-OS.git
cd Uni-Lab-OS
```

如果您需要贡献代码，建议先 Fork 仓库：

1. 访问 https://github.com/deepmodeling/Uni-Lab-OS
2. 点击右上角的 "Fork" 按钮
3. Clone 您的 Fork 版本：
   ```bash
   git clone https://github.com/YOUR_USERNAME/Uni-Lab-OS.git
   cd Uni-Lab-OS
   ```

### 第二步：安装基础环境

**推荐方式**：先通过**方式一（一键安装）**或**方式二（手动安装）**完成基础环境的安装，这将包含所有必需的依赖项（ROS2、msgs 等）。

#### 选项 A：通过一键安装（推荐）

参考上文"方式一：一键安装"，完成基础环境的安装后，激活环境：

```bash
conda activate unilab
```

#### 选项 B：通过手动安装

参考上文"方式二：手动安装"，创建并安装环境：

```bash
mamba create -n unilab python=3.11.11
conda activate unilab
mamba install -n unilab uni-lab::unilabos -c robostack-staging -c conda-forge
```

**说明**：这会安装包括 Python 3.11.11、ROS2 Humble、ros-humble-unilabos-msgs 和所有必需依赖

### 第三步：切换到开发版本

现在你已经有了一个完整可用的 Uni-Lab 环境，接下来将 unilabos 包切换为开发版本：

```bash
# 确保环境已激活
conda activate unilab

# 卸载 pip 安装的 unilabos（保留所有 conda 依赖）
pip uninstall unilabos -y

# 克隆 dev 分支（如果还未克隆）
cd /path/to/your/workspace
git clone -b dev https://github.com/deepmodeling/Uni-Lab-OS.git
# 或者如果已经克隆，切换到 dev 分支
cd Uni-Lab-OS
git checkout dev
git pull

# 以可编辑模式安装开发版 unilabos
pip install -e . -i https://mirrors.tuna.tsinghua.edu.cn/pypi/web/simple
```

**参数说明**：

- `-e`: editable mode（可编辑模式），代码修改立即生效，无需重新安装
- `-i`: 使用清华镜像源加速下载
- `pip uninstall unilabos`: 只卸载 pip 安装的 unilabos 包，不影响 conda 安装的其他依赖（如 ROS2、msgs 等）

### 第四步：安装或自定义 ros-humble-unilabos-msgs（可选）

Uni-Lab 使用 ROS2 消息系统进行设备间通信。如果你使用方式一或方式二安装，msgs 包已经自动安装。

#### 使用已安装的 msgs（大多数用户）

如果你不需要修改 msgs，可以跳过此步骤，直接使用已安装的 msgs 包。验证安装：

```bash
# 列出所有 unilabos_msgs 接口
ros2 interface list | grep unilabos_msgs

# 查看特定 action 定义
ros2 interface show unilabos_msgs/action/DeviceCmd
```

#### 自定义 msgs（高级用户）

如果你需要：

- 添加新的 ROS2 action 定义
- 修改现有 msg/srv/action 接口
- 为特定设备定制通信协议

请参考 **[添加新动作指令（Action）指南](../developer_guide/add_action.md)**，该指南详细介绍了如何：

- 编写新的 Action 定义
- 在线构建 Action（通过 GitHub Actions）
- 下载并安装自定义的 msgs 包
- 测试和验证新的 Action

```bash
# 安装自定义构建的 msgs 包
mamba remove --force ros-humble-unilabos-msgs
mamba config set safety_checks disabled  # 关闭 md5 检查
mamba install /path/to/ros-humble-unilabos-msgs-*.conda --offline
```

### 第五步：验证开发环境

完成上述步骤后，验证开发环境是否正确配置：

```bash
# 确保环境已激活
conda activate unilab

# 检查 ROS2 环境
ros2 --version

# 检查 msgs 包
ros2 interface list | grep unilabos_msgs

# 检查 Python 可以导入 unilabos
python -c "import unilabos; print(f'Uni-Lab版本: {unilabos.__version__}')"

# 检查 unilab 命令
unilab --help
```

如果所有命令都正常输出，说明开发环境配置成功！

---

## 验证安装

无论使用哪种安装方式，都应该验证安装是否成功。

### 基本验证

```bash
# 确保已激活环境
conda activate unilab  # 或 unilab-dev

# 检查 unilab 命令
unilab --help
```

您应该看到类似以下的输出：

```
usage: unilab [-h] [-g GRAPH] [-c CONTROLLERS] [--registry_path REGISTRY_PATH]
              [--working_dir WORKING_DIR] [--backend {ros,simple,automancer}]
              ...
```

### 检查版本

```bash
python -c "import unilabos; print(f'Uni-Lab版本: {unilabos.__version__}')"
```

### 使用验证脚本（方式一）

如果使用一键安装，可以运行预打包的验证脚本：

```bash
# 确保已激活环境
conda activate unilab

# 运行验证脚本
python verify_installation.py
```

如果看到 "✓ All checks passed!"，说明安装成功！

---

## 常见问题

### 问题 1: 找不到 unilab 命令

**原因**: 环境未正确激活或 PATH 未设置

**解决方案**:

```bash
# 确保激活了正确的环境
conda activate unilab

# 检查 unilab 是否在 PATH 中
which unilab  # Linux/macOS
where unilab  # Windows
```

### 问题 2: 包冲突或依赖错误

**解决方案**:

```bash
# 删除旧环境重新创建
conda deactivate
conda env remove -n unilab
mamba create -n unilab uni-lab::unilabos -c robostack-staging -c conda-forge
```

### 问题 3: 下载速度慢

**解决方案**: 使用国内镜像源（清华、中科大等）

```bash
# 查看当前 channel 配置
conda config --show channels

# 添加清华镜像
conda config --add channels https://mirrors.tuna.tsinghua.edu.cn/anaconda/cloud/conda-forge/
```

### 问题 4: 权限错误

**Windows 解决方案**: 以管理员身份运行命令提示符

**Linux/macOS 解决方案**:

```bash
# 不要使用 sudo 安装 conda 包
# 如果 conda 安装在需要权限的位置，考虑重新安装 conda 到用户目录
```

### 问题 5: 安装脚本找不到 conda（方式一）

**解决方案**: 确保你已经安装了 conda/miniconda/miniforge，并且安装在标准位置：

- **Windows**:

  - `%USERPROFILE%\miniforge3`
  - `%USERPROFILE%\miniconda3`
  - `%USERPROFILE%\anaconda3`
  - `C:\ProgramData\miniforge3`

- **macOS/Linux**:
  - `~/miniforge3`
  - `~/miniconda3`
  - `~/anaconda3`
  - `/opt/conda`

如果安装在其他位置，可以先激活 conda base 环境，然后手动运行安装脚本。

### 问题 6: 安装后激活环境提示找不到？

**解决方案**: 尝试以下方法：

```bash
# 方法 1: 使用 conda activate
conda activate unilab

# 方法 2: 使用完整路径激活（Windows）
call C:\Users\{YourUsername}\miniforge3\envs\unilab\Scripts\activate.bat

# 方法 2: 使用完整路径激活（Unix）
source ~/miniforge3/envs/unilab/bin/activate
```

### 问题 7: conda-unpack 失败怎么办？（方式一）

**解决方案**: 尝试手动运行：

```bash
# Windows
cd %CONDA_PREFIX%\envs\unilab
.\Scripts\conda-unpack.exe

# macOS/Linux
cd $CONDA_PREFIX/envs/unilab
./bin/conda-unpack
```

### 问题 8: 环境很大，有办法减小吗？

**解决方案**: 预打包的环境包含所有依赖，通常较大（压缩后 2-5GB）。这是为了确保离线安装和完整功能。如果空间有限，考虑使用方式二手动安装，只安装需要的组件。

### 问题 9: 如何更新到最新版本？

**解决方案**:

**方式一用户**: 重新下载最新的预打包环境，运行安装脚本时选择覆盖现有环境。

**方式二/三用户**: 在现有环境中更新：

```bash
conda activate unilab

# 更新 unilabos
cd /path/to/Uni-Lab-OS
git pull
pip install -e . --upgrade -i https://mirrors.tuna.tsinghua.edu.cn/pypi/web/simple

# 更新 ros-humble-unilabos-msgs
mamba update ros-humble-unilabos-msgs -c uni-lab -c robostack-staging -c conda-forge
```

---

## 下一步

安装完成后，请继续：

- **快速启动**: 学习如何首次启动 Uni-Lab
- **配置指南**: 配置您的实验室环境和设备
- **运行示例**: 查看启动示例和最佳实践
- **开发指南**:
  - 添加新设备驱动
  - 添加新物料资源
  - 了解工作站架构

## 需要帮助？

- **故障排查**: 查看更详细的故障排查信息
- **GitHub Issues**: [报告问题](https://github.com/deepmodeling/Uni-Lab-OS/issues)
- **开发者文档**: 查看开发者指南获取更多技术细节
- **社区讨论**: [GitHub Discussions](https://github.com/deepmodeling/Uni-Lab-OS/discussions)

---

**提示**:

- 生产环境推荐使用方式二（手动安装）的稳定版本
- 开发和测试推荐使用方式三（开发者安装）
- 快速体验和演示推荐使用方式一（一键安装）
