# dev 工作流集成记录

集成分支：`feat/dev-workflow-integration-20261112`。以 dev 的传输、进程管理、HTTP API 和资源权威为基础，未切回 main 架构。

## 显式资源作用域

```python
@workflow(compiler="ast")
def experiment(ctx):
    with ctx.resource_scope("reactor", name="reaction"):
        ctx.run("reactor/load", {})
        with ctx.loop_for(3):
            ctx.run("reactor/stir", {"duration": 10})
        ctx.run("reactor/unload", {})
```

参数使用设备 ID（模板角色），前端实例化时沿用同一份设备绑定。作用域只声明所有权边界；每次动作仍须独立取得实际使用权。外层所有权覆盖循环全部轮次、嵌套循环和动作间隙，不随每轮完成而释放。声明在循环内部的作用域仍按轮次创建。

HTTP `resource-scopes` 接口也接受跨循环节点引用；跨入循环时，边界提升到包含它的整个循环。作者应把 `resource_scope` 写在希望覆盖的循环外侧。暂停或重启不会释放区间；异常会保留，需先核实关联动作、样品和设备，再手动确认。设备托管独立于区间：区间结束不表示样品已卸载。

模板实例化及全图保存可通过 `resource_scopes` 与节点、I/O 一起原子保存，避免图已更新但作用域仍引用旧节点。嵌套作用域必须完全位于父作用域内。

## 托管与库存恢复

库存恢复读取 runtime / materials 两个 SQLite 库的持久事实。任务取消后，未用库存只在没有活动设备托管时释放。人工确认取走样品会唤醒对账；重启后同样有效，重复对账不会重复结算。

最新 PLR 的旧 `max_volume` 中，0 表示默认未指定容量，`"Infinity"` 表示无额定上限。这些旧值不投影成库存容量许可。显式 `capacity` 仍要求正有限数值，已知额定上限不能放宽。

## 验证记录

- 新 PLR 源码基线：`5c17d1956fe597b354d9231e2f1f1e838a6ee77b`，独立 Python 3.12 环境。
- 资源全表创建、序列化往返及容量兼容测试通过；PLR 发布包版本标记与发布校验另行处理。
- 区间、托管及双库恢复：63 项通过。
- 作者声明、模板实例化、循环执行及跨循环暂停/重启/失败：67 项通过。
- 以上测试有重叠，不合计成总覆盖数；仅虚拟设备，未重启现有 Host/slave。

## 包操作恢复与重启协调

`driver_package_operations.json` 持久保存操作日志、版本、切换意图、提交前后台账快照与核实说明。源码切换前记录备份位置，崩溃恢复按已提交台账判断保留新源码还是恢复旧源码；不重放依赖安装器。依赖安装失败可能已改变共享环境，同样要求人工核实。

包变更、托管 slave 启动与重启使用共享变更锁。Host 重启先关闭新变更入口，等当前操作及最终日志落盘，再持有跨进程锁直到退出；`immediate` 也不截断包操作。请求 slave 重启被拒绝时保留原进程运行。远端 Host 必须以新进程身份重连，不能把旧连接一直在线误判为重启完成。

待核实的包恢复阻止设备 runtime 与托管 slave 启动；Host 仅提供管理服务。`POST /driver-packages/operations/{id}/confirm` 接收 `expected_version`、`reason`，只登记核实，不重启。源码/台账仍冲突时先修复，再调用同路径下的 `/recover` 重新检查，最后人工确认并手动重启 Host。

验证：包、源码监测、进程、启动、重启及 SDK 联合测试 78 项通过；恢复确认及重启专项 23 项通过（与前者重叠）。故障注入使用真实临时子进程强制退出，网络下载、依赖安装与硬件调用使用替身，没有变更现有设备环境。


## 条件库存、动态来源和补料

静态无条件需求沿用整任务原子预留。`ctx.run(..., inventory_mode="on_ready", inventory=[...])` 在节点就绪后预留，缺料时保留 pending 并显示在运行处置的“等待补料”。条件分支和含入口/上游结果引用的需求自动使用此语义；未选分支不预留。补料入库后定期重查，暂停任务不自行恢复。

库存选择器和 quantity/unit 可以引用入口参数或上游结果；key/kind 必须静态。引用遵守相同分支及循环作用域规则。实际来源在材料事务前写入 attempt.control_data.inventory_request；材料已提交但应答丢失时复用原预留，不重新选料。循环和显式重试使用新 attempt，重新求值。已冻结来源使用的入口参数不能再修改。确定的库存不足与不确定网络异常分开处理，只有前者允许下一次预留申请。

验证：`test_workflow_deferred_inventory.py` 覆盖选中分支、动态来源补料、跨库提交恢复、两轮预留及等待补料时重开数据库；与库存权威、恢复、条件、并行和预检共 61 项通过。


## 物理动作、实际用量与库位结算

`transfer`、`transfer_step`（pick/place）、`aliquot` 已接入调度权威。派发前在材料库冻结来源、目标、相关容器、子树和夹爪库位，并绑定 attempt/claim/参数哈希。分步抓取自动取得样品托管，放置结算成功后解除；显式资源作用域独立控制循环期间的设备所有权。

驱动只上报实测事实，由权威提交材料账本。例如：

```python
class AliquotResult(TypedDict):
    material_aliquot_receipts: list[dict[str, JSONValue]]
    inventory_consumptions: list[dict[str, JSONValue]]

# 分装：完整列出本次所有目标，未完成的目标明确填 0。
return {
    "material_aliquot_receipts": [
        {"target_material_uuid": target.unilabos_uuid, "actual_quantity": 8.0, "quantity_unit": "ul"}
    ],
    "inventory_consumptions": [
        {"key": "solvent", "lot_uuid": lot_uuid, "actual_quantity": 8.0, "quantity_unit": "ul"}
    ],
}
```

搬运返回 `material_transfer_receipts`，每项必须包含 `material_uuid`、`target_owner_uuid`、`target_site_uuid`；来源由冻结计划补全并校验。返回目标必须与计划一致。分装按来源混合物组分比例分配，校验单位、可用量和每个容器容量；任一目标失败，内容物、库位和库存差额全部回滚，保留占用。

库存开始扣预留量，结算按各批次实际量退差额或补扣。显式实际量必须覆盖全部批次，允许 0，禁止负数、NaN/Infinity 与重复项。旧动作没有 `inventory_consumptions` 时保留原有整份扣量语义，不把计划量标成实测。没有可靠密度时不猜测质量/体积换算。

## 故障与人工处置

设备结果先写 runtime 意图，再提交 materials 事务，最后收敛任务。崩溃或应答丢失只重放结算，不再次执行设备。材料库 schema v5 保存执行记录和独占表；从 v4 升级保留物料、内容物和账本，并保留迁移备份。

物理回执缺失/冲突时显示“补记实际结果”或“终止任务”，不提供直接重试。任务终止后仍保留相关物料、库位与设备阻塞，并标记 `cleanup_status=requires_attention`。前端须逐项填写实测量、填写现场说明并勾选确认，才允许以下操作：

- 补记实际结果：原任务失败/取消状态保留，只结算实际物料结果。
- 已恢复原状，释放占用：确认样品库位和内容物已恢复动作前事实；已隔离库存不会自动返还。

人工结算也先持久保存已验证意图，恢复时只重放相同回执。版本冲突不会自动重试。若实物落在计划外库位，必须先人工纠正现场状态，再按冻结目标结算或确认已恢复原状；此版本不自动规划补救搬运。

运行处置入口 `/operations`：库存缺料、跨库对账、物理结算、资源借还、设备托管、包操作恢复和手动重启分别显示，单一 Host 路由离线不会清空其他权威记录。

## 联合演示与兼容修正

`LabDeviceIntegrationDemo/complex_workflow_demo/workflows.py` 的 `联合演示：托管分装与补料`：外层 `reactor` 显式作用域 → 抓取托管 → 两轮动态选择来源和条件分支 → 缺料等待 → 补料后每轮实测分装 8 ul → 放回目标库位。入口只定义默认角色、参数和过程，设备换绑仍由前端负责。

最新 PLR 装配不接受权威 `config.capacity` 构造参数；适配层现用 sidecar 保留该约束并保证往返不丢失。材料 HTTP 查询支持 `reactor/gripper` 这类层级 ID，并正确编码路径。

保留边界：`order_sensitive` 特殊调度仍未开启；ROS 2 与真实硬件的搬运/分装驱动回执需要各设备联验。这里的实际量来自虚拟驱动，不能替代硬件标定。PLR 发布 wheel 的版本元数据仍独立于本次运行验证。

## 最终联合验证

- 物理结算、崩溃后仅重放账本、终态人工补记、设备阻塞、动态库存、schema 升级及资源往返回归：49 项通过。
- Demo HostLink 两进程联调与模板合同：4 项通过。内部运行五条循环流程、资源借还/托管场景及新的条件补料分装流程。
- 前端 core：556 项通过；developer-web：858 项通过；类型检查及发布构建通过。
- 浏览器接入真实独立后端，确认缺料、跨循环设备所有权、夹爪托管同时可见；核实说明和勾选前禁止提交，确认后对账请求返回 HTTP 200，无页面脚本错误。
- 实际结算结果：两轮每轮计划 10 ul、实测 8 ul，来源 100 → 84 ul，目标 0 → 16 ul，库存 7 + 补料 20 − 实测 16 = 11 ul；未选分支没有预留，最终无活动物理占用或托管。

本地预览：`http://localhost:5193/operations`，后端端口 18034，数据根 `/home/wz/unilab-joint-preview-20261112`。原 Windows dev 工作区的未提交修改未被覆盖。全部为本地集成分支提交，未推送或合并远端。
