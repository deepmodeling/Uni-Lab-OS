# 设备包的模型从哪来：资产库锁定 与 本地模型

读取时机：给领域设备包接显示模型、换资产修订、离线部署、前端加载慢，或者设备包不想依赖 OSS。

设备类（`@device` / `@resource`）的 `model.entry` 指向导入包里的一个 USD 文件，OS 启动后只按这个
路径读本地文件，经 `/api/v1/material-models/…` 给前端。**OS 运行时不连 OSS。** 模型文件怎么来到
这个路径，有两种维护方式，可以在同一个设备包里混用。

## 两种方式

| | 资产库锁定（推荐给要复用、要追溯的设备） | 本地模型（开发中、一次性、或不进资产库的设备） |
| --- | --- | --- |
| 仓库里放什么 | 资产锁 `unilab.asset-dependencies/1`，每行 `model_dir`（+可选 `model_subdir`）；模型目录 `<model_dir>/usd/` 进 `.gitignore` | USD 和贴图本身，直接提交在 `<model_dir>/usd/`（或任意 `model.entry` 指向的目录） |
| 模型来源 | 资产库的固定修订（`asset_id` + `package_digest`），OSS、本地镜像或本地工程包目录 | 开发者自己维护的文件 |
| 换模型 | 发新修订 → 改锁 → 重新落盘 | 直接改文件并提交 |
| 校验 | `assets verify --project` 逐文件核对落盘副本仍来自锁定包 | 无（自己负责） |
| 前端轻量档 | 包里的 `profiles/web`（见 [web-profile.md](web-profile.md)），落盘默认取它 | 自己准备轻量版本 |

## 资产库锁定：落盘与共享贴图

```
unilab package assets fetch --lock <锁> --cache <缓存> --project <设备包根> [--profile web|spatial] [--offline]
unilab package assets verify --lock <锁> --cache <缓存> --project <设备包根>
```

- **取包**（不强制联网）：锁行写了 `path` 的直接用本地工程包；其余行先按摘要在缓存里找，缓存里有就
  不连存储；缓存缺包才打开存储下载（默认 OSS，`--mirror` 用本地资产库镜像）。`--offline` 下缺包直接
  报错。凭据只从环境变量读，OSS SDK（`oss2`）只在真要下载时才需要。
- **本地工程包行**：`{"key": …, "path": "<工程包目录，相对锁文件或绝对路径>", "package_digest": …, "model_dir": …}`。
  给了 `package_digest` 就按原生规则校验；不给即“未固定的开发中包”，只适合开发期，发布前换成资产库
  固定修订。行为构建（`assets build`）只接受资产库固定包。
- **落盘**：按锁行把包的交付闭包逐字节拷到 `<model_dir>/usd/<model_subdir>`。`web`（默认）取
  `simready_usd/profiles/web` 轻量档，包里没有就回落完整空间入口；`spatial` 总取完整入口。
- **共享贴图**（默认开，`--no-share-textures` 关）：同一导入包里字节相同、出现不止一处的 PNG 收成
  `<导入包>/_shared_textures/<sha256>.png` 一份，改写 USD 层里的贴图引用，删掉被收走的副本。贴图字节
  不变（文件名就是内容摘要），USD 只改贴图引用路径，几何、层级、关节、材质绑定不动。
  前端按这个地址共享下载和显存，同一张贴图只下一次。`_shared_textures/` 也进 `.gitignore`。
- **分次落盘**：多个锁（例如设备显示模型锁和场景资产锁）分别落盘时，后一次会连同共享清单
  `_shared_textures/manifest.json` 里已登记的模型目录一起重新共享，结果与一次性落盘一致。
- **核对**：`verify --project` 按清单认可共享：被收走的 PNG 要在共享目录里有同摘要文件，被改写的层要
  由同一源文件改写而来且字节与清单一致。

跨资产共享只在落盘这一步做。资产库里每个修订都自洽、各带一份贴图，不引用别的包；贴图内容上的
处理（缩放、无损重编码、减面）在资产管线生成轻量档时做，结果发布成修订。

## 本地模型：直接放在设备包里

- 把 USD 和贴图放在 `model.entry` 所在目录（OS 只放行声明模型所在目录及其子目录，外加共享贴图目录）。
  贴图用相对路径引用，不要引用模型目录以外的文件。
- 多个本地模型有相同贴图时，同样可以共享：

  ```
  unilab package assets share --project <设备包根> --root <导入包>/…/models/<A>/usd --root <导入包>/…/models/<B>/usd
  ```

  这会改写已提交的 USD 层和删掉重复 PNG，跑完把 `_shared_textures/` 和改动一起提交（本地模型的共享
  目录不进 `.gitignore`；与锁定模型混用时，给 `_shared_textures/` 单独决定是否提交，不要一半提交一半忽略）。
- 本地模型以后要复用或追溯时，走资产管线入库（[asset-library-os.md](asset-library-os.md)），
  发布后把这个设备改成锁定方式。

## 前端加载慢时先查

1. 落盘的是不是轻量档：`<model_dir>/usd/` 里的根文件应来自 `profiles/web`（锁定方式）。
2. 贴图有没有共享：`<导入包>/_shared_textures/` 是否存在；同一张贴图是否还在多个模型目录各有一份。
3. 单件大模型：减面不够的资产另出轻量档修订（[web-profile.md](web-profile.md)）。
4. 前端用开发模式（vite dev）时请求经代理转发，比打包后的版本慢，测加载时间用打包版本。
