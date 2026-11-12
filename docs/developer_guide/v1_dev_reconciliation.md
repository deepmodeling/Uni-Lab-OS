# V1 → Dev 逐项消解清单

核验日期：2026-11-09。本文区分“已实现”“保留 Dev 设计而关闭差异”“待移植”和“待验收”，不以文件差异消失代表功能完成。

2026-11-10 更新：生命周期 DTO、HTTP SDK 和 V1 的 drain 接口已在本地工作分支接入现有 server/protocol/client 分层，见 [生命周期协议迁移](runtime_lifecycle_migration.md)。用户确认一个 backend 对应一个主 Host/runtime，设备通过多 slave 扩展；多主 Host 不属于目标差异。布局、模板和旧管理操作日志仍需单独处理，详见 [Workspace Host 规整说明](workspace_host_cleanup.md)。

同日后续：C01 容量/资源快照回归已本地修复，相关回归 114 项通过。旧 `max_volume` 允许保存无法可靠换算体积的事实，已知体积仍受上限约束；显式 `capacity` 继续完整校验。详见 [容量规则与兼容边界](v1_inventory_preflight_contracts.md#容量)。修复未推送，#358 的远端 CI 和合入仍待完成。

## 基线与原始目标

| 对象 | 固定版本 | 当前含义 |
| --- | --- | --- |
| Dev | `4153b03291afa32acc8c9240cb56224a9f3b432d` | 本轮重新 fetch 后的 `origin/dev`；主工作区仍在此版本 |
| #358 候选 | `0c940dedd85ed7448c42e1609ab942198f1f0c0f` | 库存、预检、严格合同及合并前修复；尚未合入 Dev |
| 本轮工作分支 | `feat/v1-diff-resolution-20261109` | 基于 #358 候选，移除调试站、纠正术语并记录核验；本地分支 |
| V1 / 1.0 | `690491a3c7066d7c71e5fc6a1dcc2dd2b01032f9` | `origin/feat/schedule` 与 `v1.0` 指向同一提交；没有将 `v1.0` 当作 Dev |
| 正式前端 | `0ff92396ccdcdd6ae59538c012b4d6dfc4b4888b` | `https://git.dp.tech/lab-os/uni-lab-fe` 的 main 快照 |

原始目标是：保障 Dev 与七个 demo、独立 PLR/Opentrons 包和前端交互正常，再逐项吸收 V1 的关键能力，相互取长补短。用户后续明确保留 Dev 的进程、驱动包、slave 和并行调度设计。本轮不是把 V1 全树覆盖到 Dev。

历史恢复已覆盖 2,269 条可见消息（ID `19811312`–`20494658`）、全部 34 条用户消息；最后一条是原助手的周用量限制提示，并非工作完成确认。最新用户消息 `20494657` 要求本地代码至少进入分支。原始用户消息 `19968877` 明确“物料天然带锁”；`19970453` 要求包放工作目录、独立设备进程交给 slave；`19971647` 当时允许保留 React 调试站，已被本轮“删除 React 测试站”取代。

PR #329–357 已合并；#358 是本次唯一尚未合并的 PR。其他人的 PR、旧 #311/#262 不在范围内。详细历史保存在此前会议包的 `PR_HISTORY.md`。

## 结构对照与已确定方向

| 层次 | Dev | V1 参考实现 | 消解方式 |
| --- | --- | --- | --- |
| 启动 | `app/backend_main.py` 的主进程持有 Scheduler / Workflow / Registry / Materials，默认监督 Host 子进程 | `app/runtime_topology.py`、`workspace_host/`；combined / workspace_backend / edge_runtime 角色 | 保留 Dev；不另搬 workspace_host 来重复监督主进程 |
| 运行时拓扑 | 单主 Host/runtime（可本地或远端），下接多个 HostLink slave；支持设备进程管理与包安装 | V1 自身的 runtime、workspace 生命周期与控制面约定 | 按用户确认保留一个 backend 对应一个主 runtime，多主 Host 不在目标范围 |
| 浏览器入口 | 主进程统一 API 地址；Host 专属请求经 runtime.v1 通道代理，Host 不另占 HTTP 端口 | V1 本地 Console 及独立前端协议 | 正式 UI 使用 Uni-Lab FE；删除主仓库 React 调试站，继续保留 API、OpenAPI、SSE 与代理 |
| 设备 | `backend/runtime` 公共内核 + HostLink / 可选 ROS2；工作目录中的设备包和 slave | V1 工站执行适配、包目录和作者编译 | 保留 Dev 驱动生命周期；只抽取需要的合同/编排能力 |
| 调度 | `server/backend/scheduler`；并行 DAG、动作/物料互斥、FIFO、原生授锁插件和 Python fallback | `app/scheduler`、`workflow`、`scheduling`；控制区域、资源区间、Claim/Lease、开始许可、引擎协议 | 功能逐项适配；原生授锁插件不等于 V1 全部排程引擎 |
| 库存 | `server/services/materials` + `protocol/materials.py` + `client/materials`；lot / instance / substance / 命令流水 | reagent/current_substance、数量预留、跨库协调及物理效果结算 | 保留 Dev 数据权威，补语义与迁移；不复制第二套库存库 |
| 工作流作者 | `registry/workflows.py` 的 `ctx.run` / `ctx.run_template` / 循环；任务冻结计划 | `workflow/authoring.py`、Python AST、条件区域、group/parallel、资源使用区间 | 运行能力与作者语法分开验收 |

“一进程启动 backend”描述的是入口和调度权威；设备 runtime 可以是子进程、slave 或远端执行面，不应解释为所有设备驱动必须与 backend 同进程。

## 本轮可直接关闭的差异

| ID | 状态 | 决定与证据 |
| --- | --- | --- |
| D01 | 本地已完成 | 删除 `frontend/` 全部 React 调试站及 `tests/frontend/test_debug_console_api.py`；README 改指正式前端。后端 OpenAPI 导出和 API 测试保留。 |
| D02 | 生命周期协议已本地迁移；业务协议待归位 | 状态/重启/重置 DTO 与 SDK 归入 protocol/runtime、client/runtime；drain 三个接口接入 server/backend 与 server/api/runtime。保留 Dev 主进程监督；布局、模板及旧操作日志尚不能判为已消解。 |
| D03 | 拓扑按设计消解，现场验收待做 | 一个 backend 对应一个主 Host/runtime，下接多个 slave；符合本轮用户确认，不再把多主 Host 列为缺失能力。现有 supervisor、device-processes、driver-packages、HostLink 覆盖基本路径，真实工站验收列 T02。 |
| D04 | 按设计消解 | 保留并行任务和任务级单步；不将 V1 当前工站级单步准入策略直接复制过来。 |
| D05 | 按设计消解 | 保留原生授锁插件可选、加载失败回退 Python；保留版本化数据库迁移机制。0.13 发布线四个库都从 schema v1 开始（2026-11-13 把开发期 runtime v2–v12、materials v2–v8 迁移链压平为 v1 基线），旧开发库启动时备份后重建；发布后改结构从 v2 起追加迁移。 |
| D06 | 已配置 | WSL 的 `git.dp.tech` 使用 Windows Git Credential Manager；站点代理 `http://127.0.0.1:3897`。克隆成功，无需再次网页登录；凭据不写入报告或 patch。 |

## 功能消解清单

“已有”均注明是 Dev 主线还是未合并的 #358。每项完成后应补提交号和验收结果。

| ID | 当前状态 | 下一步与关闭条件 |
| --- | --- | --- |
| M01 默认物料锁 | Dev 已有；本轮只纠正术语 | ResourceSlot 等物料参数默认申请互斥锁，正常终态释放。保留显式免锁例外；不得把共享资源解释成 `materials_free`。 |
| M02 空闲借出 / 独占区间 | 部分基础已有，完整策略未接入 | 明确所有者区间、动作使用区间、借出和归还、取消/异常/未知执行。验收：A 空闲时 B 可借共享资源；A 占用使用时不能冲突；独占资源在整个声明区间不得借出。 |
| W01 `@workflow()` / `action.lock.x()` | 用户给定写法当前不支持 | Dev 装饰器需要 `display_name`，函数接收 ctx；`action` 无 `lock` 属性。先冻结作者接口，再接入资源区间；不能仅新增一个装饰器别名就宣称锁语义完成。 |
| W02 action 返回值驱动 `if/else` | Dev 未接通；V1 有实现和专门测试 | 将上游具名输出绑定到条件节点，等待生产者完成，再只执行命中分支；未选节点标记 skipped。覆盖 false、缺字段、非布尔、失败/重试、冻结快照、暂停恢复。 |
| W03 并行 | Dev DAG 已有；`parallel(A, B)` 作者接口没有 | Dev 保留调度实现；作者层编译出分叉/汇合并定义返回引用和失败语义。V1 实际写法是 `with parallel():` + `group`，不是 `parallel(A, B)`。 |
| W04 循环 | Dev 已有 for/while 及 `ctx.step_output` 条件 | 现有循环测试通过。它不能抵消 W02 的 if/else 缺口；后续只补作者语法和嵌套组合回归。 |
| A01 动作预计时长 | Dev 尚无预计时长合同 | 现有 `timeout` / `execution_timeout` 是硬/软超时，后者允许参数表达式。需增加独立预计时长字段、参数求值、冻结计划和调度消费；检查负数、缺参数、表达式限制。 |
| A02 严格动作合同 | #358 已实现基础，未合并 | `contract="typed"`、AST 参数/具名返回 Schema、冻结和派发/Host 校验已接入；先解决 C01。 |
| A03 高级资源合同 | 仅能解析部分声明，运行语义未接入 | `device_tenancy`、`transfer`、`transfer_step`、`operate_in_place`、`aliquot`、`order_sensitive` 当前拒绝未实现语义。按物理效果和持久占用分别移植，验收不能止于解析成功。 |
| I01 库存增量 | #358 已实现基础，未合并 | 单位换算、整批虚拟余量预检、容量、计数盘点；先解决 C01，再合入。 |
| I02 内容物数量共享与补料 | V1 语义尚未对齐 | 对接 Dev lot/material/substance，补多任务数量预留、Saga、补料分支和失败补偿。数量预留不能用 M02 的物理锁代替。 |
| I03 物理效果结算 | 未移植完整 | 搬运/分装按真实结果结算、与通用扣减去重、测量 Job 证据自动盘点；未知执行不能当“未消耗”回补。 |
| I04 废弃枪头仍留在物料权威 | 未修复；以 strict xfail 回归保留 | 设备侧拿起枪头后不放回（`TipSpot.get_tip()` + `empty()` + `tracker.commit()`，枪头被丢弃），再用 `DeviceNode.update_resource` 同步台面：本地 PLR 树里已没有这支 Tip，但权威里它仍是该 TipSpot 的子物料。原因是快照路径（`resource_tree_to_snapshot` → `apply_snapshot`）只更新既有物料、不删除缺席的子节点，从权威恢复（重启、跨设备 transfer 重新实例化）时会凭空多出一支枪头。2026-11-12 基线中 `tests/backend/hostlink/test_prcxi_material_e2e.py` 阶段 9 的断言 `_tip_children(tip_spot) == []` 即因此失败；该测试只能跑在 PRCXI 驱动上，已随驱动迁出主仓库（`fix/p6-devices`）；同一场景改用测试夹具（`tests/plr_fixtures.py` 的 `SiteDeck` + PLR 枪头盒）保留为 `tests/backend/hostlink/test_material_creation_flows_e2e.py::test_discarded_tip_is_removed_from_authority`（strict xfail）。关闭条件：快照能表达结构孩子的移除（或废弃枪头走显式 discard/销毁），该用例转为通过并去掉 xfail。 |
| P01 运行预检 | #358 已有 Dev API；正式 FE 未兼容 | 复用 Dev 执行计划/输入/当前库存，无业务写入；保留运行时 deferred 检查。先修 F02/F03，绑定修订并回归只读性。 |
| R01 启动许可 / fencing | 尚未移植 | 设备调用前确认当前 attempt 的开始权限并持久化；处理消息重发、旧 epoch、应答丢失和重启。许可不是执行成功或实际用量证据。 |
| R02 持久 Claim/Lease 与恢复 | Dev 有锁恢复/人工裁决基础；未完全等同 V1 | 保留 Dev 的未知执行和人工裁决；围绕 R01/M02/A03/I03 设计持久事实和迁移，补 Host/权威重启与重放回归。 |
| C01 #358 合并 | 容量回归本地已修复；远端 CI / 合入待做 | 保留原资源测试，按旧规格与显式容量声明区分校验；本轮 114 项回归通过。远端仍为 `0c940ded`、CI `42584235496` 未更新，不能按本地结果宣称合并完成。 |
| T01 device status | TODO，按用户要求后续做 | 现有 telemetry / status_fields / device_state 不等于用户要求的新设备状态合同；本轮不实现。 |
| T02 苏州工站 Markdown 测试 | TODO，等待用户提供 | 收到测试 Markdown、工站包和预期后，覆盖主 runtime 下多 slave、共享/独占锁、if、parallel、预计时长、正式 FE 的整链路场景。 |
| U01 其他文件差异 | 已建立全文件索引，待逐项核验 | 目录迁移、文档、生成物、测试和驱动差异不能按行数判定为缺失能力；索引中的“待核验”不可当作已关闭。 |

## 锁的统一语义

按用户最新定义：共享资源在所有者空闲期间可以借给其他任务；独占资源在占用区间内不能借出。两者在真正使用期间都需要互斥。“共享”不是读写锁中的共享读，也不是允许两个设备同时拿同一件物料。

历史要求“默认加锁”已有实现。Dev 的 `registry/material_locks.py` 生成默认锁列表，`server/backend/scheduler/resource_manager.py` 一次申请完整锁集合；已开始动作的取消在实际结束前不释放，未知执行等待核实。已有 `begin_handoff` 会把所有权直接交给后继，并验证物料 handle 证据；这种交接不等于把资源空闲期借给第三个任务。

V1 的 `scheduling/resource_bindings.py` 区分完整绑定和 `active_resource_ids`，支持显式区间内纯所有权共享、活动使用互斥。这可作为 M02 参考，但不能仅凭名字中的 shared 就断言已覆盖用户所说的跨任务空闲借出。`MaterialLock(free=True)` 是免锁，不是默认共享锁。

## 作者语法的实测结果

目前 Dev 可写：

```python
@workflow(display_name="检查流程")
def inspect_flow(ctx):
    step = ctx.run("device/inspect", {})
    # step 是 WorkflowStep 声明，不是设备执行后的返回值。
```

本轮最小探针的结果：

- `workflow()`：缺少必填 `display_name`。
- `action.lock`：无此属性；`WorkflowBuildContext.parallel`：无此属性。
- `ctx.run(...)` 返回 `WorkflowStep`，`bool(step)` 为 True，`step["qualified"]` 不可下标访问。直接 Python if 会在构图时执行，无法等待真实 action 返回值。
- Dev 接受 `condition` 图节点并形成节点运行记录，但实际 `BackendScheduler._build_dag` 抛出 `executor_kind 'condition' is not wired locally`。
- V1 的 `tests/workflow/test_structured_condition_authoring.py::test_condition_waits_for_and_reads_previous_node_result` 覆盖了用户所说的等待测量结果，再判断 `measurement.qualified` 的场景；这是移植候选，不是 Dev 已通过的测试。
- V1 声明了 `estimate_duration_fixed` 和 `estimate_duration_express`，注册表与 AST 有投影测试。本轮没有证明这些字段已被 V1 排程引擎端到端消费；Dev 当前连这两个预计字段也没有，不能用超时表达式冒充。

## 正式前端 API 消解清单

本轮在正式前端原始 main 运行 core 测试，然后用它的真实 FetchTransport / domain clients 请求 Dev 候选的临时 HTTP 服务。数据库全为临时数据，未启动调度器和设备；这验证请求和解码，不是浏览器 E2E 或工站执行验收。

| ID | 真实差异 | 处理建议 / 关闭条件 |
| --- | --- | --- |
| F01 工作流类型与目录 | FE 列表/详情要求 `workflow_type`；Dev 响应未提供，解码报 `UNSUPPORTED_WORKFLOW_TYPE`。FE 的 `status=published` 过滤也未在 Dev 同一路由定义。 | 明确统一类型词表、已发布列表和详情；不得把缺字段默认为已发布。 |
| F02 Preflight 入参 | FE 总发送 `inventory_bindings: []`；Dev 严格 DTO 拒绝该字段。HTTP 200，业务 `code=1000`，FE 报 `OS_REQUEST_REJECTED`。 | 采用显式 Dev adapter，或者先实现 Dev 库存绑定合同；仅删字段只能解决空绑定。 |
| F03 提交绑定与修订 | Dev 建任务 DTO 忽略 FE 的 `inventory_bindings` 和 `priority`；前端也未发送 `expected_revision`。空绑定提交返回 Task 不代表非空绑定生效。 | 建立数量绑定、优先级和幂等/修订约定；传递预检修订，非空绑定不得静默丢弃。 |
| F04 Task / NodeRun / Attempt | Task 详情可解码；FE 的 Task Jobs 要求 `topological_index`，Job 详情要求 `executor_kind`，Dev attempt DTO 无这些字段。 | 对齐 Dev 的 NodeRun / attempt 分层，在 adapter 明确组合数据；不能无依据填 0 或成功状态。 |
| F05 库存与物料路径 | FE 的 `/inventory/instances`、`/reagents`、`/materials/graph` 在 Dev 不存在；Dev 提供 `/materials/instances`、lot/chemical 等自己的合同。 | 以 Dev 库存权威做显式映射，覆盖读/写 DTO、信封、版本和错误语义；不是简单换 URL。 |
| F06 动作与作者目录 | core client 引用了 `/devices`、`/authoring/device-catalog`、`/workflow-node-templates`、`/device-action-runs`；Dev API 组织不同。 | 对接 registry 与设备单点 Task 接口，保留正式前端领域端口；逐条验收 Schema/权限。 |
| F07 控制与观测扩展 | presentations、execution-locks、人工确认、干预等部分 FE 路径无直接 Dev 对应；动态 URL 尚需进一步展开。 | 区分可以投影的既有 Dev 事实和真正缺少的后端能力，不创建第二个前端事实权威。 |
| F08 Center 与工站边界 | core 同时包含 center/project/experiment 等接口，不都属于 OS 本地 backend。 | 按真实路由归属标为 Center 独立能力，不把所有未命中都列成 Dev 欠账。 |
| F09 浏览器与苏州联调 | 尚未执行 | 前述协议适配后再跑真实页面和 T02；本轮 531 个 mock/领域测试不能替代联调。 |

静态扫描范围是 `packages/core/src/domain` 的显式 method+url 请求对象：106 处请求、102 个去重操作，14 个在 Dev 候选 OpenAPI 存在、88 个未命中，另有 29 处动态 URL/方法未解析。这是用于定位的扫描结果，既不是“只有 14 个 API 可用”，也不是“Dev 缺 88 个必做功能”；存在的路由仍可能有 DTO 不兼容，Center 路由需要 F08 分流。逐条结果和源码行号在 patch 包 `evidence/frontend-routes.json`。

## 验证、阻塞与后续顺序

- Python 3.12.16；核心相关回归：113 passed / 2 skipped。覆盖 OpenAPI、预检、token、backend-only、物料锁、超时、授锁、单步并行和循环。
- 补充进程/包拓扑回归：36 passed，覆盖 supervisor、device-processes、driver-packages、HostLink hello/RPC。
- `tests/backend/runtime/test_resource.py` 修复前复现容量问题：8 passed / 2 failed。修复后该文件全部 10 项通过，已包含在本轮 114 项回归中；不是 Windows/Linux 差异，也不是缺少 ROS 导致的导入错误。
- 2026-11-10 容量修复回归：114 passed，覆盖 `tests/server/test_materials*.py`、库存增量、调度库存权威、运行预检、runtime 资源服务、PLR 适配/序列化与 HostLink 物料代理。新增 24 项容量边界用例；不与此前生命周期等重叠批次累加计数。
- 正式前端 pnpm 10.13.1，core 67 个文件 / 531 项测试通过；这些使用 mock，不代表 Dev 兼容。
- 单独 HTTP 诊断用例通过，断言的是上述真实不兼容可复现，同时确认 Dev 合法预检和 instances 读接口可响应。它不是“全部接口通过”的验收。
- 主工作区 Dev 未改动；修改在独立分支，包中提供真正可应用的 patch 和前端只读核验材料。
- C01 的具体冲突：旧 `RegularContainer(max_volume=100)` 快照含 `NaCl` / `catalyst` 的 µg 质量记录，新增容量验证没有可信密度就不能按体积上限验算。本地修复仅对旧体积规格允许记录未知体积；显式容量仍要求完整换算，混合记录的已知超量、数值溢出、模板/实例约束冲突继续拒绝并回滚。

建议讨论顺序：先将 C01 本地修复接入 #358，完成远端 CI 后合入；再落实 F01–F05 的正式前端最小运行链路；随后共同设计 M02/W01 的资源区间接口，接入 W02/W03/A01；最后在 R01/R02 基础上完善 I02/I03/A03。T01/T02 保留明确 TODO。每一轮关闭的是对应语义和验收项，不要求源码目录和 V1 完全相同。
