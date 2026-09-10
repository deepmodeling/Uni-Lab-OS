"""ROS 动作传输适配器（ROS Action Transport Adapter）的运行时合同测试。"""

from copy import deepcopy

from unilabos.ros.action_transport import (
    build_runtime_action_mappings,
    required_action_endpoint_ids,
    wait_for_action_endpoints,
)
from unilabos_msgs.action import SetPumpPosition, StrSingleInput


def test_json_commands_derive_internal_endpoints_without_mutating_public_projection() -> None:
    """公开动作投影保持纯净，运行时副本按同步性补齐两个内部 ROS 端点。

    参数：无；测试构造同步、异步和原生三种动作映射。
    返回：无；断言运行时映射包含所需内部端点且原公开投影未被修改。
    异常：任何投影污染、端点遗漏或类型错误都会触发断言失败。
    """

    # ``public_mappings`` 是包目录（Package Catalog）向查询侧发布的公开动作投影。
    public_mappings = {
        "scan": {"type": "UniLabJsonCommand", "schema": {"type": "object"}},
        "wait": {
            "type": "UniLabJsonCommandAsync",
            "schema": {"type": "object"},
        },
        "set_position": {"type": SetPumpPosition},
    }
    # ``before`` 保存公开投影的调用前快照，用于证明适配器没有反向污染它。
    before = deepcopy(public_mappings)

    runtime_mappings = build_runtime_action_mappings(public_mappings)

    assert public_mappings == before
    assert runtime_mappings["_execute_driver_command"]["type"] is StrSingleInput
    assert (
        runtime_mappings["_execute_driver_command_async"]["type"]
        is StrSingleInput
    )
    assert runtime_mappings["scan"] == public_mappings["scan"]
    assert runtime_mappings["wait"] == public_mappings["wait"]


def test_required_endpoints_collapse_json_commands_and_keep_native_actions() -> None:
    """业务动作映射被折叠成实际必须就绪的稳定 ROS 端点集合。

    参数：无；测试构造包含重复同步 JSON 命令和原生动作的运行时映射。
    返回：无；断言同步 JSON 命令只要求一个通用端点，原生动作保留独立端点。
    异常：端点遗漏、重复或顺序漂移都会触发断言失败。
    """

    # ``runtime_mappings`` 是设备包装器实际持有的运行时动作映射。
    runtime_mappings = build_runtime_action_mappings(
        {
            "scan": {"type": "UniLabJsonCommand"},
            "reset": {"type": "UniLabJsonCommand"},
            "set_position": {"type": SetPumpPosition},
        }
    )

    assert required_action_endpoint_ids("pump-a", runtime_mappings) == (
        "/devices/pump-a/_execute_driver_command",
        "/devices/pump-a/set_position",
    )


def test_endpoint_readiness_requires_every_required_client() -> None:
    """本地设备仅在每个必需动作客户端都连接服务端后才允许宣告在线。

    参数：无；测试构造一个已就绪客户端和一个未就绪客户端。
    返回：无；断言部分就绪仍返回失败，全部就绪才返回成功。
    异常：门禁采用“任一就绪”或吞掉缺失端点时触发断言失败。
    """

    class Client:
        """提供可切换就绪结果的最小测试客户端。"""

        def __init__(self, ready: bool) -> None:
            """保存预期就绪状态。

            参数：``ready`` 表示测试端点是否已被 ROS 图发现。
            返回：无。
            异常：无。
            """

            self.ready = ready

        def server_is_ready(self) -> bool:
            """返回端点就绪状态。

            参数：无。
            返回：构造时提供的布尔值。
            异常：无。
            """

            return self.ready

    # ``required_ids`` 是本地设备宣告可调度前必须全部连接的端点。
    required_ids = (
        "/devices/pump-a/_execute_driver_command",
        "/devices/pump-a/set_position",
    )
    # ``clients`` 模拟 HostNode 已创建的 ROS 动作客户端集合。
    clients = {
        required_ids[0]: Client(True),
        required_ids[1]: Client(False),
    }

    assert wait_for_action_endpoints(clients, required_ids, wait_timeout=0.0) is False
    clients[required_ids[1]].ready = True
    assert wait_for_action_endpoints(clients, required_ids, wait_timeout=0.0) is True
