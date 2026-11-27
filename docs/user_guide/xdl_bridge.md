# XDL Bridge

`unilabos.xdl_bridge` 是可选模块，用于把 AI 或用户生成的 XDL 转为标准
Uni-Lab workflow。它不会改变已有 workflow、设备调度或驱动行为。

## 使用流程

1. 用户与 Agent 确认要使用的实验室设备。
2. Agent 生成 XDL，并选择该工站对应的 bridge profile。
3. Agent 调用 `build_xdl_workflow()` 或 `upload_xdl_workflow()`。
4. workflow 上传到玻尔跃迁后，用户在 Workflow 页面检查并手动启动。

启动 Uni-Lab edge 仍使用原有命令。例如 comprehensive 预设工站：

```bash
unilab -g unilabos/test/experiments/comprehensive_protocol/comprehensive_station.json \
  --upload_registry \
  --addr https://leap-lab.bohrium.com/api/v1 \
  --disable_browser
```

AK/SK 应通过命令行、环境变量或会话注入，不能写入 XDL、profile 或日志。

## Python API

```python
from unilabos.xdl_bridge import build_xdl_workflow, upload_xdl_workflow

profile = "unilabos/test/experiments/comprehensive_protocol/xdl_bridge.yaml"
workflow = build_xdl_workflow("experiment.xdl", profile)
result = upload_xdl_workflow("experiment.xdl", profile, tags=["xdl"])
```

## Profile

Profile 只绑定目标工站：

- 设备图与 registry；
- 工作站 ID；
- XDL 硬件角色到工站资源 ID 的映射；
- `virtual` 或 `real` 运行模式。

XDL 操作到 `TransferProtocol`、`HeatChillProtocol` 等 Uni-Lab Protocol 的映射是
模块共享合同，不随工站复制。若工站缺少某个 Protocol、handle 或资源绑定，bridge 在
上传前报错。
