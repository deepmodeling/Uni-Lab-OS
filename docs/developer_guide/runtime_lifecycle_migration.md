# Workspace Host 生命周期协议迁移

2026-11-10，本地 `feat/v1-diff-resolution-20261109` 分支实现。Dev 已有管理 API 与控制面，因此沿现有目录迁移，不新增 Workspace Host daemon 或第二个管理端口。

## 结构与文件

一个 backend 持有 Scheduler / Workflow / Registry / Materials，对应一个主 Host。Host 是 runtime，负责本地设备与多个 slave。浏览器和 SDK 连接 backend 的统一 HTTP 地址；runtime 保持唯一的 runtime.v1 控制 WebSocket。重连替换连接的既有语义不变，多主 Host 不属于目标功能。

| 层次 | 文件 | 本轮职责 |
| --- | --- | --- |
| 协议 | `unilabos/protocol/runtime/lifecycle.py` | Health、Readiness、Restart、Reset、SchedulerDrain DTO；从 diagnostics 移出已有请求模型并补充响应模型 |
| 协调 | `unilabos/server/backend/lifecycle.py` | 排空、恢复与在途设备作业合并查询，使用现有 Scheduler |
| 调度 | `unilabos/server/backend/scheduler/service.py` | 按来源保存派发暂停，覆盖已获派发许可到执行端登记之间的窗口 |
| API | `unilabos/server/api/runtime/lifecycle.py` | V1 三条 drain 路由，装入现有 diagnostics router |
| 现有生命周期 | `server/backend/restart.py`、`app/backend_main.py` | 重启和重置各自持有暂停来源，解除时保留其他来源 |
| SDK | `unilabos/client/runtime/lifecycle.py` | HTTPLifecycleClient，复用 HTTPClient、地址规范化与控制面 token；不自动启动进程 |

`server/backend` 放协调实现，公开 DTO 放 `protocol/runtime`，API 和 SDK 分别放 `server/api/runtime`、`client/runtime`。协议命名沿 Dev 的 runtime 层；`edge` 继续作为兼容的重启 scope，不做整仓名称替换。

## API

| 接口 | 行为 |
| --- | --- |
| `GET /api/v1/health` | 原有存活查询，响应字段保持 |
| `GET /api/v1/readiness` | 原有就绪查询；排空或维护暂停时返回 503，health 仍返回 200 |
| `GET/POST/DELETE /api/v1/restart` | 原有查询、请求、取消；默认只重启主 runtime，backend 常驻 |
| `GET/POST /api/v1/reset` | 原有预览和显式确认；仍在停机、关库后归档，未搬入 V1 旧库扫描/删库逻辑 |
| `GET /api/v1/scheduler/drain` | 新增，只读状态查询 |
| `POST /api/v1/scheduler/drain` | 新增，幂等阻止新派发；已经获得许可的动作继续完成 |
| `POST /api/v1/scheduler/drain/resume` | 新增，仅解除显式 drain |

写请求使用请求头 `X-UniLab-Token`（不再接受 Bearer）；没有本地 Scheduler 的 Host 返回业务码 5001，客户端应连接 backend。三个 drain 路径和 `phase/active_device_job_count/active_device_job_ids` 保留 V1 最小合同，附加 `requested/dispatch_paused/pause_reasons`。已有 restart/reset DTO 的 diagnostics import 仍可用。

`phase` 的含义：未暂停为 running；已暂停且有在途作业为 draining；已暂停且无在途作业为 drained。drained 表示设备执行端排空，不代表所有工作流完成。排队节点、任务级暂停和人工确认等待都保留。执行态未知的设备 attempt 阻止排空，需先裁决。

暂停来源为 drain、restart、reset、manual。同一来源重复请求幂等，全部解除后才恢复派发。例如 drain 和 restart 同时存在，取消重启后仍保持 drain；drain/resume 也不能解除 reset。状态随 backend 进程存活，runtime 重启不会清除 drain；backend 自身重启不持久恢复该暂停，请维护工具重新确认状态。

## SDK 示例

```python
from unilabos.client.runtime import HTTPLifecycleClient, SchedulerDrainTimeout
from unilabos.protocol.runtime.lifecycle import RestartRequest

with HTTPLifecycleClient("http://127.0.0.1:8002") as client:
    print(client.health())
    print(client.readiness())
    try:
        status = client.drain_and_wait(timeout=30)
    except SchedulerDrainTimeout as exc:
        print("仍有在途作业：", exc.status)
    else:
        print(status)
        # 按需维护；显式解除排空，继续原有等待任务。
        client.resume_from_drain()

    # 单独重启主 runtime 时使用现有协议；此调用登记重启，不等待完成。
    # client.request_restart(RestartRequest(mode="quiescent", scope="edge"))
```

构造、查询和关闭客户端都不启动或停止进程；离线抛 HTTP 连接异常，不伪造成功状态。排空等待失败不杀进程、不自动恢复派发。重启/重置不自动重试：响应丢失时由调用方查询已有状态，避免重复执行。生命周期 API 返回裸 DTO，HTTPClient 新增显式配置，原业务接口的信封解析默认行为保持。

## 仍需逐项处理的旧 Workspace 协议

- `/v1/snapshot`、`/v1/operations` 及 operationId/expectedRevision/持久操作日志未建立兼容入口；新 SDK 不是 WorkspaceHostClient 的原位替代。旧客户端尚不能直接删掉。
- workspace/backend start/stop 和 backend 自身拉起属于外部启动器/服务管理器；本轮 drain 不负责杀进程。
- 材料布局 preview/apply 需接入 Graph/Materials Authority；模板静态校验需接入包/Registry 服务，尚未在本轮移植。
- 旧发布、authority.switch、PLC 和 renderer 管理未搬入 runtime 协议。V1 分支的旧文件未改动。
- #358 的容量回归、if/parallel 作者语法、资源借出和预计时长等原有待办继续有效。

验证覆盖：真实 WorkflowService + BackendScheduler 的在途完成与后继恢复、派发交接窗口、重启取消/重连与 drain 共存、执行未知与人工确认区别、HTTP 鉴权、SDK 超时/离线/不重试和 OpenAPI 模型导出。没有操作真实设备或生产数据库。

本轮结果：核心回归 127 passed、3 skipped；另外启用独立临时双进程部署的 drain → resume → drain → reset 测试，并核验原 HTTP 业务信封兼容，共 3 passed。合计 130 项通过，剩余两项跳过均为原生授锁插件未编译/未启用的测试。真实部署测试使用空设备包、临时数据库和随机端口；已按现有控制面鉴权要求携带测试进程自己的 token。
