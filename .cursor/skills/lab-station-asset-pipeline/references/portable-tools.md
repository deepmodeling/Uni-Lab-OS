# 随技能携带的转换与验收工具

读取时机：实际转换/封包/验收、消费机器没有 Asset_pipeline checkout，或更新随技能携带的工具。使用者复制整个技能目录即可取得工具源码；Blender、OpenUSD、SolidWorks、CAD 内核与可选 SDK 仍由所用运行时提供。

## 入口与依赖

```text
python <skill>/scripts/asset_tools.py list
python <skill>/scripts/asset_tools.py verify-toolkit
python <skill>/scripts/asset_tools.py run <command> -- --help
```

toolkit 使用 Python 3.11+；已验证环境为 Python 3.12.3、OpenUSD 26.8、NumPy 1.26.4。元数据/缩略图分支需要 Pillow，部分几何分支需要 SciPy；按路线使用已安装版本并记录实际版本，不自动混装所有依赖。

`asset_tools.py` 默认使用启动它的 Python；选用已具备目标路线依赖的解释器。调用前验证随包代码的摘要，命令参数传给原工具。运行时不读取作者 checkout，不自动下载工具或安装依赖。

`convert` 的 GLB/SolidWorks 路线通过 `historical_blender.py` 导入网格。SolidWorks 的 GLB 内容保持
CAD 的 Z-up，而 Blender 会做 Y-up→Z-up 基变换；`--native-cad-frame` 会把这次基变换抵消（网格数据
与节点变换同时共轭）。自己写 Blender 脚本时必须做同样的事，否则整站会以 Y-up 交付；转换后统一跑
`verify_up_axis.py` 门禁，不要只看 `upAxis`。

| 路线 | command / API | 运行时与范围 |
|---|---|---|
| USD、GLB、展开 URDF、STEP、SolidWorks | `convert` | OpenUSD；GLB/URDF 需显式 Blender 路径；当前 CLI 的 STEP/SW 路线使用 Windows SolidWorks COM 和 Blender。USD 保留依赖闭包。 |
| 可选 STEP/XDE CAD 内核 | `toolkit/asset-library/neutral_cad.py:convert` | OCP、OpenUSD；调用前核对函数参数与来源单位，无商业 CAD 时可选。不是 CLI `convert` 的自动替代。 |
| 标准库封包/入库 | `ingest`；`build_native_library.export_package` | 原生类别、厂家、修订、manifest/BOM；明确分配资产身份。组合资产使用 `preserve_composition` 与完整 support files，不能默认 flatten 掉工程引用。 |
| 库及单包检查 | `audit-library`、`native-verify` | 原生路径、BOM、摘要与引用完整性；`audit-library` 按现有完整索引合同，不用于没有完整索引的散包。 |
| 图物化、复用与锁检查 | `client` | 原有 materialize / verify-project；完整行为领域包生成仍由已安装 OS 和项目合同负责。fetch 仅在需要时使用显式配置与用户凭据。 |
| USD 几何、依赖与比较 | `inspect-usd`、`verify-source`、`compare-usd` | OpenUSD、NumPy；源几何路线可能需 OCP。源包围盒检查不等于逐面正确性。 |
| VC、RoboDK 离线转换 | `convert-vc`、`convert-robodk` | OpenUSD、NumPy，部分几何需 SciPy；保留来源声明和未解码项，不执行来源行为脚本，不授予原生求解/动力学资格。 |
| SimReady 元数据及官方检查 | `finish-simready`、`check-native-rules`、`audit-simready` | 元数据收尾按 contract 执行；官方检查需已安装 SDK/规则和明确版本。NP.005/hash 通过不代表全部 profile 通过。 |
| SolidWorks 引用式装配 | `assemble-solidworks`；`cad_composition.from_library`、`verify_solidworks_assembly.verify` | Windows/COM 与显式配方；实际重开、配合运动需在原生应用执行。验收 API 接收真实应用对象。 |
| **Z-up 门禁（所有转换/入库之后）** | `scripts/verify_up_axis.py`（技能脚本目录） | OpenUSD（USD 侧）+ NumPy（GLB 侧）。检查 upAxis/mpu/网格数/最高轴；`--glb` 对比原始 glTF 包围盒（Y/Z 互换即失败）；`--landmark/--top` 用地标证明几何 Z-up。见 [up-axis.md](up-axis.md)。 |
| **前端轻量档（web profile）** | `scripts/web_profile/make_web_profile_revision.py`（核心 `webprofile.py` + `simplify.mjs`） | OpenUSD、NumPy、Pillow、node + npm 包 meshoptimizer（`--meshopt-from` 或 `MESHOPT_FROM`）；封包与入库用目标资产库自己的同版本工具（`--library-tools` 指向含 `admit_reviewed_asset_batch.py` 的目录）。逐网格减面、贴图降采样，prim/关节/变换/材质绑定不变，带放行门与血缘。见 [web-profile.md](web-profile.md)。 |
| 碰撞表示生成和验收 | `collision-export`、`collision-build`、`collision-verify`、`collision-package`；单元测试 `collision-profiles/test_profiles.py` | OpenUSD/NumPy；保护区域与参数按用途指定；精确覆盖核对、外包策略和包修订分别验收。查询内核不随包携带，在 OS 的 `unilabos.simulation.collision`（需 `python-fcl`），见 [collision-profiles.md](collision-profiles.md#查询内核os-unilabossimulationcollision)。 |

Python API 文件与其本地依赖位于 `scripts/toolkit` 下，保留原相对目录。使用 API 时把相应 `asset-library` 目录加入当前进程的模块搜索路径；不需要添加来源仓。工具提供哪些选项以随包版本的 `--help` 和实现为准。

碰撞导出器当前采用“存在 authored collider 时选择 authored collider，否则选择 CAD 几何”的资产级策略。混合资产必须逐刚体审计覆盖；不能因某处有 collider 就认定未选 CAD 区域可以忽略。必要时先在候选表示中补齐明确 collider 或分范围处理，再生成 profile。legacy schema/role 字符串保留与消费者兼容，不是通用设备分类依据。

## 可携带与维护

新增 `check-identity`、`check-replacement`、`check-motion`，输入合同与运行界限见 [p0-checks.md](p0-checks.md)。前者只需标准库；替换需 OpenUSD/NumPy/SciPy；运动需 OpenUSD/NumPy 和受信任行为代码。

只新增命令且不升级旧工具时，可用导出器 `--base-toolkit <既有工具目录>`：先验证基线摘要，保留原有文件逐字节不变，加入新入口及未携带依赖。新增依赖若需改变旧实现，应改走明确升级及回归，不能混用来源仓的最新版冒充已验证随包版本。

```text
lab-station-asset-pipeline/
  scripts/
    preflight.py
    bundle_check.py
    meshy_retexture.py
    asset_tools.py
    toolkit/
      manifest.json            # 命令、实现来源、逐文件 SHA256
      asset-library/*.py       # 转换/封包/校验及必要依赖
      asset-library/enrichment/...
      asset-library/robot_behavior/...
      asset-browser/*.py       # 被通用实现引用的元数据辅助模块
      collision-profiles/*.py
```

Asset_pipeline 是随包工具的实现来源；技能携带经过核验的发布副本。更新时使用仓库 `tools/skill-toolkit/export_toolkit.py --repo <repo> --output <新的工具目录>` 生成依赖闭包，对比 manifest、执行受影响的测试、再替换技能内同一版本集合。保留旧副本与回执在任务输出目录；不要仅编辑技能内的复制算法导致两套实现分叉。manifest 的 HEAD 只是来源提示，实际内容以工作树文件 SHA256 为准。

工具包是显式通用入口的依赖闭包；其中间接依赖可能含旧批处理或云操作函数，不因随包携带就触发执行。`list` 中的命令才是推荐入口。作者机器路径、凭据、模型资产、缓存及历史回执不随工具打包。

目前不把固定旋蒸零件的 CAD 往返脚本、固定工站/A/B/C 配方、供应商批次管理或六轴机器人专用 PhysX 试验当作通用转换器。OS/FCL/Isaac 运行实现继续作为正式 package/运行时依赖，不复制进技能。遇到尚未通用化的路线先明确其假设，再在来源工具中抽取参数化能力，验证后随技能更新；不要求新项目复刻历史工站。

## 验收与更新的证据

复制技能到另一目录，在没有来源仓挂载/PYTHONPATH 的进程中校验工具摘要，并实际执行代表性转换→标准封包→读回→重复实例物化→碰撞 profile 核对；保留缺依赖、摘要篡改、漏件等反例。每条商业应用/物理引擎路线按本次实际执行情况记录，不因文件已携带或 `--help` 成功就写成运行验收通过。

拆分边界和装配语义验收以 [decomposition-boundaries.md](decomposition-boundaries.md) 为准；这些工具提供确定性文件与几何检查，不取代语义判定。
