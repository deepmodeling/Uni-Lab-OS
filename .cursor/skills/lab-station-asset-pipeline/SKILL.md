---
name: lab-station-asset-pipeline
description: 处理实验室工站 CAD/URDF/网格与参考照片，建立可实例化的标准资产库，补齐既有资产 CAD，并从机构组合设备、生成 SolidWorks 装配及 OS 原生领域设备包。用于资产接入、机构/接触建模、外观修复、行为打包和组合验证；也用于按开发指南给工站配置纯仿真、数字孪生与真机整站切换。复用现有 OS 调度与库存实现。
metadata:
  version: "1.20.0"
---

# 实验室工站资产工程

将工程资料转为标准资产库中的独立定义，再通过实例组成可编辑工站。设计项目以 OS 原生领域设备包交付，OS graph 保存装配；单纯浏览、补材质或局部修复按用户范围交付。默认中文沟通，沿用已有工作区约定。

本 skill 可独立分享：随包包含操作方法、模板、通用转换/封包/验收脚本及其源码依赖闭包。Blender、OpenUSD、CAD 应用、OS 与可选 SDK 由使用者环境提供；脚本不依赖作者 checkout 或固定服务器路径。工具入口与依赖见 [portable-tools.md](references/portable-tools.md)。

## 开始与选路

从请求和文件推断来源、目标对象、目标动作、消费者与输出位置。关键冲突才询问；先完成不依赖答案的清点。已有授权在本轮持续有效。仅浏览或补贴图时不强制全站动力学检查；局部修改从受影响对象继续。

1. 读取工作区约定，辨认当前资产版本与未提交修改。在来源目录之外建立本轮输出，保持来源与历史回执。
2. 用当前 skill 目录定位 `scripts/`，不要假定当前工作目录等于 skill 目录。基础工具使用 Python 3.10+，随包 toolkit 使用 Python 3.11+；选择所需依赖齐全的解释器（Windows 常为 `py -3`；macOS/Linux 常为 `python3`）。
3. 运行只读预检；具体参数先看 `scripts/preflight.py --help`。先看输入错误和路线缺口。STEP 头部初检不证明 CAD 可解析；工具探测仅表示可发现，许可证、实际导入和运行分别检查。
4. 请求涉及整个工站接入时，先按下面的阶段表判断所处阶段，按顺序走；单项工作直接查后面的参考表。两张表都只读必要参考，无需一次加载全部文件。

### 按工站所处阶段选流程

| 阶段 | 怎么判断 | 按顺序做 | 先问人 |
| --- | --- | --- | --- |
| 一、刚拿到总装 CAD 和需求文档 | 有总装 CAD/照片和需求文档；库里没有该工站资产，也没有设备包 | 需求→设备与动作清单并查重 → 拆分、关节、库位 → 碰撞三层与 web 轻量档 → 行为类试跑 → 入库发布 → 场景与资产锁 → 纯仿真整站跑通 | 入库/发布修订、改目录条目、推送；不写驱动、不接真机 |
| 二、通信接口已接，还没做孪生 | 设备包驱动已是 `@device`/`@action` 且能连真机；没有行为类、机构运行时配置或场景锁 | 驱动动作逐个对资产与行为类（同名同参，否则 `motion=`）→ 补资产/行为类/步骤表/物料定义 → 发布修订并改锁 → 纯仿真再孪生 → 说明反馈等级，按需碰撞查询 | 发布、改锁、推送；不改驱动通信逻辑 |
| 三、已有仿真，要上真机联调 | 纯仿真和孪生都已跑通，锁指向固定修订 | 非标定量清单与风险 → 整站切 twin 后一次只动一台、先空载、读回对比推演 → 超差改资产并重发修订 → 机械臂/转运先碰撞查询再低速 → 真机回放与仿真回放对照 | 每台设备上电前、发布、改锁、推送 |

每个阶段的展开步骤、对应参考页和可直接复制的示例提问见 [station-stages.md](references/station-stages.md)；三种运行方式、反馈等级和验收清单的总体规则以开发指南 [station-twin-dev-guide.md](references/station-twin-dev-guide.md) 为准。

### 按单项工作选参考

| 本次工作 | 读取参考 | 预期产物 |
| --- | --- | --- |
| SolidWorks / STEP / URDF / 已有网格 / 无 CAD 照片 | [source-routes.md](references/source-routes.md) | 来源清单、转换路线、坐标证据 |
| **任何 CAD→USD、GLB→USD、整站场景或入库之前** | [up-axis.md](references/up-axis.md) | Z-up 证据（地标 + 原始 glTF 包围盒 + 门禁输出） |
| 商业整机/自动化装置的拆分边界、耗材提取、机构组合与验收 | [decomposition-boundaries.md](references/decomposition-boundaries.md) | 边界清单、可复用定义、实例装配与放行证据 |
| 通用转换、原生封包、几何/碰撞验收或更新随包工具 | [portable-tools.md](references/portable-tools.md) | 可脱离来源仓运行的工具及摘要回执 |
| 拆分对象、修复漏件、家族与实例、整站装配 | [identity-assembly.md](references/identity-assembly.md) | 对象映射、覆盖清单、可编辑分层资产 |
| 入库、耗材拆分、重复摆放、资产行为或 OS 领域包组合 | [asset-library-os.md](references/asset-library-os.md) | 标准原生资产包、实例图、资产锁、领域设备包和空项目验证 |
| 按锁拉取资产、接入已有 device/action、构建资产行为或升级 OS 消费版本 | [os-asset-binding.md](references/os-asset-binding.md) | 同版本 fetch/build/check-bindings、静态合同与实际执行分项回执 |
| 设备包的显示模型怎么来：资产库锁定落盘（OSS/镜像/本地工程包，可离线）还是本地模型直接放包里；模型间共享相同贴图；前端加载慢 | [model-sources.md](references/model-sources.md) | `assets fetch --project` 落盘与 `verify --project` 核对回执，或本地模型的 `assets share` 结果 |
| 给工站接入数字孪生：sim/twin/real 整站切换、设备包与资产行为对应、观测 basis、碰撞与前端轻量档的分工 | [station-twin-dev-guide.md](references/station-twin-dev-guide.md) | 按指南逐项核对的接入清单；细则跳转本技能对应参考页 |
| 给资产写机构动作（开关门、取放板、移液等）供仿真和孪生用，补行程、工具点或台面基准 | [asset-behavior-mechanism.md](references/asset-behavior-mechanism.md) | 零导入行为类、候选修订及逐项依据、试跑报告；已授权时发布成资产修订并重锁 |
| 耗材（板、吸头盒）的孔位/吸头位、移液点位，从耗材资产生成 PLR/`@resource` 物料定义 | [物料定义](references/asset-behavior-mechanism.md#耗材的孔位吸头位从耗材资产生成物料定义) | 生成的工厂模块、孔深/配合深度依据、`--check` 核对 |
| 驱动只有“跑某个协议/方法”的动作，要展开成基本机构动作 | [步骤表](references/asset-behavior-mechanism.md#协议级动作步骤表) | 带来源的步骤表、机构运行时的 `table` 绑定、整条预演结果 |
| 行为、行程或库位改完后发布成资产修订、改锁 | [发布成资产修订](references/asset-behavior-mechanism.md#发布成资产修订) | 封存修订、复跑与读回、重锁后的拉取结果 |
| 定义或修改库位、排查库位显示形状 | [库位几何与尺寸验收](references/asset-library-os.md#库位几何与尺寸验收) | 逐库位测量、空间语义、尺寸来源与消费读回 |
| **任何正式入库、发布修订或更新广场条目之前** | [catalog-intake.md](references/catalog-intake.md) | 13 个公共字段核对、已确认分类映射、固定模型引用、索引与检索读回 |
| 给既有资产补 CAD、机构组合成设备、生成或验证 SolidWorks 装配 | [cad-assembly.md](references/cad-assembly.md) | 同条目 CAD 表示、固定装配引用、原生配合与运动读回 |
| 机械臂/末端工具入库、品牌关联或资产迁移回归 | [robot-modules.md](references/robot-modules.md) | 双向型号关联、固定 pin、品牌/OS 消费验收 |
| 机器人/夹爪/滑台、孔槽内腔、抓放/插入 | [mechanisms-contact.md](references/mechanisms-contact.md) | 机构、碰撞、操作部位及任务验证 |
| 碰撞体构建、规划 profile、碰撞查询或加载性能；任意机构（机械臂、导轨、龙门、门盖等）或仿真/孪生运动的碰撞检查 | [collision-profiles.md](references/collision-profiles.md) | 分层碰撞表示、覆盖与误差证据、锁定的 OS 场景绑定、OS 碰撞查询调用、运动来源与证据标注 |
| 前端显示太慢、大 CAD 资产要给 three.js/网页看（减面、贴图降采样） | [web-profile.md](references/web-profile.md) | 同包新修订里的 `profiles/web`、血缘 `parameters.web_profile`、放行门与前端实测 |
| CAD 网格导出 USD、Blender 着色异常、标准 PBR、Meshy 重贴图、标识与屏幕 | [appearance-meshy.md](references/appearance-meshy.md) | 显式法线、跨软件显示证据、可替换的外观层及来源 |
| 交付、能力声明、变更复验、便携打包 | [validation-delivery.md](references/validation-delivery.md) | 版本、证据、完整依赖与入口 |
| 批量核对身份依据、替换不变量、关节与行为 | [p0-checks.md](references/p0-checks.md) | 明确检查范围、三组机器回执及未确认项 |
| 遇到源缺陷、物料重复、局部通过但整站干涉 | [station-cases.md](references/station-cases.md) | 两类工站的排查思路 |

## 核心工作流

### 1. 固定任务与来源

确认本次目标是外观、编辑、机构、接触，还是指定任务。多目标可并行记录，不把能力压成单一 `simulation_ready`。需要书面配置时，以 [task.example.json](assets/task.example.json) 为起点，替换示例路径，只保留本次需要的字段。

记录输入摘要、配置/抑制状态、转换工具与版本、单位和坐标基准。用户已有合格资产时记录其版本并复用。原始 CAD 配置、文件名和完整装配路径保留在本地来源清单；分享交付按用户授权选取内容。

### 2. 建立对象与坐标

区分几何原型、源 occurrence、语义对象、设备家族、工站实例和运行刚体。名称只能帮助检索；相同零件在不同祖先路径下是不同实例。保存源到目标映射，明确完整覆盖、排除项和未决项。

家族局部坐标与整站位姿分开；保留局部/世界变换、单位与轴向。通用交付采用明确声明的米制 USD，实际 up-axis 按来源与目标消费者转换。导出 GLB 时单独验证该格式/导入器的坐标约定，避免二次旋转。

**Z-up 是交付约定，必须实测，不能凭导出选项假设。** 完整判定、修正与门禁见
[up-axis.md](references/up-axis.md)。要点：

- SolidWorks 的 glTF/GLB 按规范写 Y-up，几何内容通常还是 CAD 的 Z-up；Blender 等导入器会
  **无条件**执行 Y-up→Z-up，直接导入会把整站放倒（Y 变成上、机器仰面躺倒）。
- 首选随包转换器：`historical_blender.py --native-cad-frame` 已实现基变换抵消；自己写脚本时在
  根节点叠加 `Rx(-90°)` 再用 `export_yup=False` 导出，不要再手工套第二次 `rotateX(90)`。
- 用实物地标证明：全部脚轮/地脚共面于同一 Z（minZ 一致），三色灯（或 AGV 顶门）在最高 Z；
  脚轮共面于 Y、或最高件落在 Y 上，就是被放倒了。`上罩` 是侧上罩，不适合当"最高件"地标。
- 只改 `upAxis` 元数据、不在几何上验证，不算修正；典型错误表现是"仪器挤在竖直薄层、底面离地、
  堆栈/立柱被压扁"。
- 入库与验收门：`upAxis=Z`、`metersPerUnit=1`、地面 Z≈0、脚轮共面 Z、最高件在 Z、USD 包围盒与
  原始 glTF 包围盒不呈 Y/Z 互换；不通过不得入库和交付。门禁：`scripts/verify_up_axis.py`。

### 3. 拆分、修复与分层加工

默认拆分按 [decomposition-boundaries.md](references/decomposition-boundaries.md)：单个成品商业设备保持整机资产；全部耗材独立提取并复用定义、按数量实例化；自制/集成自动化装置拆至完整直线模组、完整传感器、末端工具和独立采购运动部件层面。资产边界与内部刚体/关节划分分开，内部仿真可动不要求继续拆资产。

按源 occurrence 保存覆盖与归属，引用子资产组装自动化设备；机构持有自身行为，设备持有协调行为。修复、坐标和几何复用按 [identity-assembly.md](references/identity-assembly.md)；拆分完整性、实例数量、无重复表示和第二空项目按边界参考逐项验收。

外观、运动、碰撞和语义分别维护。机器人先找匹配型号的厂家骨架，再将 CAD 部件归入 link；门、夹指和滑台通过多个姿态验证归组。记录操作区域：抓持截面、支撑面、接收口、插入轴、接近/退出方向。

### 4. 配置外观与物理

**CAD 三角网格导出 USD 时必须显式保存可靠的着色法线。** 按 [CAD 网格法线](references/appearance-meshy.md#cad-网格法线) 保留或重建面角法线，默认写入 `normals` 并设置 `faceVarying` 插值；在 Blender 中重新导入验证。把法线作为资产数据交付，不能以单个渲染器自动计算后的正常画面替代这项检查。

普通金属、玻璃、塑料优先使用可调材质模板；复杂外观按需调用 Meshy Retexture。标签、刻度和读数用精确图层。Meshy 返回件先检查几何表面、尺寸、变换和 UV，再接到视觉层。

碰撞资产生产与规划查询按 [collision-profiles.md](references/collision-profiles.md) 分开表示、记录覆盖并按用途验收。碰撞根据任务选择：简单实体、凸分解、静态三角网格或适合目标引擎的 SDF。对孔、槽、薄壁和接触垫做针对性检查。质量、摩擦、驱动与传感参数标明来自测量、厂家、计算还是工程假设；渲染参数不自动变成物性。

### 5. 入库并组装 OS 项目

**正式入库必须完善公共字段并找到合适的已确认分类。** 按 [公共字段与分类入库规则](references/catalog-intake.md) 核对 13 个字段，复用规范分类 ID、记录来源与分类依据，完成统一条目及索引同步，并实际按分类检索读回。工程包写盘或旧包索引更新不等于完整入库；字段、分类或消费方投影尚未完成时保留候选状态及具体缺口。可空字段按真实适用性记录，不编造厂家、驱动或模型。

正式资产按 [asset-library-os.md](references/asset-library-os.md) 写入当前标准库，耗材与设备分别封装，固定 ID、修订、入口和原生 BOM；复用现有入库/校验器。先生成可审查的候选包，发布遵循用户已有授权。`.blend` 或松散的 `families/` 文件夹用于编辑/预览，不替代入库。

**入库前必须先查重**：按 manufacturer/model/规格、几何指纹（包围盒、三角形数、网格组成）和接口数据检索标准库；命中同一定义时只引用其 asset_id/修订，不重复入库、不新建 ID 或修订目录，也不为"本地版本更新"另建平行条目（确需变更时追加同一资产的修订）。查重结论写入入库清单（命中项记录被复用的 asset_id 与依据，未命中项记录检索范围），只有确认无匹配才生成新包。检索实现与字段见 [asset-library-os.md](references/asset-library-os.md#2-标准资产库)。

**资产命名一律中文**：目录条目、`original_name`、包目录与 USD 入口文件名使用中文名，允许字母（如 `SPR检测模块`）；型号与图纸号写在 `title`。USD prim 标识符受 `SdfPath` 限制保持 ASCII，中文原名写入 `lab:sourceName`；内部脚本键可用 ASCII，映射写进入库清单。已发布的英文名资产改名时保留原 `asset_id`，按新中文名发布修订，不新建平行身份。详见 [asset-library-os.md](references/asset-library-os.md#资产命名一律中文)。

给已有资产补 CAD 时更新同一资产条目的表示与入口，保持身份；由已入库子资产组成的父装配保存固定引用，按需在项目中生成 CAD。涉及原生总装与运动时执行 [CAD 与装配标准](references/cad-assembly.md)，分别验证文件可开、装配正确和关节可动。

从锁定资产构造 OS graph：每个设备、载架、物料分别实例化，明确父子关系、局部位姿、CAD 锚点和模型绑定。Blender 编辑实例布局，经现有适配器保存并重新读取 graph。同一实物在视觉、碰撞与库存中的表示保持身份关联；取出源槽物料时更新同一物料的归属，场景中只保留一个实物实例。

资产保存几何、接口、库位定义和专属行为；领域包保存 OS 声明、实例、布局、绑定和工艺工作流；共享 Python package 保存真正共用的能力。复用已有实现，构建时固化资产脚本并锁摘要；OS 继续负责调度、Claim、库存和结算。逐项登记未接通的后端，完成加载与编译后仍需实际执行验收。

### 6. 按用途验证与交付

先检查依赖、坐标、对象覆盖与编辑重开，再执行本次目标所需的机构或接触验证。坐标检查不是看
`upAxis` 就结束：必须跑 [up-axis.md](references/up-axis.md) 的门禁（地标方向 + 原始 glTF 包围盒
对齐），把输出写进证据。实际运行应读取引擎中的位姿/接触/关节，而不是仅检查命令目标。抓放包含支撑、释放、撤离与稳定窗口；局部通过后，整站任务另查机器人和周围设备。

按 [validation-delivery.md](references/validation-delivery.md) 保存每项能力的对象、版本、状态和证据。资产先异地重开；涉及领域包组合时，再在第二个空项目中更换实例名/布局验证复用，按对应参考执行 Catalog、工作流及运行检查。报告标准库位置、领域包/graph 入口、独立对象、已验证动作及具体缺口。回放、过程模型、OS 结算、真实规划与物理执行分别表达。

## 自带工具与运行方式

- `scripts/asset_tools.py`：随包通用转换、原生封包、USD/来源/碰撞验收入口；先执行 `list` 和 `verify-toolkit`，实际参数以对应命令 `--help` 为准。代码来源与第三方依赖见 [portable-tools.md](references/portable-tools.md)。
- `scripts/verify_up_axis.py`：Z-up 门禁。对每个 USD 输出 `upAxis`/`metersPerUnit`/网格数/包围盒/最高轴；`--glb` 把原始 glTF 包围盒与 USD 对齐（Y/Z 互换即判 Y-up 放倒），`--landmark 脚轮 --top 三色灯` 用地标证明几何真的 Z 向上，`--require-floor` 要求地面 Z≈0。见 [up-axis.md](references/up-axis.md)。
- `scripts/web_profile/make_web_profile_revision.py`：给资产包做前端轻量档的新修订（meshoptimizer 减面 + 贴图降采样，prim/关节/变换/材质绑定不变，带放行门与血缘、原生封包）；核心在同目录 `webprofile.py` + `simplify.mjs`（node + meshoptimizer）。见 [web-profile.md](references/web-profile.md)。
- `scripts/preflight.py`：环境发现、来源清单、STEP 头部初检及按格式的路线缺口；离线、只读，不自动安装软件。
- `scripts/bundle_check.py`：工程交付目录的文件摘要与读回；可选 OpenUSD 依赖检查。它不生成原生资产库 BOM；标准资产包使用随包原生封装/校验器或目标库同版本实现，避免往已封包目录额外写 seal 文件。
- `scripts/meshy_retexture.py`：离线预览、一次提交、单次状态查询及结果下载。先按外观参考准备授权与费用依据；凭据只从使用者自己的环境变量读取。
- `scripts/asset_behavior/conformance_os.py`：资产行为转发声明的 OS 合同对照。构建器本体在 Uni-Lab-OS `unilabos.package_manager.package_distribution.asset_behavior`；本工具用 OS 静态扫描器逐个对照领域包 @device 类与生成的转发类，只读输入。见同目录 README。

现有项目若有经过验证的转换器，优先复用并检查当前实现。新环境可以使用合适的本地工具或编写小型适配脚本；不要为满足路径假设复制作者私仓。需要安装 CAD/渲染运行时，先给出与本次路线相符的具体依赖与用途，利用已有授权完成可逆配置。

Meshy 的生成/上传授权与额度按用户本次请求执行。仅请求设计或安装 skill 不触发付费任务；已有任务从保存的 ID 查询，未知提交结果不自动再次 POST。真实硬件和远端发布按当前任务的明确授权执行。

## 完成时回答

用简洁中文给出：本次处理了什么、默认打开哪个文件、哪些对象和动作已验证、哪些问题仍需处理。附输出路径及可复现命令；不要把历史案例的验证结果当成本次运行结果。
