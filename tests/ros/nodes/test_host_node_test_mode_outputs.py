"""Host 模拟动作结果的合同化回归。"""

from __future__ import annotations

from unilabos.ros.nodes.presets.host_node import HostNode


def test_json_command_mock_preserves_material_passthrough_outputs() -> None:
    """JSON 命令模拟结果按输出合同透传同名物料并填充标量。

    参数：无。返回：无；断言模拟动作不会丢失后续调度所需的物料 UUID。异常：
    合同投影或结果生成回归时由 pytest 报告。
    """

    host = object.__new__(HostNode)
    host._action_value_mappings = {
        "robot": {
            "pick": {
                "schema": {
                    "properties": {
                        "result": {
                            "type": "object",
                            "properties": {
                                "command_id": {"type": "string"},
                                "state": {
                                    "type": "string",
                                    "enum": ["RUNNING", "SUCCEEDED", "FAILED"],
                                },
                                "success": {"type": "boolean"},
                                "resource": {
                                    "type": "object",
                                    "properties": {"uuid": {"type": "string"}},
                                    "required": ["uuid"],
                                },
                            },
                        },
                    }
                }
            }
        }
    }

    result = host._build_simulated_action_return(
        "robot",
        "pick",
        {"resource": {"uuid": "material-1"}, "site": "A1"},
    )

    assert result == {
        "action_mode": "simulate",
        "action_name": "pick",
        "command_id": "",
        "state": "SUCCEEDED",
        "success": True,
        "resource": {"uuid": "material-1"},
    }


def test_handle_mock_uses_data_key_when_handler_key_is_empty() -> None:
    """旧连接点映射缺少处理键时仍以数据键保留同名物料。

    参数：无。返回：无；断言旧动作映射兼容路径。异常：断言失败时由 pytest
    报告。
    """

    host = object.__new__(HostNode)
    host._action_value_mappings = {
        "robot": {
            "place": {
                "handles": {
                    "output": [
                        {
                            "data_key": "resource",
                            "handler_key": "",
                        }
                    ]
                }
            }
        }
    }

    result = host._build_simulated_action_return(
        "robot",
        "place",
        {"resource": {"uuid": "material-2"}},
    )

    assert result["resource"] == {"uuid": "material-2"}
