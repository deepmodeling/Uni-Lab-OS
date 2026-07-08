# Bioyond 工作站接入补充指南

本文件是 `add-workstation` 的 Bioyond/Bioyond Studio/奔曜供应商系统补充说明。遇到以下场景时读取：

- 新接入或迁移 Bioyond 风格的外部系统工作站。
- 继承或参考 `BioyondWorkstation`、`BioyondV1RPC`、`WorkstationHTTPService`、Bioyond 物料同步代码。
- 需要暴露 Bioyond LIMS 订单、设备 operation、物料同步、人工确认、错误处理、报告/附件动作。
- 外部设备包通过 `--devices ./<package> --external_devices_only` 加载，而不是直接改 Uni-Lab-OS monorepo。

## 目录

1. 接入姿势
2. 基类提供什么，哪些必须按工站适配
3. 后端路线，不使用前端路线
4. 现场 API 测试
5. 设备 operation 封装
6. 注册表 / action / 人工确认契约
7. 物料、Deck、仓库同步
8. 回调、错误处理、报告和附件
9. 随包 vendored 代码与运行路径
10. 测试分层
11. 完成检查表

---

## 1. 接入姿势

Bioyond 这类工作站优先按“外部系统工作站”接入，而不是按 protocol compiler 接入。默认做法：

- 用 `WorkstationBase` 或已有 Bioyond 基类承载工站。
- 用显式 `@action` 暴露订单创建、等待、取消、报告、物料同步、复位、错误处理等业务动作。
- 用 `NodeType.MANUAL_CONFIRM` 做人工确认/审批/异常处置门禁。
- 用 sub-device proxy 暴露具体设备 operation，例如机械臂、LCMS、液体处理器、合成仪、离心机等。
- 用 PLR `Deck`/`Resource` 表示 Uni-Lab 侧资源树，用映射把 Bioyond 物料类型、仓库和库位转成 PLR 资源。
- 用现场 API 结果修正 wrapper/schema；不要只凭旧快照或另一个工作站。

若项目明确要求把 Bioyond 工作流编译成 Uni-Lab protocol，再考虑 protocol 路径。否则优先站在“外部系统已有 workflow engine，Uni-Lab 负责调度、展示、同步和人工确认”的模型上。

---

## 2. 基类提供什么，哪些必须按工站适配

不同 Uni-Lab-OS 版本、monorepo 版本和外部设备包可能不一致。实现前先读当前环境源码，并在代码/文档里标出依赖来自哪一层。

### 2.1 Uni-Lab-OS 工作站框架通常提供

| 能力 | 通常来源 | 使用方式 | 注意 |
|------|----------|----------|------|
| 工作站注册 | `@device(..., category=["workstation"])` | 被装饰的工作站类 | AST 扫描只看可导入定义；继承/helper 里的动作需要薄 wrapper。 |
| 动作注册 | `@action` | station 或 sub-device 方法 | docstring、签名、handles、manual-confirm metadata 都会影响前端。 |
| Deck 注入 | 图文件 `config.deck` + `WorkstationBase.__init__` | `__init__(..., deck=None, **kwargs)` 后 `super().__init__(deck=deck, **kwargs)` | 不要在 `__init__` 手动新建主 Deck。 |
| 初始化阶段 | `post_init(ros_node)` | 保存 `_ros_node`，启动 HTTP 服务/监控/同步器 | 网络服务和后台线程放这里，不放 `__init__`。 |
| 子设备容器 | `ROS2WorkstationNode` | `self._ros_node.sub_devices[...]` 或当前版本的 `_children` | 以当前源码为准；不要自己维护另一套子设备列表。 |
| 资源上传 | ROS node `update_resource` | 上传 Deck 或变更资源到云端 | 注意上传根节点，避免只上传内层 child。 |
| 硬件代理 | workstation 子设备初始化 | 适合串口/IO/Modbus 型硬件 | Bioyond API proxy 通常不是这个机制，而是 RPC wrapper。 |
| 物料同步抽象 | `ResourceSynchronizer` | 自定义 `sync_from_external`/`sync_to_external`/`handle_external_change` | 抽象不负责 Bioyond payload、仓库映射、缓存策略。 |
| HTTP 回调框架 | `WorkstationHTTPService` | route 分发到 `process_*`/`handle_*` 方法 | 版本可能有 bug；Bioyond material-change 是否真正委托给 station 要测试。 |

### 2.2 Bioyond 基类可能提供

如果使用已有 `BioyondWorkstation` 或 vendored Bioyond base，常见可复用能力包括：

- `hardware_interface` 保存 Bioyond RPC client。优先访问 `self.hardware_interface`，不要假设存在 `self.rpc`。
- workflow 队列/topic 辅助，例如 `workflow_sequence` 或 `append_to_workflow_sequence()`，但方法名和语义要看当前源码。
- callback/report 的部分默认处理，例如 step/sample/order/material/error report 入口。
- debug call log、请求日志、运行目录解析。
- Bioyond resource conversion、material sync、cache 或 publish 辅助。

必须确认当前基类是否真的提供这些方法。不要把参考站里的 station-specific 方法当成基类能力。例如 `process_and_execute_workflow()`、`wait_for_order_finish()`、某个 station 的 material allocator、某个 station 的 workflow builder，都可能只属于那个站。

### 2.3 每个 Bioyond 工站通常必须适配

| 主题 | 必须适配的内容 |
|------|----------------|
| 订单/实验 | `order_id`、`order_code`、项目 ID、样本 ID、plate ID 的关系；创建、取消、等待、报告状态码。 |
| workflow | workflow UUID/name、步骤参数、Day1/Day2 等业务路线、运行状态转换。 |
| 设备 operation | 设备名称、operation 中文名/英文名、参数范围、下拉枚举、是否允许执行。 |
| 物料类型 | PLR 类名到 Bioyond material type UUID/显示名的映射。 |
| 仓库/库位 | warehouse UUID、site UUID、坐标命名、y 轴/前端展示方向、空位策略。 |
| 资源模型 | 是否建 wells/tips/slots 子资源；哪些外部明细只是 metadata。 |
| 同步策略 | 外部为准、Uni-Lab 为准、双向、move-first、clear-stale、虚拟暂存区。 |
| 人工确认 | 展示表列、审批默认值、assignee、异常/取消/复位如何阻断或继续流程。 |
| 报告附件 | 哪些文件类型要上传 notebook，ZIP 是否等待，失败如何重试/告警。 |

---

## 3. 后端路线，不使用前端路线

Bioyond 前端 route 和后端 LIMS/API route 不是同一个接口层。运行时代码使用后端 API。不要用前端 route/HAR payload 实现工作站动作；前端观察最多用于理解业务页面展示，不能替代后端 schema、当前源码或 live 后端 API 结果。

---

## 4. 现场 API 测试

Bioyond 接入必须安排现场 API 验证。静态快照和 OpenAPI 只能保证“看起来合理”，不能证明当前部署能执行。

### 4.1 先做只读探测

优先验证这些只读事实：

- `device-list`：设备数量、设备名称、每台设备的 operation 数量、operation 参数 schema。
- 仓库/库位：仓库 UUID、site UUID、库位命名、空位和不可用位。
- 物料类型：type UUID、显示名、单位、必填字段。
- 物料列表：实际 Bioyond 返回的容器/plate/子项/位置字段。
- 订单列表/报告：已知订单的 `order_id`、`order_code`、status、报告文件 URL。
- 错误/异常枚举：异常处置 option、token、ijk 等字段是否和文档一致。

记录每次探测的 host、时间、endpoint、样本 ID、业务 `code/message/data`。如果 API 返回 HTTP 200 但业务 `code` 非成功，wrapper 必须按失败处理。

### 4.2 再做受控写入

写接口只在测试环境、凭证、样本数据和安全边界明确后执行。建议顺序：

1. 对单个低风险 action 做最小写入。
2. 验证 Bioyond UI/后端状态变化。
3. 验证 Uni-Lab 资源树、日志、callback、报告是否一致。
4. 再扩大到完整 workflow。

### 4.3 快照怎么用

`device-list` operation 快照适合生成 wrapper、dropdown 和参数范围；执行前仍要用当前 live `device-list` 验证。不同部署可能设备数相同但 operation 为空，也可能 operation 名相同但参数枚举不同。

---

## 5. 设备 operation 封装

Bioyond sub-device proxy 通常是薄封装：每个 `@action` 调用统一的 `_execute_operation(<operation_name>, params)`。

建议模式：

- wrapper 类只做参数规范化、schema 描述、错误包装，不写复杂业务流程。
- operation name 和参数 schema 来自当前部署快照 + live read-only 验证。
- 每个 action 的错误返回包含 endpoint、device、operation、request params、业务 `code/message` 和必要 response 摘要。
- HTTP status 不是成功标准；必须检查业务 envelope。
- 若 operation live 不存在，动作应失败并给出清晰错误，不要静默跳过。

不要直接复用另一个工作站的 operation payload。Bioyond 设备 operation 具有部署相关性。

---

## 6. 注册表 / action / 人工确认契约

Bioyond 工作站通常会暴露许多流程动作，前端可用性取决于 registry metadata（注册元数据）。把 action schema 当成用户界面契约维护。

先读 [workflow-node-guidelines.md](workflow-node-guidelines.md) 里的通用规则：展示名来源、默认值、变量排序、变量分组、handles、日志、阻塞/非阻塞错误和工作流状态语义。然后再应用当前 Bioyond 工站或当前仓库定义的具体显示名、必填标记、内部字段命名等约定。

### 6.1 AST 可见性

- 所有工作流可见 action 写在当前被装饰的 station/sub-device class 上。
- 如果能力来自 base/helper，用薄 wrapper 暴露。
- `TypedDict`、变量分组结构、枚举、`Field(...)` 要可导入可见。
- `@resource` Deck/labware class 要可导入可见；必要时在 package `resources/__init__.py` eager import，避免 PLR 通过 `__subclasses__()` 找不到类。

### 6.2 参数说明

推荐规则：

- `Args:` 使用 `param[显示名]` 格式。
- 必填标记、内部字段展示方式和业务字段命名以当前仓库约定为准；不要从某个参考工站搬运专属 schema。
- 若同时存在用户可读编号和系统内部 ID，在说明里讲清二者来源、用途和传递方式。
- 方法签名、`goal_default`、handles、分组字段和 docstring 的顺序尽量一致。
- 分组参数用可导入可见的 `TypedDict` + `Annotated[..., Field(description=...)]`。

### 6.3 人工确认（manual-confirm）

Bioyond 常见人工门禁：

- 上下料确认。
- 等待订单完成后的物料卸载/异常确认。
- 复位前确认。
- 错误处理 option 选择。
- 报告/附件选择或人工审阅。

规则：

- handle 输入值用于展示和依赖传递，默认只读。
- 审批结果用 goal params，默认值必须保守。
- 失败或拒绝时抛异常，使 workflow 真正停止。
- 不要为 manual-confirm 节点增加没有独立业务含义的 `confirmed=True` 参数；节点本身就是确认门禁。
- assignee、表格 placeholder、renderer 字段要在当前前端/云端验证。

---

## 7. 物料、Deck、仓库同步

Bioyond 物料同步是最容易反复出错的部分。先明确同步方向和身份模型，再写代码。

### 7.1 同步方向

常见策略：

- 外部为准：从 Bioyond 拉取物料，Uni-Lab Deck 反映外部状态。
- Uni-Lab 为准：Uni-Lab 资源变化推送到 Bioyond。
- 双向：callback + 主动同步都可能移动资源，需要 cache/source 防止回环。

如果外部系统有独立物料管理，通常先实现“外部为准 + Uni-Lab 展示/操作后回写”的保守路径。

### 7.2 资源身份

为每个资源保留外部身份字段，例如 material UUID、barcode、warehouse/site UUID、order/sample 关联。移动物料时优先保留资源 identity，不要删除再新建，除非外部系统明确认为身份变化。

### 7.3 move-first 同步

推荐思路：

1. 先按外部 material UUID 或 barcode 找已有 PLR 资源。
2. 若存在，移动到新位置并更新 state。
3. 若不存在，创建资源并 assign 到目标位置。
4. 对无位置物料放入虚拟 holding/limbo，或按业务规则删除。
5. 最后做 stale sweep，只清理本轮快照确认消失的根资源。

### 7.4 publish root

更新资源树时上传合适的根节点。嵌套 plate/child 变化通常要发布 material root、warehouse root 或 Deck root，而不是只上传内层 child。否则前端可能不刷新，或云端只知道局部对象。

### 7.5 PLR 往返

每个 labware/resource class 必测：

- `cls(name="X")`
- `cls(**resource.serialize())`
- `Resource.deserialize(...)`
- 带 `children` 的 Deck 反序列化

构造器建议：

- 接受 `*args, **kwargs`。
- 对 `size_x/size_y/size_z/model/category/sites/children/ordering` 等 serialize 字段容错。
- 用 `kwargs.setdefault(...)` 补默认值。
- itemized plate/tip/tube rack 提供 `ordered_items` 或 `ordering`。

### 7.6 不要过度建模 wells/tips

Bioyond 明细行不一定需要成为 PLR 子资源。若每个 plate 的 wells/tips 让资源树过大、同步慢、前端卡顿，而流程不需要单孔实体动作，可以把外部 detail rows 保存为 metadata/state。只有当 Uni-Lab 侧动作需要单孔寻址、容量、液体状态或位置碰撞时，再建真实子资源。

---

## 8. 回调、错误处理、报告和附件

### 8.1 HTTP 回调

若使用 `WorkstationHTTPService`，验证每条 route 是否真正调用 station 方法：

- `/report/step_finish`
- `/report/sample_finish`
- `/report/order_finish`
- `/report/material_change`
- `/report/error_handling`

不要只看 HTTP ack。用本地 fake POST 和 station spy/mock 验证 `process_*` 或 `handle_*` 被调用。若当前 Uni-Lab-OS 版本对 Bioyond callback 有 bug，可以 vendored 修复，但必须写测试说明为什么 vendor。

### 8.2 订单完成与等待

等待订单完成通常要处理：

- `order_id` 和 `order_code` 二者只有一个可用时如何反查。
- 多订单并行时如何 disambiguate。
- callback 先到、polling 后到或超时。
- 成功、异常停止、人工停止等状态如何映射。
- 完成后是否立即做 materials-by-order-id 同步。

不要假设参考站的 `wait_for_order_finish` 是基类方法；当前站应显式实现或 thin-wrapper。

### 8.3 错误处理

推荐两段式：

1. 普通动作或等待动作收集错误上下文并输出给下游。
2. manual-confirm 动作展示错误和 options，操作员选择后调用 Bioyond 错误处理接口。

错误处理 action 要保留 Bioyond 要求的 token/ijk/option 等字段，并对 option 过期、缺字段、重复提交给出清楚错误。

### 8.4 报告和 Notebook 附件

如果要把 Bioyond 报告写入 Uni-Lab notebook，明确区分两组后端 API：Bioyond LIMS 报告 API 提供报告文件，Uni-Lab Lab/OSS API 负责把文件作为 Notebook 附件保存。

Bioyond LIMS 报告 API：

- 报告摘要：`POST {bioyond_api_host}/api/lims/order/order-report`
- 报告文件列表：`POST {bioyond_api_host}/api/lims/order/order-report-files`
- 常见 payload：`{"apiKey": "...", "requestTime": "...", "data": "ORDER_ID"}`
- 成功标准以 Bioyond 业务 envelope 为准，例如 `code == 1` 后读取 `data`；不要只看 HTTP status。
- 文件列表可能返回相对路径，写入 Notebook 前要按当前 `api_host` 转成可下载 URL。

Uni-Lab Notebook/OSS API：

- Lab API base URL 通常来自运行时 `HTTPConfig.remote_addr`，常见形态是 `https://.../api/v1`；下面的 `{lab_base_url}` 指这个后端 API base，不是浏览器页面 URL。
- 鉴权 header 的值是 `Lab ` 加 `BasicConfig.auth_secret()`。
- 读取 Notebook：`GET {lab_base_url}/lab/notebook/detail?uuid=NOTEBOOK_UUID`。
- 上传附件文件：`GET {lab_base_url}/lab/storage/token?scene=file&filename=FILENAME&content_type=MIME_TYPE`，再 `PUT` 文件字节到 token 返回的 signed URL。
- 保存完整 `lab_record`：`GET {lab_base_url}/lab/storage/token?scene=record&filename=RECORD_JSON_FILENAME&content_type=application/json&sub_path=NOTEBOOK_UUID`，再 `PUT` record JSON 到 signed URL。
- 写回 Notebook：当前外部 runtime helper 使用 `PATCH {lab_base_url}/lab/notebook/lab-record`，payload 包含 `uuid`、`lab_record=RECORD_PUBLIC_URL`、`lab_record_status=editing`。如果当前 Uni-Lab 后端 router 改为 `PATCH /api/v1/lab/notebook/content` 等新接口，按当前 router/client 替换并做 live API 测试。

实现规则：

- 从运行上下文解析 notebook/lab record ID；显式入参可覆盖。
- 拉取 order report files，区分 PDF、Excel、ZIP、图片等。
- ZIP 可能延迟生成；可短暂 retry，但不要无限等待。
- Notebook 附件节点使用 Lab OSS 返回的 `public_url`、`path`、`name`、`size`、`mimeType`，不要把 Bioyond 外部 URL 直接当内部附件。
- patch notebook `lab_record` 前先读取既有内容，追加块后整体保存，避免覆盖已有记录。
- 用 mocked HTTP 做离线测试，live 测试只验证少量安全样本。

---

## 9. 随包 vendored 代码与运行路径

外部设备包常遇到 installed Uni-Lab-OS 和 monorepo 最新代码不一致。可以 vendor，但要克制。

适合 vendor 的情况：

- 当前安装版缺少 Bioyond 必需行为。
- 上游 callback 分发/资源转换/RPC envelope 有已确认 bug。
- 迁移期间必须保持外部包在旧环境可运行。

vendor 要求：

- 在文件或 package doc 中写明为何 vendor、和上游差异是什么。
- 保持公共入口尽量与上游一致，方便未来删除 vendor。
- 对 vendor 差异写离线测试，尤其 callback route、material change、resource conversion。
- 运行路径使用 Uni-Lab working dir、配置项或 `cwd/unilabos_data`，不要依赖 monorepo 深度如 `Path(__file__).parents[4]`。
- debug monitor/ping-pong 类后台线程默认不要过于嘈杂；失败要可观测但不淹没日志。

---

## 10. 测试分层

### 10.1 AST/check-mode

外部设备包至少跑：

```bash
unilab --check_mode --devices ./<device_package_dir> --external_devices_only
```

确认：

- station/sub-device/resource 都能扫描。
- action metadata、handles、manual-confirm placeholder 不丢。
- graph `_resource_type` 可导入。

### 10.2 离线 pytest

离线契约测试应覆盖：

- decorator fallback/stub：没有完整 ROS/unilabos 环境也能检查 `_action_registry_meta`。
- action 参数过滤：未知字段跳过，`0`/`False`/空字符串按业务规则保留。
- 人工确认 metadata：`NodeType.MANUAL_CONFIRM`、goal defaults、handles、必填 label。
- PLR resource 构造/serialize/deserialize。
- callback service fake POST。
- report/notebook client mocked HTTP。
- runtime log path。

### 10.3 live read-only

测试当前部署事实：

- `device-list`
- warehouse/site/material type
- known order report
- known material lookup
- report files

### 10.4 live write/action

只在测试环境和明确批准后执行。每次记录输入、外部系统状态变化、Uni-Lab resource tree、callback/log/report 结果。

---

## 11. 完成检查表

- 已确认使用后端 Bioyond API，不使用前端 route。
- 已列出当前基类提供的方法，station-specific 方法有 explicit wrapper 或实现。
- 已用 live read-only API 验证设备 operation、仓库、物料类型、报告样本。
- RPC wrapper 检查业务 `code/message/data`，不只看 HTTP status。
- action schema 的 `Args:`、handles、必填标记、`Field(...)` 一致。
- manual-confirm 节点的展示数据、goal params、失败语义和 assignee 已验证。
- `@resource`/Deck/labware import-visible，必要 eager import 已存在。
- PLR 资源通过 `cls(name=...)`、`cls(**serialized)`、Deck with children 反序列化测试。
- resource sync 保留 identity，处理 virtual holding，发布正确 root，stale sweep 不删保留 root 下的 child。
- callback route 用 fake POST 验证确实调用 station 方法。
- 随包 vendored 代码有原因说明和覆盖测试。
- 外部设备包有 `--check_mode --devices ... --external_devices_only` 验证和离线 pytest。
