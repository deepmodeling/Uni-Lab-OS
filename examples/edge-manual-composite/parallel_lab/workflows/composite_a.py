# DSL 节点赋值即工作流审计事实，部分节点无需再被 Python 表达式引用。
# ruff: noqa: F841

from unilabos.workflow.authoring import device, workflow, workflow_output, resources, group, parallel, repeat_until, until
from parallel_lab.devices.channel import ChannelA, ChannelB, Region

a: ChannelA = device("channel_a")
b: ChannelB = device("channel_b")
region: Region = device("region")

@workflow(workflow_uuid="70000000-0000-4000-8000-000000000001", displayname="综合并行验收 A", description="真实Edge到PLC Sim；含共享范围、并行join、条件和三轮循环")
def composite_a(*, choose_first: bool = True, hold_seconds: float = 2.0):
    # unilab:node_uuid=2f998b2d-a539-583c-b2f8-42d06c887928
    area = region.marker()
    # unilab:node_uuid=b41fc615-f3ba-50ff-942f-5e9de10a71a3
    start = a.run(label="起步", hold_seconds=hold_seconds, dependency=area.message)
    with resources("region"):
        # unilab:node_uuid=d01597f8-c8d8-5fcf-b187-c20778493a78
        first = a.run(label="共同范围第一步", hold_seconds=hold_seconds, dependency=start.message)
        # unilab:node_uuid=d970ea2e-124d-5225-9fbc-87dedfdcbca4
        second = b.run(label="共同范围第二步", hold_seconds=hold_seconds, dependency=first.message)
    with parallel():
        # unilab:node_uuid=63e38dde-dfca-54a9-b860-3ce08caf8d5d
        with group(name="left"):
            # unilab:node_uuid=77522b37-94e0-5914-a5e6-d43777c7e08f
            left = a.run(label="并行分支一", hold_seconds=hold_seconds, dependency=second.message)
        # unilab:node_uuid=512f0eaa-0d9e-56b4-8b5c-337a85d02e3f
        with group(name="right"):
            # unilab:node_uuid=2183e7d5-6368-5735-97e3-cbf4b661ea99
            right = b.run(label="并行分支二", hold_seconds=hold_seconds, dependency=second.message)
    # unilab:node_uuid=2ffc4c75-3fb7-5daf-b60a-4f44d0a5cc1b
    joined = a.run(label="汇合", hold_seconds=hold_seconds, dependency=left.message, dependency2=right.message)
    # unilab:node_uuid=1c54d43b-85b8-5948-953b-07b86569fa15
    if choose_first:
        # unilab:node_uuid=3ad08ea7-bfec-58bd-a8ff-b961242f2656
        yes = a.run(label="条件真", hold_seconds=hold_seconds)
    else:
        # unilab:node_uuid=5faf73a2-6b29-5072-bfa0-1e5903d59026
        no = b.run(label="条件假", hold_seconds=hold_seconds)
    # unilab:node_uuid=708e616f-e9be-54be-8ad7-a495238a554c
    with repeat_until(max_iterations=3, carry={"iteration": 1}) as loop:
        # unilab:node_uuid=a7995913-a7de-5583-8dc3-ca1c5521fe5b
        measured = a.run(label="循环", iteration=loop.carry["iteration"], stop_after=3, hold_seconds=hold_seconds)
        loop.next(iteration=measured.next_iteration)
        until(measured.done)
    # unilab:node_uuid=612e2fc3-f78e-5970-952f-a9b1e841c528
    finished = b.run(label="收尾", hold_seconds=hold_seconds)
    return workflow_output()
