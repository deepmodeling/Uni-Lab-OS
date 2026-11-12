# 从首次 Jazzy 提交到当前 dev

## 比较范围

起点是 **2026-08-10 `99558a5a`（PR #304）**，首次引入 Jazzy / Python 3.12 / NumPy 2；
不是 8 月 19 日的“双环境支持”提交。终点是 **2026-09-14 `862ce87a`（PR #317）**。
采用提交祖先关系比较，不能简单用 `git log --since`：部分后合入提交的作者日期更早。

这个区间里 `unilabos`、`docs`、`tests` 共改变 731 个文件（142,382 行新增、39,048 行删除），
包含目录迁移和生成文件，不能把行数直接理解为新增业务规模。
本手册描述终点的接口，并把旧设计与现状的差异单列；不把中间版本里的所有方案同时保留为可用接口。

## 关键阶段

| 提交 / 时间 | 接口相关变化 |
| --- | --- |
| 99558a5a，08-10 | Jazzy/Python/NumPy 环境迁移起点 |
| c2cdf658 / 163cb4ed / ffa0156b，08-18 | 规范 Resource/Site，对接注册表、设备与 ROS 适配 |
| b285c924 / 2b98a443，08-18 | 传输无关设备合同、分布式 HostLink |
| 73573cf4 / 0cde8c02 / ef4be0f1，08-19 | failed attempt 交给调度；只暴露 HostLink/ROS2；注册表发布 backend/Site/error 元数据 |
| e757967c / 629dba96，08-19 | Jazzy/Humble 并行环境与动作 schema 稳定 |
| 94c87e26，09-04 | runtime.v1、微后端、双 backend 与六个 demo 集成 |
| a5be29a0 / 9970aa3a，09-06 | 权威/Host 双进程、lot 库存、ResourceSlot 名称解析、中继等待路由就绪 |
| 3402c34d，09-14，PR #316 | v1 物料、Site 导入绑定、运行控制等收口 |
| 862ce87a，09-14，PR #317 | ROS2 子设备恢复，七个 demo 工作流校验/修复 |

中间 retry/cancellation/决策重放提交属于演变过程；最终语义以本版
“Backend 下发、Host 执行、失败后由 Backend 决策并调度新 attempt”为准。

## 接入方需要迁移什么

| 原先入口 / 假设 | 本版入口 / 规则 | 迁移影响 |
| --- | --- | --- |
| app/web 内置状态页、编辑器和展示 API | server/api 业务 HTTP，前端独立实现 | 旧页面路由不能当新后台能力；主页只作入口展示 |
| 一个进程承包管理 API 与设备 | 权威常驻管理端口，Host 子进程仅控制 WS/出站 HTTP/HostLink | 健康、重启、日志要区分进程 |
| unilabos.ros、顶层 hostlink | backend.ros2、backend.hostlink；共享 backend.runtime | 更新驱动 import 和扩展点 |
| unilabos.workflow 的多重含义 | server.services.runtime.workflow 管理；registry.workflows 声明；experiments 执行协议 | 上传、编排、实验编译不能混为同一个命名空间 |
| 顶层 compile 协议代码 | experiments.compile | 更新高级实验协议引用 |
| package_cli 独立入口 | app.cli 的 package/pkg 子命令 | 从统一 unilab 入口使用 |
| 各处自建 HTTP client / server.client | unilabos.client 按业务域调用 | 不同响应信封仍需区分，不能一个 unwrap 覆盖所有域 |
| Edge 直接创建/重跑任意 job | 提交 workflow-task，调度下发 execute_job | retry 是新 attempt，不是本地循环 |
| 设备闲忙完全靠驱动内部等待 | endpoint 能力、调度准入、材料锁声明 | 不新增嵌套锁升级，不把锁竞争隐藏成动作长时间卡住 |
| UUID 同步依赖 Edge/Cloud 映射 | 权威创建后使用返回的 material/site UUID | 草稿先登记再挂载；开机图 adopt 另有条件创建语义 |
| PLR 从 klass/外层 type 猜类 | 仅 config.type 用于 PLR 反序列化 | 修复旧图/包的 config，不允许兜底解析 |
| Site 标签 T1 直接等价于实例位点 | 浏览器显式确认；API resolve 到作用域内唯一 site_uuid | 添加确认与待填计数，不能只靠非空判断 |
| 跨设备 transfer 仅本地树移动 | 权威事务 → 来源 unload → 目标 load | 网络失败检查三阶段，503 可能已经移动权威记录 |
| 数据库单一读写热点 | runtime/materials/telemetry/history 四库与各自服务 | 调用业务接口，不直接跨进程写同名表 |
| 前端和 Host 共用 schedule WS 观察状态 | 前端 SSE，Host 专用 runtime.v1 WS | 多页面不应顶掉执行控制连接 |
| 每秒 GET 日志 / 仅受管 Slave 尾部 | 日志变更通知 + 按源游标 HTTP 拉取 | 同时支持 Host/远端 Slave，独立游标与轮转提示 |
| 枚举 basic 等 backend | CLI 正式仅 hostlink / ros2 | 特殊 ROS 能力继续 ros2；MoveIt/RViz 不支持 HostLink |

旧图的单次升级留在图读取/上传边界（`legacy_adaptor/legacy/graph.py`）；
不能因此让当前 Graph Authority 或 PLR 序列化同时接受多套互相矛盾的字段。
旧云端 Legacy 适配仍是独立边界，本次既不删除，也不改写它的协议。

## 已知缺口：文档不能替代码兑现

| 当前事实 | 接入时如何处理 |
| --- | --- |
| OpenAPI 大量响应没有详细 schema | 查业务章及固定版本的 handler / 协议模型；不要生成“返回空对象”的客户端 |
| OpenAPI 对 Workflow 创建标 200，handler 实际 201 | 接受正确的 2xx，继续检查 code；本手册注明动态状态 |
| x-openlab-role 与默认双进程实际处理位置不完全一致 | 按 HTTP 章节的实际路由矩阵，不据标签决定安全权限 |
| 0.12.3 及以前的 HTTPWorkflowClient.watch_task / CLI --follow 用控制 WS | 升级 SDK：watch 已改走 `/api/v1/events` SSE + HTTP |
| HTTPWorkflowClient.save_graph 没有 site_binding_mode 参数 | preserve 草稿用原始 HTTP；默认 SDK 导入是 resolve |
| 后端没有“工作流 card 待填数字”专用字段/API | 微前端根据必填与未确认绑定计算，不能假定服务已代算 |
| klass/class_name 仍在部分模型中 | 不宣称字段全面删除；仅禁止用其兜底 PLR config.type |
| backend_http 会通过 WS 携带 base64 请求体 | 不宣称 WS 完全零正文；注意中继超时及写入结果不确定性 |
| EdgeControl 当前只有一个活动执行控制连接 | 不把前端或多个独立 Host 随意接到同一控制槽位 |
| 部分控制命令缓存仍在内存 | 崩溃恢复检查 durable 执行记录/对账，不承诺全链路 exactly-once |
| Legacy 只提供显式 factory，当前 CLI 没有 --legacy | 不按历史参数启动；废弃计划与实际适配保留分开记录 |
| 包卸载 handler 不含完整的关联物料/进程清理编排 | 不把卸载 operation 成功当作实验室数据清空；单独核对关联状态 |
| 数据库按 `schema_version` 线性迁移，0.13 发布线四个库都从 v1 基线开始（尚无迁移）；没有迁移路径时开发期四个库都备份后重建，正式发布前 runtime / materials 改为拒绝启动 | 改 DDL 或 contract_version 时把 schema_version +1 并补迁移；`tests/server/test_database_schema_versions.py` 固定各库指纹 |

这些是对固定提交的静态审阅结论，不表示本次已运行并复现每一种故障。
尤其 watch 所在客户端文件在本地另有未提交编辑，本次文档没有把那些改动算入已发布合同。

## 验收清单

接口“文档齐全”与“所有硬件/并发/恢复场景通过”是两件事。发布时至少核对：

- 生成 OpenAPI 路径/方法与 HTTP 逐项参考一一对应，模型引用、MCP 工具 schema 和 CLI 帮助可复现。
- 普通前端/两个页面使用 SSE，打开地图/日志不抢占 Host 控制连接。
- 空图启动、受管包 launch、显式不同 machine_name 的同机 Slave、Workstation 子设备能力均可观测。
- 物料创建、位置、内容、Site、账本、SSE、设备投影一致；transfer 在 unload/load 故障时不双挂载。
- 浏览器预填 T1 仍待确认；API 唯一匹配转换成功，重名/失效 UUID/缺能力拒绝且不写半图。
- step 一次一个 attempt，两页面并发只放行一次；resume 不新建任务，重试保留 node-run 与新的 job。
- retry、人工结果替换、软超时 wait、硬超时取消、状态联锁各有对应记录，不混为一个 failed 字段。
- 按件物料与按量 lot 的预留/消耗/释放正确；库存不足在动作下发前失败。
- Host/Slave 日志无新增时无前端定时全量刷新；轮转、断线、重连、has_more 都能处理。
- 七个 demo 同时核对测试断言与实际模板/工作流，不以 --test-mode 的统一模拟成功代替业务验证。

现有测试入口见 [demo 验收](../readme_demo_tests.md)、
[步进测试](https://github.com/deepmodeling/Uni-Lab-OS/blob/862ce87a38222ac2d51739f2f89e5daecd3cd907/tests/server/test_workflow_step_execution.py)。
本次文档生成与静态/离线校验不启动真实设备，不变更当前实验室数据库。

## 后续文档维护

每次修改接口同时核对：协议模型 / handler → 本地离线契约核对 → Markdown 业务章节与例子 → 对应测试。
以独立干净 checkout 为依据审阅差异；生成脚本、JSON 和自动导出的参考表只放本地忽略目录，不提交或发布。
不要仅因模型文件名没变就认为线协议兼容；默认值、角色转发、响应信封、版本检查同样属于接口。

比较依据：[首次 Jazzy 提交](https://github.com/deepmodeling/Uni-Lab-OS/commit/99558a5a)、
[当前 dev 提交](https://github.com/deepmodeling/Uni-Lab-OS/commit/862ce87a38222ac2d51739f2f89e5daecd3cd907)。
