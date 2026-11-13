# Dev 包管理与 runtime 管理入口

本轮在 dev 迁移分支上统一入口，不引入 V1/main 的四层 `package_manager` 或独立 Workspace Host。V1 tag worktree 保留为只读对照。

## 包管理

`package inspect` 仍在本地做 AST/YAML 检查和归档。其余管理命令通过统一管理地址进入现有 `/api/v1/driver-packages`；backend 转发给唯一主 Host，Host 持有台账、源码缓存与安装操作。CLI 不直接修改另一份台账，也不在调用者的解释器里安装包体。

`package inspect` 的 AST 扫描范围与 `package install` / `--devices` 挂载一致：按 pyproject 的
`[tool.setuptools.packages.find] include`（以及 `src/` 布局）找到顶层 Python 包，模块路径相对导入根推导
（`<仓库>/<包>/x.py` → `<包>.x:类名`，不带仓库目录名）；扫描失败的文件会让 inspect 报错退出，而不是从
`resources.json` 里悄悄丢掉设备。不传 `--out` 时归档和清单写到包目录自己的 `dist/`（归档时排除）。

驱动包的 YAML 注册表在运行时（`--devices`）、`package inspect` 和 `package install` 台账（`package list` 的
`device_ids` / `loaded_device_ids`）三处按同一规则发现（`registry/package_layout.py:registry_roots_for_mount`）：
挂载目录与包根（含 `pyproject.toml`）下的 `registry/`、`unilabos_registry/`，以及 pyproject 声明的
`[tool.unilabos.registry].paths`（相对包根，与默认位置叠加而非替换）；每个注册表根下 `devices/`、`device_comms/`
递归读取。三处都把 YAML 设备与 `@device` 设备合并，同一 id 以 `@device` 为准。

```bash
# 本地静态检查，不需要后端或设备运行
unilab package inspect --path /absolute/path/to/driver

# 管理地址可在顶层或子命令后指定；省略时沿用会话/配置，最后回退本机 8002
unilab package list --address http://127.0.0.1:8002 --json
unilab package catalog --address http://127.0.0.1:8002
unilab package changes --address http://127.0.0.1:8002
unilab package install /absolute/path/on/host/driver --wait
unilab package install https://github.com/example/driver@v1.0 --disabled --wait
unilab package enable driver
unilab package disable driver
unilab package uninstall driver --wait
unilab package operations
unilab package operation <operation_id> --wait --timeout 300
```

安装来源沿用 dev 服务：GitHub 仓库、归档 URL、Host 本机源码目录。指定远端管理地址时，目录也是 **Host 上的路径**；CLI 不上传调用者的目录。建议使用绝对路径。

旧 CLI 的 `package install name==version`、对任意 pip/Git spec 安装包体和 `--no-inspect` 已撤除。普通 Python 环境依赖可使用环境自身的 pip/uv；驱动包的第三方依赖由 Host 的驱动包服务处理。`package inspect` 的扫描、外部 Registry 合同和归档格式保持不变。

安装/卸载默认返回服务端操作记录，`running` 仅代表已接受。`--wait` 有界等待完成，失败时退出码为 1；超时不会取消服务端操作，也不会重复提交。可用返回的 `operation_id` 继续查询。启停、安装和卸载后通过 `package changes` 查看具体需要重启的进程；原 `restart_required` 保留为兼容提示，不能据此把所有 slave 一起重启。

同一 Host 同时只接受一个包写操作；有变更在途时，其他安装、卸载或启停请求返回 HTTP 409，读台账和进度不受影响。完成后可重新提交。`--disabled` 决定新包的初始状态；重装保留原启停状态，后续用 `enable` / `disable` 显式修改。

升级先在候选源码上完成扫描，旧源码在新台账提交前保留可回滚备份；台账写入异常时，升级或卸载会恢复旧源码。源码包的随包图只从登记的 `package_root` 发现，兼容平铺、`src/` 和根目录本身是 Python 包的布局；同名旧 pip 分发不能覆盖或补回源码已删除的图。

| 层 | 所有者 | 职责 |
| --- | --- | --- |
| CLI | `app/cli/package.py` | 参数、静态检查、调用 SDK、展示操作结果 |
| 请求协议 | `protocol/runtime/driver_packages.py` | 安装与启停请求，供 API 和 SDK 共用 |
| 客户端 | `client/runtime/driver_packages.py` | HTTP、操作查询与等待，不自动重发写请求 |
| HTTP 路由 | `server/api/driver_packages.py`、`edge_proxy.py` | 统一管理入口、控制面鉴权与 Host 转发 |
| 包所有者 | `server/services/driver_packages.py` | 下载、依赖、AST 扫描、源码登记、启停、台账 |
| 发现与加载 | `registry/`、`app/community_packages.py`、`backend/runtime/driver_creator.py` | 沿用 dev 的扫描、社区引用解析、类加载 |
| 进程 | `app/backend_main.py`、`app/supervisor.py`、`server/services/device_processes.py` | 监督 Host 和多个 slave，包管理不再另设进程所有者 |

服务现有边界继续保留：依赖安装发生在 Host 解释器中，文件回滚不回滚 pip/uv 对共享环境的修改；操作历史在 Host 内存中，Host 重启后不能靠旧 operation ID 恢复进度。当前源码回滚覆盖进程内异常，不是断电/强杀恢复；包变更与进程重启仍需统一协调。具体复查结果见 [后续调整项](dev_package_runtime_audit.md)。

## 设备源码变化与重启提示

Host 内的 `server/services/driver_source_changes.py` 观察源码，不启动新守护进程。策略为 **先提示、手动确认后调用现有重启接口**；修改文件和查询提示都不会自动停止设备。

观察目录来自 Host 本次启动的挂载目录、包台账中的源码根，以及每个受管 slave 实际启动命令和工作目录的挂载目录。因此覆盖 `<working_dir>/driver_packages/...`（包括 `unilabos_data`）、原地登记的外部开发目录和 `--devices` 指定目录；不观察仓库的 `unilabos/devices`：厂商驱动都在外部驱动包里，该目录只剩随框架发布的内置代码，改动它与改框架一样需要整体重启。未加载、未登记的任意 data 文件不推断进程归属。

每 2 秒检查内容摘要，连续两次内容一致后发布，通常在保存后 2–4 秒提示。支持新增、修改、删除及同大小/同时间戳改写；观察 `.py/.pyi/.yaml/.yml/.toml/.json/.ini/.cfg/.xml/.xdl/.pyd/.so/.dll`，忽略日志、数据库、缓存、`.git`、虚拟环境和事务临时目录。源码读取失败显示 `scan_error`，不清除已有提示。包事务期间暂停文件通知，避免把临时移动和回滚当成稳定改动。

提示可以从日志、`unilab package changes`、`GET /api/v1/driver-packages/changes` 获取；包清单的 `source_changes` 和单个设备进程的 `restart_notices` 也返回它。安装完成结果中的提示是当时快照，当前状态以 changes 接口为准。正式前端尚未接入弹窗，应使用这些字段呈现确认内容：

- `target/process_id/name/pid`：具体 Host 或 slave。
- `changed_files`：新增、修改、删除的文件绝对路径；`changed_packages/reasons`：包启停、升级、卸载、共享依赖安装等原因。
- `message/affected_slaves`：Host 重启会停止当前本机托管 slave，仅 `auto_start=true` 自动恢复；单个 slave 重启不重启 Host 或其他 slave。
- `restart_request`：已有接口的请求描述，查询不会提交它。Host 使用安静点重启；现有 slave 重启接口立即停进程，调用前须确认设备任务已结束，`waits_for_idle=false` 明确这一点。

同一源码被 Host 注册表扫描并被 slave 加载时，两者都会提示；目前按挂载目录保守判断，不声称已精确分析 Python 动态导入。只给 slave 显式挂载且 Host 未加载的包只提示相应 slave。共享第三方依赖安装可能影响所有受管进程，安装失败也可能已改变环境，因此同样提示检查并重启。

提示一直保留到对应进程停止或真正开始下一次启动；读接口不会确认或清除它。slave 在 Popen 前取基线，所以子进程启动期间的编辑也能检测。Host 重启后重新建立基线，恢复的 slave 各自重新建立基线。这是源码版本提示，进程启动成功仍不等于设备已就绪。远程及手动启动的 slave 不在本机受管清单中，当前不会替它们推断重启范围。

此功能没有关闭包升级/卸载与 slave 启动、Host 重启之间的竞争窗口，也没有给运行中的进程保留不可变源码版本；这些协调仍是独立待办。

## Workspace Host 管理入口替换

启动继续使用 `unilab`；查询和操作使用 `unilab runtime`，连接现有 dev 管理端。没有独立 workspace daemon、session 发现文件或第二个主进程监督器，也不新增 `workspace` 目录或兼容守护进程。

```bash
unilab --graph graph.json --test_mode
unilab runtime status --address http://127.0.0.1:8002 --json
unilab runtime drain --wait --timeout 300
unilab runtime resume
unilab runtime restart
unilab runtime restart --cancel
unilab runtime reset
```

- `status` 聚合 health、readiness、restart 和 drain 的真实响应；离线时返回失败，不启动服务。
- `drain` 关闭新派发并等待在途动作；超时保持排空闸门和进程运行。`resume` 只解除显式 drain，不解除其他暂停原因。
- `restart` 固定请求 `mode=quiescent, scope=edge`，由 dev 安全重启主 Host，调度权威继续运行。backend 进程自身的重启仍由外部启动器或服务管理器负责。
- `reset` 默认只预览。实际执行要求显式传入预览获得的 `--confirmation-token` 和服务要求的 `--confirmation`，继续由已有 reset 服务验证、关闭资源并归档。

这替换的是管理入口和进程归属，不模拟 V1 的 `/v1/snapshot`、通用 operation/revision/eventCursor 协议。V1 的物料源布局、模板 worker、旧发布同步不属于这轮包管理和生命周期入口；既有待补项仍见 [Workspace Host 盘点](workspace_host_cleanup.md)，不可将此改动视为那些业务能力已经迁移。
