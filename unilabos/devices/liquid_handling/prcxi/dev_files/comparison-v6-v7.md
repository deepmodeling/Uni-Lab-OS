# V04 v6.0.0 与 v7.0.0 差异对比

对比对象：

- `V04-v6.0.0`
- `V04-v7.0.0`

结论先行：v7 对基于 SDK 的二次开发影响主要集中在“添加方案”能力。v6 的方案添加示例依赖客户端本地生成 XAML/XML 并写入 NeonGenesis `project` 目录；v7 改为通过 `ISolution.AddSolution_V04(name, boardId, steps)` 远程创建方案，由服务端根据传入的 V04 步骤模型生成 XML。C# SDK 依赖也从 `Prcxi.Lilith.Client` 1.4.1 升级到 1.4.7，并移除本地 `Prcxi.Alilat.Pipette` 项目依赖。

## 一、会影响 SDK 二次开发的变化

### 1. C# SDK 版本升级

| 项目 | v6.0.0 | v7.0.0 | 影响 |
|---|---|---|---|
| `Prcxi.Lilith.Client` | `1.4.1`（文档里仍有 1.3.1 描述） | `1.4.7` | 需要升级 NuGet 包后才能使用 `AddSolution_V04` 和新的 V04 步骤模型。 |
| `Prcxi.Alilat.Core` | `1.0.5-fix.1` | 移除 | 二次开发不再需要为了方案生成引用该包。 |
| `Prcxi.Alilat.Pipette` | 本地项目引用 | 移除 | v6 中用于本地构造 XAML 步骤的类型不再随 Demo 源码提供；v7 改用 `Prcxi.Lilith.Model` 内置的 `SolutionStepBase_V04` 子类。 |

影响说明：

- 如果现有二次开发项目仍按 v6 Demo 引用 `Prcxi.Alilat.Core`、本地 `Prcxi.Alilat.Pipette` 或自己写 XAML 文件，需要迁移到 v7 的 `Prcxi.Lilith.Client` 1.4.7。
- 如果只调用 `Start`、`Stop`、`Pause`、`Resume`、`Reset`、`GetSolutionList`、`LoadSolution`、`GetStepStateList`、`IMatrix *_V04` 等已有 RPC，接口名未观察到变化，主要风险是 NuGet 版本升级后的模型兼容性。

### 2. 添加方案方式改变

v6 的 C# Demo：

- `Form1.cs` 中有 `PlanPath` 固定路径。
- `button9_Click` 调用 `SaveXml(textBox1.Text)`。
- `SaveXml()` 用 `GlobalActivity`、`DeckInfo`、`XamlServices.Save()` 在客户端本地生成 XML。
- 生成文件直接保存到 NeonGenesis `project` 目录。
- 文档说明 `AddSolution(...)` 服务端未开放，添加方案靠“本地文件方式”。

v7 的 C# Demo：

- 删除 `PlanPath`、`SaveXml()`、`CreatBaseActivity()` 和 `System.Xaml`/`System.Xml.Linq` 相关引用。
- `button9_Click` 改为调用：

```csharp
var planName = solution.AddSolution_V04(
    textBox1.Text,
    _boards.First().Id,
    CreateSolutionSteps());
```

- 新增 `CreateSolutionSteps()`，返回 `List<SolutionStepBase_V04>`。
- 服务端负责生成 `{BaseDirectory}/project/{name}.xml` 并刷新方案列表。

影响说明：

- 添加方案前必须先调用 `matrix.GetWorkTabletMatrices_V04()` 获取布局，并传入真实存在的 `Board.Id` 作为 `boardId`。
- 方案步骤不再是本地 `Prcxi.Alilat.Pipette` 的 `BaseActivity`/`LoadTips`/`Aspirate` 等类型，而是 `Prcxi.Lilith.Model` 中的 `SolutionStepBase_V04` 子类。
- 旧版 `AddSolution(string name, string matrixId, List<StepData> data)` 在 v7 文档中明确标为 V04 服务端不可用，应使用 `AddSolution_V04`。
- `AddSolution_V04` 失败时通常抛异常，应捕获 `Exception.Message`；`IMatrix` 写操作仍返回 `Result`，需检查 `Success` 和 `Message`。

### 3. 新增 V04 方案步骤模型

v7 新增 Python SDK 文件：

- `RPC_Python_V04/prcxi_sdk/models/solution_steps_v04.py`

该文件新增了 Python 侧可序列化的 V04 步骤模型，用于调用 `AddSolution_V04`：

- `SolutionStepV04` 协议：要求实现 `to_rpc_dict()`
- `LoadTipsStepV04`
- `AspirateStepV04`
- `DispenseStepV04`
- `MixStepV04`
- `UnloadTipsStepV04`
- `TempSetStepV04`
- `TempAndOscStepV04`
- `OscSetStepV04`
- `MagneticStandStepV04`
- `PauseStepV04`
- `MvKitStepV04`
- `SolutionStepV04Type`
- `create_demo_solution_steps()`

影响说明：

- Python 二次开发可以直接构造这些 dataclass，然后传给 `client.solution.add_solution_v04()`。
- 每个步骤会序列化出 `Kind` 字段，服务端依赖它判断具体步骤类型。
- 液体类步骤需要传 `AxisType`、`Tips` 等字段；v7 C# 文档特别提醒 `Tips` 使用 `TipsType_V04.Tips1` / `Tips8` / `Tips96`，不要使用默认 0 值。
- Python 模型当前没有覆盖 C# 文档中提到的 `LiquidCoolSetStep_V04`，如果二次开发需要液体冷却步骤，需要补充 Python 模型或直接构造 RPC 字典。
- 注意 `prcxi_workflow` 离线 XAML 模型与 `solution_steps_v04` RPC 模型存在命名差异：XAML 里是 `UnLoadTips`，RPC `Kind` 是 `UnloadTips`；v6 离线 demo 默认 7 步，v7 RPC demo 默认 11 步。

### 4. Python SDK 新增 `add_solution_v04`

文件：

- `RPC_Python_V04/prcxi_sdk/services/solution.py`

v6：

```python
def get_solution_list(self) -> List[Solution]: ...
def load_solution(self, solution_id: str) -> Any: ...
def remove_solution(self, name: str) -> Any: ...
```

v7 新增：

```python
def add_solution_v04(self, name: str, board_id: str, steps: Sequence[SolutionStepV04]) -> Any:
    return self._invoke("AddSolution_V04", [name, board_id, list(steps)]).data
```

影响说明：

- Python SDK 二次开发多了正式的远程添加方案入口。
- 参数顺序是 `name`、`board_id`、`steps`。
- `steps` 中每一项需要能被底层序列化逻辑转换为 RPC JSON；v7 新增步骤类通过 `to_rpc_dict()` 支持这一点。
- `prcxi_sdk/models/__init__.py` 和 `prcxi_sdk/__init__.py` 没有导出这些新步骤类型，二次开发需要显式从 `prcxi_sdk.models.solution_steps_v04` 导入。
- Python SDK 包内 `__version__` 未随新增 API 更新，仍应以实际文件/API 为准，不要只依赖版本字符串判断能力。

### 5. Python GUI 添加方案路径改为 RPC

文件：

- `RPC_Python_V04/prcxi_gui/main_window.py`

v6：

- 导入 `re`
- 导入 `find_neon_genesis_project_dirs`
- 导入 `create_demo_plan` / `write_plan_xaml`
- 维护 `_solution_project_dirs`
- `_create_plan_xml()` 生成离线 XAML/XML，并尝试写入 NeonGenesis 方案目录。
- 包含 `_safe_file_stem()`、`_plan_output_paths()`、`_local_generated_plan_path()`、`_infer_solution_project_dirs()` 等本地文件写入辅助函数。

v7：

- 删除本地写 XML 的相关导入和辅助函数。
- 导入 `create_demo_solution_steps`
- `_create_plan_xml` 改名/替换为 `_create_plan_v04`
- 添加方案时检查当前是否连接、是否已有布局、方案名是否为空、`Board.Id` 是否有效。
- 调用 `self._client.solution.add_solution_v04(plan_name, board_id, create_demo_solution_steps())`
- 添加/删除方案后用 `QTimer.singleShot(3000, self._refresh_solutions)` 延迟刷新列表。

影响说明：

- GUI 行为与正式 SDK 路径对齐，不再要求客户端能访问服务端 `project` 目录。
- 二次开发中如果复用 GUI 逻辑，应优先参考 v7 的 `_create_plan_v04()`。
- 删除方案后刷新从立即刷新改为 3 秒后刷新，说明服务端文件/列表刷新可能存在异步延迟。

### 6. C# Demo 连接端口变化

文件：

- `RPC_Demo_V04/Demo_RPC_V04/Form1.cs`

v6：

```csharp
LilithClient lilithClient = new LilithClient("127.0.0.1", 52230);
```

v7：

```csharp
LilithClient lilithClient = new LilithClient("127.0.0.1", 9999);
```

影响说明：

- v7 与文档默认端口 `9999` 一致。
- 二次开发不要硬编码 v6 Demo 中的 `52230`，应读取 NeonGenesis 的 `RPCIpPort` 或允许配置。

### 7. RPC 文档内容更新

文件：

- `RPC_Demo_V04/Demo_RPC_V04/RPC调用文档.md`
- `RPC_Demo_V04/GenWordDoc/Program.cs`
- v7 顶层新增 `RPC调用文档.md`

关键变化：

- SDK 版本从文档里的 `1.3.1` 更新为 `1.4.7`。
- NuGet 依赖从 `Prcxi.Lilith.Client` + `Prcxi.Alilat.Core` 变为仅 `Prcxi.Lilith.Client`。
- 前置条件新增：调用 `AddSolution_V04` 前先获取布局和有效 `boardId`。
- `ISolution` 方法表新增 `AddSolution_V04(string name, string boardId, List<SolutionStepBase_V04> steps)`。
- 添加方案示例从“本地生成 XML 保存到 project 目录”改为“V04 RPC 方式”。
- 新增 V04 步骤类型列表、继承关系、`Kind` 字段说明、`Tips` 枚举注意事项。
- 注意事项新增旧版 `AddSolution` 不可用、`AddSolution_V04` 异常处理。

影响说明：

- v7 文档是二次开发时更可靠的接口说明。
- 顶层 `RPC调用文档.md` 与 Demo 目录内文档内容一致，便于直接查阅。

### 8. Python XAML 工作流模块地位降低

文件：

- `RPC_Python_V04/README.md`
- `RPC_Python_V04/prcxi_workflow/XAML_FORMAT.md`

v6 README：

- 说明“生成方案 XML 输出到 `examples/generated/`”。

v7 README：

- 改为说明“添加方案”调用 `ISolution.AddSolution_V04`，由服务端生成方案 XML。
- `examples/generated/` 仅保留离线示例脚本输出。

v6 `XAML_FORMAT.md`：

- GUI 应调用 `prcxi_workflow.create_demo_plan()` / `write_plan_xaml()`。

v7 `XAML_FORMAT.md`：

- 明确当前 GUI 添加方案入口已改为 `ISolution.AddSolution_V04`。
- `prcxi_workflow` 保留为离线示例、格式比对和开发参考。

影响说明：

- 二次开发不应再把 `prcxi_workflow` 当成正式添加方案路径。
- 如果要离线生成/比对 XML，`prcxi_workflow` 仍可作为参考，但真实设备方案创建应走 RPC。

## 二、有效源码与文档差异清单

以下列表排除了 `.git`、`.vs`、`bin`、`obj`、`__pycache__`、`logs`、`.exe`、`.dll`、`.pdb`、`.pyc`、`.cache`、`.docx` 等生成/运行/二进制文件后得到，共 39 项。

### 修改的文件

| 文件 | 变化摘要 | 是否影响二次开发 |
|---|---|---|
| `RPC_Demo_V04/Demo_RPC_V04.sln` | 移除 `Prcxi.Alilat.Pipette` 项目，只保留 `Demo_RPC_V04`。 | 是，C# Demo 编译依赖变化。 |
| `RPC_Demo_V04/Demo_RPC_V04/Demo_RPC_V04.csproj` | `Prcxi.Lilith.Client` 从 1.4.1 升到 1.4.7；移除 `Prcxi.Alilat.Core`；移除 `Prcxi.Alilat.Pipette` 项目引用。 | 是，NuGet 依赖需要迁移。 |
| `RPC_Demo_V04/Demo_RPC_V04/Form1.cs` | 添加方案从本地写 XML 改为 `solution.AddSolution_V04`；连接端口改为 9999；新增 V04 步骤构造示例；移除 XAML 生成代码。 | 是，核心接口使用方式变化。 |
| `RPC_Demo_V04/Demo_RPC_V04/RPC调用文档.md` | 更新 SDK 版本、依赖、`AddSolution_V04` 说明、步骤模型和注意事项。 | 是，开发文档变化。 |
| `RPC_Demo_V04/GenWordDoc/Program.cs` | 生成 Word 文档的内容同步更新为 v7 接口说明。 | 间接影响，仅文档生成器。 |
| `RPC_Python_V04/README.md` | 添加方案说明改为调用 `ISolution.AddSolution_V04`。 | 是，Python GUI/SDK 使用说明变化。 |
| `RPC_Python_V04/prcxi_gui/main_window.py` | GUI 添加方案从写 XML 改为 `add_solution_v04`；删除本地方案目录查找和文件写入逻辑。 | 是，Python 参考实现变化。 |
| `RPC_Python_V04/prcxi_sdk/services/solution.py` | 新增 `add_solution_v04()`。 | 是，Python SDK 新增能力。 |
| `RPC_Python_V04/prcxi_workflow/XAML_FORMAT.md` | 说明 `prcxi_workflow` 不再作为 GUI 添加方案路径，仅保留离线参考。 | 是，影响方案生成路线选择。 |

### 新增的文件

| 文件 | 变化摘要 | 是否影响二次开发 |
|---|---|---|
| `RPC_Demo_V04/NuGet.config` | 新增本地包源 `local-lilith` 和 `nuget.org`。 | 可能影响。若本机没有 `e:\storehouse\垃圾桶\Prcxi.Lilith\RPCDemo\local-nuget`，还原包可能失败或退回公网源。 |
| `RPC_Python_V04/prcxi_sdk/models/solution_steps_v04.py` | 新增 Python V04 方案步骤模型和 demo 步骤构造函数。 | 是，Python 添加方案必须关注。 |
| `RPC调用文档.md` | v7 顶层新增一份 RPC 调用文档。 | 是，便于查阅 v7 接口。 |

### 删除的文件/目录

v7 删除了 v6 中的本地 C# `Prcxi.Alilat.Pipette` 项目源码：

- `RPC_Demo_V04/Prcxi.Alilat.Pipette/Pipette/Activity/CommandActivity.cs`
- `RPC_Demo_V04/Prcxi.Alilat.Pipette/Pipette/Command/LiquidCommand.cs`
- `RPC_Demo_V04/Prcxi.Alilat.Pipette/Pipette/Command/PositionCommand.cs`
- `RPC_Demo_V04/Prcxi.Alilat.Pipette/Pipette/Command/SpeedCommand.cs`
- `RPC_Demo_V04/Prcxi.Alilat.Pipette/Pipette/Enums/AdpSelector.cs`
- `RPC_Demo_V04/Prcxi.Alilat.Pipette/Pipette/Enums/AspirateMethod.cs`
- `RPC_Demo_V04/Prcxi.Alilat.Pipette/Pipette/Enums/AxisCode.cs`
- `RPC_Demo_V04/Prcxi.Alilat.Pipette/Pipette/Enums/DeepWellPlateType.cs`
- `RPC_Demo_V04/Prcxi.Alilat.Pipette/Pipette/Enums/DispenseMethod.cs`
- `RPC_Demo_V04/Prcxi.Alilat.Pipette/Pipette/Enums/DispensingDirectionEnum.cs`
- `RPC_Demo_V04/Prcxi.Alilat.Pipette/Pipette/Enums/MvKitDirect.cs`
- `RPC_Demo_V04/Prcxi.Alilat.Pipette/Pipette/Enums/PauseEnum.cs`
- `RPC_Demo_V04/Prcxi.Alilat.Pipette/Pipette/Enums/TipsType.cs`
- `RPC_Demo_V04/Prcxi.Alilat.Pipette/Pipette/Enums/ZLocationEnum.cs`
- `RPC_Demo_V04/Prcxi.Alilat.Pipette/Pipette/Step/Aspirate.cs`
- `RPC_Demo_V04/Prcxi.Alilat.Pipette/Pipette/Step/Dispense.cs`
- `RPC_Demo_V04/Prcxi.Alilat.Pipette/Pipette/Step/LiquidCoolSet.cs`
- `RPC_Demo_V04/Prcxi.Alilat.Pipette/Pipette/Step/LoadTips.cs`
- `RPC_Demo_V04/Prcxi.Alilat.Pipette/Pipette/Step/MagneticStand.cs`
- `RPC_Demo_V04/Prcxi.Alilat.Pipette/Pipette/Step/Mix.cs`
- `RPC_Demo_V04/Prcxi.Alilat.Pipette/Pipette/Step/MvKit.cs`
- `RPC_Demo_V04/Prcxi.Alilat.Pipette/Pipette/Step/OscSet.cs`
- `RPC_Demo_V04/Prcxi.Alilat.Pipette/Pipette/Step/Pause.cs`
- `RPC_Demo_V04/Prcxi.Alilat.Pipette/Pipette/Step/TempAndOsc.cs`
- `RPC_Demo_V04/Prcxi.Alilat.Pipette/Pipette/Step/TempSet.cs`
- `RPC_Demo_V04/Prcxi.Alilat.Pipette/Pipette/Step/UnLoadTips.cs`
- `RPC_Demo_V04/Prcxi.Alilat.Pipette/Prcxi.Alilat.Pipette.csproj`

影响说明：

- 这些文件在 v6 中主要支撑本地 XAML 方案生成；v7 改为通过 `Prcxi.Lilith.Model` 的 V04 步骤模型和服务端生成 XML。
- 如果二次开发项目复制过这些源码，需要决定是继续维护本地 XAML 路径，还是迁移到 `AddSolution_V04`。

## 三、生成文件、缓存、日志、二进制差异

这些差异是真实存在的目录差异，但一般不应作为 SDK 接口变化解读。

### 1. 顶层发布包/安装包

| v6.0.0 | v7.0.0 | 影响 |
|---|---|---|
| `SC93-4.6.4.2-V04.exe` | `SC93-4.8.2.1-V04.exe` | 服务端/主程序版本升级。会影响运行时行为，尤其是是否支持 `AddSolution_V04`。 |
| `RPC_Python调用文档.docx` | 同名 docx 内容变化 | 文档更新。 |
| `RPC调用文档.docx` | 同名 docx 内容变化 | 文档更新。 |
| 无 | `RPC调用文档.md` | v7 新增 Markdown 版文档。 |

### 2. C# 构建产物

v7 新增或变更了大量 `bin`/`obj` 文件，包括：

- `RPC_Demo_V04/Demo_RPC_V04/bin/Release/net8.0-windows/*`
- `RPC_Demo_V04/Demo_RPC_V04/obj/Release/net8.0-windows/*`
- `RPC_Demo_V04/GenWordDoc/bin/Release/net8.0/*`
- `RPC_Demo_V04/GenWordDoc/obj/Release/net8.0/*`
- `RPC_Demo_V04/Demo_RPC_V04/obj/project.assets.json`
- `RPC_Demo_V04/Demo_RPC_V04/obj/project.nuget.cache`
- `RPC_Demo_V04/GenWordDoc/obj/project.assets.json`
- `RPC_Demo_V04/GenWordDoc/obj/project.nuget.cache`

v6 中的 `Prcxi.Alilat.Pipette/bin` 和 `Prcxi.Alilat.Pipette/obj` 产物在 v7 中随项目删除。

影响说明：

- 这些文件是构建输出，不应直接作为 SDK 源码依赖。
- 但 Release 输出显示 v7 已按 `Prcxi.Lilith.Client` 1.4.7 和相关运行依赖重新构建。

### 3. Visual Studio 与 Git 元数据

差异中包含：

- `RPC_Demo_V04/.vs/**`
- `RPC_Demo_V04/.git/**`

影响说明：

- 属于本地 IDE 状态和嵌套仓库元数据，不影响 SDK 二次开发。
- 不建议把这些差异纳入迁移依据。

### 4. Python 运行缓存和日志

差异中包含：

- `RPC_Python_V04/logs/rpc-*.txt`
- `RPC_Python_V04/prcxi_gui/**/__pycache__/*.pyc`
- `RPC_Python_V04/prcxi_sdk/**/__pycache__/*.pyc`
- `RPC_Python_V04/prcxi_sdk/models/__pycache__/solution_steps_v04.*.pyc`

影响说明：

- `logs` 是 GUI/RPC 运行记录。
- `__pycache__` 是 Python 编译缓存。
- 它们不代表 SDK 接口变化。

### 5. 示例构建缓存

差异中包含：

- `RPC_Python_V04/examples/xaml_reference/obj/**`

影响说明：

- 是 C# 示例项目构建缓存，不影响 Python SDK 接口。

## 四、迁移建议

1. C# 二次开发项目升级到 `Prcxi.Lilith.Client` 1.4.7。
2. 移除对本地 `Prcxi.Alilat.Pipette` 项目和 `Prcxi.Alilat.Core` 的直接依赖，除非仍需维护 v6 的离线 XAML 生成路径。
3. 添加方案时改用 `solution.AddSolution_V04(name, boardId, steps)`。
4. 添加方案前先调用 `matrix.GetWorkTabletMatrices_V04()`，使用返回的 `Board.Id`。
5. 步骤列表使用 `Prcxi.Lilith.Model` 中的具体 V04 步骤子类，如 `LoadTipsStep_V04`、`AspirateStep_V04`、`DispenseStep_V04`、`MixStep_V04` 等。
6. Python 二次开发使用 `client.solution.add_solution_v04(name, board_id, steps)`，并优先复用 `prcxi_sdk.models.solution_steps_v04` 中的步骤模型。
7. 对旧代码中硬编码的端口进行检查，v7 Demo 和文档统一使用 `9999`，实际项目建议改为配置项。
8. 不要再把 `prcxi_workflow.write_plan_xaml()` 作为正式设备添加方案路径；它现在更适合作为离线参考和格式对比工具。

## 五、需要特别注意的风险

- `NuGet.config` 使用了本机绝对路径 `e:\storehouse\垃圾桶\Prcxi.Lilith\RPCDemo\local-nuget` 作为包源。其他机器可能无法还原，需要改为可访问的内部包源或确认 `nuget.org` 上存在对应包。
- v7 Python 步骤模型未完全覆盖文档中列出的所有 C# V04 步骤，尤其是 `LiquidCoolSet`。如果二次开发需要该步骤，需要补模型。
- `AddSolution_V04` 依赖服务端版本。必须使用支持该接口的 NeonGenesis/SC93 版本，例如 v7 顶层发布包 `SC93-4.8.2.1-V04.exe`。
- v7 添加/删除方案后示例采用 3 秒延迟刷新，说明服务端状态刷新可能不是立即可见；自动化脚本中应考虑重试或轮询。
- RPC 请求 JSON 历史字段名仍为 `Paramters`，不是标准拼写 `Parameters`；跨语言自行实现客户端时必须保持该字段名。
