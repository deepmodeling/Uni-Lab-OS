"""Uni-Lab OS Python 导入示例。

该文件由 Local 模式的静态 AST 编译器读取，不会被服务端执行。
"""

from szlab_poly_studio.devices.szlab_mixer_pump.device import SzlabMixerPumpDevice
from unilabos.workflow.authoring import device, workflow_definition


szlab_mixer_pump_device: SzlabMixerPumpDevice = device("szlab_mixer_pump")


@workflow_definition(
    workflow_uuid="7c8a0c5e-44f4-4ad4-9f8c-0a6d9e1d7b21",
    displayname="导入测试实验操作",
    description="用于验证 Python 工作流导入。",
    tags=["import-test"],
    workflow_type="experiment_operation",
)
def import_demo_operation(*, pump: int = 1, volume: int = 8) -> None:
    # unilab:node_uuid=6db5d2a9-2e5b-4d6f-9a8b-1c2d3e4f5a60
    szlab_mixer_pump_device.run_solvent_addition(
        beaker_true_means_present=True,
        process=1,
        pump=pump,
        skip_level_check=False,
        skip_robot=True,
        volume=volume,
        volume_pump_1=1,
        volume_pump_2=1,
    )
