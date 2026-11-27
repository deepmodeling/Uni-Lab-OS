# Z-up 交付与 glTF / SolidWorks 轴向

读取时机：任何 CAD→USD、GLB→USD、整站场景组装、入库或验收之前。此页是 up-axis 的唯一依据；
其他页只引用，不重复另立规则。

## 1. 交付契约

| 产物 | 约定 |
|---|---|
| 资产 USD（模块/设备/耗材） | 米制、`upAxis=Z`、`metersPerUnit=1`、资产自身原点 |
| 整站引用层 / OS graph / `scene_assembly` | Z 向上；图位置用 mm，节点只做平移（资产已带自身朝向） |
| Blender 场景 | Z 向上；地面 Z=0；地标件方向正确 |
| GLB | 只是转换中间件。按 glTF 规范它是 Y-up；**SolidWorks 写出的几何内容却常是 CAD 的 Z-up** |

## 2. 为什么反复踩

1. SolidWorks 的 glTF 导出把 CAD 的 XYZ 原样写进 `POSITION`（Z 仍是上），但 glTF 规范要求 Y-up，
   文件本身不带 up-axis 字段，任何按规范工作的导入器都会把它当 Y-up。
2. Blender 的 glTF 导入器**无条件**执行基变换 `(x,y,z)→(x,−z,y)`：Z-up 内容被转成"Y 向上"，
   整站仰面躺倒，三色灯/立式件变成横躺。
3. 如果再把 Blender 的坐标直接 `export_yup=False` 写成 USD，`upAxis` 元数据是 `Z`，
   但几何是躺的——**元数据检查全部通过，内容却是错的**。
4. 只看包围盒也容易漏：机器接近方箱时，`3.94 × 2.34 × 1.95` 与 `3.94 × 1.95 × 2.34` 都能解释，
   必须用**已知地标**（脚轮、三色灯、上罩、门板）判方向。

## 3. 判定（按顺序做，三项都要过）

1. **源侧地标**：在 GLB 的原始坐标（不做任何转换）里计算每个节点的世界包围盒。
   - 全部 `脚轮/脚杯/地脚` 应共面于同一 **Z**（minZ 一致，容差 5 mm）；
   - `三色灯/上罩/顶部` 应在最高 Z；
   - 若脚轮共面于 Y 或最高件落在 Y 上 → 该文件的内容是 Y-up，转换时必须补一次轴交换。
2. **原始 glTF 包围盒**：直接对节点变换求世界包围盒，与 Blender 导入后的包围盒比较。
   若两者正好是 Y/Z 互换，说明导入器的基变换没有被抵消。
3. **对照已知竖直件**：门板、立柱、Z 轴模组的长边应沿 Z；脚轮应在最低 Z。

## 4. 正确转换

### 4.1 用随包工具（首选）

`scripts/toolkit/asset-library/historical_blender.py` 的 `--native-cad-frame` 已实现完整抵消：

```python
basis = Matrix(((1,0,0,0),(0,0,-1,0),(0,1,0,0),(0,0,0,1)))   # glTF -> Blender 基
correction = basis.inverted()
# 1) 把 correction 烘进网格数据；2) 用 correction @ matrix_world @ basis 共轭节点变换
```

调用方式见 `asset_tools.py convert --family glb|solidworks ...`；SolidWorks 原生网格走
`solidworks_native.py`（自带 `--dependency-root` 与 native 坐标系）。

### 4.2 自己写 Blender 脚本时

导入后立刻把根节点摆正（等价于全局抵消），再导出：

```python
bpy.ops.import_scene.gltf(filepath=glb)
root = bpy.data.objects.get("SPE_3D")          # 顶层装配节点
root.matrix_world = Matrix.Rotation(math.radians(-90.0), 4, "X") @ root.matrix_world
bpy.context.view_layer.update()
bpy.ops.wm.usd_export(filepath=..., export_yup=False, ...)   # 关闭二次转换
```

要点：

- `export_yup=False` 是"保持 Blender 的 Z-up"；**不要再手工套一次 `rotateX(90)`**。
- 若资产有多个顶层根，对每个根做同一变换；不要逐个零件转。
- 中文对象名导出成 USD prim 会被 sanitize（`Mesh_###`），需要逐个零件的检查请在 GLB 侧做，
  或在导出前给 prim 写 `lab:sourceName`。

## 5. 验收门（入库/交付前必跑）

```text
python scripts/verify_up_axis.py <usd-or-dir> ... \
    --glb work/station.glb --landmark 脚轮 --landmark 脚杯 --top 三色灯 --require-floor
```

`--top` 用真正最高的件（三色灯、AGV 顶门）；`上罩` 一类侧上罩不是最高件，不要拿它当判据。

脚本输出每个 USD 的 `upAxis`、`metersPerUnit`、网格数、包围盒与最高轴，并做：

- `upAxis=Z`、`metersPerUnit=1`、网格数 > 0；
- `--glb`：原始 glTF 包围盒与 USD 包围盒对齐；若呈 Y/Z 互换 → **判失败**（Y-up 放倒）；
- `--landmark`：这些件的世界包围盒必须共面于 Z；`--top`：这些件必须在最高 Z；
- `--require-floor`：场景根 `minZ≈0`。

任一项失败都不得入库、不得作为验收场景交付。

**门禁自测**：拿正常场景做一个"故意放倒"的引用层（根节点 `rotateX(90)`），门禁必须报
`yz_swapped` 并以退出码 1 结束；若它通过了，说明 `--glb` 或地标参数没生效，先修门禁再谈交付。

## 6. 真实事故（2026-11-09，SPE 固相萃取平台）

| 项 | 现象 |
|---|---|
| 源 | `SPE_3D.x_t` → SolidWorks 批量另存 → `SPE_3D.SLDASM` → GLB（872 MB） |
| 错误 | Blender 导入 GLB 后直接导出 USD；`upAxis=Z` 但整站仰倒 |
| 证据 | 8 个 `重型脚轮 怡和达-CPM66-D100-BE` 原始 glTF `z=−0.66` 共面（Z-up 正确）；转换后变成 `Y=0.66` 共面（放倒） |
| 表现 | 三色灯横躺、脚轮在竖直面上、模块高度被压扁（耗材堆栈 0.15 m 而非 0.71 m） |
| 修正 | 根节点叠加 `Rx(−90°)` 抵消；重导 12 模块 + 整站 + 库包 |
| 修正后 | 3.937 × 1.950 × 2.338 m，地面 Z=0，场景根抬升 732 mm，三角形数不变 |

教训：`upAxis=Z` 只是声明；**必须用脚轮/三色灯这类地标证明内容真的 Z 向上**。

## 7. 排查清单

1. 打开 GLB 的 JSON，确认根节点与顶层装配有没有 `rotation`；记下矩阵/四元数。
2. 用原始坐标算 3–5 个地标件的世界中心：脚轮 minZ 是否一致？三色灯是否最高？
3. 比较原始 glTF 包围盒与转换后包围盒：尺寸是否 Y/Z 互换？
4. USD 里 `upAxis`、`metersPerUnit`、`UsdGeom.BBoxCache` 三者一起看，不只看元数据。
5. 场景里地面是否 Z=0、最高件是否沿 Z、模块高度是否合理（堆栈/立柱类不应压扁）。
6. 把上述数字写进交付报告；只写"已 Z-up"没有数字不算验收。
