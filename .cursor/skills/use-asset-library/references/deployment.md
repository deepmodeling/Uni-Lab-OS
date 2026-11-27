# 部署与诊断

## 用 npx 安装技能

从 OS 仓库的集成分支安装本技能（需要该私有 GitLab 仓库的 SSH 读取权限）：

```sh
npx skills add "ssh://git@git.dp.tech/lab-os/uni-lab-os.git#integrate/peptide-v1.0.0" --skill use-asset-library --agent codex -y
```

默认安装到当前项目；需要用户级安装时加 `--global`，Cursor 用户把 `codex` 换成 `cursor`。以安装器报告的技能目录为准，进入该目录执行下文命令。安装包包含网页、桥接服务、Blender 插件和 OSS 下载工具，不包含资产模型和凭据；新电脑继续按 [OSS 初始化流程](oss.md) 配置并拉取目录。Node/npx 用于安装技能，服务运行使用 Python 3.11+。

离线已检出仓库时，也可执行 `npx skills add <OS仓库路径> --skill use-asset-library --agent codex -y --copy`。

## 文件与依赖

完整复制本技能目录，保持 `assets/runtime/tools/` 的相邻目录结构。准备 Python 3.11+。已有镜像按本页启动；没有镜像按 [OSS 初始化流程](oss.md) 从空目录拉取目录及所选模型。网页和本地包准备可使用标准库；OSS 同步/下载时在宿主 Python 中安装 `assets/requirements-oss.txt`，Blender 不需要安装 oss2。

镜像至少应有根 `library.json`、`统一目录/` 的索引/条目/分类/厂家资料。离线插入还需要所选包的完整文件与相对引用依赖。只复制 USD 主文件不足以搬迁资产。封面使用的相对图片也要随镜像提供。

在技能目录运行（路径按目标机器替换）：

```powershell
python scripts/manage.py verify-bundle
python scripts/manage.py configure --library-root D:/Asset_library --output C:/Users/DP/AppData/Local/UniLab/AssetLibrarySkill --state-root D:/UniLab_cache/AssetBrowser
python scripts/manage.py start --config-dir C:/Users/DP/AppData/Local/UniLab/AssetLibrarySkill
python scripts/manage.py check --config-dir C:/Users/DP/AppData/Local/UniLab/AssetLibrarySkill
```

`configure` 默认本地模式，需要现存镜像；带 `--oss-config` 可使用空镜像目录，随后先运行 `bootstrap`。它不自动安装插件，也不覆盖已有配置。`start` 在后台隐藏启动缺少的服务；已运行且库根匹配时复用，不停止其他进程。配置目录内保存日志和本次启动的 PID。需要在终端观察日志时使用 `serve square` 或 `serve bridge` 加 `--config-dir`，Ctrl+C 结束该前台服务。

默认端口 8766/8765，可在 `configure` 时用 `--square-port`、`--bridge-port` 修改。随后同步修改插件偏好；生成的 runtime 已包含相应自动启动配置。部署目录移动后重新生成本机配置。

## OSS 可选配置

使用用户已有的无密钥 OSS 配置或本项目随包示例，传给 `configure --oss-config <path>`；字段为 `endpoint`、`region`、`bucket`、以 `/` 结尾的 `prefix` 和 `layout`。新电脑的命令、读取权限和凭据设置见 [OSS 初始化流程](oss.md)。

“同步 OSS 目录”更新目录，不等于下载全库；单资产插入触发按需准备。`sync_full` 是全量下载，应只在用户要求完整镜像时执行。同步不会把旧项目固定版本静默改成最新版本；退役摘要拦截时，先核对远端目录与本地发布状态。

本地模式设置 `local_candidates=true`，同步按钮只刷新本地；缺失包需要另行配置 OSS 后才能下载。技能不包含凭据，不会主动上传或删除 OSS 对象。

## 诊断路径

| 现象 | 下一步 |
|---|---|
| 端口已有服务但库根不同 | 用 `check` 查看两端根路径，选空闲端口；保留用户正在使用的服务 |
| 网页能开，Blender 插入失败 | 先查 8765 与包资格，不只查 8766 |
| 有产品信息、没有插入按钮 | 查看是否只有公共登记、是否有固定工程引用 |
| 筛选后空白 | 清除条件后逐项恢复；核对实际场景绑定与模型状态 |
| 模型损坏/摘要不符 | 记录固定 ID、摘要和错误文件，使用资产管线修复或按已授权来源重新准备 |
| 服务启动失败 | 看配置目录的 `square.log` / `bridge.log`；检查镜像索引和随包依赖摘要 |
| 远端访问失败 | 检查已授权 OSS 配置和凭据环境；输出日志时避开签名 URL/密钥 |

网页仅绑定本机，未提供跨机器授权、HTTPS 或多用户服务。跨电脑交接是在目标电脑部署服务与镜像，不把本机 127.0.0.1 地址当作共享服务器。
