# 本机 MCP 接入

微后端在同一个管理端口提供 Streamable HTTP `POST /mcp`（无状态、JSON 响应；`GET /mcp` 回 405，不开独立 SSE 流），
实现位于 `unilabos.server.mcp`。不需要额外运行一个 MCP 进程，也不需要 ROS 2。

详细操作见 [MCP Guide](mcp_guide.md)，
覆盖工具发现、工作流建图/执行、异常、物料、锁、日志与可复用 prompt。
`protocol_guide` 工具和 `unilab://protocol/guide` 资源提供运行时简要约定。
本文保留部署、实现边界和 demo 验收入口。

## 启动与连接

安装运行依赖后按正常方式启动，例如：

```powershell
python -m pip install -r unilabos/utils/requirements.txt
python -m unilabos.app.main --disable_browser --port 8003
```

MCP 地址为 `http://127.0.0.1:8003/mcp`。默认双进程形态中，MCP 在调度权威端口接收请求；
设备进程/日志等 Host 路由仍经原控制面代理转发。没有新的设备通信协议，不改 legacy。

Codex 可添加：

```toml
[mcp_servers.unilab]
url = "http://127.0.0.1:8003/mcp"
startup_timeout_sec = 60
tool_timeout_sec = 60
```

配置方法见 [Codex MCP 文档](https://developers.openai.com/codex/mcp)。
使用本机 Codex 现有登录，不把 Codex 的账号凭据发给微后端，也不在项目中保存认证文件。

## 137 个工具

131 个公开业务操作与 protocol 操作 ID 一一对应：把 ID 中的 `.` 和 `-` 换成 `_` 即为工具名。
白名单在 `server/mcp/catalog.py`；请求 schema 从微后端 OpenAPI 动态生成。

| 范围 | 能力 |
| --- | --- |
| 系统与运行态 | 健康、HostLink peers、Host/Slave 日志、调度资源、会话、endpoint、job/command 查询 |
| 工作流 | 模板绑定、新建/保存图、整图/单点提交、逐步/自动切换、attempt/结果/反馈、人工确认、authoring |
| 注册表 | 生效/挂起条目、版本、影响范围、升级、还原、工作流模板 |
| 物料与库存 | 模板、实例化、物料树、位置、内容物、移动/跨设备移交、拓扑边、批次入库、预留与账本查询、按品类的仓储视图 |
| 设备图 | Graph Authority、实时拓扑 |
| 遥测与历史 | 事件、来源游标、历史 payload 查询 |
| 异常决策 | 状态事件、动作异常、用户授权的中止/重试/人工替换 |
| 驱动包与进程 | 包发现/安装/启停/卸载、随包图、受管设备进程管理 |
| 数据诊断 | 四库状态和单表只读分页浏览，不提供 SQL 执行 |

另有 6 个辅助工具：

- `protocol_guide`：先读接入约定、调度/物料/异常语义。
- `protocol_search`：按关键词或域检索操作。
- `protocol_inspect`：读取完整请求 schema 与对应 HTTP 定义。
- `protocol_result_read`：按 JSON Pointer、offset/limit 读取大型响应。
- `protocol_wait_task`：有界等待终态或人工待办，最多 20 秒，不自行作决策。
- `protocol_batch`：并发调用最多 16 个公开业务操作，例如同时提交锁竞争任务。

只读资源为 `unilab://protocol/guide` 与 `unilab://protocol/operations`。

## 参数、返回和业务边界

每个业务工具只接受公开操作对应的参数：

```json
{
  "path": {"workflow_uuid": "从工作流创建结果读取"},
  "body": {"revision": 1, "nodes": [], "edges": []}
}
```

查询参数在 `query` 中，字段定义与原 HTTP API 相同。物料 mutation 的 payload
另外引用已有 `unilabos.protocol.materials` 模型，按现有 Materials 客户端方式补齐默认值、
绑定幂等信封。不会生成业务 UUID 的替身或直接修改数据库。

结果保留 `http_status` 与原始 `body`。HTTP 错误或业务 `code != 0` 标记 MCP `isError`；
查询到任务 `failed` 是真实业务数据，不会伪装成传输失败或成功执行。
大响应超过 40,000 字符时返回 `result_id`、结构 outline 和显式截断提示，缓存最多
32 项、16,000,000 字符、10 分钟。沿 JSON Pointer 继续读取，勿猜数组下标。
缓存过期时可以重新执行读操作，不能盲目重发写操作。

`protocol_batch` **不是事务**：整批参数先校验，每个 HTTP 操作独立提交；某项失败不回滚
其他项。工具不自动重试。设备动作必须经 `workflow_task_create` 交给调度执行。

现有实时变化仍使用 SSE 通知加 HTTP 拉取：`/api/v1/events` 与
`/api/v1/materials/events`。无限 SSE 流不作为一次 MCP 工具结果返回；
单个任务的运行事件由 `workflow_task_events`（`/api/v1/workflow-tasks/{task_uuid}/events`）
按 `after_sequence` 游标分页读取。

## 安全

该入口面向本机可信客户端，**有写权限，不是只读接口**。默认同时检查 TCP 对端为 loopback、
Host 为 loopback/localhost，并拒绝带 Origin 的浏览器请求，防止网页或 DNS rebinding
借 MCP 操作设备。没有远端无认证接入或任意 URL/文件/shell 工具。

正常操作入口包括执行、删除、重置、包安装和进程管理；调用这些工具必须得到对应用户授权。
MCP annotations 是给客户端的提示，不代替用户授权或业务鉴权。Codex 的 `-s read-only`
仅约束本地文件操作，**不会把 MCP 写工具变成只读**。

30 个内部控制面操作不在白名单中：不得伪造执行状态/反馈、创建后台 command、消费 outbox、
写物料快照或代调度修改预留。全量重置仍要求原 HTTP API 的确认令牌。

## 自然语言 demo 验收

```powershell
python -m tests.e2e.run_mcp_demos --demo workstation_demo --output .whalent_tmp/mcp-luna/my-run
```

不传 `--demo` 时依次运行 7 个 demo；可重复指定多个。需要本机已有同级 demo checkout，
夹具不会自动克隆或安装包。每个 demo 使用新四库、新端口、显式机器名，以及真实默认
“调度权威 + Host + 可选 Slave”拓扑。验证不接入用户现有的 8003 服务。

夹具启动本地 Codex `gpt-5.6-luna`，`model_reasoning_effort="max"`，禁用 shell/web，
忽略用户工具配置但复用原有登录，只接入该隔离实例的 MCP。
模型和推理档位见 [GPT-5.6 Luna 文档](https://developers.openai.com/api/docs/models/gpt-5.6-luna)。
该测试会消耗当前 Codex 账号额度，不自动进入 pytest/CI。

给模型的是当前 demo 的自然语言任务与 README，而非预先生成的 tool-call 脚本。
夹具只启动环境、读取证据和断言；工作流实例化、保存、提交、批次并发和错误决策必须出现在
Codex 的 MCP 调用记录中。未授权的错误决策不能由模型自行批准。

每轮保留：`metadata.json`、`codex/prompt.txt`、`codex/command.json`、`codex/events.jsonl`、
`codex/answer.md`、`runtime/*.log`、`observations.jsonl`、`evidence.json`。
验收核对任务与 attempt 历史、具体设备结果、锁 waiting/blockers、库存余额和物料权威树；
不以模型自报完成代替这些证据。七个场景的结果、失败及纠正记录见
[2026-09-13 验收报告](mcp_demo_validation_20260913.md)。

已有留档可用 `--recheck <demo 留档目录> --output <复核目录>` 离线复核；
可重复指定 `--recheck`，不启动 Codex/设备、不重写原始证据。
