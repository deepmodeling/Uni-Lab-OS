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
from typing import Any, Literal

from unilabos.registry.decorators import ActionInputHandle, DataSource, action, device, not_action, topic_config
from unilabos.devices.workstation.szlab_poly_studio.plc import SZLabPolyPLCDevice
from unilabos.devices.workstation.szlab_poly_studio.sensor import wait_sensor_conditions

from .sensors import (
    ADDITION_BEAKER_SENSOR,
    S06_ALLOW_PROCESS_VAR,
    S06_DONE_VAR,
    S06_PARAM_WRITTEN_VAR,
    S06_PROCESS_SELECT_VAR,
    S06_READY_VAR,
    S06PipelineKind,
    S06PipelineRoute,
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
        opcua_client: SZLabPolyPLCDevice | None = None,
        opcua_browse_depth: int = 8,
        opcua_browse_limit: int = 5000,
        opcua_node_id_map: dict[str, str] | None = None,
        opcua_allow_recursive_browse: bool = False,
        plc_device_id: str = "szlab_poly_plc",
        use_plc_gateway: bool = False,
        **kwargs,
    ):
        self.url = url
        self.timeout = timeout
        self.plc_device_id = plc_device_id
        self._plc_gateway = None
        self._client = None
        if opcua_client is not None:
            self._client = opcua_client
        elif not use_plc_gateway:
            self._client = SZLabPolyPLCDevice(
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

    @not_action
    def set_plc_gateway(self, plc_gateway) -> None:
        self._plc_gateway = plc_gateway

    @not_action
    def _opc_client(self):
        client = self._plc_gateway or self._client
        if client is None:
            raise RuntimeError("S06 泵未绑定 PLC gateway，无法访问 OPC UA 变量")
        return client

    @property
    @topic_config()
    def status(self) -> str:
        return self._status

    @not_action
    def disconnect(self) -> None:
        if self._client is not None:
            self._client.disconnect()

    @not_action
    def get_variables(self, variable_names: list[str], use_cache: bool = False) -> dict[str, dict[str, Any]]:
        return self._opc_client().get_variables(variable_names, use_cache=use_cache)

    @not_action
    def get_opc_variable_metadata(self, variable_name: str) -> tuple[str, str | None]:
        return self._opc_client().get_opc_variable_metadata(variable_name)

    @not_action
    def _wait_allow_process(self) -> dict[str, Any] | None:
        """等待 PLC 确认可加工（含储液瓶液量充足等前置条件）。"""
        if self._opc_client().wait_equal(S06_ALLOW_PROCESS_VAR, True, timeout=self.timeout, interval=0.2):
            return None
        return {"success": False, "message": "等待 S06 允许加工超时"}

    @not_action
    def _wait_ready(self) -> dict[str, Any] | None:
        if self._opc_client().wait_equal(S06_READY_VAR, True, timeout=self.timeout, interval=0.2):
            return None
        return {"success": False, "message": "等待 S06 准备信号超时"}

    @not_action
    def _material_sensor_conditions(self, process: int) -> dict[str, bool]:
        del process
        return {ADDITION_BEAKER_SENSOR: True}

    @not_action
    def _wait_material_sensors(self, process: int, phase: str) -> dict[str, Any]:
        conditions = self._material_sensor_conditions(process)
        client = self._opc_client()
        context = f"S06 加液{'前置' if phase == 'pre' else '后置'}传感器检查"
        waiter = getattr(client, "wait_sensor_conditions", None)
        if callable(waiter):
            success, values = waiter(conditions, timeout=self.timeout, interval=0.2, context=context)
        else:
            success, values = wait_sensor_conditions(
                client,
                conditions,
                timeout=self.timeout,
                interval=0.2,
                context=context,
            )
        return {
            "success": bool(success),
            "phase": phase,
            "conditions": conditions,
            "values": values,
            "mismatches": {
                name: {"expected": expected, "actual": values.get(name)}
                for name, expected in conditions.items()
                if values.get(name) != expected
            },
        }

    @not_action
    def _apply_pipeline_route(self, pump: int, pipeline: S06PipelineKind) -> None:
        route = self._pipeline_routes[(pump, pipeline)]
        self._opc_client().write(s06_pump_valve_var(pump), int(route.control_valve))
        self._opc_client().write(s06_pump_position_var(pump), int(route.absolute_position))

    @not_action
    def _s06_amount_vars_for_process(self, process: int) -> list[str]:
        if process == 3:
            return [s06_solution_amount_var(1), s06_solution_amount_var(2)]
        return [s06_solution_amount_var(process)]

    @not_action
    def _s06_amount_values_for_process(
        self,
        process: int,
        *,
        volume_pump_1: int = 0,
        volume_pump_2: int = 0,
    ) -> dict[str, int]:
        if process == 1:
            return {s06_solution_amount_var(1): int(volume_pump_1)}
        if process == 2:
            return {s06_solution_amount_var(2): int(volume_pump_2)}
        return {
            s06_solution_amount_var(1): int(volume_pump_1),
            s06_solution_amount_var(2): int(volume_pump_2),
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
                self._opc_client().write(name, value)
            except Exception:
                # 清理阶段尽量执行，不用二次异常覆盖真正的执行错误。
                continue

    @not_action
    def _execute_s06_addition(
        self,
        process: int,
        *,
        require_allow: bool = True,
        volume_pump_1: int = 0,
        volume_pump_2: int = 0,
    ) -> dict[str, Any]:
        """按最新 PLC 接口执行 S06 加液：工艺选择 + 溶液添加量 + 参数写入。"""
        if process not in (1, 2, 3):
            return {"success": False, "message": "S06 工艺选择必须为 1、2 或 3"}
        amount_values = self._s06_amount_values_for_process(
            process,
            volume_pump_1=volume_pump_1,
            volume_pump_2=volume_pump_2,
        )
        invalid_amounts = [name for name, amount in amount_values.items() if amount <= 0]
        if invalid_amounts:
            return {"success": False, "message": f"{', '.join(invalid_amounts)} 的体积必须大于 0"}

        try:
            sensor_precheck = self._wait_material_sensors(process, phase="pre")
        except Exception as exc:
            self._status = "Error"
            return {"success": False, "message": f"S06 前置物料传感器读取失败: {exc}"}
        if not sensor_precheck["success"]:
            self._status = "Error"
            return {
                "success": False,
                "message": "S06 等待加液烧杯在位超时",
                "sensor_precheck": sensor_precheck,
            }

        if require_allow:
            err = self._wait_allow_process()
            if err:
                return err
            err = self._wait_ready()
            if err:
                return err

        for amount_var in amount_values:
            accessible, detail = self._opc_client().check_variable_accessible(amount_var)
            if not accessible:
                self._status = "Error"
                return {"success": False, "message": f"{amount_var} 的 OPC UA NodeId 无效，无法执行工艺 {process}: {detail}"}

        self._status = "Running"
        try:
            try:
                self._opc_client().write(S06_PROCESS_SELECT_VAR, int(process))
                for amount_var, amount in amount_values.items():
                    self._opc_client().write(amount_var, amount)
                self._opc_client().write(S06_PARAM_WRITTEN_VAR, True)
            except Exception as exc:
                self._status = "Error"
                return {"success": False, "message": str(exc)}
            try:
                if not self._opc_client().wait_new_cycle_done(S06_DONE_VAR, timeout=self.timeout):
                    self._status = "Error"
                    return {"success": False, "message": "S06 加工完成等待超时"}
            except Exception:
                self._status = "Error"
                raise
        finally:
            self._clear_s06_written_params(process)
        try:
            sensor_postcheck = self._wait_material_sensors(process, phase="post")
        except Exception as exc:
            self._status = "Error"
            return {
                "success": False,
                "status": "verification_failed",
                "message": f"S06 加液已完成，但物料传感器读取失败: {exc}",
            }
        if not sensor_postcheck["success"]:
            self._status = "Error"
            return {
                "success": False,
                "status": "verification_failed",
                "message": "S06 加液已完成，但烧杯在位验证失败",
                "sensor_precheck": sensor_precheck,
                "sensor_postcheck": sensor_postcheck,
            }
        self._status = "Idle"
        return {
            "success": True,
            "message": f"S06 工艺 {process} 溶液添加完成",
            "data": {
                "process": process,
                "volume_pump_1": volume_pump_1,
                "volume_pump_2": volume_pump_2,
                "amount_values": amount_values,
                "sensor_precheck": sensor_precheck,
                "sensor_postcheck": sensor_postcheck,
            },
        }

    @not_action
    def _execute_s06_step(
        self,
        process: int,
        pipeline: S06PipelineKind,
        volume: int,
        direction: Literal["aspirate", "dispense"],
        *,
        require_allow: bool = True,
    ) -> dict[str, Any]:
        del pipeline, direction
        if process not in (1, 2, 3):
            return {"success": False, "message": "S06 工艺选择必须为 1、2 或 3"}
        return self._execute_s06_addition(
            process,
            require_allow=require_allow,
            volume_pump_1=volume,
            volume_pump_2=volume,
        )

    @action(
        auto_prefix=True,
        description="执行 S06 单步转液（工艺选择 + 溶液添加量 + 加工完成等待）",
        handles=[
            ActionInputHandle(
                key="process",
                data_type="szlab_s06_process_select",
                label="S06工艺选择",
                data_key="process",
                data_source=DataSource.HANDLE,
                description="S06 工艺选择，1=只加1号溶液，2=只加2号溶液，3=1和2都添加",
            )
        ],
    )
    def transfer_liquid(
        self,
        process: int = 1,
        volume: int = 1,
        direction: Literal["aspirate", "dispense"] = "aspirate",
        pipeline: S06PipelineKind = "aspirate",
    ) -> dict[str, Any]:
        return self._execute_s06_step(
            process,
            pipeline=pipeline,
            volume=volume,
            direction=direction,
        )

    @action(
        auto_prefix=True,
        description="S06 泵加液完整流程：烧杯检测 → 液位确认 → 写入工艺参数 → 等待加工完成",
        handles=[
            ActionInputHandle(
                key="process",
                data_type="szlab_s06_process_select",
                label="S06工艺选择",
                data_key="process",
                data_source=DataSource.HANDLE,
                description="S06 工艺选择，1=只加1号溶液，2=只加2号溶液，3=1和2都添加",
            )
        ],
    )
    def run_solvent_addition(
        self,
        process: int = 1,
        volume_pump_1: int = 1,
        volume_pump_2: int = 1,
        skip_level_check: bool = False,
        beaker_true_means_present: bool = True,
    ) -> dict[str, Any]:
        if process not in (1, 2, 3):
            return {"success": False, "message": "S06 工艺选择必须为 1、2 或 3"}
        del skip_level_check, beaker_true_means_present

        self._status = "Running"
        steps: list[dict[str, Any]] = []

        result = self._execute_s06_addition(
            process,
            require_allow=True,
            volume_pump_1=volume_pump_1,
            volume_pump_2=volume_pump_2,
        )
        steps.append({"step": "写入溶液添加量并启动 S06", **result})
        if not result["success"]:
            self._status = "Error"
            return {**result, "steps": steps}

        self._status = "Idle"
        return {
            "success": True,
            "message": f"S06 工艺 {process} 加液流程完成",
            "data": {
                "process": process,
                "volume_pump_1": volume_pump_1,
                "volume_pump_2": volume_pump_2,
            },
            "steps": steps,
        }


if __name__ == "__main__":
    import runpy

    runpy.run_module("unilabos.devices.workstation.szlab_poly_studio.s06_pump.debug_pump", run_name="__main__")
