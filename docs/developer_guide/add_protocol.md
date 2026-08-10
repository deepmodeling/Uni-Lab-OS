# 添加新实验操作（Protocol）

在 `Uni-Lab` 中，实验操作（Protocol）指的是**对实验有意义的单个完整动作**——加入某种液体多少量；萃取分液；洗涤仪器；机械+末端执行器等等，就像实验步骤文字书写的那样。

而这些对实验有意义的单个完整动作，**一般需要多个设备的协同**，或者同一设备连续动作，还依赖于他们的**物理连接关系（管道相连；机械臂可转运）**。`Protocol` 根据实验操作目标和设备物理连接关系，通过 `unilabos/compile` 中的“编译”过程产生硬件可执行的机器指令，并依次执行。

开发一个 `Protocol` 一般共需要修改6个文件：

1. 在 `unilabos_msgs/action` 中新建实验操作名和参数列表，如 `PumpTransfer.action`。一个 Action 定义由三个部分组成，分别是目标（Goal）、结果（Result）和反馈（Feedback），之间使用 `---` 分隔：

```{literalinclude} ../../unilabos_msgs/action/PumpTransfer.action
```

2. 在 `unilabos_msgs/CMakeLists.txt` 中添加新定义的 action
因为在指令集中新建了指令，因此调试时需要编译，并在终端环境中加载临时路径：
```bash
cd unilabos_msgs
colcon build
source ./install/local_setup.sh
cd ..
```

调试成功后，发起 pull request，Uni-Lab 的 CI/CD 系统会自动将新的指令集编译打包，mamba执行升级即可永久生效：

```bash
mamba update ros-jazzy-unilabos-msgs -c uni-lab -c conda-forge -c robostack-jazzy
```

3. 在 `unilabos/messages/__init__.py` 中添加 Pydantic 定义的实验操作名和参数列表
```{literalinclude} ../../unilabos/messages/__init__.py
:start-after: Start Protocols
:end-before: End Protocols
```

4. 在 `unilabos/compile` 中新建编译为机器指令的函数，函数入参为设备连接图 `G` 和实验操作参数。
```{literalinclude} ../../unilabos/compile/pump_protocol.py
:start-after: Pump protocol compilation
:end-before: End Protocols
```

5. 将该函数加入 `unilabos/compile/__init__.py` 的 `action_protocol_generators` 中：
```{literalinclude} ../../unilabos/compile/__init__.py
:start-after: Define
:end-before: End Protocols
```

6. 记得将新开发的 `Protocol` 添加至启动时的 `devices.json` 中。
```{literalinclude} ../../devices.json
:lines: 2-4
```
