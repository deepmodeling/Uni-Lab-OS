"""Uni-Lab-OS 微后端：四库、工作流权威与调度。

子包与模块按职责划分，请直接从深路径导入：

- ``unilabos.server.database``：四个物理 SQLite 文件的表模型、schema 与布局。
- ``unilabos.server.services``：materials / runtime / telemetry / history 四域服务。
- ``unilabos.server.composition``：``ServerServices`` 装配入口。
- 调度权威：``unilabos.server.scheduler``（调度器）、``unilabos.server.edge_dispatch``
  （经 runtime.v1 控制面向 Host 派发）、``unilabos.server.lifecycle``（停机信号与
  排空 / 重启 / 重置），由 ``unilabos.server.execution_composition`` 装配；
  ``execution_queue`` 与 ``inventory`` 两边都用。Host 侧的执行链、传输运行时与
  Backend 连接在 ``unilabos.client.host``。
- ``unilabos.server.api``：微后端 FastAPI 路由。

本包 ``__init__`` 不做 eager re-export：协议层（``unilabos.protocol``）与表模块
互有引用，顶层导入全家桶会在子模块加载途中制造循环。
"""
