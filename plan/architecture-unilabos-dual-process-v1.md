---
goal: UniLabOS 工站调度与设备执行双进程改造
version: 1.0
date_created: 2026-08-28
last_updated: 2026-08-29
owner: Uni-Lab-OS Team
status: InProgress
tags:
  - architecture
  - process
  - scheduler
  - edge-runtime
  - reliability
---

# Introduction

![Status: In Progress](https://img.shields.io/badge/status-In%20Progress-blue)

本文档描述 UniLabOS 从兼容单进程运行方式收敛到“工站调度进程 + 设备执行进程”的正式双进程架构所需改动。

本文档基于 `product/durable-scheduler-kernel-v2` 分支当前代码进行整理。代码已经具备双进程角色、Workspace Host 托管、WebSocket 命令通道以及 HTTP 作业与结果通道，因此本次工作不是从零拆分进程，而是完成默认启动、权威边界、版本一致性、物料结算和故障恢复。

## 目标运行结构

用户只执行一条命令：

```bash
unilab workspace start
```

Workspace Host 负责启动、监控和停止两个业务进程：

```text
Workspace Host（仅负责进程管理，不承担调度或设备执行）
├── 工站调度进程（Station Scheduler Process）
│   ├── 稳定代码角色：workspace_backend
│   ├── 工作流任务（WorkflowTask）与作业调度
│   ├── 物料（Material）、库存（Inventory）与库位（Site）权威
│   ├── HTTP 业务入口
│   └── Edge Control 服务端
│
└── 设备执行进程（Edge Runtime Process）
    ├── 稳定代码角色：edge_runtime
    ├── Edge Control 客户端
    ├── HostNode
    ├── 设备驱动与执行适配器（Execution Adapter）
    └── 已冻结作业（Job）的物理执行
```

`combined` 是将调度模块和设备执行模块放在同一个业务进程中的遗留兼容模式。它不会在内部继续创建上述两个业务进程，也不属于目标部署结构。

## 当前状态摘要

| 能力 | 当前状态 | 代码证据 |
|---|---|---|
| 运行角色 | 当前实现：已有 `combined`、`workspace_backend`、`edge_runtime` | `unilabos/app/runtime_topology.py` |
| 调度进程启动 | 当前实现：Workspace Host 可独立构造并启动 `workspace_backend` | `unilabos/workspace_host/launch.py::resolve_backend_launch` |
| 执行进程启动 | 当前实现：Workspace Host 可独立构造并启动 `edge_runtime` | `unilabos/workspace_host/launch.py::resolve_edge_launch` |
| 进程托管 | 当前实现：Workspace Host 分别监控 Backend 与 Edge Runtime | `unilabos/workspace_host/host.py` |
| 调度侧运行时 | 当前实现：本地工作流、调度器（Scheduler）、库存（Inventory）和 Edge 协议权威已存在 | `unilabos/app/scheduler/runtime.py` |
| Edge 命令接收 | 当前实现：Edge Runtime 通过 WebSocket 接收命令 | `unilabos/app/edge_control/client.py` |
| Edge 本地投递存储 | 当前实现：`edge_control.db` 保存命令、反馈和结果投递状态 | `unilabos/app/edge_control/store.py` |
| 调度侧协议权威 | 当前实现：调度侧提供 Edge 注册、命令、反馈和结果接口 | `unilabos/app/edge_control/local_authority.py` |
| 单命令启动 | 当前实现：默认 `workspace start/stop/restart` 统一托管 Backend 与 Edge 两个进程 | `unilabos/workspace_host/host.py`、Workspace CLI |
| 安全停止 | 当前实现：本地调度器先停止新派发，等待设备在途和执行未知作业收敛后再停止两个进程 | Scheduler drain API、Workspace Host |
| 动作目录一致性 | 本版暂缓：两个进程必须使用同一 Python 环境和领域包，并在领域包变化后同时重启 | 运维约束；不支持单边热更新 |
| 物料写入边界 | 未完成：`HostNode` 仍有少量直接访问库存（Inventory）的路径 | `unilabos/ros/nodes/presets/host_node.py::discard_resource`、`::_do_transfer_resource` |
| 跨进程人工干预 | 未完成：单进程式错误处置尚未形成完整的持久命令与事件闭环 | Edge Control 与 Scheduler 相关模块 |

## 改动总览

| 改动项 | 现状 | 需要怎么改 | 影响什么 | 难度 |
|---|---|---|---|---|
| 默认运行方式 | 三种运行角色并存，部分入口仍可默认进入 `combined` | 正式部署默认使用 `workspace_backend + edge_runtime`；`combined` 仅作为遗留兼容 | CLI、启动配置、旧测试 | 中 |
| 一条命令启动 | Workspace Host 已能分别启动两个进程，但用户入口不统一 | `unilab workspace start` 依次启动 Scheduler 和 Edge；二者全部就绪才返回成功 | Workspace Host、运维脚本 | 低 |
| 组合根拆分 | 两个角色仍经过较多共享启动逻辑 | 将调度进程和执行进程的对象装配收敛到两个独立组合根 | `app/main.py`、依赖加载、测试 | 中 |
| 工作流加载 | 领域包 Python 工作流经过 AST 扫描后进入本地工作流目录 | 只由调度进程加载、验证和冻结工作流；Edge 只消费冻结作业 | 工作流导入、任务创建 | 中 |
| 动作目录一致性 | 两个进程可分别加载设备能力，但缺少内容一致性证明 | 计算并交换领域包版本和动作目录指纹（Catalog Fingerprint）；不一致时关闭失败 | 领域包、Edge 注册协议 | 高 |
| 调度权威 | Scheduler 已承担主要调度职责 | 明确只有调度进程能创建和推进工作流任务、作业、预留与占用 | Scheduler 状态机 | 中 |
| 物料与库位写入 | Edge 的 `HostNode` 仍有直接调用库存的路径 | Edge 上报不可变变更集（ChangeSet）；调度进程校验后统一提交物料与库位变化 | HostNode、Inventory、台账 | 高 |
| 作业执行占用 | 已有部分资源控制，但跨进程持久占用语义仍需收敛 | 派发前创建作业执行占用（JobExecutionClaim）和栅栏令牌（Fencing Token） | 并发执行、迟到结果 | 高 |
| 人工干预 | 单进程错误处置与跨进程协议没有完全对齐 | 建立 `intervention_required → intervention_selected → ACK/结果` 持久闭环 | API、Scheduler、Edge、前端 | 高 |
| 取消与安全停止 | 已有取消命令，但取消请求不等于物理停止 | 分离取消请求、安全停止和物理结算（PhysicalSettlement） | 状态机、设备安全 | 中 |
| 崩溃恢复 | 已有重连和部分投递重放 | 覆盖发送前、发送后未知、结果未确认和两进程独立重启 | Edge Control、恢复扫描 | 高 |
| 日志与健康状态 | 主要检查进程和端口 | 增加目录加载、Scheduler 就绪、Edge 注册、指纹匹配等业务就绪状态 | Workspace Host、可观测性 | 中 |
| `combined` 兼容 | 仍被部分直接启动和测试使用 | 第一阶段保留但不作为正式部署；双进程验收后再决定是否删除 | 旧脚本、旧测试 | 低至中 |

## 1. Requirements & Constraints

- **REQ-001**: `unilab workspace start` 必须通过 Workspace Host 启动工站调度进程和设备执行进程，并在两个进程均达到业务就绪后返回成功。
- **REQ-002**: 工站调度进程必须是本地控制配置下工作流任务（WorkflowTask）、作业（Job）、物料（Material）、库存（Inventory）、库位（Site）、任务物料预留（TaskMaterialReservation）和作业执行占用（JobExecutionClaim）的唯一权威。
- **REQ-003**: 设备执行进程只能执行已冻结作业、保存边缘执行镜像（EdgeExecutionMirror）并上报反馈、结果和物理证据，不得推进工作流任务或直接裁决物料与库位事实。
- **REQ-004**: 工站调度进程和设备执行进程必须通过稳定的 HTTP/WebSocket 接口协作，不得共享 Python 对象、进程内锁或可写 SQLite 连接。
- **REQ-005（后续）**: 需要支持单边更新或多版本滚动部署时，Edge 注册再增加领域包版本、动作目录指纹、设备能力指纹和启动代次校验；当前版本不实现。
- **REQ-006**: 所有可能改变物料或库位的设备结果必须以变更集（ChangeSet）形式提交，由调度进程在校验作业身份、占用和栅栏后结算。
- **REQ-007**: 同一逻辑命令的投递重放（DeliveryReplay）必须保留 Command UUID、Job UUID、尝试序号和派发效果身份，不得隐式创建新的物理执行。
- **REQ-008**: 无法证明物理命令未发送或设备已停止时，作业必须进入 `execution_unknown` 或 `requires_attention`，禁止盲目物理重放（Blind Physical Replay）。
- **REQ-009**: 设备人工干预必须形成持久的请求、选择、命令、ACK 和结果记录，不能依赖跨进程直接方法调用。
- **REQ-010**: `combined` 在第一阶段仅作为遗留兼容存在，不得成为正式部署默认值。
- **CON-001**: `inventory.db` 和 `workflow_history.db` 仅由工站调度进程写入；`edge_control.db` 仅由设备执行进程写入。
- **CON-002**: 工作流 Python 源和领域设备包归领域仓库所有；Uni-Lab-OS 不得扫描兄弟仓库源码路径、修改 `sys.path` 或复制领域实现。
- **CON-003**: 本计划不引入鉴权改造；进程拆分不得改变现有无鉴权产品决定。
- **CON-004**: 稳定代码值 `workspace_backend`、`edge_runtime` 和现有 wire 字段在第一阶段保持兼容，不因文档名称变化直接重命名。
- **GUD-001**: HTTP 保存和查询持久事实；WebSocket 承担低延迟控制和在线反馈，但不能成为唯一历史来源。
- **GUD-002**: 任务物料预留（TaskMaterialReservation）、作业执行占用（JobExecutionClaim）和库位占用（SiteOccupancy）必须保持为三类独立事实。
- **PAT-001**: 两个进程通过公开接口形成接缝（Seam），调用方和测试均从该接口验证行为，禁止新增只做透传的浅模块。
- **CON-005**: 当前版本的两个进程由同一 Workspace Host 使用同一 Python 环境、同一已安装领域包启动；领域包变化后必须同时重启，不支持单边热更新。

## 2. Implementation Steps

### Implementation Phase 1 — 收敛启动入口与进程职责

- GOAL-001: 建立一条命令、两个业务进程、一个明确就绪结果的运行方式，同时保留 `combined` 作为非默认遗留兼容。

| Task | Description | Completed | Date |
|---|---|---|---|
| TASK-001 | 修改 Workspace CLI 默认启动行为，使 `unilab workspace start` 调用 Workspace Host 依次启动 Backend 和 Edge；保留显式单组件启动仅用于诊断。 | ✅ | 2026-08-29 |
| TASK-002 | 修改 `unilabos/workspace_host/host.py`，定义统一启动状态：Scheduler HTTP 就绪、数据库迁移完成、目录加载完成、Edge 已注册且指纹一致后整体为 READY。 |  |  |
| TASK-003 | 修改 Workspace Host 停止流程：调度器停止新派发，等待设备在途作业归零；执行未知保持阻塞；排空后依次停止 Edge 和 Backend。 | ✅ | 2026-08-29 |
| TASK-004 | 从 `unilabos/app/main.py` 提取两个组合根：调度组合根只装配 Web、Scheduler、Inventory 和 LocalEdgeControlAuthority；执行组合根只装配 EdgeControlClient、EdgeControlStore、HostNode 和设备适配器。 |  |  |

完成标准：一次命令能够启动两个独立 PID；任一进程未达到业务就绪时命令返回非零；停止命令不会在设备仍执行时将系统误报为安全停止。

### Implementation Phase 2 — 建立动作目录与领域包一致性证明

- GOAL-002: 保证调度进程校验和冻结的动作定义与设备执行进程实际执行的动作定义完全一致。

本阶段按 2026-08-29 产品决定暂缓。当前部署约束为同一环境、同一领域包、同时
重启；一旦需要单边热更新、滚动升级或跨机器混合版本，必须在放开这些能力前完成
TASK-005 至 TASK-008。

| Task | Description | Completed | Date |
|---|---|---|---|
| TASK-005 | 在运行时设备目录生成流程中，对规范化设备类型、动作名、参数 Schema、资源声明和领域包版本计算动作目录指纹（Catalog Fingerprint）。 |  |  |
| TASK-006 | 扩展 `unilabos/app/edge_control/client.py::_registration_devices()` 的注册载荷，上报领域包版本、动作目录指纹、设备能力指纹和启动代次。 |  |  |
| TASK-007 | 修改 `unilabos/app/edge_control/local_authority.py` 的注册处理，在同一 Edge 身份下保存并校验指纹；不一致时将 Edge 标记为不可派发并返回可诊断错误。 |  |  |
| TASK-008 | 为 `edge_control.db` 和调度侧 Edge 会话存储增加版本化迁移，保存指纹、启动代次、首次注册时间和最后确认时间。 |  |  |

完成标准：相同领域包得到稳定相同指纹；动作参数或资源声明变化会改变指纹；两个进程指纹不一致时没有作业能够进入设备执行。

### Implementation Phase 3 — 清理物料、库位和执行安全权威

- GOAL-003: 消除 Edge 对库存权威的直接写入，使所有物料与库位变化由调度进程持久结算。

| Task | Description | Completed | Date |
|---|---|---|---|
| TASK-009 | 审计并替换 `HostNode.discard_resource()`、`HostNode._do_transfer_resource()` 及其他 Edge 侧 Inventory 调用，使它们产生结构化设备结果和变更集（ChangeSet），不直接修改 `inventory.db`。 |  |  |
| TASK-010 | 在调度侧增加统一变更集结算入口，验证 Job UUID、尝试序号、作业执行占用、栅栏令牌和幂等键后，原子更新物料、库位与物料台账。 |  |  |
| TASK-011 | 将派发前的作业执行占用和栅栏信息写入持久调度记录，并放入冻结作业载荷；Edge 的反馈和结果必须原样返回这些身份。 |  |  |

完成标准：Edge Runtime 不打开 `inventory.db`；相同变更集重复提交返回同一回执；旧栅栏或冲突内容不能改变物料和库位状态。

### Implementation Phase 4 — 补齐人工干预、取消和故障恢复

- GOAL-004: 在网络中断、进程崩溃和设备结果不确定时保持物理执行安全，并允许操作人员通过持久协议完成处置。

| Task | Description | Completed | Date |
|---|---|---|---|
| TASK-012 | 在 Scheduler 与 Edge Control 间增加人工干预状态和消息：`intervention_required`、`intervention_selected`、干预 Command UUID、ACK 和最终结果。 |  |  |
| TASK-013 | 修改取消状态机，分离取消请求（Cancel Request）、安全停止（Safe Stop）和物理结算（PhysicalSettlement）；未收到安全停止证据时不得释放执行占用。 |  |  |
| TASK-014 | 完善 EdgeControlStore 的未确认命令、反馈和结果恢复；重连后按稳定身份重放传输，不产生新的物理执行身份。 |  |  |
| TASK-015 | 完善调度恢复扫描：区分具有未发送证明的作业、已发送待结果的作业和 `execution_unknown` 作业；禁止自动重放未知物理执行。 |  |  |

完成标准：Scheduler 或 Edge 独立重启后能恢复未完成状态；未知执行不会自动重发；人工选择能够从 API 持久传递到 Edge 并形成可查询结果。

### Implementation Phase 5 — 验证并切换默认模式

- GOAL-005: 用真实双进程生产路径完成验收，将双进程设为正式默认，同时保留可审计的兼容策略。

| Task | Description | Completed | Date |
|---|---|---|---|
| TASK-016 | 增加双进程契约测试，覆盖启动、注册、指纹校验、冻结作业、反馈、结果、取消、人工干预和物料结算。 |  |  |
| TASK-017 | 增加进程故障测试，覆盖命令发送前崩溃、发送后断线、Edge 重启、Scheduler 重启、结果迟到和启动代次变化。 |  |  |
| TASK-018 | 通过 Workspace Host 启动批准的模拟器或真实设备，运行至少一个包含物料移动的完整工作流任务并核对 Scheduler 权威事实。 |  |  |
| TASK-019 | 将正式部署配置切换为双进程默认；将 `combined` 标记为遗留兼容，并在完成兼容影响审计前不删除其代码。 |  |  |
| TASK-020 | 更新运行手册、架构图和故障排查文档，明确两个业务进程、四类存储归属、READY 条件和安全停止语义。 |  |  |

完成标准：正式启动路径只需一条命令；双进程验收覆盖正常路径和故障路径；真实设备执行不存在重复派发、跨进程双写或无证据释放资源。

## 3. Alternatives

- **ALT-001**: 继续以 `combined` 单进程作为正式模式。未选择，因为调度故障和设备执行故障无法隔离，且模块仍可通过内存对象形成隐式耦合。
- **ALT-002**: 使用 Python `multiprocessing` 直接共享对象和锁。未选择，因为共享内存不能提供可恢复的权威事实，也无法满足独立重启、投递重放和物理执行安全要求。
- **ALT-003**: 两个进程直接读写同一个 SQLite 文件。未选择，因为这会形成双写权威、迁移所有者不清和崩溃恢复歧义。
- **ALT-004**: 第一阶段立即删除 `combined`。未选择，因为旧测试、直接启动脚本和本地调试路径的影响尚未完成审计。

## 4. Dependencies

- **DEP-001**: Workspace Host 必须继续作为唯一进程托管入口，并能获得两个业务进程的 PID、日志路径、健康状态和退出原因。
- **DEP-002**: 领域仓库必须通过安装后的公开包提供工作流 Python 源、设备动作定义和版本信息；Uni-Lab-OS 不读取兄弟仓库源码目录。
- **DEP-003**: Edge Control HTTP/WebSocket 协议必须保留稳定 Job UUID、Command UUID、事件 UUID、尝试序号和启动代次。
- **DEP-004**: SQLite 迁移必须分别由 `inventory.db`、`workflow_history.db`、`edge_control.db` 和 `device_state.db` 的既有迁移所有者执行。
- **DEP-005**: 真实设备验收依赖可用实验设备或经过批准、且走相同公开装配路径的模拟器。

## 5. Files

- **FILE-001**: `unilabos/app/runtime_topology.py`——运行角色和进程计划；保持稳定角色值并调整默认选择。
- **FILE-002**: `unilabos/workspace_host/launch.py`——两个业务进程的启动参数和环境变量。
- **FILE-003**: `unilabos/workspace_host/host.py`——启动顺序、停止顺序、重启和整体 READY 状态。
- **FILE-004**: `unilabos/app/main.py`——现有组合入口；需要将双进程装配职责提取为独立深模块。
- **FILE-005**: `unilabos/app/scheduler/runtime.py`——工站调度进程的 Scheduler、Inventory 和 Edge Authority 装配。
- **FILE-006**: `unilabos/app/edge_control/client.py`——Edge 注册、命令接收、反馈和结果提交。
- **FILE-007**: `unilabos/app/edge_control/local_authority.py`——调度侧 Edge 注册、命令与结果权威。
- **FILE-008**: `unilabos/app/edge_control/store.py`——Edge 本地持久投递状态和恢复。
- **FILE-009**: `unilabos/registry/runtime_device_catalog.py`——内存设备动作目录和动作目录指纹来源。
- **FILE-010**: `unilabos/ros/nodes/presets/host_node.py`——移除 Edge 对 Inventory 的直接写入，改为结果和变更集上报。
- **FILE-011**: `tests/app/test_control_plane_runtime.py`——运行角色、装配边界和数据库归属测试。
- **FILE-012**: `tests/workspace_host/test_workspace_cli.py`——一条命令启动、READY、停止和重启测试。
- **FILE-013**: `tests/networking/test_edge_control_protocol.py`——注册指纹、命令、反馈、结果和人工干预协议测试。
- **FILE-014**: `tests/networking/test_edge_control_store_lifecycle.py`——Edge 重启、未确认消息和投递重放测试。
- **FILE-015**: `tests/hostlink/test_host_node_local_actions.py`——HostNode 不直接写 Inventory 的回归测试。

## 6. Testing

- **TEST-001**: 验证 `unilab workspace start` 产生两个不同业务 PID，且 Workspace Host 自身不加载 Scheduler 或设备驱动。
- **TEST-002**: 验证 Scheduler 未就绪时 Edge 不启动，Edge 未注册时整体状态不是 READY。
- **TEST-003**: 验证两个进程加载相同领域包时动作目录指纹一致；任一动作 Schema 变化会导致指纹不同并阻止派发。
- **TEST-004**: 验证设备执行进程不创建或写入 `inventory.db`、`workflow_history.db`。
- **TEST-005**: 验证调度进程不初始化 ROS、HostNode 或具体设备驱动。
- **TEST-006**: 验证一个正常作业从持久派发意图、WebSocket 命令、HTTP 反馈到结果与物料结算完整闭环。
- **TEST-007**: 验证相同 Command UUID 和 ChangeSet 重放幂等；相同身份但内容冲突时拒绝。
- **TEST-008**: 验证旧栅栏令牌和旧启动代次的迟到结果不能覆盖当前权威事实。
- **TEST-009**: 验证 Scheduler 在命令持久化后、发送前崩溃时具有未发送证明，并按同一身份安全恢复。
- **TEST-010**: 验证命令可能已发送但结果未知时进入 `execution_unknown`，不自动创建新执行尝试。
- **TEST-011**: 验证人工干预从 Edge 请求、API 选择、Scheduler 持久化到 Edge ACK 的跨进程闭环。
- **TEST-012**: 验证取消请求不会立即释放执行占用，只有安全停止和物理结算完成后资源才可复用。
- **TEST-013**: 验证 Workspace Host 可以独立重启 Scheduler 或 Edge，并恢复未完成工作而不重复执行物理动作。
- **TEST-014**: 验证正式双进程路径下完成一个真实或批准模拟的含物料移动工作流，并从公开接口核对物料与库位权威事实。

## 7. Risks & Assumptions

- **RISK-001**: 两个进程加载不同领域包或动作目录时，Scheduler 可能派发 Edge 无法安全执行的动作；必须在注册和派发两个阶段校验指纹。
- **RISK-002**: Edge 继续直接写 Inventory 会产生物料和库位双重权威，导致重启后状态无法判定。
- **RISK-003**: 命令发送与持久状态之间存在崩溃窗口；没有派发效果身份和未发送证明时，自动重发可能造成重复物理动作。
- **RISK-004**: 取消请求被误认为设备已经停止，会提前释放设备、物料或库位，造成并发物理冲突。
- **RISK-005**: 当前 `app/main.py`、Workspace Host、Edge Client 和 Local Authority 文件较大；继续向原文件叠加逻辑会降低局部性，实施阶段必须按组合根、协议、存储和恢复职责拆分。
- **RISK-006**: 当前工作区包含其他尚未提交的内存模板与工作流改动；实施时必须保护这些用户改动，并以独立提交组织双进程改造。
- **ASSUMPTION-001**: 本地控制配置继续由 Uni-Lab-OS 内置 Scheduler 承担本地调度权威，生产 Backend 控制配置仍由外部 Backend 承担权威。
- **ASSUMPTION-002**: 设备动作模板和工作流定义在 OS 启动时从已安装领域包加载并通过 AST 校验；物料模板继续持久化在 SQLite。
- **ASSUMPTION-003**: 第一阶段保留稳定代码值 `workspace_backend`、`edge_runtime` 和 `combined`，只改变正式默认运行方式。
- **ASSUMPTION-004**: 本计划的 7 至 11 个开发日估算不包括真实设备排期和现场故障复现等待时间。

### 本轮大文件保留决定

- `unilabos/workspace_host/host.py` 继续保存单一工作区生命周期权威，但新增的 HTTP
  排空协议已提取到 `workspace_host/scheduler_lifecycle.py`，不再把轮询与协议解析
  堆入 Host。后续拆分目标是 component lifecycle、authority switching 和 renderer
  automation 三个模块；本轮不同时搬迁旧代码，避免扩大双进程改造风险。
- `unilabos/app/scheduler/service.py` 只保留排空门禁和在途作业状态合并，因为它们
  必须与重排锁处于同一原子边界；HTTP 路由继续位于 `scheduler/api.py`。
- `unilabos/workflow/task_scheduler_bridge.py` 只负责把既有持久 Task/Job 安全事实
  提供给调度器，不复制状态。测试新增在独立小文件；不再继续扩展已有超大 Host
  测试文件。

## 8. Related Specifications / Further Reading

- `CONTEXT.md`：Uni-Lab-OS 当前共享接口、权威、工作流与控制配置术语。
- `AGENTS.md`：Uni-Lab-OS 启动、分层和仓库开发约束。
- `plan/refactor-ephemeral-workflow-catalog-1.md`：进程内工作流目录改造计划。
- `plan/refactor-in-memory-device-catalog-1.md`：进程内设备动作目录改造计划。
- `unilabos/app/runtime_topology.py`：当前进程角色定义。
- `unilabos/workspace_host/host.py`：当前进程托管实现。
- `unilabos/app/edge_control/`：当前跨进程命令、反馈、结果和边缘执行镜像实现。
