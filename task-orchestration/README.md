# task-orchestration

本服务仅用于本机开发与联调；请仅绑定回环地址 `127.0.0.1`：

## SZLab 独立模拟器联调

每个服务使用独立终端。端口上已有正确服务时直接复用，不要重复启动。Python 命令应在
项目规定的 Python 3.11 `mamba` 环境中运行。Task API 终端必须进入
`task-orchestration`，使 `Path.cwd()` workspace root 能读取同目录下
`szlab_robot_action_workflow.json.task-workspace.json`；其余命令从仓库根目录执行。

Task API：

```bash
cd task-orchestration
PYTHONPATH=src python -m uvicorn task_orchestration.main:app \
  --host 127.0.0.1 \
  --port 8091
```

带 SZLab preset 的 `workflow_ui`：

```bash
PYTHONPATH=. python -m scripts.workflow_ui \
  --host 127.0.0.1 \
  --port 8014 \
  --preset szlab_robot_action_workflow \
  --no-browser
```

Vite：

```bash
npm --prefix unilabos_local_ui run dev
```

Vite 就绪后打开 `http://127.0.0.1:5174/`。Vite 将 `/api` 代理到
`workflow_ui` 的 `8014` 端口，将 `/task-api` 代理到 Task API 的 `8091` 端口。

### 复用 245 个 Task

仓库根目录的 `szlab_robot_action_workflow.json` 是经过验证的 27 Action 主流程；
`task-orchestration/szlab_robot_action_workflow.json.task-workspace.json` 是与它同名的
默认 Task workspace，包含 17 个模板、245 个等待中的 Task，以及各实例已保存的参数。
workspace 中不包含提交者的执行日志、OPC 会话或资源占用状态。

合并提交并启动上述三个服务后，在画布导入仓库根目录的
`szlab_robot_action_workflow.json`。页面应直接显示 245 个 Task；首次派发前保持暂停，
点击一次“重置并复用”以按当前时间重建样品起始间隔，然后重新连接 Task OPC。不要导入
名字不同的副本，否则前端会切换到另一个 Task workspace。

### 网页操作顺序

1. 在流程画布点击“切换 Task 模板编辑”，拖拽框选节点后右键“设为 Task 模板”。
   系统不会按 SZLab 工位或工艺自动切分；新模板的 `resources`、输入条件和输出条件
   默认均为空，创建模板无需先连接 OPC。
2. 仓库默认 workspace 已将 17 个模板预排到 Resource Schedule，并预置 245 个可复用
   Task。自建模板仍需拖入“待排模板”；生成器只使用该区域中的模板，未拖入的模板不会
   进入 OPC 配置。
   Resource Schedule 中由节点汇总的设备信息只用于展示，不表示设备占用或互斥。
3. 点击“生成 OPC 模拟配置”。在“变量目录”中逐项核对真实 OPC 变量名，并确认：
   `direction` 是 `pc_to_plc` 或 `plc_to_pc`，`data_type` 是 `bool`、`int`、
   `float` 或 `string`；仅 `plc_to_pc` 可设置 `initial_value`，也只有该方向允许
   模拟器写入。
4. 按节点索引逐个补全 `channel`、`trigger`、`on_trigger`、`on_complete`；需要复位
   握手时启用并补全 `reset_when` 和 `after_reset`。右侧会显示精确缺失路径。
5. 选择保存方式：
   - “保存草稿”写入 `status: "draft"`，允许保留校验错误，不能启动；
   - “校验并保存”要求全部路径通过，并写入 `status: "runnable"`；
   - “保存并下载”仅在 runnable 校验通过并成功保存后下载同一份规范化 JSON。
6. 安全默认 URL 为仅本机可达的 `opc.tcp://127.0.0.1:4840`，可直接启动。任何不同
   URL（包括旧 Bohrium 地址）都会显示阻断式远程写确认，只有明确确认后网页才发送
   `allow_unsafe_url: true`。独立 CLI 使用非默认 URL 时同样必须显式传
   `--allow-unsafe-url`；该外部进程不受网页状态/停止按钮管理。
7. 在“Task OPC 连接”填写与 profile 相同的 URL，点击“连接 OPC”，确认已连接且注册
   变量数大于 0。直接复用默认的 245 个 Task 时先点击“重置并复用”，再开始派发；需要
   新建其他批次时，设置样品数并点击“生成样品任务”。
8. 在 Task Queue/执行状态查看任务进度，在 Recent logs 查看模拟器触发、写入和恢复
   日志。调度周期按 `poll → advance → tick` 运行；“暂停派发”不会取消已下发动作，
   而会继续收割在途动作。
9. 结束后点击“停止并恢复 OPC”，等待 `STATE=stopped` 且
   `RESTORE=succeeded`。出现 `stopping`、`error` 或 `uncertain` 时不要再次启动，
   先核对远端 OPC 状态。

“启动 OPC 模拟”和“运行调度”完全独立，任一按钮都不会触发另一个流程，默认也不会
自动启动模拟器。

### Action/OPC 调度边界

- Task 调度只维护样品内前序关系并派发 Action，不读取 `Robot_Home`、
  `Robot_任务允许写入` 等设备握手变量，也不会为设备或工位添加 mutex。同一设备的
  多个 Action 请求允许并发进入设备 Action。
- 设备可用性、OPC 握手及必要的串行保护由 Action 内部负责；独立 Action 可以保留自身
  的内部锁。
- `Template.resources` 与顶层 `dynamic_resource_leases` 是旧 sidecar/API 的废弃兼容
  字段，不参与候选选择、Action claim 或排程互斥。
- input/output trigger 是可选的用户流程级条件。store 读取旧 sidecar 时只会丢弃已废弃
  的单数 `Template.trigger`；契约内合法的 `input_triggers`/`output_triggers` 会继续
  保留，升级时不会自动删除，确认不再需要后可在模板编辑区手动清除。
- OPC profile 根据已排模板所含 workflow 节点及其 Action `opc_variables` 生成。模板
  条件可以为空，不是生成 profile 或派发 Action 的前置要求。

### Schema v2

六节点 runnable 示例位于
`scripts/config/szlab_task_opc_simulator.json`。字段精简说明：

- 根字段：`schema_version: 2`、`status`、`name`、`opc`、`variables`、`nodes`；
  `opc` 严格包含合法 `opc.tcp` `url`、0.05–60 秒的 `poll_interval` 和
  0.1–60 秒的 `io_timeout`，各层均不接受额外字段。
- `variables[]`：`name`、`direction`、`data_type`、`source`，以及仅适用于
  `plc_to_pc` 的可选 `initial_value`；`source` 仅允许 `action_node`、
  `task_input`、`task_output`、`manual`，手工新增变量固定使用 `manual`。
- `nodes[]`：原流程信息 `workflow_node_id`、`task_template_ids`、`device_id`、
  `method`、`params`，以及运行配置 `channel`、`trigger`、`on_trigger`、
  `on_complete`、可选 `reset_when`/`after_reset`。
- 条件组使用 `{"all":[...]}`；条件字段是 `variable`、固定为 `eq` 的 `operator`、
  类型化 `value` 和 `rising|falling|level` 的 `edge`。阶段由 `writes` 组成，
  `on_complete`/`after_reset` 另含非负 `delay`。

`runnable` 不允许未知变量、未知方向/类型、PC→PLC 变量写入或值类型混淆。样品数属于
Task 排程，不是 profile 字段。

Workflow UI 中生成、保存或导入的配置 JSON 写入
`task-orchestration/configs/`；可在界面下拉选择已有文件并 **加载所选配置**。首次启动
会把 `scripts/config/szlab_task_opc_simulator.json`（六节点 runnable 示例）以及
`scripts/config/*-opc-simulator.json` 旧文件复制到该目录（不覆盖已有同名文件）。

### 独立 CLI

独立脚本没有 `--samples` 参数：

```bash
python -m scripts.szlab_task_opc_simulator \
  --config scripts/config/szlab_task_opc_simulator.json
```

非默认 URL 必须由操作者显式授权；`--url` 会覆盖 profile 中的 URL：

```bash
python -m scripts.szlab_task_opc_simulator \
  --config scripts/config/szlab_task_opc_simulator.json \
  --url opc.tcp://host:4840 \
  --allow-unsafe-url
```

其他公开参数为 `--timeout`、`--io-timeout`、`--poll-interval`、`--log-level` 和
`--dry-run`。轮询和 I/O 超时默认取 profile，CLI 参数仅作显式覆盖；省略
`--timeout` 或传 `0` 表示持续运行直到停止，显式正值才建立全局 deadline。
`--dry-run` 只读不写，不能驱动任务完成。

### Profile 与进程 API

- `POST /api/opc-simulator/profiles:generate`：根据 `workflow`、
  `scheduled_template_ids` 与节点 Action 变量目录生成 draft；`templates` 用于确定
  已排节点范围，模板条件不是必填输入。
- `POST /api/opc-simulator/profiles:validate`：校验 `{profile, file_name}`，返回规范化
  profile 和 `validation_errors` 路径。
- `PUT /api/opc-simulator/profiles/{file_name}`：保存 `{profile}`。覆盖已有文件必须
  发送与当前 revision 相同的 `If-Match`；新文件不发送。
- `GET /api/opc-simulator/profiles/{file_name}`：读取 profile、校验路径和 revision。
- `POST /api/opc-simulator/start`：提交
  `{file_name, expected_revision, allow_unsafe_url}`；后者必须是严格布尔值，非默认
  URL 为 `false` 时会在创建子进程前被拒绝。
- `GET /api/opc-simulator/status`：读取状态、`run_id`、URL、退出码、恢复状态和日志。
- `POST /api/opc-simulator/stop`：提交当前运行的
  `{"expected_run_id":"<32位小写十六进制 run_id>"}`；省略 run ID 表示停止当前运行，
  网页对非本标签页启动的运行会再次确认。

profile 文件名只能是 `scripts/config/` 下一层安全 ASCII slug `.json`，不存在、路径
越界或符号链接会被拒绝。若连接、轮询或执行请求缺少当前 workflow 路径，接口返回
“缺少当前 workflow 路径”。校验错误按 `nodes[2].trigger.all` 等真实 JSON path 返回，
不要凭字段描述猜位置。保存返回 409 表示 revision 冲突，必须点击 `Reload` 取得后端
版本后再修改和保存；启动也必须携带最新 `expected_revision`。

### 安全与停止语义

- `workflow_ui` 进程管理器全局只允许一个模拟器子进程处于活动生命周期。模拟器另外按
  规范化 OPC URL 使用 macOS/Linux 本机文件锁，阻止同机同 endpoint 并发。
- 文件锁不能跨主机协调，因此禁止从不同主机同时运行指向同一 endpoint 的模拟器。
  恢复前只在远端当前值仍与本实例最后写值严格同类型、同值时回写原值；这是
  best-effort 所有权保护，不能消除 ABA 或其他远端并发写入。
- 页面 `pagehide` 只对本标签页持有的 `run_id` 发出 keepalive 停止请求，是
  best-effort；关闭页面后仍须检查后端状态和远端值。
- 普通“停止并恢复 OPC”发送 `SIGTERM` 并默认等待 15 秒。超时返回 504，但不会
  `SIGKILL`；进程保持 `stopping`、恢复状态为 `uncertain`，让恢复继续进行。
- 仅 `workflow_ui` 自身关闭时执行最多 60 秒的有界 graceful shutdown；超时后为避免
  留下仍写远端的孤儿进程，会强制终止进程组，并明确保持 `failed/uncertain`，不能
  视为恢复成功。
- `restore_status`：`not_started` 表示尚未开始停止恢复；`pending` 表示已发停止并在
  恢复；`succeeded` 表示子进程以 0 退出并完成恢复；`error` 表示异常退出或恢复失败；
  `uncertain` 表示停止超时或被强制终止，远端恢复结果未知。

所有 UI、PLC gateway 和管理 API 均不使用 Bearer token。PLC 注册和快照分发仍由
`/opc/registrations` 与 `/opc/snapshots` 提供，并保留版本冲突检测与快照负载限制。

### 验证命令

先激活项目 Python 3.11 环境：

```bash
mamba activate unilab
python -m pytest tests/szlab_poly_studio -q
python -m pytest task-orchestration/tests -q
npm --prefix unilabos_local_ui test
npm --prefix unilabos_local_ui run build
```

提交前请确认并一次性 stage 本次 Task 调度相关文件，避免只提交 index 中的旧版本。

## Trigger DTO

- CSV OPC 条件：`{"kind":"opc","config":{"plc_device_id":"szlab_poly_plc","variable":"变量名","value":<类型化值>}}`。
  默认网关向 `/api/v1/opc/snapshots` 写入相同 `plc_device_id: "szlab_poly_plc"` 的快照；
  `test_default_provider_snapshot_starts_frontend_mapped_opc_trigger` 覆盖该路径。
- 当前持久化 Trigger 仅接受 `kind: "opc"`。资源、工位、internal 及旧
  `opc_condition` DTO 均不属于当前契约，会以 422 拒绝。
- input trigger 只在 Task 进入运行前判定；output trigger 保留为流程级输出描述，
  不负责判定 Action 完成。设备就绪和完成握手由 Action 自身处理。
