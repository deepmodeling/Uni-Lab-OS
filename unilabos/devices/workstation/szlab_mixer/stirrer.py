from __future__ import annotations

from typing import Any

from unilabos.registry.decorators import ActionInputHandle, DataSource, action, device, not_action, topic_config

from .opcua_client import SzlabMixerOpcUaClient


@device(
    id="szlab_mixer_stirrer",
    display_name="SZLab 磁搅",
    category=["heaterstirrer"],
    description="SZLab VirtualMixer 磁搅工位设备",
)
class SzlabMixerStirrerDevice:
    def __init__(
        self,
        url: str,
        username: str | None = None,
        password: str | None = None,
        timeout: float = 300.0,
        **kwargs,
    ):
        self.url = url
        self.timeout = timeout
        self._client = SzlabMixerOpcUaClient(url=url, username=username, password=password)
        self._last_position = 1
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

    @action(
        auto_prefix=True,
        description="运行磁搅工位",
        handles=[
            ActionInputHandle(
                key="stirrer_position",
                data_type="szlab_mixer_stirrer_position",
                label="磁搅位置",
                data_key="position",
                data_source=DataSource.HANDLE,
                description="磁搅工位编号，范围 1-6",
            )
        ],
    )
    def run_stirring(
        self,
        position: int = 1,
        speed: int = 300,
        temperature: int = 25,
        duration: int = 60,
    ) -> dict[str, Any]:
        if position < 1 or position > 6:
            return {"success": False, "message": "磁搅位置必须在 1-6 范围内"}
        self._status = "Running"
        self._last_position = position
        index = position - 1
        station = f"S04{position}"

        if not bool(self._client.read(f"{station}允许加工")):
            self._status = "Error"
            return {"success": False, "message": f"{station} 不允许加工"}

        self._client.write(f"磁搅速度设置_上位机[{index}]", int(speed))
        self._client.write(f"磁搅温度设置_上位机[{index}]", int(temperature))
        self._client.write(f"磁搅安全温度设置_上位机[{index}]", int(max(temperature + 10, temperature)))
        self._client.write(f"磁搅时间设置_上位机[{index}]", int(duration))
        self._client.write(f"磁搅搅拌_上位机[{index}]", True)
        self._client.write(f"磁搅加热_上位机[{index}]", int(temperature) > 0)
        self._client.pulse(f"{station}参数写入完成")

        if not self._client.wait_new_cycle_done(f"{station}加工完成", timeout=self.timeout):
            self._status = "Error"
            return {"success": False, "message": f"{station} 加工完成等待超时"}
        self._status = "Idle"
        return {
            "success": True,
            "message": f"磁搅工位 {position} 执行完成",
            "data": {
                "position": position,
                "speed": speed,
                "temperature": temperature,
                "duration": duration,
            },
        }
