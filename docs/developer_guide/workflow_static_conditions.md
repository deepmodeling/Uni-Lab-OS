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
| 原生 if/else | 编译为结构化条件区域 | 不支持动作结果驱动的原生 if（统一后与 V1 相同：只按源码编译） |
| 身份 | 工作流 UUID、节点 UUID 源码锚点 | 函数路径派生模板 UUID、步骤顺序派生节点 UUID |

两种动作调用形式都容易解析。影响可靠性的是能否在执行动作之前静态看到全部分支、
依赖和作用域。因此本轮采用 v1 的静态编译思路，但不替换 dev 调度器，也不复制 v1 的
完整作者 API。工作流按源码静态编译（与 V1 相同，只认 `if` / `with` 结构），不执行作者函数，
`@workflow` 没有选择编译方式的参数。原 dev 的 builder 模式（注册时执行一次函数收集步骤）已移除，
`compiler` 参数随之删除：旧驱动包里写的 `compiler="ast"` 照常加载，值被忽略，每个定义告警一次；
`compiler="builder"` 或其他取值报错并提示去掉该参数（装饰器与注册表 AST 扫描同一规则）。
7 个 demo 仓库与示例驱动包的 31 个模板全部按源码静态编译，原来走 builder 的 27 个与 builder
收集出的步骤逐项一致。

### 辅助函数

编译前把辅助函数调用替换成它的源码（`registry/workflow_helpers.py`），不执行作者代码：

- 步骤辅助函数：第一个实参（或任一实参）是 `ctx` 的调用，例如 `_reset(ctx)`，替换成函数体里的步骤语句；
  `m = _measure(ctx)` 取函数末尾 `return` 的步骤，之后可以照常写 `m.purity`、`if m.ok:`。
- 值辅助函数：不接收 `ctx`、函数体只有一句 `return 表达式`，例如 `_water(500)`，替换成代入实参后的表达式。
- 支持关键字实参与缺省值、辅助函数嵌套调用、从其它模块导入的辅助函数；辅助函数引用的模块常量随之引入
  （与模板里同名不同值时自动改名），局部名改成不会冲突的新名字。
- 不支持递归、`*args` / `**kwargs`、值辅助函数里的多条语句；报错指向模板里的调用行。
  展开出的步骤源码范围记在调用行，节点 UUID 锚点只认模板里亲手写的语句。

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

@workflow(display_name="三轮质量检查")
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
原生 `while <条件>:` 与 `for i in range(...)`（见下节「循环与跨轮携带的变量」），以及原有
`ctx.loop_for` / `ctx.loop_while` 声明。条件可包围循环，也可位于循环体内。

动作参数支持 JSON 字面量、模块或闭包中的 JSON 常量、循环占位符，以及动作结果引用。
可写 `result.field`、`result["field"]`、`result.output("field")`、数组下标和整个 `result`。引用可嵌入字典或数组，调度器等来源节点成功后取值。
缺失字段会在调用设备前失败，返回数据中的引用标记或占位符不会再次求值。
变量可以赋值为动作结果的字段、字面量、工作流输入或其他变量（`volume = dosed.next_volume`）；
辅助函数按上文规则展开；除此之外不支持其它函数调用、运算（`x = x + 1`）、`break` / `continue`、
循环的 `else`、提前 return 或参数展开；遇到不支持的语法给出函数名和相对源码行号，不回退执行函数体。
分支内定义的结果不能在其他分支或汇合点使用，循环体结果不能泄漏到循环外（循环携带的变量除外，
见下节）；分支外已有的名字不能在分支里改写（分支外的取值会不确定）。

本入口用于已安装设备包：包模块仍按 dev 原方式导入。只有标记函数的构图是 AST 解析，
这不是用于不可信上传源码的沙箱；v1 的不导入上传模块入口尚未迁移。

## 循环与跨轮携带的变量

循环直接写 Python 的 `while` / `for`，编译成调度器逐轮执行的循环（不在构图时展开，函数体不执行）：

```python
@workflow(display_name="加倍直到够量")
def double_until_enough(ctx, *, start: float = 1.0, rounds: int = 3):
    volume = start                       # 循环前赋值……
    enough = False
    while not enough and volume < 100:   # 每轮开始前判定，读本轮的 volume / enough
        dosed = ctx.run("pump/dose", {"volume": volume}, name="加样")
        if volume >= 4:
            ctx.run("recorder/note", {"text": "大体积"}, name="记录")
        volume = dosed.next_volume       # ……循环体里重新赋值：这就是下一轮的值
        enough = dosed.enough
    ctx.run("pump/dose", {"volume": volume}, name="收尾")   # 循环之后：最终值

    for i in range(rounds):              # 轮数：整数字面量或工作流输入
        ctx.run("recorder/note", {"text": i}, name="编号")   # i 同 {{loop.index}}
```

- `while <条件>:` 的条件与 `if` 同一套表达式（动作结果、工作流输入、变量、比较、`and` / `or` / `not`），
  每轮开始前求值，假就结束。源码里没有上限，按默认 1000 轮（`DEFAULT_MAX_ITERATIONS`）：到了还没结束
  就按失败收敛（`while 循环达到 max_iterations=1000 仍未结束`）。条件读不到设备状态；等设备状态仍用
  `with ctx.loop_while(ctx.device_state(...), interval_seconds=...)`。
- `for i in range(n)`：`n` 是整数字面量（含模块常量、保存字面量的变量）或工作流输入（建任务时换成输入值），
  至少一轮；`range(start, stop)` 只接受整数字面量。`i` 是本轮的循环变量（`range(n)` 时与 `{{loop.index}}`
  相同，`range(start, stop)` 时是 `start + index`），可以作动作参数、写进 `if` / 内层 `while` 条件；循环体里
  不能改写它，循环结束后不能再读。`for x in <列表>` 暂不支持。
- **携带的变量**：循环前已有（字面量、工作流输入、之前动作结果的字段、外层循环携带的变量）、在循环体里
  重新赋值的名字逐轮携带。循环前的值是第一轮的值；循环体里在重新赋值之前读到的是本轮的值，之后读到的是
  新值；本轮循环体全部成功后，循环体末尾的值就是下一轮的值（没有重新赋值的保持不变）；循环结束后这个名字
  就是最终值。赋值只能是动作结果的字段、字面量、工作流输入或其他变量，不能做运算（`x = x + 1` 在编译时
  报错，需要计算的值让设备动作返回）。整个动作结果也可以携带：循环前 `m = ctx.run(...)`、循环体里再
  `m = ctx.run(...)`，`while not m.ok:` 就是“至少测一次、达标即停”。
- 携带的变量只能在每轮都会执行的位置重新赋值：直接写在循环体里，或在 `ctx.group` / `ctx.lock` 块、
  内层循环里（内层循环结束后的最终值就是外层这一轮的值）。写在 `if` / `elif` / `else` 或 `ctx.parallel`
  分支里会让下一轮的值取决于走了哪条分支，编译时拒绝——需要分支各自的值时在分支里用新名字。
  `with ctx.loop_for` / `ctx.loop_while` 的循环体不携带变量，也不能改写循环外的名字。
- 不支持 `break` / `continue`（用 `while` 条件结束循环，用 `if` 跳过本轮的步骤）和循环的 `else` 子句。

V1 的 `with repeat_until(max_iterations=…, carry={...}) as loop: … loop.next(...); until(cond)` 对应
“循环前赋初值 + `while not <cond>:` + 循环体里重新赋值”；V1 的标记函数语法不移植。原有的
`with ctx.loop_for(n):` / `with ctx.loop_while(ctx.device_state(...) / ctx.step_output(...)):` 写法照常可用，
不携带变量。

运行与持久化：携带的变量是循环节点自己的结果 `return_value.carry`。每轮开始时调度器把本轮的值随轮次
写进循环节点运行的 `control_data.loop.carry`（与 `iteration` 同一事务），本轮结束后按 `carry_next` 读本轮
已落库的结果算出下一轮；进程重启后从 `control_data.loop.carry` 续用，本轮结束前重启则按落库结果重算，
得到同一个值。外层循环开始新一轮时内层循环从初值重新开始。初值或下一轮的值读不到（字段缺失等）时循环
按失败收敛，不调用后续设备动作。

节点参数（`type="loop"` 的 `param`，模板阶段用步骤 key，实例化后换成节点 UUID）：

```json
{
  "mode": "while",
  "expression": {"op": "and", "args": [
    {"not": {"field": {"field": {"node_uuid": "循环节点 UUID"}, "name": "carry"}, "name": "enough"}},
    {"op": "lt", "args": [{"field": {"field": {"node_uuid": "循环节点 UUID"}, "name": "carry"}, "name": "volume"},
                          {"lit": 100}]}]},
  "max_iterations": 1000,
  "carry": {"volume": {"kind": "workflow_input", "parameter": "start"}, "enough": false},
  "carry_next": {"volume": {"kind": "node_output", "workflow_node_uuid": "加样节点 UUID", "data_key": "next_volume"},
                 "enough": {"kind": "node_output", "workflow_node_uuid": "加样节点 UUID", "data_key": "enough"}}
}
```

`for` 循环是 `{"mode": "for", "count": 3 或 {"kind": "workflow_input", "parameter": "rounds"}, "start": 0}`。
动作参数里读携带的变量是指向循环节点的普通结果引用
`{"kind": "node_output", "workflow_node_uuid": "循环节点 UUID", "data_key": "carry.volume"}`（for 循环变量是
`"data_key": "value"`）；读取方在该循环体内时取本轮值，在循环之后取最终值。外层循环与循环体之间的这类引用
不产生执行依赖（循环体本来就在循环里），循环之后的读取照常依赖循环节点。原生 while / for range 循环暂不能
由图生成源码（`/authoring/generate-python` 返回 `graph_not_representable`）。

## 人工装入 / 取走 / 补量（material_io）

`ctx.material_io(...)` 声明一步人工物料操作：执行到这里停下等人确认，确认后由权威在库存里装入、取走或补量，
不派发任何设备动作（只把库存变化同步到所属设备的台面）。它和 `ctx.run` 一样返回步骤，结果可以被后面的步骤引用；全部参数都是关键字参数。

```python
from unilabos.registry.workflows import workflow


@workflow("换吸头盒并补液")
def refresh(ctx, *, volume: float = 20.0):
    # 把一只新吸头盒放到 deck 的 A1，后面的动作用 box.material 引用这件新物料
    box = ctx.material_io(mode="load", mount="deck", site="A1", resource_template="tip_box_96",
                          material_name="吸头盒-新", assignee_user_ids=["operator-1"])
    ctx.run("liquid_handler/pick_up_tips", {"tip_rack": box.material})
    # 原位补液：只写加量，位置不变、不新建物料
    ctx.material_io(mode="refill", mount="reagent_rack", site="B1", material="solvent_bottle",
                    reagent={"quantity": volume, "quantity_unit": "mL"}, timeout_seconds=600)
    # 用完取走：软删除盒子与它的内容
    ctx.material_io(mode="unload", mount="deck", site="A1", material=box.material,
                    disposition="soft_delete", scope="material_with_contents", name="收走旧盒")
```

| 参数 | load | unload | refill | 说明 |
| --- | --- | --- | --- | --- |
| `mode` | `"load"` | `"unload"` | `"refill"` | 字面量 |
| `mount` | 必填 | 必填 | 必填 | 挂载物料：`{"uuid": ...}`、物料 UUID 或物料业务 ID（`resource_id`）；可用工作流输入 |
| `site` | 必填 | 必填 | 必填 | `mount` 上的库位名（不区分大小写）或库位 UUID；可用工作流输入。调度器锁定的就是它所属的 `mount` |
| `resource_template` | 必填 | — | — | 新物料的资源模板名或模板 UUID。注册表同步的模板（名字就是注册表资源条目 id）建出整棵 PLR 树，带默认子物料（如吸头盒里的吸头）；`mount` 属于设备时必须是这种模板，否则起跑即失败 |
| `material` | — | 必填 | 必填 | 要取走 / 补量的物料，必须正在 `site` 上；可以是前面步骤的结果（`box.material`）或工作流输入 |
| `material_name` / `barcode` | 可选 | — | — | 新物料名（缺省按模板短名加本地时间生成）、条码（不能带首尾空白） |
| `reagent` | 可选 | — | 必填 | 对象字面量；`quantity` 为正数（可用工作流输入）+ `quantity_unit`。load 另要 `reagent_info_uuid`，或 `name` / `cas`（已登记的 CAS 或唯一同名身份直接复用，没有就按 `name` 新建）；refill 只能有 `quantity` / `quantity_unit`，换了一瓶请用 unload 再 load |
| `disposition` / `scope` | — | 必填 | — | `detach`（解除挂载，物料留在库存）/ `soft_delete`；`material_only` / `material_with_contents`。`detach` 只支持 `material_only`，`material_only` 要求物料没有子物料和内容物 |
| `timeout_seconds` | 可选 | 可选 | 可选 | 1..86400，缺省 3600；超时节点失败、库存不变 |
| `assignee_user_ids` | 可选 | 可选 | 可选 | 与人工确认同一规则；`ctx.material_io` 缺省写入 `[]`（任何人可确认），画布节点必须显式给出 |
| `name` / `description` | 可选 | 可选 | 可选 | 与 `ctx.run` 一样是步骤显示名与说明；缺省名为「人工装入」「人工取走」「人工补量」 |

这些规则在编译源码、保存整图（含画布节点）和调度器起跑时各校验一次（`protocol/runtime/material_io.py`），
不符合时报错指向源码行。节点落成 `type="material_io"`、不带 `material_uuid`；`node_output` 引用 material_io
步骤的结果与引用动作结果一样参与执行依赖。节点模板面板里有 `host_node` 名下的「人工物料」结构模板
（`node_type=material_io`，带一对 ready 连接点）；它的 `goal_default` 只带模式与确认设置，保存前要填好库位与该模式的
必填字段。`/authoring/generate-python` 把 material_io 节点生成 `ctx.material_io(...)`。

库存写好后，权威把变化同步到挂载物料所属设备（沿父链往上最近的设备）的台面：load 让设备把新物料（连同子物料）挂到
库位上，unload 让设备把物料从台面移除，refill 让设备重载这只容器的内容物。结果在步骤输出的 `device_sync`（`ok` /
`skipped` 与原因）；挂载物料不属于任何设备（普通货架）时 `skipped`。与 transfer 一样，同步失败不回滚库存，但设备确认前
这一步不算完成：步骤保持运行，按同一命令身份重发（不会多装一件、不会多加一次量），设备回来后才成功；设备一直回不来就取消
任务，库存保持已写入。

运行时的锁定、确认、库存写入、台面同步与恢复见 [调度与执行架构](scheduling_and_execution.md#人工装入--取走--补量material_io)。
material_io 是作者显式写出的独立一步，和运行中缺料的动作执行前预检决策（`stage=preflight`）互不依赖；V1 的
`material_source(supports_replenishment=True)` + `sufficient` 补料分支不移植。

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

`/api/v1/events` 只用于失效通知，页面重读 REST 权威状态。支持 dev 的 task / node_run / node_job / manual_confirmation / authoring 事件及 stream.reset；节点事件缺少任务 ID 时做全局失效，连接建立和重连后补读快照。同一条流上的 `material.changed`、`runtime.logs.changed` 与工作流事件共用一个序列，每帧都带整数 id；工作流页面不处理它们，但它们的 id 同样推进续传游标。反馈读取 `/workflow-node-jobs/{uuid}/feedback-history`，按 sequence 续读。

当前运行入口不支持额外库存换绑或高优先级调度；适配器明确拒绝非空换绑和高优先级请求，提交体不发送未支持字段。后端任务创建 DTO 拒绝额外字段，避免调用方误以为配置已生效。后续若新增优先级或库存业务，应先定义 dev 合同再开放界面选项。

浏览器验证使用两台虚拟反应釜中的 reactor-2，将检测阈值设为 0.8。正常运行得到 `[false, false, true]`、`polish_count=2`、`purity=0.9`；单步首次只完成复位动作，恢复后暂停不再派发新动作，再恢复可完成；另一次暂停态任务可取消。预检和控制面要求鉴权时，令牌只保存在当前浏览器会话。


## 并行分组与动态结果（迁移阶段 W1）

```python
@workflow(display_name="两路检测")
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

同时就绪的成员按书写顺序派发（模板实例化时把声明序号记在节点 `meta_data.source_order`，
计划的拓扑序先按它排）；用同一台设备的成员因此按书写顺序依次执行。这只是同时就绪时的先后，
不是顺序保证：分支里前面有别的步骤、某个成员在等库存或另一项资源、失败重试时，后写的成员可能先做。
`parallel` 的意思是「顺序无所谓」，先后有影响就写成顺序执行。动作可以声明
`@action(material_contract=MaterialRole.order_sensitive())`（V1 同名合同 `order_sensitive`）：
同一资源上没有先后关系的两个兄弟动作只要有一方声明，就在发布和创建任务时被拒绝，
报错指出是哪两步，提示改成顺序执行或用依赖明确先后。这里的「同一资源」只有两类：动作节点自己的设备
（`execution_policy.always_free` 的动作不算），以及工作流 `meta_data.unilab.resource_scopes` 显式声明的资源作用域；
物料等其他资源参数不参与比较（`scheduler/interval_plan.py`）。比较在同一层级里进行：循环体里的动作只和同一循环体里的
兄弟比，循环节点本身不带设备；条件分支之间的互斥不算先后关系。发布时只有在当前注册表能解析到动作合同时才检查，
已发布的同一修订重复发布直接返回；创建任务（含 V1 工站调用）总会按执行计划再查一次，依据是计划里冻结的动作合同。
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
