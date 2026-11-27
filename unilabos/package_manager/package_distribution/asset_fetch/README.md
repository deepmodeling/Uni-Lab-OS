# 资产拉取（Asset Fetch）

## 职责

按领域包资产锁（`unilab.asset-dependencies/1`）里固定的 `asset_id` + `package_digest`，
从资产库存储（OSS `asset-library/` 或本地镜像）拉取 NVIDIA SimReady 工程包到缓存，并做
字节级完整性校验。它让"仓库里只放锁、实际资产按锁下载"成立；不转换 CAD/USD，不生成
模型或行为代码（行为固化见同层 `asset_behavior`）。

## 公开 Interface

`__init__.py` 导出：`fetch_lock(store, lock_path, cache, only=None)`、
`fetch_package(store, asset_id, package_digest, cache)`、`read_asset_lock(path)`、
`verify_package(root, expected_digest)`、`LibraryIndex`、`AssetStore` 协议与两个错误类型。
命令行：`unilab package assets fetch|verify`（见 `../../cli.py`）。

后续显式行为构建与绑定检查由同层 `asset_behavior` 的 `assets build|check-bindings` 提供；
fetch/verify 不自动注册 device/action，不导入下载脚本，也不推断同名动作之间的对应关系。

## 依赖方向

只用 Python 标准库。存储通过 `AssetStore.get(key)` 注入；OSS SDK 只出现在
`../adapters/asset_store.py`。不导入工作区运行时、驱动运行时或注册表。

## 不变量

- 摘要算法与资产库工具链（lab-station-asset-pipeline `toolkit/asset-library/native_package.py`
  与 `native_transfer.py`）逐项一致：`package_digest` = 包清单文件的 SHA-256。
- 先在 `.staging` 下完整下载并校验，通过才原子改名进缓存；缓存文件设为只读。
- 已有缓存先校验；损坏时报错并保留现场，绝不静默换成另一修订或覆盖。
- 索引里找不到固定版本即失败，要求显式迁移锁；不回退到"最新版"。
- 不强制联网：锁行写 `path` 的直接用本地工程包（给了 `package_digest` 就校验）；其余行先按清单摘要
  在缓存里找，缓存缺包才打开存储（`fetch_lock` 的 `store` 可以是返回存储的无参函数）；`store=None`
  即离线，缺包报错。命令行 `--offline`。
- 凭据只来自环境（`OSS_ACCESS_KEY_ID` / `OSS_ACCESS_KEY_SECRET`），不写入锁、回执或日志。

## 修改路由

- 包校验规则：`native_package.py`（与资产库工具链同步修改）。
- 下载、缓存与锁读取：`fetch.py`。
- OSS / 本地镜像存储：`../adapters/asset_store.py`。
- 测试：`tests/package_manager/asset_fetch/`。
