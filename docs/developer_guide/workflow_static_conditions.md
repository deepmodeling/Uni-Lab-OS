# @workflow 静态条件流程

本轮在 dev 的模板、任务快照、NodeRun / attempt 和调度器上增加条件执行。
设备仍经 runtime.v1 → Host → HostLink / ROS 2 执行。本轮实际验证了 HostLink 虚拟设备。

## v1 与原 dev 的区别

| 项目 | v1.0 | 原 dev |
| --- | --- | --- |
| 作者调用 | `reactor.action(...)`，设备由 `device(...)` 声明 | `ctx.run("reactor/action", params)` |
| 装饰器 | `@workflow(workflow_uuid=..., displayname=...)` | `@workflow(display_name=...)` |
| 构图 | 只解析源码 AST，不执行作者函数 | 执行一次函数，ctx 收集步骤声明 |
| 动作返回值 | 编译为节点结果引用 | `ctx.run` 返回步骤声明；`step_output` 可供 while 条件引用 |
| 原生 if/else | 编译为结构化条件区域 | 不支持动作结果驱动的原生 if；对结果判断真假在注册时报错 |
| 身份 | 工作流 UUID、节点 UUID 源码锚点 | 函数路径派生模板 UUID、步骤顺序派生节点 UUID |

两种动作调用形式都容易解析。影响可靠性的是能否在执行动作之前静态看到全部分支、
依赖和作用域。因此本轮采用 v1 的静态编译思路，但不替换 dev 调度器，也不复制 v1 的
完整作者 API。现有模板保持兼容，新模板显式使用 `compiler="ast"`。

builder 模式在注册时执行一次函数，此时动作还没有运行、没有结果。对 `ctx.run` 返回的步骤、
`step.output(...)` 引用或 `ctx.step_output` / `ctx.device_state` 条件判断真假、与常量比较大小或相等、
拼进 f-string，都会在注册时抛出 `TypeError` 并给出改法（以前 `if step:` 恒为真，else 分支被静默丢弃）。
按结果分支改用 `compiler="ast"`；按结果或设备状态循环用 `with ctx.loop_while(...)`。
引用本身照常放进参数和条件，入库时是普通 JSON。

## 新流程写法

作者层使用可读设备 ID，不要求写设备实例 UUID。`ctx.run("reactor/check_quality", ...)`
中的 `reactor` 是模板角色 ID，未指定绑定时默认匹配图中的同名 `device_id`。
Python 模板只定义默认设备 ID、默认动作参数和过程（顺序、条件、循环），不编写部署换绑逻辑。
用户在前端使用模板时选择实际设备、调整参数；前端把选择提交给后端实例化或保存工作流。
后端接收前端提交的绑定并统一解析动作目标与 `ctx.device_state("reactor", ...)` 状态条件，
校验后供调度器执行。仅用于状态读取、没有动作调用的设备同样提供给前端选择。
换绑作用于实例化的工作流，不改写设备包中的 Python 模板源码。
资源 UUID、工作流 UUID、节点 UUID 仍由系统内部维护，不作为作者复用流程的必填信息。
设备显示名只用于展示，不参与设备解析。复用流程需要目标设备提供相同的动作与状态契约。

| 层级 | 职责 |
| --- | --- |
| Python `@workflow` | 声明默认设备、默认参数、步骤和控制流 |
| 前端 | 展示模板、选择或更换实际设备、修改实例参数、提交运行 |
| 后端 | 接收并校验选择，统一处理动作和状态引用，生成任务快照并调度 |

```python
from unilabos.registry.workflows import workflow

@workflow(display_name="三轮质量检查", compiler="ast")
def check_quality(ctx):
    ctx.run("reactor/reset", {}, name="复位")
    with ctx.loop_for(3, name="检测 ×3"):
        quality = ctx.run("reactor/check_quality", {"threshold": 0.85}, name="检测")
        if quality.ready:
            ctx.run("reactor/report", {}, name="达标确认")
        else:
            ctx.run("reactor/polish", {}, name="未达标提纯")
    ctx.run("reactor/report", {}, name="汇总")
```

`quality.ready` 与 `quality["ready"]` 都是静态结果引用；它们不是构图时的实际检测值。
支持 `if/elif/else`、嵌套条件、`and/or/not`、`== != < <= > >=` 和链式比较，
以及原有 `ctx.loop_for` / `ctx.loop_while` 声明。条件可包围循环，也可位于循环体内。

动作参数支持 JSON 字面量、模块或闭包中的 JSON 常量、循环占位符，以及动作结果引用。
AST 模式可写 `result.field`、`result["field"]`、数组下标和整个 `result`；builder 模式
使用 `step.output("field")`。引用可嵌入字典或数组，调度器等来源节点成功后取值。
缺失字段会在调用设备前失败，返回数据中的引用标记或占位符不会再次求值。
不支持任意 helper 调用、赋值计算、原生 for/while、break/continue、return 或参数展开；
遇到不支持的语法给出函数名和相对源码行号，不回退执行函数体。
分支内定义的结果不能在其他分支或汇合点使用，循环体结果不能泄漏到循环外。

本入口用于已安装设备包：包模块仍按 dev 原方式导入。只有标记函数的构图是 AST 解析，
这不是用于不可信上传源码的沙箱；v1 的不导入上传模块入口尚未迁移。

## 条件协议与调度

条件节点 `type="condition"` 的参数为版本化 JSON：

```json
{
  "version": 1,
  "expression": {
    "field": {"node_uuid": "检测节点的 UUID"},
    "name": "ready"
  }
}
```

条件节点持久化 `return_info.return_value.matched`。分支成员使用
`execution_policy.condition_guards=[{"node_uuid":"条件节点 UUID","equals":true}]`。
嵌套成员继承外层条件。模板中先保存 `node_key`，实例化和组合展开时映射到实际 UUID。
条件读取依赖和分支依赖由执行计划自动补入，不能仅靠源码声明序碰巧等到结果。

只读取当前 attempt 的成功结果；字段缺失、来源未成功、类型不匹配或非布尔判定都失败。
不把数字 0/1、字符串、空对象解释成布尔值，也不执行表达式字符串。
未选分支记录 `skipped`，不申请设备资源、不派发动作；它在 DAG 中允许后续汇合继续。
条件判定和跳过不消费设备单步许可。选中的设备动作遵守原有暂停、单步和取消约束。

循环体每轮重新创建 attempt，条件重新读取该轮结果；跳过也保留 attempt 历史。
重启后已完成的条件沿用持久化结果；未完成的条件没有设备副作用，可恢复为 pending。
设备动作仍遵守 dev 的执行态未知与人工裁决规则。任务创建后修改工作流定义不改变旧任务快照。

## Demo 与边界

修改的是外部 `LabDeviceComplexWorkflowDemo` 的 `polish_until_ready` 一条模板，保留显示名
「循环演示：提纯达标」；其余四条流程保持原逻辑。它从纯度 0.4 开始，固定检测三轮：

| 轮次 | 检测 ready | 达标确认 | 未达标提纯 |
| --- | --- | --- | --- |
| 1 | false | skipped | succeeded，纯度变为 0.65 |
| 2 | false | skipped | succeeded，纯度变为 0.9 |
| 3 | true | succeeded | skipped |

最终 `polish_count=2`、`purity=0.9`。smoke 经注册表上报、模板实例化、任务提交和
node-runs 查询验证完整链路，同时运行原来的定次加样、等待升温、嵌套板孔、复杂流程。

后续已接入 parallel/group 与动态结果传参，见下节；资源占用区间、条件分支的库存数量预留、
完整作者输入输出 API、源码与画布双向编辑仍未完成。条件分支声明库存需求会被明确拒绝，避免给未选分支预留库存。
新条件协议也不与 v1 条件区域 JSON 直接互通；旧 v1 图需要后续专门适配。
框架 e2e 中外部 demo 的远程提交 pin 未改变，本轮测试使用本地修改的 demo checkout。


## 前端实例化入口

工作流页的「从设备包创建」读取 `GET /api/v1/workflows/template-catalog`，显示 `@workflow` 模板、默认设备 ID、动作参数和流程结构。设备目录直接使用物料权威中的 `resource_id`，不把显示名或 UUID 当成设备 ID；也允许输入未列出的远端设备 ID，设备在线状态和动作可用性由执行预检确认。

前端提交 `POST /api/v1/workflows/template-instances`：

```json
{
  "template_uuid": "模板 UUID",
  "name": "本次工作流名称",
  "bindings": {"reactor": "reactor-2"},
  "node_params": {"step-2": {"threshold": 0.8}},
  "site_binding_mode": "preserve"
}
```

- 每次成功创建都是新的工作流和节点身份，不覆盖已有实例。
- `bindings` 是前端实例配置；Python 源码仅保留默认设备 ID、默认参数和过程。
- `node_params` 只允许动作步骤，以模板步骤 key 定位。指定步骤的参数对象完整替换，未指定的步骤沿用默认值；不能通过该字段修改循环、条件或过程结构。
- 动作目标、设备状态条件一起换绑；结果条件、分支守卫、父子关系和执行依赖使用本次实例节点 UUID。
- 浏览器保留 Site 草稿，创建失败时清理本次新建的空工作流。创建不会启动设备，执行仍须单独提交任务。
- 原 `POST /api/v1/workflows/from-template` 保持模板＋绑定对应一个工作流的幂等行为，供既有脚本使用。前端不会回退调用这个接口，旧后端缺少新入口时明确报错。
- dev 工作流读取接口补充 `workflow_type=workflow`、当前修订对应的 `status` 和列表 `has_more`；存储和调度合同不变。

创建后的实例可通过下述运行入口执行。现有实例的二次编辑、循环内分支展开编辑，以及前端其他页面的 dev 协议迁移另行处理。


## dev 前端运行与任务控制

前端 developer-web 显式使用 dev 适配器。运行前检查使用工作流当前 revision，提交同一版本的任务；参数修改使已有预检失效，迟到的旧预检响应不会覆盖新参数。提交成功后「查看任务」直接进入本次任务。

```json
// POST /api/v1/workflows/{workflow_uuid}/run-preflight
{"run_mode": "step", "input": {}, "expected_revision": 2}

// POST /api/v1/workflow-tasks
{"workflow_uuid": "实例 UUID", "run_mode": "step", "input": {}, "expected_revision": 2, "idempotency_key": "本次运行的提交键"}

// POST /api/v1/workflow-tasks/{task_uuid}/commands
{"type": "step", "expected_revision": 0, "idempotency_key": "本次控制的提交键"}
```

任务创建的 `expected_revision` 是工作流修订号；命令的同名字段是任务 `control_revision`。命令只允许 pause / step / resume / cancel，版本冲突时重读事实，不能自动按新版本再次放行。相同提交在网络超时后重试复用幂等键；成功后的下一次主动运行生成新键。

`pause` 关闭新动作派发，已下发动作会继续完成；循环控制节点可保持 running，任务仍处于暂停派发状态。`step` 只放行一个动作，`resume` 恢复自动派发。取消后的任务状态与条件分支的 skipped 是不同事实，页面分别展示。

任务详情从 dev 的 NodeRun 读取全部 attempts，为每次执行补上逻辑节点的执行类型和拓扑序。每次执行的 ID、参数和返回值保持原值，循环中的成功及 skipped 记录都保留。进度仅使用各逻辑节点的当前 attempt；时间线注明第几次执行，设备 ID 来自任务冻结快照中的绑定。

`/api/v1/events` 只用于失效通知，页面重读 REST 权威状态。支持 dev 的 task / node_run / node_job / manual_confirmation / authoring 事件及 stream.reset；节点事件缺少任务 ID 时做全局失效，连接建立和重连后补读快照。反馈读取 `/workflow-node-jobs/{uuid}/feedback-history`，按 sequence 续读。

当前运行入口不支持额外库存换绑或高优先级调度；适配器明确拒绝非空换绑和高优先级请求，提交体不发送未支持字段。后端任务创建 DTO 拒绝额外字段，避免调用方误以为配置已生效。后续若新增优先级或库存业务，应先定义 dev 合同再开放界面选项。

浏览器验证使用两台虚拟反应釜中的 reactor-2，将检测阈值设为 0.8。正常运行得到 `[false, false, true]`、`polish_count=2`、`purity=0.9`；单步首次只完成复位动作，恢复后暂停不再派发新动作，再恢复可完成；另一次暂停态任务可取消。预检和控制面要求鉴权时，令牌只保存在当前浏览器会话。


## 并行分组与动态结果（迁移阶段 W1）

```python
@workflow(display_name="两路检测", compiler="ast")
def parallel_check(ctx):
    with ctx.parallel("并行检测"):
        with ctx.group("左路检测并记录"):
            left = ctx.run("reactor/check_quality", {"threshold": 0.8})
            ctx.run("recorder/report", {"value": left.purity})
        right = ctx.run("reactor-2/check_quality", {"threshold": 0.8})
    ctx.run("recorder/report", {"value": [left.purity, right.purity]})
```

`parallel` 的直接成员从同一前驱出发，结束后等待全部分支；`group` 内部顺序执行。
组框只承载展示与层级，不产生 Job。可否同时调用设备仍受 dev 的设备与物料锁约束。
并行分支不能读取兄弟分支尚未完成的结果，汇合后可以；条件分支结果和循环体结果仍不可
泄漏到各自作用域之外。循环内参数只取当前 attempt 的成功结果。

模板实例接口把步骤 key 换为该实例的节点 UUID；组合展开再次映射内部引用。
图校验和计划编译自动补齐结果依赖，保存时同时检查结果、条件、顺序依赖的环。
模板弹窗支持分组展示，含结果引用的参数只读，常量参数可编辑；接口拒绝删除或改写引用。

2026-11-11 本地验证：相关后端 240 项、前端 core 551 项、页面 19 项通过，TypeScript /
Vite 构建通过。新增并发测试用可控执行器证明返回前两路已同时派发及汇合等待；HostLink
测试验证逐轮动态传参、嵌套路径、缺字段不调用设备和循环内分组。新增并行模板尚未做
真实浏览器 + 多 slave 联调；此前条件 demo 的浏览器证据继续独立保留。
完整作者输入输出合同、源码往返及剩余业务见 workflow_v1_migration.md，W1 尚未关闭。
