"""SZLab S07 固体加料工位设备驱动。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from unilabos.registry.decorators import action, device, not_action

from .sensors import (
    NODE_ALLOW_PROCESS,
    NODE_COARSE_POSITION,
    NODE_COARSE_SHAKE_MAX_SPEED,
    NODE_FINE_POSITION,
    NODE_FINE_SHAKE_MAX_SPEED,
    NODE_HOME,
    NODE_LOAD_POSITION,
    NODE_PARAMS_WRITTEN,
    NODE_PROCESS_COMPLETE,
    NODE_PROCESS_SELECT,
    NODE_TARGET_WEIGHT,
    POSITION_RANGE,
    PROCESS_DOSE_POWDER,
    PROCESS_ROTATE_TO_FEED,
    PROCESS_SCAN_CARTRIDGES,
    iter_s07_powder_param_vars,
    normalize_powder_params,
    s07_powder_param_var,
    s07_qr_code_var,
)

DEFAULT_POWDER_PARAMS_PATH = Path(__file__).resolve().parent / "s07_powder_params.json"


@device(
    id="szlab_s07_solid_addition",
    display_name="S07 固体加料工位",
    category=["workstation", "szlab"],
    description="苏州实验室 S07 固体加料工位，通过 szlab_poly_plc 转发 PLC 读写",
)
class SZLabS07SolidAdditionDevice:
    def __init__(
        self,
        plc_device_id: str = "szlab_poly_plc",
        process_timeout: float = 300.0,
        poll_interval: float = 0.2,
        *args,
        **kwargs,
    ):
        self.plc_device_id = plc_device_id
        self.process_timeout = process_timeout
        self.poll_interval = poll_interval
        self._plc_gateway: Any = None

    @not_action
    def set_plc_gateway(self, plc_gateway) -> None:
        self._plc_gateway = plc_gateway

    @not_action
    def _plc(self):
        if self._plc_gateway is None:
            raise RuntimeError("S07 固体加料工位尚未绑定 szlab_poly_plc")
        return self._plc_gateway

    @not_action
    def _read_plc_variable(self, node_name: str) -> Any:
        return self._plc().read_variable(node_name, use_cache=False)

    @not_action
    def _write_plc_variable(self, node_name: str, value: Any) -> None:
        self._plc().write_variable(node_name, value)

    @not_action
    def _wait_plc_bool(self, node_name: str, expected: bool, timeout: float, description: str) -> bool:
        return self._wait_plc_equal(node_name, expected, timeout, description)

    @not_action
    def _wait_plc_equal(self, node_name: str, expected: Any, timeout: float, description: str) -> bool:
        plc = self._plc()
        if not hasattr(plc, "wait_variable_equal"):
            raise RuntimeError(f"{self.plc_device_id} 不支持 wait_variable_equal，S07 需要直接复用 plc.py 等待逻辑")
        return bool(plc.wait_variable_equal(node_name, expected, timeout=timeout, interval=self.poll_interval))

    @not_action
    def _wait_process_complete(self, expected: int, timeout: float) -> bool:
        return self._wait_plc_equal(NODE_PROCESS_COMPLETE, expected, timeout, "S07 工艺完成")

    @not_action
    def _reset_unilab_written_params(self) -> None:
        for node, value in (
            (NODE_PROCESS_SELECT, 0),
            (NODE_PARAMS_WRITTEN, False),
            (NODE_LOAD_POSITION, 0),
            (NODE_COARSE_POSITION, 0),
            (NODE_FINE_POSITION, 0),
            (NODE_TARGET_WEIGHT, 0.0),
        ):
            self._write_plc_variable(node, value)
        for node, value in iter_s07_powder_param_vars():
            self._write_plc_variable(node, value)

    @not_action
    def _run_s07_process(self, process_id: int, timeout: float) -> dict[str, Any]:
        timeout = self.process_timeout if timeout is None else timeout
        if not self._wait_plc_bool(NODE_HOME, True, timeout, "S07 原点信号"):
            return {"success": False, "message": "等待 S07 原点信号超时"}
        if not self._wait_plc_bool(NODE_ALLOW_PROCESS, True, timeout, "S07 允许加工"):
            return {"success": False, "message": "等待 S07 允许加工超时"}
        self._write_plc_variable(NODE_PROCESS_SELECT, process_id)
        self._write_plc_variable(NODE_PARAMS_WRITTEN, True)
        if not self._wait_process_complete(process_id, timeout):
            return {"success": False, "message": f"等待 S07 工艺完成超时（期望 {process_id}）"}
        self._reset_unilab_written_params()
        return {"success": True, "process_type": process_id, "status": {"process_complete": process_id}}

    @not_action
    def _read_qr_codes(self) -> dict[int, list[int]]:
        return {
            position: [int(self._read_plc_variable(s07_qr_code_var(position, index)) or 0) for index in range(30)]
            for position in POSITION_RANGE
        }

    @not_action
    def _write_powder_params(self, kind: str, params: dict[str, Any], shake_node: str) -> None:
        normalized = normalize_powder_params(params)
        field_map = {
            "opening": "开口量",
            "feed_speed": "落粉匀速",
            "rotation_speed": "旋转速度",
            "stop_amount": "提请停止量",
        }
        for key, field in field_map.items():
            for index, value in enumerate(normalized[key]):  # type: ignore[index]
                self._write_plc_variable(s07_powder_param_var(kind, field, index), value)
        self._write_plc_variable(shake_node, normalized["shake_max_speed"])

    @not_action
    def _load_powder_params_from_json(self, params_json: str | None, recipe_name: str) -> tuple[dict[str, Any], dict[str, Any]]:
        path = Path(params_json or DEFAULT_POWDER_PARAMS_PATH)
        data = json.loads(path.read_text(encoding="utf-8"))
        if recipe_name not in data:
            raise ValueError(f"注粉参数 JSON 中未找到 recipe: {recipe_name}")
        recipe = data[recipe_name]
        return dict(recipe.get("coarse_params", {})), dict(recipe.get("fine_params", {}))

    @action(auto_prefix=True, description="S07 粉罐扫码盘点")
    def scan_powder_cartridges(self, timeout: float = 300.0) -> dict[str, Any]:
        result = self._run_s07_process(PROCESS_SCAN_CARTRIDGES, timeout)
        if result.get("success"):
            result["qr_codes"] = self._read_qr_codes()
        return result

    @action(auto_prefix=True, description="S07 替换粉罐旋转到进料位")
    def rotate_powder_cartridge_to_feed(self, position: int, timeout: float = 300.0) -> dict[str, Any]:
        if position not in POSITION_RANGE:
            return {"success": False, "message": "position 必须在 1-10 范围内"}
        self._write_plc_variable(NODE_LOAD_POSITION, int(position))
        result = self._run_s07_process(PROCESS_ROTATE_TO_FEED, timeout)
        result["position"] = position
        return result

    @action(auto_prefix=True, description="S07 注粉")
    def dose_powder(
        self,
        coarse_position: int,
        fine_position: int,
        target_weight: float,
        timeout: float = 300.0,
        params_json: str | None = None,
        recipe_name: str = "default",
    ) -> dict[str, Any]:
        if coarse_position not in POSITION_RANGE or fine_position not in POSITION_RANGE:
            return {"success": False, "message": "coarse_position/fine_position 必须在 1-10 范围内"}
        coarse_params, fine_params = self._load_powder_params_from_json(params_json, recipe_name)
        self._write_plc_variable(NODE_COARSE_POSITION, int(coarse_position))
        self._write_plc_variable(NODE_FINE_POSITION, int(fine_position))
        self._write_plc_variable(NODE_TARGET_WEIGHT, float(target_weight))
        self._write_powder_params("粗注粉", coarse_params, NODE_COARSE_SHAKE_MAX_SPEED)
        self._write_powder_params("精注粉", fine_params, NODE_FINE_SHAKE_MAX_SPEED)
        result = self._run_s07_process(PROCESS_DOSE_POWDER, timeout)
        result["target_weight"] = target_weight
        result["recipe_name"] = recipe_name
        return result
