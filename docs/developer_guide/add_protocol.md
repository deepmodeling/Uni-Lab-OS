# 添加新实验操作（Protocol）

在 `Uni-Lab` 中，实验操作（Protocol）指的是**对实验有意义的单个完整动作**——加入某种液体多少量；萃取分液；洗涤仪器；机械+末端执行器等等，就像实验步骤文字书写的那样。

而这些对实验有意义的单个完整动作，**一般需要多个设备的协同**，或者同一设备连续动作，还依赖于他们的**物理连接关系（管道相连；机械臂可转运）**。`Protocol` 根据实验操作目标和设备物理连接关系，通过 `unilabos/experiments/compile` 中的“编译”过程产生硬件可执行的机器指令，并依次执行。

开发一个 `Protocol` 一般共需要修改6个文件：

1. 在 `unilabos_msgs/action` 中新建实验操作名和参数列表，如 `PumpTransfer.action`。一个 Action 定义由三个部分组成，分别是目标（Goal）、结果（Result）和反馈（Feedback），之间使用 `---` 分隔：

```{literalinclude} ../../unilabos_msgs/action/PumpTransfer.action
```

2. 在 `unilabos_msgs/CMakeLists.txt` 中登记新 action。调试时编译消息包，并在
当前终端加载工作区：
```bash
cd unilabos_msgs
colcon build
source ./install/local_setup.sh
cd ..
```

使用已发布的消息包时，可通过 Mamba 更新：

```bash
mamba update ros-jazzy-unilabos-msgs -c uni-lab -c conda-forge -c robostack-jazzy
```

3. 在 `unilabos/experiments/models.py` 中添加 Pydantic 定义的实验操作名和参数列表
```{literalinclude} ../../unilabos/experiments/models.py
:start-after: Start Protocols
:end-before: End Protocols
```

4. 在 `unilabos/experiments/compile` 中新建编译为机器指令的函数，函数入参为设备连接图 `G` 和实验操作参数。
```{literalinclude} ../../unilabos/experiments/compile/pump_protocol.py
:start-after: Pump protocol compilation
:end-before: End Protocols
```

5. 将该函数加入 `unilabos/experiments/compile/__init__.py` 的 `action_protocol_generators` 中：
```{literalinclude} ../../unilabos/experiments/compile/__init__.py
:start-after: Define
:end-before: End Protocols
```

6. 在 `unilabos/registry/devices/work_station.yaml` 中公开对应的工作站动作，并用
完整 Registry 检查动作模型、句柄和默认值。
