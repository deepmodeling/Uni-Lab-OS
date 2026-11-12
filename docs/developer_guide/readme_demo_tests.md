# 七个 demo 的工作流回归

入口：`tests/e2e/test_readme_demos.py`。版本清单：`tests/e2e/readme_demos.py`。
用例经真实 CLI、独立运行时进程和公开 HTTP API 执行，不在 pytest 内拼接执行器。
共 27 个模板工作流；物料第三阶段另上传一张三节点图，缺料失败后补料重跑，共 29 次任务提交。

| Demo | 核对的业务证据 |
| --- | --- |
| LAN | 跨设备 Topic 订阅闭环，以及远端 echo → stop → start 的轮次递增 |
| Workstation | 子设备路由和动作登记、PONG、slave_id 3/7、串口调用 1 次、Modbus 操作 4 次 |
| Exception | abort 保留原异常、人工替换不增加 attempt、retry 新建 attempt 并关联失败记录 |
| ComplexWorkflow | for / 状态 while / 结果 while / 嵌套循环的实际样品、孔位、温度和迭代历史 |
| Materials | 两轮位点流转、内容物同步、卸载重建、递归删除；按件和按量联合预留失败回滚，补料后出库挂到 Slave 并给 A1 加水 1200 ul，lot 剩余 9300 ul |
| Lock | 同动作与同板排队、blockers 关联本组 attempt、always_free 并行、独立设备账本审计 |
| Inventory | 每步完成后直查库存：100 → 60 → 不足拒绝仍为 60 → 盘点 60，reserved 归零 |

## 运行方式

默认使用独立 Python 3.14 环境，安装默认包和 `pytest` / `pytest-asyncio`，不安装 ROS。
开发者可直接使用 `python scripts/dev_install.py --extras full`。ROS2 测试另用
Jazzy/Humble 环境，需要该发行版的原生 `rclpy`、DDS 与 `unilabos_msgs`，不能只安装消息占位包。

PowerShell 示例：

```powershell
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD = '1'
$env:UNILABOS_E2E_BACKEND = 'hostlink'
$env:UNILABOS_E2E_TOPOLOGY = 'split'
python -m pytest tests/e2e/test_readme_demos.py -p anyio.pytest_plugin -q
```

分别运行 `hostlink` / `ros2` 与 `single` / `split` 的四种组合。
CI 在 Jazzy、Humble 环境各自执行四种组合；发行版矩阵本身不代表已测试 ROS2 transport。
`single` 指权威与 Host 同进程，LAN / Materials 仍有单独的 Slave；`split` 指默认的权威 + Host 子进程拓扑。

默认只取清单固定的提交，不隐式使用 `../LabDevice…Demo`。本地修改联调或离线运行时，显式设置
`UNILABOS_README_EXAMPLES_ROOT`，目录下按仓库名准备所选 demo 的完整源码。
显式目录错误、拉取失败、协议不匹配均报错，不以 skip 变绿。缓存有修改时保留原目录，另取干净的同版本副本。

所有实例使用临时数据库、独立端口及机器名，不读取用户的 `UNILABOS_*` 配置覆盖或 demo 控制变量。
注册表校验和图生成也在临时工作目录执行，避免污染 demo checkout。结束时清理本测试启动的进程树。

## 无 ROS 安装验证

从 0.12.3 起，CI 增加独立的无 ROS Python 安装矩阵（Windows、Linux、macOS）：
只安装默认包和测试工具，执行 `pip check` 和 `verify_installation.py --assert-no-ros`，再运行七个 demo 的
完整 CLI/HTTP/HostLink 链路。不能用已安装 Jazzy/Humble 的环境代替此项。
证据产物为 `no-ros-evidence-<系统>`；原有 ROS2 的两发行版回归继续保留。

Slave 在物料和服务初始化完成前仅登记通信归属，不进入调度能力快照。
测试应等待真实就绪状态，不靠固定 sleep 掩盖 Site 尚未创建的竞态。

## Site 与业务断言

API 导入（`from-template`，以及显式传 `site_binding_mode=resolve` 的 `PUT /graph`）使用 `resolve`。标记为 Site 的 `T3` / `T4` 会被解析为权威 UUID；动作回报 UUID 是正常契约。
测试必须验证 UUID 对应的 label、owner 和实际占位物料，不应取消绑定、退回旧的 `type` / `class_name` 字段或只断言 succeeded。
浏览器仍需确认物料/Site 绑定；自动导入和人工确认的区别见 [UI API](edge_ui_api.md)。

主用例在保留原始返回值的基础上复用 demo 的语义断言。旧 smoke 接收 label 或决策 action 字符串时，
只转换断言输入，不改原始协议证据；物料最终状态从当前 `config.type` 和权威树读取。

## 失败证据

pytest 临时目录保留 `workflow-proofs.json`、`material-flow-proof.json`、Host/Slave 日志和独立数据库。
CI 将四组运行目录上传为 `readme-demo-evidence-<发行版>`。
遇到失败先看当前任务的 node-runs / attempts / error decision，再对照设备日志与物料账目；不要直接放宽断言。

本回归不调用真实 Codex，也不代表微前端浏览器交互或自然语言 MCP 流程已经重测；这些有独立的验收入口。
