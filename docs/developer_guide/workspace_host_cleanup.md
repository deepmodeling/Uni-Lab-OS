# Workspace Host 规整说明

核验日期：2026-11-10。固定比较 Dev `4153b032` / review 分支 `5883fa44` 与 V1 `690491a3`，不是对所有远端分支的实时盘点。本轮只整理职责与协议，没有删除 V1 运行代码。

## 先把当前工作归成四件事

1. **架构方向已确定**：使用 Dev 主进程持有 Scheduler / Workflow / Registry / Materials，监督执行进程；设备包和 slave 沿用 Dev。无需再引入一个外层 Workspace Host 来启动和监督 backend。
2. **已有代码但尚未合并完**：库存增量、预检、严格动作合同在 #358 候选中。上一轮核验的容量/资源快照回归仍是未关闭项；不能当作全部移植完成。
3. **明确的功能缺口**：action 返回值 if/else、资源空闲借出/独占区间、parallel 作者语法、预计时长、高级资源结算与开始许可。它们不由删除 Workspace Host 自动解决。
4. **接入与验收**：正式前端已有 API 不兼容清单；device status、苏州工站 Markdown 测试继续保留 TODO。React 调试站已在本地 review 分支移除，尚未应用到主工作区 Dev。

## Workspace Host 到底装了什么

当前 Dev 没有 `unilabos/workspace_host/`。V1 的该目录有 **13 个 Python 文件、8,273 行**，其中 `host.py` 2,521 行、`release_publish.py` 2,912 行。它同时承担了进程所有者、管理 API、SDK 发现、物料源编辑和旧发布同步，不能只按目录名判成“都是启动器”。

| 职责 | V1 位置 | 规整方向 |
| --- | --- | --- |
| 进程启动、监督、恢复、日志 | `host.py`、`launch.py`、`discovery.py` | 由 Dev `app/backend_main.py`、`app/supervisor.py` 和 `server/services/device_processes.py` 承担；逐项核对退出语义 |
| 状态、操作、错误协议 | `model.py`、`client.py`、`host.py` 的 HTTP handler | 抽出纯协议和客户端，挂到主进程 API；兼容期保持旧客户端可解释的结果 |
| 安全停止、恢复派发 | `scheduler_lifecycle.py`、`host.py::_stop_workspace` | 保留“先停派发、等在途收敛、再停进程”；对接 Dev restart/scheduler，不复制第二个生命周期管理器 |
| 本地数据重建 | `reset_safety.py`、`host.py::_reset_local_state` | 使用 Dev reset 的停机、关库、归档边界，按 Dev 事实表补齐必要检查；不直接复制旧表名扫描和删库逻辑 |
| 物料布局与模板校验 | `material_layout.py`、`material_template_worker.py`，及 host 中的接线 | 移到物料/图与包校验服务；保留预览、版本冲突和静态校验语义 |
| 旧远端发布/权威切换 | `release_publish.py`、`authority_sync.py`，及 host 中的旧方法 | 当前公开入口已关闭，优先清理；如以后需要数据导出迁移，作为独立工具重新定义边界 |
| PLC 模拟器、renderer | `launch.py` / `host.py` 的相关部分 | 放到开发工具/受管进程适配；不混入调度和物料协议。删除 React 调试站不等于删除物料模型或所有渲染工具 |

## 这里确实有协议，主要是以下几组

### 1. Workspace 管理协议

版本为 `unilab-workspace-host/v1`。实际路由只有四种：

| 接口 | 数据与约定 |
| --- | --- |
| `GET /v1/snapshot` | `schemaVersion`、`revision`、`eventCursor`、host、configuration、components；组件目前固定为 backend / edge / plc / renderer |
| `POST /v1/operations` | `operationId`、`command`、`parameters`、可选 `expectedRevision`；返回 202，后台串行执行 |
| `GET /v1/operations/{operationId}` | `pending → running → succeeded/failed`，包括结果、错误和时间；同 ID/同请求回放，同 ID/不同请求冲突 |
| `GET /v1/logs/{component}` | 有界日志尾部；通过 `maxBytes` 控制 |

另有结构化错误 `code/message/details`，以及 HTTP 401/404/409/400 的映射。发现文件位于 `<workspace>/.unilabos/runtime/workbench/session.json`，通过 `host.token` 的 Bearer token 访问本机服务。

这些是 CLI/SDK 所依赖的对外约定，不能随着 `host.py` 一起消失。建议把数据结构放入新的 `protocol/workspace.py`，HTTP client 放入新的 `client/workspace.py`，主进程路由放入新的 `server/api/workspace.py`。以上路径是建议，尚未实现；不要求继续保留独立 HTTP 进程或原端口。

整理时要保留几个区别：

- `operationId` 是管理请求的幂等身份，不是工作流 Task、设备 Job 或执行 attempt 身份。
- `expectedRevision` 当前在**提交入队时**检查；不能宣称它已经保证排队后执行时的版本不变。
- operation 写盘不等于设备动作“恰好执行一次”。目前组件恢复代码会接管存活进程；没有看到重启时扫描 pending/running operation 并统一补偿的通用流程，迁移后应明确重启中断的结果。
- `WorkspaceHostClient.status()` 有只读离线返回；`ensure_workspace_host()` 会启动进程。`DomainSource.discover()` 当前调用后者，迁移时应拆开“发现地址”和“启动服务”。
- 当前 revision/cursor 在新 Host 实例中从 0 开始。若保留为客户端并发控制字段，需要明确跨进程代次，不能拿它当执行 fencing。

### 2. Scheduler 生命周期协议

V1 客户端调用 `POST/GET /api/v1/scheduler/drain` 和 `POST /api/v1/scheduler/drain/resume`，检查 `phase=running/draining/drained` 及在途 Job 数量/ID。它支持整个 workspace 停止前先排空；超时则保持运行。

Dev 没有这三个同名接口，但已有 `/api/v1/restart` 和 `RestartCoordinator`：暂停派发、等待在途动作、重启 Host、恢复派发。建议复用这套协调逻辑，把需要对外使用的排空状态独立定义；**安全重启已实现，不等于 V1 的所有 stop/drain 操作已等价替代**。

`backend.restart` 还有生命周期差别：旧 Workspace Host 位于 backend 外层，能拉起退出的 backend；Dev 的 backend 就是顶层主进程，不能靠自身 API 监督自己。当前 Dev 明确拒绝权威自身 `scope=process`。应保留“重启执行面”与“由外部启动器/服务管理器重启 backend”的区别。

### 3. 物料布局与模板校验协议

`material_layout.py` 定义了 `unilab-material-layout-change/v1`、`...-preview/v1`、`...-apply/v1`。它对工作区图文件做预览，使用文件摘要作为 revision，apply 时再次检查源版本、候选摘要和阻塞诊断，最后原子替换图文件。模板 worker 输出 `unilab-material-template-validation/v1`，包括 valid/invalid、catalogDigest、统计和 diagnostics。

保留这些能力时，应接入 Dev 的 Graph/Materials Authority，明确源文件编辑与运行数据的关系。Dev 的 `/api/v1/lab/layout` 当前是网格、区域和墙体数据，不是设备节点坐标/资产预览的替代接口。不能因为两处都叫 layout 就删掉一边，也不能新建第二份运行物料权威。

### 4. 旧发布协议与真正的执行协议

`release_publish.py` 包含 `WorkspaceRelease`、`DeploymentPlan/Result`、`VerificationReport`，以及 build → plan → apply → verify → receipt 流程；回执版本为 `unilab-workspace-deployment/v1`。其中 `ReleaseBuilder/DeploymentTarget/ReleaseVerifier` 是 Python `typing.Protocol` 接口，不是额外的网络传输协议。

这套旧流程负责跨后端模板/物料/工作流身份映射。当前 `_dispatch()` 对 `authority.switch`、`release.publish`、`release.inspect` 均直接返回 `backend_mode_removed`；`_switch_authority()` 本身也拒绝执行。静态调用盘点显示 `_publish_release()`、`_inspect_release_target()`、`_bootstrap_backend_authority()` 已没有本模块内的正常调用入口。两份旧模块合计 **3,398 行，占该目录约 41%**，是最明确的首批清理候选。

但执行开始确认、命令账本和结果恢复在 V1 的 `app/edge_control/`、`workflow/`、`app/scheduler/` 等目录；Dev 对应 `protocol/runtime/control.py`、`server/backend/edge_control.py`、`backend/runtime/` 等。Workspace Host 只是启动这些进程，或在显式 reset 时操作它们的存储。清理旧管理层不能把这些执行协议一并认定为可删，也不能宣称它们已经随主进程替换而完成移植。

## 关于多 runtime，纠正上一轮的表述

当前 Dev 的精确结构是：

```text
backend 主进程：Scheduler / Workflow / Registry / Materials
  └─ 一个主 Host 的 runtime.v1 控制连接（本地子进程，或远端主 Host）
       ├─ 本进程设备
       ├─ slave 设备进程 A
       └─ slave 设备进程 B / …（HostLink）
```

这符合“一个 backend 下通过 slave 启动多个设备运行实例”的已有路径。若目标是多个**独立主 Host/runtime** 各自直接接入同一个 backend，则还需扩展协议。

证据：`server/backend/edge_control.py:126` 明确写着“单 Edge”；它只有一组 `edge_uuid/session_uuid/connection_epoch/outgoing`，且模块全局只有一个 service。`attach_connection():673` 实现后来连接替换旧连接。`device_processes.py::build_command` 则使用 `--is_slave --host_node_ip --hostlink_port` 启动多个设备进程。

因此：先保留一个主 Host + 多 slave 的运行方式。若继续扩展多主 Host，必须按 runtime 身份隔离 session、epoch、通知队列、事件游标、HTTP 代理和在途 Job，并让重启/排空带明确目标；只把 `components.edge` 改成列表不够。

## 调用方与清理顺序

V1 的直接调用方包括 `app/main.py` 的 workspace CLI、`agent_tools/workflow.py`、`client/domain.py`、`client/material_layout.py`、`client/material_renderer.py`、`client/material_template.py`，以及 `app/cli/material.py` 的错误类型。正式 Uni-Lab FE 当前检出的源码和本地 UniLabOS-Launcher 中未检出直接使用 Workspace Host 名称或其 `/v1/snapshot` 协议的代码；这不证明所有外部版本都没有依赖。

建议按五步实施，每步单独提交：

1. **清已停用链路**：移除旧发布/同步模块及 host 中的无入口实现；旧公开命令先保留明确拒绝，调整直接测内部旧实现的测试。此步不动设备控制协议。
2. **拆出纯协议和 SDK**：冻结 snapshot/operation/error/发现约定；文件路径、token 生成、Popen 不进入纯协议。旧 import 提供过渡适配，迁移 CLI/Agent/DomainSource。
3. **替换进程所有者**：接入 Dev 主进程、Host child、slave 和受管进程；明确 backend 自身重启归属，以及旧 `WorkspaceHost.close()` 留下子进程的行为是否继续保留。
4. **归位业务能力**：排空/重置归生命周期服务；布局归 Graph/Materials；模板检查归包/注册表工具链；PLC/renderer 归开发工具适配。统一 Dev 数据权威与鉴权方式。
5. **最后撤旧入口**：确认旧客户端迁移后删除兼容层和独立 host daemon。验收幂等冲突、版本变化、离线查询不启动进程、在途停止、未知执行不能重放、布局并发冲突、多个 slave 隔离。

这轮最值得先做的是第 1、2 步：可减少明显冗余，并让后续替换进程结构不再牵动业务协议。多主 Host 控制面属于独立扩展，不应夹在目录清理中顺便改变。

本轮验证：静态盘点 13 个模块、25 个命令和直接调用方；Dev 的连接代次、重连补发、HTTP 代理、重启等待/取消及权威自身重启拒绝共 6 个现有测试通过。没有启动 V1 工站、修改数据库或操作真实设备。
