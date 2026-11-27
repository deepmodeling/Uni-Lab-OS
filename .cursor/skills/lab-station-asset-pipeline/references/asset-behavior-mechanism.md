# 资产行为类：机构动作的写法与验收

用于给资产写「机构怎么动」的行为，供仿真和数字孪生共用。按锁拉取、动作合同核对见 [os-asset-binding.md](os-asset-binding.md)；
库位几何见 [asset-library-os.md](asset-library-os.md#库位几何与尺寸验收)；碰撞见 [collision-profiles.md](collision-profiles.md)。
需要带 `unilabos.simulation.mechanism` 的 OS（分支 `codex/sim-mechanism` 及之后）。

## 分工

| 内容 | 归属 | 说明 |
|---|---|---|
| 关节、限位、库位、台面基准、工具点 | 资产 USD | 跟型号走 |
| 机构动作（开门、转到某角度、取放板、通道到某点）和设备固有的时长 | 资产行为类 | 同一型号在哪个工站都一样 |
| device/action 合同、真机驱动、协议 | 设备包驱动（纯 `@device`/`@action`） | 驱动里不出现资产 UUID，也不写仿真代码 |
| 资产条目指向驱动 | 统一目录条目的 `module`、`source_registry` | 方向是条目 → 驱动 |
| 台面上摆了什么、示教偏移、协议展开成哪些基本动作 | 工站数据 | 每个工站不同，不进资产 |
| 调度、摆位、限位校验、读回、观测、库存结算 | OS | 资产不碰 USD，也不写库存 |

工艺或配方决定的时长（脚本跑多久、合成多久）不进资产。挂载关系和液体量的变化写在动作合同里由 OS 结算，
资产只给画面提示（`carry`）。

## 行为类的写法

**零导入的普通 Python 类**：不 import OS、pxr、设备包或任何工站代码，无参构造。公开方法（不以下划线开头）
就是机构动作；第一个参数是 OS 传入的只读关节视图，另可按名字要 `sites`、`materials` 两个视图；其余参数是动作入参。

```python
class Freezer:
    def open_door(self, joints):
        """开门：抽屉伸到资产声明的最大行程"""
        return {"move": {"drawer_joint": joints.upper("drawer_joint")}, "duration": 4.0, "state": {"door": "open"}}
```

返回一个步骤或按顺序执行的步骤列表。每步的键：

| 键 | 含义 |
|---|---|
| `move` | `{关节: 目标}`，SI 单位（平移米、转动弧度）。关节用实例根下的相对路径或唯一的 prim 名 |
| `duration` | 秒。每步都要写，除非调用方另给了默认时长 |
| `state` | 动作完成时写入的语义状态，例如 `{"door": "open"}` |
| `carry` | `{"from": 库位, "to": 库位}`，物料画面上从哪到哪；两端可写 `resource:<资源名>`。只是显示提示，不改库存 |
| `ease` | 默认 True（起停平滑） |
| `spin` | `{关节: 角速度}` 或 `{关节: [起始, 结束]}`（rad/s），这一段内角速度线性变化、不平滑。只用于没有声明限位的连续转动关节（转子、转盘）；终值 = 起始值 + 平均角速度 × 时长 |
| `advance` | `{关节: 增量}`（rad），相对当前位置转过这么多。只用于转动关节；声明范围覆盖整圈（上限 − 下限 ≥ 2π）时 OS 按整圈折回到范围内（姿态相同、限位不放宽），适合卷轴、收卷盘；范围不足整圈时越界照常拒绝 |

同一关节在一步里只能用 `move`、`spin`、`advance` 中的一个。`spin`、`advance` 需要 OS 提交 c9ddc140 及之后。
不要在行为类里用「0 秒退回整圈」或「转到很大的角度」来模拟连续转动，用这两个键。

三个视图都是只读的：

- `joints`：`names`、`kind(n)`、`lower(n)`、`upper(n)`、`position(n)`。
- `sites`（资产 USD 里 `Sites` 下的 Xform；名字是相对路径去掉 `Sites` 一级，可只写唯一的结尾）：
  `position(n)`、`along(关节, 库位)`（沿关节轴的坐标，两库位之差就是平移关节要走的距离）、
  `angle_about(关节, 库位)`（绕关节轴的方位角）、`from_frame(基准库位, 点)`、
  `reach(工具点, 点, clearance=0)`（直角坐标机构把工具点送到某点所需的关节目标）。
- `materials`：`point(资源名, 锚点)` 给出孔、吸头位等在台面坐标系里的点（OS 沿资源树算，锚点
  `origin`/`top_center`/`bottom_center`/`center`）；`get(资源名, 属性)` 取 `size_z`、`tip_length`、`fitting_depth` 等。

规则：

- 目标超出资产声明范围、平移关节没有声明有限行程、物料缺少需要的几何属性时，OS 直接拒绝。不要在行为类里裁剪、
  缩放或填默认值来凑通过。
- 位置一律从资产自己的关节和库位算，不查硬编码的表；确实没有依据的常数要在文件里写明是候选并说明来源。
- 行为类可以保留少量自身状态（例如哪个通道装着多长的吸头），但不保存库存事实。
- 非直角机构（SCARA、六轴）不要用 `reach`，交给机器人包逆解或用轨迹库。
- 连续旋转（离心转子）用 `spin` 按转速积分；有限转角但覆盖整圈的卷轴用 `advance`。画面采样远低于转速时会混叠，
  这是显示问题；`advance` 折回时关节值在一帧内跳 2π，消费端不要跨这一帧插值。

## 动作名与现有驱动对齐

OS 按 **Python 方法名** 把驱动动作和机构动作对上：同名、参数同名即对应，驱动多出来的可选参数（如超时）忽略。
所以先看设备包里同类适配器已有的方法名和参数，照着起名：

- 存储类：`fetch_plate_to_loading_tray(site)`、`take_in_plate(site)`、`open_door`、`close_door`。
- 移液类：`pick_up_tips(tip_spot_names, use_channels, offset_*_mm)`、`drop_tips`、
  `aspirate(resource_names, volumes_ul, use_channels, liquid_heights_mm, …)`、`dispense`。

名字或参数对不上的老驱动，在驱动的 `@action` 上加一行，不改资产：
`motion={"action": "rotate", "args": {"angle_deg": "angleEnum"}}`；参数可以是数值表达式，或
`"column_{posY:02d}/level_{posX}"` 这样的文字模板。没有驱动的设备由 OS 从行为类生成只声明合同的 `@device`
（`generate_simulated_device`），库存合同等用类属性 `ACTION_CONTRACTS = {动作名: {...}}` 补充。

## 资产要补的信息

行为要用到而资产里没有的，做成候选修订补上，逐项写依据：

- **有限行程**。没有厂家参数或实测时按资产几何估：`scripts/asset_behavior/estimate_limits.py 资产包`
  （活动连杆连同它带着的子连杆，包围盒沿关节轴不超出整机包围盒）。依据写「按资产几何估计，不是厂家参数或实测」。
- **库位**：存取位、交接位、铲板或托盘的承托面（挂在会动的连杆上）。
- **台面基准与工具点**（移液、取放类）：台面坐标原点在资产里的位置；每个通道或抓手的工具点挂在对应连杆上。
  有装配记录（固件坐标到资产坐标的平移、关节零位对应的坐标）的照记录写，没有的取连杆底面中心并标为估计。

候选修订用 `scripts/asset_behavior/make_candidate.py 已发布包 输出目录 行为.py 类名 [--range 关节=下,上:依据]
[--limit 关节=上限度:依据] [--site 路径=x,y,z:依据]` 生成：引用已发布包的 USD，只叠加改动，不修改封存包。
直角移液机构可以从 `cartesian_pipettor_template.py` 起步，改顶上的 `TOOLS`、`Z_JOINTS`。
正式入库仍走 [catalog-intake.md](catalog-intake.md)，发布需要明确授权。

## 验收

只加载一个资产包即可，不需要驱动、设备图或工站：

```text
python -m unilabos.simulation try --package 候选包                          # 列出关节、库位数和机构动作
python -m unilabos.simulation try --package 候选包 --action open_door --action 'rotate={"angle_deg": 90}' --output try-report.json
python -m unilabos.simulation try --package 候选包 --resources 资源图.json --action 'aspirate={"resource_names": ["plate_well_A1"], "use_channels": [0]}'
```

每个资产交：行为类、候选包、`try-report.json`。至少覆盖：每个动作走一遍；一个越界或缺前置条件的调用被拒绝；
`installation_preserved` 为真；到位值与手算一致（带物料的动作要核对到位高度 = 资源点 + 吸头或夹持偏移）。
用 PLR 耗材定义搭演示台面可用 `make_demo_deck.py`，演示台面不是现场布局，绝对位置不能当作与真机一致的证据。

资格标注：结果是几何运动学证据（`kinematic_readback`）。估出来的行程、标称时长、候选常数都不是硬件标定；
`carry` 不是库存事实。验收通过只说明行为与资产几何自洽。

## 耗材的孔位、吸头位：从耗材资产生成物料定义

移液类行为用 `materials.point(资源名, 锚点)` 要孔和吸头位的点，用 `materials.get(资源名, "tip_length" | "fitting_depth")`
要吸头几何。这些信息的正本是 PLR + OS `@resource` 的物料定义；不要在资产库位上另加一套给运行时直接读的键。
耗材资产自带孔位或吸头位和几何，物料定义从它生成，两边就不会各写一遍对不上：

```python
from unilabos.package_manager.package_distribution.asset_behavior import generate_resource_definition, resource_facts

facts = resource_facts(耗材资产包, kind="plate", well_depth="39.5:依据")            # 或 kind="tip_rack", fitting_depth="6:依据", tip_volume_ul=200
source = generate_resource_definition("asset_collection_plate_96_2p2ml", facts)   # 模块源码，工厂函数 make_<id>
```

- 孔位、吸头位取资产 `interfaces_data.sites`，外形取资产包围盒，吸头长度和顶面高度取资产里的吸头模型。
- **孔深、吸头配合深度从几何量不出来，必须给出并写依据**，生成器不猜；没有厂家数据时标为估计。
- 生成的工厂默认不带子节点（设备图里放 96 个孔会让资源树过大）；运行时算点时用 `with_items=True` 展开一次，不进资源树。
- 物料坐标系原点取资产包围盒最小角点，模块里的 `ASSET_ORIGIN_MM` 记录资产原点在其中的位置。
- 落地：生成结果放设备包的 `resources`（设备包里留一份清单和 `--check` 核对脚本，资产 ID 必须被场景资产锁固定）；
  统一目录里该耗材条目的 `module`、`source_registry` 指向生成的工厂（改条目属于入库，需要授权）。
- 运行时：机构运行时配置或 `try --scene` 的场景说明写 `{"scene": OS 场景图, "device": 本设备节点, "frame_site": 台面基准库位,
  "definitions": {资产 ID: "模块:工厂函数"}}`，资源名写成 `节点/库位`（例如 `cyclization_plate_1/A1`）。
  点位 = 场景图里节点的位姿 × 物料定义展开出的孔位；节点只支持绕 Z 的旋转。

## 协议级动作：步骤表

驱动只有“跑某个协议”一类动作时（例如移液站的 `run_protocol_3(protocol, volume, position)`），在工站数据里写步骤表，
把它展开成基本机构动作；机构运行时的动作绑定写 `{"table": 步骤表.json, "key": "{protocol}"}`，步骤表是
`{协议名: [{"mechanism": 动作, "args": {...}}, ...]}`，参数里的文字支持 `{入参}` 模板（含列表里的资源名）。
整条先算计划再执行，中间有一步被拒绝则整条不执行。步骤内容必须有来源（厂家方法导出、工作表或用户给的清单），
没有来源时只能标为示意，用来验证链路。

## 发布成资产修订

行为的改动落在资产修订上；设备包除了个别 `motion=` 对应声明不需要改。发布需要明确授权，流程和门槛见
[catalog-intake.md](catalog-intake.md)，这里只列行为修订特有的做法（2026-11 的 15 个 R194 修订按此完成）。

1. **在库里的来源包旁边建新修订目录**，不新建资产 ID：复制来源包的 `simready_usd/`，目录名用
   `<原名>__<新修订号>`。修订号先查索引里已用到的标签，不要撞号。
2. **入口 USD 改名为包目录名**，`simready_usd/` 根下只能有这一个 USD（封包规则 NP.005）。所以候选修订里那层
   “引用原 USD 再叠改动”的 usda 不能直接当入口：把其中的行程、库位改动写进复制出来的入口层
   （`Sdf.CopySpec` 逐个属性、新库位整棵复制），原文件里的相对贴图路径才不会断。
3. **换行为**：`behavior/` 只留新的行为类文件；`behavior_binding` 指向新类，`requires` 为空；`entrypoints.behavior`
   指向新文件，指向已删除文件的其他入口一并去掉。旧绑定、逐项改动和依据记进
   `parameters.mechanism_behavior`，原有的运动验收证据保留并注明描述的是来源修订。
4. **封包前核对**：几何事实与来源一致、依赖闭包都在包内、关节集合不变且变化的关节正好是声明改动的那些、
   新库位在来源里不存在；然后重算 `usd_dependencies`、readiness 的绑定摘要并封包。
5. **封存后再试跑一遍**：用封存的包（不是暂存候选）重跑暂存时的全部动作，结果逐条一致才入库。
6. **入库与上传**沿用目标库现成的修订发布器（更新同一目录条目的 `model.targets`、工程索引追加新包并保留旧包、
   重建检索索引、对象不可变上传、冷下载重开校验、最后提交远端索引）。凭据只放进发布进程的环境，不落盘。
7. **发布后读回**：从另一台机器按 `asset_id + 摘要` 读回索引、条目、包清单和行为文件，与构建结果比对。
8. **让使用方用上**：发布不会改变任何现有锁。把场景或设备包的资产锁改到新修订并重新拉取，再用整站跑一遍确认。

已知边界：网页轻量档按字节沿用来源修订，不含新补的行程和库位；要在前端读到它们需要另出一版轻量档。

## 例子

`scripts/asset_behavior/examples/` 下是七个已经试跑通过的行为类，按复杂度从低到高：

| 文件 | 类型 | 看点 |
|---|---|---|
| `freezer.py` | 单关节开关 | 目标取资产声明的行程上限，不写数字 |
| `lower_door.py` | 没有驱动的被动机构 | 配合 OS 生成只声明合同的设备 |
| `turntable.py` | 带入参的转动 | 角度入参换算成弧度，越界由 OS 拒绝 |
| `rotating_stack.py` | 取放板、多步、`carry` | 用 `sites.angle_about`、`sites.along` 从库位算目标 |
| `g3.py` | 直角移液加夹爪 | 模板生成的四个移液动作加设备特有动作 |
| `hamilton_star.py` | 多通道移液 | 台面基准、每通道工具点、吸头长度 |
| `fluent.py` | 多通道移液（估计行程） | 行程全部按几何估计并标注依据 |
