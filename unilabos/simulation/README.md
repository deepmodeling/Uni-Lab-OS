# 可选数字孪生接口

普通 OS 导入不加载 Blender/Isaac SDK。设备观测由 OS 后端的
`DeviceObservationService` 与 `ObservationSessions` 管理，使用现有 DeviceStateStore，
支持 SQLite/PostgreSQL。通过 `UNILABOS_OBSERVATIONS_ENABLED=true` 启用；
旧 TwinRuntime、兼容导入和旧 HTTP 路径已经删除。

设备和资源装饰器的 `metadata.twin` 声明版本、具名属性、语义、单位/frame、类型、TTL 和关节映射。model 继续使用既有唯一入口。矩阵采用行排列的16元素、列向量齐次变换，平移量在索引3/7/11，单位与frame由包声明。未知值为 null，不归零。

现有 scheduler API 下新增 `/device-state/observations/snapshot`、`/device-state/observations/resolve`、`/device-state/observations/changes`、`/device-state/observations/events`、`/device-state/observations/operations/{id}`、`/device-state/observations/actions/{job_id}` 只读接口。SSE 首先发送一致基线；游标溢出或会话改变返回 stream.reset，读者重新获取基线。连续状态不替换已有业务 `/events`。

可信包通过 Python API 配置组、prepare/switch、ready 和 publish_batch。每个原生实例只能属于一个组；旧代际/源/epoch/会话在写入时拒绝。切换查询使用 sources.operation(id)。组有未决动作、原生活动 Claim 或包未完成握手复位/效果提交时拒绝切换。重启关闭派发，不自动执行旧动作；reconcile_legacy_action 必须提交包的对账回执并再次证明 quiescent，然后 recover，再进行新的 switch/ready。

启用功能后，仅调度派发使用 SourceDispatcher；原后端对象身份保留，供 Edge 路由和生命周期管理使用。OS 组合根通过 register_simulation_route(provider, dispatch) 登记可信执行路由；未登记的受控设备拒绝执行，replay 模式不执行动作。未受控的既有设备保持原派发路径。包路由接收原生 payload 和准入身份，仍须使用既有 Edge Task/Job 回报与库存权威接口。通用 API 不内置工站名称或设备动作；领域包提供描述、动作白名单及物理证据。

显示命令：

```sh
python -m unilabos.simulation display --url http://127.0.0.1:PORT/api/v1/device-state/observations \
  --bindings /path/to/native-template-bindings.json --assets-root /path/to/assets \
  --blender /path/to/blender --output /path/to/new-run
```

bindings 是原生资源模板 UUID 到 DisplayAsset 的 JSON。包负责显式根对象、逻辑属性与轴/矩阵绑定。已有 blend 及外部文件均需匹配 SHA256；缺依赖失败，不修补材质。各实例共享只读几何，各自保留变换、质量和生命周期。观察端只应用 observed，unknown/stale 保留诊断，不替换为目标或零。显示不写入物理反馈。渲染为独立操作，离线录像不能充当实时性能证明。

`python -m unilabos.simulation.isaac_worker` 是需在 Isaac 环境运行的可选 CPU worker：加载显式 stage/prim，严格验证世界/源会话/tick，重复扫描返回同一 ACK，未知物理结果阻断后续推进。默认50 ms/12个240 Hz子步。普通 OS 安装不包含 Isaac SDK。服务器机制验证使用 Testbed scripts/os_twin/run_isaac_probe.py；它不证明任何设备工程模型或动作完成条件。

本次源码恢复、依赖安装与验证范围见 [职责收敛与迁移](../../docs/development/domain-runtime-finalization.md)。历史 Testbed 报告不能替代当前部署的引擎、资产或硬件验收。
# Edge 上报与确认步组合

启用设备观测后，既有 Edge API 增加
`POST /api/v1/device-state/observations/report`，请求为 `device_id` 和完整 `samples`
列表。每条必须携带原生 UUID、代际、描述版本、provider、epoch 与源会话。
该入口沿用 Edge 的部署访问控制；不会从旧标量变化缓存猜测观测时间或质量。
一批样本原子接受，过期源被拒绝；读取仍走 `/api/v1/device-state/observations/snapshot`。

`unilabos.simulation.coupling.CoupledWorld` 复用可选依赖
`unilab-plc-sim==0.2.11` 的正式 `CoupledSession`。OS 按 50 ms 确认扫描调用；
Isaac 每次推进 12 个 1/240 秒子步。`IsaacSocketPort` 校验世界、会话、epoch、
tick 和子步数；异常后不自动重放。默认通用 socket worker 不接受设备专有控制；通过显式 `--model` 工厂加载的包模型提供控制 hook。模型的物理资格须由其资产与实际引擎单独验证。
PLC-Sim 0.2.11 需从配套锁定源码预先安装；当前公共索引没有该发行包，不能仅依赖 `pip install unilabos[twin-simulation]`。它要求 Python 3.11，见第三批文档的安装顺序。


## 显式模拟 provider 接入

设置 `UNILABOS_SIMULATION_PROVIDER_CONFIG` 为受信部署配置文件后，组合根装配
`ProviderControl`。配置明确 group、provider、原生实例 ID 的描述版本及每个设备
允许的模拟 Action 名称。组合根必须显式注入独立 `simulation_dispatch(payload, admission)`，缺失时拒绝启动，绝不默认转发到真实 Edge。模拟路由负责沿用作业回报和库存权威；白名单拒绝发生在动作
准入前，不把被拒绝的动作留下为 pending。默认关闭此入口。

组合到既有 Edge API 的 `/device-state/observations/provider/` 提供 connect、ready、
samples、settle、recover 和 disconnect。connect 仅提交 simulation 代际，worker 完整准备后
再 ACK ready；samples 原子提交跨实例同一扫描的样本。观察者仍使用只读
`/device-state/observations/snapshot` 或 `/device-state/observations/events`。settle 逐项核对当前租约、原生 Job 和库存台账
回执；无规范回执或旧会话不能解除 pending。重启不会自动恢复或重做动作，
recover 仅接受当前重启会话、原组 epoch/source 及每个 pending Job 的规范台账回执；原生 Claim 仍会阻止恢复。幂等恢复回执可查询；恢复后仍保持未就绪，必须另行 connect/ready，不自动重做动作。

`ObservationPublisher` 由模拟 worker 使用；`DeviceWorldExecutor.observe` 仅接收
已确认扫描及握手复位扫描，Blender 不在执行循环中。HTTP 失败不自动重试物理动作。
本地测试覆盖 HTTP、描述版本/白名单/租约拒绝和库存台账对账。恢复资格需独立重启/故障运行记录，不能继承正常执行或离线回放结果。

显示资产可用 `probes` 指定只读世界矩阵探针，例如设备包的 TCP；探针不写入几何。`frame_camera=false` 使大地面不参与自动取景，几何仍渲染。render 的可选 samples 参数只影响 EEVEE 采样质量；性能、录像帧率和物理扫描频率分别报告。

## 可选 Blender 物理执行端

`bullet_worker.py` 通过 Blender 的 `--python` 启动，与观察用的
`blender_worker.py` 分工独立。确认步与控制幂等已提取到 `physics.py`；
`isaac.py` 保留旧导入兼容。设备包提供 command、prepare_scan、before_substep、
advance_substep、after_substep、finish_scan 钩子，OS 唯一推进世界。

```sh
blender --background --factory-startup --python-use-system-env --python-exit-code 1 \
  --python /installed/unilabos/simulation/bullet_worker.py -- \
  --model trusted_package.model:World --model-config /deployment/model.json \
  --socket /private/run/plant.sock --output /private/run/worker \
  --world-id WORLD --session-id SESSION \
  --headers /dependencies/pinned-blender-source --bridge-cache /private/bridge-cache
```

模型入口和路径来自可信部署配置。独立环境需能导入 OS、设备包和模型依赖；
普通 OS 导入不加载 bpy。`bullet_native.py` / `bullet_contacts.cpp` 使用当前
Blender 内置求解器；不导出 `RB_*` 符号的构建（如 Windows 版 Blender）依次回退
`bullet_standalone/`（独立编译的 Bullet 动态库 + 同一 `RB_*` C ABI）和
`bullet_bpy.py`（bpy 公共刚体 API，按 fps=240、substeps=1 推进）。三条路径共用
同一 `World` / `B` / `E` / `pose` / `velocity` / `quat` 接口，`BACKEND` 为
`native` / `standalone` / `bpy`；`NATIVE_ERROR`、`STANDALONE_ERROR` 保留各自原始
失败，`MANIFEST` 标注 `backend`、`blender_version` 与独立库的
`library_path`/`abi`，失败不静默冒充其它后端，也不按平台或版本拒绝。
`UNILABOS_BULLET_BACKEND` 可强制 `native`、`standalone` 或 `bpy`（不可用即报错），
默认 `auto` 按 native → standalone → bpy 选择。`bullet_usd.py` 将既有
USD 刚体/碰撞产物加载到该求解器，不产生转换资产。输出 capabilities、ready 和
每个唯一确认扫描的 steps.jsonl。原生桥限定 macOS 和 Blender 构建 d13f752e3b9c，
头文件提交/脏状态与桥接摘要均检查；这是受限候选后端，不能宣称跨 Blender 版本
ABI 稳定。独立库与 bpy 回退的接触/约束语义各有差异（见
`bullet_standalone/README.md` 与 `bullet_bpy.LIMITATIONS`），不能当作原生
manifold 冲量或站级物理资格。

## 出片（render）

`unilabos.simulation.render` 是引擎无关的出片公共层：任务回放记录（MCAP / 回放 JSONL）→ 冻结时间线
（`unilab.frozen-timeline/1`，固定帧率，`log`/`focus` 时间策略，带摘要）→ 镜头（活跃设备跟随）、
分片与出片合同（`unilab.render-contract/1`）→ 引擎适配器出图、编码 → 读回帧数/帧率验收。
场景说明（`unilab.render-scene/1`）从统一图与运行中的 OS 读一次后定格，适配器不再访问 OS。
引擎适配器在 `render/adapters/`；Blender 适配器按 USD 关节（与前端 usdJoints 同一公式）摆位，
分片并行出 PNG，用 Blender 自带编码出 MP4（兼容 Blender 5 的 strips / media_type）。

```sh
python -m unilabos.simulation.render freeze --sources task.mcap --out timeline.json --fps 24 --policy focus
python -m unilabos.simulation.render scene --backend URL/api/v1 --graph unified.json --package pylabrobot-unilab=/pkg --out scene.json
python -m unilabos.simulation.render blender --timeline timeline.json --scene scene.json --blender /path/blender --out DIR --shards 4
```

## 碰撞查询（collision）

`unilabos.simulation.collision` 是与机构种类无关的整站碰撞查询。机械臂、导轨、三轴龙门、移液头、门盖、离心机上料机构和夹着的物料，都按“刚体 + 位姿”接入同一个内核。它不规划、不读库存、不推进时钟、不启动仿真世界。机器人领域库（`unilab-robot-runtime`）的 `fcl_collision`、`refined_collision`、`collision_instance`、`cad_scene` 改为调用这里的实现，只保留逆解和路径搜索。

| 模块 | 作用 |
|---|---|
| `profile.CollisionProfile` | 读取碰撞 profile（`planning-geometry.json` + 同目录 `collision.npz`），核对摘要；`lazy=True` 时按需解码 |
| `instance.CollisionInstance` | 把同一份 profile 放到某个实例的 `world_from_asset`：几何共享，身份和位姿独立 |
| `scene.station_scene` | 从部署图（毫米、父链、行向量朝向，与 `scene_assembly` 同一口径）和各节点的 outer/exact profile 装配整站场景 |
| `kernel.SceneChecker` | 第 1 级包围盒剪枝（刚体、图元，向量化）+ FCL 窄相位；`clearance()` 求最小间隙 |
| `refined.RefinedChecker` | 三级检查：包围盒 → 保守外包 → 只对外包命中的对按需加载精确几何确认；`clearance()` 用精确几何求间隙 |
| `mechanism.KinematicChain` / `Mechanism` / `Attachment` | URDF（含 mimic）或关节列表构成运动链：关节值 → 连杆与碰撞刚体的世界位姿；携带物随连杆走 |
| `query.CollisionQuery` | 对一段轨迹逐点检查，返回碰撞采样点、首批碰撞对、最小间隙、各级计数和耗时；`receipt()` 写成 `collision_checked` 的轨迹回执 |
| `query.states_from_motion_feed` / `densify` | 从纯仿真或孪生的运动流读出逐点状态；在相邻给定点之间按步长线性细分 |

```python
from unilabos.simulation.collision import CollisionQuery, KinematicChain, Mechanism, receipt, states_from_motion_feed
from unilabos.simulation.collision.scene import station_scene

scene = station_scene(document, {"gantry": (outer_json, exact_json), "rack": (rack_outer, rack_exact)})
gantry = Mechanism.from_initial_poses("gantry", KinematicChain.from_urdf(urdf_text), scene.world_from_asset["gantry"],
                                      body_links={...}, initial_world_poses={...})
query = CollisionQuery(scene.checker(), [gantry])
states = [s for _, s in states_from_motion_feed("motion.jsonl", ["gantry"])]
report = query.check_states(states, allowed_pairs=mount_pairs, clearance_margin_m=0.02, source="simulated")
```

规则：

- **接触许可显式给出**。每个阶段只授权有机械依据的具体对象对，例如安装面、夹指与被夹物料、导向副内部；同一法兰下的兄弟附件不自动豁免。
- **每个实物只有一个碰撞所有者**。父设备与子机构不要重复登记同一刚体。
- **外包命中不是结论**，要由精确几何确认。精确几何首次解码的耗时计入那一次查询。
- **结论标明运动来源**：`planned`（规划候选）、`simulated`（纯仿真行为表的插值运动）、`estimated`（孪生推演）、`observed`（真机读回）。
- **离散采样**：不证明连续扫掠无碰撞。关节按弧度、直线轴按米分别给步长；不补出未给出的连接段。
- **最小间隙只在要求时计算**（`clearance_margin_m`），不进入碰撞检查的热路径。超过 margin 时报告为“大于 margin”，不报告为 0。
- 内核依赖 `python-fcl`（0.7.x），只在用到内核时才导入；只用 profile 读取和运动链的调用方不需要安装它。

## 资产行为类（mechanism）

资产包里的行为类是**零导入的普通 Python 类**，不依赖 OS、不碰 USD。公开方法（不以下划线开头）就是
机构动作，第一个参数是 OS 传入的只读关节视图，返回普通数据：

```python
class Freezer:
    def open_door(self, joints):
        """开门"""
        return {"move": {"drawer_joint": joints.upper("drawer_joint")}, "duration": 4.0, "state": {"door": "open"}}
```

- 返回一个步骤或步骤列表；每步可含 `move`（关节 → SI 目标）、`duration`（秒）、`state`、`ease`。
- `joints` 只能查 `names` / `kind` / `lower` / `upper` / `position`；资产自己测试时传一个假对象即可。
- OS 侧的宿主 `unilabos.simulation.mechanism.Mechanism` 读实例根下的 UsdPhysics 关节，用 `joint_projection`
  摆位和读回。目标超出资产声明范围、或平移关节没有声明有限行程时拒绝（`MechanismError.code`），不裁剪、不缩放。
- `plan_action` / `Playback` 供仿真执行和孪生共用；结果是几何运动学证据（`kinematic_readback`），
  不是物理或真机证据。

单独试跑一个资产包，不需要驱动、设备图或工站（会导入并执行行为源码，只对可信资产使用）：

```text
python -m unilabos.simulation try --package PKG                       # 列出关节和机构动作
python -m unilabos.simulation try --package PKG --action open_door --action 'rotate={"angle_deg": 90}'
python -m unilabos.simulation try --package PKG --behavior 开发中.py:类名 --action ... --output report.json
```

试跑把实例装在非原点、带旋转的位置，检查读回与计划终值一致、安装变换未被改动；动作被拒绝时退出码非零。
依赖 `usd-core`、`numpy`、`scipy`。

### 设备运行时里的机构运行时

设置 `UNILABOS_SIMULATION_COMPOSITION=<工站仿真装配.json>`（`unilab.station-simulation/1`）后，设备运行时（Edge）
按配置为设备节点打开资产实例。每台设备写一段 OS 仿真执行配置的 `composition`，由 `compile_composition`
编译校验；资产行为运行时实现的是 `feedback: kinematic`、`completion: feedback`、`scheduler: coupled`，没有
`sources`：资产 USD 在进程内的运动学场景里按确认步（每段终了）摆位、读回，读回与计划值在容差内才算到位，
只是计时到了不算完成；搬运在所在段的确认步核对两端库位并记下读回后的实际位置。不做刚体动力学和接触力。
其余反馈档（`analytic` / `physics` / `resource_state`）在启动前报错，不降级。

- `composition.attachment: direct`：这台设备不构造驱动、不连协议端点（沿用已有的模拟动作目录注册），绑定了
  机构动作的动作按资产行为类的时长执行到完成再回报；被资产拒绝时按失败作业回报。没有绑定的动作保持原来的
  即时模拟结果。孪生（`UNILABOS_OBSERVATION_FEED_PROVIDER=twin-feed`）下不允许 direct。
- `composition.attachment: protocol`：设备照常由驱动执行（连协议模拟端，或孪生时连真机）；动作被接受时开始
  推演、给出结果时收尾（成功摆到终态，失败停在当前位置）。推演出错只记录，不影响真实动作。

完成证据标 `kinematic`，带各确认步的读回（`confirmed_steps`）和装配摘要 `composition_digest`。调度、占用、结果提交都走原有路径。观测写成后端
`observation_feed` 能跟随的 JSONL。配置里「对外动作名 → 机构动作 + 参数换算」由 `mechanism_binding.bind_actions`
从源码静态得到：驱动方法与资产行为类方法同名即对应；名字或参数不同时在驱动的 `@action(motion=...)` 上写明。
未设置该环境变量时行为与之前完全相同。
