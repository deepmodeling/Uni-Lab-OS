"""原生仿真执行服务的公共装配边界、记录、结算及资源生命周期。"""

import json
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from unilabos.runtime.execution import (
    BackendExecutionClient,
    JournaledExecution,
    RuntimeMaterials,
)

from .execution import DeviceWorldExecutor, WorldClient


@dataclass
class DeviceRuntime:
    """设备包返回的世界、行为模型和原生动作绑定；观察映射可选。"""

    world: object
    model: object
    bindings: dict
    observation_values: object = None
    composition: object = None


def runtime_world(config, composition, record, *, world=None):
    """按编译装配选择统一调度或既有世界客户端，支持注入无物理世界。"""
    if composition and composition.scheduler == "coupled":
        from .scheduled_world import ScheduledWorldClient

        return ScheduledWorldClient(
            config.get("plant_socket") if world is None else None,
            config["world"],
            world=world,
            record=record,
        )
    return (
        world
        if world is not None
        else WorldClient(config["plant_socket"], config["world"])
    )


class SimulationSession:
    """持有设备模型和本地命令日志，通过后端接口授权和结算；不打开业务数据库。"""

    def __init__(self, config, build, *, execution_port=None):
        """初始化原生授权与结算；部分初始化失败时关闭已取得的本轮资源。"""
        self._resources = ExitStack()
        self.publisher = None
        try:
            if "inventory_db" in config or "workflow_db" in config:
                raise ValueError(
                    "执行端不再接受数据库路径；请配置 execution_url 并由后端提供执行接口"
                )
            if (
                not config.get("command_journal")
                or config["command_journal"] == ":memory:"
            ):
                raise ValueError(
                    "command_journal 必须配置固定的持久路径，不能随会话变化"
                )
            if execution_port is None:
                from unilabos.config.config import EdgeControlConfig

                execution_port = BackendExecutionClient(
                    config.get("execution_url", ""),
                    config.get("execution_api_key") or EdgeControlConfig.api_key,
                )
            self.execution_port = execution_port
            self.commands = JournaledExecution(
                config["command_journal"], namespace="simulation-execution"
            )
            if self.commands.journal.has_unknown():
                raise RuntimeError("execution_reconciliation_required")
            self.journal = self._resources.enter_context(
                Path(config["journal"]).open("x")
            )
            self.materials = RuntimeMaterials(execution_port)
            runtime = build(config, self.materials, self.record)
            self.world, self.model = runtime.world, runtime.model
            self.composition = runtime.composition
            self._resources.callback(self.model.close)
            if hasattr(self.world, "control"):
                self.control = self.world.control
            self.observation_values = runtime.observation_values
            self.bindings = runtime.bindings
            self.grants: dict[str, dict[str, Any]] = {}

            def authorize(request):
                grant = execution_port.call(
                    "authorize", {"request": request, "bindings": self.bindings}
                )
                previous = self.grants.setdefault(
                    request["identity"]["node_job_uuid"], grant
                )
                if previous != grant:
                    raise ValueError("execution_grant_changed")
                return grant

            self.authorize = authorize
            if config.get("observation_provider"):
                if not callable(self.observation_values):
                    raise ValueError("observation_values_required")
                from unilabos.config.config import EdgeControlConfig

                from .provider_client import ObservationPublisher

                provider = config["observation_provider"]
                self.publisher = ObservationPublisher(
                    provider["url"],
                    versions=provider["versions"],
                    operation_id=config["world"]["world_id"],
                    source_session=config["world"]["session_id"],
                    api_key=provider.get("api_key") or config.get("execution_api_key") or EdgeControlConfig.api_key,
                )
                self._resources.callback(self.publisher.close)
                self.publisher.ready()

            def settle(request, proof):
                """提交规范效果；观察来源会话不再维护动作结算状态。"""
                receipt = execution_port.call(
                    "settle",
                    {
                        "request": request,
                        "proof": proof,
                        "grant": self.grants[request["identity"]["node_job_uuid"]],
                        "bindings": self.bindings,
                    },
                )
                return receipt

            self.executor = DeviceWorldExecutor(
                self.world,
                self.model,
                settle=settle,
                authorize=authorize,
                record=self.record,
                observe=self.observe if self.publisher else None,
            )
        except BaseException:
            self._resources.close()
            raise

    def observe(self, state, request):
        """发布设备映射的物理属性，附原始采样时间和当前请求身份。"""
        assert self.publisher is not None
        self.publisher.publish(
            state["sim_time"],
            self.observation_values(state),
            sample_group=f"{self.model.world_id}:{state['tick']}",
            identity=request["identity"],
            request_id=state.get("request_id"),
        )

    def record(self, event):
        """追加并刷新标准 JSONL 事件，拒绝 NaN 等非标准数值。"""
        self.journal.write(json.dumps(event, allow_nan=False) + "\n")
        self.journal.flush()

    def poll_controls(self):
        """处理调度控制；单步反馈消费失败时同时锁定世界和执行器。"""
        if hasattr(self.world, "pump_controls"):
            self.world.pump_controls()
            if self.world.single_request is not None:
                try:
                    self.model.accept_state(self.world.scan(single=True))
                except BaseException:
                    self.world.invalidate()
                    self.executor.faulted = True
                    raise

    def execute(self, request):
        """执行原生请求；失败时断开观察端并保存额外清理错误。"""
        try:
            completed = self.commands.completed(request)
            if completed is not None:
                return completed
            grant = self.authorize(request)
            return self.commands.run(
                request, grant, lambda: self.executor.execute(request)
            )
        except Exception:
            if self.publisher:
                try:
                    self.publisher.close()
                except Exception as error:
                    self.record(
                        {
                            "event": "observation.disconnect_failed",
                            "error": f"{type(error).__name__}: {error}",
                        }
                    )
            raise

    def close(self):
        """逆序关闭所拥有资源；一个关闭失败仍会尝试其余资源，不停止外部 worker。"""
        self._resources.close()
