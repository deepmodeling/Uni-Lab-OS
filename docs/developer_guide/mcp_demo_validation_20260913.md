# MCP 自然语言 demo 验收报告（2026-09-13）

## 结论与边界

7 个 demo 的工作流均已复现并通过独立验收：28 个工作流定义、29 次任务、83 条
node-run 记录、320 次 MCP 调用。29 次任务中，26 次成功，3 次按场景要求失败
（异常中止、库存不足、物料第三阶段缺料）；没有把预期失败改成成功。

运行的是本机真实 Codex CLI，模型严格为 `gpt-5.6-luna`，
`model_reasoning_effort="max"`，复用原有登录，未读取或复制账号凭据。
每次只给当前 demo 的自然语言要求和 README；模型自行发现工具、实例化或建图、
经调度提交、读取结果，并处理明确授权的 demo 异常决策。

每个 demo 使用独立端口、四库和机器名，默认“调度权威 + Host + 可选 Slave”拓扑。
本次均为 HostLink 与虚拟驱动，含跨进程通信；未验证物理跨机网络、真实硬件或 ROS 2。
夹具没有代模型运行 smoke/提交业务操作，Codex 未使用 shell、文件修改或 web search。
验证结束已关闭夹具启动的进程，未重启或修改用户原来的 8003 实例。

MCP 实现及接入见[本机 MCP 接入](mcp.md)。共 131 个 protocol 业务工具及 6 个辅助工具；
此次端到端验证覆盖下列场景用到的操作，并不代表所有 131 个操作均做过实机验证。

## 各 demo 结果

| Demo | 定义 / 任务 | MCP 调用 | 验收内容 |
| --- | --- | --- | --- |
| Workstation | 1 / 1 | 10 | PONG；Modbus slave ID 3/7；共享串口计数 1、Modbus 计数 4；4 节点成功 |
| LAN | 1 / 1 | 11 | 启动阶段订阅/远端停止证明；echo → stop → start 的轮次递增；3 节点成功 |
| Complex Workflow | 5 / 5 | 33 | 定次、等待、until、嵌套和综合流程；节点数 4/5/5/6/10；核对各轮实值及历史 |
| Exception | 3 / 3 | 28 | 异常传播后中止；人工替换返回值；调度重试产生第二个 attempt，旧失败及关联保留 |
| Inventory | 4 / 4 | 40 | 入库 100 ml → 出库 40 ml → 余额 60 ml；申请 500 ml 在派发前失败；盘点仍为 60 ml |
| Lock | 9 / 9 | 69 | 同组批量并发提交；动作锁 waiting/blockers；always_free 重叠；跨设备同板互斥；4 项设备账本审计 |
| Materials | 5 / 6 | 129 | 四个模板流程及模型自行创建的第三阶段三节点图；先缺料回滚，再补料、出库挂载、加液与权威数据同步 |

这里的“通过”不依赖模型自报成功。验收读取 task、node-run、attempt、job、设备返回，
以及只读观察器保存的锁/库存变化；复用 demo 的语义断言，另补第三阶段物料权威断言。
验收脚本不改变被验证的状态，也不自行批准异常。

## 物料第三阶段的完整证据

模型自行创建 `flow_plate_01`、`flow_plate_02`，以及水批次；随后保存同一张三节点图：
`host_node.apply_deduct_resource` → `material_bench.fill_well` → `material_bench.bench_report`。

- 工作流：`18fed504-e243-45c8-8cf6-d7e628c0fe51`。
- 第一次任务：`4d87ca17-7e49-4945-a6f6-849e651bf8ef`。库存仅 500 µl，需求 1200 µl；
  `plan_not_executable` 明确短缺 700 µl。三个节点均 canceled、没有设备结果，预留列表为空。
  在失败后、补料前的原始 MCP 回包中，两块板仍 active 且未挂载，水库存仍为 500/500/0。
- 补料：向同一个 lot 增加 10000 µl，total/available/reserved 变为 10500/10500/0。
- 新任务：`6bb9a8b3-d81f-42e5-81d5-719e4f6f3c79`。三个节点成功，均只有一次 attempt。
  `flow_plate_01` 变为 in_use，挂到 Slave 的 `bench_deck/T1`；第二块板仍 active。
- 权威终态：A1 的 volume=1200，substances 为 Water / 1200 / ul；水批次余额
  9300/9300/0，板与液体的两个 reservation 均 consumed。T3/T4 保持前两轮的板。

选中板 UUID 为 `ebff6342-2fec-4d0f-9ae7-71217ade7daa`，水 lot UUID 为
`9f8a5b9c-2dca-4186-ba8e-9a1f97aed958`，A1 孔的权威 UUID 为
`6c69381c-faca-42a4-bac3-888f67b4fae7`。权威树原始响应保存在 `materials-authority.json`。

## 实际遇到的失败与修正

1. **物料归属推断确有运行时缺陷。** 首轮四个模板成功，但第三阶段出库挂载失败：
   台面已挂在权威设备节点下，`owner_device_of` 却只读最外层的旧 extra 绑定字段。
   改为沿权威树查最近的设备祖先 `resource_id`，支持 sub-device；转移后不被旧绑定标记误导。
   保留显式绑定的独立资源根语义，拒绝无归属及成环。补了 5 项测试，并用全新实例重跑通过。
   首轮失败时 Luna 没有擅自用人工替换结果掩盖错误。
2. **物料 payload 默认值。** 初版 MCP 原样转发省略默认值的 JSON，与服务端规范化后的
   typed payload 比较时触发 422。现复用已有 `Materials` 客户端的 `bind_payload`，
   仍使用同一个 protocol 模型和幂等信封；测试验证重复入库不叠加数量、改内容复用键返回 409。
3. **工作流报错缺少字段原因。** 原来只报通用 invalid_input，模型无法知道缺什么。
   现在保留错误码，补充字段与校验原因，不回显整个输入或内部堆栈。
4. **首轮提示超出当前场景。** 最早的通用提示在非物料 demo 中也提到了物料第三阶段，
   模型在本场景完成后继续尝试库存操作。已停止这些隔离试跑、保留记录，改为按 demo
   生成提示并增加越界写操作断言；最终采纳的是收窄范围后的新运行。
5. **验收夹具自身的三处问题。** LAN 的首次订阅 proof 会被后续 start 更新，改为运行
   工作流前冻结证据并重新运行；Complex 的 node-run 需关联图才能取名称；Exception 当前
   options 是对象列表，旧 smoke 断言需要投影 action 字段。后两项只对原始证据重新验证，
   没有重跑设备动作或修改结果；原始 Codex 日志、证据及早期批次 summary 保留。
6. **模型调用仍有可恢复错误。** 最终 Materials 中两次把 `registry_class` 放错层级，
   被 schema 拦截后自行纠正；查询虚拟 `host_node` 的物料行得到 404，随后使用真实 bench
   的物料上下文和明确的 `target_device_id=host_node` 成功运行。指南已说明这一点。
   单独库存 dry-run 没有公开工具，缺料分支通过真实调度提交验证，未绕过协议白名单。
7. **自然语言报告不应替代 JSON。** Luna 最终简报将 A1 孔 UUID 抄错了一位；原始 MCP
   回包与权威树一致，上文记录的是权威 UUID。独立验收未采用模型手写 UUID 或成功结论。

当前没有遗留未复现完的 demo 工作流；以上失败尝试和验证边界仍应保留。

## 证据位置与复核

以下目录相对于仓库根目录，属于本机 `.whalent_tmp` 留档，不随源码提交：

| Demo | 最终采纳目录 |
| --- | --- |
| Workstation | `.whalent_tmp/mcp-luna/final-a/workstation_demo` |
| Inventory | `.whalent_tmp/mcp-luna/final-a/inventory_demo` |
| LAN | `.whalent_tmp/mcp-luna/final-lan/lan_demo` |
| Complex | `.whalent_tmp/mcp-luna/final-b/complex_workflow_demo` |
| Exception | `.whalent_tmp/mcp-luna/final-c/exception_demo` |
| Lock | `.whalent_tmp/mcp-luna/final-c/lock_demo` |
| Materials | `.whalent_tmp/mcp-luna/final-materials/materials_demo` |

每个目录含 `metadata.json`、`evidence.json`、`observations.jsonl`、`codex/command.json`、
`codex/prompt.txt`、`codex/events.jsonl`、`codex/answer.md` 及进程日志。
`metadata.source` 记录本地源码目录、Git HEAD/状态和 Python 文件 SHA-256；Inventory、
Complex、Lock 的本机目录没有可用 Git HEAD，因此按留档文件指纹追溯，不称为已验证远端提交。
Materials 的本机目录名是 `LabDeviceSiteDemo`，包名为 `materials_demo`。

失败记录还在 `pilot-1/workstation_demo`、`suite-1/lan_demo`、`suite-2/exception_demo`、
`materials-1/materials_demo`；LAN 初次采样错误记录在 `final-b/lan_demo`。
前三个提示越界试跑是中断记录，不是已通过验收的轮次。

最终汇总为 `.whalent_tmp/mcp-luna/validated-summary.json`。部分早期批次的 `summary.json`
保留了夹具未修正时的失败结论，不能代替这个七场景逐一重验的汇总。
可以对已有证据离线复核，不启动模型、不产生账号费用、也不修改原始记录：

```powershell
python -m tests.e2e.run_mcp_demos --output .whalent_tmp/mcp-luna/recheck --recheck .whalent_tmp/mcp-luna/final-materials/materials_demo
```

`--recheck` 可重复指定七个目录；需要对应本地 demo 源码以加载语义断言。
重新进行自然语言全流程运行则用 `--demo` 或不指定 demo，使用全新 `--output`，会消耗账号额度。

## 回归

547 passed，1 skipped。运行范围：

```powershell
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'
python -m pytest tests/server tests/resources/test_material_owner.py tests/resources/test_materials_helpers.py tests/backend/hostlink/test_material_transfer_proxy.py tests/backend/hostlink/test_materials_proxy.py tests/e2e/test_mcp_demo_harness.py -q -p anyio.pytest_plugin --disable-warnings
```

显式关闭环境自动加载的 ROS pytest 插件，使用 AnyIO 插件；不因此绕过上述业务测试。
MCP 测试包含真实 JSON-RPC 初始化/调用、137 个工具 schema、生命周期、错误信封、
本机/Origin/Host 防护、大响应分页、幂等重放、批次派发前的 typed payload 校验和等待时限。
