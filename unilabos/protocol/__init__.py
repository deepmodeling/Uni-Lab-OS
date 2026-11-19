"""微后端稳定通信协议（契约层，不依赖 ``unilabos.server``；目录严格按四库划分）。

请从具体子模块导入：``base``（DTO 基类与规范 JSON）、
``runtime``（runtime.db 域子包，统一版本 runtime.v1：``data``=数据/执行
边界、``control``=业务控制面、``workflow``、``registry``）、``materials``（物料 DTO 与
``InventoryMutation`` 幂等信封）/ ``telemetry`` / ``history``（单文件域）；
校验与编解码辅助统一在 ``utils``
（``utils.workflow_validation``、``utils.json_codec``）。

本包 ``__init__`` 不做 eager re-export：部分协议模块复用
``unilabos.server.database.tables`` 中的表模型作 DTO，顶层全家桶会在
表模块加载途中制造循环导入。
"""
