# 微后端数据库：runtime / materials / telemetry / history

Uni-Lab-OS 的持久状态放在四个**相互独立的 SQLite 文件**里。本文从代码整理出这四个库的位置、
锁、结构版本、连接参数、每张表的用途、由哪个进程写入，以及备份、重置、查看与排障的做法。
以代码为准的入口：

| 主题 | 代码 |
| --- | --- |
| 四库规格聚合 `DATABASE_SPECS` | `unilabos/server/database/__init__.py` |
| 建表 DDL 与行模型（唯一来源） | `unilabos/server/database/tables/`（`runtime/`、`materials.py`、`telemetry.py`、`history.py`） |
| 结构版本、指纹、迁移与建库 | `unilabos/server/database/schema.py` |
| 路径解析与隔离校验 | `unilabos/server/database/layout.py`、`unilabos/server/startup.py` |
| 根目录锁 `unilab.lock` | `unilabos/server/database/root_lock.py`、`unilabos/app/main.py:_claim_process_database_root` |
| 单连接 / 单写者 | `unilabos/server/database/connection.py`、`sqlite_domain.py` |
| 配置项 | `unilabos/config/config.py`（`BasicConfig.server_database_root`、`database_rebuild_on_mismatch`） |

业务代码不跨库外键、不 `ATTACH`：跨库只通过服务层和规范 UUID 协作（`server/composition.py:ServerServices`）。

## 1. 四个库一览

| 键 | 文件 | 规格里的职责（`role`） | `synchronous` | 无迁移路径时 | 内容 |
| --- | --- | --- | --- | --- | --- |
| `runtime` | `runtime.db` | critical microbackend command and execution control, workflow authority and registry snapshots | `FULL` | `refuse` | 工作流定义 / 任务 / 节点运行 / 作业、调度持久事实（执行认领、资源区间、设备托管、等待队列）、注册表条目版本、V1 进程管理记录；Host 侧的 runtime.v1 命令与执行作业 |
| `materials` | `materials.db` | resource, material, site, topology link and lab graph authority | `FULL` | `refuse` | 物料权威：资源模板、物料树、库位、拓扑边、命名设备图快照、批次 / 预留 / 账本、内容物、化学品、物理执行结算 |
| `telemetry` | `telemetry.db` | high-frequency device state and telemetry | `NORMAL` | `rebuild` | 设备最新状态与状态事件 |
| `history` | `history.db` | large payload and append-only audit history | `NORMAL` | `rebuild` | 大载荷与只追加的作业 / 决策审计事件 |

`runtime` / `materials` 是实验室的事实（工作流、任务、注册表、物料），结构不符又没有迁移路径时拒绝启动；
`telemetry` / `history` 可以丢，备份后自动重建（`tests/server/test_database_schema_versions.py::test_release_policy_refuses_runtime_and_materials_without_migration`）。

同一个库文件上可以有多个"域服务"共用一条连接和一把写锁（`sqlite_domain.py`）：

- `runtime.db`：宿主 `RuntimeService`（`server/services/runtime/data.py`），共存 `WorkflowService`、
  `RegistryService`；
- `materials.db`：宿主 `MaterialsService`（`server/services/materials/core.py`），共存 `GraphService`、`ChemicalService`；
- `telemetry.db`：`TelemetryService`；`history.db`：`HistoryService`。

## 2. 位置规则

### 2.1 根目录

四个库默认放在同一个**数据库根目录**下。根目录的解析顺序（`server/startup.py:configured_database_root`）：

1. 命令行 `--server_database_root`；
2. `BasicConfig.server_database_root`（配置文件，或环境变量 `UNILABOS_BASICCONFIG_SERVER_DATABASE_ROOT`）；
3. `<working_dir>/database`。

工作目录本身的规则见 `config/working_dir.py`（`--working_dir` → `--config` 所在目录 → 当前目录下的
`.unilabos` / `unilabos_data` → 默认 `~/.unilabos`）。兼容 0.12.x：第 3 步的 `<working_dir>/database` 里还没有任何库文件、
而旧默认位置 `~/.unilabos` 下已有库文件时，继续用 `~/.unilabos` 并打印一次迁移提示
（`server/startup.py:default_database_root`）。

### 2.2 每个进程的根目录

一个根目录只归一个进程（见第 3 节）。各运行形态实际打开的根目录：

| 运行形态 | 本进程打开的根目录 | 代码 |
| --- | --- | --- |
| 默认 `unilab`：调度权威 | `<root>` | `app/backend_main.py:run_backend_process`（`launch_host=True`） |
| 默认 `unilab`：权威拉起的 Host 子进程 | `<root>/edge`（命令行带 `--server_database_root <root>/edge`） | `app/supervisor.py:host_child_command` |
| `unilab --role backend` | `<root>/backend` | `server/startup.py:process_database_root`、`backend_main.py` |
| `unilab --address <权威>` 顶层 Host | `<root>` | `server/startup.py:process_database_root` |
| `unilab --no-safe-restart`（权威与 Host 同进程） | `<root>` | 同上 |
| 手动启动的 `--is_slave` | `<root>`（Slave 自己工作目录下的） | 同上 |
| Host 管理的受管 Slave | `<working_dir>/device_processes/<id>/database` | `server/services/device_processes.py`（`--server_database_root`） |
| `--check_mode` | 不取锁、不开库 | `app/main.py:_claim_process_database_root` |

### 2.3 单库覆盖

`--runtime_db` / `--materials_db` / `--telemetry_db` / `--history_db` 可以把单个库放到别处
（`server/startup.py:resolve_database_paths` → `ServerDatabasePaths.resolve`）：

- 相对路径相对根目录解析，绝对路径原样使用；`~` 会展开；
- 四个库必须是四个不同的物理文件，同一路径或已存在文件的硬链接都会被拒绝（`DatabaseLayoutConflict`）；
- 默认拓扑里这些覆盖只给权威：Host 子进程的命令行把四个 `--*_db` 置空，四库全部落在 `<root>/edge` 下
  （避免两个进程打开同一个文件）；
- 根目录锁只锁根目录，**不保护**放在根目录之外的覆盖文件；全量重置（第 9.3 节）也拒绝自定义库路径。

### 2.4 目录示例（默认拓扑）

```text
<working_dir>/                     默认 ~/.unilabos
├── local_config.py
├── control_token                  控制面 token（第 8 节）
├── driver_packages.json           驱动包台账
├── driver_packages/               驱动包源码
├── device_processes.json          受管设备进程规格
├── device_processes/<id>/database/   受管 Slave 自己的四库与 unilab.lock
├── registry_cache.pkl
├── logs/
└── database/                      <root>
    ├── unilab.lock                权威持有
    ├── runtime.db  (+ -wal / -shm)
    ├── materials.db
    ├── telemetry.db
    ├── history.db
    ├── graph_cache/  workflow_sources/  reset-backups/
    └── edge/                      Host 子进程的根目录
        ├── unilab.lock            Host 子进程持有
        ├── runtime.db  materials.db  telemetry.db  history.db
        └── graph_cache/
```

## 3. 根目录锁 `unilab.lock`

打开任何库之前，进程先独占**数据库根目录**下的 `unilab.lock`（`app/main.py:_claim_process_database_root`，
权威另在 `backend_main.py` 里对自己的根目录再取一次，同一进程内可重入）。锁在根目录里，不在工作目录里。

- 是操作系统级的独占锁：POSIX 用 `fcntl.lockf`，Windows 用 `msvcrt.locking`，锁文件第 0 字节。进程无论怎么结束
  （正常退出、崩溃、`kill -9`、`TerminateProcess`）都由操作系统释放，不需要清理陈旧锁。锁文件会留在目录里，
  **文件存在不代表被占用**，是否被占用以能否加锁为准。
- 第 1 字节起是持有者记录（JSON：`pid`、`started_at`、`role`、`host`、`command`），冲突时用来说明是谁占着。
- 第二个实例拿不到锁：打印"数据库根目录 … 已被另一个 Uni-Lab-OS 进程占用（pid …）"并以**退出码 73**结束
  （`app/supervisor.py:DATABASE_ROOT_LOCKED_EXIT_CODE`，EX_CANTCREAT）。顶层启动不等待，立即失败。
- 被看护的进程（Host 子进程、薄监督下的 Host、受管 Slave）重新拉起时上一代刚退出，最多等
  `RELAUNCH_ROOT_LOCK_WAIT_S`（10 s）。Host 子进程以 73 退出时，看护器按崩溃退避再拉起，并提示"常见于上次遗留的
  Host 进程仍在运行"。
- 根目录无法创建、锁文件无法加锁（例如文件系统不支持记录锁）时抛 `DatabaseRootLockError`，退出码 1；
  根目录需在支持文件锁的本地磁盘上。
- 权威持 `<root>`、Host 子进程持 `<root>/edge`，两把锁互不相干。全量重置归档前，权威会临时独占 `<root>/edge`
  确认没有遗留的 Host 进程还开着那些库（`backend_main.py`，`wait_s=5`）。

要在同一台机器上并行跑第二个实例，给它另一个 `--working_dir` 或 `--server_database_root`。只换根目录、共用工作目录时
锁不冲突，但两个实例仍共用工作目录里的 `control_token`、驱动包台账、日志与 `registry_cache.pkl`。

## 4. 结构版本与指纹

### 4.1 版本、指纹与身份

每个库由一个 `DatabaseSpec` 声明（`schema.py`）：

- `schema_version`：迁移序号，记在 `PRAGMA user_version`；
- `contract_version`：表结构之外的行内容契约（JSON 列形状、幂等键派生、枚举取值），计入指纹；
- **结构指纹** `checksum`：`-- contract_version=N` 加上全部 `CREATE TABLE` / 索引 / 触发器 / 视图语句的 SHA-256；
- 身份表 `schema_identity(database_key, checksum, applied_at_ms)`：记录这个文件属于哪个库以及当前指纹；
- 迁移审计表 `schema_migration`（只在执行过迁移的库里出现，不属于任何领域、不计入指纹）；
- `released_checksums`：已发布版本的指纹，库内是这些版本时指纹也必须一致才允许迁移。

0.13 发布线是基线：四个库都是 schema v1、`migrations=()`。`tests/server/test_database_schema_versions.py` 把每个库的
`(schema_version, checksum)` 固定在 `PINNED` 里：改了 DDL 或 `contract_version` 而不提升版本，测试失败。
发布后改结构的步骤：

1. 该库 `schema_version` +1；
2. 在 `migrations` 里追加对应版本的迁移（`sql_migration`，或改 CHECK / 列约束用 `rebuild_table_migration`；
   迁移只能用 `connection.execute`，不能用会提前提交的 `executescript`）；
3. 把旧版本的指纹登记进 `released_checksums`；
4. 更新 `PINNED`。

### 4.2 打开时的判定

`initialize_database()` 打开文件后先按下表判定（`schema.py:_inspect`）：

| 库内状态 | 判定 | 结果 |
| --- | --- | --- |
| 文件不存在，或只有 `schema_migration` / 空的 `schema_identity` | empty | 建全部表，写身份行，`user_version` = 代码版本 |
| `schema_identity` 里有别的 `database_key` | — | 抛 `DatabaseIdentityConflict`，拒绝打开（与策略无关） |
| 有领域表但没有 `schema_identity`，或有身份表却没有身份行 | unmigratable | 按策略 |
| `user_version` 高于代码版本（指纹一致也算） | unmigratable | 按策略 |
| 指纹与代码一致 | current | 直接复用（`CREATE … IF NOT EXISTS` 补齐，`user_version` 对齐） |
| `1 ≤ user_version < 代码版本`，且该版本不在 `released_checksums` 或指纹与登记一致 | migratable | 迁移（见下） |
| `user_version` 在 `released_checksums` 里但指纹不同 | unmigratable | 按策略 |
| `user_version = 0` 且指纹不同（迁移机制之前的旧结构） | unmigratable | 按策略 |
| `user_version` 等于代码版本但指纹不同 | unmigratable | 按策略 |

**迁移**（migratable）：先用 SQLite backup API 做一致性快照 `<file>.pre-v<N>-<时间戳>.bak`（含 WAL 里未检查点的页），
再关外键、在一个 `BEGIN IMMEDIATE` 事务里依次执行迁移，最后与按当前声明新建的内存库逐表比对列、索引、触发器和视图，
并做 `PRAGMA foreign_key_check`。任何一步不符就整体回滚，抛 `DatabaseMigrationFailed`，库保持迁移前状态。

**按策略**（unmigratable）：

| `on_unmigratable` | 库 | 默认行为 | `database_rebuild_on_mismatch = True` 时 |
| --- | --- | --- | --- |
| `refuse` | runtime、materials | 抛 `DatabaseMigrationRequired`，旧库原样保留，进程以退出码 1 结束（只打印说明、不打调用栈） | 同 `rebuild` |
| `rebuild` | telemetry、history | 旧库连同 `-wal` / `-shm` 改名为 `<file>.bak-<时间戳>`，重建空库，打印 warning | 同左 |

`BasicConfig.database_rebuild_on_mismatch` 也可以用环境变量 `UNILABOS_BASICCONFIG_DATABASE_REBUILD_ON_MISMATCH=true` 打开。
它对本进程所有 `refuse` 库一起生效；备份文件不会自动删除，确认不需要后手工清理。

## 5. 连接与事务

每个库文件在一个进程里只有**一条**连接（`schema.py:_open_connection`）：

| 设置 | 值 | 说明 |
| --- | --- | --- |
| `sqlite3.connect(timeout=…)` | 30 s | `check_same_thread=False`，`row_factory = sqlite3.Row` |
| `PRAGMA foreign_keys` | `ON` | 迁移期间临时关闭，结束前统一检查 |
| `PRAGMA journal_mode` | `WAL` | 磁盘上会有 `-wal` / `-shm` 两个伴随文件 |
| `PRAGMA synchronous` | runtime / materials `FULL`，telemetry / history `NORMAL` | 来自各库 `DatabaseSpec.synchronous` |
| `PRAGMA busy_timeout` | 30000 ms | 只在别的连接（只读调试、外部 `sqlite3`）同时访问时起作用 |

进程内：`SerializedConnection` 用一把 `RLock` 包住每次 `execute` 并在锁内取完结果；写事务只走
`SqliteDomain.write()`，以 `BEGIN IMMEDIATE` 开始、异常回滚、正常提交。同库共存的域共享这把锁，所以同一库文件上
同一时刻只有一个进程内 writer。

## 6. 表清单

下面每张表一行用途。"无写入方"表示 0.13 发布线的代码里只有建表与读取、没有任何 `INSERT` / `UPDATE`。
每个库都另有 `schema_identity`（第 4 节）。

### 6.1 runtime.db

**运行控制（Host 侧 runtime.v1，`tables/runtime/data.py`，写入方 `server/services/runtime/data.py`）**

| 表 | 用途 |
| --- | --- |
| `backend_session` | Host 与调度权威之间的 runtime.v1 控制会话（`edge_uuid`、权威代次、连接代次、active / disconnected） |
| `executor_endpoint` | 执行端身份与能力快照：设备路由、动作能力及可用性（见第 7.3 节） |
| `command_inbox` | 收到的 runtime.v1 命令（`execute_job`、取消、错误决策），处理前先落盘，按 UUID 幂等 |
| `execution_job` | Host 侧一次被派发作业的生命周期（accepted → … → 终态，失败时的 `terminal_waiting` 闸门） |
| `adapter_command_outbox` | 交给执行适配器的命令队列（如取消），由协调器认领 |
| `adapter_event_inbox` | 预留的适配器事件收件箱；无写入方 |
| `backend_event_outbox` | Host → 权威的业务事件，收到 ack 前保留 |

**工作流权威（`tables/runtime/workflow.py`，写入方 `server/services/runtime/workflow/`）**

| 表 | 用途 |
| --- | --- |
| `workflow` | 工作流定义（名称、类型、修订、元数据） |
| `workflow_operation_category` | V1 实验操作类别（`/experiment-operation-categories`），软删除 |
| `workflow_node_template` / `workflow_handle_template` | 节点模板（注册表动作投影与结构模板）及其连接点 |
| `workflow_node` / `workflow_edge` | 工作流图的节点与边 |
| `workflow_publication` | 显式发布的不可变修订快照 |
| `workflow_source_registration` | `@workflow` 源码文件与工作流的登记（包根、相对路径、源 URI） |
| `workflow_authoring` | 源码作者状态：草稿哈希、诊断、候选、回写状态 |
| `workflow_task` | 一次运行（任务）：冻结的快照 / 执行计划、状态、`priority`、`inventory_bindings`、`dispatch_gate` |
| `workflow_node_run` | 任务内每个节点唯一的执行单元（≡ runtime.v1 `attempt_group_uuid`） |
| `workflow_node_job` | 节点运行的一次尝试（≡ runtime.v1 `job_uuid`），`control_data` 里冻结库存请求、material_io 计划等 |
| `workflow_task_command` | 任务级命令的幂等记录（step / pause / resume / cancel / edit_input / switch_to_step / unlock_resources） |
| `workflow_intervention` | 作业失败等待人工决策时的干预记录（open / selected / superseded） |
| `workflow_manual_confirmation` | durable 人工确认单（`manual_confirm` 与 `material_io` 共用） |
| `frontend_event` | 前端失效通知流；后台按 `BasicConfig.frontend_event_retention_seconds` / `frontend_event_max_count` 定期清理 |
| `execution_lock_lease` | 旧执行锁租约表；无写入方（`count_rows` 白名单里可读） |
| `workflow_node_job_result` / `workflow_node_job_feedback_history` | 作业结果 / 反馈历史表；无写入方，读取（`list_job_results` / `list_job_feedback_history`）恒为空 |
| 视图 `workflow_runs` / `job_runs` | 由 `workflow_task` / `workflow_node_job` 投影的只读运行历史（V1 `workflow_history.db` 同形）；`job_runs.actual_s` 也是时间线实测时长的来源 |

**调度持久事实**

| 表 | 用途 |
| --- | --- |
| `workflow_execution_claim` | 派发前每个尝试的资源声明（reserved / running / uncertain / timed_out / released） |
| `workflow_execution_fence` | 每个锁键单调递增的栅栏计数 |
| `workflow_execution_lease` | 每个锁键的当前持有声明 |
| `workflow_execution_authority` | 单行，调度权威代次 |
| `workflow_execution_dispatch` | 每个作业的派发记录：载荷哈希、调用身份、执行端代次、开始许可、回执 |
| `workflow_resource_interval` | 编译出的资源占用区间所有权（held / retained / released，ADR 0001） |
| `workflow_resource_admission` / `workflow_resource_use` | 每个作业的区间准入，以及按锁键的实际使用 |
| `workflow_resource_loan` | 任务之间显式借还区间 |
| `workflow_device_tenancy` / `workflow_tenancy_transition` | 设备托管（reserved / loaded / retained / released）及每个作业的托管转移 |
| `workflow_resource_waiter` | 持久的老化 FIFO 等待者（锁键、阻塞者） |
| `workflow_station_control` / `workflow_station_control_command` | V1 工站运行 / 暂停模式与其幂等控制命令 |
| `workflow_error_session` / `workflow_station_error` | V1 工站错误处置会话与错误决策 |
| `workflow_human_interaction` | 设备 / 工作流向操作员发起的结构化人工交互及回复 |

**注册表、进程管理**

| 表 | 用途 |
| --- | --- |
| `registry_entry` | 每个注册表条目每个版本一行不可变完整定义 |
| `registry_entry_state` | 每个条目一行可变状态：生效版本、待确认版本、软移除 / 不可用 |
| `registry_report` | 每次 Host 上报的批次统计 |
| `process_management_operation` / `process_management_state` | V1 进程管理协议的幂等操作记录与快照修订号 |

### 6.2 materials.db（`tables/materials.py`）

**物料与拓扑**

| 表 | 用途 |
| --- | --- |
| `resource_template` | 资源模板：一行一个完整模板（类别、库位定义、连接点） |
| `material` | 物料身份与低频静态字段，`parent_material_uuid` 构成物料树（防环触发器） |
| `material_position` | 物料位置，1:1 |
| `material_data` / `material_substance` | 物料数据，及其下的当前物质（数量权威） |
| `site` | 库位当前快照与占用者（占用者必须是后代的触发器） |
| `material_link` | 物料 / 设备节点之间的拓扑边（node-link 的 link） |
| `lab_graph` | 命名设备图快照（node-link JSON，名称派生稳定 UUID，`revision`） |
| `material_state_history` | 物料状态追加历史（V1 `/materials/{uuid}/states`） |

**库存**

| 表 | 用途 |
| --- | --- |
| `inventory_lot` | 批次：总量 / 可用 / 预留、单位、效期、`warehouse_zone_id`、`location_site_uuid` |
| `inventory_reservation` | 每个尝试一行的库存预留（条目 JSON；active / consumed / released / quarantined） |
| `inventory_ledger` | 模板 / 物料 / 库位 / 批次 / 预留变化的只追加账本，兼作 `/materials/changes` 变更流（`delivery_status`） |
| `inventory_command_effect` | 每个库存变更 `(command_uuid, effect_key)` 的幂等结果，指向账本序号区间 |
| `inventory_processed_command` | V1 类型化库存命令（`POST /inventory/commands`）按 `command_id` 的首次结果 |
| `material_content` / `material_content_history` | 试剂 / 样品 / 当前内容物（`material_substance` 上的角色扩展）及变更历史 |
| `material_content_reservation` | 任务对容器内容物的数量预留 |
| `material_source_binding` | 任务准入时冻结的物料来源绑定 |
| `station_ingress_reservation` | 工站入站库位预留 |
| `material_execution` / `material_execution_hold` | 一次设备尝试的冻结物理计划与结算；未结算动作对物料 / 库位的唯一占用 |
| `chemical` | 化学品定义（CAS / InChIKey 可空唯一） |

实验室档案 / 分区 / 摆放（`lab_meta` / `lab_zone` / `lab_placement`）与 runtime.db 的网格布局（`lab_layout`）
按负责人 2026-11-17 的决定已删除，连同 `/lab/profile`、`/lab/layout`、`/lab/zones`、`/lab/placements` 路由；
不用这些表的 `/lab/warehouse`（按品类聚合批次）与 `/lab/assembly/{id}`（物料组合树）保留。

### 6.3 telemetry.db（`tables/telemetry.py`）

| 表 | 用途 |
| --- | --- |
| `telemetry_source_cursor` | 每个 endpoint 的来源游标（epoch / generation / sequence），去重与排序 |
| `device_state_latest` | 每个 endpoint / 设备的完整最新状态（触发器拒绝更旧的来源位置） |
| `telemetry_event` | 设备状态 / 属性变化事件（只在值变化时记录） |

### 6.4 history.db（`tables/history.py`）

| 表 | 用途 |
| --- | --- |
| `payload_object` | 大载荷：≤ 256 KiB 内联，否则外部 URI；SHA-256、可选过期 |
| `history_event` | 只追加的审计事件：job_transition、action_availability、job_feedback、job_result、job_log、error_snapshot、decision_audit |

## 7. 哪个进程写哪个库

### 7.1 默认 `unilab`（调度权威 + Host 子进程）

权威与 Host 子进程**都会打开**自己根目录下的全部四个库（`ServerServices.open`），但实际写入的范围不同：

| 库 | 权威 `<root>` | Host 子进程 `<root>/edge` |
| --- | --- | --- |
| runtime.db | 工作流、任务、调度持久事实、注册表、进程管理 | 只有运行控制表：`backend_session`、`executor_endpoint`、`command_inbox`、`execution_job`、`adapter_command_outbox`、`backend_event_outbox`（`server/backend/coordinator.py:WorkflowBusinessCoordinator`） |
| materials.db | 物料权威、Graph Authority、库存全部 | 基本为空：Host 子进程带 `--material_microbackend_addr <权威>`，物料经 HTTP 写到权威（`server/startup.py:setup_host_server_stack`） |
| telemetry.db | 建表后不写 | 设备状态投影（`server/backend/telemetry.py:TelemetryDeviceStateProjection`） |
| history.db | 建表后不写 | 作业转移、反馈、结果、日志、错误快照与决策审计（`coordinator.py`、`composition.py` 的状态事件监听） |

浏览器只连权威端口：`/api/v1/runtime`、`/telemetry`、`/edge/history`、`/device-state` 等 Host 数据由
`server/api/edge_proxy.py` 转发给 Host 子进程、读的是 `<root>/edge` 下的库；workflow / registry / materials /
graphs / lab / scheduler 由权威用 `<root>` 下的库回答。`/api/v1/debug/databases` 只列出权威自己的四库。

### 7.2 其他形态

| 形态 | 写入 |
| --- | --- |
| `--no-safe-restart` | 一个进程写 `<root>` 下全部四库（7.1 两列的并集） |
| `--role backend` | `<root>/backend` 下的 runtime.db 与 materials.db（同 7.1 权威列）；Host 是经 `--address` 接入的另一个进程 |
| `--address` 顶层 Host | `<root>` 下的运行控制表、telemetry、history；没有 `--material_microbackend_addr` 时物料也写本机 `<root>/materials.db` |
| `--is_slave` | 只把 `-g` 图登记进自己的 `materials.db`（`lab_graph`）并写 `graph_cache`；物料、作业都经 HostLink 由 Host 处理 |

### 7.3 `executor_endpoint` 的写入与读取

- **写入**：Host 侧 `WorkflowBusinessCoordinator` 构造时以 `state="online"` 登记一行
  （`endpoint_uuid = "<backend>:<machine_name>"`，`server/backend/composition.py:setup_execution_backend`），
  执行器就绪或设备增减时 `publish_endpoint_capabilities()` 写入设备路由与动作能力（内容哈希去重）。
  也可以经 `PUT /api/v1/runtime/endpoints/{uuid}/snapshot` 整份写入。代码里没有把它置为 `offline` 的路径。
- **读取**：`GET /api/v1/runtime/endpoints`（默认拓扑经 edge_proxy 读 Host 的库）；设备目录与预检
  （`composition.py:read_preflight_endpoints`、`services/runtime/devices.py`）、V1 工站动作列表
  （`api/runtime/stations.py`）、工作流 Site 绑定校验（`api/runtime/workflow.py`）；`execution_job`、
  `adapter_command_outbox` 等外键引用它，建作业、取消、认领适配器命令前都要求 endpoint 存在。

## 8. 数据库之外的持久文件

| 路径 | 写入方 | 用途 | 全量重置是否移走 |
| --- | --- | --- | --- |
| `<root>/unilab.lock`（每个根目录一个） | 持锁进程 | 根目录独占锁与持有者记录 | 否 |
| `<进程根目录>/graph_cache/<图 uuid>.json` | 加载 `-g` 的进程（默认拓扑是 Host 子进程，在 `<root>/edge/graph_cache`） | Graph Authority 载荷的本地缓存，`load_graph_payload` 从它装配（`app/main.py:_write_graph_cache`） | 否 |
| `<权威根目录>/workflow_sources/` | 调度权威 | 托管的 `@workflow` 源码（`server/backend/composition.py:setup_local_scheduler`） | 否 |
| `<root>/reset-backups/<随机标识>/`、`<root>/reset-pending.json` | 全量重置 | 归档与进行中标记（第 9.3 节） | — |
| `<file>.pre-v<N>-<时间戳>.bak`、`<file>.bak-<时间戳>` | 建库 | 迁移前快照、重建前的旧库 | 否 |
| `<working_dir>/driver_packages.json` | Host 驱动包服务 | 驱动包台账（来源、版本、sha256、源码根、启用状态） | 否 |
| `<working_dir>/driver_packages/<name>/<version>/` | 同上 | 远端来源解压后的源码；`.staging/` 放切换中的备份 | 否 |
| `<working_dir>/driver_package_operations.json` | 同上 | 包操作日志与崩溃恢复意图（`server/services/driver_package_journal.py`） | 否 |
| `<working_dir>/.host-mutation.lock` | 同上 | 包变更 / Slave 启动 / Host 重启共用的跨进程变更锁 | 否 |
| `<working_dir>/driver_package_catalog.json` | 用户 | 本地补充的可安装目录 | 否 |
| `<working_dir>/device_processes.json`、`device_processes/<id>/` | Host 受管进程服务 | 受管 Slave 规格；每个进程的图、日志与数据库根目录 | **是** |
| `<working_dir>/control_token` | 调度权威首次启动 | 控制面 token（`config/control_token.py`；`unilab token` 打印） | 否 |
| `<working_dir>/station_api_key` | 用户 | V1 工站调用密钥（`config/station_key.py`，不自动生成） | 否 |
| `<working_dir>/registry_cache.pkl` | 注册表构建 | AST 扫描 / 构建结果缓存，删除即完全重建 | 否 |
| `<working_dir>/logs/` | 每个进程 | `<日期 时间>.log` 与 `ws_comm_<日期 时间>.log` | 否 |
| `<working_dir>/session.json`、`session.lock` | 云端登录子命令 | 登录会话 | 否 |
| `<working_dir>/native/` | 用户 | 可选的原生授锁策略库 | 否 |

## 9. 备份、查看与重置

### 9.1 备份

- **一致的整套备份**：停止 `unilab`（权威退出时 Host 子进程随之退出），复制整个根目录，包括 `edge/`、
  每个库的 `-wal` / `-shm` 文件；需要时连同工作目录里的 `control_token`、驱动包台账与 `device_processes*` 一起复制。
  四个库之间没有跨库事务，只有停机复制能保证它们彼此一致。
- **运行中单库备份**：用 SQLite 在线备份，例如 `sqlite3 runtime.db ".backup /path/runtime.bak.db"`。
  它对单个文件一致，但不同库之间、权威与 `edge/` 之间不保证一致。运行中不要只复制 `.db` 文件而丢下 `-wal`。
- 建库自己留下的 `.pre-v<N>-*.bak`（迁移前）与 `.bak-*`（重建前）可以直接用 `sqlite3` 打开。

### 9.2 查看

- **调试 API**：`GET /api/v1/debug/databases` 列出四库文件、大小与每张表的行数；
  `GET /api/v1/debug/databases/{database}/tables/{table}?limit=&offset=&order=` 分页读一张表（默认最新在前）。
  每次请求用 `mode=ro` 短连接打开，不共享 writer 连接、不抢写锁，BLOB 只回长度（`server/api/debug.py`）。
  导航页的「DB Debug」链接指向 `/api/docs#/debug`。默认拓扑里它只看得到权威 `<root>` 下的库。
- **命令行**：`sqlite3 "file:/path/runtime.db?mode=ro" "PRAGMA user_version; SELECT * FROM schema_identity;"`。
  WAL 下只读连接不阻塞服务写入。
- **不要在服务运行时用外部工具写库**：直接写会绕过服务层的写锁、幂等记录、账本与前端事件。

### 9.3 全量重置（`/api/v1/reset`）

现有重置接口（`server/backend/reset.py`，路由在 `server/api/runtime/diagnostics.py`）只在**默认拓扑**
（权威带 Host 子进程）里可用；`--role backend`、`--no-safe-restart`、`--address` 顶层 Host 的预览返回 `supported=false`。

1. `GET /api/v1/reset` 预览：范围、备份目录、本进程的一次性 `confirmation_token`。
2. `POST /api/v1/reset`，body `{"confirmation_token": …, "confirmation": "清空全部数据"}`。先做前置检查：没有进行中的重启、
   调度暂停后没有活跃设备作业、Host 报告没有在线 Slave、没有运行中的驱动包操作；任何一项不满足就恢复派发并拒绝
   （HTTP 200 + 业务码 3003）。
3. 通过后返回 202，进入维护状态：除 `/api/v1/reset` 与 `/api/v1/health` 外的请求都得到 HTTP 503 + 业务码 5001；
   随后服务停机。
4. Host 子进程退出、全部 writer 关闭后，权威临时独占 `<root>/edge`，把下列文件**移动**（不是删除）到
   `<root>/reset-backups/<随机标识>/`：`<root>` 与 `<root>/edge` 下四库各自的 `.db` / `-wal` / `-shm`，
   以及 `<working_dir>/device_processes.json` 和 `<working_dir>/device_processes/`。先写 `plan.json`，每移动一个文件前把意图写进
   `<root>/reset-pending.json`，完成后写 `manifest.json`，并把 pending 标记移成 `journal.json`。
5. 控制台打印"重置完成"后，不带旧 `-g` 重新启动 `unilab`。

不移走：`graph_cache/`、`workflow_sources/`、驱动包源码与台账、`control_token`、日志、`registry_cache.pkl`、建库备份。
任何库被单库覆盖到默认位置之外、或目标是符号链接 / 目录联接时，预览与请求都会拒绝。归档中断时 `reset-pending.json`
留在根目录，下次启动直接报错拒绝，按其中的 `moved` / `next` 与 `plan.json` 核对，完成归档或恢复后再删除该标记。
操作说明另见 [本机全量重置](full_reset.md)。

### 9.4 手工重置单个库

停机后把该库的 `.db`、`-wal`、`-shm` 一起移走，下次启动建空库：

- telemetry.db / history.db：只丢设备状态历史与审计 / 大载荷；
- runtime.db：丢工作流、任务、注册表版本、布局网格与进程管理记录；Host 子进程的 `edge/runtime.db` 丢在途的 runtime.v1 命令与作业记录；
- materials.db：丢物料、库位、设备图快照、库存与账本。

## 10. 故障排查

**启动被拒：`数据库 … 与当前版本不兼容，需要重建`**（`DatabaseMigrationRequired`，退出码 1）。
runtime 或 materials 的结构与代码不一致又没有迁移路径（常见于开发期旧库：0.13 把开发期的迁移链压平成 v1 基线）。
选择其一：

- 旧数据还要：用对应版本的代码导出，或换一个位置启动（`--server_database_root` / `--runtime_db` 等）；
- 旧数据不要：设 `BasicConfig.database_rebuild_on_mismatch = True` 或环境变量
  `UNILABOS_BASICCONFIG_DATABASE_REBUILD_ON_MISMATCH=true`，旧库改名为 `.bak-<时间戳>` 后重建。

默认拓扑里 Host 子进程的 `<root>/edge` 也要过同一关；子进程继承同一配置文件与环境变量。只有 edge 的库被拒时，
Host 子进程会以 1 退出并被按崩溃退避反复拉起，看 Host 输出里的这条说明。

**迁移失败：`迁移失败，已回滚（备份 …）`**（`DatabaseMigrationFailed`，退出码 1）。库已回滚到迁移前状态，
`<file>.pre-v<N>-*.bak` 是迁移前快照；修正迁移后重启即可。

**身份冲突：`database file belongs to 'X', cannot open it as 'Y'`**（`DatabaseIdentityConflict`，未单独处理，打印调用栈、退出码 1）。
某个 `--*_db` 指向了别的库的文件，或手工把文件改了名。把路径改回去；这种情况不会被 `database_rebuild_on_mismatch` 覆盖，
不会误清数据。两个库被配置成同一个文件时，解析路径阶段就报 `DatabaseLayoutConflict`。

**第二个实例：`数据库根目录 … 已被另一个 Uni-Lab-OS 进程占用`**（退出码 73）。提示里有占用者的 pid、角色、启动时间与命令行。
停掉那个进程，或给新实例另一个 `--working_dir` / `--server_database_root`。默认拓扑里 Host 子进程报 73，多半是上一次遗留的 Host 进程还活着。

**`上次全量重置未完成；请根据 reset-pending.json 恢复或完成归档后再启动`**：见第 9.3 节。

**`[Database] 数据库仍在旧的默认位置 ~/.unilabos`**：0.12.x 的库还在旧位置，本次继续用；按提示停机后把四库（连同 `-wal` / `-shm`）
和 `edge/`、`backend/` 移到 `<working_dir>/database`，或用 `BasicConfig.server_database_root` 固定位置。

## 11. V1 → 统一分支对照

V1 由 `app/runtime_storage.py` 在一个运行目录下打开三类本地库（默认每次启动用私有临时目录，`--preserve_runtime_databases`
才保留），正式 Backend 模式下 Edge 只留 `edge_control.db`。统一分支的库是持久的，重置走 `/api/v1/reset` 归档。
逐文件对照见 [V1 文件索引](v1_file_index/README.md)（`app.md`、`scheduler.md`、`workflow.md`）。

| V1 | 统一分支 | 说明 |
| --- | --- | --- |
| `inventory.db`（`app/scheduler/inventory/store.py`、`reagent_store.py`、`content_store.py`、`ingress_schema.py`） | `materials.db` | 单一库存权威，不另建 inventory.db。`sample` / `current_substance` 合成按角色区分的 `material_content`；`sync_outbox` 由 `inventory_ledger.delivery_status` 代替；`inventory_lot` / `inventory_reservation`、`station_ingress_reservation` 同名保留；V1 的 `lab_meta` / `lab_zone` / `lab_placement` 移植后按负责人 2026-11-17 决定删除 |
| `device_state.db`（`app/scheduler/device_state.py`） | `telemetry.db` | latest / history 两表由 `device_state_latest` / `telemetry_event` 承担 |
| `workflow_history.db`（`workflow/store.py` 及其迁移、`app/scheduler/history.py`） | `runtime.db` 工作流域 | `workflow_runs` / `job_runs` 从表变成只读视图；V1 `store_migrations` 不移植（四库都是 v1 基线） |
| `inventory.db` 的派发准入（`app/scheduler/inventory/dispatch_admission.py`：`station_execution_claim` / `_fence_counter` / `_lock_lease`）与 `workflow_history.db` 的执行锁（`workflow/store_migrations.py`：`execution_claim`、`execution_lock_waiter`、`task_device_tenancy` 等） | `runtime.db` `workflow_execution_*`、`workflow_resource_*`、`workflow_device_tenancy` + `materials.db` `material_execution*` | 执行认领、栅栏、租约、等待与托管在 runtime.db，物理计划与结算在 materials.db |
| `edge_authority.db`（`app/edge_control/local_authority.py`、`local_edge_session.py`，本地回环的 Backend 侧） | 调度权威的 `runtime.db` + 内存中的 `EdgeControlService` | 派发意图与结果由工作流表与 `workflow_execution_dispatch` 承担；会话 / 在线投影见 `backend_session`、`executor_endpoint` |
| `edge_control.db`（`app/edge_control/store.py`，Edge 侧协议恢复） | Host 的 `<root>/edge/runtime.db` | `command_inbox`、`execution_job`、`backend_event_outbox` 等：命令确认前落盘、事件 ack 前留在 outbox 的语义一致 |
| `scheduler_timing_observations`（`app/scheduler/timing_store.py`） | 无对应表 | 学习估时不移植；实测时长取 `job_runs.actual_s` |
| `.unilabos/runtime/workbench/operations/<id>.json`（Workspace Host） | `runtime.db` `process_management_*` | 操作记录与修订号持久化 |
| 工作目录进程锁（`runtime_storage.py:_RuntimeDirectoryLock`） | `<root>/unilab.lock` | 锁的是数据库根目录，冲突退出码 73 |
| — | `history.db` | dev 新增：大载荷与审计事件 |
| — | `materials.db` `lab_graph` / `material_link` | dev 的 Graph Authority 快照与拓扑边（dev 的网格布局 `runtime.db` `lab_layout` 按负责人 2026-11-17 决定删除） |
