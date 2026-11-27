# 前端轻量档（web profile）

前端（three.js USD 加载器）只用来看，不做物理或测量。大 CAD 资产（几百万到几千万三角面、大贴图）直接给前端会加载很慢，所以给资产另做一份"web 轻量档"，放在同一个包里，和空间入口并列。

## 什么时候做

- 包的入口 USD 三角面超过约 50 万，或贴图超过 1024 px，而且前端要显示它。
- 物理、碰撞、测量仍然用空间入口（`entrypoints.usd`），轻量档不参与。

## 做法（不改资产身份，只加修订）

1. 原包不动，按 [asset-library-os.md](asset-library-os.md) 的修订规则另建一个修订目录（例如 `__R193web`）。复制 `simready_usd`，根文件随目录改名。除此之外，空间入口的内容要逐字节不变。
2. 把 `simready_usd` 的层树原样复制到 `simready_usd/profiles/web/usd/`，然后只替换两样东西：
   - 网格：用 meshoptimizer `simplifyWithAttributes` 逐网格减面，按 GeomSubset 分组分别减。误差取各网格尺寸的相对值，默认 `1e-3`；少于 200 个三角面的网格不动。法线、UV 等顶点属性一起保留。
   - 贴图：长边缩到 1024 px，用 PIL LANCZOS。PNG 还要无损重编码：全不透明就去掉 alpha，灰度图存成单通道，保留 gAMA/cHRM/sRGB、EXIF 和 dpi；结果不比原来小就保留原字节。动画、16 位或浮点、带 ICC 或 HDR 元数据（cICP/cLLI/mDCV）的 PNG 不重编码，只缩放。各类处理的计数写进 `web.json` 报告的 `png_actions`。贴图压缩只在这里做，设备包落盘时照原样拷贝（只把模型之间字节相同的贴图收进共享目录、改引用，字节不变，见 [model-sources.md](model-sources.md)），这样锁里的摘要就能覆盖前端拿到的字节。
   - prim 层级和名字、UsdPhysics 关节和限位、xform 算子、材质绑定、GeomSubset 归属都**不改**。
3. 写入 `profiles/web/web.json`，内容包括生成参数、报告和检查结果，以及 `profiles/web/dependency_closure.json`。
4. 修改 `unilab.json`：
   - `entrypoints.web_profile = simready_usd/profiles/web/usd/<根文件>`
   - `parameters.web_profile = {source_library_path, source_package_digest, spatial_entry_unchanged: true, triangles_spatial, triangles_web}`
   - 第二项是血缘。消费方（比如站图生成器）靠它把锁从旧修订换到新修订，并沿用原来的变换。
5. 用资产库自己的原生封包器重新封包，包括 usd_closure、readiness 绑定和 manifest。

以上第 1–5 步由 `scripts/web_profile/make_web_profile_revision.py` 一次完成。核心减面在 `webprofile.py`，它会调用 `simplify.mjs`（node + meshoptimizer 1.1.1）。

```
python scripts/web_profile/make_web_profile_revision.py --library-tools <含 admit_reviewed_asset_batch.py 的目录> \
    --package "<库内相对路径>" [--package ...] --suffix __R193web --revision R193 --out <工作目录> \
    --meshopt-from <node_modules 目录>        # 或设环境变量 MESHOPT_FROM
```

需要 pxr、numpy、Pillow、node，以及 npm 包 meshoptimizer。贴图处理的回归测试：`python -m pytest scripts/web_profile/test_webprofile_png.py`。脚本不负责发布，它把每个修订的发布行写到 `<out>/built_assets.json`，交给库的发布器处理：先入库，然后不可变上传、独立冷下载核对 web 档闭包，最后提交索引。

## 放行门（任何一项不过，修订不写入）

- web 档的 prim 路径集合与原包相同，UsdPhysics 关节（类型和两端刚体）相同。
- 包围盒误差 ≤ max(1 mm, 2 × rel × 对角线)。
- 空间入口的几何事实（`geometry_facts`）不变。
- 两个依赖闭包都完整：空间入口的闭包在包内，web 档的闭包在 `profiles/web/usd` 内。
- 前端实测：用前端的 USD 加载器加载 web 档，和 pxr 的包围盒对比（通常在 2 mm 以内）。

实测参考：BSL-2 外框台面（含 36 个定位件）从 2319 万三角面减到 378 万，前端拉取从 191 MB 降到 43.6 MB，前端与 pxr 偏差 1.5 mm。

## 组合资产给前端用时的一个坑

组合资产（把几个现有层组装成一个包）不要在引用处覆写被引用 prim 自己的变换算子。比如在引用 `</Asset>` 的 prim 上另写 `xformOpOrder` 和变换，USD 会用引用处这一份，three.js 的加载器却会叠加两份。正确做法是：外面一层 Xform 写补偿变换 `own⁻¹ · rel`，里面一层只做纯引用。这样所有 USD 加载器得到的结果一致。
