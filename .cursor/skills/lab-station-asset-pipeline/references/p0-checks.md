# 三组 P0 检查合同

读取时机：批量确认耗材对应、替换工站模型、为可动资产补关节/行为。选择本次变更相关的组，不把三组强制施加给纯浏览任务。具体厂家和批次见 [station-cases.md](station-cases.md)。

## 入口与结果

```text
python <skill>/scripts/asset_tools.py run check-identity --input identity.json --output identity-report.json
python <skill>/scripts/asset_tools.py run check-replacement --input replacement.json --output replacement-report.json
python <skill>/scripts/asset_tools.py run check-motion --input motion.json --output motion-report.json --execute-behavior
```

输入是 UTF-8 JSON；相对路径以输入文件目录为基准，包内路径以包根为基准。每份输入必须给非空、无重复的 `expected_ids`，并与实际条目 ID 集合相同。范围应由采购清单、资产锁或发布清单独立确定；从待查列表自己复制出的范围不能证明业务覆盖。报告保留输入、工具摘要和逐项结果。退出码 0=通过，1=失败，2=资料不全/未执行；不能把“脚本正常结束”当作验收通过。

三个工具只写报告，不保存 USD 或修改源包。运动入口会执行受信任的 Python 行为代码，`--execute-behavior` 是显式开关，不是安全沙箱；外来代码先审查。运行时禁止写字节码缓存，缓存不应污染封包。

## 1. 身份映射与参数依据

Schema：`asset-identity-evidence/1`。字段：

- `entities`: `id / system / key`，分别定义采购、厂家、模板、业务名称及资产身份。
- `sources`: `id / kind / path / sha256`；kind 为 manufacturer、procurement、database、cad、photo、measurement、user_confirmation、derived 或 catalog。URL 可保留为附加信息，但核验需要固定本地快照。
- `claims`: `id / subject / status / evidence`。字段断言附 `field / value`；数值再附 `numeric:true / unit`。关系断言附 `relation / target`，relation 为 `same_product / selected_for / geometry_reference / compatible_with`。
- `evidence`: 每项为 `source / scope / locator`。scope 为 identity、specification、geometry、selection、compatibility 或 record；可用 `pointer / equals` 直接读回 JSON 快照中的指定值。

`status` 为 confirmed、candidate、unknown、not_applicable。后三者必须有 `reason`；candidate/unknown 输出 incomplete，不自动升为 confirmed。照片数值需 `measurement:{method,reference,plane,uncertainty}`，误差为字段单位下的正数。同款身份不能仅凭照片、几何参考或选用指令确认。

通过只证明来源字节、定位声明与结构一致；扫描件文字、三视图语义、厂家法律实体及图示是否真正支持结论仍需人工/视觉核对。`catalog` 的字段读回只证明目录当前这样记载，不是独立原厂证明。

## 2. 替换不变量与引用闭包

Schema：`asset-replacement-checks/1`，条目放 `checks`；每项包含 `id / kind`。

| kind | 输入 | 检查边界 |
|---|---|---|
| package | `root / entrypoint / native_digest`；非原生包可用完整 `files` 摘要清单 | manifest/BOM、丢失或额外文件、实际依赖、绝对/包外引用；默认 composed 状态 |
| replacement | `before:{usd,prim} / after:{usd,prim} / tolerance_m / matrix_tolerance`，可附 `frames:[{before,after}]` | 米制、同轴向、子树可解析、根/指定 frame 世界矩阵、实际世界面集合一一对应 |
| json_invariants | `before / after / allowed_changes`（精确 JSON pointer 数组） | 仅白名单路径可变，其余含嵌套值保持相同；未使用白名单也拒绝 |

几何比较适用于预期不改视觉几何的替换，如补关节或换相对引用。它不比较材质、UV、法线，不用于批准形状重建；重建应用明确几何目标和独立误差合同。面对应保留重复数量与顶点顺序，不自动认可重新网格化。所有需要保护的支撑 frame、取放 frame 由输入显式列出；未列 frame 不等于验证。

依赖检查不遍历未选中的所有 variant 组合，也不发现 Python 运行时动态读取的资源；需要这些状态时分别生成输入或另做运行验证。原始目录多出缓存时先记录失败，再在新目录按已封存 BOM 物化并复验，不能删除源现场后覆盖失败证据。历史基线缺失时只允许恢复可验证的原固定版本，不得以当前资产冒充替换前状态。

## 3. 关节与行为一致性

Schema：`asset-motion-checks/1`，条目放 `assets`：

```json
{
  "id":"device-a",
  "package_root":"packages/device-a",
  "entrypoint":"simready_usd/device.usdc",
  "usd_sha256":"<实际 SHA256>",
  "expected_joints":["MotionJoints/lift"],
  "sites":[{"path":"MovingBody/Sites/place","owner_joint":"MotionJoints/lift"}],
  "behavior":{"module":"behavior/motion.py","factory":"Motion",
    "files":[{"path":"behavior/motion.py","sha256":"<实际 SHA256>"}]}
}
```

行为协议为 `factory(stage, root).set_positions({相对关节路径: SI值})` 与 `reset()`；直线用米、转动用弧度，验证器独立换算 USD 度制限位。现有行为可写薄适配器，但应调用真实行为实现，不能另造只为通过验收的运动实现。

当前仅覆盖显式 body0/body1 的 revolute/prismatic 运动学关节；solver 必须关闭，零位须在非零范围内，一子刚体一个控制关节。固定、球形、mimic、物理求解路线需相应适配及独立检查，不因不支持而删除其定义。

检查包括：关节集合与范围一致、局部关节帧、限位和单位、实际非零运动、边界/越界/NaN/inf/未知名称的拒绝及原子性、组合姿态与复位、库位实际随动、改名且有非零平移旋转的双实例互不影响、源码摘要不变。观测取 USD 实际世界矩阵，预期取 USD 关节帧，不依赖行为自己返回的状态快照。未传执行开关仅做静态检查，结果为 incomplete。

当前 `check-motion` 的 Sites 检查只验证存在与随动，`check-replacement` 的 frame 检查只验证声明的变换不变量；两者均未自动测量库位边界或判定显示体尺寸正确。库位尺寸及消费读回须另按 [库位几何与尺寸验收](asset-library-os.md#库位几何与尺寸验收) 留证，不能用上述脚本通过替代。

通过不证明接触、自然携带、加热或振荡工艺效果、硬件响应、Isaac/网页前端绑定。消费者实际加载与控制仍按 [validation-delivery.md](validation-delivery.md) 分项验收。
