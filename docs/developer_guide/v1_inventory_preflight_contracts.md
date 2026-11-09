# V1 库存、运行预检与动作合同接入 dev

基线：dev `4153b032`；参考：`feat/schedule`（V1）的 `690491a3`。

2026-11-09 后续审查：按用户最新决定移除主仓库 React 调试站，正式前端改为
[Uni-Lab FE](https://git.dp.tech/lab-os/uni-lab-fe)。本文保留 #358 的实现记录；
当前消解清单、API 不兼容项和 CI 阻塞见 [V1 / Dev 消解清单](v1_dev_reconciliation.md)。

## 启动确认解决什么

启动确认是 Host 在调用设备前向调度权威确认“这个 Job 的这个 attempt 现在仍允许开始”，并持久保存开始许可。它不是网页上的人工确认按钮，也不是动作成功证明。

以吸液为例：消息重发或 Host 崩溃后，仅有“命令已发送”无法判断是否已经吸过。持久开始许可与 attempt 身份、结果记录结合，才能区分可恢复派发的未开始动作与需要核实的未知执行。即使已经取得许可，设备仍可能尚未动作，所以不能据此推断实际吸液量。

本次没有实现 dev 的 begin/fencing 握手，也没有把当前内存资源锁变成持久租约。

## 这次接入的能力

| 能力 | dev 原有行为 | 本次增量 |
| --- | --- | --- |
| 按量库存 | 批次 FIFO/有效期筛选、整任务原子预留、幂等流水、失败隔离 | 同维度单位换算、共享虚拟余量的整批只读预检、完整缺口明细、随数量级缩放的浮点容差 |
| 容量 | 容器 config、内容物与化学品关联 | 额定与实例上限校验；同一容器所有内容物合计；建树、内容更新、快照、增量、配置及模板变更在同一事务最终状态校验 |
| 盘点 | 库存版本与幂等命令 | 人工计数盘点；必需操作人、理由与期望版本；预留、隔离、已登记实物的数量差异拒绝自动覆盖 |
| 运行预检 | 直接建任务再调度 | 复用建任务的执行计划与入口参数；检查在线动作能力、严格合同、物料和库位、当前库存；不建任务、不预留、不记命令流水 |
| 动作合同 | 宽松签名推断、默认物料锁 | 显式 `@action(contract="typed")`；AST 严格参数与具名返回合同；冻结到任务；派发及 Host 调用前再校验，错误结果进入失败决策 |
| 前端接入 | 主仓库调试站曾接入预检 | 调试站现按决定移除；保留后端预检与修订保护，正式前端仍需协议适配 |

继续使用 dev 的 Authority/Host 拓扑、materials.db、并行任务调度、HostLink/ROS2 适配器和既有物料接口。本次没有新增数据库列或表，因此不需要 DDL 迁移。新能力使用既有 config/definition 字段及命令流水；旧动作默认保持 legacy。

## API

### 候选运行预检

`POST /api/v1/workflows/{workflow_uuid}/run-preflight`

```json
{"run_mode":"normal","input":{},"expected_revision":3}
```

沿用工作流响应信封 `{"code":0,"data":...}`。`expected_revision` 可选，传入时拒绝检查另一修订。结果包含 `workflow_revision`、`checked_at`、`checks`、`summary`，以及：

- `invalid`：入口参数、执行计划或已知动作参数不符合合同。
- `temporarily_unavailable`：设备能力、资源状态或库存当前不满足条件。
- `runnable_now`：当前可检查的条件通过；`deferred` 项仍需运行时判断。

客户端检查并提交时，应把报告的 `workflow_revision` 作为 `POST /api/v1/workflow-tasks` 的 `expected_revision`。服务端在创建任务的同一事务内比较修订，变化时返回冲突且不创建任务。既有客户端可以省略该字段；同幂等键重试仍返回最初冻结的任务。原调试站已实现该传递；正式前端当前还没有传递，不能将两者混同。

上游输出、库存分配参数、循环变量和后续循环迭代不会被猜成静态常量。派发仍校验实际值。预检不承诺跨 runtime/materials/Host 的原子快照，不提供执行许可，也不排他占用设备。dev 保留多任务并行和任务级单步，不照搬 V1 当前工站准入限制；这不表示 V1 没有并行调度能力。

### 库存预检

`POST /api/v1/materials/inventory/preview`

```json
{"requirements":[{"key":"dose","kind":"lot","template_uuid":"reagent-template","quantity":1,"unit":"mL"}]}
```

返回 `available`、候选 `allocations` 和全部 `shortages`。一批需求共用虚拟余量，不会分别判断后超卖。真正预留复用同一分配算法，分配与扣减仍以每个批次的原单位保存。传给动作的同一需求合计及 lot 明细统一为首个批次的单位；typed 物料参数只投影为合同要求的 `{uuid}`。

同维度支持 mg/g/kg、µL/μL/uL/mL/L，以及 count/piece/粒。同名的其他单位仍兼容原行为。散装 lot 没有可信密度字段，不进行质量与体积的跨维度猜测。

### 人工计数盘点

`POST /api/v1/materials/lots/{lot_uuid}/stocktake`，请求使用既有 `InventoryMutation` 信封：

```json
{
  "command_uuid":"stocktake-command-1",
  "effect_key":"stocktake",
  "operation":"stocktake_inventory_lot",
  "actor_type":"operator",
  "actor_uuid":"operator-1",
  "payload":{"lot_uuid":"tips-lot","expected_version":4,"measured_count":96,"reason":"人工逐件核对"}
}
```

仅用于离散整数库存；重复相同命令幂等回放，版本变化或涉及预留/隔离/已登记实物的差异返回冲突。它记录人工核对，不冒充已验证的称重/计数设备 Job 证据。Local/HTTPMaterialsClient 同时提供 `preview_inventory` 与 `stocktake_inventory_lot`。

这些 POST 路由沿用现有写请求 token 中间件；预检本身只读。

## 容量

容器模板 `definition.capacity` 声明额定上限，实例 `config.capacity` 可收紧上限。支持 `max_volume_ul`、`max_mass_g`、`max_count`。旧 `config.max_volume` 仍作为 µL 规格参与约束。

```json
{"capacity":{"max_volume_ul":1500}}
```

未配置上限的旧记录保持兼容，不强迫为全部旧设备补容量。配置了约束时不能用未知单位跳过检查；内容物按维度求和。液体质量与体积换算必须有内容物所关联化学品的有效正密度，且没有浓度/混合物声明。不会用固体密度猜测装填体积。上限和数量冲突时整笔写入回滚。

## 严格动作合同

```python
from typing import Annotated, TypedDict
from pydantic import Field
from unilabos.registry.decorators import action
from unilabos.registry.placeholder_type import ResourceSlot
from unilabos.registry.annotations import MaterialLock

class DoseResult(TypedDict):
    actual_volume: float

@action(contract="typed")
def dose(
    self,
    source: ResourceSlot,
    reference: Annotated[ResourceSlot, MaterialLock(free=True)],
    volume: Annotated[float, Field(ge=0)] = 1.0,
) -> DoseResult:
    ...
```

严格声明由 AST 静态解析，不执行作者模块。支持的有限注解语言、默认值、资源引用、SiteSelector 和具名返回来自 V1 的规范解析器。必填参数不再被类型默认值补成 0/空串；物料默认在动作使用期间互斥，免锁用 `MaterialLock(free=True)` 声明。用户所说的“共享”是空闲时可以借出，并不意味着免锁或同时使用；跨动作的共享/独占区间尚未完整接入。兼容装饰器断言不能覆盖规范字段；格式错误的显式 typed 动作不能悄悄降级为普通动作。

规范 Schema 的 `x-unilabos-action-contract.version` 为 2。输入/输出 handles、默认值、选择器和物料锁由同一份 Schema 派生。画布节点即使没有动作模板，也会从当前注册表取合同并冻结到任务。实际参数在调度端与 Host 调用前验证；返回值不符合合同会进入失败决策，不上报成功。人工替换结果也遵守返回合同，无效替换保留待决策记录。

## 尚未达到 V1 完整运行期对等

这次提供可独立接入 dev 的库存增量、预检和严格动作合同，不宣称整个 V1 库存运行期已经移植：

- V1 的内容物共享数量预留、运行时补料分支与数量预留 Saga，尚未取代 dev 的 lot 预留模型。
- V1 的搬运/分装实际结果结算、与通用扣减去重、可信测量 Job 自动盘点，尚未接入。
- `device_tenancy`、`transfer`、`transfer_step`、`operate_in_place`、`aliquot`、`order_sensitive` 的 Schema 可以静态解析，但当前 dev 的运行值校验明确拒绝这些未接通的执行语义。不会仅因声明了合同，就假装完成持久占用或物理结算。
- 正式前端在独立 Uni-Lab FE 仓库；本轮只读核验其代码和 API，不修改该仓库业务实现。主仓库调试站已移除。

上述运行期能力应与开始许可、attempt fencing、持久资源租约及恢复/人工裁决规则一起接入，防止未知执行被当成“未消费”而重新派发或补回库存。

## 验证记录

以下为 #358 及原调试站尚在时的历史验证，不代表移除后的前端已通过正式联调。

- V1 纯解析器与 dev 接线、库存事务、预检 API 首轮：264 项通过。
- dev `tests/server tests/registry` 扩大回归：1081 项通过、13 项跳过、4 项排除。排除的 4 项在原始 dev `4153b032` 上同样失败（已安装 demo 包污染 3 项、缺少 ROS2 `rosidl_parser` 1 项）。缓存格式升级的断言已同步。
- 最后补齐容量浓度判断、失败结果标记和人工结果替换合同后的定向回归：98 项通过、2 项跳过，覆盖库存、调度器、Host 执行与 Authority 控制服务。
- 7 个 README demo 在 HostLink、Authority/Host 分进程拓扑通过。
- 前端构建、OpenAPI 同步及 66 项测试通过，包括“预检阻塞不得提交任务”。

2026-11-09 合并前复核：CI 的五个失败任务均由两个 README 示例断言既有库存不足文案触发，现已保留预留接口文案。新增建任务修订保护与库存权威不可达时的结构化预检报告。Python 3.12.16 下重新跑通 7 个 HostLink 分进程示例；前端 67 项测试、构建及 OpenAPI 一致性检查通过。

随后 CI 运行 `42584235496` 暴露另一项回归：ROS jazzy/humble 的两个资源快照测试，
因旧容器 `max_volume` 与质量单位内容物无法换算而被容量校验拒绝。#358 仍为 OPEN，
不能把此前错误文案修复视为全部 CI 已通过。后续候选的验证记录见消解清单。
