# Uni-Lab-OS 完整接口手册

本手册面向微前端、上游 Backend、设备驱动、脚本与 AI 工具接入者。
它描述**已经合入 dev 的实际接口**，而不是未实施的设计方案。

## 版本与范围

| 项目 | 本次基线 |
| --- | --- |
| 起点 | `99558a5a`，2026-08-10，首次在 dev 引入 Jazzy / Python 3.12 / NumPy 2（PR #304） |
| 终点 | `862ce87a38222ac2d51739f2f89e5daecd3cd907`，2026-09-14（PR #317），版本号 0.12.1 |
| 运行时 | Python 3.14（cp314）；ROS2 Jazzy / Humble；无 ROS 通信时使用 HostLink |
| HTTP | 当前离线导出 134 个路径、163 个操作；含两条 SSE 路由 |
| 额外通道 | runtime.v1 控制 WS、内部 HTTP 中继、HostLink TCP、MCP `/mcp` |
| 本次不纳入 | 工作区未提交的代码、独立前端内部组件 API、每一台硬件厂商的私有指令集 |

所有源码链接固定到该提交。文档审阅时使用独立 checkout，没有启动真实设备，
也没有读取或修改正在使用的实验室数据库。

## 阅读导航

```{toctree}
:maxdepth: 1

design.md
http.md
api-catalog.md
materials.md
workflows.md
realtime.md
python-cli-mcp.md
migration.md
reference-comparison.md
```

| 你要做什么 | 先读 |
| --- | --- |
| 先理解为什么这样划分架构 | [设计理念与系统边界](design.md) |
| 查某一条 HTTP 接口 | [完整 API 目录](api-catalog.md)，再读对应业务章节 |
| 做微前端 / 实验室地图 | [HTTP](http.md)、[物料](materials.md)、[实时通道](realtime.md)；不可连接设备控制 WS |
| 上传模板 / 运行工作流 / 处理报错 | [工作流与 Site 绑定](workflows.md)、[信封与状态码](http.md) |
| 自己实现上游调度 Backend | [runtime.v1 控制面与 Host HTTP 中继](realtime.md) |
| 写设备或 Workstation 驱动 | [Python 接入、装饰器与物料门面](python-cli-mcp.md) |
| 用脚本或 MCP 操作 | [Python、CLI 与 MCP 接入指南](python-cli-mcp.md) |
| 从旧 Jazzy 版本迁移 | [迁移与变更记录](migration.md) |
| 对照原飞书设备接入规范 | [旧规范与本版的对照](reference-comparison.md) |
| 查参数、模型或函数签名 | 下文的接口定义入口；业务约束仍需同时阅读对应章节 |

## 进程与唯一权威

默认启动的管理进程负责 Scheduler、Workflow、Registry、Materials，并看护一个 Host 子进程。
浏览器只连接管理进程的 `--port`。Host 子进程执行设备动作，监听 HostLink TCP 给 Slave，
**不监听 HTTP 端口**；它通过出站 HTTP 和一条 runtime.v1 WS 与权威通信。

| 层 | 职责 / 数据 |
| --- | --- |
| 微后端权威 | 工作流、任务准入、调度、动作/物料锁、库存预留、决策、注册表版本、物料位置与内容 |
| Host | 已准入动作执行、设备路由、HostLink、受管 Slave、遥测与执行历史；不自行重新排程 |
| Slave | 本进程驱动与物料投影；通过 HostLink 向 Host 请求物料权威读写 |
| 微前端 | 展示、填写/确认 Site 与物料、显式提交任务/干预；不替代物料权威或调度器 |

四库为 `runtime.db`、`materials.db`、`telemetry.db`、`history.db`。
分进程运行时 Host 自己的数据根在权威根的 `edge/` 下；浏览器应通过统一 API 访问，
不要按文件名猜测哪一个进程持有事实，也不要直接写 SQLite 表绕开版本和账本。

## 接口定义入口

本目录只维护 Markdown 说明，不放生成脚本、JSON 快照或自动导出的参考表。
生成物仅保存在本地用于核对，不纳入 Git，也不随文档站发布。

| 内容 | 查看方式 |
| --- | --- |
| 本版全部业务 HTTP 操作 | [人工维护的 API 全目录](api-catalog.md)，含请求模型、查询参数和返回约束 |
| 运行中 HTTP 路由与请求模型 | 管理地址下的 `/api/docs`、`/api/redoc`、`/api/openapi.json`；以当前部署实际挂载的路由为准 |
| 本版 HTTP handler 与响应处理 | [固定版本 API 源码](https://github.com/deepmodeling/Uni-Lab-OS/tree/862ce87a38222ac2d51739f2f89e5daecd3cd907/unilabos/server/api)；实际角色转发见 [HTTP 章节](http.md) |
| 协议字段、默认值与校验 | [协议模型](https://github.com/deepmodeling/Uni-Lab-OS/tree/862ce87a38222ac2d51739f2f89e5daecd3cd907/unilabos/protocol)、[规范资源对象](https://github.com/deepmodeling/Uni-Lab-OS/tree/862ce87a38222ac2d51739f2f89e5daecd3cd907/unilabos/resources/objects) |
| Python 客户端签名 | [客户端源码](https://github.com/deepmodeling/Uni-Lab-OS/tree/862ce87a38222ac2d51739f2f89e5daecd3cd907/unilabos/client)；使用边界见 [接入指南](python-cli-mcp.md) |
| CLI 参数 | 本机 `unilab --help` 及对应子命令的 `--help` |
| MCP 工具与 inputSchema | 当前 MCP 会话的 `tools/list`；本版白名单见 [工具目录](https://github.com/deepmodeling/Uni-Lab-OS/blob/862ce87a38222ac2d51739f2f89e5daecd3cd907/unilabos/server/mcp/catalog.py) |

OpenAPI **目前不是全部业务语义的完整定义**：大量 handler 返回普通字典；实际角色路由、
动态状态码、Mutation 的 payload 类型、SSE 与隐藏接口在人工章节补充。
不能把 `{}` 响应 schema 理解为“空对象”，也不能因为 endpoint 出现在全集里就认定当前进程可调用。

## 更新与校验

以要发布的**干净 checkout**为依据，在本地核对路由、模型、CLI 与 MCP 声明，再更新 Markdown。
离线导出应只使用临时数据库，不起服务、不拉设备包、不访问正在运行的微后端。
生成与校验工具、导出的 Schema 和参考表均保留在本地忽略目录，不写回本目录。
更新代码后同时人工核对业务章节，不能仅重新导出表格就认为契约审阅完成。

### 本次校验记录

2026-09-15，在该固定提交的独立 checkout、Python 3.12 Jazzy 环境重新执行：

- 8 份本地生成快照可重复生成并通过一致性检查；它们不属于发布文档。
- 人工 API 目录的 163 个 HTTP 操作、body 模型、query/header 参数名与本地契约逐项一致；另单列 4 个内部 HTTP 操作。
- 222 份模型 Schema（含不同导出视角）、137 个 MCP 工具 Schema 引用有效。
- 文档本地/固定源码链接、JSON/Python 示例语法和 CLI 示例参数已离线检查。
- 87 项相关回归通过（22.10 秒）：OpenAPI、步进、Site、物料 HTTP/SSE、日志、MCP。
- 本机 ROS `launch_testing` 与已装 pytest 存在插件接口冲突；测试进程设置
  `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1` 后运行通过，没有升级或卸载环境依赖。

未执行真实硬件动作、双浏览器 UI 验收或 Sphinx HTML 构建（当前环境未安装 Sphinx/MyST）。
示例的语法/参数校验不代表已经对真实设备执行成功；端到端部署仍按迁移章的验收清单核对。

既有材料教程、MCP 指南和 demo 验收文档仍可阅读；若旧文档与本手册不一致，
对本次版本以固定源码与这里列出的已知差异为准。
