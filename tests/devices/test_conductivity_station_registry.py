from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
DEVICE_DIR = (
    ROOT
    / "unilabos"
    / "devices"
    / "workstation"
    / "conductivity_station"
)


def test_conductivity_station_actions_are_discoverable_by_registry() -> None:
    tree = ast.parse(
        (DEVICE_DIR / "conductivity_station.py").read_text(encoding="utf-8")
    )
    device_class = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "ConductivityStation"
    )
    device_decorator = next(
        decorator
        for decorator in device_class.decorator_list
        if isinstance(decorator, ast.Call)
        and isinstance(decorator.func, ast.Name)
        and decorator.func.id == "device"
    )
    device_id = next(
        keyword.value.value
        for keyword in device_decorator.keywords
        if keyword.arg == "id" and isinstance(keyword.value, ast.Constant)
    )
    actions = {
        method.name
        for method in device_class.body
        if isinstance(method, ast.FunctionDef)
        and any(
            (isinstance(decorator, ast.Name) and decorator.id == "action")
            or (
                isinstance(decorator, ast.Call)
                and isinstance(decorator.func, ast.Name)
                and decorator.func.id == "action"
            )
            for decorator in method.decorator_list
        )
    }
    init_method = next(
        method
        for method in device_class.body
        if isinstance(method, ast.FunctionDef) and method.name == "__init__"
    )

    assert device_id == "conductivity_station"
    assert actions == {
        "station_status",
        "material_status",
        "batch_status",
        "batch_result",
        "start_batch",
        "stop_current_batch",
        "manual_run",
        "clear_current_batch",
    }
    assert {arg.arg for arg in init_method.args.args if arg.arg != "self"} == {
        "ip",
        "port",
        "connect_timeout",
        "response_timeout",
        "max_message_bytes",
        "encoding",
        "frame_delimiter",
        "station_action_names",
    }
