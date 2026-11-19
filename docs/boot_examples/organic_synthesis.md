# 有机常量合成样例

本样例演示如何配置和操作有机常量合成工作站，实现抽真空和充气等基本操作。

## 准备工作

### 设备配置文件

有机合成工作站的完整配置可在 `unilabos/test/experiments/mock_reactor.json` 文件中找到。该配置文件采用平展结构，通过 `type` 字段区分物料和设备，并通过 `parent` 和 `children` 字段实现层级关系。

配置文件示例片段：

```json
{
    "nodes": [
        {
            "id": "ReactorX",
            "children": [
                "reactor",
                "vacuum_valve",
                "gas_valve",
                "vacuum_pump",
                "gas_source"
            ],
            "parent": null,
            "type": "device",
            "class": "workstation"
        },
        {
            "id": "reactor",
            "parent": "ReactorX",
            "type": "container",
            "class": "RegularContainer",
            "template_name": "container",
            "config": {"type": "RegularContainer", "max_volume": 5000.0}
        },
        {
            "id": "vacuum_valve",
            "parent": "ReactorX",
            "type": "device"
        },
        {
            "id": "gas_valve",
            "parent": "ReactorX",
            "type": "device"
        },
        {
            "id": "vacuum_pump",
            "parent": "ReactorX",
            "type": "device"
        },
        {
            "id": "gas_source",
            "parent": "ReactorX",
            "type": "device"
        }
    ],
    "links": [
        {
            "source": "reactor",
            "target": "vacuum_valve"
        },
        {
            "source": "reactor",
            "target": "gas_valve"
        },
        {
            "source": "vacuum_pump",
            "target": "vacuum_valve"
        },
        {
            "source": "gas_source",
            "target": "gas_valve"
        }
    ]
}
```

配置文件定义了反应系统的组成部分，主要包括：

1. **反应工作站 (ReactorX)** - 整个系统的父节点，包含所有子组件
2. **反应器 (reactor)** - 实际进行反应的容器
3. **真空阀 (vacuum_valve)** - 连接反应器和真空泵
4. **气体阀 (gas_valve)** - 连接反应器和气源
5. **真空泵 (vacuum_pump)** - 用于抽真空
6. **气源 (gas_source)** - 提供充气

这些组件通过链接关系形成一个完整的气路系统，可以实现抽真空和充气的功能。

## 启动方法

### 1. 启动反应器节点

使用以下命令启动模拟反应器：

```bash
unilab -g unilabos/test/experiments/mock_reactor.json
```

### 2. 执行抽真空和充气操作

默认的 HostLink backend 下，在网页端对 `ReactorX` 发起 `EvacuateAndRefillProtocol`，或直接调用
微后端的单动作接口。动作的物料入参都要带物料 uuid（`id` 留给协议编译器查拓扑），uuid 可从
`GET http://localhost:8002/api/v1/graphs/live/payload` 里 `reactor` 节点读到：

```bash
curl -s -X POST http://localhost:8002/api/v1/job/add -H "Content-Type: application/json" -d '{
  "device_id": "ReactorX",
  "action": "EvacuateAndRefillProtocol",
  "action_type": "EvacuateAndRefillProtocol",
  "action_args": {"vessel": {"id": "reactor", "uuid": "<reactor 的物料 uuid>"}, "gas": "nitrogen"}
}'
# 返回 data.jobId；GET /api/v1/job/<jobId>/status 查看进度（status 4 为成功）
```

以 `--backend ros2` 启动时也可以直接发 ROS Action：

```bash
ros2 action send_goal /devices/ReactorX/EvacuateAndRefillProtocol unilabos_msgs/action/EvacuateAndRefill "{vessel: reactor, gas: N2, repeats: 2}"
```

两种方式都会触发工作站编译协议，再依次调用真空泵、气源和电磁阀等虚拟设备执行抽真空和充气。运行期间可在
`http://localhost:8002/api/docs` 查看当前微后端提供的状态与遥测接口。
