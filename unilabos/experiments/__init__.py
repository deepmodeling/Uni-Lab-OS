"""实验协议域：XDL 风格实验动作的参数模型与协议编译器。

- models: 实验协议动作的 Pydantic 参数模型（PumpTransferProtocol 等），
  与 unilabos_msgs 的 ROS Action 定义一一对应。
- compile: 协议编译器，将实验协议步骤依据设备连接图展开为设备动作序列，
  由 workstation 节点在执行时调用。
"""
