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
| `ResourceSlot` / `list[ResourceSlot]` | 物料引用 `{"uuid": ...}`，加物料锁 | 严格；Host 解析成物料实例 |
| `Annotated[str, SiteSelector(owner=...)]` | 库位选择器 | 严格；预检核对库位 |
| `DeviceSlot` / `SiteSlot` | 设备 ID / 库位 UUID 字符串 | 严格 |
| `dict`、`Dict[str, Any]`、`Mapping` | 任意 JSON 对象 | 只校验是对象 |
| 没写注解、`Any`、`object` | 任意 JSON 值（`{}`） | 不限值 |
| 严格解析器不认识的注解（`Union[int, str]`、`List[Optional[float]]`、枚举、PLR / pydantic 类型……） | 退回注册表原来的签名类型推断 | 按推断结果；推断不出的部分接受任意值 |
| `**kwargs` | goal 允许多传字段 | 多传的字段不校验 |
| `*args` | 无法表示，忽略并按定义告警一次 | — |

- **没有默认值的参数就是必填**：缺了在调度端 / Host 调用前报「动作参数缺少必填字段：…」，不再被补成 0 / 空串。
  `goal_default`（`@action(goal_default=...)`）只是编辑器里的初始参数值，叠加在签名默认值上，不改变参数是否必填。
- 参数名之外的字段一律拒绝（`**kwargs` 除外）；框架注入的 `sample_uuids`、`action_context` 不算参数。
- 物料参数（`ResourceSlot`）接受框架的几种引用形态：`{"uuid": ...}`（可带 id / name）、只给名字的
  `{"name": ...}`（派发前由调度器向物料权威解析出 uuid）、上游端口传来的扁平资源树（列表）、uuid 字符串；
  能否解析出权威 uuid 由派发前的物料锁检查保证。不可空的物料参数给空值（`{}` / `""` / `[]`）报
  「物料参数 … 没有给物料」；可空的物料参数留空等于没给。
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

同一个合同字段只能出现一次（例如 `load` 与 `unload` 不能写在同一个动作上，设备间转移用 `from_device=`）；
`uses` 可以和其他语义同时写。内部仍规范化成第 2 版资源合同，调度器按它执行托管、搬运与分装结算。

## 前端选择器：`Placeholder`

物料、设备、库位由注解类型表达（`ResourceSlot`、`DeviceSlot`、`SiteSlot` / `SiteSelector`），注册表自动生成条目的
`placeholder_keys`。其他前端选择器写在注解里，只影响前端，不改变类型与校验：

```python
from typing import Annotated
from unilabos.registry.annotations import Placeholder
from unilabos.registry.placeholder_type import PLACEHOLDER_MANUAL_CONFIRM, PLACEHOLDER_NODES

def transfer(self, target: Annotated[ResourceSlot, Placeholder(PLACEHOLDER_NODES)] = None): ...
def confirm(self, assignee_user_ids: Annotated[list[str], Placeholder(PLACEHOLDER_MANUAL_CONFIRM)]): ...
```

## 不兼容变更

以下写法已删除，装饰器（导入时）和注册表 AST 扫描（`--check_mode`、启动扫描）都直接报错，错误信息给出新写法；
不做转换，也不只是告警。

| 删除的写法 | 新写法 |
| --- | --- |
| `@action(contract="typed")` / `contract="legacy"` | 去掉：所有 JSON 动作同一标准 |
| `@action(resource_contract={...})` | `@action(material=Material.xxx(...))`，对照表见上 |
| `@action(placeholder_keys={...})` | 物料 `ResourceSlot`、设备 `DeviceSlot`、库位 `SiteSlot` / `SiteSelector` 注解；其他选择器 `Annotated[..., Placeholder(...)]` |
| JSON 动作的 `@action(goal={...})` / `result={...}` 重命名 | 去掉：字段名就是参数名 / 返回 TypedDict 的字段名（要改名就改参数名） |

行为上的变化：

- 以前的普通动作不校验参数，必填参数缺了会被 `goal_default` 补成 0 / 空串；现在缺了直接报错。
- 以前的普通动作不校验返回值；现在写了返回注解就按它校验，不符进入失败决策（例如注解 `-> bool` 却返回字典）。
- 以前没写 `handles=` 的普通动作在注册表里没有端口；现在按 schema 生成（每个参数一个输入端口，具名输出各一个输出端口，
  物料参数另有同名的透传输出）。写了 `handles=` 的动作照旧用写的端口。
- 统一 schema 不再给普通动作的 goal 注入 `unilabos_device_id`（以前上报云端时注入，云端适配层会剥离它）。
- `@action(materials_need_lock=...)` / `materials_free=...` 仍按此前的决定忽略并告警（物料参数一律加锁）。
