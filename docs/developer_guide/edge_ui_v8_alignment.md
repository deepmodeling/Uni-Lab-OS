# Edge UI v8 对齐记录

## 对齐基线

- 输入归档：`Z:\gaojing\a.tar.gz`
- Edge UI 分支：`feat/backend-resource-contract-v7`
- 基线提交：`cab89d1 feat(ui): complete Edge runtime workflows and diagnostics`
- 协议版本：OpenLab Protocol 1.9.0（Edge v8）
- 默认部署：完整 Host API 使用 `:8002`；独立 Scheduler Provider 使用 `:8092`

本次以归档为接口真相，将 Scheduler、Inventory 与 Resource Provider 收进
UniLabOS。设备运行时只提供 `hostlink` 和 `ros2` 两个公开 backend；HostLink 的本地
驱动执行器归属 `unilabos.hostlink`。

## 已在 UniLabOS 对齐

| 边界 | UniLabOS 落点 | 状态 |
| --- | --- | --- |
| Workflow 定义、Graph、Task、Node Job | `unilabos/server/workflow/` | 已挂到统一 composition root；补齐 manual confirmations、interventions、results、feedback history 只读接口 |
| Scheduler Provider | `unilabos/server/scheduler/` | Host 内嵌与 `:8092` 独立启动均可；Host 模式默认启用 |
| Inventory Provider | `unilabos/server/scheduler/inventory/` | 本地 SQLite 权威、17 类命令、Outbox、云端命令回报使用同一 wire schema |
| Resource Provider | `unilabos/server/scheduler/inventory/backend_api.py` | Resource Template、Material、Site、状态历史接口已并入 |
| Device State 与 Status Incident | `unilabos/server/scheduler/device_state.py`、`status_incidents.py` | 状态策略、联锁、Scheduler Hold、决策与恢复事件已接通 |
| 实时监控 | `unilabos/server/scheduler/monitor.py`、`unilabos/app/web/event_bus.py` | material/device/action/scheduler/status 五通道共享同一个进程内序列 |
| 配置、存储与生命周期 | `unilabos/config/config.py`、`unilabos/server/storage/`、`unilabos/app/web/server.py` | 统一路径、authority profile、启动与关闭已接入 |

旧 Host 专用诊断路由改为 `/api/v1/host-error-decisions*` 和
`/api/v1/host-monitor/*`；Edge UI 契约里的 `/api/v1/error-decisions*` 与
`/api/v1/monitor/*` 由 Scheduler Provider 独占，避免 FastAPI 注册顺序导致请求被
旧实现截获。

## 尚未对齐

### 1. Edge UI 仍混用两套 Workflow 语义

新 Workflow Authority 约定：

- `/api/v1/workflows` 管理 Workflow 定义；
- `/api/v1/workflows/{uuid}/graph` 管理整图；
- `/api/v1/workflow-tasks` 创建和查询运行。

但归档中的 `packages/protocol/src/workflow.ts`、Scheduler store、Editor、Devices
快捷动作和 organic synthesis workbench 仍向 `POST /api/v1/workflows` 提交旧
Scheduler DAG，并调用 `POST /api/v1/workflows/{id}/cancel`。同一路径不能同时
安全承载“创建定义”和“开始运行”。当前 UniLabOS 明确关闭 Scheduler 旧形状路由，
以 Workflow Authority 为唯一写入权威，因此这些页面的提交/取消链路尚不可用。

后续应把 UI 写链路迁到：创建或更新定义 -> 保存 Graph -> 创建 Workflow Task；
不要在 UniLabOS 中恢复第二套 `/workflows` 执行入口。

### 2. Action retry/skip 尚未形成完整的新 attempt

Host 只公布错误决策，不应直接篡改 Scheduler 状态。当前 retry/skip 回调已经能
传递，但 retry 决策还没有在 Scheduler/Workflow Task 侧原子创建新的 attempt/job
后再释放 Host。未完成这一步前，不能把错误恢复视为 durable。

### 3. Canonical Workflow Task 与旧实时 Timeline 仍是两条投影

历史查询已读取统一持久化数据；旧 Scheduler snapshot/timeline 仍主要观察
`EdgeScheduler` 自身提交的运行。UI 全量迁到 Workflow Task 后，需要让实时
timeline 读取同一个执行投影。

## 回归范围

- Edge UI：协议校验通过；protocol 104 个测试通过；应用 22 个测试通过；
  `vue-tsc` 与 Vite production build 通过。
- UniLabOS：Provider v8 与 HostLink coordinator 无 ROS 回归通过。
- 完整 Host `:8002` 导入仍依赖本机 ROS/unilabos_msgs 版本；当前环境安装的
  `unilabos_msgs` 缺少部分 action 类型，因此不能把本机导入失败归为本次接口回归。
