# 从 OSS 开始使用

## 新电脑最短流程

没有 OSS 权限时，按 SKILL.md 的联系人规则提示用户申请发布目录与模型前缀的只读 GET 权限及非密钥连接配置。安装技能不授予 OSS 权限，也不应要求用户删除已有本地库。密钥由用户通过本机凭据管理或环境变量配置，不要粘贴进聊天或网页。

`python scripts/manage.py doctor` 在没有配置时返回诊断；助手按 SKILL.md 给出联系人提示。指定 `--config-dir` 可检查配置与凭据是否存在；加 `--check-oss` 才会实际读取工程、统一目录两个入口，区分权限拒绝、对象不存在和连接失败。只验证入口成功不代表全库所有模型都具有权限。

安装 Python 3.11+，解压完整技能包，准备一个空镜像目录，例如 `D:/Asset_library`。在技能目录打开 PowerShell。`assets/oss.example.json` 提供本项目已使用的非密钥存储位置；换其他 Bucket 时修改副本，保持 `layout=nvidia-usd/1`。

```powershell
python -m pip install -r assets/requirements-oss.txt
python scripts/manage.py verify-bundle
# 先通过目标机器已有的凭据管理方式设置 OSS_ACCESS_KEY_ID / OSS_ACCESS_KEY_SECRET。
# 配置内只复制 endpoint、region、bucket、prefix、layout，不复制密钥。
python scripts/manage.py configure --library-root D:/Asset_library --output C:/UniLab/AssetLibraryClient --state-root D:/UniLab_cache/AssetBrowser --oss-config assets/oss.example.json
python scripts/manage.py doctor --config-dir C:/UniLab/AssetLibraryClient --check-oss
python scripts/manage.py bootstrap --config-dir C:/UniLab/AssetLibraryClient
python scripts/manage.py start --config-dir C:/UniLab/AssetLibraryClient
python scripts/manage.py check --config-dir C:/UniLab/AssetLibraryClient
```

然后打开 `http://127.0.0.1:8766/`。首次同步下载工程索引、统一目录、公共条目、分类/场景/厂家字典及工程缩略图；不下载 USD、CAD 等模型主体。只想快速检查目录时使用 `bootstrap --skip-previews`，此时部分封面暂不可用，后续不带该参数再运行会补齐。公共条目中不受发布元数据覆盖的来源图片不保证可用。

下载在独立缓存目录暂存，已验证内容可复用；断网后重复 `bootstrap`。内容通过摘要核验后才切换索引。两个云端索引不属于同一快照时拒绝激活，等待发布完成再重试。已有镜像上同步会拒绝改写被本地修改的目录文件、拒绝恢复本地已退役包；这不是删除本地库重新下载的理由。

凭据只需对应前缀的对象 GET 权限；新版消费传输不要求列 Bucket、读版本控制或写权限。临时凭据除 AK/SK 外还需设置 `OSS_SESSION_TOKEN`。进程不会把凭据通过 HTTP 发给 Blender，也不写入技能 ZIP。

若不希望每次新开终端都重新设置长期 AK/SK，可在已安全设置环境变量的 Windows 终端中执行：

```powershell
python assets/runtime/tools/asset-browser/service.py --save-credentials
```

它使用当前 Windows 用户的 DPAPI 保存 AK/SK，不能跨电脑复制解密。短期 token 不由该保存流程持久化；继续通过环境变量提供。其他系统使用环境变量。密钥不要粘贴到交接文档或模型里。

## 只下载选中的模型

方式一：按 [Blender 指南](blender.md) 安装 ZIP，刷新目录后选择模型。插入操作自动触发桥接服务的 `prepare` 任务，下载完整固定包并校验 BOM、文件摘要和主入口，再插入到 3D 光标。再次使用有效本地包不再下载。

方式二：在网页选择有 3D 模型的条目，取得公共 UUID，然后运行：

```powershell
python scripts/manage.py prepare --config-dir C:/UniLab/AssetLibraryClient --entry-id <公共条目UUID>
# 已知工程 catalog_id 时可使用 --catalog-id，二者选一。
```

成功结果包含 `package_root`、固定 `target` 与 `downloaded`，并写入配置目录的 `last-prepare.json`。纯产品登记会明确报“无映射工程模型”。固定版本不在云端时会失败，不自动替换为相近型号或其他修订。

网页本身保持只读；它用于检索和取得引用，模型准备由上述命令或 Blender 插入触发。完整包可能包含组件 USD、贴图和行为文件，这是选中模型的依赖内容，不是全库下载。

## 日常更新及交接边界

使用 `bootstrap` 更新目录；新版配置下 Blender 的“同步 OSS 目录”也走相同的公共/工程目录同步。`sync_full` 仍表示全库模型下载，只有明确要求完整离线镜像时才使用。

安装插件后可把配置目录的 `runtime.json` 复制到已安装插件目录，使插件能自动启动两项本机服务。凭据需在服务启动时可用，后台服务不会继承未来新终端才设置的环境变量。

技能包包含消费代码，不包含本机未发布的资产，也不会上传目录。以 `last-bootstrap.json` 中的条目数与 `snapshot_sha256` 记录这台电脑实际获取的发布快照。
