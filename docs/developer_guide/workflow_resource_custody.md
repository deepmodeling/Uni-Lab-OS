# 资源借还与设备托管（dev）

本阶段使用 dev 的 Scheduler → 持久准入 → 开始许可 → Host 执行链路。
实现位于 WSL `/home/wz/unilab-custody-review-20261111`，演示位于
`/home/wz/LabDeviceCustodyDemo-review-20261111`。Windows 主目录和已有预览未合入或重启。

## 设备托管

v1.0 的 `task_while_loaded` 表示样品装入后设备持续归该任务托管，成功卸载才释放。
任务停止、取消、超时、跳过、进程退出都不是卸载证据。

在 `@action(contract="typed", resource_contract=...)` 声明参数名：

```python
{"version": 1, "device_tenancy": {
    "mode": "task_while_loaded", "material_param": "sample",
    "acquire_device_param": "vessel"
}}
```

卸载使用 `release_device_param`；设备间转移同时指定取得和释放参数。
原位处理使用 `{"version": 2, "operate_in_place": {"material_param": "sample"}}`。
样品字段为 ResourceSlot；设备参数使用字符串 ID，也可解析旧 ResourceSlot 设备身份为可读 `resource_id`，
例如 `reactor`。设备字符串经过物料权威校验，避免把设备当作 PLR 物料实例装配；旧 ResourceSlot 的实际装配仍受其 PLR 配置约束。Python 定义默认设备 ID 和过程，前端负责样品、设备绑定。

装载动作准入时先记 `reserved`，成功变为 `loaded`；失败或未知变为 `retained`。
转移期间源设备和目标设备均保留托管；只有成功结果释放准入时钉住的源托管 UUID。
迟到结果不能释放后来建立的新托管。已证明未派发的 pending 撤回只撤销新预占，
不释放原先已装载的源设备。

托管设备及其样品会阻挡其他任务，包括标记 always_free 的当前设备动作。
原位动作还必须匹配同一任务、样品和实际执行设备。动作开始许可再次检查这些事实。
合同随派发内容冻结并参与摘要；Host 当前动作合同变化时拒绝旧计划。
没有开始许可的直接执行不能启用托管语义。

流程跑完仍有样品时，成功终态改为 failed，`cleanup_status=requires_attention`，
并保留库存预留。原任务结束且全部动作收敛后，操作者核实样品已经取走，再显式释放。
最后一条托管释放后重试库存清理；重启也会重新收拢这一已确认事项。

## 显式空闲借还

核对 v1.0 tag 未找到独立的 borrow/lend/shared-idle 实现；这一部分是对 dev 的新增能力，
不是把现成的 v1 借还模块搬过来。

借出要求：原任务已暂停，指定区间处于 held，当前没有实际使用，且设备/样品没有托管。
操作者明确选择借用任务并填写原因；本阶段不根据文件变化、空闲时间或暂停自动借出。
物理区间所有权仍只有一条，借用任务的逻辑区间通过借用记录映射到该所有权。
借用任务的完整资源集合、借用身份和逐动作使用权在一个事务内取得。

原任务 resume 后必须等待归还。借用许可覆盖借用任务的一个连续资源区间；
正常结束自动归还，失败/取消/超时/未知执行保留为 retained。借用区间不能建立长时间
样品托管，不能再转借。仍有未收敛动作或待执行步骤时拒绝人工归还。
等待关系持久化，遇到跨任务借还/托管等待环路拒绝新的申请，不抢占正在使用的资源。

## 管理接口

这些请求发给调度权威的同一管理地址，沿用现有控制面认证。

| 接口 | 用途 |
| --- | --- |
| GET `/api/v1/workflow-resource-intervals?task_uuid=...` | 查询区间所有权及版本 |
| POST `/api/v1/workflow-resource-intervals/{owner_uuid}/lend` | 明确借给指定任务 |
| GET `/api/v1/workflow-resource-loans?task_uuid=...` | 查询有效借用，含出借任务、借用任务和设备键 |
| POST `/api/v1/workflow-resource-loans/{loan_uuid}/return` | 核实后人工归还 |
| GET `/api/v1/workflow-device-tenancies?task_uuid=...` | 查询任务、样品、设备和取得托管的动作 |
| POST `/api/v1/workflow-device-tenancies/{tenancy_uuid}/release` | 核实样品已取走后释放 |

借出请求：`loan_uuid`（调用方生成的幂等键）、`borrower_task_uuid`、`expected_version`、`reason`。
人工归还/释放请求：`expected_version`、`reason`。版本为正整数，拒绝 bool；原因不能空白。
借用绑定后版本递增，操作前需重新 GET。冲突返回现有工作流错误协议。
调度快照增加 `loans` 和 `tenancies`；修改提交后发布 `workflow.resource.changed` 并唤醒排队动作。

## 持久化与验证

runtime.db 记录借还与托管的四张表：`workflow_resource_loan`、`workflow_device_tenancy`、
`workflow_tenancy_transition`、`workflow_resource_waiter`（0.13 发布线的 schema v1 基线，
开发期的 v7 → v8 迁移已随迁移链压平删除）。

回归覆盖成功卸载、错误样品、失败/取消/超时/跳过、未知结果和迟到成功、撤回未派发转移、
并发连接、等待环路、重启、人工归还/释放、旧驱动合同拒绝及库存清理（当时还含 v7 活动 Claim 升级，
该升级测试已随迁移链压平删除）。
相关回归 507 passed / 2 skipped（包含本文件对应的 23 项资源借还/托管测试）。
双进程 smoke 的 9 个任务成功，46 次设备尝试和准入、13 个已释放区间、1 次已归还借用、
1 条已释放设备托管，实际使用残留为 0。证据文件见交付目录 validation.json。

外部 demo 新增两个 AST `@workflow`：`资源演示：样品托管` 和 `资源演示：空闲借还`。
smoke 经 HTTP 实例化、单步、暂停、借出、恢复，在默认 authority + Host 双进程中执行；
检查两库开始记录、借还和托管终态，结束后无活动实际使用。

未覆盖：前端资源处置面板、自动空闲借出策略、跨循环显式作用域、transfer/aliquot 数量及
库位实际结算、多 slave、ROS2 和真实硬件验收。设备转移的托管保护不等于物料搬运账本已迁移。
