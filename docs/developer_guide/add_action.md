# 添加新动作指令（Action）

本指南将引导你完成添加新动作指令的整个流程，包括编写、在线构建和测试。

## 1. 编写新的 Action

### 1.1 创建 Action 文件

在 `unilabos_msgs/action` 目录中新建实验操作文件，如 `MyDeviceCmd.action`。一个 Action 定义由三个部分组成，分别是目标（Goal）、结果（Result）和反馈（Feedback），之间使用 `---` 分隔：

```action
# 目标（Goal）- 定义动作执行所需的参数
string command
float64 timeout
---
# 结果（Result）- 定义动作完成后返回的结果
bool success  # 要求必须包含success，以便回传执行结果
string return_info  # 要求必须包含return_info，以便回传执行结果
...  # 其他
---
# 反馈（Feedback）- 定义动作执行过程中的反馈信息
float64 progress
string status
```

### 1.2 更新 CMakeLists.txt

在 `unilabos_msgs/CMakeLists.txt` 中的 `add_action_files()` 部分添加新定义的 action：

```cmake
add_action_files(
  FILES
  MyDeviceCmd.action
  # 其他已有的 action 文件...
)
```

## 2. 在线构建和测试

为了简化开发流程并确保构建环境的一致性，我们使用 GitHub Actions 进行在线构建。

### 2.1 Fork 仓库并创建分支

1. **Fork 仓库**：在 GitHub 上 fork `Uni-Lab-OS` 仓库到你的个人账户

2. **Clone 你的 fork**：

   ```bash
   git clone https://github.com/YOUR_USERNAME/Uni-Lab-OS.git
   cd Uni-Lab-OS
   ```

3. **创建功能分支**：

   ```bash
   git checkout -b add-my-device-action
   ```

4. **提交你的更改**：
   ```bash
   git add unilabos_msgs/action/MyDeviceCmd.action
   git add unilabos_msgs/CMakeLists.txt
   git commit -m "Add MyDeviceCmd action for device control"
   git push origin add-my-device-action
   ```

### 2.2 触发在线构建

1. **访问你的 fork 仓库**：在浏览器中打开你的 fork 仓库页面

2. **手动触发构建**：

   - 点击 "Actions" 标签
   - 选择 "Multi-Platform Conda Build" 工作流
   - 点击 "Run workflow" 按钮

3. **监控构建状态**：
   - 构建过程大约需要 5-10 分钟
   - 在 Actions 页面可以实时查看构建日志
   - 构建完成后，可以下载生成的 conda 包进行测试

### 2.3 下载和测试构建包

1. **下载构建产物**：

   - 在构建完成的 Action 页面，找到 "Artifacts" 部分
   - 下载对应平台的 `conda-package-*` 文件

2. **本地测试安装**：

   ```bash
   # 解压下载的构建产物
   unzip conda-package-linux-64.zip  # 或其他平台

   # 安装测试包
   mamba install ./ros-jazzy-unilabos-msgs-*.conda
   ```

3. **验证 Action 是否正确添加**：
   ```bash
   # 检查 action 是否可用
   ros2 interface show unilabos_msgs/action/MyDeviceCmd
   ```

## 3. 提交 Pull Request

测试成功后，向主仓库提交 Pull Request：

1. **创建 Pull Request**：

   - 在你的 fork 仓库页面，点击 "New Pull Request"
   - 选择你的功能分支作为源分支
   - 填写详细的 PR 描述，包括：
     - 添加的 Action 功能说明
     - 测试结果
     - 相关的设备或用例

2. **等待审核和合并**：
   - 维护者会审核你的代码
   - CI/CD 系统会自动运行完整的测试套件
   - 合并后，新的指令集会自动发布到官方 conda 仓库

## 4. 使用新的 Action

如果采用自己构建的action包，可以通过以下命令更新安装：

```bash
mamba remove --force ros-jazzy-unilabos-msgs
mamba config set safety_checks disabled  # 如果没有提升版本号，会触发md5与网络上md5不一致，是正常现象，因此通过本指令关闭md5检查
mamba install xxx.conda --offline
```

## 常见问题

**Q: 构建失败怎么办？**
A: 检查 Actions 日志中的错误信息，通常是语法错误或依赖问题。修复后重新推送代码即可自动触发新的构建。

**Q: 如何测试特定平台？**
A: 在手动触发构建时，在平台选择中只填写你需要的平台，如 `linux-64` 或 `win-64`。

**Q: 构建包在哪里下载？**
A: 在 Actions 页面的构建结果中，查找 "Artifacts" 部分，每个平台都有对应的构建包可供下载。
