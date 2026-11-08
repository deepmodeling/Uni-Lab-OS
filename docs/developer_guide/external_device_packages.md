# 外部设备包开发

公开版 v1.0 不在发行包中携带仓库内的 `unilabos.devices` 驱动。设备集成应放在独立 Python 包中；设备代码、依赖和许可由包作者维护。

## 创建 mock 包

先按[安装指南](../user_guide/installation.md)安装 Uni-Lab-OS 源码，并为完整本地运行环境准备 Python 与 ROS 2 依赖。包检查和 mock 驱动测试不连接真实设备；本地工作流运行仍需要平台使用的 ROS 2 运行依赖，但无需真实设备或设备凭据。

```bash
unilab workspace init --output ./device-echo-demo --name device_echo_demo
cd device-echo-demo
python -m pip install -e ".[dev]"
python -m pytest -q
unilab package inspect --path . --out dist/inspect
```

`workspace init` 会创建可独立维护的包骨架，包括 `pyproject.toml`、`package.yaml`、设备定义、工作流源码、dry-run 设备图和合同测试。`package inspect` 编译设备与工作流定义并生成目录摘要，不会导入或执行设备作者代码。

目录结构如下：

```text
device-echo-demo/
├── pyproject.toml
├── package.yaml
├── deployment/graphs/dry-run.json
├── device_echo_demo/
│   ├── devices/demo_device.py
│   └── workflows/demo_workflow.py
└── tests/test_workspace_contract.py
```

设备使用公开的 `@device` 与 `@action` 装饰器。示例设备没有硬件副作用，输入文字会原样返回：

```python
from typing import TypedDict

from unilabos.registry.decorators import action, device


class EchoResult(TypedDict):
    success: bool
    echoed_text: str


@device(
    id="demo_device",
    category=["example"],
    displayname="示例设备",
    description="无硬件副作用的回显 mock。",
    metadata={"hardware_side_effects": False},
)
class DemoDevice:
    @action(displayname="回显文本", description="原样返回输入文本。")
    def echo(self, text: str) -> EchoResult:
        return {"success": True, "echoed_text": text}
```

工作流源码由静态编译器读取，不要直接运行 `demo_workflow.py`：

```python
from typing import TypedDict

from device_echo_demo.devices.demo_device import DemoDevice
from unilabos.workflow.authoring import device, workflow


class DemoWorkflowResult(TypedDict):
    success: bool
    echoed_text: str


demo_device: DemoDevice = device("demo_device_01")


@workflow(
    workflow_uuid="a99d1ff7-2016-5ab8-8721-d5fb98731176",
    displayname="示例设备回显",
    description="调用 mock 设备并返回结果。",
)
def run_demo(*, text: str = "Hello Uni-Lab OS") -> DemoWorkflowResult:
    # unilab:node_uuid=b6b5531e-232e-579d-936f-a6b54f8ba95a
    echoed = demo_device.echo(text=text)
    return {"success": echoed.success, "echoed_text": echoed.echoed_text}
```

`package.yaml` 把工作流 UUID 与源码路径登记在包清单中。上面的 UUID 是 `device-echo-demo` 模板生成值；另建包时保留 `workspace init` 生成的 UUID，新增节点也使用稳定的 UUID 字面量。设备和工作流的额外依赖声明在 `pyproject.toml`。不要把访问密钥、连接口令、真实硬件地址或未脱敏协议资料放入包中。

## 运行首个 Task

工作区启动会扫描并装载生成的 mock 工作流。若已按安装指南安装完整 Python requirements，生成的 `ensure_dependencies = true` 可保留；只想在受控环境启动 mock 时，可在 `pyproject.toml` 的 `[tool.unilabos.startup]` 中将它设为 `false`，避免启动时自动安装包。ROS 2 运行依赖仍必须预先安装；Mamba/Conda 只用于安装平台所需的 ROS 2 组件和匹配的 `unilabos_msgs`。

```bash
unilab workspace start --workspace . --runtime-mode normal --wait 300
unilab workspace status --workspace . --json
unilab workflow list --workspace . --json
```

从本地工作流页面发布“示例设备回显”后，复制工作流 UUID，并运行：

```bash
unilab workflow run <WORKFLOW-UUID> --workspace . --inputs '{"text":"Hello Uni-Lab OS"}' --follow
```

Task 应成功完成，输出中的 `echoed_text` 应为 `Hello Uni-Lab OS`。此示例不连接真实硬件。结束后，在没有运行中 Task 时停止工作区：

```bash
unilab workspace stop --workspace .
```

要把设备包作为其他工作区的显式依赖，可在主工作区添加本地包路径：

```bash
unilab package add ../device-echo-demo --workspace ../my-lab
```

## 兼容性与示例

独立设备包使用自己的许可。不要把设备驱动复制到 Uni-Lab-OS 源码树中。

[LabDeviceLanDemo](https://github.com/Xuwznln/LabDeviceLanDemo) 与 [LabDeviceWorkstationDemo](https://github.com/Xuwznln/LabDeviceWorkstationDemo) 面向较新的 `dev` 分支接口，不是 v1.0 可运行示例：它们使用 v1.0 未提供的设备装饰器参数或工作流导入路径；工作站示例还导入了发行包不包含的 `unilabos.devices.workstation`，并使用 v1.0 CLI 不接受的 backend 名称。请仅在对应的新开发分支上使用这些示例。
