# 内置虚拟设备

`unilabos/devices/virtual/` 里的虚拟设备是协议编译器、示例图、`--test_mode` 和测试共用的模拟器。
它们和外部驱动包里的设备一样，用 `@device` / `@action` 装饰器注册（注册表条目由 AST 扫描生成，
没有单独的 YAML），在默认的 HostLink backend 下真实执行，也声明了 ROS 2 支持。

## 设备清单

| 注册表条目（设备 id） | 显示名 | 协议编译器使用的动作（ROS 2 原生动作类型） |
| --- | --- | --- |
| `virtual_transfer_pump` | 虚拟传输泵 | `set_position`（SetPumpPosition）、`set_valve_position`（SendCmd）、`transfer`（Transfer） |
| `virtual_multiway_valve` | 虚拟八通阀 | `set_valve_position` / `set_position`（SendCmd） |
| `virtual_stirrer` | 虚拟搅拌器 | `stir`（Stir）、`start_stir`（StartStir）、`stop_stir`（StopStir） |
| `virtual_heatchill` | 虚拟加热冷却器 | `heat_chill`（HeatChill）、`heat_chill_start`（HeatChillStart）、`heat_chill_stop`（HeatChillStop） |
| `virtual_solenoid_valve` | 虚拟电磁阀 | `set_valve_position`（SendCmd）、`set_status`（StrSingleInput）、`open` / `close`（EmptyIn） |
| `virtual_filter` | 虚拟过滤器 | `filter`（Filter） |
| `virtual_vacuum_pump` | 虚拟真空泵 | `set_status`（StrSingleInput）、`open` / `close`（EmptyIn） |
| `virtual_gas_source` | 虚拟气体源 | `set_status`（StrSingleInput）、`open` / `close`（EmptyIn） |
| `virtual_rotavap` | 虚拟旋转蒸发仪 | `evaporate`（Evaporate） |
| `virtual_separator` | 虚拟分离器 | `separate`（Separate）、`valve_open`（SendCmd，分液协议的底阀放液） |
| `virtual_column` | 虚拟柱层析装置 | `run_column`（RunColumn） |
| `virtual_centrifuge` | 虚拟离心机 | `centrifuge`（Centrifuge） |
| `virtual_solid_dispenser` | 虚拟固体分装器 | `add_solid`（Add） |
| `virtual_heating_platform` | 三工位虚拟加热平台 | 不被协议编译器使用；演示工位（Site）、物料权威写入与按工位遥测 |

动作名、参数名和 ROS 2 动作类型就是协议编译器与示例图依赖的合同：ROS 2 工作站只给原生动作类型建
动作客户端，并按 goal 字段转消息；HostLink 下 goal 字段就是驱动方法的参数。各设备另有带 `auto-` 前缀的
辅助动作（初始化、查询、单步操作等），动作都带中文 `display_name`。

## 写法约定

- 构造参数按类型拆开（如 `max_speed: float`），图节点的 `config` 平铺写即可；旧注册表形状
  `{"config": {...}}`（编辑器按旧 `init_param_schema` 生成）内层同样生效。
- 状态用带 `@topic_config` 的属性暴露（`status`、`progress`、`remaining_time`、温度、转速等）。
- 长动作分步推进：每步检查取消、更新进度与剩余时间，并通过 `ActionContext` 发布动作反馈；
  停止类动作（如泵的 `auto-stop_operation`）请求取消正在进行的动作。ROS 2 不注入 `ActionContext`，
  此时动作照常执行，只是不发布这份反馈、不支持取消。
- 失败一律抛异常（参数越界、无法识别的命令等），作业进入失败决策，不会把 `success=False` 当成功返回。
  成功时返回字典（`{"success": true, ...}`）。

## 模拟时间倍率

动作的模拟时长（搅拌时间、离心时间、柱塞移动距离 ÷ 速度等）按倍率缩放后真实等待：

```text
真实等待 = 模拟时长 × time_scale
```

倍率只按设备配，没有全局设置：

- 图节点 `config.time_scale`，例如 `{"time_scale": 1.0}` 按真实时长运行，`0` 不等待；
- 没配时用设备类自带的默认值 `0.001`（`VirtualDevice.default_time_scale`，1 小时的模拟操作约 3.6 秒跑完）。
- `virtual_heating_platform_demo.json` 把倍率设为 1，按真实时长画出升温曲线。

协议编译器生成的 `wait` 步骤由工作站按真实时长等待，不受这个倍率影响（例如抽真空充气协议的
抽气 / 充气等待）。

## 容器与内容物

协议编译器按 `{"id": <设备图节点 id>}` 传容器；单动作调用也可以传 uuid、字符串或 `{"uuid": ...}`，
ROS 2 原生动作里的 `Resource` 字段到达时已是 PLR 实例。设备把它们统一解析为：

- 标签：引用自带的 `display_name` → 设备图里该容器的 `display_name` → 节点 id；
- 本地物料实例：在本设备的资源跟踪器里按 uuid 查找（工作站的子设备共用工作站的跟踪器）。

找得到时，泵的 `transfer`、过滤器的 `filter`、柱层析的 `run_column` 在两个容器之间按比例转移液体，
旋蒸的 `evaporate` 去掉蒸出的溶剂，固体分装器的 `add_solid` 记入固体（质量单位）。改动通过 PLR 体积
跟踪器提交，由快照观察者上报物料权威。容器不在本设备可见范围内时只模拟过程，动作照常完成。

## `--test_mode`

虚拟设备都声明了 `run_in_test_mode = True`：测试模式下它们同样真实执行（默认倍率下很快），
没有声明的真实驱动仍返回模拟结果。

## 示例图与测试

示例图在 `unilabos/test/experiments/`：协议测试站（`Protocol_Test_Station/`、`mock_protocol/`、
`comprehensive_protocol/comprehensive_station.json`）、单设备图（`mock_devices/`）、
抽真空充气反应器（`mock_reactor.json`）和三工位加热平台（`virtual_heating_platform_demo.json`）。

`tests/devices/test_virtual_devices_hostlink.py` 覆盖：注册合同（动作名、ROS 2 动作类型与 goal 字段）、
每台设备每个动作在 HostLink 下的真实执行与失败用例、时间倍率 / 进度 / 取消、容器内容物、示例图物料构建，
以及每张协议测试站的代表协议——编译出的每一步都是目标设备注册过的动作、参数都被驱动接受，并在按图配置
构造的设备上执行成功。标记为 `slow` 的用例起真实 `unilab` 跑一张协议测试站。
