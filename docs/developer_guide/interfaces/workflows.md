# 工作流、Site 绑定、执行与干预

接口前缀为 `/api/v1`。本章的 Workflow HTTP 结果需同时检查 HTTP 状态和 `code`；
字段类型与路由见 [Workflow API 实现](https://github.com/deepmodeling/Uni-Lab-OS/blob/862ce87a38222ac2d51739f2f89e5daecd3cd907/unilabos/server/api/runtime/workflow.py)
和 [工作流协议模型](https://github.com/deepmodeling/Uni-Lab-OS/blob/862ce87a38222ac2d51739f2f89e5daecd3cd907/unilabos/protocol/runtime/workflow.py)。

## 先区分定义、运行与尝试

| 身份 | 生命周期 | 观测入口 |
| --- | --- | --- |
| template_uuid | 设备包声明的可复用模板 | registry/workflow-templates |
| workflow_uuid | 绑定设备后的图定义，有 revision | workflows/{uuid}、/graph |
| task_uuid | 一次提交，持有该次运行的工作流快照 | workflow-tasks/{uuid} |
| node_run_uuid | 该任务中的逻辑节点运行 | workflow-tasks/{uuid}/node-runs |
| job_uuid | 一次物理动作尝试；retry 会产生新 job | workflow-node-jobs/{uuid} |
| attempt_group_uuid / retry_of_job_uuid | 关联尝试组与重试来源 | job / 执行命令 |
| decision_uuid / confirmation_uuid | 异常决策 / 显式人工确认单 | error-decisions / workflow-manual-confirmations |

不能拿设备名字当作 job 去重键，也不能为了刷新结果重新提交一个 task。
界面按 node-run 展示逻辑进度，展开后显示 attempts；失败、人工替换、重试应保留完整链路。
`attempt_no` 是尝试编号，`retry_count` 是重试计数；循环的下一轮不等同于失败重试。

## 模板 → 图 → 运行

### 设备包模板导入

1. GET `/registry/workflow-templates`，展示模板角色、guide 和步骤说明。
2. POST `/workflows/from-template`，body：

   ```json
   {
     "template_uuid": "实际模板UUID",
     "bindings": {"模板角色ID": "实际device_id"},
     "name": "本次实验工作流",
     "site_binding_mode": "preserve"
   }
   ```

3. 成功 HTTP 201，读取 `data.workflow.uuid`。同一模板与同一组角色绑定使用稳定工作流身份，
   再次导入会 upsert；**不是每次都创建一个独立副本**。
4. GET `/workflows/{uuid}/graph` 和工作流详情，编辑并确认输入后 PUT 全图。
5. POST `/workflow-tasks`，拿到 `data.uuid` 才开始观测这次任务。

显式设备角色默认对应同名 device_id；类角色只有恰好一个候选设备时才能自动绑定，
否则必须提供 bindings。缺设备时不得选择列表第一项凑数。

### 自建与编辑工作流

POST `/workflows` 至少提供 name；浏览器通常不自定 workflow_uuid。
PUT `/workflows/{uuid}` 更新元数据，PUT `/workflows/{uuid}/graph` 是**带 revision 的全图替换/协调**，不是增量 append：

```text
GraphWriteRequest = {
  revision: 当前工作流 revision（整数 >= 1）,
  nodes: WorkflowNodeWrite[],
  edges: WorkflowEdgeWrite[],
  site_binding_mode: "preserve" | "resolve"
}
```

节点包括 uuid、name、type、parent_uuid、material_uuid、action_name/type、param、
execution_policy、meta_data、pose、disabled、minimized 等。
UUID 字段必须是非 nil 的有效 UUID，disabled/minimized 必须是 JSON 布尔值，不接受字符串。
公共读 DTO 不返回节点内部默认 status；参数编辑不要把运行状态混回定义。

边的 source/target_node_uuid 和 source/target_handle_uuid 必须引用实际图/模板身份。
设备包 `@workflow` 顺序步骤用 `execution_policy.depends_on` 表达顺序，可能没有 handle 连线；
空 edges 不必然表示可以并行。

节点 param 就是运行参数。当前整图任务提交不提供自由的 task input/output 通道；
不要在 POST task 时放一份未被绑定的 `input`，以为会覆盖节点参数。

## 工作流即节点：声明 I/O、发布与插入

一个工作流声明输入输出后可显式**发布**为不可变修订，再作为组合调用插入另一个工作流。
身份是 `(workflow_uuid, revision)` 加内容摘要 `source_hash`（语义图规范 JSON 的 `sha256:`），
对外合同由 `contract_digest` 标识，没有单独的 contract_uuid。

1. PUT `/workflows/{uuid}/io`：

   ```text
   {
     revision: 当前 revision,
     input_contract:  {version: 1, parameters: [{name, schema, required?, default?, title?, description?}]},
     output_contract: {version: 1, outputs: [{name, schema, implicit?, title?, description?}]},
     output_bindings: {输出名: {kind: "node_output", workflow_node_uuid, source_handle_uuid}
                              | {kind: "workflow_input", parameter}},
     node_input_bindings: {node_uuid: {handle_uuid: {parameter: 输入名}}},
     node_params?: {node_uuid: {data_key: 静态值}}
   }
   ```

   写入服务端保留的 `meta_data.unilab`（普通 PUT 工作流 / PUT graph 不能写、也不会清掉它），
   按整图（含节点 / Handle 模板）校验后 revision +1，返回更新后的整图。`node_input_bindings`
   是完整替换；绑定的输入成为唯一 Provider，节点 param 里的同名静态值被移除。
   解绑必填输入时用 `node_params` 同时给回静态值。绑定引用 Handle UUID，因此只有引用了节点模板的节点能绑定。
   校验失败 code=1000，消息含 `workflow_io_invalid（路径）`；revision 不符 code=3003。
2. POST `/workflows/{uuid}/publications`，body `{revision}`（必须等于当前 revision）：
   冻结 `{workflow, nodes, edges, node_templates, handle_templates, applied_source}`，
   并投影为一个 `node_type="workflow"` 的节点模板校验。成功 201；同一修订再次发布返回同一条（幂等），
   非当前修订且未发布过返回 3003。GET `/workflows/{uuid}/publications` 按 revision 倒序列出；
   GET `/workflow-publications` 给每个未删除工作流的最新发布（调色板数据源）。
   返回项含 uuid、workflow_uuid、revision、name、source_hash、contract_digest、inputs、outputs、
   `node_template`（schema 为 JSON 文本，与 `/graph` 一致）和 `handle_templates`；不含快照正文。
3. POST `/workflows/{parent_uuid}/composite-invocations`：

   ```text
   {revision: 父图 revision, child_workflow_uuid, child_revision?（缺省=最新发布）,
    invocation_uuid?, pose?, arguments?: {输入名: 静态值 | {kind: "workflow_input", parameter: 父输入名}}}
   ```

   子工作流按所选发布的冻结快照**内联展开**：父图多一个组合调用节点（无 Job，仅画布/追溯/边界映射）
   和它展开出的普通节点与连线，节点 uuid 由调用 uuid 派生。展开节点各自申请设备 / 物料锁，
   没有组级锁。静态实参下推到内部节点 param；未给出的可选输入在父图里不存在。
   成功 201，返回 `{invocation_uuid, contract_pin, graph}`，父图 revision +1。
   子工作流未发布 code=3002；递归（插入自身、子工作流内含父工作流）、缺必填实参、实参类型不符等
   展开诊断 code=1000，消息含诊断码与路径（如 `composite_recursive_reference（/composite/child_workflow_uuid）`）。
   嵌套组合按各调用固定的发布修订重新展开；同一子工作流在嵌套中固定了不同修订会被拒绝。

   子工作流可以是画布形状（节点不引用模板、用 `execution_policy.depends_on` 排序）：展开时 depends_on
   换成展开后的节点身份。父图里 depends_on 指向组合调用节点，等价于依赖它展开出的全部叶子；
   组合调用节点自己的 depends_on 施加到全部叶子上（执行计划里写回平面化后的 depends_on）。
   不引用模板的节点不能做 I/O 绑定，也不参与 ready 连线的结构映射。

已发布工作流模板的 uuid 只由 workflow_uuid 派生：插入更新的修订会更新共享模板行；
仍固定旧修订、且输入输出集合不同的父图在建任务时 fail-closed（code=1000），需重新插入。

## Site 默认值与确认规则

**默认 T1 是一个待绑定标签，不是已确认的 Site 身份。**
只有动作声明标记为 SiteSlot（placeholder `unilabos_sites`）的参数才走 Site 解析；
普通字符串参数即使等于 T1，也不能随意替换成 UUID。

| 调用方 | 保存模式 | 应做的事 |
| --- | --- | --- |
| 浏览器模板卡片 / 画布草稿 | preserve | 展示预填标签，提示绑定物料与 Site，要求用户确认 |
| 浏览器确认后 | 保存实际 site_uuid | 参数仍为 UUID，并保留所属物料；执行前检查绑定是否仍有效 |
| 脚本/API 导入 | resolve（默认） | 权威自动解析唯一匹配，将标签替换为 site_uuid 并记录绑定元数据 |

preserve 不是“跳过所有运行校验”，而是允许保存未完成草稿。
前端工作流 card 的待填数字应统计“缺少必填项 + 未确认的资源/Site 绑定”，
**不能只检查输入框是否非空**。当前没有单独的“card 待填数量”HTTP 路由；
此计数和确认交互是前端责任，不能宣称后端已返回该数字。

resolve 的范围按优先级确定：

1. `node.meta_data.site_binding_owners[参数名]`：显式 owner material_uuid，仅该 owner 的 Site。
2. 动作存在 `param.mount_resource`：按其 uuid 或唯一 name/id 确定目标物料，仅该 owner 的 Site。
3. 否则在目标设备的物料子树查找，设备由 meta_data.target_device_id 或 material_uuid 确定。

不能从任意 ResourceSlot 推断目标父物料，因为它可能表示来源物料。
匹配到 0 个或多个 Site 都报错；格式上是 UUID 但已失效，或不属于目标范围，也报错，
**禁止把失效 UUID 再当 label 回退**。

解析成功后写入：

```text
node.param[参数名] = site_uuid
node.meta_data.site_bindings[参数名] = {
  site_uuid, owner_material_uuid, device_id, action_name
}
```

导入先解析所有节点再写图，失败不会保存一半。
已禁用节点（含被禁用祖先）跳过；明确由上游 handle 映射的动态参数不在导入时强行静态绑定。
动作声明优先使用在线 endpoint 的 active capability，包含 Workstation 子设备，
再查询已知注册表定义。能力尚未就绪时应等待或 preserve 保存草稿，不能静默认为“该动作没有 Site”。

Site 绑定只确认身份与范围，不搬动物料，也不预留该 Site；占用情况仍需在执行时校验。

## 自动、逐步、图内单点与设备单点

| 类型 | POST /workflow-tasks 参数 | 行为 |
| --- | --- | --- |
| 整图自动 | workflow_uuid，run_mode=normal | 调度器正常运行 DAG |
| 整图逐步 | workflow_uuid，run_mode=step | 创建暂停任务，每次 step 放行一个动作尝试 |
| 图内单点 | workflow_uuid，run_mode=single_node，target_node_uuid | 执行指定图节点；不是从该点继续整图 |
| 设备页单点 | execution_kind=ad_hoc_device_action，device_id、action_name、param | 新建独立单 job 任务，同样经过调度与历史 |

设备单点可携带 action_type、execution_policy、execution_timeout_seconds、idempotency_key。
整图任务同样可带 `idempotency_key`（1..200 字符）：同一次提交的按钮重发、网络重试复用同一键，只建一个任务、
只调度一次；同键但 workflow_uuid / run_mode / target_node_uuid 不同按冲突拒绝。不带键时每次提交都是新任务。
它不是直接向 Host 调用函数，不能绕开已声明的物料锁、错误策略或库存需求。

### 步进与切回自动

```json
{"workflow_uuid":"实际工作流UUID","run_mode":"step"}
```

任务初始 `control_status="paused"`。每次先 GET 当前任务，再 POST
`/workflow-tasks/{task_uuid}/commands`：

```json
{"type":"step","expected_revision":0,"idempotency_key":"本次点击的稳定唯一键"}
```

这里的 0 只是新任务示例，实际必须取任务的 **control_revision**，不是工作流 revision。

- 同一次点击的网络重试复用同一键、同一 body，只会放行一次。
- 两个页面拿同一 revision 但使用不同键并发点击，最多一个成功，另一个 code=3003 后刷新。
- 动作未完成时继续 step 不能积攒许可；一个许可对应一个 physical attempt。
- 如果这次失败需要 retry，新的 attempt 仍需要下一次 step。
- `type="resume"` 切回 normal，继续**同一个 task / DAG**，不是新建 task；执行中也可请求恢复自动。
- `type="pause"` 把自动运行中的任务转为逐步并关上派发闸门：已下发的动作照常跑完、继续持有锁；
  尚未下发的节点（包括排队等锁的）不再派发，排队申请撤回、让给其它任务，之后由 step 逐个放行或
  resume 恢复自动。逐步执行中的那一步照常跑完，完成时转 paused；等待裁决的任务裁决后停在 paused。
  已暂停再 pause、任务已结束时返回 3003。
- `type="cancel"` 取消任务（整图与单点设备动作都适用，暂停中也可以）：任务立即进入 `canceling`，不再派发；
  排队等锁、等单步的节点直接收为 canceled；已下发的动作收到取消请求（job 显示 `cancel_requested`），
  **设备锁保持到它真实返回**——协程驱动在下一个 await 处被打断，同步驱动跑在线程里无法打断，只能等它返回
  （驱动可调用 `context.raise_if_cancelled()` 提前结束）。全部返回后任务落 `canceled`。
  取消中再取消、任务已结束时返回 3003。
- 当前命令类型是 step / resume / pause / cancel。

重启后的执行状态未知会进入 waiting_reconciliation / execution_unknown 等状态，
此时不能用 resume 自动重做设备动作，应先核对实际执行结果并处理干预。

## 错误、人工确认与告警：三个入口

### 动作异常决策

1. 驱动报错，执行层保留 error gate，记录失败信息，通知后端出现待决策。
2. 前端 GET `/error-decisions`，使用当前 options、超时和重试上限展示选项；没有额外的 `/pending` 子路径。
3. POST `/error-decisions/{decision_id}`，提交 action、reason；人工替换还需 result。
4. 后端把决策变成带 scheduler revision 的 runtime.v1 命令，Host 收到后释放闸门。
5. 非人工替换路径上报失败终态；调度器消费 `return_info.error_resolution.selected_action`，
   决定追加 retry attempt、跳过或中止。**本版 retry attempt 是由终态消费推进，不是驱动内部重跑。**

请求模型 ErrorDecision 还声明 option、scheduler_updated、job_id、device_id、extra；
浏览器应使用待决策返回的真实身份，不伪造已完成调度更新。
建议人工结果放在顶层 result，避免 option 与顶层字段同时给出相互矛盾的数据。
正常成功响应为 `{"decision_id":"...","status":"resolved"}`，不是 Workflow code 信封。

| 决策 | 控制命令 / 结果 |
| --- | --- |
| operator_intervention 且 result 非 null | replace_result，用人工结果完成，同时保留替换链 |
| retry / skip / abort 等当前允许选项 | release_failed，带 selected_action；由后端处理后续 DAG/attempt |
| wait（软超时允许时） | resume_pending，动作仍在执行，关闭干预闸门并重新计时 |

不要永远显示 retry：后端会按 options 和 max_retries 验证。
execution_timeout 是软超时提示/决策，不能等同于执行线程已退出；取消也不保证强杀 Python 线程或硬件。
完整异常响应、失败记录、result replacement-chain 与新 job 都应可追溯，人工干预不等于删除原始错误。

### 工作流人工确认节点

GET `/workflow-tasks/{task_uuid}/manual-confirmations` 或按 confirmation_uuid 读取单项，
POST `/workflow-manual-confirmations/{confirmation_uuid}/decision`。
body 为 action、confirmed_by、comment、decision_idempotency_key。
另有 task 维度 decision 路径，会验证确认单确实属于该任务。
确认人受 assignee 约束；已决定或幂等键冲突不能重复推进。
HTTP 记录确认后由调度器消费，不是 handler 内直接执行下一个设备函数。

### 设备状态异常

`status-incidents` 是 status_policy 产生的状态事件/联锁，独立于 action error policy。
阻塞联锁与非阻塞告警不可混用；建议动作是 recommendation，不代表设备自行开始恢复动作。
前端按 incident 实际状态和允许操作展示，不把一次普通告警等同于失败 job。

## 结果、反馈与事件刷新

| 读取目的 | 路由 |
| --- | --- |
| 任务概览 | GET /workflow-tasks/{task_uuid} |
| 逻辑节点及尝试 | GET /workflow-tasks/{task_uuid}/node-runs；GET /workflow-node-runs/{run_uuid} |
| 平铺物理执行 | GET /workflow-tasks/{task_uuid}/jobs；GET /workflow-node-jobs/{job_uuid} |
| 结果历史 | GET /workflow-node-jobs/{job_uuid}/results |
| 反馈历史 | GET /workflow-node-jobs/{job_uuid}/feedback-history |
| 人工干预记录 | GET /workflow-tasks/{task_uuid}/interventions |

订阅 `/events` 收到变更后再读取上述 DTO；不要把 event 通知当完整 task 覆盖本地缓存。
程序收到 HTTP 201 只能显示“已提交”，完成应以任务/job 的实际终态为准。

## Python authoring 与子工作流

已存图的 Python 编辑走 authoring，不是直接在设备进程 eval 任意字符串：

1. GET `/workflows/{uuid}/authoring` 获取当前草稿、候选与编译诊断。
2. PUT `/authoring/draft`，携带 python_source、expected_draft_hash（首次可 null，但字段必须存在）、
   expected_workflow_revision。
3. 等待当前草稿的候选编译结果；如有诊断先修复，不使用旧 candidate。
4. POST `/authoring/apply`，同时提供 expected_draft_hash、expected_candidate_hash、expected_workflow_revision。
5. 应用成功后才是新的图修订；再次 GET 核对。哈希格式 `sha256:` 加 64 位小写十六进制。

这三个比较分别防止草稿、编译候选和图的并发覆盖，不能只传一个 revision。

设备包 `@workflow` 是另一种声明入口：ctx.run/ctx.run_template 声明步骤，
ctx.loop_for/ctx.loop_while 声明由调度器执行的循环，ctx.device_state/ctx.step_output 构造条件。
循环变量 `{{loop.index}}`、`{{loop.iteration}}`、`{{loop.count}}` 由运行时处理。
这不等同于支持把任意 Python while/try/except 直接变成可恢复的工作流。

已发布子工作流通过冻结的发布快照与输入/输出 handle 合同展开，不能只凭函数名调用（见上文“工作流即节点”）。
**本轮没有升级嵌套作用域锁**：父组资源预占、组内并行竞争等讨论不能当成本版已兑现能力。
跨设备共享物料声明依然使用 `@action(materials_need_lock=["输入参数名"])`，让调度器集中处理。

## 源码与回归依据

- [HTTP 请求与返回信封](https://github.com/deepmodeling/Uni-Lab-OS/blob/862ce87a38222ac2d51739f2f89e5daecd3cd907/unilabos/server/api/runtime/workflow.py)
- [Site 解析](https://github.com/deepmodeling/Uni-Lab-OS/blob/862ce87a38222ac2d51739f2f89e5daecd3cd907/unilabos/server/services/runtime/workflow/site_bindings.py)
- [后端错误决策](https://github.com/deepmodeling/Uni-Lab-OS/blob/862ce87a38222ac2d51739f2f89e5daecd3cd907/unilabos/server/backend/edge_control.py)
- [步进、多页面与重启测试](https://github.com/deepmodeling/Uni-Lab-OS/blob/862ce87a38222ac2d51739f2f89e5daecd3cd907/tests/server/test_workflow_step_execution.py)
- [模板声明](https://github.com/deepmodeling/Uni-Lab-OS/blob/862ce87a38222ac2d51739f2f89e5daecd3cd907/unilabos/registry/workflows.py)
