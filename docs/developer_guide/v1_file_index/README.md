# V1.0 → 统一分支逐文件索引

V1.0（`690491a3`）的 492 个源文件（不含厂商驱动与 `unilabos/test/`）逐个对照统一分支，给出落在哪里、缺了什么。
四张表按 V1 目录分区：

| 区 | 表 | 文件 | 已移植 | 部分 | 缺失 | 有意不移植 | 不涉及 |
|---|---|---|---|---|---|---|---|
| workflow | [workflow.md](workflow.md) | 129 | 87 | 12 | 5 | 22 | 3 |
| scheduler | [scheduler.md](scheduler.md) | 88 | 40 | 18 | 1 | 26 | 3 |
| app | [app.md](app.md) | 92 | 50 | 13 | 1 | 19 | 9 |
| rest | [rest.md](rest.md) | 183 | 111 | 15 | 6 | 16 | 35 |
| 合计 | | 492 | 288 | 58 | 13 | 83 | 50 |

表里的结论是对 #361 合入点（`6f94ec04`）的核验，之后的补齐不回写到逐文件表，统一记在下面的「缺口处理」里。
`v1_dev_reconciliation.md` 是按条目（W / A / I / R / D / F …）的对账表，两者互相引用。

## 结论的含义

- `PORTED`：功能在统一分支落地，写出对应位置（文件 / 类或函数），必要时一句说明差异。
- `PARTIAL`：部分落地，写清缺了哪一块（行为 / 字段 / 路由）。
- `MISSING`：没有对应实现，也不在有意不移植之列，是真正的缺口。
- `DROPPED`：有意不移植，引用下面的编号或对账表 ID。
- `N/A`：纯测试辅助、`__init__`、类型别名、平台胶水等不承载功能的文件。

## 已定的有意不移植 / 换成 dev 做法

表中「#n」「已定 #n」指这里的编号。

1. **Workspace Host 概念整体去掉**：后端直接用 dev 的进程管理提供 V1 进程管理协议（`/v1/snapshot`、`/v1/operations`、`/v1/logs`），
   代码与表命名为 `process_management`（`server/services/process_management.py`、`server/api/process_management.py`）。
   V1 的 `workspace_host/*`、工作区产品代 / 发布部署（release_publish、launch、authority_sync 等）属于此类。
2. **graphio 删除**：图只认 node-link JSON，经 Graph Authority 加载（`server/services/materials/graph.py`），GraphML 不再支持。
3. **厂商驱动移出核心**：`unilabos/devices/` 只留工作站基类、虚拟设备、抽象移液站；驱动在外部驱动包（`unilab package install`）。
4. **架构沿用 dev**：权威进程 + Host 子进程、HostLink 传输（ROS 2 可选）、runtime.v1 控制面、dev 的调度器（`server/backend/scheduler/`）
   与工作流服务（`server/services/runtime/workflow/`）、dev 的物料权威（`server/services/materials/`）与驱动包系统（`server/services/driver_packages.py`）。
   V1 的 EdgeScheduler / edge_control / local authority 等由这些承担，核验的是**行为**是否等价。
5. **开源树没有时间规划引擎（CP-SAT）**：`/scheduler/time-plan` 返回 `{enabled:false}`，时间线用实测平均时长 + 动作声明的预计时长（A01）。
6. **运行时补料分支不做**（`material_source(supports_replenishment=True)` + `sufficient` 分支）：缺料是预检报错——任务启动时整任务预留不足直接失败；
   节点就绪时才预留的需求不足则开 `category="preflight"` 决策（重试 / 跳过 / 人工替换为成功 / 放弃），补料后重试。
   `material_io` 人工装入 / 取走 / 补量节点后来决定补上，作为作者显式写出的独立一步（`ctx.material_io(...)`，见下面「需要确认的三点」）。
7. **builder 模式删除**：工作流只按源码静态编译（`registry/workflow_ast.py` + `workflow_helpers.py`），与 V1 一样只认 if / with。
8. **数据库**：四个库都是 v1 基线、没有迁移链（V1 的 store_migrations 之类归此）；结构不一致且无迁移路径时拒绝启动，提示重建。
   位置、锁、表清单与 V1 库文件对照见 [微后端数据库](../databases.md)。
9. 与 V1 的已知有意差异（不算缺口）：发布后被编辑的工作流按发布指纹调用工站时拒绝（3003）且 dev 运行当前图；`resolve-uncertain` 后作业为
   `failed`；`mark_used` 存为 `retired`；部分路由按 V1 返回裸 JSON；`/error-decisions` 加可选 `Idempotency-Key`；消耗列表多 `inventory_type:"lot"`；
   读接口免 token；Host 启动期返回 200 + 5001 + `Retry-After`。

## 缺口处理

核验找出的缺口（跨区重复的已合并）和负责人的决定。「已补」写的是 #361 之后的补齐提交标题。

### A. 会静默出错

| # | 缺口 | 处理 |
|---|---|---|
| 1 | 普通 `@action` 返回 `success=false` / `False` 被当成功 | **按 dev，不改行为**：动作失败要 raise，返回值不表达失败（`add_device.md` 已写明，与 V1 不同） |
| 2 | 任务的 `inventory_bindings` 不生效 | 已补：*task inventory bindings select the inventory a requirement reserves* |
| 3 | 组合调用丢失子操作的资源作用域 | 已补：*composite keeps the child's scopes* |
| 4 | 内容物预留消费与分装结算重复扣量 | 实测属实，已补：*an aliquot source reserved as content is deducted once* |
| 5 | 临时资源条件直接让任务失败 | 已补：*temporary resource conditions keep the attempt queued*；HTTP 物料网关的 6007 / 6011 同样等待 |
| 6 | AGV 入口预留不锁资源 | 已补：入口预留占住库位（冲突 `site_ingress_reserved`，6011），一个载体只能有一个活动预留 |
| 7 | 库位模板白名单不校验 | 已补：6008 库位模板白名单，库位成环 6009 / 父子成环 6004 |

### B. V1 驱动 / 作者兼容

| # | 缺口 | 处理 |
|---|---|---|
| 8 | `@action` / `@device` 的 V1 装饰器参数 | **按 dev**，不接受 V1 专有参数 |
| 9 | `error_policy` 旧写法 | **按 dev** |
| 10 | 子操作发布后父工作流自动刷新 | **不改**：父工作流钉在已发布的子版本上 |

### C. 运维 / 稳定性

| # | 缺口 | 处理 |
|---|---|---|
| 11 | 日志无容量控制 | **不需要** |
| 12 | 调度没有背压 | 已补：在途作业 100 / 运行任务 500 / 每工作流 100（`BasicConfig.scheduler_*`），超出的排队 |
| 13 | 安装驱动包不查定义 id 冲突 | 已补：安装时检查与已装包的定义 id 冲突；与框架内置 id 重名仍到 Host 启动才报 |
| 14 | 源码监视器与写回恢复未接线 | 已补：权威进程启动时恢复并监视已登记的工作流源码 |

### D. 小项

| # | 缺口 | 处理 |
|---|---|---|
| 15 | `unilab doctor`；`workflow run --inputs`、`workflow command`；MCP 任务事件；条件 / 循环节点模板 | 已补（condition / loop / group 结构模板） |

### E. 功能缺口

| # | 缺口 | 处理 |
|---|---|---|
| 16 | 工作流原生命令节点（人工交互、库存命令） | 延后：现场需要「流程里问人」或带计数设备时再做 |
| 17 | 命名 / 等价库位组、按设备类型选实例、多候选绑定 | 延后 |
| 18 | 循环跨轮携带值 | 已补：原生 `while` / `for` 携带循环体里重新赋值的变量（不用 ctx 写法）；复合 until 条件未做 |
| 19 | 图转源码覆盖面 | 延后：前端需要画布↔源码双向编辑时 |
| 20 | 资源模板兼容校验 | 延后：与前端适配一起 |
| 21 | 节点级人工确认超时 | **按 dev**：用人工确认动作的 `timeout_seconds`（上限，默认 3600 秒） |
| 22 | 编辑器列号 UTF-16 | **不需要** |
| 23 | 物料 3D 模型 / 2.5D 外形目录 | 延后 |
| 24 | 库位默认内容物、跨设备物料父上下文、设备模板确定性 UUID | 延后 |
| 25 | 容器内容规则（V1 试剂 / 样品 / 当前内容物互斥） | **不改**：一个容器可以同时有试剂、样品和当前内容物（按 PLR 的 name + volume 存储） |

### 需要确认的三点（已答）

- 物料只是被别的任务占用（不是真缺）：**等待**。已补：*wait while inventory is only held by other tasks*。
- `material_io`：作为后端本地执行的工作流节点补上（load / unload / refill，人工确认后一次物料事务改库存，不派发到 Host）。已补：*ctx.material_io declares a manual load / unload / refill step*、*a material.io command loads, unloads or refills in one transaction*、*run material_io steps on the authority after a manual confirmation*；写库后同步所属设备的台面（负责人 2026-11-17 决定）：*sync the owning device's deck after a material_io step writes inventory*；写法与行为见 `workflow_static_conditions.md`、`scheduling_and_execution.md`。
- 容器内容规则：见 E25，不改。
