# HTTP API 全目录

本章按业务场景人工维护，覆盖本版离线全集中的 **134 个路径、163 个 HTTP 操作**，包括两条 SSE。
版本、部署角色与认证边界见 [入口](index.md) 和 [HTTP 规范](http.md)。
它不是自动导出的文件；机器 Schema、生成脚本和自动参考表只保留本地用于核对。

## 怎样查这份目录

方法和路径逐项列出，不用省略前缀的路径拼接规则。`{...}` 都是必填路径参数；调用时替换为真实身份并正确 URL 编码。
输入列的 `Q` 是 query，`B` 是 JSON body，`H` 是 header；`—` 表示除此路径参数外没有声明输入。
`?` 表示可省略，`=值` 标注默认值。没有列出的任意 query 不应视为接口支持的功能。

表中 `M<T>` 表示 **InventoryMutation 信封，其 payload 是 T**，不是直接发送 T。
operation、完整默认字段、幂等键和 MutationResult 见 [物料写请求](materials.md)。
其余 `B: 类型名` 为该类型的直接 JSON，不能外面再套一个 data。

未特别标注的成功通常为 HTTP 200。表中的“信封”指 `{code,data}`，必须进一步检查 code；
“直出”不经过此信封。错误与 201/202/204 例外见 [响应规则](http.md)。
OpenAPI 中 `{}` 响应 schema 不表示返回空对象，目录也不承诺每个角色都挂载全体路由。

## 平台状态、重启与诊断

面向运维和应用状态栏。`health` 只能说明服务/执行端状态，不能证明某台设备已就绪。
源码：[诊断路由](https://github.com/deepmodeling/Uni-Lab-OS/blob/862ce87a38222ac2d51739f2f89e5daecd3cd907/unilabos/server/api/runtime/diagnostics.py)、
[只读数据库检查](https://github.com/deepmodeling/Uni-Lab-OS/blob/862ce87a38222ac2d51739f2f89e5daecd3cd907/unilabos/server/api/debug.py)。

| 方法 | 路径 | 输入 | 返回与使用约束 |
| --- | --- | --- | --- |
| GET | `/api/v1/health` | — | 直出 status、scheduler、execution；execution 可为 ready/restarting/disabled |
| GET | `/api/v1/ping` | Q: client_timestamp?: number | 回显客户端时间与服务端秒级时间戳，不是任务心跳 |
| GET | `/api/v1/scheduler/resources` | — | 调度资源快照；本进程无调度权威时 503 |
| GET | `/api/v1/restart` | — | 当前重启请求状态 |
| POST | `/api/v1/restart` | B: RestartRequest | 登记重启并返回状态，不等于重启已完成 |
| DELETE | `/api/v1/restart` | — | 尝试取消待执行的重启，返回当前状态 |
| GET | `/api/v1/reset` | — | ResetStatus：supported、pending、confirmation_token、backup_path、detail；只预览 |
| POST | `/api/v1/reset` | B: ResetRequest | 202，ResetStatus；执行停机归档/重建，需显式确认 |
| GET | `/api/v1/debug/databases` | — | 四库状态及表目录/行数，只读 |
| GET | `/api/v1/debug/databases/{database}/tables/{table}` | Q: limit=50（1..500）、offset=0（>=0）、order?、descending=true | 列定义与分页数据；只允许已知库/表，不是任意 SQL 入口 |

RestartRequest 的 mode 默认 quiescent，可选 immediate；scope 默认 auto，可选 edge/process。
默认权威进程的 auto 只重启 Host，不能以 scope=process 把常驻权威也重启。
ResetRequest 必填 confirmation_token 和 confirmation，后者必须为 `清空全部数据`。
仅支持该能力的部署可执行 reset，不能在连接失败、演示初始化或示例脚本中自动调用。

## 驱动包与受管进程

这组接口在默认分离部署由 Host 处理。安装成功、进程启动和设备上线应分开显示。
源码：[包管理](https://github.com/deepmodeling/Uni-Lab-OS/blob/862ce87a38222ac2d51739f2f89e5daecd3cd907/unilabos/server/api/driver_packages.py)、
[随包图](https://github.com/deepmodeling/Uni-Lab-OS/blob/862ce87a38222ac2d51739f2f89e5daecd3cd907/unilabos/server/api/driver_package_graphs.py)、
[进程管理](https://github.com/deepmodeling/Uni-Lab-OS/blob/862ce87a38222ac2d51739f2f89e5daecd3cd907/unilabos/server/api/device_processes.py)。

| 方法 | 路径 | 输入 | 返回与使用约束 |
| --- | --- | --- | --- |
| GET | `/api/v1/driver-packages` | — | 台账、扫描目录及 restart_required 等状态 |
| GET | `/api/v1/driver-packages/catalog` | — | 合并后的可安装目录，不是已安装清单 |
| POST | `/api/v1/driver-packages/install` | B: DriverPackageInstallRequest | 202，后台 operation；继续查询操作结果 |
| GET | `/api/v1/driver-packages/operations` | — | 最近后台操作数组 |
| GET | `/api/v1/driver-packages/operations/{operation_id}` | — | 单次操作状态与日志，失败需展示原因 |
| DELETE | `/api/v1/driver-packages/{name}` | — | 202，卸载 operation；不承诺所有进程及关联物料已清理 |
| PUT | `/api/v1/driver-packages/{name}/enabled` | B: DriverPackageEnableRequest | 台账更新结果；检查是否要求重启 |
| GET | `/api/v1/driver-packages/{name}/graphs` | — | 随包可启动图目录 |
| GET | `/api/v1/driver-packages/{name}/graphs/{graph_name}` | — | 随包图的 node-link 正文 |
| POST | `/api/v1/driver-packages/{name}/graphs/{graph_name}/launch` | — | 启动/更新受管进程；同名已存在时可能更新并重启，不是只读导入 |
| GET | `/api/v1/device-processes` | — | 直出 `{hostlink, processes}` |
| POST | `/api/v1/device-processes` | B: DeviceProcessWrite | 201，创建后的受管进程记录 |
| GET | `/api/v1/device-processes/device-classes` | — | 注册表与包台账发现的设备类数组；不代表设备已在线 |
| GET | `/api/v1/device-processes/{process_id}` | — | 单进程规格与运行状态 |
| PUT | `/api/v1/device-processes/{process_id}` | B: DeviceProcessWrite | 更新规格；运行进程需按实际结果重启生效 |
| DELETE | `/api/v1/device-processes/{process_id}` | — | 204，无 JSON；删除进程管理项，不等于删除其全部实验历史 |
| POST | `/api/v1/device-processes/{process_id}/start` | — | 启动结果；已运行冲突可返回 409 |
| POST | `/api/v1/device-processes/{process_id}/stop` | — | 主动停止结果，不触发故障自动重启 |
| POST | `/api/v1/device-processes/{process_id}/restart` | — | 停止后重新启动结果 |

安装 body 为 spec（必填来源）、enable=true、upgrade=false、name=""；启停包 body 仅必填 enabled:boolean。
DeviceProcessWrite 必填 name，其余为 devices=[]、graph_nodes=null、devices_dirs=[]、package_names=[]、
external_only=false、auto_start=true、restart_policy="on-failure"、max_restarts=5、extra_args=[]。
graph_nodes 一旦提供优先于 devices，用于保存完整节点和 UUID；简化 devices 的每项必填 id、class，
可带 name=""、config={}、pose=null。不要对已有进程重新生成设备身份。

## 注册表与设备图、实验室布局

注册表负责能力版本，图记录拓扑，布局记录展示区域；三者不可相互替代。
源码：[注册表 API](https://github.com/deepmodeling/Uni-Lab-OS/blob/862ce87a38222ac2d51739f2f89e5daecd3cd907/unilabos/server/api/runtime/registry.py)、
[设备图 API](https://github.com/deepmodeling/Uni-Lab-OS/blob/862ce87a38222ac2d51739f2f89e5daecd3cd907/unilabos/server/api/materials/graph.py)、
[布局模型](https://github.com/deepmodeling/Uni-Lab-OS/blob/862ce87a38222ac2d51739f2f89e5daecd3cd907/unilabos/protocol/runtime/lab.py)。

| 方法 | 路径 | 输入 | 返回与使用约束 |
| --- | --- | --- | --- |
| GET | `/api/v1/registry/digest` | — | 信封；已持有内容摘要，用于上报前判定是否需正文 |
| POST | `/api/v1/resource-templates` | B: RegistryReport JSON，可 gzip | 信封；上报统计及 missing；路径虽叫 resource-templates，报告不只包含物料模板 |
| GET | `/api/v1/registry/entries` | Q: status="" | 信封 data.entries，可筛 active/pending/removed/unusable |
| GET | `/api/v1/registry/entries/{name}` | — | 信封；生效/挂起定义及冲突 |
| GET | `/api/v1/registry/entries/{name}/versions` | — | 信封 data.versions，版本历史 |
| GET | `/api/v1/registry/entries/{name}/versions/{version}` | version 为整数 | 信封；指定历史版本全文 |
| POST | `/api/v1/registry/entries/{name}/apply` | — | 信封；挂起版本生效，可能影响已有工作流 |
| POST | `/api/v1/registry/entries/{name}/dismiss` | — | 信封；忽略挂起版本，保留历史 |
| POST | `/api/v1/registry/entries/{name}/restore/{version}` | version 为整数 | 信封；以历史版本形成新的生效版本 |
| GET | `/api/v1/registry/pending-impacts` | — | 信封 data.impacts，待升级定义影响面 |
| GET | `/api/v1/registry/reports` | Q: page=1（>=1）、page_size=50（1..200） | 信封；上报批次分页 |
| GET | `/api/v1/registry/workflow-templates` | — | 信封 data.templates；尚未绑定设备的模板 |
| GET | `/api/v1/graphs` | Q: page=1（>=1）、page_size=100（1..1000）、name="" | 信封；设备图快照分页 |
| POST | `/api/v1/graphs` | B: GraphUpsertRequest | 信封；登记/更新设备图与相关物料；replace/adopt 语义不同 |
| GET | `/api/v1/graphs/live/payload` | — | 信封；从当前权威状态构建的实时拓扑，不是旧图快照 |
| GET | `/api/v1/graphs/{identity}` | identity 可为 uuid 或 name | 信封；图元数据 |
| GET | `/api/v1/graphs/{identity}/payload` | — | 信封；指定图快照的 node-link 正文 |
| DELETE | `/api/v1/graphs/{identity}` | — | 信封；删除图快照，不能当成通用物料删除 |
| GET | `/api/v1/lab/layout` | — | 直出 LabLayoutRead；从未保存时 revision=0 |
| PUT | `/api/v1/lab/layout` | B: LabLayoutWrite | 直出 LabLayoutRead；整份替换，版本过期返回 409 |
| DELETE | `/api/v1/lab/layout` | — | 204；清除保存的区域/围墙布局 |

RegistryReport 的 entries 必须包含 Host 当前完整条目目录，只让未命中哈希的正文增量上传。
仅发送“改变的几个名字”可能导致其他条目被软移除。该 handler 自行读取 Request，
所以 OpenAPI 没有 requestBody 不代表可发送空 body，完整约定见 [注册表上报](http.md)。

GraphUpsertRequest 必填 name、payload；可选 uuid、tags=[]、description、meta_data={}、device_site_templates，
on_existing 默认 replace，启动登记用 adopt。设备图与工作流图有不同合同，不接受互换。
LabLayoutWrite 必填 revision>=0、cell_size>0 且 <=100000，zones/walls 默认为空数组。
zone 有 id/name/color/cells；cells 和 walls 用 `col,row` 整数字符串。区域 ID 不重复，格子不能同时属于不同区域或围墙，
最多 200 个区域、100000 个格子。

## 工作流定义与 Python 编辑

所有下列结果使用 Workflow 信封，具体 DTO 和错误 code 见 [工作流章节](workflows.md)。
请求字段依据：[Workflow handler](https://github.com/deepmodeling/Uni-Lab-OS/blob/862ce87a38222ac2d51739f2f89e5daecd3cd907/unilabos/server/api/runtime/workflow.py)。

| 方法 | 路径 | 输入 | 返回与使用约束 |
| --- | --- | --- | --- |
| GET | `/api/v1/workflows` | Q: page=1、page_size=20、name="" | 工作流定义分页，不是运行任务列表 |
| POST | `/api/v1/workflows` | B: WorkflowCreateRequest | 成功实际 201；新建工作流元数据 |
| POST | `/api/v1/workflows/from-template` | B: WorkflowFromTemplateRequest | 成功实际 201，data.workflow；相同模板及 bindings 会复用稳定身份 |
| GET | `/api/v1/workflows/{workflow_uuid}` | — | 单个工作流定义及 revision |
| PUT | `/api/v1/workflows/{workflow_uuid}` | B: WorkflowUpdateRequest | 更新元数据，不是更新节点图 |
| DELETE | `/api/v1/workflows/{workflow_uuid}` | — | code=0 表示删除成功；不是 204 |
| GET | `/api/v1/workflows/{workflow_uuid}/graph` | — | 图节点与连线；编辑前读取当前定义 |
| PUT | `/api/v1/workflows/{workflow_uuid}/graph` | B: GraphWriteRequest | 按 revision 全图协调，浏览器草稿须 preserve |
| GET | `/api/v1/workflows/{workflow_uuid}/authoring` | — | 草稿、编译候选及诊断状态 |
| PUT | `/api/v1/workflows/{workflow_uuid}/authoring/draft` | B: DraftWriteRequest | 保存 Python 草稿；不是直接执行脚本 |
| POST | `/api/v1/workflows/{workflow_uuid}/authoring/apply` | B: ApplyRequest | 校验草稿/候选哈希和图版本后应用 |

创建/更新 body 必填 name，可带 tags=[]、description=null、meta_data={}、workflow_uuid=null。
从模板创建必填 template_uuid，bindings 默认 {}、name 默认 null、site_binding_mode 默认 resolve。
浏览器必须按交互明确使用 preserve，确认后提交真实 Site UUID。

GraphWriteRequest 必填 revision（严格整数 >=1），nodes/edges 默认空数组，site_binding_mode 默认 resolve。
空数组有替换语义，不是“保持原节点不变”。DraftWriteRequest 必填 python_source、expected_draft_hash
（首次允许 null，但不能省略字段）、expected_workflow_revision；ApplyRequest 必填同名图版本和草稿哈希、
expected_candidate_hash。非空哈希形如 `sha256:` 加 64 个小写十六进制字符。

## 任务、单点、步进、结果与确认

下列接口仍使用 Workflow 信封。前端提交动作应走 task，不直接创建 runtime job。
带 limit 的四类历史/确认列表允许省略 limit；提供时 >=1，offset 默认 0 且 >=0。

| 方法 | 路径 | 输入 | 返回与使用约束 |
| --- | --- | --- | --- |
| GET | `/api/v1/workflow-tasks` | Q: page=1、page_size=20、workflow_uuid?、status=""、cleanup_status="" | 任务分页 |
| POST | `/api/v1/workflow-tasks` | B: WorkflowTaskCreateRequest | 成功实际 201，data.uuid 为 task 身份；已接受不等于成功终态 |
| GET | `/api/v1/workflow-tasks/{task_uuid}` | — | 任务快照、进度与 control_revision |
| POST | `/api/v1/workflow-tasks/{task_uuid}/commands` | B: WorkflowTaskCommandRequest | step/resume/pause/cancel 的处理结果；冲突需重新读取任务 |
| GET | `/api/v1/workflow-tasks/{task_uuid}/node-runs` | — | 逻辑节点运行集合 |
| GET | `/api/v1/workflow-node-runs/{run_uuid}` | — | 单个逻辑节点运行及尝试关联 |
| GET | `/api/v1/workflow-tasks/{task_uuid}/jobs` | — | 该任务的物理尝试集合 |
| GET | `/api/v1/workflow-node-jobs/{job_uuid}` | — | 单次 job 的运行信息 |
| GET | `/api/v1/workflow-node-jobs/{job_uuid}/feedback-history` | Q: limit?、offset=0 | 该尝试反馈历史 |
| GET | `/api/v1/workflow-node-jobs/{job_uuid}/results` | Q: limit?、offset=0 | 该尝试结果历史，不直接覆盖原失败 |
| GET | `/api/v1/workflow-tasks/{task_uuid}/interventions` | Q: limit?、offset=0 | 任务干预记录 |
| GET | `/api/v1/workflow-tasks/{task_uuid}/manual-confirmations` | Q: limit?、offset=0 | 任务内人工确认单 |
| GET | `/api/v1/workflow-manual-confirmations/{confirmation_uuid}` | — | 指定人工确认单 |
| POST | `/api/v1/workflow-manual-confirmations/{confirmation_uuid}/decision` | B: ManualConfirmationDecisionRequest | 确认决策，由调度器后续消费 |
| POST | `/api/v1/workflow-tasks/{task_uuid}/manual-confirmations/{confirmation_uuid}/decision` | B: ManualConfirmationDecisionRequest | 同上，并检查确认单属于该 task |

WorkflowTaskCreateRequest 默认 execution_kind=workflow、run_mode=normal，整图必须提供 workflow_uuid。
run_mode=step 创建暂停任务；single_node 还需 target_node_uuid。设备单点用 execution_kind=ad_hoc_device_action，
提供 device_id、action_name、param；可携带 action_type、execution_policy、execution_timeout_seconds、idempotency_key。
公共可选元数据为 description 和 meta_data。图任务的输入来自图节点，不能用未声明的 task.input 任意覆盖。

WorkflowTaskCommandRequest 三个字段均必填：type=step/resume/pause/cancel、expected_revision（严格整数 >=0）、
idempotency_key（1..200 字符）。expected_revision 取任务的 control_revision，不是工作流 revision。
ManualConfirmationDecisionRequest 必填 action，可带 confirmed_by、comment、decision_idempotency_key；
确认人还受该单据的 assignee 约束。完整交互及错误处理见 [运行控制](workflows.md)。

## 动作错误与设备状态异常

与 Workflow 人工确认不同，这组接口直出字典。源码在前述诊断路由。

| 方法 | 路径 | 输入 | 返回与使用约束 |
| --- | --- | --- | --- |
| GET | `/api/v1/error-decisions` | — | `{items:[...]}`；失败闸门及重启对账所需决策；没有 `/pending` 子路由 |
| POST | `/api/v1/error-decisions/{decision_id}` | B: ErrorDecision | `{decision_id,status:"resolved"}`；过期/拒绝可返回 409 |
| GET | `/api/v1/status-incidents` | Q: device_id=""、include_terminal=false | `{host_ready,incidents,holds}`；状态策略不可用时 503 |
| POST | `/api/v1/status-incidents/{incident_id}` | B: StatusIncidentDecision | incident 的 ack；不存在 404、冲突 409、无效选择 422 |

ErrorDecision 的 action 默认 abort，option 默认 null、reason 默认空、result 默认 null；
还声明 scheduler_updated=true、job_id/device_id 为空、extra={}。浏览器按待决策项给出的选项提交，
不要伪造调度状态；人工替换结果优先使用顶层 result，避免与 option/extra 中的数据互相覆盖。
StatusIncidentDecision 为 action=""、option=null、reason=""；按 incident 当前允许的操作选择。
字段有默认值不意味着后端会接受任何决定，业务状态与策略仍需校验。

## 物料模板、实例、位置与内容

读请求直出 DTO/数组；`M<T>` 写成功为 MutationResult，T 的字段不是全部在 OpenAPI 外壳中展开。
核心返回是 MaterialAggregateRead 或 MaterialTreeRead，具体结构见 [物料模型与幂等](materials.md)。
源码：[物料 HTTP handler](https://github.com/deepmodeling/Uni-Lab-OS/blob/862ce87a38222ac2d51739f2f89e5daecd3cd907/unilabos/server/api/materials/core.py)、
[全部物料请求模型](https://github.com/deepmodeling/Uni-Lab-OS/blob/862ce87a38222ac2d51739f2f89e5daecd3cd907/unilabos/protocol/materials.py)。

| 方法 | 路径 | 输入 | 返回与使用约束 |
| --- | --- | --- | --- |
| GET | `/api/v1/materials/templates` | Q: name?（精确）、include_definition=false | 模板目录数组；默认不携带大体积 definition |
| POST | `/api/v1/materials/templates` | B: `M<ResourceTemplateWrite>` | 新建模板，权威分配 UUID |
| GET | `/api/v1/materials/templates/{template_uuid}` | — | 单个模板完整信息 |
| PUT | `/api/v1/materials/templates/{template_uuid}` | B: `M<ResourceTemplateWrite>` | 更新模板，路径与 payload 身份一致 |
| DELETE | `/api/v1/materials/templates/{template_uuid}` | B: `M<{template_uuid}>` | 删除模板；引用约束仍需满足 |
| GET | `/api/v1/materials/registry-classes` | — | 可通过 registry 实例化的资源类目录 |
| POST | `/api/v1/materials/instantiate` | B: `M<MaterialInstantiate>` | registry_class/name 必填、barcode 可选；权威展开创建树 |
| POST | `/api/v1/materials/trees` | B: `M<MaterialTreeCreate>` | 单根、非空且父先于子的创建树；返回含 client_ref_map 的树 |
| GET | `/api/v1/materials/instances` | Q: roots_only=false、name?（精确） | 物料聚合数组；未找到名称返回 [] |
| GET | `/api/v1/materials/instances/by-resource-id/{resource_id}` | — | 按逻辑 resource_id 查询单个聚合 |
| GET | `/api/v1/materials/instances/{material_uuid}` | — | 按权威 UUID 查询单个聚合 |
| PATCH | `/api/v1/materials/instances/{material_uuid}` | B: `M<MaterialPatch>` | 修改名称/条码等标识或生命周期字段，不是任意整树覆盖 |
| DELETE | `/api/v1/materials/instances/{material_uuid}` | B: `M<MaterialDelete>` | 删除物料；recursive 与身份按 payload 校验，释放关联位点 |
| PUT | `/api/v1/materials/instances/{material_uuid}/data` | B: `M<MaterialDataWrite>` | 写内容物及业务数据，不改变空间关系 |
| PUT | `/api/v1/materials/instances/{material_uuid}/position` | B: `M<MaterialPosition>` | 写空间/尺寸数据，不等同于跨设备转运 |
| GET | `/api/v1/materials/instances/{material_uuid}/tree` | — | 一致性树快照，含 snapshot_sequence/state_hash |
| POST | `/api/v1/materials/move` | B: `M<MaterialMove>` | 改变权威挂载/位点关系；不是单纯改坐标 |
| POST | `/api/v1/materials/transfer` | B: `M<MaterialTransfer>` | 权威移交后同步来源 unload/目标 load；设备失败时不可假定事务未提交 |
| POST | `/api/v1/materials/notify-device` | B: ResourceTreeNotify | 直出 `{notified:...}`，仅通知已有权威变化，不创建/移动物料 |
| GET | `/api/v1/materials/links` | Q: material_uuid=""、source_material_uuid=""、target_material_uuid=""、link_type? | 拓扑边数组；连接不等于父子挂载 |
| POST | `/api/v1/materials/links` | B: MaterialLinkUpsert | 直出边记录；不是 InventoryMutation 外壳 |
| DELETE | `/api/v1/materials/links/{link_uuid}` | — | 直出 `{deleted:true}` |

DELETE 模板与 DELETE 物料都带 JSON body，不能由通用客户端丢掉。
模板删除 payload 按路径构造 `{template_uuid:实际UUID}`；MaterialDelete 则按其模型填写，不能互换。
MaterialLinkUpsert 包含 source_material_uuid、target_material_uuid、link_type、source_handle、target_handle、extra；
ResourceTreeNotify 为 device_id、action、resource_uuids，动作值及设备回读流程见物料章。

## 按量库存、预留、快照与变更账本

库存不足应在执行前处理；预留、消耗、释放、隔离各自是独立的幂等业务操作。
同一 task 批量预留失败应整体检查，不让部分设备在缺料情况下继续执行。

| 方法 | 路径 | 输入 | 返回与使用约束 |
| --- | --- | --- | --- |
| GET | `/api/v1/materials/lots` | Q: template_uuid?、unit?、include_quarantined=false | 库存批次数组；单位和隔离状态参与筛选 |
| POST | `/api/v1/materials/lots/inbound` | B: `M<InventoryLotInbound>` | 入库/补量的 MutationResult |
| GET | `/api/v1/materials/lots/{lot_uuid}` | — | 指定批次 |
| GET | `/api/v1/materials/reservations` | Q: task_uuid?、status? | 库存预留数组，不是调度物料锁数组 |
| POST | `/api/v1/materials/reservations` | B: `M<InventoryReservationCreate>` | 创建单次库存预留 |
| POST | `/api/v1/materials/reservations/batch` | B: `M<InventoryTaskReservationCreate>` | task 批量预留；按整体事务处理 |
| GET | `/api/v1/materials/reservations/by-job/{job_uuid}` | — | 查询 job 对应预留 |
| GET | `/api/v1/materials/reservations/{reservation_uuid}` | — | 单个预留记录 |
| POST | `/api/v1/materials/reservations/{reservation_uuid}/consume` | B: `M<InventoryReservationTransition>` | 消耗预留，不能用普通数据更新代替 |
| POST | `/api/v1/materials/reservations/{reservation_uuid}/release` | B: `M<InventoryReservationTransition>` | 释放预留；不能把已消耗量重复退回 |
| POST | `/api/v1/materials/reservations/{reservation_uuid}/quarantine` | B: `M<InventoryReservationTransition>` | 隔离相关库存，保留原因及关联 |
| POST | `/api/v1/materials/snapshots/apply` | B: `M<MaterialSnapshot>` | 应用快照；不是所有对象的任意覆盖入口 |
| POST | `/api/v1/materials/snapshots/compare` | B: MaterialSnapshot | 只比较，不写账本；不套 Mutation |
| POST | `/api/v1/materials/snapshots/delta` | B: `M<MaterialDelta>` | 应用差量并产生账本记录 |
| GET | `/api/v1/materials/changes` | Q: after_sequence=0（>=0）、limit=100（1..1000） | 增量账本，供同步消费者补读 |
| POST | `/api/v1/materials/changes/ack` | B: LedgerAcknowledge | `{acknowledged:数量}`；through_sequence>=0，普通浏览器不替同步消费者 ACK |
| GET | `/api/v1/materials/events` | H: Last-Event-ID? | text/event-stream；物料变更提醒，收到后拉取正文 |

材料写入失败常见 404（目标不存在）、409（幂等/版本/占用等冲突）、422（模型或操作不合法）；
transfer 还需识别设备投影失败。不要收到错误后直接改数据库或更换幂等键重放物理动作。

## Runtime：会话、在线能力与执行生命周期

面向执行适配器和诊断工具。下面的“job”是执行层记录，不能替代前端提交 workflow-task 的业务入口。
所有列表的 limit 默认 100，范围 1..1000；标有 after_sequence 的接口默认 0 且 >=0。
其他 `?` 筛选默认不设。返回直出，未找到 404、状态冲突 409、协议无效 422。

源码：[Runtime 路由](https://github.com/deepmodeling/Uni-Lab-OS/blob/862ce87a38222ac2d51739f2f89e5daecd3cd907/unilabos/server/api/runtime/data.py)、
[请求模型](https://github.com/deepmodeling/Uni-Lab-OS/blob/862ce87a38222ac2d51739f2f89e5daecd3cd907/unilabos/protocol/runtime/data.py)、
[记录模型](https://github.com/deepmodeling/Uni-Lab-OS/blob/862ce87a38222ac2d51739f2f89e5daecd3cd907/unilabos/server/database/tables/runtime/data.py)。

| 方法 | 路径 | 输入 | 返回与使用约束 |
| --- | --- | --- | --- |
| GET | `/api/v1/runtime/sessions` | Q: edge_uuid?、state?、limit | 会话记录数组 |
| GET | `/api/v1/runtime/sessions/{session_uuid}` | — | 单会话记录 |
| PUT | `/api/v1/runtime/sessions/{session_uuid}` | B: BackendSessionUpsert | 状态机 upsert，路径与 body session_uuid 一致 |
| GET | `/api/v1/runtime/endpoints` | Q: transport?、state?、host_uuid?、limit | endpoint 记录数组，含实际路由/能力 |
| GET | `/api/v1/runtime/endpoints/{endpoint_uuid}` | — | 单 endpoint 快照 |
| PUT | `/api/v1/runtime/endpoints/{endpoint_uuid}/snapshot` | B: EndpointSnapshotUpsert | EndpointSnapshotResult；完整能力快照，路径与身份一致 |
| GET | `/api/v1/runtime/commands` | Q: session_uuid?、status?、job_uuid?、command_type?、after_sequence、limit | command inbox 记录数组 |
| POST | `/api/v1/runtime/commands` | B: CommandEnvelope | 幂等接收命令；不是允许浏览器随意执行的 action 接口 |
| GET | `/api/v1/runtime/commands/{command_uuid}` | — | 命令接收记录，不等于 HTTP 命令正文下载 |
| GET | `/api/v1/runtime/jobs` | Q: status?、device_uuid?、endpoint_uuid?、retry_of_job_uuid?、attempt_group_uuid?、limit | 执行记录数组；用于关联 retry 尝试 |
| POST | `/api/v1/runtime/jobs` | B: ExecutionJobCreate | 创建执行记录；需完整调度命令身份 |
| GET | `/api/v1/runtime/jobs/{job_uuid}` | — | 单个 ExecutionJobRecord |
| POST | `/api/v1/runtime/jobs/{job_uuid}/transitions` | B: ExecutionJobTransition | 版本校验后推进合法状态，不是任意 PATCH |
| POST | `/api/v1/runtime/jobs/{job_uuid}/feedback` | B: ExecutionJobFeedback | 推进反馈序号；正文关联走历史/payload 合同 |
| POST | `/api/v1/runtime/jobs/{job_uuid}/cancel` | B: ExecutionJobCancel | 记录取消请求及适配器命令，不承诺物理动作已停止 |
| POST | `/api/v1/runtime/jobs/{job_uuid}/error-gate/open` | B: ErrorGateOpen | 打开终态闸门并登记错误上下文 |
| POST | `/api/v1/runtime/jobs/{job_uuid}/error-gate/decision` | B: ErrorGateDecision | 按调度版本决定闸门处理，不是浏览器 ErrorDecision 模型 |

会话必填 session_uuid、edge_uuid、backend_uri、authority_epoch、connection_epoch、state；state 为
connecting/active/reconciling/disconnected。command_cursor/event_send_cursor/event_ack_sequence 默认 0；
ACK 不能超过发送进度，disconnected 状态必须有 disconnected_at_ms。时间字段以毫秒记录。

endpoint 必填 endpoint_uuid、transport=hostlink/ros2、host_uuid、instance_name、authority_epoch、
state=online/offline/reconciling；可携带 adapter_epoch、reconciliation_generation、device_routes、action_capabilities、
config、reconciled_at_ms、observed_at_ms。不要用部分数组更新拼出混合 epoch 的快照。

CommandEnvelope 必填 command_uuid、session_uuid、backend_sequence>=1、command_type、payload_sha256；
可带 job_uuid、payload_uuid、summary、traceparent、received_at_ms。具体 execute_job 正文必须经过控制协议模型校验，
不等于把一个 action_name 放进 summary 就能执行。

ExecutionJobCreate 的核心必填字段为 job_uuid、task_uuid、node_uuid、attempt_group_uuid、execute_command_uuid、
device_uuid、action_name、action_payload_uuid、scheduler_revision；attempt_no 默认 1，attempt_trigger 默认 initial。
retry_of_job_uuid、route_uuid、endpoint_uuid、transport、material_bindings、accepted_at_ms 依实际执行填写。
重试形成新的 job；不覆盖原 job_uuid。

| 推进模型 | 必填字段与主要约束 |
| --- | --- |
| ExecutionJobTransition | expected_version>=1、status；可带 scheduler_status_version、feedback_sequence、result_uuid、错误与发生时间 |
| ExecutionJobFeedback | expected_version>=1、feedback_sequence>=1；observed_at_ms 默认 0 |
| ExecutionJobCancel | expected_version>=1、cancel_command_uuid、adapter_command_uuid；可带 payload_uuid、requested_at_ms |
| ErrorGateOpen | expected_version、error_uuid、error_code、error_summary、required_scheduler_revision、request_event_uuid；可带 detail_payload_uuid、summary、opened_at_ms |
| ErrorGateDecision | expected_version、decision_command_uuid、action、confirmed_scheduler_revision、adapter_command_uuid；可带 payload_uuid、result_uuid、decision、resolved_at_ms |

ExecutionJobTransition 的状态枚举为 dispatch_pending/dispatched/running/terminal_waiting/succeeded/failed/canceled/
execution_unknown/rejected；枚举合法不表示任意状态之间都能跳转。
这里 ErrorGateDecision.action 是 release_failed/replace_result/cancel，**不是**浏览器选项 retry/skip/abort，
也不能把控制命令的 resume_pending 强塞进这个 HTTP DTO；不同层的模型须分别使用。

## Runtime：适配器命令和 Backend 事件队列

以下列表共用上一节 limit/after_sequence 的默认和范围。claim 会改变租约，ack 会推进状态；
它们是消费者操作，不是日志页面的“已读”按钮。

| 方法 | 路径 | 输入 | 返回与使用约束 |
| --- | --- | --- | --- |
| GET | `/api/v1/runtime/adapter-commands` | Q: endpoint_uuid?、status?、job_uuid?、after_sequence、limit | 适配器命令记录数组 |
| POST | `/api/v1/runtime/adapter-commands` | B: AdapterCommandEnqueue | 幂等入队记录 |
| GET | `/api/v1/runtime/adapter-commands/{adapter_command_uuid}` | — | 单个适配器命令记录 |
| POST | `/api/v1/runtime/adapter-commands/claim` | B: AdapterCommandClaim | 为指定 endpoint 领取命令并建立租约 |
| POST | `/api/v1/runtime/adapter-commands/ack` | B: AdapterCommandAck | 确认单条命令，返回更新记录 |
| GET | `/api/v1/runtime/backend-events` | Q: status?、job_uuid?、aggregate_type?、aggregate_uuid?、after_sequence、limit | 待汇报 Backend 的事件记录数组 |
| POST | `/api/v1/runtime/backend-events` | B: BackendEventEnqueue | 幂等追加事件 |
| GET | `/api/v1/runtime/backend-events/{event_uuid}` | — | 单个 outbox 事件，不是 history 事件路由 |
| POST | `/api/v1/runtime/backend-events/claim` | B: BackendEventClaim | 为 session 领取事件并建立租约 |
| POST | `/api/v1/runtime/backend-events/ack` | B: BackendEventAck | `{acknowledged:数量}`；通过 through_sequence 推进 ACK |

AdapterCommandEnqueue 必填 adapter_command_uuid、endpoint_uuid、command_type，可关联 job_uuid、source_command_uuid、
trigger_event_uuid、target_adapter_epoch、payload_uuid 和 available_at_ms。AdapterCommandClaim 必填 endpoint_uuid，
now_ms=0、lease_ms=30000（>0）、limit=100（1..1000）；Ack 必填 adapter_command_uuid、ack_event_uuid，
acknowledged_at_ms=0。

BackendEventEnqueue 必填 event_uuid、event_type、aggregate_type、aggregate_uuid、aggregate_version>=1；
可带 job_uuid、summary、detail_payload_uuid、traceparent、tracestate、available_at_ms。
BackendEventClaim 必填 session_uuid，其他领取字段默认值与适配器 claim 相同；
BackendEventAck 必填 session_uuid、through_sequence>=0，acknowledged_at_ms=0。不要交叉使用两个队列的序号或租约。

## Telemetry：设备状态与遥测

本域直出 DTO/数组。设备 latest 状态不等于任务状态；来源 epoch/generation/sequence 决定事件顺序。
源码：[API](https://github.com/deepmodeling/Uni-Lab-OS/blob/862ce87a38222ac2d51739f2f89e5daecd3cd907/unilabos/server/api/telemetry.py)、
[字段模型](https://github.com/deepmodeling/Uni-Lab-OS/blob/862ce87a38222ac2d51739f2f89e5daecd3cd907/unilabos/protocol/telemetry.py)。

| 方法 | 路径 | 输入 | 返回与使用约束 |
| --- | --- | --- | --- |
| GET | `/api/v1/telemetry/events` | Q: after_sequence=0、limit=100、endpoint_uuid?、device_uuid?、event_type?、source_epoch?、source_generation?、observed_from_ms?、observed_to_ms? | TelemetryEventRecord 数组；limit 1..1000，序号/代次/毫秒非负 |
| POST | `/api/v1/telemetry/events` | B: TelemetryIngestRequest | TelemetryIngestResult：accepted、replayed、event、cursor、device_state |
| GET | `/api/v1/telemetry/events/{event_uuid}` | — | 单个遥测事件 |
| GET | `/api/v1/telemetry/sources/{endpoint_uuid}/cursor` | — | 来源游标；浏览器不自行推进来源顺序 |
| GET | `/api/v1/telemetry/states` | Q: endpoint_uuid? | DeviceStateLatestRecord 数组 |
| GET | `/api/v1/telemetry/states/{endpoint_uuid}/{device_uuid}` | — | 指定设备最新状态，不是所有历史样本 |

event_type 为 state/property_sample/connection/alarm。TelemetryIngestRequest 使用 protocol_version=telemetry.v1、
event 和可选 device_state。快照与事件的设备身份必须一致；新到达但来源顺序更旧的上报不能简单覆盖状态。
state、properties、connection_state、alarms 使用公共 JSON 字段，不要求调用者知道数据库 `_json` 列名。

## History：不可变正文与追加记录

本域不提供通用的覆盖/删除历史。人工替换通过新事件指向旧事件，保留审计链。
源码：[API](https://github.com/deepmodeling/Uni-Lab-OS/blob/862ce87a38222ac2d51739f2f89e5daecd3cd907/unilabos/server/api/history.py)、
[字段模型](https://github.com/deepmodeling/Uni-Lab-OS/blob/862ce87a38222ac2d51739f2f89e5daecd3cd907/unilabos/protocol/history.py)。

| 方法 | 路径 | 输入 | 返回与使用约束 |
| --- | --- | --- | --- |
| GET | `/api/v1/history/events` | Q: after_sequence=0、limit=100、event_types=[]、job_uuid?、endpoint_uuid?、device_uuid?、event_key?、occurred_from_ms?、occurred_through_ms? | HistoryEventRecord 数组；limit 1..1000，序号/毫秒非负；event_types 用重复 query 键 |
| POST | `/api/v1/history/events` | B: HistoryEventAppend | HistoryEventRecord，服务端分配账本 sequence |
| GET | `/api/v1/history/events/{event_uuid}` | — | 单个 HistoryEventRecord |
| POST | `/api/v1/history/events/{event_uuid}/replacement` | B: ManualResultReplacement | 新替换事件；supersedes_event_uuid 必须等于路径身份 |
| GET | `/api/v1/history/events/{event_uuid}/replacement-chain` | — | HistoryEventRecord 数组，完整替换链 |
| POST | `/api/v1/history/payloads` | B: InlinePayloadWrite 或 ExternalPayloadWrite | PayloadObjectRead；按 storage_kind 区分正文与外部引用 |
| GET | `/api/v1/history/payloads/{payload_uuid}` | — | PayloadObjectRead；缺失不是空内容 |

事件类型为 job_transition/action_availability/job_feedback/job_result/job_log/error_snapshot/decision_audit。
HistoryEventAppend 关联 job/endpoint/device、payload、版本和来源；人工替换保留 actor 与原始事件。
inline 的 HTTP JSON 正文是 Base64，解码后最多 262144 字节；external 使用 external_uri、sha256 和 byte_length，
不代表服务器会自动抓取该地址。二进制编码、时间与返回字段见 [History 说明](http.md)。

## 观察事件、Host/Slave 信息与日志

日志使用“提醒 + 按 source 游标读取”，不能让两个页面共用一个会前移的消费游标。
完整事件形状及重连流程见 [实时协议](realtime.md)。源码：[日志 API](https://github.com/deepmodeling/Uni-Lab-OS/blob/862ce87a38222ac2d51739f2f89e5daecd3cd907/unilabos/server/api/runtime/logs.py)。

| 方法 | 路径 | 输入 | 返回与使用约束 |
| --- | --- | --- | --- |
| GET | `/api/v1/events` | H: Last-Event-ID? | text/event-stream；工作流持久事件和 runtime.logs.changed 轻通知，不是 WS |
| GET | `/api/v1/hostlink/peers` | — | role、peers、client 及已协商的 Host 信息；设备列表与进程台账不同 |
| GET | `/api/v1/hostlink/log-sources` | — | Host/Slave 日志源目录，选源后读取日志 |
| GET | `/api/v1/hostlink/logs` | Q: source_id（必填，1..512 字符）、cursor=""、limit=300（1..1000） | 有界日志批次及游标；处理 reset/truncated/has_more，直到追平 |

非空日志 cursor 为 24 位小写十六进制、冒号、1..16 位十进制序号，不要自行拼接或与 SSE 事件 ID 混用。
日志轻通知未必有持久事件 ID；重连后重新读源与快照，不能承诺日志无限历史可重放。

## 内部 HTTP 与非 HTTP 通道

以下四个 HTTP 操作未计入上面的 163 项业务全集；它们服务于权威与 Host 的内部协作。
未列入离线导出不表示允许忽略协议校验，也不表示浏览器应改用这些入口。

| 方法 | 路径 | 输入 | 返回与使用约束 |
| --- | --- | --- | --- |
| GET | `/edge/commands/{command_uuid}` | 路径身份 | BackendCommandDocument；注意没有 `/api/v1` 前缀；未找到 404，服务未就绪 503 |
| POST | `/api/v1/edge/http-responses/{request_uuid}` | B: EdgeHttpResponse | `{accepted:bool}`；path/body request_uuid 必须一致 |
| POST | `/api/v1/hostlink/material-sync` | B: MaterialDeviceSync | 设备服务结果；unload/load 失败返回 409，不能直接当权威移位接口 |
| POST | `/api/v1/hostlink/notify-device` | B: ResourceTreeNotify | ResourceTreeNotifyResult；转发已发生的权威物料变更 |

内部 HTTP 来源：[控制入口](https://github.com/deepmodeling/Uni-Lab-OS/blob/862ce87a38222ac2d51739f2f89e5daecd3cd907/unilabos/server/api/runtime/control.py)、
[设备下行中继](https://github.com/deepmodeling/Uni-Lab-OS/blob/862ce87a38222ac2d51739f2f89e5daecd3cd907/unilabos/server/api/host_relay.py)。

另有 `/api/v1/ws/schedule` 控制 WebSocket、HostLink TCP RPC，以及 `/mcp` 的 Streamable HTTP/JSON-RPC。
它们不是新增几条普通业务 CRUD；消息类型、握手和超时见 [实时协议](realtime.md)，
MCP 的会话、工具分类与权限见 [MCP 接入](python-cli-mcp.md)。
管理站还有 `/api/docs`、`/api/redoc`、`/api/openapi.json` 文档端点，不纳入上述业务数量。

## 请求模型的查阅与维护

每个 body 类型的字段定义来自本章所链接的固定版本模型/handler，在运行中也可查看 `/api/docs` 的 Schemas。
物料嵌套 payload、跨字段验证、默认值和返回转换不应只根据 OpenAPI 的字典外壳猜测。
本章负责路由全集和接入要点；业务章负责字段组合、示例及失败恢复，两者要一起使用。

更新本章时在本地比对 method/path、body 类型和 query/header 名称，缺项、多项或旧路径都必须检查。
尤其注意自读 Request 的 resource-templates、带 body 的 DELETE、动态 201 与 code 信封，
以及 `/edge/commands` 不带公共前缀这些容易被 SDK 生成器遗漏的接口。
