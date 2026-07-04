"""SZLab VirtualMixer S06 注射泵设备驱动。

Docker 本地调试（推荐）：

1. 拉取镜像::

       docker pull registry-1.docker.io/styxhuang/unilabos:latest

   Mac Silicon 需指定平台::

       docker pull --platform linux/amd64 registry-1.docker.io/styxhuang/unilabos:latest

2. 启动 UI::

       docker run --rm \\
         --name unilabos-ui \\
         --platform linux/amd64 \\
         -p 50003:8000 \\
         registry-1.docker.io/styxhuang/unilabos:latest

3. 浏览器打开 http://localhost:50003/ ，选择 ``transfer_liquid`` 或 ``run_solvent_addition``，
   在页面填写 OPC UA URL（默认见 ``DEFAULT_OPCUA_URL``）后执行。

单元测试与伪 OPC UA 联调见 ``tests/szlab_poly_studio/README.md``。
单独调试脚本见 ``debug_pump.py``（改通讯地址即可切换虚拟/真机）。
"""

from __future__ import annotations

import os
import time
from typing import Any, Literal

from unilabos.registry.decorators import ActionInputHandle, DataSource, action, device, not_action, topic_config
from unilabos.devices.workstation.szlab_poly_studio.plc import SZLabPolyPLCDevice

from .sensors import (
    ADDITION_BEAKER_SENSOR,
    ROBOT_BEAKER_PICK_VAR,
    ROBOT_BEAKER_PLACE_VAR,
    S06_ALLOW_PROCESS_VAR,
    S06_DONE_VAR,
    S06_PARAM_WRITTEN_VAR,
    S06_PROCESS_SELECT_VAR,
    S06_READY_VAR,
    S06PipelineKind,
    S06PipelineRoute,
    STORAGE_BOTTLE_PRESENT,
    parse_pipeline_route_specs,
    s06_pump_position_var,
    s06_pump_valve_var,
    s06_solution_amount_var,
)

DOCKER_IMAGE = "registry-1.docker.io/styxhuang/unilabos:latest"
DOCKER_UI_URL = "http://localhost:50003/"
DEFAULT_OPCUA_URL = os.environ.get(
    "UNILABOS_SZLAB_MIXER_OPCUA_URL",
    "opc.tcp://jdht1471820.bohrium.tech:50001",
)


@device(
    id="szlab_mixer_pump",
    display_name="SZLab 注射泵",
    category=["pump_and_valve"],
    description="SZLab VirtualMixer S06 加溶液工位（注射泵）",
)
class SzlabMixerPumpDevice:
    def __init__(
        self,
        url: str = DEFAULT_OPCUA_URL,
        username: str | None = None,
        password: str | None = None,
        timeout: float = 300.0,
        pipeline_routes: dict[tuple[int, S06PipelineKind], S06PipelineRoute] | None = None,
        pipeline_route_specs: list[dict[str, Any]] | None = None,
        robot_addition_position: int = 0,
        robot_stirrer_position: int = 0,
        opcua_client: SZLabPolyPLCDevice | None = None,
        opcua_browse_depth: int = 8,
        opcua_browse_limit: int = 5000,
        opcua_node_id_map: dict[str, str] | None = None,
        opcua_allow_recursive_browse: bool = False,
        **kwargs,
    ):
        self.url = url
        self.timeout = timeout
        self._robot_addition_position = int(robot_addition_position)
        self._robot_stirrer_position = int(robot_stirrer_position)
        self._client = opcua_client or SZLabPolyPLCDevice(
            url=url,
            csv_path=False,
            username=username,
            password=password,
            opcua_object_name="VirtualMixer",
            opcua_browse_depth=opcua_browse_depth,
            opcua_browse_limit=opcua_browse_limit,
            node_id_map=opcua_node_id_map,
            opcua_allow_recursive_browse=opcua_allow_recursive_browse,
        )
        specs = pipeline_route_specs or kwargs.pop("pipeline_route_specs", None)
        self._pipeline_routes = pipeline_routes or parse_pipeline_route_specs(specs)
        self._status = "Idle"

    @property
    @topic_config()
    def status(self) -> str:
        return self._status

    @not_action
    def disconnect(self) -> None:
        self._client.disconnect()

    @not_action
    def get_variables(self, variable_names: list[str], use_cache: bool = False) -> dict[str, dict[str, Any]]:
        return self._client.get_variables(variable_names, use_cache=use_cache)

    @not_action
    def get_opc_variable_metadata(self, variable_name: str) -> tuple[str, str | None]:
        return self._client.get_opc_variable_metadata(variable_name)

    @not_action
    def _read_bool(self, name: str) -> bool:
        return bool(self._client.read(name))

    @not_action
    def _wait_beaker_present(self, beaker_true_means_present: bool = True) -> dict[str, Any] | None:
        deadline = time.time() + self.timeout
        while time.time() < deadline:
            if self._read_bool(ADDITION_BEAKER_SENSOR) == beaker_true_means_present:
                return None
            time.sleep(0.2)
        return {"success": False, "message": "等待加液位放置烧杯超时"}

    @not_action
    def _wait_allow_process(self) -> dict[str, Any] | None:
        """等待 PLC 确认可加工（含储液瓶液量充足等前置条件）。"""
        deadline = time.time() + self.timeout
        while time.time() < deadline:
            if self._read_bool(S06_ALLOW_PROCESS_VAR):
                return None
            time.sleep(0.2)
        return {"success": False, "message": "等待 S06 允许加工超时"}

    @not_action
    def _ensure_storage_bottle_present(self, pump: int) -> dict[str, Any] | None:
        """确认储液瓶在位；液量是否足够由 PLC 通过 S06允许加工 反馈。"""
        pumps = (1, 2) if pump == 3 else (pump,)
        for pump_index in pumps:
            present_var = STORAGE_BOTTLE_PRESENT.get(pump_index)
            if not present_var:
                continue
            if not self._read_bool(present_var):
                return {"success": False, "message": f"储液瓶 {pump_index} 未检测到在位"}
        return None

    @not_action
    def _apply_pipeline_route(self, pump: int, pipeline: S06PipelineKind) -> None:
        route = self._pipeline_routes[(pump, pipeline)]
        self._client.write(s06_pump_valve_var(pump), int(route.control_valve))
        self._client.write(s06_pump_position_var(pump), int(route.absolute_position))

    @not_action
    def _s06_amount_vars_for_process(self, process: int) -> list[str]:
        if process == 3:
            return [s06_solution_amount_var(1), s06_solution_amount_var(2)]
        return [s06_solution_amount_var(process)]

    @not_action
    def _s06_amount_values_for_process(
        self,
        process: int,
        volume: int,
        *,
        volume_pump_1: int = 0,
        volume_pump_2: int = 0,
    ) -> dict[str, int]:
        if process == 1:
            return {s06_solution_amount_var(1): int(volume_pump_1 or volume)}
        if process == 2:
            return {s06_solution_amount_var(2): int(volume_pump_2 or volume)}
        return {
            s06_solution_amount_var(1): int(volume_pump_1 or volume),
            s06_solution_amount_var(2): int(volume_pump_2 or volume),
        }

    @not_action
    def _clear_s06_written_params(self, process: int) -> None:
        """加工结束后清除 PC 写入 PLC 的 S06 参数。"""
        for name, value in (
            (S06_PARAM_WRITTEN_VAR, False),
            (S06_PROCESS_SELECT_VAR, 0),
            *((amount_var, 0) for amount_var in self._s06_amount_vars_for_process(process)),
        ):
            try:
                self._client.write(name, value)
            except Exception:
                # 清理阶段尽量执行，不用二次异常覆盖真正的执行错误。
                continue

    @not_action
    def _execute_s06_addition(
        self,
        pump: int,
        volume: int,
        *,
        require_allow: bool = True,
        volume_pump_1: int = 0,
        volume_pump_2: int = 0,
    ) -> dict[str, Any]:
        """按最新 PLC 接口执行 S06 加液：工艺选择 + 溶液添加量 + 参数写入。"""
        if pump not in (1, 2, 3):
            return {"success": False, "message": "S06 工艺选择必须为 1、2 或 3"}
        amount_values = self._s06_amount_values_for_process(
            pump,
            volume,
            volume_pump_1=volume_pump_1,
            volume_pump_2=volume_pump_2,
        )
        invalid_amounts = [name for name, amount in amount_values.items() if amount <= 0]
        if invalid_amounts:
            return {"success": False, "message": f"{', '.join(invalid_amounts)} 的体积必须大于 0"}

        if require_allow and not self._read_bool(S06_ALLOW_PROCESS_VAR):
            return {"success": False, "message": "S06 不允许加工"}

        for amount_var in amount_values:
            accessible, detail = self._client.check_variable_accessible(amount_var)
            if not accessible:
                self._status = "Error"
                return {"success": False, "message": f"{amount_var} 的 OPC UA NodeId 无效，无法执行工艺 {pump}: {detail}"}

        self._status = "Running"
        try:
            self._client.write(S06_PROCESS_SELECT_VAR, int(pump))
            for amount_var, amount in amount_values.items():
                self._client.write(amount_var, amount)
            self._client.write(S06_PARAM_WRITTEN_VAR, True)
        except Exception as exc:
            self._status = "Error"
            self._clear_s06_written_params(pump)
            return {"success": False, "message": str(exc)}
        try:
            if not self._client.wait_new_cycle_done(S06_DONE_VAR, timeout=self.timeout):
                self._status = "Error"
                return {"success": False, "message": "S06 加工完成等待超时"}
        finally:
            self._clear_s06_written_params(pump)
        self._status = "Idle"
        return {
            "success": True,
            "message": f"S06 工艺 {pump} 溶液添加完成",
            "data": {
                "process": pump,
                "volume": volume,
                "volume_pump_1": volume_pump_1,
                "volume_pump_2": volume_pump_2,
                "amount_values": amount_values,
            },
        }

    @not_action
    def _execute_s06_step(
        self,
        pump: int,
        pipeline: S06PipelineKind,
        volume: int,
        direction: Literal["aspirate", "dispense"],
        *,
        require_allow: bool = True,
    ) -> dict[str, Any]:
        del pipeline, direction
        if pump not in (1, 2, 3):
            return {"success": False, "message": "S06 工艺选择必须为 1、2 或 3"}
        return self._execute_s06_addition(pump, volume, require_allow=require_allow)

    @not_action
    def _transport_beaker_to_stirrer(self, skip_robot: bool) -> dict[str, Any]:
        if skip_robot:
            return {"success": True, "message": "已跳过机械臂搬运", "skipped": True}
        if self._robot_addition_position <= 0 or self._robot_stirrer_position <= 0:
            return {"success": False, "message": "机械臂加液位/磁搅位编号待定义"}
        pick = self._robot_addition_position
        place = self._robot_stirrer_position
        self._client.write(ROBOT_BEAKER_PICK_VAR, pick)
        self._client.write(ROBOT_BEAKER_PLACE_VAR, place)
        return {
            "success": True,
            "message": "已下发机械臂烧杯搬运位号（取放完成等待由机器人模块负责）",
            "data": {"pick_position": pick, "place_position": place},
        }

    @action(
        auto_prefix=True,
        description="执行 S06 单步转液（选泵 + 管路 + 抽液或排液）",
        handles=[
            ActionInputHandle(
                key="pump_index",
                data_type="szlab_mixer_pump_index",
                label="注射泵编号",
                data_key="pump",
                data_source=DataSource.HANDLE,
                description="S06 工艺选择，1=只加1号溶液，2=只加2号溶液，3=1和2都添加",
            )
        ],
    )
    def transfer_liquid(
        self,
        pump: int = 1,
        volume: int = 1,
        direction: Literal["aspirate", "dispense"] = "aspirate",
        pipeline: S06PipelineKind = "aspirate",
    ) -> dict[str, Any]:
        return self._execute_s06_step(
            pump,
            pipeline=pipeline,
            volume=volume,
            direction=direction,
        )

    @action(
        auto_prefix=True,
        description="S06 泵加液完整流程：烧杯检测 → 液位确认 → 储液瓶抽液排至烧杯 → 可选抽空气 → 机械臂骨架",
        handles=[
            ActionInputHandle(
                key="pump_index",
                data_type="szlab_mixer_pump_index",
                label="注射泵编号",
                data_key="pump",
                data_source=DataSource.HANDLE,
                description="S06 工艺选择，1=只加1号溶液，2=只加2号溶液，3=1和2都添加",
            )
        ],
    )
    def run_solvent_addition(
        self,
        pump: int = 1,
        volume: int = 1,
        volume_pump_1: int = 0,
        volume_pump_2: int = 0,
        skip_level_check: bool = False,
        skip_robot: bool = True,
        beaker_true_means_present: bool = True,
    ) -> dict[str, Any]:
        if pump not in (1, 2, 3):
            return {"success": False, "message": "S06 工艺选择必须为 1、2 或 3"}

        self._status = "Running"
        steps: list[dict[str, Any]] = []

        if not self._read_bool(S06_READY_VAR):
            self._status = "Error"
            return {"success": False, "message": "S06 未就绪（准备信号为 false）"}

        err = self._wait_beaker_present(beaker_true_means_present)
        if err:
            self._status = "Error"
            return err

        if not skip_level_check:
            err = self._ensure_storage_bottle_present(pump)
            if err:
                self._status = "Error"
                return err

        err = self._wait_allow_process()
        if err:
            self._status = "Error"
            return err

        result = self._execute_s06_addition(
            pump,
            volume,
            require_allow=False,
            volume_pump_1=volume_pump_1,
            volume_pump_2=volume_pump_2,
        )
        steps.append({"step": "写入溶液添加量并启动 S06", **result})
        if not result["success"]:
            self._status = "Error"
            return {**result, "steps": steps}

        robot_result = self._transport_beaker_to_stirrer(skip_robot)
        steps.append({"step": "机械臂至磁搅", **robot_result})
        if not robot_result["success"]:
            self._status = "Error"
            return {**robot_result, "steps": steps}

        self._status = "Idle"
        return {
            "success": True,
            "message": f"S06 泵 {pump} 加液流程完成",
            "data": {
                "pump": pump,
                "volume": volume,
                "volume_pump_1": volume_pump_1,
                "volume_pump_2": volume_pump_2,
            },
            "steps": steps,
        }


if __name__ == "__main__":
    import runpy

    runpy.run_module("unilabos.devices.workstation.szlab_poly_studio.s06_pump.debug_pump", run_name="__main__")
