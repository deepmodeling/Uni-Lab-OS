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
| `DeviceSlot` / `SiteSlot` | 设备 ID / 库位 UUID 字符串 | 严格 |
| `SiteSlot` 且写了 `material=Material.site("site", owner=...)` | 库位 UUID，带库位关系（`x-unilabos-site-selector`） | 严格；预检与派发前核对库位归属与占用 |
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

## 资源语义：`material=`

一个动作对物料 / 设备的语义只用 `@action` 的一个字段声明，值是 `Material` 的一个方法调用，多个语义写成列表。
字符串都是动作的参数名，注册时对照签名检查（参数不存在、物料参数不是 `ResourceSlot` 都会注册失败）。

```python
from unilabos.registry.decorators import Material, action
from unilabos.registry.placeholder_type import ResourceSlot

@action(display_name="装载样品", material=Material.load("sample", device="vessel"))
def load_sample(self, sample: ResourceSlot, vessel: str) -> CustodyResult: ...

@action(display_name="原位处理", material=[Material.in_place("sample"), Material.order_sensitive()])
def stir(self, sample: ResourceSlot) -> None: ...
```

| 写法 | 含义 | 内部合同字段 |
| --- | --- | --- |
| `Material.load("sample", device="vessel")` | 装进设备，任务托管设备直到卸载 | `device_tenancy`（取得设备） |
| `Material.unload("sample", device="vessel")` | 从设备取出，结束托管 | `device_tenancy`（释放设备） |
| `Material.load("sample", device="b", from_device="a")` | 设备间转移：从 a 转到 b | `device_tenancy`（取得 b、释放 a） |
| `Material.in_place("sample")` | 物料必须仍在执行设备内 | `operate_in_place` |
| `Material.pick("sample", owner="owner", site="site", carriers=["carrier"])` | 搬运第一步：抓到搬运器 | `transfer_step`（pick） |
| `Material.place("sample", owner="owner", site="site", carriers=["carrier"])` | 搬运第二步：放到目标库位 | `transfer_step`（place） |
| `Material.transfer("plate", to="target", site="site", gripper="arm.gripper", source=..., source_site=..., source_site_name=..., site_name=..., motion=[...], tools=[...])` | 一次完成的机械臂搬运 | `transfer` |
| `Material.aliquot(source="source", targets=["target"])` | 按实测量从来源分装到全部目标 | `aliquot` |
| `Material.uses(device=[...], tool=[...], motion=[...], site=[...], material=[...])` | 动作还占用这些资源参数 | `resource_params`（取代 `required_device_params`） |
| `Material.order_sensitive()` | 同一资源上的兄弟动作必须显式排序（只约束计划） | `order_sensitive` |
| `Material.site("site", owner="carrier", occupant="sample", show_occupied=True, allow_occupied=False)` | 库位参数 `site` 属于物料参数 `carrier`，放进去的是 `sample`；已占用的库位是否列出 / 能否选 | 不进资源合同：写进 `site` 参数 schema 的 `x-unilabos-site-selector` |

同一个合同字段只能出现一次（例如 `load` 与 `unload` 不能写在同一个动作上，设备间转移用 `from_device=`）；
`uses` 可以和其他语义同时写。内部仍规范化成第 2 版资源合同，调度器按它执行托管、搬运与分装结算。

### 库位参数：`SiteSlot` 与 `Material.site`

库位参数只有一个类型 `SiteSlot`（值是库位 UUID，前端用库位选择器填写）。库位属于哪个物料、放进去的是哪个物料、
已占用的库位怎么处理，属于资源语义，写在 `material=` 里：

```python
from unilabos.registry.decorators import Material, action
from unilabos.registry.placeholder_type import ResourceSlot, SiteSlot

@action(
    display_name="放到库位",
    material=Material.site("site", owner="carrier", occupant="sample", allow_occupied=False),
)
def place(self, carrier: ResourceSlot, sample: ResourceSlot, site: SiteSlot = None) -> None: ...
```

- `site` 必须注解为 `SiteSlot`（可空写法 `SiteSlot = None` / `Optional[SiteSlot]` 也行）；`owner` 必填，`occupant` 可省略；
  `owner` / `occupant` 必须是单个 `ResourceSlot` 参数。写错在注册时报错。
- `show_occupied`（默认 `True`）决定前端是否列出已占用的库位，`allow_occupied`（默认 `False`）决定已占用的库位能否选；
  运行预检与派发前核对所选库位属于 `owner`，且（不允许占用时）没有被 `occupant` 以外的物料占着。
- 只写 `SiteSlot`、不写 `Material.site` 的参数照旧是库位选择器，只是没有这层关系的核对。

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
| `@action(resource_contract={...})` | `@action(material=Material.xxx(...))`，对照表见上 |
| `Annotated[T, Placeholder(PLACEHOLDER_...)]`（`unilabos.registry.annotations.Placeholder`） | `@action(placeholder_keys={"参数名": PLACEHOLDER_...})`，参数照常注解为 `T` |
| `Annotated[str, SiteSelector(owner=..., occupant=..., show_occupied=..., allow_occupied=...)]`（`unilabos.registry.annotations.SiteSelector`） | 参数注解为 `SiteSlot`，关系写成 `@action(material=Material.site("参数名", owner=..., occupant=..., show_occupied=..., allow_occupied=...))`；生成的 schema 扩展与以前相同 |
| JSON 动作的 `@action(goal={...})` / `result={...}` 重命名 | 去掉：字段名就是参数名 / 返回 TypedDict 的字段名（要改名就改参数名） |
| 按名字注入的动作上下文：参数叫 `action_context` 但没有 `ActionContext` 注解 | 注解为 `Optional[ActionContext]`（`from unilabos.client.host.runtime.action import ActionContext`），名字随意；没有注解的 `action_context` 参数注册时报错（自动动作同样） |

行为上的变化：

- 以前的普通动作不校验参数，必填参数缺了会被 `goal_default` 补成 0 / 空串；现在缺了直接报错。
- 以前的普通动作不校验返回值；现在写了返回注解就按它校验，不符进入失败决策（例如注解 `-> bool` 却返回字典）。
- 以前没写 `handles=` 的普通动作在注册表里没有端口；现在按 schema 生成（每个参数一个输入端口，具名输出各一个输出端口，
  物料参数另有同名的透传输出）。写了 `handles=` 的动作照旧用写的端口。
- 统一 schema 不再给普通动作的 goal 注入 `unilabos_device_id`（以前上报云端时注入，云端适配层会剥离它）。
- `@action(materials_need_lock=...)` / `materials_free=...` 仍按此前的决定忽略并告警（物料参数一律加锁）。
