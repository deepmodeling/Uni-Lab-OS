from unilabos.workflow.authoring import device, workflow_definition
from szlab_poly_studio.devices.szlab_mixer_robot.device import SzlabMixerRobotDevice

szlab_mixer_robot: SzlabMixerRobotDevice = device("szlab_mixer_robot")

@workflow_definition(
    workflow_uuid="cbd240eb-aaa1-42e7-a7de-4731733dca1d",
    displayname="可导入实验操作示例",
    workflow_type="experiment_operation",
)
def importable_operation(*, position: int = 1) -> None:
    # unilab:node_uuid=ed3f9018-948c-47ce-b384-1d473de7abff
    picked = szlab_mixer_robot.submit_pick_from_s04(position=position)
