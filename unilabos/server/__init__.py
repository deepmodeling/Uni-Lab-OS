"""Uni-Lab-OS 微后端：四库、工作流权威与执行桥。

子包按职责划分，请直接从深路径导入：

- ``unilabos.server.database``：四个物理 SQLite 文件的表模型、schema 与布局。
- ``unilabos.server.services``：materials / runtime / telemetry / history 四域服务。
- ``unilabos.server.composition``：``ServerServices`` 装配入口。
- ``unilabos.server.backend``：调度器与云端 Backend 执行协调（区别于
  ``unilabos.backend`` 传输大类）。
- ``unilabos.server.api``：微后端 FastAPI 路由。

本包 ``__init__`` 不做 eager re-export：协议层（``unilabos.protocol``）与表模块
互有引用，顶层导入全家桶会在子模块加载途中制造循环。
"""
