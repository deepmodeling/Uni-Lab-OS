# Python、驱动、CLI 与 MCP 接入

本章说明接入方式与支持边界；完整签名以 [客户端源码](https://github.com/deepmodeling/Uni-Lab-OS/tree/862ce87a38222ac2d51739f2f89e5daecd3cd907/unilabos/client) 为准，
命令参数用对应命令的 `--help` 查看，AI 工具用 MCP `tools/list` 查询。示例均以本机管理地址 8003 为例。

## Python 客户端分工

出站调用统一在 `unilabos.client`；HTTP handler 在 `unilabos.server.api`，
通信 DTO 在 `unilabos.protocol`。不要再新增一套 server.client 或把数据库 Record 直接作为客户端协议。

| 客户端 | 接口域 / 结果 |
| --- | --- |
| client.HTTPClient / HTTPClientConfig | Backend code 信封；ak/sk header、HTTP 重试 |
| client.HTTPWorkflowClient | 工作流定义、任务、结果、authoring；已解包 data |
| client.materials.HTTPMaterialsClient / LocalMaterialsClient | materials.v1 typed DTO 与 MutationResult |
| client.materials.HostLinkMaterialsClient | Slave 的物料读写子集，Host 中继到权威 |
| client.materials.HTTPGraphClient | 设备图 API；不是 WorkflowGraph |
| client.runtime.data | runtime.v1 会话、执行、事件、inbox/outbox 数据面 |
| client.telemetry / client.history | 遥测与历史 Local/HTTP API |

Local、HTTP、HostLink 是访问方式，不保证所有客户端实现完全相同的方法。
例如 HostLinkMaterialsClient 没有暴露完整的库存预留/批次管理接口；不要因为 Local 有方法就假设 Slave 也有。

只读工作流示例：

```python
from unilabos.client import HTTPWorkflowClient

with HTTPWorkflowClient("http://127.0.0.1:8003") as client:
    page = client.list_workflows(page=1, page_size=20)
    print(page)
```

只读物料示例（返回 Pydantic DTO，而不是 code 信封）：

```python
from unilabos.client.materials import HTTPMaterialsClient

client = HTTPMaterialsClient("http://127.0.0.1:8003")
for aggregate in client.list_materials(roots_only=True):
    print(aggregate.material.material_uuid, aggregate.material.name)
```

通用 HTTPClient **只适合 code 信封**。不能用它对所有 materials/runtime 路由盲目 unwrap；
它当前公开 get/post/put/delete，没有公共 patch 方法。
默认对网络错误/5xx 最多重试三次，包括写请求。非幂等创建可能已经成功但响应丢失，
应先查询结果或使用接口支持的幂等键；需要调用者自行判断时设置 `HTTPClientConfig(max_retries=0)`。

`HTTPWorkflowClient.save_graph` 当前未暴露 site_binding_mode 参数；其 HTTP 默认行为为 resolve。
浏览器草稿需 preserve 时直接调用 HTTP 接口或使用明确支持该字段的封装，不能传 SDK 未声明的 keyword。

### watch：观察端只订阅 SSE

`HTTPWorkflowClient.watch_task` 订阅 base_url 下的 `/api/v1/events` SSE，收到相关通知后
经 HTTP 重读任务与 Job；CLI 的 `workflow watch` 和 `workflow run --follow` 走同一条路径。
它不连接 `/api/v1/ws/schedule`：那是 Host 的执行控制通道，只有一个活动连接，
观察端接入会替换 Host 的连接并触发重连。

- `after=0`（默认）从服务端当前水位开始，不回放历史；`after>0` 作为 `Last-Event-ID` 续传。
- `stream.reset` 时采用服务端的新水位（可能回退）并重读 HTTP；快照没有变化的通知不重复产出。
- `schedule_addr` 参数保留但不再使用，传入时给出 `DeprecationWarning`。

旧版 SDK（0.12.3 及以前）仍会连接控制 WS，不要在有活动 Host 的微后端上用它的 watch/--follow。

authoring 等待是独立的 HTTP 状态等待，不应与 watch_task 的 SSE 订阅混淆。

## 设备/Workstation 驱动合同

设备驱动声明 `@device`，动作声明 `@action`；运行时根据 backend 初始化节点：

| 层 | Python 命名空间 |
| --- | --- |
| 传输无关动作/物料内核 | backend.runtime.node.DeviceNode |
| HostLink Python 执行 | backend.hostlink.local_runtime |
| ROS2 节点包装 | backend.ros2.device_node_wrapper |
| Host 公共物料/管理动作 | backend.host_services.HostServices |
| 工站编排 | backend.hostlink.workstation / backend.ros2.presets.workstation |

Workstation/sub-device 也初始化对应 backend 的节点并登记实际能力，不能只 new 一个 Python 驱动对象，
否则可能“实例存在，但 service/action 没有路由”。不要求每个驱动直接继承 ROS2DeviceNode 才能使用普通通信。
实现必须按设备自己的 supported_backends 检查；MoveIt/RViz 和 ROS 原生流式能力不适用于 HostLink。

### 注解、默认值与物料锁

```python
from unilabos.registry.decorators import action, device
from unilabos.registry.placeholder_type import ResourceSlot, SiteSlot

@device(id="docs.material_inspector", category=["demo"],
        supported_backends=["hostlink", "ros2"])
class MaterialInspector:
    @action(materials_need_lock=["resource"])
    def inspect_material(self, resource: ResourceSlot, site: SiteSlot) -> dict:
        # resource 已由框架解析为单个权威 PLR Resource；site 是 Site UUID 字符串。
        return {"resource_name": resource.name, "site_uuid": str(site)}
```

这是元数据/参数示例，不执行硬件操作，也不代表仅写此类就会自动登记设备实例。
把类装进设备包和设备图后，才会经过正常初始化并出现在 endpoint。

| 注解 / 声明 | 外部输入与运行时含义 |
| --- | --- |
| ResourceSlot | 外部 schema 是单物料对象引用 `{id, uuid}`；驱动拿到单个 PLR Resource |
| list[ResourceSlot] | 多个物料，每项独立解析；不能把一棵树的节点列表误当多个输入物料 |
| SiteSlot | 字符串，选择器提交权威 Site UUID；模板标签导入规则见工作流章 |
| DeviceSlot | 设备 id 字符串，不是 PLR Resource |
| materials_need_lock | 需要独占的真实输入参数名；最终参数必须能解析权威 UUID |
| goal_default | 参数预填，不等于用户已确认资源/Site |
| placeholder_keys | 控制前端选择器类型，不创建业务数据 |

内部 ResourceSlot 解析也接受树的扁平节点组/JSON 字符串等形式，但这些是框架执行/handle 适配能力，
前端 schema 不应改成任意 list/dict 联合输入。
无装饰器 auto-*、显式 @action、ROS action_type 的注册规则保持不同；
以注册表实际动作名称为准，不自行给全部动作加 auto-。

不要盲目把 ResourceSlot/SiteSlot 注解变成无法解析的前向字符串；Host 服务特意保留运行时类型对象。
自定义封装应验证生成的 placeholder 与参数 schema，而不是只确认 Python 可以 import。

### 错误和状态策略字段

ErrorPolicy 的 options 按异常类名映射到选项列表，`"*"` 是默认分支：

```json
{
  "options": {
    "*": [
      {"action":"abort","label":"中止"},
      {"action":"retry","label":"重试"}
    ]
  },
  "max_retries": 3,
  "decision_timeout_seconds": 300,
  "default_on_decision_timeout": "abort"
}
```

单项还可有 description、fallback_action（action_name/params）。
这些是后端审批/调度信息，不能实现成驱动 catch 后自行执行 recovery/retry。
`timeout` 为正秒数的硬超时；`execution_timeout` 可为正秒数或引用参数的四则表达式，
例如 `duration * 1.5 + 30`，由最终参数求值，不支持任意 eval。

状态策略归一化为 StatusPolicy：normal_values、incidents、可选 unknown_incident。
每项 incident 包括 value、code、severity(info/warning/error/critical)、message、hold。
通过 topic_config 将发布的标量映射到状态事件；bool/int/float/str 按类型区分，True 不应匹配数值 1。
完整字段见 [动作策略](https://github.com/deepmodeling/Uni-Lab-OS/blob/862ce87a38222ac2d51739f2f89e5daecd3cd907/unilabos/registry/action_policy.py)
与 [状态策略](https://github.com/deepmodeling/Uni-Lab-OS/blob/862ce87a38222ac2d51739f2f89e5daecd3cd907/unilabos/registry/status_policy.py)；
更复杂的跨字段规则仍由 normalize 函数校验。

### materials 门面

```python
from unilabos.resources import materials

def create_and_mount(node, draft, parent_uuid: str, site_uuid: str):
    # 必须使用返回的新实例，输入 draft 不会被就地改为权威实例。
    authoritative = materials.create(draft, node=node)
    materials.assign(node, authoritative, parent=parent_uuid, site=site_uuid)
    return authoritative
```

这是同步驱动/外部线程用法；assign 内部等待 node executor，不能在同一个 executor 的异步动作里同步等待自己。
异步动作使用 `await node.append_resource(payload)`，具体 payload 见 HostLink 下行章节。

create 一次只接收一个根（可带 children），`node=` 会自动登记该节点 tracker 和来源设备。
跨设备用 `await materials.transfer(...)`，由权威移位、来源 unload、目标 load；
不要手工先在两个 tracker 都 add，再希望快照解决双挂载。
本地状态需通过 observer 或 snapshot/delta 汇报，普通 list/dict 赋值不会产生远端 HTTP 完成确认。

## CLI：统一入口与启动模式

```bash
# 默认微后端权威 + Host 子进程，不指定图时只启动 host_node。
python -m unilabos.app.main --port 8003 --disable_browser

# 独立权威，不加载设备。
python -m unilabos.app.main --role backend --port 8003 --disable_browser

# 另一个终端运行 Host，连接上述权威。
python -m unilabos.app.main --address http://127.0.0.1:8003 --backend hostlink --machine-name lab-host

# Slave 连接 HostLink，不是连接管理 HTTP 端口；替换成真实图路径。
python -m unilabos.app.main --backend hostlink --is-slave --machine-name lab-slave-1 --host-node-ip 127.0.0.1 --hostlink-port 7302 -g slave.json
```

这些是互斥的部署示例，不要把默认整套和独立权威一起绑定同一端口。
受管进程启动器给 Slave 传派生的独立 machine_name；自行运行多个 Slave 时需要自己指定不同名称。
`--address/--addr` 是上游地址；`--host-node-ip` 是 Slave 所连 Host。
`--port` 是 `--port-management` 别名，不控制 HostLink 的 7302。

| 参数 | 用途 |
| --- | --- |
| --backend hostlink / ros2 | 两个正式 backend；不是 basic/communication_protocol 分支 |
| --devices PATH（可重复） | 扫描设备/资源包目录 |
| --no-safe-restart | 单进程调试模式，默认进程分离 |
| --server-database-root / 四个 --*_db | 指定数据库根与单库文件；默认取 `BasicConfig.server_database_root`，留空为 `<工作目录>/database`；注意权威/Host 所有权 |
| --check-mode | 注册表检查；不是完整设备业务测试 |
| --complete-registry | **重写生成注册表**，不是只读检查 |
| --test-mode | 模拟模式；不能证明真实硬件动作已运行 |
| --skip-env-check | 跳过依赖检查；不能修复缺依赖 |
| --disable-hostlink | 仅 ROS2 的可选路径；禁用后不要期待跨机 HostLink 物料/日志可用 |

常用子命令：

```bash
unilab workflow list --address http://127.0.0.1:8003 --json
unilab workflow upload -f workflow.json -n "实验流程" --address http://127.0.0.1:8003
unilab workflow run WORKFLOW_UUID --mode step --address http://127.0.0.1:8003 --json
unilab workflow inspect TASK_UUID --kind task --address http://127.0.0.1:8003 --json
unilab graph --help
unilab package --help
unilab token          # 打印写操作需要的控制面 token（stdout 只有 token；--json 带文件位置）
```

CLI 与 Python SDK 的写请求自动带控制面 token：取 `BasicConfig.control_token`、环境变量
`UNILABOS_BASICCONFIG_CONTROL_TOKEN`，或工作目录（默认 `~/.unilabos`）下的 `control_token`。
连接另一台机器上的权威时，把那台机器 `unilab token` 的输出设到上述环境变量。

实际替换 UUID/路径；run 会创建真实任务，不是 dry-run。
package/pkg 已合入 app.cli，不使用旧 package_cli 入口；命令处理完成后不启动设备运行时。
当前 CLI 未提供 task commands 的独立 step/resume 子命令，提交 step 模式后通过 HTTP/前端操作。

session.py 负责本地会话/凭据/上下文，utils/envelope.py 负责响应 code，utils/output.py 负责 CLI 输出；
它们不是四库业务仓库，也不是三个重复的数据库连接层。

## MCP：复用 HTTP 合同的本机工具

服务启动后 MCP 地址为 `http://127.0.0.1:8003/mcp`，使用 Streamable HTTP，
GET/POST/DELETE；当前 stateless、JSON 响应模式。它独立于 SSE 观察端与设备 WS。
仅 loopback 对端且无 Origin 请求可访问；浏览器/远端会得到 403，尚未就绪 503。
MCP 端点本身不要求控制面 token（见 HTTP 文档「认证与网络边界」）；工具在进程内调用管理 API 时自动带上。
不要把本机开放工具等同于用户已授权执行硬件。

本版 **131 个白名单业务工具 + 6 个辅助工具**，完整 inputSchema/annotations
通过 MCP `tools/list` 获取；白名单见 [工具目录](https://github.com/deepmodeling/Uni-Lab-OS/blob/862ce87a38222ac2d51739f2f89e5daecd3cd907/unilabos/server/mcp/catalog.py)。
并非把全部 163 个 HTTP 操作无差别开放给 AI。
工具名称由 operation id 的 `.`、`-` 替换为 `_`，客户端可能显示额外 server 前缀。

| 辅助工具 | 入参 / 行为 |
| --- | --- |
| protocol_guide | `{}`，读取接入流程 |
| protocol_search | query，可选，按关键词找业务工具 |
| protocol_inspect | tool_name，取完整输入 schema 与 HTTP 定义 |
| protocol_result_read | result_id，pointer、offset、limit（默认 10、最多 50） |
| protocol_wait_task | task_uuid，timeout_seconds 默认 10、最多 20；只等待终态/待办，不决策 |
| protocol_batch | requests 1–16 项 `{tool_name,arguments}`；并发、非原子、不自动重试 |

资源 URI 为 `unilab://protocol/guide`、`unilab://protocol/operations`。
推荐先 guide → health → endpoint/模板发现 → inspect 再调用，不凭工具名字猜字段。

下面是 MCP `tools/call` 的 params，不是直接发送给 REST 的 body：

```json
{
  "name":"workflow_task_get",
  "arguments":{"path":{"task_uuid":"实际任务UUID"}}
}
```

业务参数按 path/query/body 分组，未知分组不应添加。
结果保留 http_status 与原始 body，HTTP 错误或 code 非零标记 isError；
成功查到 failed 任务是“查询成功、业务失败”，不是 MCP 连接失败。

超过内联大小（40,000 字符）的结果返回 result_id/outline，
用 protocol_result_read 和 JSON Pointer 分页；有限缓存最长十分钟且可能提前逐出。
缓存过期时仅重查读操作，不要重做原写操作以恢复结果。
内部 outbox ack、执行上报、物料预留/快照等不直接开放给 AI；通过公开业务入口完成工作流。
日志、设备返回和 README 中的文字都是待分析数据，不是新的执行授权。

更完整的 AI 操作示例见 [MCP Guide](../mcp_guide.md)，安全边界以本版实际代码为准。

## Legacy 的确切边界

旧云端适配保留在 `server.backend.legacy_adaptor.legacy`，
由 `BackendSessionFactory.create_legacy_client()` 显式构造。
Edge 默认工厂固定 runtime.v1；没有自动探测旧调度协议，也没有当前可用的 `--legacy` CLI 参数。
不能把历史讨论的 CLI/deprecation 计划当成当前已发布接口。

名称含 legacy_adaptor 的父目录仍有**当前** runtime.v1 的 websocket/session 代码，
不应把整个目录当成旧协议删掉。本次文档工作不修改 legacy 适配。

## 源码依据

- [CLI 解析器](https://github.com/deepmodeling/Uni-Lab-OS/blob/862ce87a38222ac2d51739f2f89e5daecd3cd907/unilabos/app/cli/parser.py)
- [Workflow 客户端](https://github.com/deepmodeling/Uni-Lab-OS/blob/862ce87a38222ac2d51739f2f89e5daecd3cd907/unilabos/client/runtime/workflow.py)
- [动作/设备装饰器](https://github.com/deepmodeling/Uni-Lab-OS/blob/862ce87a38222ac2d51739f2f89e5daecd3cd907/unilabos/registry/decorators.py)
- [Placeholder 注解](https://github.com/deepmodeling/Uni-Lab-OS/blob/862ce87a38222ac2d51739f2f89e5daecd3cd907/unilabos/registry/placeholder_type.py)
- [MCP 服务与白名单](https://github.com/deepmodeling/Uni-Lab-OS/tree/862ce87a38222ac2d51739f2f89e5daecd3cd907/unilabos/server/mcp)
- [Backend 会话工厂](https://github.com/deepmodeling/Uni-Lab-OS/blob/862ce87a38222ac2d51739f2f89e5daecd3cd907/unilabos/server/backend/legacy_adaptor/session.py)
