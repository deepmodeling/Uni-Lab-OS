# 动作的参数、返回值与资源语义

JSON 动作（`@action()` 不写 `action_type`，以及没加 `@action` 的公开方法 `auto-<方法名>`）只有一个标准：
注册表按方法签名给每个动作生成 schema，调度端（建任务、运行预检、派发）与 Host 调用驱动前都按它校验参数，
驱动返回后按返回注解校验返回值，返回值不符进入失败决策。没有 opt-in 开关，也没有第二套宽松规则。

ROS 动作类型（`@action(action_type=SendCmd, goal={...}, result={...})`）不在此列：照旧按 ROS 消息生成 schema，
`goal` / `result` 字段映射照旧。

## 注解 → schema → 校验

| 参数写法 | schema | 校验 |
| --- | --- | --- |
| `str` / `int` / `float` / `bool`、`Literal[...]`、`list[X]`、`dict[str, JSONValue]` | 对应 JSON 类型 | 严格 |
| `Optional[X]`、`X \| None` | 可空 | 严格 |
| `x: X = None`（默认值是 `None`） | 同 `Optional[X]` | 严格 |
| `Annotated[X, Field(ge=, le=, min_length=, max_length=, title=, description=)]` | 带范围与说明 | 严格 |
| `ResourceSlot` / `list[ResourceSlot]` | 物料引用 `{"uuid": ...}`（或按库位绑定的 `{"site_uuid": ...}`，准入时解析），加物料锁 | 严格；Host 解析成物料实例 |
| `DeviceSlot` / `SiteSlot` | 设备 ID / 库位 UUID 字符串（字段带 `x-unilabos-slot: "device"` / `"site"`），加设备锁 / 库位锁 | 严格 |
| `SiteSlot` 且写了 `material_contract=MaterialRole.site_picker("site", of=...)` | 库位 UUID、所属方下的库位名（工作流里写死的工位，派发前解析成库位 uuid），或按所属物料选择的 `{"owner_uuid": ...}`（准入时选定）；带库位关系（`x-unilabos-site-selector`），不限定 uuid 格式 | 严格；预检与派发前核对库位归属与占用 |
| `dict`、`Dict[str, Any]`、`Mapping` | 任意 JSON 对象 | 只校验是对象 |
| 没写注解、`Any`、`object` | 任意 JSON 值（`{}`） | 不限值 |
| 严格解析器不认识的注解（`Union[int, str]`、`List[Optional[float]]`、枚举、PLR / pydantic 类型……） | 退回注册表原来的签名类型推断 | 按推断结果；推断不出的部分接受任意值 |
| `**kwargs` | goal 允许多传字段 | 多传的字段不校验 |
| `*args` | 无法表示，忽略并按定义告警一次 | — |

- **没有默认值的参数就是必填**：缺了在调度端 / Host 调用前报「动作参数缺少必填字段：…」，不再被补成 0 / 空串。
  `goal_default`（`@action(goal_default=...)`）只是编辑器里的初始参数值，叠加在签名默认值上，不改变参数是否必填。
- 参数名之外的字段一律拒绝（`**kwargs` 除外）；框架注入的 `sample_uuids` 与动作上下文（见下节）不算参数。
- 物料参数（`ResourceSlot`）接受框架的几种引用形态：`{"uuid": ...}`（可带 id / name）、只给名字的
  `{"name": ...}`（派发前由调度器向物料权威解析出 uuid）、上游端口传来的扁平资源树（列表）、uuid 字符串；
  能否解析出权威 uuid 由派发前的物料锁检查保证。不可空的物料参数给空值（`{}` / `""` / `[]`）报
  「物料参数 … 没有给物料」；可空的物料参数留空等于没给。
- `ResourceSlot` 只指物料。设备不是物料：给物料参数传设备（或它解析到设备）时 Host 在调用驱动前报错，指出参数与设备。
  参数要的是设备时注解为 `DeviceSlot`（值是设备 id，原样交给驱动，不做物料转换）。
- 物料参数还可以**按库位绑定**：`{"site_uuid": "<库位 uuid>"}`（`list[ResourceSlot]` 的每一项也可以），表示「此刻占着这个
  库位的物料」。工作流入口参数、节点静态参数、单点动作参数都可以这样写。建任务与运行预检走同一条解析路径
  （`server/services/runtime/workflow/material_sites.py`，物料权威只读事实 `POST /api/v1/inventory/site-admission`）：
  换成占用物料的 `{"uuid": ...}`，参数写了 `AllowedResourceTemplates(...)` 时核对占用物料的模板，结果冻结进任务
  （计划节点参数、attempt 参数、任务输入；计划的 `material_site_bindings` 记录哪个参数按哪个库位绑到了哪件物料），
  之后物料被挪走也不改变任务用的物料；从此它就是普通物料参数（默认共享物料锁、派发前核对照旧）。库位不存在、库位为空、
  物料模板不符都是预检错误：建任务返回 `error.code=material_site_invalid`（1000），`error.details` 带 `reason`
  （`site_not_found` / `site_empty` / `template_mismatch`）、`parameter`、`site_uuid`、`node_uuid`；运行预检给
  `invalid_material_site` 检查，细节相同。
- 不认识的写法只会让该参数退回推断，**不会让动作注册失败**。

| 返回注解 | schema | 校验 |
| --- | --- | --- |
| 没写 | `{}` | 不校验 |
| `None` | `null` | 只接受空返回 |
| `dict`、`Dict[str, Any]` | 任意对象 | 只校验是对象 |
| 同模块的 `TypedDict` / `@dataclass(frozen=True)` | 具名输出：每个字段一个输出端口 | 字段齐全、不多字段；字段逐个按上表解析，不认识的退回推断 |
| `bool`、`float`、`list[str]` 等 | 对应 JSON 类型 | 严格 |
| 其他（如从别的模块导入的 TypedDict） | 退回推断 | 按推断结果 |

`unilabos_samples` 是 Host 剥离到结果外层的保留字段，不进返回 schema。`--test_mode` 下没声明 `run_in_test_mode = True`
的驱动不执行、由 Host 模拟返回值，模拟值不按返回注解校验。

## 动作上下文：`ActionContext`

动作需要发布反馈、检查取消或知道自己是哪一次执行时，加一个注解为 `ActionContext` 的参数，框架按**类型**注入，
参数名随意：

```python
from typing import Optional
from unilabos.client.host.runtime.action import ActionContext

@action(display_name="加热")
def heat(self, sample: ResourceSlot, seconds: float, ctx: Optional[ActionContext] = None) -> None:
    for step in range(10):
        if ctx is not None:
            ctx.raise_if_cancelled()
            ctx.publish_feedback({"progress": step * 10})
```

| 注解 | 有上下文时 | 没有上下文时（ROS 2 执行、作者直接调用） |
| --- | --- | --- |
| `Optional[ActionContext]`、`ActionContext \| None`、`ActionContext = None` | 注入 | `None` |
| `ActionContext` | 注入 | 框架现建一个（`action_id` 随机，没有反馈回调，不会被取消） |

- HostLink 执行（Host 派发、跨设备调用 `node.call_device_action[_async]`、工作站协议子步骤、`--test_mode` 下
  真实执行的驱动）总有上下文；ROS 2 执行没有框架上下文。HostLink 本地执行、`host_node`、ROS 2 设备节点、
  跨设备调用都经同一个判定（`client/host/runtime/action.py:bind_action_context`）。
- 这些参数不属于动作接口：不进 goal schema、必填列表、`placeholder_keys`、生成的端口、物料锁参数与 `goal_default`
  （AST 扫描、装饰器与运行时签名推断同一规则，`registry/action_context.py`）。goal 里同名的值被框架的上下文覆盖。
- 字段含义：`action_id` 是**这一次 attempt** 的标识。调度派发的作业里它就是 attempt 的 job uuid（runtime.v1
  `job_uuid`，`/workflow-node-jobs/{job_uuid}`），每次重试都换一个新值；同一节点的节点运行 uuid（`node_run_uuid`）
  在重试之间不变，驱动拿不到它，需要跨重试稳定的身份时由调用方作为普通参数传入。没有调度作业的调用（跨设备调用、
  直接调用）里 `action_id` 是随机值。`publish_feedback(data)` 发布动作反馈；`raise_if_cancelled()` / `is_cancelled`
  协作式检查取消（硬超时也会请求取消）。

schema 的 `x-unilabos-action-contract` 扩展（第 2 版）记录参数与输出顺序、资源模板约束和资源语义，建任务时冻结进执行计划。
手写 YAML 注册表里的 JSON 动作要跑一次 `unilab --complete_registry`，才会按同一标准重新生成 schema；
没有这个扩展的 schema（ROS 动作类型，或还没补全的手写 YAML）不做上面的校验。

## 资源语义：`material_contract=`（实验性）

**整个 `material_contract=` 字段都是实验性功能**：写了就在注册时按定义告警一次「material_contract= 是实验性功能（用到的声明）：语义与写法
以后可能调整」，装饰器（导入时）与注册表 AST 扫描共用同一身份，同一定义只告警一次。改名前的 `material=`（见「不兼容变更」）
与短暂出现过的 `materials=` 注册时都直接报错并提示改成 `material_contract=`，不会被静默忽略。

`MaterialRole` 声明的是动作**真实的物料动作**：搬运一件容器、两步搬运、在容器之间转移内容物。调度器在派发前按它解析
来源、目标与搬运设备，动作成功后由核心一次结算物料位置 / 内容物（`PhysicalMove` / 搬运回执、`AliquotReceipt`）。
不代表物料动作的声明（跨动作的设备独占、原位检查、库位选择器提示、顺序约束）也写在 `material_contract=` 里。

一个动作的语义只用 `@action` 的这一个字段声明，值是 `MaterialRole` 的一个方法调用，多个语义写成列表。
字符串参数都是动作的参数名，注册时对照签名检查参数存在、类型正确：

| 角色 | 参数类型 | 值 |
| --- | --- | --- |
| 物料（`material`、`from_material`、`to_materials`、`for_material`） | `ResourceSlot`（`to_materials` 可以是多个参数） | 物料引用；`ResourceSlot` 不能指向设备 |
| 库位（`to_site`、`from_site`、`site_picker` 的库位参数） | `SiteSlot` | 库位 uuid（带 `site_picker(of=...)` 关系时也可以是所属方下的库位名） |
| 设备（`carrier_device`、`device`、`from_device`） | `DeviceSlot` | 设备 id；省略就是**执行设备** |

```python
from unilabos.registry.decorators import MaterialRole, action
from unilabos.registry.placeholder_type import DeviceSlot, ResourceSlot, SiteSlot

@action(display_name="搬运板", material_contract=MaterialRole.move("plate", to_site="target_site"))
def move_plate(self, plate: ResourceSlot, target_site: SiteSlot) -> dict: ...

@action(display_name="抓取", material_contract=MaterialRole.pick("plate"))
def pick_plate(self, plate: ResourceSlot) -> dict: ...

@action(display_name="放下", material_contract=MaterialRole.place("plate", to_site="target_site"))
def place_plate(self, plate: ResourceSlot, target_site: SiteSlot) -> dict: ...

@action(display_name="移液", material_contract=MaterialRole.dispense(from_material="source", to_materials=["target"]))
def transfer_liquid(self, source: ResourceSlot, target: ResourceSlot, volume: float) -> dict: ...
```

### 物料动作

| 写法 | 含义 | 内部合同字段 |
| --- | --- | --- |
| `MaterialRole.move("plate", to_site="target_site", from_site=None, carrier_device=None)` | 一个动作把容器搬到 `to_site` | `transfer` |
| `MaterialRole.pick("plate", from_site=None, carrier_device=None)` | 两步搬运第一步：从来源库位抓到搬运设备的夹爪库位 | `transfer_step`（pick） |
| `MaterialRole.place("plate", to_site="target_site", carrier_device=None)` | 两步搬运第二步：从夹爪库位放到 `to_site` | `transfer_step`（place） |
| `MaterialRole.dispense(from_material="source", to_materials=["target", ...])` | 按驱动回报的实际量，在容器之间转移内容物 | `aliquot`（`AliquotReceipt`） |

- **所属方由库位决定**：`to_site` / `from_site` 是库位 uuid，调度器向物料权威读出库位的所属方——可以是载架
  （仓库、板位……），也可以是设备根节点（设备自身的库位），不再单独写所属物料参数。
- **来源**：`from_site` 省略时用物料权威记录的当前位置；给出时必须和权威一致，不一致时动作排队等待
  （`wait_reason.code=transfer_source_site_missing`），物料被规划后挪走则进入动作执行前预检决策。
- **搬运设备**：`carrier_device` 省略时就是执行设备；写了就必须是 `DeviceSlot` 参数（值是设备 id，调度器解析成该设备的物料）。
  搬运途中物料在搬运设备角色为 `gripper` 的夹爪库位上：设备的 `available_sites` 里必须有且只有一个可见的、
  `meta_data.role == "gripper"` 的库位。`move` 期间这个库位被预留、夹爪必须空着（或正夹着要搬的这件物料），
  动作成功后物料直接结算到 `to_site`。
- **两步搬运**：`pick` 成功后物料在夹爪库位上，任务托管搬运设备（跨动作的设备独占，其他任务不能用它）；`place` 要求物料
  仍在同一夹爪库位上，成功后物料在 `to_site`、夹爪库位空出、托管释放。`pick` 的 `from_site` 省略时从权威当前位置抓取。
- **分装 / 移液**：`dispense` 的来源与每个目标都是 `ResourceSlot`；驱动按实际量回报 `AliquotReceipt`，核心按回执结算内容物。
  语义与以前的 `Material.aliquot` 完全相同，只是改名。
- 这几种都会锁住涉及的物料（物料锁）与来源 / 目标库位所属的设备（设备锁）；`move` / `pick` / `place` 之间不能组合
  （一个动作只能有一种搬运声明），`dispense` 可以与下面不代表物料动作的声明同时写。
- 物料的**消耗 / 新建**这次不做声明式写法：消耗仍然用工作流节点上的 `inventory=`，新建仍在驱动里调用 `materials.create`（见下节）。

### 声明式 `MaterialRole` 与运行时 `materials` 的分工

两者都会改变物料权威里的事实，但时机和归属不同：

| | 声明式 `MaterialRole`（`registry/material_roles.py`） | 运行时 `materials`（`unilabos.resources.materials`） |
| --- | --- | --- |
| 写在哪里 | `@action(material_contract=...)` | 驱动代码里，动作执行时调用 |
| 谁读 / 谁执行 | 调度器派发前读：解析来源、目标、搬运设备，加锁、预留库位；动作成功后**核心**结算 | 驱动直接调用，立即改权威 |
| 典型操作 | `move` / `pick` / `place`（`PhysicalMove` / 搬运回执）、`dispense`（`AliquotReceipt`） | `create` / `ensure` / `get` / `search` / `resolve` / `assign` / `update` / `remove` / `snapshot` / `transfer` / `apply_substances` …… |

规则：

- 用 `MaterialRole.move` / `pick` / `place` 声明了的搬运，物料位置由核心结算；驱动**不能**再对同一件物料调用
  `materials.transfer` / `materials.assign`（会重复写位置，与核心的结算冲突）。驱动只负责物理动作：动作成功而结果里
  没有 `material_transfer_receipts` 时，核心按派发前冻结的来源 / 目标结算；驱动给了搬运回执，就必须与冻结计划完全一致
  （否则结算被拒绝，转人工核实）。人工补记结算（任务终止后）仍要逐条给出实际位置。
- `dispense` 声明了的内容物转移由核心按 `AliquotReceipt` 结算；驱动不再对同一来源 / 目标手动写内容物。
- `materials.*` 只用于**没有声明**的运行时变化：新建物料（`create`）、写内容物（`apply_substances`，用于没有 `dispense` 的动作）、
  删除（`remove`）、启动时对齐（`ensure`）、查询（`get` / `search` / `resolve` / `snapshot`）等。
- `create` / `ensure` 这次没有声明式对应写法。

### 不代表物料动作的声明

同样写在 `material_contract=` 里（与整个字段一样是实验性功能）。

| 写法 | 含义 | 内部合同字段 |
| --- | --- | --- |
| `MaterialRole.load("sample", device=None, from_device=None)` | 装进设备，任务托管设备直到卸载；`from_device` 同时从另一台设备转出 | `device_tenancy`（取得设备，可同时释放 `from_device`） |
| `MaterialRole.unload("sample", device=None)` | 从设备取出，结束托管 | `device_tenancy`（释放设备） |
| `MaterialRole.in_place("sample")` | 物料必须仍在执行设备内（由之前的 `load` 托管着） | `operate_in_place` |
| `MaterialRole.site_picker("site", of=None, for_material=None, occupied="show")` | 前端库位选择器提示：库位参数 `site` 列出 `of` 的库位 | 不进资源合同：写进 `site` 参数 schema 的 `x-unilabos-site-selector` |
| `MaterialRole.order_sensitive()` | 同一资源上的兄弟动作必须显式排序（只约束计划） | `order_sensitive` |

- `load` / `unload` 的 `device` / `from_device` 是 `DeviceSlot` 参数，省略就是执行设备；语义与以前相同（设备间转移用
  `from_device=`；`load` 与 `unload` 不能写在同一个动作上）。
- 同一个合同字段只能出现一次。内部仍规范化成第 2 版资源合同，调度器按它执行托管、搬运与分装结算。

### 按类型加锁

参数一律按**类型**校验与加锁，不再按动作声明额外占用（`Material.uses` 已删除）：

| 参数类型 | 锁 |
| --- | --- |
| `ResourceSlot` / `list[ResourceSlot]`（以及选择器为 `unilabos_resources` 的参数） | 物料锁（现状） |
| `DeviceSlot` / `list[DeviceSlot]` | 设备锁：值是设备 id，派发前核对它是已登记的设备，与该设备上正在执行的动作互斥 |
| `SiteSlot` | 库位锁：值是库位 uuid，按「所属物料 + 库位」加锁；值是 `MaterialRole.site_picker` 所属方下的库位名时按解析出的库位加锁；两者都解析不出时不加锁 |

空值（`None` / `""` / `[]`）不加锁。`always_free` 只免除执行设备自身的动作锁，按类型加的设备锁 / 库位锁照旧。

### 库位参数：`SiteSlot` 与 `MaterialRole.site_picker`

库位参数只有一个类型 `SiteSlot`（值是库位 UUID，前端用库位选择器填写）。选择器列出哪个所属方的库位、放进去的是哪件物料、
已占用的库位怎么处理，写在 `MaterialRole.site_picker`：

```python
from unilabos.registry.decorators import MaterialRole, action
from unilabos.registry.placeholder_type import ResourceSlot, SiteSlot

@action(
    display_name="放到库位",
    material_contract=MaterialRole.site_picker("site", of="carrier", for_material="sample", occupied="show"),
)
def place(self, carrier: ResourceSlot, sample: ResourceSlot, site: SiteSlot = None) -> None: ...
```

- `site` 必须注解为 `SiteSlot`（可空写法 `SiteSlot = None` / `Optional[SiteSlot]` 也行）。
- `of` 是所属方：单个 `ResourceSlot`（所属物料）或 `DeviceSlot`（所属设备的根节点）参数；省略时是执行设备的根节点。
- `for_material` 是要放进这个库位的物料参数（单个 `ResourceSlot`，可省略）。
- `occupied`：`"hide"` 不列出已占用的库位，`"show"`（默认）列出但不能选，`"allow"` 列出且能选。
  运行预检与派发前核对所选库位属于所属方，且（不是 `"allow"` 时）没有被 `for_material` 以外的物料占着。
- 生成的 `x-unilabos-site-selector` 扩展形状不变：`{"version": 1, "owner": <of>, "occupant": <for_material>,
  "show_occupied": ..., "allow_occupied": ...}`；`of` 省略时 `owner` 为 `null`（执行设备根节点），`of` 是 `DeviceSlot`
  参数时它的值是设备 id。
- 只写 `SiteSlot`、不写 `MaterialRole.site_picker` 的参数照旧是库位选择器，只是没有这层关系的核对。
- **工作流里写死的工位**：库位 uuid 由物料权威在建树时发号，同一个设备图在不同部署里的库位 uuid 不同，工作流源码
  不能写死库位 uuid。带 `MaterialRole.site_picker(of=...)` 关系的库位参数可以改给所属方下的**库位名**（或序号），例如
  `{"target_owner": {"uuid": <载架 uuid>}, "target_site": "position_x15_y01"}`：运行预检与派发前在 `of` 所属方下唯一
  匹配，解析出的库位用于归属 / 占用核对、库位锁和 `MaterialRole.move` / `place` 的目标（`from_site` 同样）。所属方下匹配不到
  或不唯一时报错；没有 `site_picker` 关系的库位名无从解析，派发前报错。

#### 按所属物料选库位：`{"owner_uuid": ...}`

带 `MaterialRole.site_picker` 关系的库位参数（以及工作流入口参数里的 `SiteSlot`）可以不给库位 uuid，而给
`{"owner_uuid": "<所属物料 uuid>"}`，由调度器在**任务准入**时挑一个库位：

- 候选是所属物料下（可见的）库位里，接收规则接受放入物料（`site_picker` 的 `for_material` 参数；它按库位绑定时先解析）的：
  库位模板白名单（`extra.allowed_resource_template_uuids`，6008 那一条）与 `allowed_resource_categories`（放入物料模板的
  `category` 要与它相交）都满足，空列表表示不限。按库位序号选第一个空着、没有活动入口预留、没有未结算物理动作占着的。
- 选中后用物料权威的入口预留占住（`POST /api/v1/inventory/ingress-reservations` 的 `owner_material_uuid` 写法，
  `backend_task_uuid` 为任务 uuid），再把库位 uuid 冻结进计划节点参数、attempt 参数与任务输入；计划的 `site_selections`
  记录所属物料、放入物料、选中的库位与预留。同一个工作流入口参数用在多个节点里只选一次。
- 用到这个库位的动作派发时预留转 `in_transit`：放入物料被直接放进这个库位等同接收（预留置为 `received`），其他物料仍被挡；
  任务结束时收尾（`POST /api/v1/inventory/ingress-reservations/finish-task`：物料已在库位上记 `received`，否则取消）。
- 所属物料下没有接受放入物料的库位是预检错误：任务在准入时失败，`error_info[0]` 为 `plan_not_executable`
  （`stage=preflight`、`reason=no_acceptable_site`，`details` 带参数、节点、所属物料）；运行预检给 `invalid_site_selection`。
  接受的库位此刻都被占着或已被预留是临时条件：任务保持 `pending` 排队，attempt 的 `wait_reason.code=site_group_unavailable`，
  有任务结束或 5 秒兜底重算时按创建先后重试；运行预检给 `site_selection_unavailable`（可以提交，提交后排队）。
- 写法本身不对时建任务就拒绝（`error.code=site_selection_invalid`，`error.details.reason`）：参数没有 `MaterialRole.site_picker` 关系
  （`no_site_relation`）、关系没有声明 `for_material`（`occupant_undeclared`）、放入物料在建任务时不确定（来自上游结果，
  `occupant_unknown`）、与 `of` 参数给的物料不一致（`owner_mismatch`）。
- 给精确的库位 uuid 照旧，不经过选择与预留。

## 前端选择器：`placeholder_keys`

`placeholder_keys` 只决定前端用哪种选择器填写一个参数（`unilabos.registry.placeholder_type` 的 `PLACEHOLDER_*`）。
注册表按类型推出一份，`@action(placeholder_keys={...})` 手写一份，最终给前端的（注册表条目的 `placeholder_keys`、
schema 里的 `_unilabos_placeholder_info`）是**两者的并集，同一参数两边不同时以手写的为准**：

| 类型 | 推出的选择器 |
| --- | --- |
| `ResourceSlot` / `list[ResourceSlot]` | `unilabos_resources` |
| `DeviceSlot` / `list[DeviceSlot]` | `unilabos_devices` |
| `SiteSlot` | `unilabos_sites` |

```python
from unilabos.registry.decorators import action
from unilabos.registry.placeholder_type import (
    PLACEHOLDER_DEDUCT_RESOURCE,
    PLACEHOLDER_MANUAL_CONFIRM,
    PLACEHOLDER_NODES,
    ResourceSlot,
)

# 出库扣减：网页上在仓储里点选要出库的物料（服务端扣减后回传实例的 uuid），挂载目标用节点选择器；
# 执行时两个参数收到的都是解析好的物料实例（ResourceSlot：解析成物料实例、加锁、按物料引用校验）
@action(placeholder_keys={"resource": PLACEHOLDER_DEDUCT_RESOURCE, "mount_resource": PLACEHOLDER_NODES})
def apply_deduct_resource(self, resource: ResourceSlot = None, mount_resource: ResourceSlot = None): ...

# 没有专门类型的选择器（节点、人工确认指派人、出库 / 试剂扣减……）一律手写
@action(placeholder_keys={"assignee_user_ids": PLACEHOLDER_MANUAL_CONFIRM})
def confirm(self, timeout_seconds: int, assignee_user_ids: list[str]): ...
```

- 手写的只改显示：参数是什么（是不是物料、要不要解析成物料实例、怎么校验）仍由类型决定。
- 物料锁取并集：`ResourceSlot` 类型的参数（含列表 / Optional），加上选择器（合并后）为 `unilabos_resources` 的参数。
  后一半是签名看不到的条目标出物料的唯一办法：手写 YAML、ROS / 协议动作的字段都靠它加锁。手写成 `unilabos_resources`
  的非 `ResourceSlot` 参数只加锁，类型与校验不变；`ResourceSlot` 参数换了显示（如上面的出库扣减）照样加锁。
- `unilabos_sites` 的值是库位 UUID：程序化导入工作流时按它把库位标签解析成 `site_uuid`。它只给 `SiteSlot` 或字符串参数用，
  物料参数要换显示，用出库扣减、节点这类选物料的选择器。
- 键必须是动作的参数名（ROS 动作类型还可以是 `goal=` 映射的字段名），否则注册时报错；`parent=True` 与协议动作的签名
  在注册时看不到，不核对。值不是已知种类时按定义告警一次、照常写入（前端以后可能加新的种类）。
- 手写 YAML 注册表里已有的 `placeholder_keys` 同样算手写：`--complete_registry` 补全时与类型推出的取并集，手写优先，
  不是参数的键丢弃。

## 不兼容变更

以下写法已删除，导入、装饰器（导入时）和注册表 AST 扫描（`--check_mode`、启动扫描）都直接报错，错误信息给出新写法；
不做转换，也不只是告警。

| 删除的写法 | 新写法 |
| --- | --- |
| `@action(contract="typed")` / `contract="legacy"` | 去掉：所有 JSON 动作同一标准 |
| `@action(resource_contract={...})` | `@action(material_contract=MaterialRole.xxx(...))`，对照表见上 |
| `@action(material=Material.xxx(...))` / `material=[...]`（字段改名前的写法） | `@action(material_contract=MaterialRole.xxx(...))` / `material_contract=[...]`：字段名改为 `material_contract=`，声明类改为 `MaterialRole`（见下一行），方法与参数不变；`material_contract=` 是实验性功能，写了就按定义告警一次 |
| `Material.xxx(...)`（声明类 `Material`，`from unilabos.registry.decorators import Material`） | `MaterialRole.xxx(...)`（`from unilabos.registry.decorators import MaterialRole`）：只改类名，方法与参数不变，与运行时物料操作模块 `unilabos.resources.materials` 区分开。`Material` 仍可导入，但 `Material.xxx(...)` 在导入时与 AST 扫描里都直接报错并给出新写法 |
| `Annotated[T, Placeholder(PLACEHOLDER_...)]`（`unilabos.registry.annotations.Placeholder`） | `@action(placeholder_keys={"参数名": PLACEHOLDER_...})`，参数照常注解为 `T` |
| `Annotated[str, SiteSelector(owner=..., occupant=..., show_occupied=..., allow_occupied=...)]`（`unilabos.registry.annotations.SiteSelector`） | 参数注解为 `SiteSlot`，关系写成 `@action(material_contract=MaterialRole.site_picker("参数名", of=..., for_material=..., occupied=...))`；生成的 schema 扩展与以前相同 |
| JSON 动作的 `@action(goal={...})` / `result={...}` 重命名 | 去掉：字段名就是参数名 / 返回 TypedDict 的字段名（要改名就改参数名） |
| 按名字注入的动作上下文：参数叫 `action_context` 但没有 `ActionContext` 注解 | 注解为 `Optional[ActionContext]`（`from unilabos.client.host.runtime.action import ActionContext`），名字随意；没有注解的 `action_context` 参数注册时报错（自动动作同样） |

### 资源语义辅助 API 重做（一次性不兼容）

资源语义改为声明真实的物料动作（见「资源语义」），声明类改名为 `MaterialRole`。下面的旧写法在装饰器（导入时）与注册表 AST 扫描里都直接报错，
错误信息给出新写法；不做转换。

| 删除的写法 | 新写法 |
| --- | --- |
| `Material.transfer("plate", to="owner", site="site" \| site_name="site", gripper="arm.gripper", source=..., source_site=..., source_site_name=..., motion=..., tools=...)` | `MaterialRole.move("plate", to_site="site", from_site=None, carrier_device=None)`：库位参数改成 `SiteSlot`（值是库位 uuid），所属方由库位决定，不写 `to` / `source`；搬运设备的夹爪库位角色统一为 `gripper`，不写 `gripper=`；`motion` / `tools` 去掉（设备参数按类型加锁） |
| `Material.pick("plate", owner="owner", site="site", carriers=["carrier"])` | `MaterialRole.pick("plate", from_site=None, carrier_device=None)`：搬运设备是 `DeviceSlot` 参数，省略即执行设备 |
| `Material.place("plate", owner="owner", site="site", carriers=["carrier"])` | `MaterialRole.place("plate", to_site="site", carrier_device=None)` |
| `Material.aliquot(source="source", targets=["target"])` | `MaterialRole.dispense(from_material="source", to_materials=["target"])`（语义不变） |
| `Material.site("site", owner="carrier", occupant="sample", show_occupied=..., allow_occupied=...)` | `MaterialRole.site_picker("site", of="carrier", for_material="sample", occupied="hide" \| "show" \| "allow")`（`show_occupied=False` → `"hide"`；`allow_occupied=True` → `"allow"`；其余 → `"show"`） |
| `Material.uses(device=[...], tool=[...], motion=[...], site=[...], material=[...])` | 删除：参数按类型加锁（`ResourceSlot` 物料锁、`DeviceSlot` 设备锁、`SiteSlot` 库位锁），把参数注解成对应类型即可 |
| `Material.load("sample", device="vessel")`（`vessel` 是 `str` / `ResourceSlot`） | `vessel` 注解为 `DeviceSlot`；装进执行设备本身时省略 `device=` |

行为上的变化：`DeviceSlot` / `SiteSlot` 参数现在默认加设备锁 / 库位锁；字段改名为 `material_contract=`，整个字段（包括
`move` / `pick` / `place` / `dispense`）注册时按定义告警一次「实验性功能」；搬运设备的夹爪库位角色统一为 `gripper`，已部署的物料权威里旧角色名
（如 `robot.gripper`）不会随设备声明自动更新，需要重建或改写这些库位的 `meta_data.role`；声明的搬运在动作成功后可以不交
搬运回执（核心按冻结计划结算）；`site_picker` 的库位参数不再限定 uuid 格式（也可以给所属方下的库位名）。

行为上的变化：

- 以前的普通动作不校验参数，必填参数缺了会被 `goal_default` 补成 0 / 空串；现在缺了直接报错。
- 以前的普通动作不校验返回值；现在写了返回注解就按它校验，不符进入失败决策（例如注解 `-> bool` 却返回字典）。
- 以前没写 `handles=` 的普通动作在注册表里没有端口；现在按 schema 生成（每个参数一个输入端口，具名输出各一个输出端口，
  物料参数另有同名的透传输出）。写了 `handles=` 的动作照旧用写的端口。
- 统一 schema 不再给普通动作的 goal 注入 `unilabos_device_id`（以前上报云端时注入，云端适配层会剥离它）。
- `@action(materials_need_lock=...)` / `materials_free=...` 仍按此前的决定忽略并告警（物料参数一律加锁）。
