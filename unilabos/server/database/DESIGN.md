# 微后端数据库边界

微后端使用四个独立 SQLite 文件。四库必须由组合根一次解析路径，每库只有一个
writer；业务代码不得通过 `ATTACH DATABASE` 或跨库外键制造隐式分布式事务。

| 数据库 | 权威内容 | 写入特征 |
| --- | --- | --- |
| `runtime.db` | 后端命令、执行 job、终态闸门、执行 endpoint 与可靠收发箱 | 低延迟、关键状态、`FULL` 同步 |
| `materials.db` | 资源模板、物料、Site、库存预留与库存账本 | 事务一致性优先、`FULL` 同步 |
| `telemetry.db` | 设备属性、连接状态、告警及高频采样 | 高频覆盖和追加、`NORMAL` 同步 |
| `history.db` | 大 payload、执行时间线、结果、日志、错误与审计 | 追加和归档、`NORMAL` 同步 |

## 表目录

### `runtime.db`

| 表 | 职责 |
| --- | --- |
| `schema_migration` | 数据库身份、版本和真实 schema checksum |
| `backend_session` | 后端连接 epoch、命令游标和事件 ACK 游标 |
| `executor_endpoint` | HostLink/ROS2 endpoint、adapter epoch 与重建进度 |
| `device_route` | 设备到 endpoint 的候选 route；每台设备最多一个 selected route |
| `device_action_capability` | endpoint 发现的 action 清单与并发策略 |
| `device_action_availability` | action 的 `free/busy/unknown` 最新观测，不承担本地调度 |
| `command_inbox` | 后端命令幂等接收箱 |
| `execution_job` | 后端下发 job 与独立 attempt；retry 使用新 job |
| `job_material_binding` | job 接收时固化的物料、Site 和 reservation 引用 |
| `terminal_gate` | 设备错误等待后端/调度确认的终态闸门 |
| `terminal_decision` | `release_failed` 或人工 `replace_result` 决定 |
| `adapter_command_outbox` | 发往 HostLink/ROS2 的可靠命令 |
| `adapter_event_inbox` | adapter 控制事件、command ACK 与重建快照 |
| `backend_event_outbox` | 发往后端的可靠领域事件 |

### `materials.db`

| 表 | 职责 |
| --- | --- |
| `schema_migration` | 数据库身份和 schema 版本 |
| `resource_template` | 资源模板规范定义；category 与 `available_sites` 均保留在 `definition_json` |
| `resource_handle_template` | Registry handle 定义、版本和软删除 |
| `inventory_lot` | 批次、可用量、预留量和隔离状态 |
| `material` | ResourceDict 静态字段和单父资源树 |
| `material_pose` | ResourceDictPosition 与所属 frame |
| `material_state` | 物料当前 data/liquids/sites_initialized 投影 |
| `material_state_source_event` | 状态来源事件幂等记录 |
| `site` | ResourceSite 聚合：实例字段、位姿、category 提示和当前占用 |
| `inventory_reservation` | 后端 job 级原子 reservation；明细保存在 `items_json` |
| `inventory_command_effect` | command effect 幂等与账本范围 |
| `inventory_ledger` | append-only 物料/库存事实账本 |
| `inventory_event_outbox` | 基于 ledger sequence 的同步事件 |
| `inventory_sync_state` | 每个 peer 的同步 checkpoint |

### `telemetry.db`

| 表 | 职责 |
| --- | --- |
| `schema_migration` | 数据库身份和 schema 版本 |
| `telemetry_source_cursor` | endpoint epoch/generation/sequence 水位 |
| `telemetry_ingest_batch` | 一次高频上报的原子接收批次 |
| `device_state_report` | 完整或增量设备状态报告 |
| `device_property_latest` | 每个 endpoint/device/property 的最新值 |
| `device_property_sample` | 属性追加样本 |
| `device_connection_latest` | HostLink/ROS2 分 endpoint 连接投影 |
| `device_connection_event` | 连接变化历史 |
| `device_alarm` | 当前告警投影 |
| `device_alarm_event` | 告警生命周期事件 |
| `telemetry_maintenance` | 各追加流的 retention 水位和分批删除配置 |

### `history.db`

| 表 | 职责 |
| --- | --- |
| `schema_migration` | 数据库身份和 schema 版本 |
| `payload_object` | 最大 256 KiB inline payload；更大内容使用外部对象存储 |
| `job_transition` | job 状态版本时间线 |
| `action_availability_event` | action 可用性观测和重建历史 |
| `job_feedback` | 按 job sequence 幂等的 feedback 历史 |
| `job_result` | 不覆盖原结果的版本化 result/supersedes 链 |
| `job_log` | job/device 日志流 |
| `error_snapshot` | stack、设备状态和 action context 引用 |
| `decision_audit` | 终态决定及 replacement result 审计 |
| `history_maintenance` | 归档、删除和恢复水位 |

## 跨库关联

跨库只保存规范 UUID、来源版本和内容哈希，不声明 SQLite 外键：

| 标识 | 权威库 | 其他引用位置 |
| --- | --- | --- |
| `command_uuid` | `runtime.command_inbox` | materials 的幂等 effect、ledger、outbox；history 的 transition |
| `job_uuid` | `runtime.execution_job` | materials 的 reservation/binding，telemetry 的来源，history 的时间线 |
| `endpoint_uuid` | `runtime.executor_endpoint` | telemetry 的连接/采样来源，history 的 action 可用性历史 |
| `material_uuid`、`site_uuid`、`reservation_uuid` | `materials.db` | runtime 的 job material snapshot/binding |
| `payload_uuid`、`result_uuid`、`error_uuid` | `history.db` | runtime 只保存引用与摘要 |

查询层可以聚合这些标识；任何单库 writer 都不能级联修改另一个数据库。

## 聚合拆表原则

- 与主记录严格 1:1、同步创建和更新、没有独立生命周期的字段不拆表。
  因此 Site 的 pose、category 提示和当前 occupant 都属于 `site`。
- category 与 `available_sites` 都是资源模板定义的一部分，保存在
  `resource_template.definition_json`；category 供前端识别，Site 实例化后才生成
  `site` 行。
- reservation items 随 backend job 整体申请和释放，作为
  `inventory_reservation.items_json` 的原子快照保存；变更历史写入 ledger。
- 只有需要独立生命周期、独立幂等键、高频追加或关系索引的数据才拆表。

## 无跨库事务的写入规则

1. 后端命令先幂等写入 `runtime.command_inbox`。
2. 如果命令影响库存，materials writer 以 `(command_uuid, effect_key)` 幂等执行并在
   同一 materials 事务中写状态、ledger 和 outbox。
3. 大 payload 或执行历史先按稳定 UUID 幂等写入 history；runtime 之后更新引用。
   中途崩溃最多留下可回收的孤立 history 对象，不得留下 runtime 指向未写入对象的状态。
4. job、action、endpoint 等控制事件先进入 runtime inbox，再由各 writer 按
   `event_uuid`、epoch 和 sequence 幂等投影。全部必要投影完成后，runtime 才把
   inbox 标记为 processed。
5. 高频设备状态不经过 runtime inbox；telemetry writer 使用自己的 ingest batch、
   source cursor 和 epoch/sequence 直接去重，避免占用关键控制库。
6. telemetry latest 只能由更新的 observation 覆盖；sample/event 表使用稳定事件 UUID
   去重。晚到数据可以进入历史，但不能覆盖较新的 latest。

## 调度和错误边界

- 后端调度器是唯一调度权威；四库都不保存本地 DAG 或待调度队列。
- action availability 只是后端可重建的观测投影，不是 edge 调度锁。
- retry 是新的后端命令和新的 `job_uuid`，通过 `retry_of_job_uuid` 关联原 job。
- 非人工错误先写 error snapshot 并打开 terminal gate；收到后端确认调度已更新的命令后，
  才允许 adapter 对原 job 发布 failed。人工干预使用 replacement result，不改写原始历史。
- Site category 仅是前端提示，不参与 materials writer 的占用准入判断。
