---
goal: Replace persisted device and device-action templates with one deterministic in-memory catalog
version: 1.0
date_created: 2026-08-27
last_updated: 2026-08-28
owner: Uni-Lab OS
status: 'Completed'
tags: [refactor, registry, workflow, compatibility]
---

# Introduction

![Status: Completed](https://img.shields.io/badge/status-Completed-brightgreen)

本计划将设备模板（Device Template）和设备动作模板（Action Template）的本地权威从 SQLite 切换为 OS 启动时由领域包静态编译产生的不可变内存目录，同时保持 FE、SZLab 和 Backend 已有 HTTP 路径、请求响应结构及同步语义不变。

## 1. Requirements & Constraints

- **REQ-001**: OS 启动时必须从已安装领域包的设备声明和工作流源码静态编译完整目录，不执行被扫描的工作流源码。
- **REQ-002**: 设备模板、设备动作模板和动作连接点不得在生产运行路径写入或读取 SQLite 模板表。
- **REQ-003**: 设备模板 UUID 必须由固定 UUIDv5 命名空间和全局唯一的设备稳定业务 `name` 确定性生成。
- **REQ-004**: 动作模板 UUID 必须由固定 UUIDv5 命名空间、设备稳定业务 `name` 和动作稳定业务 `name` 确定性生成；连接点 UUID 必须由动作模板 UUID、方向和业务键确定性生成。
- **REQ-010**: 内存目录和工作流内部以 `{device_name}.{action_name}` 作为动作模板规范键；外部 UUID 字段仅是该规范键的确定性兼容投影，不建立持久模板权威。
- **REQ-005**: FE 直连 OS 的模板 HTTP 路径、查询参数、响应 envelope 和字段形状不得改变。
- **REQ-006**: Backend 模板同步的 HTTP 路径、请求结构、幂等语义和上报顺序不得改变。
- **REQ-007**: SZLab 的 package.yaml、设备装饰器和 workflows/*.py 声明方式不得因本次重构改变。
- **REQ-008**: 工作流保存、读取、校验、单动作运行和执行计划冻结必须使用同一代不可变内存目录；运行中的任务不得重新查询可变模板。
- **REQ-009**: 物料模板、物料实例、库位和工作流运行事实继续由现有 SQLite 存储承担。
- **CON-001**: 不新增 SQLite 表，不要求 SQLite 表名与 Backend PostgreSQL 表名一致。
- **CON-002**: 生产组合根不得在内存目录缺项时回退读取旧动作模板表，避免双权威。
- **CON-003**: 一个目录代际必须先完整扫描、确定身份并校验成功，再原子替换当前快照；失败时保留上一代。
- **GUD-001**: 新增和修改的函数、类与测试使用中文 docstring，领域名词遵循 Uni-Lab OS 规范。
- **PAT-001**: 使用固定源码常量作为 UUIDv5 根命名空间，稳定业务身份不包含版本号、绝对路径、时间或进程随机值。
- **SEC-001**: AST 扫描必须关闭式失败；语法错误、重复身份、非法 Schema 或未知资源引用不得发布部分目录。

## 2. Implementation Steps

### Implementation Phase 1

- GOAL-001: 建立确定性内存目录身份和原子发布边界。

| Task | Description | Completed | Date |
|------|-------------|-----------|------|
| TASK-001 | 在 `unilabos/registry/template_identity.py` 定义固定命名空间及设备、动作、连接点 UUIDv5 生成函数，并校验设备名、动作名和连接点身份。 | ✅ | 2026-08-27 |
| TASK-002 | 在 `unilabos/registry/template_projection.py` 将设备及动作候选补齐确定性 UUID，并新增不依赖 `WorkflowStore` 的内存投影构造入口。 | ✅ | 2026-08-27 |
| TASK-003 | 在 `unilabos/registry/in_memory_template_projection_store.py` 实现候选全集校验、差量计算和原子快照替换，禁止数据库回退。 | ✅ | 2026-08-27 |
| TASK-011 | 在内存目录中增加规范 `template_key` 双向索引，并在工作流持久化边界完成外部 UUID 与内部名称键转换。 | ✅ | 2026-08-27 |

### Implementation Phase 2

- GOAL-002: 让工作流创作与运行统一消费内存目录。

| Task | Description | Completed | Date |
|------|-------------|-----------|------|
| TASK-004 | 在 `unilabos/workflow/store.py` 注入 `TemplateSnapshotProvider`，将工作流图读取、保存校验、默认参数读取和模板详情读取切换到单次目录快照。 | ✅ | 2026-08-27 |
| TASK-005 | 在 `unilabos/workflow/composition.py` 将生产组合根切换到内存投影，把同一 provider 传给 WorkflowStore、WorkflowService 和模板 HTTP API，并在领域工作流固定点完成后才激活设备目录。 | ✅ | 2026-08-27 |
| TASK-006 | 保持 Backend wire contract 不变，并将设备模板、动作定义和领域工作流来源切换为同代内存目录/注册快照。 | ✅ | 2026-08-27 |

### Implementation Phase 3

- GOAL-003: 证明重启稳定性、单权威和外部兼容性。

| Task | Description | Completed | Date |
|------|-------------|-----------|------|
| TASK-007 | 新增确定性 UUID、重复身份、失败不发布和两次冷启动 UUID 一致测试。 | ✅ | 2026-08-27 |
| TASK-008 | 新增空旧模板表条件下的图保存、图读取、单动作运行和执行计划冻结测试。 | ✅ | 2026-08-27 |
| TASK-009 | 运行 FE 直连 OS 模板 API、Backend 模板同步、领域包工作流编译及现有工作流回归测试。 | ✅ | 2026-08-27 |
| TASK-010 | 执行 Ruff、目标 Pytest、改动边界检查和 Git diff 审查，确认无新增表及无外部合同变化。 | ✅ | 2026-08-27 |
| TASK-012 | 完整工作区把设备目录激活延迟到领域工作流 fixed-point；独立资源图入口仍在提交后立即激活，保持原有调用语义。 | ✅ | 2026-08-28 |
| TASK-013 | 在候选目录发布前携带设备名关闭公共写门禁，阻止旧设备更新/删除及同名物料模板写入；启动失败也不回退 SQLite。 | ✅ | 2026-08-28 |
| TASK-014 | 将 v11 混合模板引用迁移的外键检查放在提交前，并增加失败回滚测试。 | ✅ | 2026-08-28 |

## 3. Alternatives

- **ALT-001**: 保留 SQLite 动作模板作为 UUID 分配权威；拒绝，因为仍形成持久模板依赖和双权威。
- **ALT-002**: 使用 UUID4 后只在进程内保存；拒绝，因为重启后工作流引用失效。
- **ALT-003**: 使用 Python `hash()` 或源码绝对路径生成身份；拒绝，因为跨进程、跨机器或安装目录变化时不稳定。

## 4. Dependencies

- **DEP-001**: Python 标准库 `uuid.uuid5`，不新增第三方依赖。
- **DEP-002**: 现有 `RegistryTemplateSnapshot`、`AuthoringCatalogSnapshot`、领域包 package.yaml 和 AST 编译器。

## 5. Files

- **FILE-001**: `unilabos/registry/template_identity.py`：确定性设备、动作和连接点身份。
- **FILE-002**: `unilabos/registry/in_memory_template_projection_store.py`：进程内完整代际发布。
- **FILE-003**: `unilabos/registry/template_projection.py`：编译确定性候选并发布目录。
- **FILE-004**: `unilabos/workflow/store.py`：工作流持久事实与模板目录读取解耦。
- **FILE-005**: `unilabos/workflow/composition.py`：生产组合根选择内存目录。
- **FILE-006**: `unilabos/registry/runtime_device_catalog.py`：只读设备模板内存目录及 Backend 兼容读模型。
- **FILE-007**: `unilabos/registry/local_template_identity.py`：只持久化物料模板，并安装当前设备目录。
- **FILE-008**: `unilabos/app/scheduler/inventory/store.py`：移除物料实例对单一 SQLite 模板表的外键限制；未新增表。
- **FILE-009**: `unilabos/app/scheduler/inventory/backend_contract.py`：设备模板查询、设备实例创建与物料图的双源兼容边界。
- **FILE-010**: `tests/registry/test_template_identity.py`、`tests/registry/test_template_projection.py`：稳定身份、原子发布和内部名称键测试。
- **FILE-011**: `tests/registry/test_f05_local_template_identity_projection.py`、`tests/workflow/test_c1_r4_production_wiring.py`、`tests/workflow/test_in_memory_template_snapshot_consistency.py`：生产组合、SQLite 单权威边界、固定点失败隔离、单次快照和外部合同测试。

## 6. Testing

- **TEST-001**: 相同 package ID、设备 ID、动作名和 Handle 身份跨两次冷启动生成完全相同 UUID。
- **TEST-002**: 动作 Schema 或展示信息变化不改变 UUID，动作名或 Handle 业务身份变化产生新 UUID。
- **TEST-003**: AST/Schema/重复身份或领域工作流固定点校验失败时不迁移旧设备引用、不安装设备目录，也不替换上一代内存快照。
- **TEST-004**: 生产组合根启动后旧设备及动作模板表保持空，工作流图保存、读取和执行计划构建仍成功。
- **TEST-005**: 内存目录缺少模板时关闭式失败，即使旧 SQLite 表存在同 UUID 也不得回退。
- **TEST-006**: FE 模板列表与详情响应字段、分页和错误 envelope 保持不变。
- **TEST-007**: Backend 资源模板同步请求结构和幂等键保持不变。
- **TEST-008**: SZLab package.yaml 与 workflows/*.py 现有示例无需修改即可编译。

验证结果：改动文件 Ruff、Python 编译与 `git diff --check` 通过；应用、注册表和
工作流三组完整回归共 1,972 项通过、5 项跳过、1 项预期失败。其余 9 项失败均为
固定提交上已存在的环境或基线问题：缺少 `pytest-asyncio`、库存 wire fixture
漂移、skip 隔离状态、缺省字段形状、日志捕获和旧物料来源夹具。最终聚焦回归
53 项通过（包含 v11 迁移失败回滚和旧库同名冲突隔离）。真实 SZLab 包目录编译得到 16
个领域设备、118 个原生动作；SQLite 中设备模板、动作模板和动作 Handle 均为 0
行，物料模板为 22 行。完整托管启动再加入 OS Host 与 19 个领域工作流组合动作，
得到 17 个设备、137 个动作；239 个已应用工作流节点引用全部使用
`{device_name}.{action_name}`。

## 7. Risks & Assumptions

- **RISK-001**: 设备稳定业务 `name` 或动作稳定业务 `name` 重命名会产生新身份；必须将其视为显式领域变更，不能静默兼容。
- **RISK-002**: 已保存但未发布的工作流仍引用目录 UUID；领域包删除对应动作后，该工作流应明确报告模板缺失，而非猜测替代动作。
- **RISK-003**: 旧 SQLite 模板表可能仍存在于既有 schema；生产代码必须停止读写，物理删除应在确认没有外部分支直接依赖后独立迁移。
- **ASSUMPTION-001**: 当前开发阶段无需要迁移的历史用户数据。
- **ASSUMPTION-002**: 设备 `name` 在整个 OS 活动目录中全局唯一且由领域包维护者承诺稳定；`display_name` 不参与身份。

## 8. Related Specifications / Further Reading

`AGENTS.md`

`CONTEXT.md`

`unilabos/package_manager/package_catalog/compilers/python/workflow.py`

`unilabos/workflow/authoring_kernel.py`
