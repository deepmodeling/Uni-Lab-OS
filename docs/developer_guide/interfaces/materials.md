# 物料、Site、库存与设备投影

本章描述 `materials.v1`。路径前缀为 `/api/v1/materials`；
完整字段与默认值见 [物料协议模型](https://github.com/deepmodeling/Uni-Lab-OS/blob/862ce87a38222ac2d51739f2f89e5daecd3cd907/unilabos/protocol/materials.py)，
路由与请求包装见 [物料 API 实现](https://github.com/deepmodeling/Uni-Lab-OS/tree/862ce87a38222ac2d51739f2f89e5daecd3cd907/unilabos/server/api/materials)。

## 数据模型：不要混淆四个层次

| 对象 | 身份 / 作用 | 主要接口 |
| --- | --- | --- |
| ResourceTemplate | `template_uuid` + `name`；资源定义、category、available_sites、渲染信息 | templates |
| Material | `material_uuid`；一件实际物料或设备资源的权威身份 | instances、trees |
| Site | `site_uuid` + `owner_material_uuid`；归属于具体实例的可占用位点 | 随物料聚合 / 树读取，move/transfer 更新占用 |
| Substance / Lot | Substance 是容器内容；Lot 是按量库存批次，二者不是同一张账 | instances/{uuid}/data、lots |

`MaterialAggregateRead` 包含 `material`、`position`、`position_version`、`data`、`sites`、`state_hash`。
身份、位置/几何和内容数据分开写，不要把 `data` 当成整个物料记录。
`MaterialTreeRead` 包含 `root_material_uuid`、`nodes`、`client_ref_map`、`snapshot_sequence`、`state_hash`；
`nodes` 是扁平聚合列表，通过 `material.parent_material_uuid` 还原树。

以下字段含义不同：

| 字段 | 本版本的解释 |
| --- | --- |
| template_name | 注册表/模板名称，不是 PLR Python 实现类 |
| config.type | **PLR 反序列化唯一允许读取的实现类信息** |
| resource_type / ResourceDict 外层 type | 业务资源类型，如 device/resource/deck；不能回退用它实例化 PLR |
| class_name / ResourceDict.klass（JSON alias 为 class） | 当前模型仍存在的字段；不能当作 config.type 的兜底 |
| category | `ResourceTemplateWrite.category: list[str]`；不是所有 MaterialIdentity 都有同名字段 |
| allowed_resource_categories | Site 的前端匹配提示；不是 allowed_resource_template_uuids 白名单 |

因此本版不能宣称已经彻底删除 `klass`/`class_name`；已落实的是 **PLR 不从外层 type/class 猜实现类**。
构造自定义 PLR 资源前应加载相应包/注册表，缺失或无法解析 `config.type` 必须修复定义，不能静默改成 Resource。
Site 的 category 匹配由画布提示/拦截；权威仍校验身份、父子关系、位点存在性与占用冲突，
“不强制 category”不等于“不检查位置一致性”。

## 创建：草稿不是已登记物料

正常过程是：本地草稿或注册表类 → 权威建树并分配 UUID → 读取返回的标准树 → 本地反序列化/挂载。
不得自行生成一份离线物料后直接当作已登记实例传给工作流。

两种入口：

1. `POST /instantiate`：payload 为 `MaterialInstantiate`，必填 `registry_class`、`name`，
   可选 `barcode`（默认 null）。适合没有 PLR 运行时的微前端；可选类由 `/registry-classes` 提供。
   权威展开为建树命令，使用 `operation="create_material_tree"`。
2. `POST /trees`：payload 为 `MaterialTreeCreate`。适合驱动/图同步提交已经序列化的完整草稿。
   `nodes` 必须非空、仅一个根、父节点在前、`client_ref` 唯一。
   Site 初始占用用 `occupied_client_ref` 引用树内节点，不携带自行分配的 site_uuid。

`MaterialNodeCreate.material_uuid` 默认不填，由权威分配；开机图等需保留外部身份的场景可显式填写，
这表示**有条件创建**，不是覆盖已存在同 UUID 的记录。
返回 `data.client_ref_map` 建立草稿引用到权威 UUID 的映射。

模板 `POST /templates` 不允许调用者指定 template_uuid；PUT 已有模板时路径与 payload 身份必须一致。
模板列表默认 `include_definition=false`，要拿完整定义应显式设为 true 或 GET 单项。
模板的 `name` 筛选是精确查询，不是模糊搜索。

## 写请求：幂等信封与完整 payload

多数业务写入使用 `InventoryMutation`：

```json
{
  "protocol_version": "materials.v1",
  "command_uuid": "本次命令的稳定唯一标识",
  "effect_key": "本次命令内的稳定效果标识",
  "operation": "move_material",
  "actor_type": "human",
  "actor_uuid": null,
  "job_uuid": null,
  "observed_at_ms": 0,
  "preconditions": [],
  "payload": {
    "material_uuid": "已存在的物料UUID",
    "destination_site_uuid": "已存在的Site UUID",
    "parent_material_uuid": null
  }
}
```

这里的占位值必须替换为实际身份；网络重发必须保留整份原始请求。
同一 `command_uuid + effect_key` 同内容重发返回 `replayed=true`；内容变化返回冲突。
actor、时间、preconditions 也参与请求哈希，重发不能重新生成时间或换操作人。

HTTP handler 先把 payload 解析为业务模型，再与原 JSON 比较；**非空 payload 必须包含模型默认字段和 null**。
不要只填写两三个业务字段而遗漏默认值。Python 调用方使用类型模型与 `bind_payload`：

```python
from uuid import uuid4
from unilabos.client.materials import bind_payload
from unilabos.protocol.materials import InventoryMutation, MaterialMove

def build_move_request(material_uuid: str, site_uuid: str) -> dict:
    payload = MaterialMove(
        material_uuid=material_uuid,
        destination_site_uuid=site_uuid,
    )
    mutation = InventoryMutation(
        command_uuid=str(uuid4()), effect_key="move:0",
        operation="move_material", actor_type="human",
    )
    return bind_payload(mutation, payload).model_dump(mode="json", exclude_none=False)
```

调用方保存该返回值，再 POST；不要每次重试都重新调用这个构建函数。
其他语言按该路由对应的 [物料协议模型](https://github.com/deepmodeling/Uni-Lab-OS/blob/862ce87a38222ac2d51739f2f89e5daecd3cd907/unilabos/protocol/materials.py)
补齐默认值；需要机器可读 Schema 时可在本地从该模型导出。MCP 的已适配物料工具会调用同一模型补齐，
不能据此假设原始 HTTP 也会替请求自动补齐。

可选 `preconditions` 中每项指定 aggregate_type、aggregate_uuid，
并至少带 expected_version 或 expected_state_hash；冲突时重新读取、让用户决定新命令，不能覆盖别人更新。

成功返回不是 Backend 的 code 信封，而是：

```text
MutationResult<T> = {
  protocol_version, command_uuid, effect_key, replayed, changed,
  ledger_sequence_start, ledger_sequence_end,
  affected: [{aggregate_type, aggregate_uuid, version, state_hash}],
  data: T
}
```

`changed=false` 不是所有“未变化”请求的通用成功结果：当前不少 handler 对 no-change 返回 409。

### 路由与 operation 对照

路径均省略 `/api/v1/materials`。下面列出推荐写法；同一格中的别名均由当前实现接受。

| 方法 / 路径 | operation | payload 模型 |
| --- | --- | --- |
| POST /templates；PUT /templates/{uuid} | create_template / put_template / sync_template | ResourceTemplateWrite |
| DELETE /templates/{uuid} | delete_template | 路径决定模板；见逐项参考 |
| POST /instantiate | create_material_tree | MaterialInstantiate，服务端展开成树 |
| POST /trees | create_material_tree / create_material | MaterialTreeCreate |
| PATCH /instances/{uuid} | patch_material / update_material | MaterialPatch |
| PUT /instances/{uuid}/position | put_position / update_position | MaterialPosition |
| PUT /instances/{uuid}/data | put_data / update_data / update_substances | MaterialDataWrite |
| DELETE /instances/{uuid} | delete_material | MaterialDelete，material_uuid 与路径一致 |
| POST /move | move_material | MaterialMove |
| POST /transfer | transfer_material | MaterialTransfer |
| POST /snapshots/apply | apply_material_snapshot | MaterialSnapshot |
| POST /snapshots/delta | apply_material_delta | MaterialDelta |
| POST /lots/inbound | inbound_inventory_lot | InventoryLotInbound |
| POST /reservations | reserve_inventory | InventoryReservationCreate |
| POST /reservations/batch | reserve_task_inventory | InventoryTaskReservationCreate |
| POST /reservations/{uuid}/consume | consume_inventory_reservation | InventoryReservationTransition |
| POST /reservations/{uuid}/release | release_inventory_reservation | InventoryReservationTransition |
| POST /reservations/{uuid}/quarantine | quarantine_inventory_reservation | InventoryReservationTransition |

**例外**：POST `/links` 直接接收 MaterialLinkUpsert，DELETE `/links/{uuid}` 不带 Mutation；
POST `/snapshots/compare` 直接接收 MaterialSnapshot（只比较）；POST `/notify-device` 接收 ResourceTreeNotify；
POST `/changes/ack` 接收 LedgerAcknowledge。不能给这些接口强行套 Mutation。

### 错误语义

| HTTP | 处理 |
| --- | --- |
| 404 | 目标不存在；先检查身份和权威归属，不要自动重建 |
| 409 | 占位/版本/幂等冲突、库存不足或内容无变化；读 detail 区分 |
| 410 | 已拒绝命令的通用重放错误；已知错误类型也会重放其原始状态码 |
| 422 | 模型、operation、payload 或树关系不合法 |
| 503 | transfer 的设备同步不可用/失败；**可能已经落库** |

已拒绝的命令会保留拒绝结果。修正参数、补货或处理冲突后是新的业务命令，应使用新幂等键；
网络未知结果或 transfer 投影同步失败则保留原键查询/重发，不能混为一谈。

## move、transfer、notify-device 的边界

| 接口 | 保证 |
| --- | --- |
| move | 原子更新权威父子关系、Site 占用与账本；不等于远端设备已经 load |
| transfer | 权威位置提交后，按顺序通知来源设备 unload、目标设备 load；全部确认才返回成功 |
| notify-device | 显式通知设备重载物料树；不代替权威创建/移动 |

transfer payload：source_device_id、target_device_id、items。
每个 item 为 material_uuid、target_material_uuid、target_site（Site UUID、label/index 或 null）。
同一请求不能重复物料或占据同一目标 Site；目标必须存在且未被其他物料占用。

执行顺序：

1. 验证所有目标，事务内更新权威位置和账本。
2. 用 `transfer_uuid=command_uuid` 向来源设备发送 `MaterialDeviceSync(action="unload")`。
3. 来源确认后，向目标设备发送 `action="load"`，设备从权威读取最新物料再挂载。
4. 返回 `MutationResult<MaterialTransferResult>`。

**目标 load 失败不回滚权威位置，也不把物料自动挂回来源**。503 后先查权威与设备，
恢复服务后用同一命令重试：数据库 replay，设备同步沿同一 transfer_uuid 重放。
如果物料已被后续命令移动，旧转运不能覆盖新位置。
这是软件投影同步，不代表软件会替代机械臂完成物理搬运。

ROS2 与 HostLink 的此路径共用 HostLink 下行 resource service，不能把 transfer 写成 ROS2 专有能力。
`notify-device` 响应中的 notified 只有 true 表示确认完成；false/null 都不能当成已经加载。

## 内容、快照与前端同步

`MaterialDataWrite` 是完整内容写入，含 data、substances、sites_initialized、unknown_counter、
state_status、来源 IDs 与 observed_at_ms。PUT 时不要无意把未编辑的 substances 用默认空列表覆盖。
`MaterialPosition.position_x/y/z` 必须全部为 null 或全部设置。

HTTP substances 使用具名对象，不使用三元组：

```json
{
  "substance_uuid": null,
  "name": "water",
  "quantity": 100.0,
  "quantity_unit": "uL",
  "physical_state": "liquid",
  "composition": [],
  "meta_data": {},
  "chemical_uuid": null,
  "chemical": null
}
```

`chemical_uuid` / `chemical` 的含义见下文“化学品定义”一节。

ResourceDict/PLR 侧可以使用 `(名称, 数量, 单位)` 三元组，经适配变成上述对象。
这不是“体积、质量、摩尔数三个数值字段”；协议用 quantity + quantity_unit 表达量，
不能假设后端会自动换算所有单位。

`snapshots/compare` 返回差异而不落库；apply 用完整同构快照更新既有聚合，
**不允许借快照创建/删除物料或 Site**。新增走 create，删除走显式 delete，跨设备走 transfer。
delta 用 MaterialDelta 描述允许变化的片段；不能把任意 JSON patch 当成合法 delta。
本地 observer 对 assign/unassign 与状态变化形成快照/diff，然后通过权威写入；普通 Python 属性赋值本身不是远端写入确认。

前端订阅 `/materials/events` 的 `materials.changed`，收到通知后 HTTP 拉实例/树/列表。
创建完成后“API 有物料但地图没有”应依次检查：权威读结果、parent/Site 归属、SSE 通知、前端缓存、设备 load。
具体重连与首次快照顺序见 [实时通信](realtime.md)，不要刷新页面或重复创建来修补丢失通知。

`/changes` 的完整账本提供来源 actor、command/job、版本、delta 与投递状态。
`/changes/ack` 是同步消费者确认，不是浏览器关闭提示的接口。

## 库存与物料锁

按件物料是有 UUID 的实例；按量 lot 记录数量、单位、批次/有效期与预留量。
工作流 `InventoryRequirement.kind` 决定需求类型：

- material：用 material_uuid 或 template_uuid 选择实例；不使用 lot/quantity/unit 字段。
- lot：quantity > 0 且 unit 必填，通过 lot_uuid 或 template_uuid 选择；不填写 material_uuid/父物料/Site 筛选。

字段组合的互斥与唯一性见 InventoryRequirement 模型验证器，不能在同一需求里混写两套。
任务批量预留先做整体准入，库存不足不能派发“部分成功”的实验。
reservation 的 consume/release/quarantine 由执行生命周期或明确业务操作驱动，观察前端不要自行消耗。

物料锁是另一层：动作的物料入参默认独占（`materials_free` 排除，`materials_need_lock` 显式覆盖，
规则见 [设计约定](design.md)），交给调度器按绑定后的身份占用。库存预留不自动等于设备动作锁，也不代表实现了多层子工作流锁升级。

## 化学品定义

`/api/v1/chemicals` 管理化学品身份（materials.db 的 `chemical` 表）：`chemical_uuid` 为主键，
CAS 登记号与 InChIKey 是可空的唯一键，只在未删除的行之间唯一——软删除后让出，可以重新登记。
CAS 校验格式与校验位，InChIKey 校验 27 位标准格式并统一大写；另有名称、英文名、别名、分子式、SMILES、
分子量、密度与物态（liquid / solid / gas / unknown）。修改与删除带 `expected_version`，过期返回 3003。

物料内容物通过可空的 `MaterialSubstance.chemical_uuid` 引用化学品定义（materials.db v3，
`material_substance.chemical_uuid`；化学品是软删除，故不设外键）。四条内容写入路径（建树、put_data、
快照、增量）落库前统一确定引用：

1. 显式给出 `chemical_uuid`：必须指向未删除的化学品，否则 422；该物料上已有的引用可原样回传（即使化学品已删除）。
2. 未给出：沿用同一物料同名（不区分大小写）内容物已有的引用。设备 / Host 按名称上报内容物，不会因此丢掉关联。
3. 仍没有：内容物名称与化学品的名称、英文名、任一别名或 CAS 不区分大小写精确相等，且恰好一个未删除化学品命中时自动关联；歧义或无命中保持 null。

读取物料（实例、树、列表、写入结果）时每份内容物附带只读的 `chemical` 摘要
（`chemical_uuid`、`name`、`cas`、`inchi_key`、`molecular_formula`、`smiles`、`molecular_weight`、
`density_g_per_ml`、`physical_state`），每次读取批量查询一次；引用的化学品已删除时 `chemical` 为 null、
`chemical_uuid` 保留。写入时 `chemical` 被忽略。物料聚合与 data 的 `state_hash` 包含 `chemical_uuid`、不含摘要，
编辑化学品不会改变物料 `state_hash`；快照 / 增量比较不看引用（设备不知道它），只改引用请用 put_data。

## 源码依据

- [协议模型](https://github.com/deepmodeling/Uni-Lab-OS/blob/862ce87a38222ac2d51739f2f89e5daecd3cd907/unilabos/protocol/materials.py)
- [HTTP 与错误映射](https://github.com/deepmodeling/Uni-Lab-OS/blob/862ce87a38222ac2d51739f2f89e5daecd3cd907/unilabos/server/api/materials/core.py)
- [事务、幂等、转运与库存服务](https://github.com/deepmodeling/Uni-Lab-OS/blob/862ce87a38222ac2d51739f2f89e5daecd3cd907/unilabos/server/services/materials/core.py)
- [设备侧 materials 门面](https://github.com/deepmodeling/Uni-Lab-OS/blob/862ce87a38222ac2d51739f2f89e5daecd3cd907/unilabos/resources/materials.py)
