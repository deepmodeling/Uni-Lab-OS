# 工作站高级模式参考

本文件是 SKILL.md 的补充，包含外部系统集成、物料同步、配置结构等高级模式。
Agent 在需要实现这些功能时按需阅读。

---

## 1. 外部系统工作站通用守则

外部系统工作站的难点通常不在类模板，而在事实来源、现场 API、注册表可见性和物料同步边界。开始实现前先按下面规则定边界。

### 1.1 先确定事实来源

建议按优先级取证：

1. 当前仓库源码与当前运行环境中的 Uni-Lab-OS 源码。
2. 当前项目的后端 OpenAPI/schema、供应商 API 文档、示例配置。
3. 当前部署的只读现场 API 结果，例如设备列表、仓库/库位、物料列表、订单报告。
4. 其他工作站源码、旧文档、历史日志、HAR/浏览器观察，只作为模式参考。

前端 route 和后端 route 可能完全不同。运行时代码使用后端 API，不要把前端/HAR route 当成工作站 API。

### 1.2 区分基类能力和工站差异

先查当前基类已经提供什么，再决定 thin wrapper、override 或重写。

通常由 Uni-Lab-OS 工作站框架提供：

- `deck` 反序列化并传入 `__init__(..., deck=None, **kwargs)`。
- `post_init(ros_node)` 阶段拿到 ROS 节点和子设备。
- `self._ros_node.sub_devices` / `self._children` 等子设备访问入口（以当前源码为准）。
- `ResourceSynchronizer` 这样的同步抽象和 `update_resource` 上传资源树机制。
- `WorkstationHTTPService` 这类回调分发框架（若当前版本提供）。

通常必须按每个工站适配：

- API endpoint、认证方式、请求/响应 envelope、状态码含义。
- workflow/order 创建参数、用户可读编号与内部 ID 的关系、报告轮询逻辑。
- 设备 operation 名称、下拉枚举、范围参数、错误恢复选项。
- 物料类型、仓库/库位映射、坐标方向、空位和虚拟暂存策略。
- 人工确认节点的展示表、审批默认值、assignee、失败语义。
- 资源树粒度：是否真的需要 wells/tips/子孔，还是只保存外部明细元数据。

不要假设参考工作站里的方法都是基类能力。继承方法或 helper 行为若要暴露给流程图，通常需要在当前 `@device` 类上写薄 wrapper，让 AST 扫描和前端注册表都能看到。

### 1.3 注册表、动作 schema 与人工确认

- 工作流动作必须在被装饰的 `@device` 类上 AST/import 可见。
- 资源/Deck 类也要 import-visible；图文件的 `_resource_type` 应指向可导入的 decorated 类。class 形式资源必须在运行时被 import，不能只依赖 AST 扫描。
- `Args:` docstring、参数名、`Field(title/description)`、handle key、默认值和 required 语义要一致。显示名和必填标记等 UI 约定以当前仓库文档为准。
- `NodeType.MANUAL_CONFIRM` 用于人工门禁。handle 传入值应视为只读展示/依赖数据；审批结果用 goal params 承载并给安全默认值。确认失败时抛异常，避免仅返回 `{"success": False}` 后被当成普通动作完成。

### 1.4 现场 API 测试不可省

供应商系统可能出现“HTTP 200 但业务失败”或“同一设备数但 operation 为空”。写 wrapper 时要解析业务 envelope（如 `code`/`message`/`data`），并在错误中带上 endpoint、设备、operation 和原始关键信息。

快照只能帮助设计 wrapper 和 schema；执行前仍要用当前部署的现场 API 验证。写接口需要真实测试环境和明确批准；只读探测也要记录 host、时间、返回 envelope 和样本 ID。

### 1.5 物料与资源同步先做往返测试

PLR 资源类至少要通过两种构造路径：

- 注册/同步时的 `cls(name=...)` 或工厂式创建。
- 反序列化时的 `cls(**serialized_dict)`。

优先使用容错构造器、`**kwargs`、`kwargs.setdefault(...)`，并为 itemized 资源提供 `ordered_items` 或 `ordering`。如果 Deck 已带有反序列化出的 `children`，不要再次初始化默认子物料，避免重复或陈旧资源。

需要默认 children/layout 的资源可实现 `setup()`，工作站 Deck 自动放置 WareHouse 是典型例子。`setup` 必须默认为 `False`，只有首次创建默认布局时显式打开；若图文件传入 `config.setup=true`，也只是构造参数，只有对应资源类的 `__init__` 显式处理时才会生效。默认保持 `False` 可避免反序列化已有 `children` 时重复创建或覆盖持久化状态。

---

## 2. 外部系统集成模式

### 2.1 RPC 客户端

与外部 LIMS/MES 系统通信的标准模式。继承 `BaseRequest`，所有接口统一用 POST。

```python
from unilabos.device_comms.rpc import BaseRequest


class MySystemRPC(BaseRequest):
    """外部系统 RPC 客户端"""

    def __init__(self, host: str, api_key: str):
        super().__init__(host)
        self.api_key = api_key

    def _request(self, endpoint: str, data: dict = None) -> dict:
        return self.post(
            url=f"{self.host}/api/{endpoint}",
            params={
                "apiKey": self.api_key,
                "requestTime": self.get_current_time_iso8601(),
                "data": data or {},
            },
        )

    def query_status(self) -> dict:
        return self._request("status/query")

    def create_order(self, order_data: dict) -> dict:
        return self._request("order/create", order_data)
```

参考：`unilabos/devices/workstation/bioyond_studio/bioyond_rpc.py`（`BioyondV1RPC`）

### 2.2 HTTP 回调服务

接收外部系统报送的标准模式。使用 `WorkstationHTTPService`，在 `post_init` 中启动。

```python
from unilabos.devices.workstation.workstation_http_service import WorkstationHTTPService


class MyWorkstation(WorkstationBase):
    def __init__(self, config=None, deck=None, **kwargs):
        super().__init__(deck=deck, **kwargs)
        self.config = config or {}
        http_cfg = self.config.get("http_service_config", {})
        self._http_service_config = {
            "host": http_cfg.get("http_service_host", "127.0.0.1"),
            "port": http_cfg.get("http_service_port", 8080),
        }
        self.http_service = None

    def post_init(self, ros_node):
        super().post_init(ros_node)
        self.http_service = WorkstationHTTPService(
            workstation_instance=self,
            host=self._http_service_config["host"],
            port=self._http_service_config["port"],
        )
        self.http_service.start()
```

**HTTP 服务路由**（固定端点，由 `WorkstationHTTPHandler` 自动分发）：

| 端点 | 调用的工作站方法 |
|------|-----------------|
| `/report/step_finish` | `process_step_finish_report(report_request)` |
| `/report/sample_finish` | `process_sample_finish_report(report_request)` |
| `/report/order_finish` | `process_order_finish_report(report_request, used_materials)` |
| `/report/material_change` | `process_material_change_report(report_data)` |
| `/report/error_handling` | `handle_external_error(error_data)` |

实现对应方法即可接收回调：

```python
def process_step_finish_report(self, report_request) -> Dict[str, Any]:
    """处理步骤完成报告"""
    step_name = report_request.data.get("stepName")
    return {"success": True, "message": f"步骤 {step_name} 已处理"}

def process_order_finish_report(self, report_request, used_materials) -> Dict[str, Any]:
    """处理订单完成报告"""
    order_code = report_request.data.get("orderCode")
    return {"success": True}
```

参考：`unilabos/devices/workstation/workstation_http_service.py`

### 2.3 连接监控

独立线程周期性检测外部系统连接状态，状态变化时发布 ROS 事件。

```python
class ConnectionMonitor:
    def __init__(self, workstation, check_interval=30):
        self.workstation = workstation
        self.check_interval = check_interval
        self._running = False
        self._thread = None

    def start(self):
        self._running = True
        self._thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self._thread.start()

    def _monitor_loop(self):
        while self._running:
            try:
                # 调用外部系统接口检测连接
                self.workstation.hardware_interface.ping()
                status = "online"
            except Exception:
                status = "offline"
            time.sleep(self.check_interval)
```

参考：`unilabos/devices/workstation/bioyond_studio/station.py`（`ConnectionMonitor`）

---

## 3. Config 结构模式

工作站的 `config` 在图文件中定义，传入 `__init__`。以下是常见字段模式：

### 3.1 外部系统连接

```json
{
    "api_host": "http://192.168.1.100:8080",
    "api_key": "YOUR_API_KEY"
}
```

### 3.2 HTTP 回调服务

```json
{
    "http_service_config": {
        "http_service_host": "127.0.0.1",
        "http_service_port": 8080
    }
}
```

### 3.3 物料类型映射

将 PLR 资源类名映射到外部系统的物料类型（名称 + UUID）。用于双向物料转换。

```json
{
    "material_type_mappings": {
        "PLR_ResourceClassName": ["外部系统显示名", "external-type-uuid"],
        "BIOYOND_PolymerStation_Reactor": ["反应器", "3a14233b-902d-0d7b-..."]
    }
}
```

### 3.4 仓库映射

将仓库名映射到外部系统的仓库 UUID 和库位 UUID。用于入库/出库操作。

```json
{
    "warehouse_mapping": {
        "仓库名": {
            "uuid": "warehouse-uuid",
            "site_uuids": {
                "A01": "site-uuid-A01",
                "A02": "site-uuid-A02"
            }
        }
    }
}
```

### 3.5 工作流映射

将内部工作流名映射到外部系统的工作流 ID。

```json
{
    "workflow_mappings": {
        "internal_workflow_name": "external-workflow-uuid"
    }
}
```

### 3.6 物料默认参数

```json
{
    "material_default_parameters": {
        "NMP": {
            "unit": "毫升",
            "density": "1.03",
            "densityUnit": "g/mL",
            "description": "N-甲基吡咯烷酮"
        }
    }
}
```

---

## 4. 资源同步机制

### 4.1 ResourceSynchronizer

抽象基类，用于与外部物料系统双向同步。定义在 `workstation_base.py`。

```python
from unilabos.devices.workstation.workstation_base import ResourceSynchronizer


class MyResourceSynchronizer(ResourceSynchronizer):
    def __init__(self, workstation, api_client):
        super().__init__(workstation)
        self.api_client = api_client

    def sync_from_external(self) -> bool:
        """从外部系统拉取物料到 deck"""
        external_materials = self.api_client.list_materials()
        for material in external_materials:
            plr_resource = self._convert_to_plr(material)
            self.workstation.deck.assign_child_resource(plr_resource, coordinate)
        return True

    def sync_to_external(self, plr_resource) -> bool:
        """将 deck 中的物料变更推送到外部系统"""
        external_data = self._convert_from_plr(plr_resource)
        self.api_client.update_material(external_data)
        return True

    def handle_external_change(self, change_info) -> bool:
        """处理外部系统推送的物料变更"""
        return True
```

### 4.2 update_resource — 上传资源树到云端

将 PLR Deck 序列化后通过 ROS 服务上传。典型使用场景：

```python
# 在 post_init 中上传初始 deck
from unilabos.ros.nodes.base_device_node import ROS2DeviceNode

ROS2DeviceNode.run_async_func(
    self._ros_node.update_resource, True,
    **{"resources": [self.deck]}
)

# 在动作方法中更新特定资源
ROS2DeviceNode.run_async_func(
    self._ros_node.update_resource, True,
    **{"resources": [updated_plate]}
)
```

---

## 5. 工作流序列管理

工作站通过 `workflow_sequence` 属性管理任务队列（JSON 字符串形式）。

```python
class MyWorkstation(WorkstationBase):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._workflow_sequence = []

    @property
    def workflow_sequence(self) -> str:
        """返回 JSON 字符串，ROS 自动发布"""
        import json
        return json.dumps(self._workflow_sequence)

    async def append_to_workflow_sequence(self, workflow_name: str) -> Dict[str, Any]:
        """添加工作流到队列"""
        self._workflow_sequence.append({
            "name": workflow_name,
            "status": "pending",
            "created_at": time.time(),
        })
        return {"success": True}

    async def clear_workflows(self) -> Dict[str, Any]:
        """清空工作流队列"""
        self._workflow_sequence = []
        return {"success": True}
```

---

## 6. 站间物料转移

工作站之间转移物料的模式。通过 ROS ActionClient 调用目标站的动作。

```python
async def transfer_materials_to_another_station(
    self,
    target_device_id: str,
    transfer_groups: list,
    **kwargs,
) -> Dict[str, Any]:
    """将物料转移到另一个工作站"""
    target_node = self._children.get(target_device_id)
    if not target_node:
        # 通过 ROS 节点查找非子设备的目标站
        pass

    for group in transfer_groups:
        resource = self.find_resource_by_name(group["resource_name"])
        # 从本站 deck 移除
        resource.unassign()
        # 调用目标站的接收方法
        # ...

    return {"success": True, "transferred": len(transfer_groups)}
```

参考：`BioyondDispensingStation.transfer_materials_to_reaction_station`

---

## 7. post_init 完整模式

`post_init` 是工作站初始化的关键阶段，此时 ROS 节点和子设备已就绪。

```python
def post_init(self, ros_node):
    super().post_init(ros_node)

    # 1. 初始化外部系统客户端（此时 config 已可用）
    self.rpc_client = MySystemRPC(
        host=self.config.get("api_host"),
        api_key=self.config.get("api_key"),
    )
    self.hardware_interface = self.rpc_client

    # 2. 启动连接监控
    self.connection_monitor = ConnectionMonitor(self)
    self.connection_monitor.start()

    # 3. 启动 HTTP 回调服务
    if hasattr(self, '_http_service_config'):
        self.http_service = WorkstationHTTPService(
            workstation_instance=self,
            host=self._http_service_config["host"],
            port=self._http_service_config["port"],
        )
        self.http_service.start()

    # 4. 上传 deck 到云端
    ROS2DeviceNode.run_async_func(
        self._ros_node.update_resource, True,
        **{"resources": [self.deck]}
    )

    # 5. 初始化资源同步器（可选）
    self.resource_synchronizer = MyResourceSynchronizer(self, self.rpc_client)
```
