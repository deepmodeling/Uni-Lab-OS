# prcxi_socket_client_v04 v7.py 适配 V04-v7.0.0 开发报告

日期：2026-07-08

## 适配依据

本次修改依据 `comparison-v6-v7.md`，核心结论是：V04 v7 的方案添加路径从“客户端本地生成/写入 XML”改为调用 `ISolution.AddSolution_V04(name, boardId, steps)`，由服务端生成方案 XML。纯 socket 客户端需要补齐 V04 v7 的方案步骤参数模型和 RPC 封装。

## 修改文件

- `prcxi_socket_client_v04 v7.py`
- `client-dev-report_v04_v7_260708.md`

## 主要改动

1. 新增 V04 v7 方案步骤模型：
   - `SolutionStepV04`
   - `LoadTipsStepV04`
   - `AspirateStepV04`
   - `DispenseStepV04`
   - `MixStepV04`
   - `UnloadTipsStepV04`
   - `TempSetStepV04`
   - `TempAndOscStepV04`
   - `OscSetStepV04`
   - `MagneticStandStepV04`
   - `LiquidCoolSetStepV04`
   - `PauseStepV04`
   - `MvKitStepV04`

2. 新增 `create_demo_solution_steps_v04()`：
   - 默认生成与 v7 C# Demo 接近的 11 步方案。
   - 可通过 `include_liquid_cool=True` 插入液冷步骤。

3. 新增客户端方法 `solution_add_v04()`：
   - 调用 RPC：`ISolution.AddSolution_V04`
   - 参数：`plan_name`、`board_id`、`steps`
   - 内置方案名和 `board_id` 空值检查。

4. 更新旧接口说明：
   - `legacy_solution_add()` 保留用于旧版对照。
   - 明确 V04 v7 应使用 `solution_add_v04()`，旧 `AddSolution(List<StepData>)` 不推荐。

5. 增强 `parse_data()`：
   - 支持解析 `Data` 中的 JSON 字符串，例如 `AddSolution_V04` 可能返回的带引号方案名。

6. 保留并强调协议细节：
   - 请求 JSON 字段仍使用服务端历史拼写 `Paramters`，不能改成 `Parameters`。

7. 更新使用示例：
   - 增加默认注释的 v7 添加方案示例。
   - 示例先从 `matrix_get_all()` 获取 `Board.Id`，再调用 `solution_add_v04()`。

## v7 添加方案调用示例

```python
sdk = PrcxiSocketClientV04(host="127.0.0.1", port=9999, timeout=15)

boards = parse_data(sdk.matrix_get_all()) or []
board_id = boards[0].get("Id")

resp = sdk.solution_add_v04(
    "python_v7_demo",
    board_id,
    create_demo_solution_steps_v04(),
)

print(parse_data(resp))
```

## 二次开发注意事项

- 调用 `solution_add_v04()` 前必须先获取有效 `Board.Id`。
- 方案步骤传 RPC 模型，不要传旧 `StepData`，也不要传离线 XAML 模型。
- RPC 的卸载枪头步骤 `Kind` 是 `UnloadTips`，不是 XAML 里的 `UnLoadTips`。
- 液体步骤的 `tips` 默认使用 `Tips1`，避免使用服务端枚举中不存在的 0 值。
- `LiquidCoolSetStepV04` 是根据 v7 文档和旧 C# 步骤字段补齐的纯 socket 封装；如果服务端字段有进一步变化，需要按服务端模型调整。
- 添加/删除方案后服务端列表刷新可能有延迟，自动化脚本建议轮询 `solution_get_list()`。

## 验证结果

- 已执行：`python -m py_compile "prcxi_socket_client_v04 v7.py"`
- 结果：通过。
- IDE 诊断：未发现 linter 错误。
