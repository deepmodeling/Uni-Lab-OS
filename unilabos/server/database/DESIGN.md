# 微后端数据库边界

微后端使用四个独立 SQLite 文件，每库一个 writer。分库是为了隔离关键控制、
物料事务、高频设备状态和大历史写入；不是为了把每个数据模型字段再拆成表。
业务代码不得使用 `ATTACH DATABASE` 或跨库外键。

代码命名空间统一归属微后端：工作流定义/上传位于
`unilabos.server.workflow`，调度与执行期 DAG 位于
`unilabos.server.scheduler`，四库组合根位于 `unilabos.server.composition`。
不保留 `unilabos.server.storage`，也不保留顶层 `unilabos.workflow`、
`unilabos.scheduler` 或 `unilabos.storage`；旧库不会被探测、打开或迁移。

| 数据库 | 权威内容 | 表数（含 migration） |
| --- | --- | ---: |
| `runtime.db` | 后端命令、执行 job、endpoint 与可靠收发 | 8 |
| `materials.db` | 资源模板、物料、Site、预留与库存账本 | 11 |
| `telemetry.db` | 设备最新状态和高频追加事件 | 4 |
| `history.db` | 大 payload 和统一执行历史流 | 3 |

四库合计 26 张表，其中 4 张是各库自己的 `schema_migration`。

## 表目录

### `runtime.db`

| 表 | 职责 |
| --- | --- |
| `schema_migration` | 数据库身份、版本和真实 schema checksum |
| `backend_session` | 后端连接 epoch 与命令/事件游标 |
| `executor_endpoint` | HostLink/ROS2 endpoint；route、action capability 和 availability 是 JSON 模型字段 |
| `command_inbox` | 后端命令幂等接收箱 |
| `execution_job` | 后端 job；物料 binding、错误 gate 和 terminal decision 是 job 字段 |
| `adapter_command_outbox` | 发往 HostLink/ROS2 的可靠命令 |
| `adapter_event_inbox` | adapter 控制事件、ACK 和 endpoint snapshot |
| `backend_event_outbox` | 发往后端的可靠领域事件 |

### `materials.db`

| 表 | 职责 |
| --- | --- |
| `schema_migration` | 数据库身份和 schema 版本 |
| `resource_template` | 完整模板；`category`、`available_sites`、`handles` 都是模型字段 |
| `inventory_lot` | 独立批次和数量聚合 |
| `material` | Material 身份、树关系及低频静态配置 |
| `material_position` | Material 的 1:1 `ResourceDictPosition` 几何和布局 |
| `material_data` | Material 的 1:1 杂项动态 `data`、内容版本和状态来源 |
| `material_substance` | `material_data` 的 1:N 当前内容物；每行是 `name/quantity/quantity_unit` 三元组 |
| `site` | 完整 ResourceSite 当前快照，包含 category 提示和 occupant |
| `inventory_reservation` | 每个 backend job 一行，items 是 JSON 数组字段 |
| `inventory_command_effect` | materials command 的跨重启幂等状态 |
| `inventory_ledger` | append-only 事实账本，同时承担向后端投递状态 |

### `telemetry.db`

| 表 | 职责 |
| --- | --- |
| `schema_migration` | 数据库身份和 schema 版本 |
| `telemetry_source_cursor` | endpoint epoch/generation/sequence 水位 |
| `device_state_latest` | 每个 endpoint/device 一行完整最新状态、属性、连接和告警 |
| `telemetry_event` | state/property/connection/alarm 的统一高频追加流 |

### `history.db`

| 表 | 职责 |
| --- | --- |
| `schema_migration` | 数据库身份和 schema 版本 |
| `payload_object` | 最大 256 KiB inline payload；更大内容使用外部对象存储 |
| `history_event` | transition/feedback/result/log/error/decision 的统一追加历史流 |

## 聚合与数据模型原则

- 表对应需要独立身份、生命周期、事务或高频写入隔离的聚合，不对应每个 Pydantic
  类型或对象字段。
- `ResourceTemplate.category` 是 `list[str]` 数据模型字段，SQLite 使用
  `category_json` 保存；前端用它识别，后端和 Edge 不做 Site 准入校验。
- `available_sites` 和 `handles` 同样属于 ResourceTemplate，不建立模板子表。
- Material 是对象聚合原则的例外：位置结构稳定且有独立更新节奏，杂项 `data` 内容异构，
  因此分别保存为 1:1 `material_position` 和 `material_data`。
- `material.ordinal` 保存同一父节点下的 PLR child 顺序，`site.ordinal` 保存载架声明的
  Site 顺序；`site_index` 是业务索引，不能用标签排序替代序列化顺序。
- PLR child 的 `resource_id` 使用根资源内的转义路径形成全局稳定键；展示名仍保存在
  `name`，实例身份由微后端分配的 `material_uuid` 决定。
- `ResourceDict.liquids` 改以 `substances` 表达，保存在 `material_data` 下的 1:N
  `material_substance`；每项 `(name, quantity, quantity_unit)` 对应现有
  `(liquid_name, amount, unit)` 三元组。单位不在数据库枚举，当前 Edge 写入侧主要使用
  `ul`（液体）和 `ug`（固体）。
- 内容物变化历史统一进入 append-only `inventory_ledger`，不再重复建立
  `substance_history`。
- route/capability/availability 跟 endpoint snapshot 同步重建，直接保存在
  `executor_endpoint`。
- material bindings、错误 gate 和 terminal decision 跟一次 job 同生命周期，直接保存在
  `execution_job`；审计历史另写 `history_event`。
- reservation items 随 backend job 整体申请和释放，保存在一行
  `inventory_reservation.items_json`。
- latest 与 append-only history 读写模式不同，因此设备状态保留
  `device_state_latest` + `telemetry_event` 两张表；不同事件类型不再各建一张表。

## 跨库关联

跨库只保存规范 UUID 和内容哈希，不声明 SQLite 外键：

| 标识 | 权威库 | 其他引用位置 |
| --- | --- | --- |
| `command_uuid` | `runtime.command_inbox` | materials effect/ledger；history event |
| `job_uuid` | `runtime.execution_job` | materials reservation/ledger、telemetry event、history event |
| `endpoint_uuid` | `runtime.executor_endpoint` | telemetry latest/event、history event |
| `material_uuid`、`site_uuid`、`reservation_uuid` | `materials.db` | runtime job binding JSON |
| `payload_uuid` | `history.payload_object` | runtime command/event/job 与 history event |

## 调度和错误边界

- 后端调度器是唯一调度权威；四库不保存本地 DAG、待调度队列或本地 retry。
- retry 是新的后端命令和新的 `job_uuid`，通过 `retry_of_job_uuid` 关联原 job。
- action availability 是 endpoint 快照字段，不是 edge 调度锁。
- 非人工错误把 job 置为 `terminal_waiting` 并打开 gate；收到后端确认调度已更新的
  `release_failed` 后，才允许将同一 job 更新为 `failed`。
- 人工干预使用 replacement result；原结果和替换结果都追加到 `history_event`，通过
  `supersedes_event_uuid` 关联，不覆盖原始历史。
- Site category 仅供前端画布识别，不参与 materials writer 的占用准入。

## 四库业务接口

四个数据库都只通过各自 Repository 和 Service 写入，并提供同构的 FastAPI、
Local client 与 HTTP client。公共安装入口是
`unilabos.server.api.install_server_apis`，一次挂载以下命名空间：

| 数据库 | HTTP 前缀 | 写入语义 |
| --- | --- | --- |
| `runtime.db` | `/api/v1/runtime` | session/endpoint upsert、命令和 job 状态机、gate 与可靠 outbox |
| `materials.db` | `/api/v1/materials` | 模板和物料聚合 CRUD、move、snapshot 与 ledger ACK |
| `telemetry.db` | `/api/v1/telemetry` | event ingest 推进 cursor/latest，另提供只读查询 |
| `history.db` | `/api/v1/history` | payload 保存、event 追加和人工 replacement chain |

`telemetry_event`、`history_event` 和 runtime outbox 是追加式数据，不提供任意
PUT/PATCH/DELETE。Runtime job 更新必须经过合法 transition/error gate；这些约束在
Local 和 HTTP 两种调用方式下保持一致。

## materials authority 实现入口

`materials.db` 已经通过下列分层接入运行时，不再要求调用方直接拼 SQL：

| 层 | 入口 | 职责 |
| --- | --- | --- |
| 通信协议 | `unilabos.server.protocol.materials` | `materials.v1` DTO、写命令信封、版本前置条件和结果 |
| 持久化 | `unilabos.server.repositories.materials` | 表行 CRUD、`BEGIN IMMEDIATE` 单 writer、ledger/outbox |
| 聚合服务 | `unilabos.server.services.materials` | 模板、Material Tree、Position/Data/Substance、Site move 和软删除 |
| 快照 | `unilabos.server.services.material_snapshot` | 规范哈希、逐 section diff 和一次事务应用 |
| PLR 边界 | `unilabos.server.adapters.plr_materials` | PLR 创建草稿、权威 UUID 回填、上传和下载 |
| Registry 边界 | `unilabos.server.adapters.registry_materials` | Registry/lab_resources 定义登记和模板 UUID 映射 |
| Helper | `unilabos.resources.materials` | `materials.create(plr_resource)`，按 Host/Slave 角色选择权威链路 |
| 设备运行时 | `unilabos.device_runtime.resource` | `ResourceService` 把 create/get/update 统一路由到微后端；update 使用局部 snapshot 和版本前置条件 |
| HTTP / Client | `unilabos.server.api.materials`、`unilabos.client.materials` | `/api/v1/materials` 与同构 Local/HTTP/HostLink client |

所有写请求使用 `(command_uuid, effect_key)` 幂等。成功结果保存 ledger sequence
范围；拒绝结果保存稳定错误码。Material 的 identity、position、data/substances
任一 section 变化时，Material 聚合版本只增加一次；Site 使用自己的版本。Snapshot
不隐式创建或删除聚合，结构变化必须使用显式 create/delete。

`ResourceTreeSet.from_plr_resources(..., known_random_uuid=True)` 只允许创建草稿
生成临时 Resource/Site UUID。微后端 create 总是重新分配权威 UUID，并在
`client_ref_map` 返回映射；下载得到的权威树继续使用默认严格模式。

创建请求不接受 `template_uuid`。Helper 从完整 PLR Resource 中提取稳定的
`template_name` 及 identity/position/data/substances/sites；materials authority 按
`template_name` 对齐 complete registry。名称不存在时，authority 在同一事务内登记
自定义模板并分配内部 `template_uuid`。该 UUID 仅用于数据库外键、版本和回执，调用方
不负责提供。Slave 的创建、查询和 snapshot 更新固定经 HostLink 发给 Host，再由 Host
代发到当前微后端 Materials Authority。Host 的运行时 ResourceTreeSet 只是工作副本，
不能作为查询 fallback，也不能分配或接受实例 UUID。

Edge 侧始终只有一个物料信息中心：微后端。设备、Host、Slave 和 Edge API 均不允许
根据配置切换为直连正式 Backend。后续正式 Backend 接入时，创建等需要全局权威的写接口
由微后端代为转发；微后端接收 Backend 返回的 UUID/版本后更新本地权威投影，再向调用方
返回同一份回执。查询和 snapshot 比对仍先进入微后端。本版本不实现该 Backend 转发，
当前创建由微后端本地完成。旧 `/resources/add|update|delete|list` ROS 服务和通用 HTTP
client 的 Backend 物料写入方法已经删除；保留的 ROS 资源树内部消息也统一调用
`ResourceService`，不能绕过微后端。
