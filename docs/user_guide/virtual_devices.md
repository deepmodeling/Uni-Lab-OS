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

容器参数（`vessel`、`from_vessel` / `to_vessel`、`filtrate_vessel`、分液的 `separation_vessel` /
`waste_phase_to_vessel` / `product_vessel` / `waste_vessel`）都声明为 `ResourceSlot`，可不填的写成
`Optional[ResourceSlot] = None`。参数名、动作名和 ROS 2 goal 字段都没有变，变的是谁来解析它：

- **协议编译器怎么传**：统一经 `unilabos/experiments/compile/utils/vessel_parser.py` 的
  `vessel_ref(G, 容器)` 生成 `{"id": <设备图节点 id>, "uuid": <物料 uuid>}`。uuid 取物理拓扑图
  （`physical_setup_graph`）节点数据里 Graph Authority 分配的 uuid，`id` 只为日志好读；空值和设备节点
  （例如过滤协议把过滤器本身当「容器」）给 `None`。不要在编译器里手拼 `{"id": ...}`。
- **框架解析**：HostLink 设备节点在调用驱动之前按 uuid 把引用解析成本设备可见的 PLR 实例（工作站的子设备
  共用工作站的资源跟踪器；本地没有时向物料权威取），解析不到时动作失败。`None`、`""`、`{}`（可选容器的
  goal 默认值）表示未指定：不解析、不加锁，驱动拿到 `None`。ROS 2 下这些字段的类型仍由 `unilabos_msgs`
  动作决定（`Resource`），原生动作服务端在调用驱动前同样换成 PLR 实例；`Transfer` 的 `from_vessel` /
  `to_vessel` 在消息里是 string，到达时是字符串，只作标签。
- **物料锁**：单独派发这些动作（工作流节点、单动作作业）时，调度器按 uuid 给 `ResourceSlot` 参数默认加共享
  物料锁，动作结束释放；引用必须带 uuid。工作站执行协议时，锁由协议动作本身持有（它在注册表里标为资源的
  容器参数），编译出的子步骤直接调子设备、不经调度器、不再申请锁，所以不会自己等自己；同一时间对同一容器
  跑的另一个协议或单动作在调度器里排队，等协议结束。
- **设备只读标签、改内容物**：人读标签取物料的 `display_name`（PLR 实例不带它，按 uuid 在物理拓扑图里取，
  没有设备图的进程退回 PLR 名）。泵的 `transfer`、过滤器的 `filter`、柱层析的 `run_column` 在两个容器之间
  按比例转移液体，旋蒸的 `evaporate` 去掉蒸出的溶剂，固体分装器的 `add_solid` 记入固体（质量单位）。改动通过
  PLR 体积跟踪器提交，由快照观察者上报物料权威；容器留空时只模拟过程。

## `--test_mode`

虚拟设备都声明了 `run_in_test_mode = True`：测试模式下它们同样真实执行（默认倍率下很快），
没有声明的真实驱动仍返回模拟结果。

## 示例图与测试

示例图在 `unilabos/test/experiments/`：协议测试站（`Protocol_Test_Station/`、`mock_protocol/`、
`comprehensive_protocol/comprehensive_station.json`）、单设备图（`mock_devices/`）、
抽真空充气反应器（`mock_reactor.json`）和三工位加热平台（`virtual_heating_platform_demo.json`）。

`tests/devices/test_virtual_devices_hostlink.py` 覆盖：注册合同（动作名、ROS 2 动作类型与 goal 字段，容器参数
是 `ResourceSlot` 并默认加锁）、每台设备每个动作在 HostLink 下的真实执行与失败用例、时间倍率 / 进度 / 取消、
容器内容物、示例图物料构建，以及每张协议测试站的代表协议——编译出的每一步都是目标设备注册过的动作、参数都被
驱动接受、容器引用带 uuid 并解析成工作站跟踪器里的 PLR 实例，并在按图配置构造的设备上执行成功。标记为 `slow`
的用例起真实 `unilab`：跑一张协议测试站，并验证协议持有容器锁时它的子步骤照常执行、同一容器上的另一个协议和
子设备单动作排队，协议结束后锁全部释放。
