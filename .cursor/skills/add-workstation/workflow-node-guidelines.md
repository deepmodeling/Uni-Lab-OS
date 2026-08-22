# 工作流节点指南

新增或修改工作站、子设备对工作流暴露的 `@action` 节点时阅读本文件。本文说明通用的节点 schema、参数分组、展示名、handles（连线端口）、日志和错误语义。具体工作站的命名、必填标记、内部字段展示方式等项目约定，仍以当前仓库文档或对应专项参考为准。

## 目录

1. 把暴露动作当成契约
2. 注册表可见性
3. 展示名和说明文本
4. 默认值与必填语义
5. 变量排序
6. 变量分组
7. Handles（连线端口）与返回数据
8. 人工确认（manual-confirm）节点
9. `always_free`
10. 日志与状态写法
11. 阻塞错误与非阻塞错误
12. 工作流执行状态
13. 验证清单

---

## 1. 把暴露动作当成契约

暴露给工作流的 `@action` 不只是一个 Python 方法。它同时是以下几层之间的契约：

- Python 运行时；
- registry AST 扫描器；
- 工作流图编辑器；
- 上游/下游 handles；
- 操作员看到的 UI；
- 工作流执行器的状态和错误处理。

修改一个 action 输入时，要同步检查这些表面：

- 方法签名；
- `@action(...)` metadata；
- `goal_default`；
- handles；
- 变量分组结构；
- docstring 的 `Args:`；
- output handle 依赖的返回字段；
- check-mode 或契约测试。

---

## 2. 注册表可见性

工作流要调用的方法，尽量直接写在被 `@device` 装饰的类体内。AST 扫描最可靠识别的是被装饰类里直接声明的方法。

如果真实逻辑在基类、helper、mixin 或组合对象中，在被装饰类上加薄 wrapper：

```python
@action(description="提交实验到外部调度系统")
def submit_experiment(self, params: SubmitExperimentParams) -> Dict[str, Any]:
    return self._submit_experiment_impl(params)
```

不希望暴露给工作流的公共 helper 用 `@not_action` 标记。变量分组结构、`TypedDict`、枚举和 decorator 依赖也要保持 import-visible。

---

## 3. 展示名和说明文本

展示名常来自 docstring、变量分组字段和 handles。除非有明确 UI 理由，否则几处命名要保持一致。本节先说明顶层 action 参数和 handles；分组字段见第 6 节。

### 3.1 Docstring `Args:`

顶层 action 参数的展示名和用户说明写在 `Args:` 中：

```python
def submit_experiment(
    self,
    inputs: SubmitExperimentInputs,
    options: Optional[SubmitExperimentOptions] = None,
    sample_file_path: str = "",
) -> Dict[str, Any]:
    """提交实验。

    Args:
        inputs[实验输入]: 创建实验所需的主要参数。
        options[实验选项]: 设置实验命名、同步和报告等可选行为。
        sample_file_path[样品文件路径]: 通常由上游上传节点传入的样品文件路径。
    """
```

方括号前写真实 Python 参数名，方括号内写 UI 展示名。说明文本解释用户行为和业务含义，不写内部 HTTP route 或 payload 细节。

### 3.2 Handles

handle 的 `label` 用来命名图里的端口：

```python
ActionInputHandle(
    key="sample_file_path",
    data_type="sample_file_path",
    label="样品文件路径",
    data_key="sample_file_path",
    data_source=DataSource.HANDLE,
    io_type="source",
)
```

`key` 和 `data_key` 尽量与实际输入字段或返回字段一致。`label` 负责展示名。只有图端口需要比表单字段更短时，才有意让 label 和表单展示名不同。

---

## 4. 默认值与必填语义

必填有多层含义：

- Python 签名：没有默认值通常表示运行时调用必须提供该参数。
- `goal_default`：UI/工作流 payload 默认值；不会替代 Python 运行时默认值。
- `TypedDict`：`total=True` 时组内字段必填；`total=False` 时组内字段可选。
- `Field(default=...)`：变量分组字段的 schema/UI 默认值。
- 项目展示约定：仓库可能另有视觉必填标记。

函数签名中的可变对象或昂贵默认值用 `None`，在函数体内归一化：

```python
@action(goal_default={"order_ids": [], "timeout_seconds": 3600})
def wait_for_orders(
    self,
    order_ids: Optional[list[str]] = None,
    timeout_seconds: int = 3600,
) -> Dict[str, Any]:
    order_ids = list(order_ids or [])
```

不要依赖 `goal_default` 给 Python 运行时补齐必需参数。

---

## 5. 变量排序

在兼容性允许时，函数签名、`goal_default`、handles、变量分组字段、docstring 和测试中使用同一顺序。

推荐排序：

1. 核心业务输入，机器必填字段先于可选字段。
2. 可选业务输入。
3. 由 handles 或运行上下文传入的上游/运行时值。
4. timeout、retry、poll interval、assignee 等运行控制。
5. 高级、调试、兼容性开关。

同一组内按工作流重要性排序，不按字母排序。让人识别本次操作的字段放前面，timeout/retry/log 开关靠后。

如果旧方法签名必须保持兼容，可以保留运行时签名，但仍把 docstring、handles 和 UI 字段排成对用户最友好的顺序。

---

## 6. 变量分组

当节点输入很多，或字段面向不同使用者时，用分组降低 UI 和 schema 复杂度。

推荐分组：

- **核心业务输入**：操作员或流程作者必须理解的值。
- **可选业务输入**：名称、备注、方法选择、改变业务行为的开关。
- **上游/运行时输入**：通常由 handle 传入的 ID、文件路径、缓存 payload、上下文值。
- **运行控制输入**：timeout、retry、poll interval、assignee 等。
- **高级/调试输入**：dry-run、verbose logging、原始 payload override、兼容性开关。

少量关键字段可以放在 action 顶层参数。字段较多时，用 `TypedDict` 或类似结构表达变量分组。分组中的每个字段用 `Field(title=..., description=...)` 设置展示名和说明；字段名仍是机器 key，`title` 负责 UI label，`description` 说明用户需要做什么决定。

```python
from typing import Optional, TypedDict
from typing_extensions import Annotated
from pydantic import Field


class SubmitExperimentInputs(TypedDict):
    sample_file: Annotated[
        str,
        Field(
            title="样品文件",
            description="用于创建实验的样品文件。",
        ),
    ]
    method_name: Annotated[
        str,
        Field(
            title="实验方法",
            description="本次实验使用的方法或工作流模板。",
        ),
    ]


class SubmitExperimentOptions(TypedDict, total=False):
    experiment_name: Annotated[
        str,
        Field(
            title="实验名称",
            description="可选的实验展示名称。",
        ),
    ]
    sync_materials_after_submit: Annotated[
        bool,
        Field(
            default=True,
            title="提交后同步物料",
            description="实验创建后是否刷新本地资源树。",
        ),
    ]
```

内部 ID、缓存 payload、调试字段不要混进人填写的核心业务分组，除非操作员确实需要查看或填写它们。

---

## 7. Handles（连线端口）与返回数据

handles 是工作流连线端口，不自动等同于必填。

建议：

- 上游节点提供的值用 `ActionInputHandle`。
- 本节点返回给下游的值用 `ActionOutputHandle`。
- `data_type` 保持稳定，方便上下游匹配。
- `data_key` 指向真实输入 key 或返回 key。
- 下游依赖 output handle 时，返回对象结构要稳定。
- action 可能局部失败但继续时，返回稳定形状的 `warnings`、`errors` 或逐项结果数组。

示例：

```python
@action(
    handles=[
        ActionOutputHandle(
            key="experiment_id",
            data_type="experiment_id",
            label="实验 ID",
            data_key="experiment_id",
            data_source=DataSource.EXECUTOR,
        ),
    ],
)
def create_experiment(self, inputs: SubmitExperimentInputs) -> Dict[str, Any]:
    return {
        "success": True,
        "experiment_id": "...",
        "warnings": [],
    }
```

---

## 8. 人工确认（manual-confirm）节点

人工门禁、审批、复核、异常选择使用 `NodeType.MANUAL_CONFIRM`。

通用规则：

- 硬件动作和不可逆状态变更尽量放在普通 action 中。
- manual-confirm 节点负责展示、审批和收集操作员决策。
- handle 传入的值默认视为复核/展示数据，除非当前 UI 明确支持编辑。
- 操作员决策放在 goal params 中，并给保守默认值。
- 按当前 renderer 要求设置 `placeholder_keys`。
- 被拒绝或超时后需要停止 workflow 时，抛异常；不要只返回 `success=False`。

---

## 9. `always_free`

`always_free=True` 会绕过普通忙碌队列，要有明确理由。

通常适合：

- manual-confirm 节点；
- 轻量状态查询、缓存查询；
- 资源发布或 metadata 刷新；
- 等待/轮询外部调度系统，且不应占住本地设备锁的动作；
- 只把任务送入远端调度系统并快速返回的 proxy 动作。

通常避免：

- 本地硬件运动；
- 必须和其他 action 串行保护的物理状态变更；
- 共享非线程安全 client 或资源且没有锁的动作。

---

## 10. 日志与状态写法

做 live workflow 调试前先设计日志。日志要能解释 action 做了什么、调了哪个外部系统、给下游留下了哪些关键结果。

### 10.1 日志入口

普通日志入口优先沿用当前仓库写法。Uni-Lab-OS 代码里常见两类：

```python
from unilabos.utils.log import logger
```

或者在 driver 已保存 ROS 节点时用 `self._ros_node.lab_logger()`，它会给日志加设备 namespace。

`configure_logger()` 只配置 console/file handler。真正产生日志的是代码执行到 `logger.info(...)`、`logger.debug(...)`、`logger.warning(...)`、`logger.error(...)`，或对应的 `self._ros_node.lab_logger().info(...)` 调用。只 import logger 不会产生日志。

Uni-Lab-OS 框架会自动记录一部分运行日志，例如 action 收到原始 goal、action 函数抛异常时的 traceback、HostNode 收到 job result 后的 success/failed 状态。业务步骤、外部 API 返回摘要、关键 ID、可恢复降级等信息不会自动出现，需要 action 或 helper 代码在对应位置显式调用 logger。

不要为每个 action 机械包一层 `try/except` 只为了打 traceback；`@action` 外层执行器已经会捕获异常、记录错误，并把 action 结果标成失败。只有需要补充业务上下文时才捕获，例如 endpoint、order ID、material ID，然后继续抛异常或返回明确的非阻塞状态。

### 10.2 记录内容

在 action 边界记录：

- action 名称；
- correlation ID，或 workflow/job/order ID；
- 归一化后的输入，注意脱敏；
- 外部 endpoint/operation（如果有）；
- 开始/结束时间和耗时；
- 外部调用的状态码、业务 code 或错误摘要；
- 下游 handles 会用到的关键返回 ID、数量、文件名或状态。

大 payload 只记录脱敏摘要和原始调试文件路径，不要把 secrets、API key、完整凭证或大型私密文件写入普通日志。

### 10.3 等级约定

按 workflow 影响选择日志等级和返回形状：

- `info`：action 开始/完成、关键 ID、计数、endpoint/operation。
- `debug`/`trace`：归一化参数、脱敏 payload 摘要、原始调试文件路径；大对象不要直接打到普通日志。
- `warning`：可恢复或非阻塞降级；同时在返回值里写 `warnings` 或明确 status。
- `error`：捕获到并处理的业务/API 失败。日志本身不会改变 action 状态；如果当前 action 必须失败，抛异常或让异常继续冒出。如果 workflow 可以继续，返回结构化结果，不要只靠 `logger.error()` 表达失败。

---

## 11. 阻塞错误与非阻塞错误

写 action 前先明确失败语义。

### 11.1 阻塞错误

下游继续执行会不安全或没有意义时，使用阻塞错误：

- 缺少必需输入；
- 硬件状态未知；
- 物料身份无法解析；
- 外部 API 拒绝了核心操作；
- manual-confirm 被拒绝或超时；
- 下游 handles 会收到误导性的 ID 或资源状态。

阻塞错误应抛异常，并提供足够上下文。除非已经测试过当前 executor 行为，否则不要依赖 `{"success": False}` 来停止 workflow；对 `@action` 来说，正常 return 一个包含 `success=False` 的 dict 仍可能只是一次正常返回。

### 11.2 非阻塞错误

主流程可以继续时，使用非阻塞错误：

- 可选报告文件暂不可用；
- best-effort sync 失败，但核心 action 已成功；
- batch 中某一项失败，其余项成功；
- cache refresh 失败，但旧状态仍可用；
- 外部系统给出 warning，但不应停止执行。

非阻塞错误返回稳定的成功对象，并放入 `warnings`、`errors` 或逐项状态。返回数据要清楚说明发生了什么、下游还能信任哪些字段。

```python
return {
    "success": True,
    "experiment_id": experiment_id,
    "warnings": ["报告压缩包重试后仍未就绪。"],
    "attachments": attached_files,
}
```

如果 workflow 需要按失败分支，返回明确的 status 字段供下游判断。

---

## 12. 工作流执行状态

开发 action 时把三件事分开验证：

- Python 返回值：给 handles 和下游节点使用的数据。
- action/job 状态：平台记录这次节点执行是否成功。
- workflow 是否继续：图配置、executor 语义或错误处理节点决定后续节点是否运行。

不要假设 action 返回 `success=False` 一定会停止整个 workflow。在某些 Uni-Lab-OS 流程中，节点 execution status 可能显示为 `false`，但下游 workflow 仍继续执行。

按这个现实设计：

- 必须停止 workflow 的失败要 raise。
- 可分支的结果返回明确 status 字段。
- 只有下游可安全使用时，才保持 output handles 稳定输出。
- 非阻塞失败不要只返回 `success=False`，可使用类似 `"completed_with_warnings"` 的清楚状态。
- 可由操作员处理的阻塞失败，进入 manual-confirm 或 error-handling 节点。
- 如果继续/停止行为与安全相关，要给 executor 行为补测试。

把已有 action 从“返回 false”改成“raise”之前，先检查下游 workflow 是否有意在 false 节点状态后继续。

---

## 13. 验证清单

提交前检查：

- 暴露方法直接在被装饰类上可见，或有薄 wrapper。
- 顶层输入已按使用者和行为分组。
- 函数签名、`goal_default`、handles、分组字段、docstring 和测试使用一致 key。
- 变量顺序对用户友好，并在 metadata 表面保持一致。
- 展示名通过 `Args:`、`Field(title=...)` 和 handle `label` 设置。
- 描述说明用户/业务含义，不写内部 route。
- Python 默认值、`goal_default`、分组字段默认值有意对齐。
- handles 指向真实输入/返回 key。
- manual-confirm 的拒绝/超时阻塞门禁会 raise。
- 日志记录 action 开始/结束、外部调用结果、下游关键 ID，且已脱敏。
- 阻塞错误 raise；非阻塞失败返回结构化 warnings/status。
- 测试或 check-mode 覆盖 action metadata 和工作流关键状态语义。
