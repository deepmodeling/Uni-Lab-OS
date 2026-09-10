"""Edge 微后端（调度、数据服务与 Host/Slave 网络）。

把云端 Go dagEngine 的 DAG 拆解/执行下沉到 Edge：

- 提交带 handle 依赖的工作流后由本模块拆解 DAG（对齐 Go buildTask/canRunNodes/clearFinishedNode）
- 每个工作流提交、每个子 action 完成都会触发一次全量重排（reschedule）
- 排序通过通用接口进行：本地稳定排序 stub，或 HTTP 调用 uni-lab-scheduler 微服务
- 节点间传参对齐 Go gjson/sjson 语义（dot path + ``@@@`` 分隔符）
- 时长预估两种计算模式（estimation.py）：声明式（gjson 取 resolved param）
  与历史 EMA；预估喂给远端排序 duration，并与实际起止一起经
  ``GET /api/v1/timeline`` 暴露给前端泳道图
- 实时监控总线（monitor.py）：material / device / action / scheduler 四通道
  进程内 pub/sub，调度器与仓储服务在关键节点 emit；经
  ``GET /api/v1/monitor/events``（SSE）实时推给前端监控面板，
  ``GET /api/v1/monitor/snapshot`` 提供初始填充与断线校准
- 设备状态存储（device_state.py）：``(device_id, property, value)`` 标量
  三元组 + 显式类型标记，独立 SQLite（与仓储/工作流库分开，WAL），
  latest upsert + history 变化点环形保留；归微后端管——HostNode 属性更新
  经 ``publish_device_status`` bridge 由 worker 串行落盘，REST 面
  ``GET/POST /api/v1/device-state*``，变化同步发监控 device 通道
- 工作流执行历史（history.py）：第三个独立 SQLite——workflow_runs
  （每次提交一行，含可回放的整图 spec_json 与状态流转/起止）+ job_runs
  （每个 job 完结 append，含实际/预估时长与截断返回值）；进程重启时
  把上一世代残留的非终态 run 标记 ``interrupted``；REST 面
  ``GET /api/v1/history/*``（跨重启查询）
- Host/Slave 网络（host_network.py）：Host 微后端监听所有 Slave，维护握手、
  心跳与物料请求，并统一下发 ROS domain / discovery / static peers；Slave
  微后端在 ``rclpy.init`` 前应用配置。ROS HostNode 只挂接运行时资源树。

三库分立：inventory.db（物料事务）/ device_state.db（高频遥测）/
workflow_history.db（低频审计），读写模式不同互不阻塞。

纯调度内核（models/dag_state/param_resolver/ordering/service）只依赖标准库，
FastAPI 仅在 api/main 层引入。
"""

from unilabos.app.scheduler.models import (
    Handle,
    NodeState,
    WorkflowEdge,
    WorkflowNode,
    WorkflowSpec,
)
from unilabos.app.scheduler.service import EdgeScheduler

__all__ = [
    "EdgeScheduler",
    "Handle",
    "NodeState",
    "WorkflowEdge",
    "WorkflowNode",
    "WorkflowSpec",
]
