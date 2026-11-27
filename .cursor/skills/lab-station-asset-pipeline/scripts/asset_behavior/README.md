# 资产行为构建：OS 合同对照

构建器本体在 Uni-Lab-OS：`unilabos/package_manager/package_distribution/asset_behavior/`。

`conformance_os.py`：用 OS 自己的静态扫描器，逐个对照某个领域包里的 @device 类与生成的转发类，确认 OS
登记的动作、状态、init 参数、auto 方法、模型入口与库位完全一致。参数给出 OS 源码根、领域包源码根和导入包名，
不针对任何特定领域仓库。只读输入，报告写到 `--out`，转发文件只写临时目录。

## 机构行为类的辅助脚本

配合 [references/asset-behavior-mechanism.md](../../references/asset-behavior-mechanism.md)：

- `estimate_limits.py`：按资产几何给没有声明行程的平移关节估行程，给出连杆底面中心作为工具点候选（需要 OpenUSD）。
- `make_candidate.py`：引用已发布包的 USD 生成候选修订，叠加行为类、行程、库位，逐项记依据；不改封存包。
- `cartesian_pipettor_template.py`：直角移液机构的四个基本动作，补上 `TOOLS`、`Z_JOINTS` 和类名即可用。
- `make_demo_deck.py`：用 PLR 耗材定义生成演示资源图（需要 pylabrobot）。
- `examples/`：七个试跑通过的行为类，说明见参考页末尾的表。
