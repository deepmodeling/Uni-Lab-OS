# 资产交付（Asset Delivery）

## 职责

`asset_fetch` 把锁定的工程包拉进缓存并校验之后，按锁行的 `model_dir`/`model_subdir` 把包的
USD 交付闭包落到领域包项目的模型目录 `<model_dir>/usd/`，再在同一导入包的模型之间共享字节
相同的 PNG。资产库里每个修订都自洽、各带一份贴图；跨资产共享只在交付这一步做。

## 公开 Interface

- `delivery_files(package, profile)`：`web`（默认）取 `simready_usd/profiles/web` 轻量档，没有就回落
  完整空间入口；`spatial` 总取完整入口。
- `materialize_lock(rows, packages, project, profile=, share=)`：逐字节拷贝，然后 `share_textures`。
- `share_textures(project, import_package, roots)`：重复 PNG 收进
  `<导入包>/_shared_textures/<sha256>.png`，改写 USD 层里的贴图引用，删掉被收走的副本；
  没有任何层再引用的共享文件随之删除。清单 `_shared_textures/manifest.json` 登记参与的模型目录、
  被改写层的源摘要/新摘要、被收走副本的摘要。
- `check_materialized(rows, packages, project, profile=)`：核对副本仍来自锁定包（按清单认可共享）。

命令行：`unilab package assets fetch --lock … --cache … --project <领域包根> [--profile web|spatial]
[--no-share-textures] [--offline]`；`unilab package assets verify … --project <领域包根>` 额外核对落盘副本；
直接放在领域包里的本地模型用 `unilab package assets share --project <领域包根> --root <模型目录> …` 共享贴图。
两种维护方式的说明见资产管线技能 `references/model-sources.md`。

## 不变量

- 贴图字节不变：共享文件名即内容 SHA-256，模型读取接口（`package_catalog/material_models.py`）
  只按这个规则放行 `_shared_textures` 并复核摘要。
- USD 层只改贴图引用路径，几何、层级、关节、材质绑定不动。
- 多个锁分次落盘时，后一次连同清单里已登记的模型目录一起重新共享，结果与一次性落盘一致。

## 依赖方向

拷贝与核对只用标准库；改写 USD 层用 `pxr`（`share_textures` 内延迟导入）。不导入工作区运行时。

## 测试

`tests/package_manager/asset_delivery/`。
