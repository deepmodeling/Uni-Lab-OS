# 验证与便携交付

按本次用途选择检查；不要求每个外观任务都进行接触实验。生成报告、校验工具或写入物理字段，不等同于已经运行对应检查。

## 最小记录

保存一个便于机器读取的对象表和一份简短报告。可以沿用已有项目 schema；新项目至少记录以下关系：

| 字段 | 含义 |
| --- | --- |
| source_digest / source_occurrence | 输入版本与完整祖先路径 |
| prototype_id / family_id | 共享几何、可复用设备/资源类型 |
| instance_id / target_prim | 当前场景实例与运行表示 |
| role / parent / frame | 静态、机构、物料、传感器等角色及装配关系 |
| source_to_local / local_to_world | 单位和矩阵约定明确的变换 |
| material_id / slot_id | 涉及库存时的物料身份与槽位 |
| property_provenance | 数值来源：measured、vendor、derived、assumed、unknown |
| capability / evidence | 对象、版本、用途、实际结果及证据相对路径 |

缺少来源时保留 unknown，不能从相似名称填入已确认值。family_id 可在本地命名；没有领域包时不伪造 `source_fqid`。相同几何指纹不自动说明属于同一设备型号。

## 能力分别记录

| 维度 | 典型检查 |
| --- | --- |
| geometry | 覆盖、尺寸、世界变换、孔槽/接触面和遗漏 |
| appearance | 显式法线与消费者读回、多视角、材质分区、纹理依赖、实拍对应 |
| editing | 单对象移动旋转、子件跟随、另一实例不变、保存与恢复 |
| kinematics | 轴/限位、多个姿态、CAD 到 link 归组、TCP |
| contact | 本次引擎实际 collider、落稳、夹持、内腔/孔道探针 |
| task | 指定机器人/夹具、对象、负载、布局和动作全过程 |
| observation / process | 只有本次接入真实实现并验证时填写；否则 not_run |
| portability | 依赖、摘要、异地目录重开、消费者/安装读回 |

建议状态为 passed、failed、partial、not_run、not_applicable。not_applicable 要给理由；没有运行结果用 not_run。不同维度互不自动升级。报告试验次数、仿真步数和断言数时区分含义。

一个结果记录示例：

```json
{
  "capability": "contact",
  "subject": "plate_instance_01",
  "status": "not_run",
  "asset_digest": null,
  "runtime": null,
  "scope": "lift, transport, release and retract with selected fixture",
  "evidence": [],
  "reason": "The target runtime has not been selected."
}
```

## 有针对性的独立检查

CAD 网格导出或法线变更，按 [CAD 网格法线完成判据](appearance-meshy.md#cad-网格法线) 执行结构检查、Blender 重导入/保存重开与实际渲染；按交付消费者补充对应引擎证据。法线字段存在不等于着色正确，单个消费者通过也不自动代表其他消费者通过。

优先从来源重新读取矩阵、几何或映射，不仅比较构建器自己写出的两个摘要。对易错机制保留一个能揭示错误的对照：错轴/逆矩阵、缺依赖、堵孔凸包、开放夹爪或错误槽位。按照具体变更选取，不将整套历史测试强制施加给每次小改动。

几何容差依据 CAD 离散化、浮点序列化和任务间隙分别制定。不要把 float32 的正常舍入误差当缺件；也不要用宽松显示容差覆盖精密接触误差。修正判定逻辑时保留原记录，并说明是重分析还是新物理运行。

抓放任务至少看实际支撑、双侧或预期接触、物料连续身份、释放后承载、撤离和稳定窗口。孔槽检查结合原 CAD 射线或截面与引擎探针；单看包围盒/渲染外观不足以检验内腔。

## 坐标与 up-axis 门（入库/交付前必跑）

USD、OS graph 与 Blender 场景必须 Z 向上、米制、地面 Z=0。SolidWorks 的 glTF/GLB 按规范写 Y-up，
几何内容却常是 CAD 的 Z-up（脚轮共面于同一 Z）；Blender 等导入器会无条件做 Y-up→Z-up 转换，
直接把整站放倒。必须实测而不是假设；只改 `upAxis` 元数据不算通过。

| 检查 | 判据 |
|---|---|
| 阶段声明 | `upAxis=Z`、`metersPerUnit=1` |
| 整站朝向 | 包围盒最高维度在 Z，且与来源 CAD 的高度量级一致 |
| 落地 | 场景根抬高后 minZ≈0；单个资产的底面不低于其安装基准太多 |
| **地标朝向** | 全部脚轮/地脚共面于同一 Z（minZ 一致）；三色灯/上罩在最高 Z；脚轮共面于 Y 即已放倒 |
| 对照件 | 取门板/立柱等已知竖直件，确认其长边沿 Z |

任一失败即视为坐标未修正：重新从来源量测，调整导入器或根变换后重跑，不能只改元数据或注释。
修正方式：Blender 导入 GLB 后在根节点叠加 `Rx(-90°)` 抵消导入器转换，再以 `export_yup=False`
导出 USD。可用随包门禁 `scripts/verify_up_axis.py`（对每个 USD/包输出 upAxis、mpu、包围盒与最高轴）。

## 标准库与领域包交付

入库或工站设计任务的正式交付遵循 [asset-library-os.md](asset-library-os.md)：标准原生资产包、库索引/锁定修订，以及独立 OS 领域设备包和 graph。逐项核对：

- 按 [拆分边界及放行证据](decomposition-boundaries.md) 核对商业整机保持完整、自动化功能单元归属、全部耗材提取、定义复用和实例数量；物料身份与几何表示一一对应。
- 原生 manifest、BOM、USD 入口、声明的行为源码和引用闭包通过库校验；从空缓存按摘要取出后可消费。
- graph 的父子关系、局部/世界位姿和库位 frame 一致；Blender 保存后读回能还原布局。
- 领域包的实际 OS Catalog、动作类型、资源/Sites 和工作流往返检查通过；安装后所需 workspace 描述文件齐全。
- 第二空项目复用相同行为源码，允许构建器原样固化，实例身份与可变状态隔离；消费方不依赖发布者源仓。
- 对本次要求的动作运行并检查实际结果；过程 test double、USD 回放、OS 库存结算、规划碰撞和物理接触分别登记。

## 工程预览与证据布局

以下适合预览、局部处理或验收证据，不能代替标准库发布或 OS 领域包；按实际产物创建目录：

```text
delivery/
  station.usda                 默认整站入口
  families/                    独立对象与模型依赖
  materials/                   可替换外观和贴图
  scenes/                      任务配置与测试场景
  provenance/                  来源摘要、对象和坐标映射
  evidence/                    本次检查结果与必要缩略图
  RUN-REPORT.md                如何打开、结果与后续入口
  bundle.sha256.json           文件封装摘要
```

完整来源可外置保管；交付运行所需文件必须在包内或有明确的外部依赖合同。纹理和 USD 层使用相对引用。部署信息、私有标签和模型外传遵循本次交付对象及授权，避免把个人环境、凭据、缓存带入分享包。

## 自带工具

批量替换或补运动定义时，使用 [三组 P0 检查](p0-checks.md)，范围取自本轮清单/锁文件，拒绝空范围、重复项和漏项。包闭包通过、整站入口可移植、OS 领域包可安装分别报告；历史总装的旧依赖消失时，保留失败记录，不能跳过该实例后宣称全量通过。

原生库、转换/几何、来源和碰撞 profile 检查已随技能携带，见 [portable-tools.md](portable-tools.md)。按声明范围调用，保留实际输出；语义拆分与动作验收仍按本次来源/场景执行。

运行 `python scripts/bundle_check.py --help` 确认参数。此工具用于上述工程目录：seal 建立文件摘要，verify 检查缺失、篡改和额外文件；不能代替标准库的 manifest/BOM 校验，也不在已封包资产内增加文件。实际依赖检查使用该工具可选的 `--usd` 分支，需要当前 Python 可导入 `pxr`。文件名过滤会拒绝常见凭据、数据库和缓存；它不扫描文件内容，经过检查的证据日志和运行适配源码可以交付。OS 库存数据库与运行证据单独放在项目 runs 中，不作为可复用资产定义。

使用自己的环境路径替换下面的参数，先执行 help 确认当前版本：

```text
python <skill-dir>/scripts/bundle_check.py seal <delivery-directory>
python <skill-dir>/scripts/bundle_check.py verify <relocated-delivery-directory> --usd
```

将交付复制到另一临时路径后执行 verify；再用目标消费者实际重开。USD 检查应覆盖所有声明入口、引用/子层/payload/纹理及变体中的资源。对消费者才会加载的插件资源另做运行检查。缺少 `pxr` 时记录依赖检查未运行，不能依据文件摘要声称 USD 已重开。

Mac/Linux 上通过不代表 Windows 图形软件已测试。发布报告明确实际 OS、工具版本、测试范围和未执行项。

## 增量变化

纹理变化复验外观、依赖和部署显示；几何或坐标变化复验受影响的接触与机构；驱动、引擎、碰撞配置变化复验运行结果。保留未变资产的既有证据，但记录它们与新组合的适用关系。

## 官方参考

- [OpenUSD 共享实例与编辑限制](https://openusd.org/release/api/_usd__page__scenegraph_instancing.html)：共享可节省场景开销，instanceable 子树的逐实例覆盖受限；据编辑需求选择共享粒度。
- [PhysX 碰撞表示](https://nvidia-omniverse.github.io/PhysX/ovphysx/latest/simulation_setup/collision.html)：根据静态/动态和接触细节选择表示，运行时能力以已安装版本的小型探针为准。
