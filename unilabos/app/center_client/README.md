# 中心客户端与持久事件

备份/恢复使用随中心维护包分发的 `tools/recovery_backup.py`，通过本模块公共恢复 CLI 进入只补证据模式。
须停写并同时覆盖工作流、库存及部署其他持久库；只恢复到新目录，不能覆盖原库。
完整清单格式、资产加密边界和 PG 延期验证说明见中心维护文档 `docs/backup-restore.md`。
恢复 CLI 可显式指定 `--database-type sqlite`；PG 使用 `--database-type postgresql --database-url-env <变量名>`，连接串不进入命令行。

工站仅在 `CENTER_ENABLED=1` 时启动中心轮询；连接配置使用 `CenterClientConfig` 的
`CENTER_*` 环境变量。运行事实、命令收件箱和事件发件箱共享 `workflow_history.db`
的存储选择，支持 SQLite 和 PostgreSQL。普通重启保留事件纪元，显式重置整个工作流
事实库才会生成新纪元。新进程始终使用新 process UUID；历史会话只能用于原 owner 补报。

工站如果需要让中心通过 HTTP 主动查询任务，设置 `CENTER_HTTP_BASE_URL`，例如
`http://192.168.1.20:8002`。它必须是中心能够访问的 HTTP origin，只包含协议、主机和端口，
不能带路径、凭据或查询参数。WSS 连接建立时，客户端会把这个地址放进 `hello.http_base_url`；
中心会保存到工站记录，供实验节点查询使用。未配置时仍可建立中心连接和执行任务，但中心没有工站
HTTP 地址，无法执行依赖该地址的节点明细查询。地址通过 WSS hello 上报，因此关闭
`CENTER_WSS_ENABLED` 时，中心不会得到这个地址，节点明细查询也不可用。

## 操作台访问地址

浏览器操作台使用 `public_http_base_url`，与中心访问后端的 `http_base_url` 分开。
地址优先级为：本地自定义值 → `CENTER_PUBLIC_HTTP_BASE_URL`（包括 station.yaml 的
`center.publicHttpBaseUrl`）→ 本次运行由工站页面提交的浏览器 origin → Launcher 本次启动的候选
→ 之前保存的浏览器候选。重启后旧浏览器端口不会覆盖 Launcher 本次绑定的实际端口。
没有候选时保留空值，不猜测前端端口。HTTP/HTTPS、域名/IP 和可选端口均可；首期不接受
子路径、凭据、查询参数、片段或 `0.0.0.0`/`::` 通配地址。

`GET /api/v1/center-connection` 新增 `data.console_address`，也可独立读取
`GET /api/v1/center-connection/console-address`。返回：

```json
{
  "mode": "auto",
  "custom_url": null,
  "automatic_url": "http://192.168.1.20:4183",
  "automatic_source": "browser",
  "effective_url": "http://192.168.1.20:4183",
  "source": "browser",
  "sync_state": "synced",
  "error_code": null
}
```

来源为 `custom`、`deployment`、`browser`、`launcher` 或 `none`；`automatic_source`
不含 `custom`。页面以 `automatic_url` 为占位文本，以 `custom_url` 为输入值。
保存时发送 `POST /api/v1/center-connection/console-address`，请求头沿用
`X-Unilab-Local-Request: 1`，请求体为：

```json
{"custom_url": null, "browser_url": "http://192.168.1.20:4183"}
```

`custom_url` 必须显式提供，空串或 `null` 清除覆盖并恢复自动；`browser_url` 可省略，
省略时保留之前的浏览器候选。前端只传自己的 `window.location.origin`，不传业务路由
或外层中心地址。首次配对也可在原请求中附加相同结构的 `console_address`；省略时
保持原设置。设置按工站实例保存到连接目录的 `console-address.json`，续证不改写此文件。

保存返回只确认本地写盘；`sync_state=pending` 表示尚未获中心确认，`synced` 必须由
携带当前生效地址的 WSS `hello_ack` 确认。配置文件读取异常时为 `error`。在线更改会
唤醒连接管理器，复用原身份、证书和控制会话重新建立 WSS；断线重连后补报。同步状态
只表示中心收到了地址，不证明用户浏览器可访问，不能用后端的 `http_reachability` 替代。

Launcher 为 Backend 选定页面与 API 共用的监听端口，再将候选通过内部环境变量
`UNILABOS_LAUNCHER_CONSOLE_HTTP_BASE_URL` 传给后端。`--web-port 0` 可分配实际可用端口。
明确监听某个 IP 时直接使用；通配监听优先尝试配置中心的出站路由，否则使用本机名称解析
得到的唯一非回环网卡地址，多候选时留空。独立启动的前端由浏览器提供候选。
这些候选不能推断 Nginx 域名或 NAT 映射；跨网访问使用显式配置的浏览器可达地址。

## 事项通知

WSS 收到 `hello_ack` 后发送一次 `attention.changed`，`payload` 严格为 `{}`，即使没有
事项也发送。人工确认或异常处置状态变化再次唤醒小通知，中心后台经
`GET /api/v1/center-attentions` 拉取当前数量和摘要，空快照清除旧事项。通知线程不读取
完整事项提供器，不把交接证明或物料列表塞入 16 KiB WSS 帧，普通心跳也不触发事项拉取。
响应携带 station/instance/process/session 身份及 `session_epoch`，与快照在运行时轮询锁内
一致读取；无当前会话返回 503。接口不依赖可选库存模块，沿用原工站同源访问边界。

HTTP 列表不按通知帧大小或事项数量截断，保留全部身份、原修订、状态、选项和计数。
原生人工确认、人工交互、节点错误字段原样保留；交接 `meta_data.progress` 只保留原
`handoff_uuid/revision/role/state`，物料 `meta_data` 只保留 `last_error` 和原
`execution_status`（如有），两类 `last_error` 按 UTF-8 限 500 字节。完整证明及逐件保护
继续查询 `/api/v1/center-handoffs/{handoff_uuid}`，完整物料诊断继续查询
`/api/v1/center-material-exports/{invocation_uuid}`。摘要只读，不改业务事实和处置入口。

具有 `uncertainty_reason` 的 Job 走独立的 `resolve-uncertain` 处理，不进入异常快照。
设备干预只取 `status=open`；节点错误取 `status=open|selected` 且
`stage=DECISION_REQUIRED|MANUAL_HANDLING|APPLYING`。`RESOLVED` 不再计数，即使原生记录
的 status 仍为 open；不会把普通失败、超时或其他人工处置通道归为原生异常。

人工确认批准、拒绝或超时提交后，先唤醒快照，再执行计时器重排、继续调度或取消；
即使后续副作用失败，中心也能收到已提交的确认状态。状态没有变化的重复决定不额外唤醒，
但仍沿用原有逻辑补做未完成的调度副作用；通知回调异常不会遮蔽原业务结果。

外部 Task Cancel（包括中心 `task.cancel`）提交人工确认关闭后会唤醒快照；设备干预
选择提交后，即使设备投递失败并返回业务冲突，也会刷新快照，使已离开 `open` 的事项移出。
修订或选项校验失败没有状态变化，不唤醒；通知异常不改变已提交操作的成功或原业务失败。

原生错误控制器在新增错误、接受处置决定、人工会话流转和解决等事务提交后，统一唤醒既有
事项通知；回滚和无变化的幂等重放不唤醒，通知失败不撤销已提交事实。WSS 在发送首次
小通知前注册监听，首次发送期间发生的新变化会继续提示。物料/交接后台轮询只比较诊断
修订和状态等小字段，无变化不唤醒。原生通知也只比较已绑定中心任务的事项小状态，
纯工站本地事项变化不发送提示；不做心跳快照、定期校准或快照历史。中心先支持
`attention.changed` 和 HTTP 拉取后再升级 OS；OS 不再发送 `attention.snapshot`。

中心调用既有 `/api/v1/stations/{station_id}/error-handling` 和
`/api/v1/stations/{station_id}/errors/{decision_id}/decision` 接口时，错误控制器接受
同库 `center_runtime_state` 已保存的注册 station UUID 作为原有本地身份的别名；原有 `local`
身份和错误、会话、命令均保持不变，任意未注册 UUID 返回 404。无中心注册账本的独立部署
继续使用原配置身份。进入人工处置会话后，手动动作、完成或返回仍使用工站原生接口，中心
本次没有新增这些代理接口或数据库表。

## 任务优先级

工站页面的 `POST /api/v1/workflow-tasks`、工站调用入口
`POST /api/v1/station/workflow-invocations` 和中心客户端创建本地任务时，缺省优先级
统一为字符串 `normal`，持久化到既有 `workflow_task.priority`，返回值也为 `normal`。
页面据此显示“普通”，调度器使用既有映射得到基础权重 100；显式 `high` 的基础权重
为 200。这里不改变调度器的老化、资源约束或排序算法。

中心任务包仍不增加优先级字段。中心提交、按调用查找、领取核对、灾备核实和历史
物料合同核对都按相同的 `normal` 构造本地提交身份。`normal`/`high` 字符串参与
请求指纹，重复提交仍返回原 Task；相同调用键下改变输入、发布修订、优先级或
截止时间仍会冲突，不重复创建 Task/Jobs。工站调用入口继续接受旧调用方显式指定
的有限数值，其数值权重与请求指纹算法保持不变；普通页面创建入口仍只接受
`normal`/`high`，不扩展为 `low`/`urgent`。

当前为测试阶段，本次不提供此前默认数值 `1.0` 的中心任务到新规则的指纹兼容，
也不迁移或清理已有测试任务。历史记录的优先级、指纹和执行结果均原样保留；
按新规则重放或恢复旧调用可能返回指纹冲突，且不会因此另建任务。验收时从中心
重新创建实验任务，生成新的全局任务和工站调用身份，不能只修改旧任务的
`priority`、调用键或指纹来重跑旧执行。本次没有新增 API、数据库表、列或索引。

## 容量保护与永久请求错误

新 claim 前检查积压与本机文件系统可用空间。既有 Task、已确认领取的原调用恢复、已入箱取消和结果补报继续处理；压力消退后原通知重新进入准入，不改 Task/调用/事件身份。压力期间 HTTP 暂停拉取新命令，WSS 收到尚未落盘的新通知时不写 inbox、不发 ACK，断开后由中心保留待重放；已有 inbox 的重复通知仍可精确确认。容量理由发生变化时写告警，避免每次轮询刷屏。

| 环境变量 | 默认值 | 含义 |
| --- | ---: | --- |
| `CENTER_OUTBOX_HIGH_WATER_EVENTS` | 10000 | 边界及交接未确认事件合计高水位，包含已暂停重传的事件 |
| `CENTER_OUTBOX_HIGH_WATER_BYTES` | 67108864 | 上述事件 JSON 的 UTF-8 字节高水位 |
| `CENTER_DISK_FREE_RESERVE_BYTES` | 268435456 | 新领取后仍须保留的本机空闲空间底线 |
| `CENTER_RESULT_RESERVE_BYTES` | 2097152 | 每个未结束的已领取调用，以及候选新调用的结果预算 |

判定会额外预留每调用两条边界事件和配置的结果字节数；预算与实际积压合计超过高水位就暂停新领取。空闲磁盘小于底线加预算，或磁盘探测失败，也暂停。预算是保守准入估算，并非文件系统硬配额；部署须按实际结果体积调整，不能保证抵抗其他进程消耗磁盘。PG 模式探测的是工站本机文件系统，远端数据库空间须另外监控。历史已确认/隔离证据不被删除；其磁盘占用仍反映在可用空间里。

运行时命令、恢复任务、边界/交接发件箱及库存交接 intent 每页最多100条；待处理命令和恢复任务轮转分页，前页待核实项目不阻塞后页独立调用。事件只有精确 ACK 后才从待发送集合移出；暂停重传不是 ACK，统计仍包含这些积压。

命令确认有独立轮转游标，暂停的ACK不挡住后续确认和已确认命令执行；未确认命令继续保留。灾备永久隔离的旧调用不会再产生新结果，退出核实后不再占未来结果预算；其真实未确认事件、文件占用仍计入压力。只补证据中的永久请求错误转needs_review，继续核对后续原序号。

HTTP 429/503 及 WSS 握手的 Retry-After 支持秒数和带时区 HTTP 日期。HTTP 按1/2/4/8/16/30秒加抖动退避，成功重置；服务器指定期限优先，不被普通通知唤醒缩短。停止可立即打断等待。400/413/415/422/431 请求错误按方法、路径和原正文摘要写入 `center_request_holds`，原请求跨重启暂停发网；事件/body及身份保留在原持久表，不通过改序号、事件 UUID 或目录版本绕过。鉴权、会话接管和业务冲突继续使用既有专门恢复规则。

本机诊断不需要机器私钥或密钥；使用与运行时一致的容量环境变量。下面路径须替换为原工作流库，不能另建空库：

```bash
python -m unilabos.app.center_client.capacity --database /absolute/path/workflow_history.db --database-type sqlite status
python -m unilabos.app.center_client.capacity --database /absolute/path/workflow_history.db --database-type sqlite retry-request --request-digest 'sha256:...' --expected-attempts 1 --reason '已核实并修正中心限制'
```

诊断包含当前预算、积压条数/字节、最早事件发生时间和暂停请求列表（每页100条，以 `status --after-digest` 续读）。最早时间来自事件发生时间，不冒充网络入库延迟。显式重试须核对当前 `attempts`；过期核实不能清除新的拒绝。操作只重开原请求，成功仍须经过正常校验和精确 ACK。PG 运维可显式选 `--database-type postgresql --database-url-env <变量名>`；本批尚未运行 PG 专项验证。

`workload`直接聚合原工作流库的Task/Job总数、中心提交数量，以及重复提交/同节点同attempt/多个current attempt组数。节点身份采用runtime_node_id（没有时用workflow_node_uuid），合法重试和展开节点不会被计作重复；缺工作流表返回unavailable。此聚合仅用于诊断，不在新claim热路径执行。

活跃进程可调用 `CenterRuntime.diagnostics()` 查看 `connection`：从TLS连接尝试开始，到有效hello_ack才算成功；首次连接成功后的尝试才计重连，包含TLS/HTTP拒绝和异常断开后的重试。重连成功率分母只含已成功或失败的重连，主动取消和未完成尝试排除，无样本为null；带PID/uptime，重启重新计数。离线CLI不能读取另一进程内存，connection明确返回LIVE_RECEIVER_PROCESS_REQUIRED；不能把它当作零失败。

## 事件格式与确认

`CenterClient.report(envelope)` 只接受完整信封，不再发送旧的散字段请求。信封字段为：

```text
protocol_version: "1.0"
event_uuid, event_epoch_uuid, event_sequence
station_uuid, instance_uuid, station_invocation_uuid, local_task_uuid
event_type, payload, occurred_at, event_digest
```

`occurred_at` 为不早于 Unix 纪元的 UTC RFC3339 字符串。新事件输出固定三位毫秒，例如
`2023-11-14T22:13:20.123Z`；校验也接受无小数的秒精度形式，冻结后保留原字符串。
不接受时区偏移、非 `Z` 时区或其他小数位数。序号从 1 开始且不超过 `2^53-1`。`event_digest`
是对除自身外完整对象做 RFC 8785 JCS 后的 SHA-256，带 `sha256:` 前缀；不包含会话字段。
UUID、纪元、序号、时间及正文均从已提交 outbox 读取，重传不得重新构造身份。

| event_type | payload |
| --- | --- |
| accepted | `{task_payload_digest, accepted_at, local_task_uuid}` |
| running | `{task_payload_digest}` |
| outcome | 旧 `{task_payload_digest, status: succeeded\|failed\|canceled, output: object}`，或完整增加 `{material_refs, container_refs, artifact_refs, started_at, finished_at}` |
| attention_changed | `{task_payload_digest, required: boolean, reason: string}` |

accepted 的 `local_task_uuid` 必须与顶层相同，`accepted_at` 遵循相同 UTC 时间格式。
工作流事务将 Task.create_time 转为 accepted_at 和 occurred_at。任务包及 claim 接口中的
`payload_digest` 字段不受事件 payload 命名调整影响。

`reason` 去掉首尾空白后不能空，UTF-8 长度不超过 500 字节。`required=false` 是一条
审计事实，是否解除 attention 仍由中心政策决定。当前工作流事务自动生成 accepted 和
outcome；成功必须读取真实 `workflow_result`，timeout 转为 failed 并保留
`output.failure_code=timeout`。尚不自动生成 running 或 attention_changed。

中心必须返回 HTTP 202 及包含 `event_uuid/event_epoch_uuid/event_sequence/event_digest/application/
last_contiguous_event_sequence` 的确认。前四个身份字段必须逐字段、逐类型匹配，才会
更新本条事件。连续前缀不能用于跳过其他本地事件。

- applied / duplicate：标记本条已确认，保留正文和序号。
- conflict：证据已被中心可靠接收，但不是成功应用。保留 `acknowledged=0`、记录
  `application=conflict` 并停止此事件热重传，交给人工核实。
- 状态码不是 202，或 ACK 缺字段、身份或摘要不匹配：保留原事件等待原样重传。

## 事务与升级

公开 `submit_station_workflow(..., center_invocation_uuid=...)` 在创建事务核验同库的已领取
冻结 intent、Task 永久调用身份及请求指纹，一次提交 Task/Jobs、inbox 绑定与 accepted。
工作流的真实 Task 终态日志入口在同一事务写入 outcome，成功结果来自 `workflow_result`；
缺少结果或持久时间会报错并回滚，不以当前时间或 Task.output 代替。取消待确认、单个 Job
完成等非 Task 终态不会提前生成 outcome。

`persistence.center_events.append_event_in_transaction` 使用调用方已有事务检查阶段、分配
序号、写完整信封并推进高水位；它不持有连接生命周期、不提交事务、不调用网络。
`DurableCenterStore.record_event` 作为独立账本入口也复用该 writer。重复阶段返回原信封，
不占新序号；身份或终态内容变化明确报错。已确认和冲突记录保留，序号耗尽不回绕。

轮询器只发送已提交 outbox，不从 Task 快照合成事件。`recover_center_workflow_invocation`
按冻结 intent 在一个工作流事务内恢复已存在任务的绑定和历史缺失事件，包含软删除 Task；
不领取、不创建、不调度新执行。旧 accepted 的观测时间和所有已有信封保持原样。

新工站迁移加入 inbox 的 `acceptance_version` 与非空本地 Task 绑定的唯一索引。任务包使用
普通 JSON 保留数值类型，同摘要重放沿用首次冻结包。旧 JCS 包如果已丢失 `1.0`/`1e20`
等类型而无法复现既有 Task 请求指纹，会明确拒绝并保留历史供核实，不猜测或改写身份。

完整事件使用普通 JSON 持久化以保留 binary64 浮点表示，JCS 仅用于摘要；例如 `1e20`
不能先规范化为大整数文本再存储，否则 Python 重启后无法按安全整数合同计算摘要。

启动会将旧 `center_boundary_outbox.event_json` 升级为完整信封。按原发生时间、调用
UUID、阶段、事件 UUID 稳定排序分号，保留原事件 UUID、业务内容、时间和确认状态；
旧散字段的整数毫秒时间精确转换为 UTC 三位毫秒文本，不经过浮点时间戳。
旧 writer 使用 JCS 后可能把合法浮点输出（如 `1e20`）存成整数字面量。仅对旧散字段
`output` 内这类超出安全整数范围的值尝试恢复为有限 binary64；候选浮点的 JCS 字面量
必须与原整数字面量完全相同，否则迁移整体回滚。此规则不用于时间、序号或完整信封。
已确认记录标记 `legacy_acknowledged`。升级 DDL、转换与高水位同事务提交，失败不改写
旧事件。已升级记录只校验，不重新计算摘要或序号。发现账本缺失、纪元冲突或序号缺口
时停止，不创建新纪元逃避恢复。升级时应停用旧版本写入进程，不能并行混跑两个版本。
升级只支持已发布的旧散字段格式。已有完整信封不符合当前合同（包括未发布的中间格式、
已经丢失浮点类型的不安全大整数）时保留库并明确拒绝，不能猜测或重算历史摘要。

旧已确认事件保留确认状态，不会自动重发。若中心尚无对应的新 receipt，中心连续前缀
可能保留历史缺口；新事件仍须取得逐条精确确认。不能通过更换纪元或跳过本地事件掩盖
该缺口。现在可在停止常规客户端/工作流写入后，通过 `EventHistoryStore` 和
`EventHistoryRecovery` 显式恢复：

```python
from unilabos.app.center_client.event_history import EventHistoryStore, EventHistoryRecovery

history = EventHistoryStore(workflow_db_path, station_uuid=config.station_uuid,
                           instance_uuid=config.instance_uuid)
try:
    recovery = EventHistoryRecovery(CenterClient(config), history)
    print(recovery.prefix())   # 从中心已收件/已隔离事实重算前缀；不授予执行权
    print(history.gaps(after_sequence=0, limit=100))  # 分窗口查看本地缺口
    print(recovery.recover(original_sequence))      # 逐项显式选择原序号
finally:
    history.close()
```

恢复保留原实例、纪元和高水位；原历史会话及有效凭据必须存在。先持久化恢复意图，
再经 mTLS 查询原信封/精确确认。中心缺收件时，只重发本地原调用或证据类交接信封；
prepared/released 只能恢复已持久的原响应，不能重新发出控制承诺。中心已有唯一原
信封时可填本地空洞；相同位置多变体、两端都缺证据、原身份/正文/回执漂移均保留
`needs_review`，不能跳号或猜测 ACK。网络响应丢失保持 `pending`，重启重试同一序号。

新精确确认记录在 `center_event_history_recovery`；既有 `legacy_acknowledged` 和
交接原响应不覆盖。原交接响应中的旧前缀与查询返回的当前前缀分别保留。恢复不接入
WorkflowService，不创建/恢复 Task、Job、物料保护或控制会话；填完事件不等于核实
灾备后的新执行资格（见本文灾备只补证据与新纪元）。

本地 Task/Job 事实与 outbox 的原子性不等同于跨数据库分布式事务，也不证明物理设备
副作用恰好发生一次。手工物料交接使用独立持久桥接，当前不会自动构造物料 DAG；WSS 已提供通知与持久补拉。
新 outcome 支持显式引用及真实 Task 开始/结束时间，未开始时 started_at 为 null。
已存旧事件原样重传。公开物料端口来自冻结图 meta_data.unilab.material_ports；成功物料
出口缺证据时 Task/Job/真实结果保持完成，center_outcome_evidence 记录
material_evidence_required 或 material_output_invalid，暂不发 outcome，不重新调度动作。
整数还受中心 JCS 安全范围 `[-9007199254740991, 9007199254740991]` 约束，有限 binary64
浮点数仍按原值导出。若真实结果不能规范化进入事件，保留 Task、Job 的完成状态与精确
`workflow_result`，复用 `center_outcome_evidence.pending_reason=output_not_exportable`
记录待核实；不占用新终态事件序号，不截断或转换数值，恢复核对也不重建或派发 Job。
中心未收到终态前仍保留最近状态和 output。这类责任通过 `material_export` 的 HTTP
事项摘要及完整物料详情供核实；当前没有新增数值修正 API 或自动处理策略。已发布合同 UUID、
摘要和目录投影不变，普通 outbox 数据库写入故障仍使原事务回滚。

受信任站内适配器可调用 WorkflowStore.record_center_outcome_evidence，验证已领取调用、
原 owner、任务及冻结摘要后，把权威引用和 outcome 同事务持久化；相同证据重放，
不同证据拒绝。该内部接缝没有 HTTP 路由，库存自动导出已集成，自动物料 DAG 仍待后续依赖任务。

## 显式核实现存调用

在当前进程已有的 `CenterRuntime` 上使用 `runtime.reconciliations`。后台轮询不会自动
发起人工确认，也不应为核实另起 WorkflowService 或 runner。操作者先核对任务与设备
记录，再准备并保存工作单：

```python
recovery = runtime.reconciliations
snapshot = recovery.inspect(invocation_uuid)
request_uuid = recovery.prepare_resume(
    invocation_uuid,
    reason="已核对原任务与设备日志",
    evidence_refs=["log://station/original-task"],
)
receipt = recovery.send(request_uuid)
```

`prepare_resume` 查询中心冻结任务包，并核对本地永久调用映射、现存 Task、输入和执行
指纹。缺失、身份不符或首次发送前已终态均拒绝。准备步骤持久保存完整正文和幂等键，
调用者应保存返回的 `request_uuid`；响应丢失或进程重启后，在当前 runtime 使用同一个
`recovery.send(request_uuid)` 重试。成功回执再次核对原请求和原实例，随后事务内修复
原 Task 的 inbox 绑定及事实发件箱。正常轮询负责补报已提交事件；核实不会创建 Task、
Job 或触发调度，普通 claim 的恢复保护也不会解除。

核实过程中原任务已终态或软删除，已尝试发送的工作单仍可原样重放并补报真实终态。
实例或事件纪元变化会使旧 resume 凭据失效，需要重新核实。`execution_authorized`
固定为 `false`，回执不表示设备或 cleanup 关注已经解决，attention 可能继续保留。

仅追加证据使用 `prepare_evidence(invocation_uuid, reason=..., evidence_refs=...)`；
确认中心已记录的终态使用 `prepare_result_confirmation(invocation_uuid,
local_task_uuid=..., result_event_uuid=..., result_digest=..., reason=...,
evidence_refs=...)`。这两个入口同样返回持久工作单 UUID，随后调用 `send`，不会修改
本地 Task 或结果。结果确认必须引用中心快照中已经形成终态的原事件与摘要，不能提交
新的 status/output；输入或结果内容修订属于独立流程。


## WSS 通知与 HTTP 恢复

交接包可携带 `chain_uuid`、正整数 `leg_no`；第一段不带 `previous_handoff_uuid`，后续段必须指向另一交接。三者属于同一交接的固定身份，后续包修订不能改变。旧包缺少链字段时仍按原正文及摘要验收，不回填或改写旧包。容器集合改变或原链关闭后由中心建立新链，工站不推算段号或容器版本；沿用中心返回的权威身份。

`CENTER_WSS_ENABLED=1` 默认启用专用 mTLS 通知线程，使用相同机器证书、密钥及持久控制会话。`0` 表示仅用 HTTPS。WSS 先把不可变通知提交到 inbox，再精确 ACK；HTTP 定期补拉和 inbox 执行继续运行，重复通知不会创建新 Task。断线、慢连接或序列缺口会转为 HTTP 恢复；运行时停止时先等待通知线程退出，再关闭持久存储。

中心取消已经领取的调用时会发送 `task.cancel`，与 `task.available` 共用持久收件、精确 ACK 和 HTTP 补拉。运行时核对原调用和全局任务身份后，仅取消原有 Task，使用中心命令 UUID 作为本地控制幂等键。Task 尚未落地或本地控制未成功受理时保留通知；领取后恢复发现已有有效取消通知时，只补齐原 Task/Job 事实并阻止首次派发。执行取消前通过中心复验当前控制会话；正在停止的设备仍等待真实终态回报，已完成 Task 的结果不被取消覆盖。升级时，旧客户端因不支持类型而隔离的 `task.cancel` 会重新进入处理队列；灾备隔离和失效控制会话仍禁止执行。

WSS 不上传任务包、结果或大文件，也不授予 claim 许可。测试使用本机 `unilab` 环境和 SQLite，真实 WSS 与双库回归由中心集成测试驱动。

## 灾备只补证据与新纪元

恢复旧备份前停止执行服务，对原工作流库运行：

```sh
python -m unilabos.app.center_client.recovery_mode --database /path/workflow.db enter --reason '旧备份恢复'
python -m unilabos.app.center_client.recovery_mode --database /path/workflow.db status
```

持久 evidence_only 模式只轮询原事件历史，缺中心主体的完整信封进入独立保管；不会创建 Task/Job、claim、控制会话或库存保护。有效原凭据、历史会话及 epoch 仍必需。
保管回执不等于业务应用 ACK；原 outbox/精确 ACK 保留。已保管事件独立隔离，不会持续热重传挡住新通知。

退出前先取得中心管理员的 station-checkpoint：请求含 checkpoint_uuid 幂等键、当前 status 的 recovery_uuid 及原 station/instance/epoch。
中心生成时已永久隔离旧身份和通知并关闭原进程；GET /stations/{station_uuid}/recovery-checkpoints/{checkpoint_uuid} 只能由原实例有效 mTLS 历史会话读取。

运行 `review` 得到当前 revision、evidence_digest 与记录数，再准备 JSON：expected_revision、expected_evidence_digest、reviewed_by、reason、非空 evidence_refs、previous_work="keep_isolated"、checkpoint_uuid。
用 `resume --verification-file /path/review.json` 退出；CLI 实时读取中心基线，不接受文件伪造证明。新事实到达导致摘要变化时必须重新核实。
从未有中心账本的独立工站 checkpoint_uuid 可为 null；有中心表但原实例账本缺失则拒绝此方式。

退出保存历史 epoch/原会话/高水位并创建新 epoch，从序号1开始；每个原事件仍保留原完整信封与精确回执。
历史补证可用 `EventHistoryStore(..., event_epoch_uuid=原纪元)`。新的中心控制许可仍需现有 epoch 审批，退出不能恢复旧 Task/Job、旧 claim 或旧交接。

库存分库在启动前显式同步已提交核实链：

```sh
python -m unilabos.app.center_client.recovery_mode --database /path/workflow.db sync-inventory --inventory-database /path/inventory.db
```

该步骤核验原工站/实例，事务记录库存纪元迁移；失败可重试。旧库存保护、映射、原 intent/信封/receipt 不改写；配置变更不能代替核实。
缺原精确回执的旧库存 intent 保留在 station_handoff_recovery_pending，可由 authority.recovery_pending_intents() 查询；不分配新序号、不释放保护、不阻塞新纪元意图。
旧调用物料边界继续隔离待核实，不在新纪元重新生成。

PG 专项延期至最终批次；当前测试使用 SQLite，不依赖本地 Docker。完整两端操作与核实文件示例见中心 docs/evidence-only-recovery.md。


## 已发布工作流展示信息上报

目录投影沿用发布合同的 `name` 和可选 `description`，不从 UUID 或其他字段猜测。
名称提供时必须是非空白字符串；描述原样保留，包括空字符串，没有描述时省略。
旧合同/旧适配器缺少展示字段时仍可使用原执行投影。新增字段不参与
`projection_digest`，但包含在 `chunk_digest`、`catalog_digest` 和持久重传正文中；
执行指纹、合同摘要、冻结任务和内部合同发布序号均保持原意。

中心先更新接收协议，再更新工站。下一次正常目录同步会为已有合同补齐展示字段，
不需要重新发布或审批；旧待传目录优先按原正文提交，再同步带展示信息的新目录。
中心复用 `workflow_contracts.body` 保存，`GET /api/v1/workflow-contract-catalog`
通过 `items[].contract.name` 和 `items[].contract.description` 返回，未上报的字段省略。
携带名称但不带描述的新上报会清空旧描述；完全不带展示字段的旧工站不会清掉已有值。
没有新增数据库表、迁移或 API。
