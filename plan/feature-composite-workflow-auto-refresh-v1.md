---
goal: 子工作流更新后自动刷新引用它的父工作流
version: 1.0
date_created: 2026-08-31
last_updated: 2026-08-31
owner: Uni-Lab-OS Team
status: Completed
tags:
  - workflow
  - composite-workflow
  - authoring
  - local-catalog
---

# 子工作流自动刷新实施计划

本计划以 `product/durable-scheduler-kernel-v2` 当前代码为基线。它将新的产品规则
落实到 UniLabOS Local 模式：父工作流中的组合调用仍是静态展开图，但子工作流
的新发布合同应用后，OS 默认重新编译并替换兼容的父图，不要求前端发起额外的“重新
编译”操作。

## 1. Requirements & Constraints

- **REQ-001**: 子工作流应用新发布合同后，OS 必须按稳定工作流 UUID 找到直接引用它
  的活动父工作流，并重新编译父源码。
- **REQ-002**: 父工作流没有待应用编辑时，兼容的新子发布合同必须自动应用到父图；
  调用节点 UUID、父级布局、参数绑定和外部连线保持稳定。
- **REQ-003**: 子工作流删除、改名或不兼容修改输入输出时，父图必须保持上一个
  可运行定义，并发布 `composite_contract_stale` 诊断，禁止静默传错参数。
- **REQ-004**: 父工作流存在用户尚未应用的草稿或候选时，OS 只重新编译并展示
  诊断，不得替用户自动应用其他编辑。
- **REQ-005**: 刷新必须沿父子依赖继续向上游传播，但不得形成递归调用或重复
  应用；现有组合工作流环路校验继续作为关闭式门禁。
- **REQ-006**: 已创建的工作流任务继续使用创建时冻结的图和执行计划，不随工作流
  定义刷新。
- **CON-001**: 不要求前端新增适配，不增加 SQLite 表，不让工作流定义重新成为
  SQLite 持久权威；参数约束继续使用既有 ``parameters``，任务实际值继续使用
  ``input``，节点填写值继续使用 ``param``。
- **CON-002**: 不修改其他仓库；Core #178 的旧“固定子修订、显式应用”规则由
  本轮用户确认的新默认刷新规则取代，但本轮不编辑他人的 Core 工作区。
- **GUD-001**: 新增或修改的函数和测试使用中文 docstring，解释参数、返回值、
  异常与状态不变量。

## 2. Implementation Steps

### Phase 1 — 依赖识别与自动刷新

- **GOAL-001**: 把目录变化后的“重新编译”扩展为安全的父工作流自动应用。

| Task | Description | Completed | Date |
|---|---|---|---|
| **TASK-001** | 从活动领域包依赖与已应用组合节点中识别直接父工作流，避免重编译无关工作流。 | ✅ | 2026-08-31 |
| **TASK-002** | 扩展目录依赖刷新深模块：区分干净父源码与待应用草稿，干净父源码可自动应用候选。 | ✅ | 2026-08-31 |
| **TASK-003** | 在 `WorkflowService.apply_authoring` 提交后接入刷新，并复用现有目录重建、源码 CAS 与候选应用入口。 | ✅ | 2026-08-31 |

### Phase 2 — 兼容与故障边界

- **GOAL-002**: 兼容修改自动替换，不兼容修改保留父图并可诊断。

| Task | Description | Completed | Date |
|---|---|---|---|
| **TASK-004** | 覆盖实现变化、兼容参数变化、破坏性参数变化和父源码已有候选四类行为。 | ✅ | 2026-08-31 |
| **TASK-005** | 证明刷新失败只产生提交后 warning，不把已经提交的子工作流伪装成失败。 | ✅ | 2026-08-31 |
| **TASK-006** | 证明旧 WorkflowTask 的冻结图不变化，新 Task 使用刷新后的父图。 | ✅ | 2026-08-31 |

### Phase 3 — 验证与说明

- **GOAL-003**: 用真实公开组合入口验证冷启动、交互更新和回归边界。

| Task | Description | Completed | Date |
|---|---|---|---|
| **TASK-007** | 运行组合工作流、创作固定点、源码监视和工作流 API 定向回归。 | ✅ | 2026-08-31 |
| **TASK-008** | 运行边界检查、`git diff --check`、文件长度审查并更新本仓领域上下文。 | ✅ | 2026-08-31 |

## 3. Alternatives

- **ALT-001**: 由前端点击“重新编译父工作流”。拒绝，因为前端不应拥有工作流
  展开和兼容性判断。
- **ALT-002**: 运行任务时动态读取最新版子工作流。拒绝，因为会破坏冻结执行计划
  和可恢复性。
- **ALT-003**: 子工作流变化时无条件覆盖所有父图。拒绝，因为会覆盖父工作流未
  应用的用户草稿，也可能静默破坏参数绑定。

## 4. Dependencies

- **DEP-001**: 现有 `CompositeAuthoring` 静态展开与合同兼容校验。
- **DEP-002**: 活动领域包提供的 `dependency_workflow_uuids` 和父图组合元数据。
- **DEP-003**: 现有源码协调、候选签发、目录重建和 `apply_authoring` 线性化事务。

## 5. Files

- **FILE-001**: `unilabos/workflow/catalog_dependent_authoring_refresh.py` — 父工作流刷新深模块。
- **FILE-002**: `unilabos/workflow/composite_contract_refresh.py` — 已发布合同调用的兼容检查与原子替换深模块。
- **FILE-003**: `unilabos/workflow/service.py` — 识别依赖并接入源码应用、合同发布后的刷新。
- **FILE-004**: `tests/workflow/test_composite_auto_refresh.py` — 领域源码自动替换、草稿保护和运行快照合同。
- **FILE-005**: `tests/workflow/test_composite_contract_refresh.py` — 参数、连接点和失败关闭的纯领域合同。
- **FILE-006**: `tests/app/test_workflow_publication_api.py` — 前端现有 HTTP 调用下的父图和上游父图刷新。
- **FILE-007**: `CONTEXT.md` — 记录组合调用自动刷新与运行快照边界。
- **FILE-008**: `unilabos/workflow/published_contract.py`、
  `unilabos/workflow/composite_invocation.py` 与
  `unilabos/workflow/composite_compatibility.py` — 统一发布合同和组合调用中的
  参数约束字段为 ``parameters``。
- **FILE-009**: `unilabos/workflow/_execution_plan_graph.py` 与
  `unilabos/workspace_host/release_publish.py` — 让执行计划和工作区发布继续消费
  同一个 ``parameters`` 字段。

## 6. Testing

- **TEST-001**: 子工作流内部实现变化后，父工作流自动推进修订并更新组合 pin。
- **TEST-002**: 新增带默认值的可选输入后，父工作流自动更新且既有参数绑定不变。
- **TEST-003**: 删除、改名或改变必填输入后，父工作流保留旧图并返回稳定诊断。
- **TEST-004**: 父工作流已有未应用草稿时不自动应用，只刷新当前诊断。
- **TEST-005**: 已创建 Task 保留旧执行快照，更新后创建的 Task 使用新图。

验证结果（2026-08-31）：

- `tests/workflow`：865 passed，5 skipped；另有 1 项日志捕获测试失败，在固定点
  `adfd7ee7` 的干净工作树中同样复现，不是本功能引入；
- 发布、组合展开、自动刷新、执行计划和工作区导入定向回归：43 passed；
- Python 编译和 `git diff --check` 通过；本次新增模块及修改片段已按 Ruff 格式
  整理，两个既有超大工作区发布文件仍有与本功能无关的历史格式差异；
- 产品启动接线测试另有 5 项因隔离环境未安装 ROS `action_msgs`/`uvicorn`
  无法运行，失败发生在导入阶段，不是本功能断言失败。

## 7. Risks & Assumptions

- **RISK-001**: `unilabos/workflow/service.py` 已超过 800 行；本轮只保留依赖查询和
  组合调用，刷新策略放在既有深模块中，不继续堆叠状态机。
- **RISK-002**: 子工作流提交已经完成后，父工作流源码或目录刷新可能失败；结果
  必须表示为可恢复 warning，不能回滚或伪报子提交失败。
- **ASSUMPTION-001**: 当前领域包的父子 import 可在启动扫描中形成依赖；旧包缺少
  依赖元数据时，以已应用父图的 `child_workflow_uuid` 补充识别。

### 文件长度审查与拆分决定

| 文件 | 行数 | 本轮职责 | 决定 |
|---|---:|---|---|
| `unilabos/workflow/service.py` | 5676 | 应用服务编排与线性化锁 | 保留公共门面；组合替换算法已下沉 `composite_contract_refresh.py`，后续按“发布/组合编排、源码创作编排、任务运行编排”顺序拆分，现阶段不在同一变更中移动稳定公共接口。 |
| `unilabos/workflow/authoring_graph.py` | 1437 | AST 编译后的工作流图校验与候选生成 | 本轮只修复目录代际下组合合同鉴权；为保持候选签发和图校验原子局部性暂不拆，后续把组合节点合同校验迁入独立 validator，并以现有编译器公开接口回归。 |
| `unilabos/workflow/composite_compatibility.py` | 692 | 组合合同投影、摘要鉴权和兼容分类 | 保留为单一内聚模块；全部函数共享同一规范投影和摘要不变量，新版自动替换已放入另一深模块，继续拆分反而会暴露中间形状。 |
| `unilabos/workflow/composite_contract_refresh.py` | 530 | 已发布子合同的兼容检查与父图原子替换 | 当前保持一个深模块，避免把中间图和替换步骤暴露给服务层；后续若继续增长，先把纯合同描述符比较提取为 `composite_contract_compatibility.py`，公共入口和领域回归保持不变。 |
| `unilabos/workflow/published_contract.py` | 703 | 发布合同冻结、公开投影与读取 | 本轮只统一参数约束字段；发布事务和合同投影共享同一不变量，当前保持单模块。 |
| `unilabos/workflow/_execution_plan_graph.py` | 814 | 把冻结工作流图转换为执行计划 | 本轮只更新组合参数约束读取键；为保持执行图转换的私有接口不扩散暂不拆，后续按“组合节点展开、普通节点连接”拆分并保留现有编译入口。 |
| `unilabos/workspace_host/release_publish.py` | 2872 | 工作区发布、导入与身份映射 | 本轮只更新两个合同消费点；它是既有超大文件，本次不扩大为无关重构，后续按模板、物料、工作流三类发布阶段拆分。 |
| `tests/workspace_host/test_release_publish.py` | 2417 | 工作区发布公共行为回归 | 本轮只调整一个组合参数用例；继续通过同一发布入口验收，后续与生产模块按模板、物料、工作流行为拆分测试文件。 |

本轮修改后的其他源码和测试文件均未超过 500 行；`test_workflow_publication_api.py`
为 443 行，继续按同一公开 HTTP 发布合同组织。

## 8. Related Specifications / Further Reading

- Dashi Taskboard `LOCAL-3` / Core #178：组合工作流创作合同。
- `CONTEXT.md`：Local Workflow Catalog 与 WorkflowTask 冻结快照边界。
