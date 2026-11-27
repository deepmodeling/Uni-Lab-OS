---
goal: Make domain-package Python the Local workflow authority while preserving durable task execution facts
version: 1.2
date_created: 2026-08-28
last_updated: 2026-08-28
owner: Uni-Lab OS
status: 'Complete'
tags: [refactor, workflow, local-mode, compatibility]
---

# Introduction

![Status: Complete](https://img.shields.io/badge/status-Complete-brightgreen)

本计划把 Local 模式的工作流定义从文件 SQLite 切换为“领域包 Python 源码 +
进程内目录”：领域包 `workflows/*.py` 在 OS 启动时经 AST 扫描、编译和校验后装入
内存；HTTP 导入的 JSON 先确定性转换为 Python，Python 导入则先做 AST 校验，二者
成功后都写入当前领域包并登记到 `package.yaml`。后续图和元数据修改原子回写同一
Python 源文件。工作流任务、节点作业、执行结果、库存预留、控制命令和恢复日志
继续持久化，避免定义生命周期调整破坏运行安全。

## 1. Requirements & Constraints

- **REQ-001**: Local 模式启动必须从已授权领域包工作流源码重建完整工作流定义目录，且源码必须先经 AST 静态扫描、编译和图校验。
- **REQ-002**: Local 模式不得把工作流定义、图、源码注册、创作草稿或发布合同写入文件 SQLite；既有定义表允许保留在 schema 中，但生产路径不得继续作为权威读写。
- **REQ-003**: `POST /api/v1/local/workflows/import-python` 必须保持路径、请求和响应结构不变；导入源码通过 AST 与图固定点校验后写入当前领域包 `workflows/*.py`，并在重启后恢复。
- **REQ-004**: FE、领域包和 Backend 已有工作流 HTTP 合同不得因存储实现调整而变化。
- **REQ-005**: 工作流任务（WorkflowTask）、工作流节点作业（WorkflowNodeJob）、结果、反馈、控制命令、库存分配、执行占用和运行日志必须继续持久化并支持崩溃恢复。
- **REQ-006**: Task 创建必须从同一份不可变内存图快照生成输入、执行计划和首轮作业；事务提交后恢复只依赖 Task 已冻结的 `workflow_snapshot` 与 `execution_plan`。
- **REQ-007**: 运行事实中的 `workflow_uuid` 仅作为来源身份和追踪字段，不得再通过数据库外键强制依赖可消失的工作流定义。
- **REQ-008**: 进程内目录的创建、更新、删除、发布和 Python 导入仍必须保持现有修订、幂等、冲突与原子图替换语义。
- **REQ-009**: `POST /api/v1/workflows/import` 的 JSON 图必须先确定性生成规范 Python，并与 Python 导入共用同一领域源码登记和激活入口。
- **REQ-010**: 已登记工作流通过图或元数据接口修改后，必须生成可回编译的规范 Python，并以草稿哈希 CAS 回写原登记文件；写回失败不得伪装为已同步。
- **REQ-011**: 新工作流源码必须追加到所选领域包 `package.yaml`，工作流 UUID、包内路径和文件内容三者不得出现重复归属。
- **CON-001**: 不新增永久数据库表，不要求 SQLite 表名与 Backend PostgreSQL 表名一致。
- **CON-002**: 当前阶段没有需要保留的历史 SQLite 工作流定义；旧文件库定义不得回退为新进程权威，领域包 Python 才是跨重启定义事实。
- **CON-003**: 领域包启动编译失败时必须关闭式失败，不得发布半套目录；已持久运行任务不得因此被删除或改写。
- **GUD-001**: 生产组合根显式区分“进程内定义目录”和“持久运行事实库”，不以隐式数据库回退形成双权威。
- **PAT-001**: 复用现有 `WorkflowStore` 的成熟定义语义作为 `:memory:` 实现，但通过应用服务边界与文件运行库物理隔离。
- **SEC-001**: 上传源码不得执行；继续使用现有 AST 编译器的大小、语法、装饰器、模板引用和图语义校验。

## 2. Implementation Steps

### Implementation Phase 1

- GOAL-001: 建立定义与运行事实的明确存储边界。

| Task | Description | Completed | Date |
|------|-------------|-----------|------|
| TASK-001 | 为 `WorkflowService` 注入独立定义目录；定义、图、源码注册、创作和发布操作走内存目录，任务、作业与恢复操作走文件运行库。 | ✓ | 2026-08-28 |
| TASK-002 | 为文件运行库增加 runtime-only 初始化模式，移除 `workflow_task.workflow_uuid` 对定义表的外键，并关闭定义读写入口。 | ✓ | 2026-08-28 |
| TASK-003 | 让 Task 创建显式接收来自内存目录的应用图，在文件库单事务内冻结快照、计划、作业和库存分配。 | ✓ | 2026-08-28 |

### Implementation Phase 2

- GOAL-002: 让领域包启动和用户导入统一进入进程内目录，并由领域包 Python 保证重启恢复。

| Task | Description | Completed | Date |
|------|-------------|-----------|------|
| TASK-004 | 在生产组合根创建共享的 `:memory:` 工作流定义目录，并供来源激活、发布工作流展开、编译器重建及 HTTP 服务共同使用。 | ✓ | 2026-08-28 |
| TASK-005 | 为当前领域包建立唯一可写源码目标；Python 导入落盘，JSON 导入先转为规范 Python，二者都以 CAS 更新 `package.yaml` 与 `workflows/*.py`。 | ✓ | 2026-08-28 |
| TASK-006 | 保持领域包来源 fixed-point 激活、源码监视和发布模板生成现有语义，冷启动每次从源码重建目录。 | ✓ | 2026-08-28 |
| TASK-010 | 让已登记工作流的图和元数据修改复用现有 Authoring 生成、固定点校验与源码 CAS 写回链路。 | ✓ | 2026-08-28 |

### Implementation Phase 3

- GOAL-003: 证明外部兼容性与运行时恢复安全。

| Task | Description | Completed | Date |
|------|-------------|-----------|------|
| TASK-007 | 增加定义与运行库隔离测试：文件库定义表为空、Task 可创建、运行快照完整且重启后 Task/Job 可恢复。 | ✓ | 2026-08-28 |
| TASK-008 | 回归 FE 工作流 CRUD/图接口、Python 导入、领域包来源激活、组合工作流、任务调度、控制与恢复测试。 | ✓ | 2026-08-28 |
| TASK-009 | 更新 `CONTEXT.md` 领域语言，执行 Ruff、Pytest、数据库外键检查、改动边界检查和 Git diff 审查。 | ✓ | 2026-08-28 |

## 3. Alternatives

- **ALT-001**: 继续把工作流定义写入文件 SQLite，仅启动时覆盖；拒绝，因为仍保留双权威和历史定义回退风险。
- **ALT-002**: Task 创建时在文件库写一条空 `workflow` 锚点满足外键；拒绝，因为名义上定义不持久化，实际上仍依赖持久定义记录。
- **ALT-003**: 同时把 Task/Job 改为内存态；拒绝，因为会失去崩溃恢复、结果幂等和物理执行安全。
- **ALT-004**: 重写一套纯字典工作流 CRUD；暂不采用，现有 `WorkflowStore(:memory:)` 已提供经过测试的修订、事务、图校验和源码创作语义，重写会扩大兼容风险。

## 4. Dependencies

- **DEP-001**: 现有 `WorkflowAuthoringEngine`、AST Python 导入编译器和领域包来源发现计划。
- **DEP-002**: 现有 `WorkflowStore` 定义/图事务语义和工作流运行事实表。
- **DEP-003**: 现有设备/动作模板不可变内存目录，工作流编译继续消费同代快照。

## 5. Files

- **FILE-001**: `unilabos/workflow/service.py`：显式拆分定义目录与运行事实库。
- **FILE-002**: `unilabos/workflow/store.py`：runtime-only 数据库边界、Task 外键迁移和外部图快照创建入口。
- **FILE-003**: `unilabos/workflow/composition.py`：生产组合根创建并共享进程内工作流目录。
- **FILE-004**: `unilabos/workflow/python_workflow_import.py`：保持 AST 编译合同；由服务决定安装到内存目录。
- **FILE-005**: `tests/app/test_python_workflow_import_api.py`：验证 Python 导入合同与文件库零定义写入。
- **FILE-006**: `tests/workflow/` 相关组合与恢复测试：验证领域包重建和运行事实持久化。
- **FILE-007**: `CONTEXT.md`：修订 Local 工作流定义、来源和运行快照术语。
- **FILE-008**: `unilabos/workflow/domain_source_target.py`：提供领域包 Python 文件与 `package.yaml` 的安全 CAS 登记边界。
- **FILE-009**: `tests/workflow/test_domain_workflow_source_sync.py`：验证 JSON/Python 导入、API 修改回写及冷启动重建。

## 6. Testing

- **TEST-001**: Python 导入后可立即通过现有列表、详情和图接口读取；领域包出现规范 `.py` 与 manifest 登记，关闭并重启后可从源码恢复。
- **TEST-002**: 文件 `workflow_history.db` 的工作流定义、节点、边、源码注册、创作和发布合同不因用户导入增加行。
- **TEST-003**: 领域包工作流每次冷启动均从同一源码重建，编译失败不发布部分目录。
- **TEST-004**: 从内存图创建 Task 时，文件库持久化完整 `workflow_snapshot`、`execution_plan` 和首轮 Jobs，且不需要文件库存在 workflow 定义行。
- **TEST-005**: 进程重启后，已持久 Task/Job 仍能按冻结计划恢复；定义目录是否重建不改变已运行任务事实。
- **TEST-006**: 工作流 CRUD、图保存、发布/组合和 Python 导入 HTTP 路径、状态码、envelope 与字段保持不变。
- **TEST-007**: `PRAGMA foreign_key_check` 在迁移后和运行回归后均无违规。
- **TEST-008**: JSON 导入生成 Python，完成 Python → AST → 图 → Python → AST → 图语义固定点，并在重启后恢复相同工作流与节点身份。
- **TEST-009**: 图和工作流元数据修改后，领域包源文件同步变化；并发草稿变化返回 CAS 冲突，不覆盖人工编辑。

## 7. Risks & Assumptions

- **RISK-001**: 源文件与 `package.yaml` 是两个文件，无法依赖 SQLite 事务原子提交；实现必须先完成全部 AST/图校验，再以文件 CAS 发布，并让未登记孤儿文件不进入启动目录权威。
- **RISK-002**: 领域包删除或重命名工作流后，新进程不再提供该定义；既有 Task 仍按冻结快照恢复，不猜测替代定义。
- **RISK-003**: 旧文件库可能含历史定义；生产路径必须完全忽略，清理物理旧行需保证不影响持久 Task/Job。
- **ASSUMPTION-001**: 当前开发阶段没有必须迁移保留的历史 Local 工作流定义。
- **ASSUMPTION-002**: Backend-controlled 模式的 Backend PostgreSQL 工作流权威不受本次 Local 内部调整影响。

## 8. Related Specifications / Further Reading

`AGENTS.md`

`CONTEXT.md`

`plan/refactor-in-memory-device-catalog-1.md`

`unilabos/workflow/authoring_kernel.py`

`unilabos/workflow/source_discovery.py`
