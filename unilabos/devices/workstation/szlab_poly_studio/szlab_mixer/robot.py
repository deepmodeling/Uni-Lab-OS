from __future__ import annotations

from typing import Any

from unilabos.registry.decorators import action, device, not_action


@device(
    id="szlab_mixer_robot",
    display_name="SZLab Mixer 机器人任务",
    category=["robotic_arm"],
    description="SZLab Mixer 机器人任务设备，负责向 PLC 下发 S04/S05 取放料任务号",
)
class SzlabMixerRobotDevice:
    def __init__(
        self,
        plc_device_id: str = "szlab_poly_plc",
        *args,
        **kwargs,
    ):
        self.plc_device_id = plc_device_id
        self._plc_gateway = None
        self._last_task: dict[str, Any] = {}

    @not_action
    def set_plc_gateway(self, plc_gateway) -> None:
        self._plc_gateway = plc_gateway

    @not_action
    def _write_variable(self, name: str, value: Any) -> None:
        if self._plc_gateway is None:
            raise RuntimeError("机器人任务需要注入 szlab_poly_plc 网关")
        self._plc_gateway.write_variable(name, value)

    @not_action
    def _submit_task(
        self,
        task: str,
        station: str,
        task_number: int,
        variables: dict[str, Any] | None = None,
        **data: Any,
    ) -> dict[str, Any]:
        written_variables: dict[str, Any] = {}
        for name, value in (variables or {}).items():
            self._write_variable(name, value)
            written_variables[name] = value
        self._write_variable("PLC_R任务号", int(task_number))
        written_variables["PLC_R任务号"] = int(task_number)

        self._last_task = {
            "task": task,
            "station": station,
            "task_number": int(task_number),
            "status": "submitted",
            "written_variables": written_variables,
            **data,
        }
        return {
            "success": True,
            "status": "submitted",
            "message": f"机器人任务已提交: {station} {task}",
            **self._last_task,
        }

    @not_action
    def _validate_magnetic_stirrer_position(self, position: int) -> int:
        position = int(position)
        if position < 1 or position > 6:
            raise ValueError("磁搅位置必须在 1-6 范围内")
        return position

    @action(auto_prefix=True, description="从磁搅位置取料")
    def submit_pick_from_magnetic_stirrer(self, position: int = 1) -> dict[str, Any]:
        try:
            position = self._validate_magnetic_stirrer_position(position)
        except ValueError as exc:
            return {"success": False, "message": str(exc)}
        try:
            return self._submit_task(
                "pick",
                "S04",
                8,
                variables={"S04取放料编号": position},
                position=position,
            )
        except Exception as exc:
            return {"success": False, "message": str(exc), "task": "pick", "station": "S04", "position": position}

    @action(auto_prefix=True, description="向磁搅位置放料")
    def submit_place_to_magnetic_stirrer(self, position: int = 1, sample_id: str = "") -> dict[str, Any]:
        try:
            position = self._validate_magnetic_stirrer_position(position)
        except ValueError as exc:
            return {"success": False, "message": str(exc)}
        try:
            return self._submit_task(
                "place",
                "S04",
                7,
                variables={"S04取放料编号": position},
                position=position,
                sample_id=sample_id,
            )
        except Exception as exc:
            return {
                "success": False,
                "message": str(exc),
                "task": "place",
                "station": "S04",
                "position": position,
                "sample_id": sample_id,
            }

    @action(auto_prefix=True, description="从拍照工位取料")
    def submit_pick_from_photo_station(self, sample_id: str = "") -> dict[str, Any]:
        try:
            return self._submit_task("pick", "S05", 10, sample_id=sample_id)
        except Exception as exc:
            return {"success": False, "message": str(exc), "task": "pick", "station": "S05", "sample_id": sample_id}

    @action(auto_prefix=True, description="向拍照工位放料")
    def submit_place_to_photo_station(self, sample_id: str = "", allow_occupied: bool = False) -> dict[str, Any]:
        try:
            return self._submit_task(
                "place",
                "S05",
                9,
                sample_id=sample_id,
                allow_occupied=bool(allow_occupied),
            )
        except Exception as exc:
            return {
                "success": False,
                "message": str(exc),
                "task": "place",
                "station": "S05",
                "sample_id": sample_id,
                "allow_occupied": bool(allow_occupied),
            }

    @action(auto_prefix=True, description="读取最近一次机器人任务提交记录")
    def last_submitted_task(self) -> dict[str, Any]:
        return {"success": True, "task": self._last_task}
