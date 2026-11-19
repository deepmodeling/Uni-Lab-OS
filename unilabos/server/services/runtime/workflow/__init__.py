"""Workflow Authority 的领域服务：编排、authoring、组合展开与上传。

线上 DTO 契约位于 ``unilabos.protocol.runtime.workflow``；持久化行模型与 DDL 位于
``unilabos.server.database.tables.runtime``；存储基座在本包 ``store``
（``WorkflowService`` 直接继承 ``WorkflowStore`` 持库）。
"""
