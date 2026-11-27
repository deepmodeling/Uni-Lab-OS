# DSL 节点赋值即工作流审计事实，部分节点无需再被 Python 表达式引用。
# ruff: noqa: F841

from unilabos.workflow.authoring import device, workflow, workflow_output, resources, group, parallel, repeat_until, until
from parallel_lab.devices.channel import ChannelA, ChannelB, Region

a: ChannelA = device("channel_a")
b: ChannelB = device("channel_b")
region: Region = device("region")

@workflow(workflow_uuid="70000000-0000-4000-8000-000000000002", displayname="综合并行验收 B", description="真实Edge到PLC Sim；含共享范围、并行join、条件和三轮循环")
def composite_b(*, choose_first: bool = True, hold_seconds: float = 2.0):
    # unilab:node_uuid=7d4d57fa-2ca0-5334-aa9d-9b52a68f9e46
    area = region.marker()
    # unilab:node_uuid=bbee0571-3255-58f0-b13c-dbf7052b66b2
    start = b.run(label="起步", hold_seconds=hold_seconds, dependency=area.message)
    with resources("region"):
        # unilab:node_uuid=a7062948-8b60-5fe4-84ac-3d056190b4fe
        first = b.run(label="共同范围第一步", hold_seconds=hold_seconds, dependency=start.message)
        # unilab:node_uuid=1e6eb5aa-fb42-57dc-ae60-532c367ce517
        second = a.run(label="共同范围第二步", hold_seconds=hold_seconds, dependency=first.message)
    with parallel():
        # unilab:node_uuid=73836639-e3a6-54b1-bd45-af95736b8204
        with group(name="left"):
            # unilab:node_uuid=8202a0e7-08cf-560f-bd6f-8488bfae1ef9
            left = b.run(label="并行分支一", hold_seconds=hold_seconds, dependency=second.message)
        # unilab:node_uuid=b517c532-60b5-501b-9d9b-c28a25342276
        with group(name="right"):
            # unilab:node_uuid=0ba051f7-7d9a-579b-9a6e-8b4e09a7c150
            right = a.run(label="并行分支二", hold_seconds=hold_seconds, dependency=second.message)
    # unilab:node_uuid=aa314c13-2408-59dd-8a49-1ebc34716185
    joined = b.run(label="汇合", hold_seconds=hold_seconds, dependency=left.message, dependency2=right.message)
    # unilab:node_uuid=82e86d61-c05b-537b-92bc-7961bdfe6b74
    with repeat_until(max_iterations=3, carry={"iteration": 1}) as loop:
        # unilab:node_uuid=24afc625-fa76-59c8-8950-a50261843c8e
        measured = b.run(label="循环", iteration=loop.carry["iteration"], stop_after=3, hold_seconds=hold_seconds)
        loop.next(iteration=measured.next_iteration)
        until(measured.done)
    # unilab:node_uuid=bc87bed6-accf-5b6f-a201-0f4f9dd07bd2
    if choose_first:
        # unilab:node_uuid=b00b2d15-5dfd-51b8-820e-0673edb76e78
        yes = b.run(label="条件真", hold_seconds=hold_seconds)
    else:
        # unilab:node_uuid=ff8f16a5-c54c-585d-a2d9-f514a2a77e89
        no = a.run(label="条件假", hold_seconds=hold_seconds)
    # unilab:node_uuid=50f5e465-e6a9-56ee-9101-6f2d118287fa
    finished = a.run(label="收尾", hold_seconds=hold_seconds)
    return workflow_output()
