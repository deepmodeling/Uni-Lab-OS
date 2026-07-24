# PRCXI V04 v7 驱动适配日志

日期：2026-07-08

## 背景

依据 `prcxi_socket_client_v04.py` 与 `prcxi_socket_client_v04 v7.py` 的差异，以及
`comparison-v6-v7.md` / `client-dev-report_v04_v7_260708.md` 的说明，V04 v7 的核心变化是：

- 旧版 V04 不再依赖客户端本地生成/写入 XML 方案。
- 添加方案改为调用 `ISolution.AddSolution_V04(name, boardId, steps)`。
- `steps` 必须是带 `Kind` 字段的 V04 方案步骤模型，例如 `LoadTips`、`Aspirate`、`Dispense`、`Mix`、`UnloadTips`、`MvKit`。
- `boardId` 必须来自 V04 `Board.Id`，也就是当前驱动中的 `matrix_id`。

## 修改内容

### 1. 新增旧步骤到 V04 v7 步骤模型的转换

在 `prcxi.py` 中新增 `legacy_steps_to_v04_solution_steps()`，把驱动内部历史 `Function` 风格步骤转换为
`AddSolution_V04` 可接受的 payload：

- `Load` -> `Kind=LoadTips`
- `UnLoad` -> `Kind=UnloadTips`
- `Imbibing` -> `Kind=Aspirate`
- `Tapping` -> `Kind=Dispense`
- `Blending` -> `Kind=Mix`
- 相邻 `DefectiveLift` + `PutDown` -> 单个 `Kind=MvKit`
- `Shaking` -> `Kind=OscSet`

同时补充 `_v04_axis_type()`、`_v04_tips_type()`、`_v04_position_fields()` 等辅助函数，统一生成
`AxisType`、`Tips`、`Position`、`Row`、`Col` 等 v7 字段。

### 2. 补齐 `PRCXI9300Api.add_solution_v04()`

新增 `add_solution_v04(name, board_id, steps)`：

- 仅允许在 `protocol_version="v04"` 下调用。
- 校验方案名、`board_id` 和步骤列表。
- 调用 `ISolution.AddSolution_V04`，参数顺序为 `[plan_name, board_id, v04_steps]`。
- 保留旧 `add_solution()` 仅供 legacy 协议使用，并将 v04 下的错误提示更新为指向 `AddSolution_V04`。

### 3. 修改 V04 运行链路

`PRCXI9300Backend._run_protocol_v04()` 从“无 `protocol_id` 时模拟运行”改为真实 v7 链路：

1. 使用当前 `protocol_name` 或时间戳生成方案名。
2. 调用 `api_client.add_solution_v04(plan_name, self.matrix_id, self.steps_todo_list)`。
3. 使用返回的方案名或原方案名调用 `LoadSolution`。
4. 调用 `Start` 并等待 `GetStartStatus` / `GetStepStateList` 完成判定。

如果外部显式传入 `protocol_id`，仍按已有方案名直接 `LoadSolution(protocol_id)`，不重复创建方案。

### 4. 调整 debug 与错误处理

- debug mock 增加 `AddSolution_V04`，返回传入的方案名，便于本地无设备验证链路。
- RPC 失败时同时读取 `Message` 与 `Msg`，兼容 v7 socket client 的响应字段。

## 注意事项

- 当前转换保留驱动原有“先积累旧 StepData 风格步骤，再运行时转 V04 v7 payload”的结构，避免大范围改写 aspirate/dispense/pick/drop 的步骤生成逻辑。
- `MvKit` 需要源/目标槽位；驱动会把相邻的 `DefectiveLift` 和 `PutDown` 合并为一个移动步骤。若出现孤立夹取/放下步骤，会退化为同槽位 `MvKit`，后续真机联调可按厂商细节继续收紧。
- `Tips` 根据 `HoleNumbers` 推断为 `Tips1` / `Tips8` / `Tips96`，避免使用服务端枚举默认 0 值。
- 服务端添加/删除方案后方案列表刷新可能存在延迟；当前运行链路创建后立即 `LoadSolution`，如真机发现服务端刷新异步影响加载，可再加短轮询重试。
