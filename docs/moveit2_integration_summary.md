# Uni-Lab-OS MoveIt2 集成架构总结

## 概览

Uni-Lab-OS 通过三个核心文件实现了对 MoveIt2（ROS 2 运动规划框架）的深度集成，形成了从**底层运动规划接口** → **业务逻辑封装** → **场景构建与启动**的完整链路。

```
┌──────────────────────────────────────────────────────────────────────┐
│                    resource_visalization.py                         │
│         场景构建层：URDF/SRDF 生成、MoveIt2 节点启动                   │
│   ros2_control_node / move_group / robot_state_publisher / rviz2   │
└────────────────────────────┬─────────────────────────────────────────┘
                             │ 提供 MoveIt2 运行环境（Planning Scene、控制器）
                             ▼
┌──────────────────────────────────────────────────────────────────────┐
│                       moveit_interface.py                           │
│         业务逻辑层：pick_and_place、set_position、set_status          │
│         管理多个 MoveGroup，提供 FK/IK 计算、资源 TF 更新              │
└────────────────────────────┬─────────────────────────────────────────┘
                             │ 调用 MoveIt2 Python API
                             ▼
┌──────────────────────────────────────────────────────────────────────┐
│                          moveit2.py                                 │
│          底层接口层：MoveIt2 ROS 2 Python 客户端实现                   │
│  Action Client / Service Client / Collision Scene / 轨迹执行         │
└──────────────────────────────────────────────────────────────────────┘
```

---

## 1. `moveit2.py` — MoveIt2 底层 Python 接口

**路径**: `unilabos/devices/ros_dev/moveit2.py`  
**行数**: ~2443 行  
**角色**: 对 MoveIt2 ROS 2 接口的完整 Python 封装，是整个运动控制的基础。

### 1.1 核心类：`MoveIt2`

对 MoveIt2 框架的 ROS 2 通信协议进行了全面的 Python 封装，提供了规划、执行、运动学计算和碰撞管理的统一接口。

#### 1.1.1 ROS 2 通信拓扑

`MoveIt2` 类在初始化时创建了丰富的 ROS 2 通信端点：

| 类型 | 名称 | 用途 |
|------|------|------|
| **Subscriber** | `/joint_states` | 实时获取关节状态（BEST_EFFORT QoS） |
| **Action Client** | `/move_action` | 通过 MoveGroup Action 一体化规划+执行 |
| **Action Client** | `/execute_trajectory` | 独立的轨迹执行 |
| **Service Client** | `/plan_kinematic_path` | 关节空间/笛卡尔空间运动规划 |
| **Service Client** | `/compute_cartesian_path` | 笛卡尔路径规划（直线插补） |
| **Service Client** | `/compute_fk` | 正运动学计算 |
| **Service Client** | `/compute_ik` | 逆运动学计算 |
| **Service Client** | `/get_planning_scene` | 获取当前规划场景 |
| **Service Client** | `/apply_planning_scene` | 应用修改后的规划场景 |
| **Publisher** | `/collision_object` | 发布碰撞物体 |
| **Publisher** | `/attached_collision_object` | 发布附着碰撞物体 |
| **Publisher** | `/trajectory_execution_event` | 发送轨迹取消指令 |

#### 1.1.2 运动规划与执行

提供两种执行模式：

- **MoveGroup Action 模式** (`use_move_group_action=True`)：规划和执行合并在一个 Action 调用中完成，由 MoveIt2 内部管理整个流程。
- **Plan + Execute 模式**：先通过 Service 调用进行路径规划（`/plan_kinematic_path` 或 `/compute_cartesian_path`），再通过 ExecuteTrajectory Action 执行。

关键方法：

| 方法 | 说明 |
|------|------|
| `move_to_pose()` | 移动到目标位姿（位置 + 四元数姿态），支持笛卡尔直线规划 |
| `move_to_configuration()` | 移动到目标关节配置 |
| `plan()` / `plan_async()` | 异步路径规划，返回 `JointTrajectory` |
| `execute()` | 执行规划好的轨迹 |
| `wait_until_executed()` | 阻塞等待运动完成，返回成功/失败 |
| `cancel_execution()` | 取消当前运动 |

#### 1.1.3 目标约束系统

支持多层次的目标设定：

- **位置约束** (`set_position_goal`): 以球形区域定义目标位置的容差
- **姿态约束** (`set_orientation_goal`): 支持 Euler 角和旋转向量两种参数化方式
- **关节约束** (`set_joint_goal`): 直接指定目标关节角度
- **复合约束** (`set_pose_goal`): 位置 + 姿态的组合
- **路径约束** (`set_path_joint_constraint`, `set_path_position_constraint`, `set_path_orientation_constraint`): 对整条运动路径施加约束
- **多目标组** (`create_new_goal_constraint`): 支持同时设置多组目标约束

#### 1.1.4 运动学服务

- **正运动学 (FK)**: `compute_fk()` / `compute_fk_async()` — 给定关节角求末端位姿
- **逆运动学 (IK)**: `compute_ik()` / `compute_ik_async()` — 给定目标位姿求关节角，支持传入约束和避碰选项

#### 1.1.5 碰撞场景管理

提供完整的 Planning Scene 管理接口：

| 方法 | 说明 |
|------|------|
| `add_collision_box()` | 添加长方体碰撞体 |
| `add_collision_sphere()` | 添加球形碰撞体 |
| `add_collision_cylinder()` | 添加圆柱碰撞体 |
| `add_collision_cone()` | 添加锥形碰撞体 |
| `add_collision_mesh()` | 添加三角网格碰撞体（依赖 trimesh） |
| `move_collision()` | 移动已有碰撞体 |
| `remove_collision_object()` | 移除碰撞体 |
| `attach_collision_object()` | 将碰撞体附着到机器人 link 上 |
| `detach_collision_object()` | 从机器人上分离碰撞体 |
| `allow_collisions()` | 修改 Allowed Collision Matrix，允许/禁止特定碰撞 |
| `clear_all_collision_objects()` | 清空所有碰撞物体 |

#### 1.1.6 状态管理

通过 `MoveIt2State` 枚举和线程锁实现并发安全的状态管理：

- `IDLE`: 空闲
- `REQUESTING`: 已发送运动请求，等待接受
- `EXECUTING`: 正在执行轨迹

配合 `ignore_new_calls_while_executing` 标志，防止运动冲突。

#### 1.1.7 辅助工具函数

文件末尾提供模块级工具函数：

- `init_joint_state()`: 构造 `JointState` 消息
- `init_execute_trajectory_goal()`: 构造 `ExecuteTrajectory.Goal` 消息
- `init_dummy_joint_trajectory_from_state()`: 构造用于重置控制器的虚拟轨迹

---

## 2. `moveit_interface.py` — 业务逻辑封装层

**路径**: `unilabos/devices/ros_dev/moveit_interface.py`  
**行数**: 385 行  
**角色**: 将 `MoveIt2` 底层接口封装为实验室场景中可用的高级操作（pick/place、状态切换、资源管理）。

### 2.1 核心类：`MoveitInterface`

`MoveitInterface` 是连接实验室业务逻辑与 MoveIt2 运动规划的桥梁。它不直接继承 `MoveIt2`，而是通过**组合**的方式管理多个 `MoveIt2` 实例（每个 MoveGroup 一个），并在其上构建抓取放置、状态切换等实验室级操作。

---

#### 2.1.1 类属性与实例属性

```python
class MoveitInterface:
    _ros_node: BaseROS2DeviceNode    # ROS 2 节点引用（post_init 注入）
    tf_buffer: Buffer                # TF2 坐标变换缓冲区
    tf_listener: TransformListener   # TF2 变换监听器
```

**构造函数参数：**

| 参数 | 类型 | 说明 |
|------|------|------|
| `moveit_type` | `str` | 设备类型标识，用于定位 `device_mesh/devices/{moveit_type}/config/move_group.json` 配置文件 |
| `joint_poses` | `dict` | 预定义关节位姿字典，结构为 `{move_group: {status_name: [joint_values...]}}` |
| `rotation` | `optional` | 设备旋转参数（预留） |
| `device_config` | `optional` | 设备自定义配置 |

**实例属性：**

| 属性 | 类型 | 初始值 | 说明 |
|------|------|--------|------|
| `data_config` | `dict` | 从 JSON 加载 | Move Group 配置，结构为 `{group_name: {base_link_name, end_effector_name, joint_names}}` |
| `arm_move_flag` | `bool` | `False` | 机械臂运动标志（预留） |
| `move_option` | `list` | `["pick", "place", "side_pick", "side_place"]` | 支持的抓取/放置动作类型 |
| `joint_poses` | `dict` | 构造函数传入 | 预定义关节位姿查找表 |
| `cartesian_flag` | `bool` | `False` | 当前是否使用笛卡尔直线规划（在 `pick_and_place` 执行过程中动态切换） |
| `mesh_group` | `list` | `["reactor", "sample", "beaker"]` | 碰撞网格分组类别 |
| `moveit2` | `dict` | `{}` | MoveIt2 实例字典，键为 Move Group 名称 |
| `resource_action` | `str/None` | `None` | 发现的 `tf_update` Action Server 名称 |
| `resource_client` | `ActionClient/None` | `None` | 用于发送资源 TF 更新请求的 Action Client |
| `resource_action_ok` | `bool` | `False` | `tf_update` Action Server 是否就绪 |

---

#### 2.1.2 初始化流程：`__init__` + `post_init`

采用**两阶段初始化**设计：

**阶段一：`__init__`（纯数据初始化，不依赖 ROS 2）**

```
__init__(moveit_type, joint_poses, ...)
    │
    ├── 加载 move_group.json 配置文件
    │   路径: device_mesh/devices/{moveit_type}/config/move_group.json
    │   内容: { "arm": { "base_link_name": "base_link",
    │                     "end_effector_name": "tool0",
    │                     "joint_names": ["joint1", "joint2", ...] } }
    │
    ├── 初始化状态标志 (arm_move_flag, cartesian_flag, resource_action_ok)
    └── 记录 joint_poses 预定义位姿
```

**阶段二：`post_init(ros_node)`（ROS 2 依赖初始化）**

```
post_init(ros_node)
    │
    ├── 保存 ROS 节点引用 → self._ros_node
    │
    ├── 创建 TF2 基础设施
    │   ├── Buffer() → self.tf_buffer
    │   └── TransformListener(buffer, node) → self.tf_listener
    │
    ├── 遍历 data_config 中的每个 Move Group
    │   │
    │   │  对于每个 {move_group, config}:
    │   │
    │   ├── 生成带设备前缀的名称
    │   │   ├── base_link_name  = "{device_id}_{config.base_link_name}"
    │   │   ├── end_effector    = "{device_id}_{config.end_effector_name}"
    │   │   └── joint_names     = ["{device_id}_{name}" for name in config.joint_names]
    │   │
    │   └── 创建 MoveIt2 实例
    │       self.moveit2[move_group] = MoveIt2(
    │           node=ros_node,
    │           joint_names=...,
    │           base_link_name=...,
    │           end_effector_name=...,
    │           group_name="{device_id}_{move_group}",    ← MoveIt2 Planning Group 名
    │           callback_group=ros_node.callback_group,   ← 共享回调组
    │           use_move_group_action=True,                ← 使用 MoveGroup Action 模式
    │           ignore_new_calls_while_executing=True      ← 防止运动冲突
    │       )
    │       .allowed_planning_time = 3.0                  ← 规划超时 3 秒
    │
    └── 创建定时器 (1秒间隔)
        → wait_for_resource_action()                      ← 异步等待 tf_update Action Server
```

**设备名前缀机制**：所有关节名、link 名、group 名都加上 `{device_id}_` 前缀。这是多设备共存的关键——当多台机械臂在同一 ROS 2 网络中运行时，前缀保证名称唯一，MoveIt2 能正确区分不同设备的规划组。

---

#### 2.1.3 资源 TF 更新服务发现

`MoveitInterface` 需要在运行时动态更新实验室资源（如烧杯、样品瓶）的 TF 父链接——当机械臂抓取物体时，物体的 TF 从 `world` 切换到末端执行器；放置时切换回 `world`。

```
wait_for_resource_action() [定时器回调，1秒间隔]
    │
    ├── 若 resource_action_ok 已为 True → 直接返回
    │
    ├── 轮询 check_tf_update_actions()
    │   │
    │   ├── 遍历所有 ROS 2 Topic
    │   ├── 查找 topic_type == "action_msgs/msg/GoalStatusArray" 的 Topic
    │   ├── 从 Topic 名称中提取 Action 名（去掉 "/_action/status" 后缀）
    │   └── 检查最后一段是否为 "tf_update" → 返回 Action 名称
    │
    ├── 创建 ActionClient(SendCmd, action_name)
    ├── 等待 Action Server 就绪 (timeout=5s，循环等待)
    └── 设置 resource_action_ok = True
```

**为什么用 Topic 发现而非硬编码？**  
`tf_update` Action Server 由 `resource_mesh_manager` 节点提供，其命名空间可能随部署配置变化。通过 Topic 自动发现机制，`MoveitInterface` 能自适应不同的部署环境。

---

#### 2.1.4 底层运动方法：`moveit_task`

```python
def moveit_task(self, move_group, position, quaternion,
                speed=1, retry=10, cartesian=False,
                target_link=None, offsets=[0,0,0])
```

这是笛卡尔空间运动的核心封装，所有高级方法最终都通过它调用 `MoveIt2.move_to_pose()`。

**执行流程：**

```
moveit_task(move_group, position, quaternion, speed, retry, ...)
    │
    ├── 速度限幅: speed_ = clamp(speed, 0.1, 1.0)
    ├── 设置 MoveIt2 速度:
    │   ├── moveit2[group].max_velocity     = speed_
    │   └── moveit2[group].max_acceleration = speed_
    │
    ├── 计算最终位置: pose_result = position + offsets (逐元素相加)
    │
    └── 重试循环 (最多 retry+1 次):
        │
        ├── moveit2[group].move_to_pose(
        │       target_link=target_link,       ← 可指定非默认末端 link
        │       position=pose_result,           ← 目标 [x, y, z]
        │       quat_xyzw=quaternion,           ← 目标 [qx, qy, qz, qw]
        │       cartesian=cartesian,            ← 笛卡尔直线 or 自由空间
        │       cartesian_max_step=0.01,        ← 笛卡尔插补步长 1cm
        │       weight_position=1.0             ← 位置约束权重
        │   )
        │
        ├── re_ = moveit2[group].wait_until_executed()  ← 阻塞等待
        │
        └── 若 re_ 为 True → 返回成功; 否则 retry -= 1 继续
```

**参数说明：**

| 参数 | 说明 | MoveIt2 对应 |
|------|------|-------------|
| `position` | 目标位置 `[x, y, z]`（米） | `move_to_pose(position=...)` |
| `quaternion` | 目标姿态四元数 `[x, y, z, w]` | `move_to_pose(quat_xyzw=...)` |
| `speed` | 速度因子 0.1~1.0，同时控制速度和加速度 | `max_velocity` + `max_acceleration` |
| `retry` | 规划失败时的最大重试次数 | N/A（应用层重试） |
| `cartesian` | 是否使用笛卡尔直线规划 | `move_to_pose(cartesian=...)` |
| `target_link` | 目标 link（默认末端执行器） | `move_to_pose(target_link=...)` |
| `offsets` | 位置偏移量 `[dx, dy, dz]` | 叠加到 `position` |

---

#### 2.1.5 底层运动方法：`moveit_joint_task`

```python
def moveit_joint_task(self, move_group, joint_positions,
                      joint_names=None, speed=1, retry=10)
```

关节空间运动的核心封装，调用 `MoveIt2.move_to_configuration()`。

**执行流程：**

```
moveit_joint_task(move_group, joint_positions, joint_names, speed, retry)
    │
    ├── 关节角度转 float: joint_positions_ = [float(x) for x in joint_positions]
    ├── 速度限幅: speed_ = clamp(speed, 0.1, 1.0)
    ├── 设置 MoveIt2 速度
    │
    └── 重试循环:
        │
        ├── moveit2[group].move_to_configuration(
        │       joint_positions=joint_positions_,
        │       joint_names=joint_names        ← None 时使用 MoveIt2 默认关节名
        │   )
        │
        ├── re_ = moveit2[group].wait_until_executed()
        │
        ├── 打印 FK 结果 (调试用):
        │   compute_fk(joint_positions) → 显示对应的末端位姿
        │
        └── 若成功 → 返回; 否则 retry -= 1
```

**与 `moveit_task` 的区别：**

- `moveit_task`：目标是笛卡尔空间位姿（位置 + 姿态），MoveIt2 自动进行 IK 求解
- `moveit_joint_task`：目标直接是关节角度，无需 IK 计算，确定性更高
- 每次循环后调用 `compute_fk` 输出当前末端位姿，便于调试

---

#### 2.1.6 资源 TF 管理：`resource_manager`

```python
def resource_manager(self, resource, parent_link)
```

通过 `SendCmd` Action 向 `tf_update` 服务发送 TF 父链接更新请求。

```
resource_manager("beaker_1", "tool0")
    │
    ├── 构造 SendCmd.Goal:
    │   goal.command = '{"beaker_1": "tool0"}'    ← JSON 格式: {资源名: 新父link}
    │
    └── resource_client.send_goal(goal)           ← 异步发送，不等待结果
```

**在 pick/place 流程中的角色：**

- **pick 时**：`resource_manager(resource, end_effector_name)` — 资源跟随末端执行器
- **pick 且有 target 时**：`resource_manager(resource, target)` — 资源挂到指定 link
- **place 时**：`resource_manager(resource, "world")` — 资源释放到世界坐标系

---

#### 2.1.7 直接位姿控制：`set_position`

```python
def set_position(self, command: str)
```

最简单的运动接口，解析 JSON 指令后直接委托给 `moveit_task`。

**JSON 指令格式：**

```json
{
    "position": [0.3, 0.0, 0.5],
    "quaternion": [0.0, 0.0, 0.0, 1.0],
    "move_group": "arm",
    "speed": 0.5,
    "retry": 10
}
```

**调用链：**

```
set_position(command_json)
    │
    ├── JSON 解析 (替换单引号为双引号)
    └── moveit_task(**cmd_dict)
        └── MoveIt2.move_to_pose(...)
```

---

#### 2.1.8 预定义状态切换：`set_status`

```python
def set_status(self, command: str)
```

将机械臂移动到预定义的关节配置（如 home 位、准备位等），关节角度从 `self.joint_poses` 查找表中获取。

**JSON 指令格式：**

```json
{
    "status": "home",
    "move_group": "arm",
    "speed": 0.8,
    "retry": 5
}
```

**调用链：**

```
set_status(command_json)
    │
    ├── JSON 解析
    ├── 查找预定义关节角: joint_poses[move_group][status]
    │   例: joint_poses["arm"]["home"] → [0.0, -1.57, 1.57, 0.0, 0.0, 0.0]
    │
    └── moveit_joint_task(move_group, joint_positions, speed, retry)
        └── MoveIt2.move_to_configuration(...)
```

**`joint_poses` 查找表结构：**

```python
{
    "arm": {
        "home":    [0.0, -1.57, 1.57, 0.0, 0.0, 0.0],
        "ready":   [0.0, -0.78, 1.57, 0.0, 0.78, 0.0],
        "pick_A1": [0.5, -1.2, 1.0, 0.0, 0.5, 0.3],
        ...
    },
    "gripper": {
        "open":  [0.04],
        "close": [0.0],
        ...
    }
}
```

---

#### 2.1.9 核心方法详解：`pick_and_place`

```python
def pick_and_place(
    self,
    option: str,
    move_group: str,
    status: str,
    resource: Optional[str] = None,
    x_distance: Optional[float] = None,
    y_distance: Optional[float] = None,
    lift_height: Optional[float] = None,
    retry: Optional[int] = None,
    speed: Optional[float] = None,
    target: Optional[str] = None,
    constraints: Optional[Sequence[float]] = None,
) -> None:
```

这是 `MoveitInterface` 最复杂的方法，实现了完整的抓取-放置工作流。它动态构建一个**有序函数列表** (`function_list`)，然后顺序执行。

**破坏性变更（注册表 / 客户端）**：`pick_and_place` 在注册表中由 **SendCmd**（单字段 `command` JSON 字符串）改为 **UniLabJsonCommand**，goal 为与上表同名的**结构化字段**。云端与其它调用方需按 schema 提交 goal，而不再把整段 JSON 塞进 `command`。

**动作 goal 示例（字段与旧版 JSON 一致，现为结构化 goal）：**

```json
{
    "option": "pick",
    "move_group": "arm",
    "status": "pick_station_A",
    "resource": "beaker_1",
    "target": "custom_link",
    "lift_height": 0.05,
    "x_distance": 0.1,
    "y_distance": 0.0,
    "speed": 0.5,
    "retry": 10,
    "constraints": [0, 0, 0, 0.5, 0, 0]
}
```

##### 阶段 1：参数与动作类型判定

```
pick_and_place(option, move_group, status, ...)
    │
    ├── 校验 option ∈ move_option，否则直接 return
    ├── 动作类型判定:
    │   move_option = ["pick", "place", "side_pick", "side_place"]
    │                    0        1         2            3
    │   option_index = move_option.index(option)
    │   place_flag   = option_index % 2    ← 0=pick类, 1=place类
    │
    ├── 提取运动参数:
    │   config = { move_group }；若 speed/retry 非 None 则写入 config
    │
    └── 获取目标关节位姿:
        joint_positions_ = joint_poses[move_group][status]
```

##### 阶段 2：构建资源 TF 更新动作

```
根据 place_flag 决定资源 TF 操作:

    if pick 类 (place_flag == 0):
        if target is not None:
            function_list += [resource_manager(resource, target)]       ← 挂到自定义 link
        else:
            function_list += [resource_manager(resource, end_effector)] ← 挂到末端执行器
    
    if place 类 (place_flag == 1):
        function_list += [resource_manager(resource, "world")]         ← 释放到世界坐标
```

##### 阶段 3：构建关节约束

```
if constraints is not None:
    for i, tolerance in enumerate(constraints):
        if tolerance > 0:
            JointConstraint(
                joint_name = moveit2[group].joint_names[i],
                position   = joint_positions_[i],    ← 约束中心 = 目标关节角
                tolerance_above = tolerance,
                tolerance_below = tolerance,
                weight = 1.0
            )
```

约束的作用：限制 IK 求解的搜索空间，确保机械臂在抬升/移动过程中保持特定关节（如肘关节）在安全范围内。

##### 阶段 4A：有 `lift_height` 的完整流程

这是最复杂的场景，涉及 FK/IK 计算和多段运动拼接：

```
if lift_height is not None:
    │
    ├── Step 1: FK 计算 → 获取目标关节配置对应的末端位姿
    │   retval = compute_fk(joint_positions_)   ← 可能需要重试
    │   pose      = [retval.position.x, .y, .z]
    │   quaternion = [retval.orientation.x, .y, .z, .w]
    │
    ├── Step 2: 构建"下降到目标点"动作
    │   function_list = [moveit_task(position=pose, ...)] + function_list
    │   注：此时 function_list 已包含 resource_manager，它被插入到中间
    │
    ├── Step 3: 构建"从目标点抬升"动作
    │   pose[2] += lift_height                 ← Z 轴抬升
    │   function_list += [moveit_task(position=pose_lifted, ...)]
    │
    ├── Step 4 (可选): 水平移动
    │   if x_distance is not None:
    │       deep_pose = copy(pose_lifted)
    │       deep_pose[0] += x_distance
    │       function_list = [moveit_task(pose_lifted)] + function_list
    │       function_list += [moveit_task(deep_pose)]
    │   elif y_distance is not None:
    │       类似处理 Y 方向
    │
    ├── Step 5: IK 预计算 → 将末端位姿转换为安全的关节配置
    │   retval_ik = compute_ik(
    │       position = end_pose,               ← 最终抬升/移动后的位姿
    │       quat_xyzw = quaternion,
    │       constraints = Constraints(joint_constraints=constraints)
    │   )
    │   position_ = 从 IK 结果提取各关节角度
    │
    └── Step 6: 构建"关节空间移动到起始位"动作
        function_list = [moveit_joint_task(position_)] + function_list
```

##### 阶段 4B：无 `lift_height` 的简单流程

```
else (lift_height is None):
    │
    └── 直接关节运动到目标位姿
        function_list = [moveit_joint_task(joint_positions_)] + function_list
```

##### 阶段 5：顺序执行动作列表

```
for i, func in enumerate(function_list):
    │
    ├── 设置规划模式:
    │   i == 0: cartesian_flag = False    ← 第一步用自由空间规划（大范围移动）
    │   i >  0: cartesian_flag = True     ← 后续用笛卡尔直线规划（精确控制）
    │
    ├── re = func()                       ← 执行动作
    │
    └── if not re:
        return（无返回值，不构造 SendCmd.Result）← 任一步骤失败即中止
```

##### 完整 pick 流程示例（含 lift_height + x_distance）

假设指令为：pick beaker_1 from station_A, lift 5cm, move 10cm in X

```
最终 function_list 执行顺序:
┌─────────────────────────────────────────────────────────────────────┐
│ Step 0: moveit_joint_task → IK 求解的关节角                         │
│         [cartesian=False, 自由空间规划]                              │
│         机械臂从当前位置移动到抬升位                                   │
├─────────────────────────────────────────────────────────────────────┤
│ Step 1: moveit_task → 水平移动后的抬升位 (pose_lifted)               │
│         [cartesian=True, 笛卡尔直线]                                │
│         对齐到 station_A 正上方                                      │
├─────────────────────────────────────────────────────────────────────┤
│ Step 2: moveit_task → station_A 目标位姿 (FK 计算的位置)             │
│         [cartesian=True, 笛卡尔直线]                                │
│         末端执行器下降到目标点                                        │
├─────────────────────────────────────────────────────────────────────┤
│ Step 3: resource_manager("beaker_1", end_effector)                 │
│         资源 TF 附着到末端执行器                                      │
├─────────────────────────────────────────────────────────────────────┤
│ Step 4: moveit_task → 抬升位 (z + lift_height)                     │
│         [cartesian=True, 笛卡尔直线]                                │
│         抓取后垂直抬升                                               │
├─────────────────────────────────────────────────────────────────────┤
│ Step 5: moveit_task → 水平偏移位 (x + x_distance)                  │
│         [cartesian=True, 笛卡尔直线]                                │
│         抬升后水平移开，避免碰撞                                      │
└─────────────────────────────────────────────────────────────────────┘
```

##### 完整 place 流程（同结构，反向操作）

与 pick 相同的运动轨迹，但 `resource_manager` 调用变为 `resource_manager(resource, "world")`——在 Step 3 处将资源从末端执行器释放到世界坐标系。

---

#### 2.1.10 MoveIt2 API 调用汇总

`MoveitInterface` 使用的 `MoveIt2` 接口及其调用位置：

| MoveIt2 方法 | 调用位置 | 用途 |
|-------------|---------|------|
| `MoveIt2(...)` 构造 | `post_init` L51-60 | 为每个 MoveGroup 创建实例 |
| `.allowed_planning_time = 3.0` | `post_init` L61 | 设置规划超时时间 |
| `.max_velocity = speed_` | `moveit_task` L119, `moveit_joint_task` L151 | 动态设置最大速度缩放因子 |
| `.max_acceleration = speed_` | `moveit_task` L120, `moveit_joint_task` L152 | 动态设置最大加速度缩放因子 |
| `.move_to_pose(...)` | `moveit_task` L129-137 | 笛卡尔空间运动规划与执行 |
| `.wait_until_executed()` | `moveit_task` L138, `moveit_joint_task` L157 | 阻塞等待运动完成 |
| `.move_to_configuration(...)` | `moveit_joint_task` L156 | 关节空间运动规划与执行 |
| `.compute_fk(...)` | `pick_and_place`, `moveit_joint_task` | 正运动学：关节角 → 末端位姿 |
| `.compute_ik(...)` | `pick_and_place` | 逆运动学：末端位姿 → 关节角（含约束） |
| `.end_effector_name` | `pick_and_place` | 获取末端执行器 link 名 |
| `.joint_names` | `pick_and_place` | 获取关节名列表 |

---

#### 2.1.11 错误处理策略

| 场景 | 处理方式 |
|------|---------|
| FK 计算失败 | 最多重试 `retry` 次（每次间隔 0.1s），超时则提前 `return`（无返回值） |
| IK 计算失败 | 同上 |
| 运动规划失败 | 在 `moveit_task` / `moveit_joint_task` 中最多重试 `retry+1` 次 |
| 动作序列中任一步失败 | `pick_and_place` 立即中止并 `return`（不返回 `SendCmd.Result`） |
| 未知异常 | `pick_and_place` 捕获 Exception，打印并重置 `cartesian_flag`（`set_status` 仍返回 SendCmd.Result） |

---

#### 2.1.12 `cartesian_flag` 状态机

`cartesian_flag` 控制 `moveit_task` 中是否使用笛卡尔直线规划。在 `pick_and_place` 执行过程中，它被动态切换：

```
执行前: cartesian_flag = (上次残留状态)

动作序列执行:
    Step 0: cartesian_flag ← False   (自由空间规划，适合大范围移动)
    Step 1: cartesian_flag ← True    (笛卡尔直线，适合精确操作)
    Step 2: cartesian_flag ← True
    ...
    Step N: cartesian_flag ← True

异常时: cartesian_flag ← False      (重置，防止残留影响后续操作)
```

这种设计的考量：第一步通常是从安全位移动到工作区附近（距离远、可能需要绕障），使用自由空间规划更灵活；后续步骤是在工作区内的精确操作（下降、抬升、平移），笛卡尔直线规划确保路径可预测。

---

#### 2.1.13 数据流总览

```
外部系统 (base_device_node)
    │
    │  set_position/set_status: JSON 指令字符串（SendCmd.command）
    │  pick_and_place: UniLabJsonCommand 结构化 goal → Python 关键字参数
    ▼
┌── MoveitInterface ──────────────────────────────────────────────────┐
│                                                                     │
│  set_position(cmd) ──→ moveit_task() ──→ MoveIt2.move_to_pose()    │
│                                                                     │
│  set_status(cmd) ──→ moveit_joint_task() ──→ MoveIt2.move_to_config│
│                                                                     │
│  pick_and_place(option, move_group, status, ...)                    │
│    │                                                                │
│    ├─ MoveIt2.compute_fk() ─── /compute_fk service ──→ move_group  │
│    ├─ MoveIt2.compute_ik() ─── /compute_ik service ──→ move_group  │
│    ├─ moveit_task()        ─── /move_action         ──→ move_group  │
│    ├─ moveit_joint_task()  ─── /move_action         ──→ move_group  │
│    └─ resource_manager()   ─── SendCmd Action       ──→ tf_update   │
│                                                                     │
│  内部状态:                                                           │
│    joint_poses  ← 预定义位姿查找表                                    │
│    moveit2{}    ← MoveIt2 实例池 (per MoveGroup)                     │
│    tf_buffer    ← TF2 坐标变换缓存                                    │
│    cartesian_flag ← 规划模式状态机                                    │
│                                                                     │
└─────────────────────────────────────────────────────────────────────┘
```

---

## 3. `resource_visalization.py` — 场景构建与 MoveIt2 启动

**路径**: `unilabos/device_mesh/resource_visalization.py`  
**行数**: 429 行  
**角色**: 根据实验室设备/资源配置，动态生成 URDF/SRDF，并启动 MoveIt2 所需的全部 ROS 2 节点。

### 3.1 核心类：`ResourceVisualization`

#### 3.1.1 模型加载与 URDF 生成

初始化阶段完成以下工作：

1. **遍历设备注册表**：区分 `resource`（静态资源如 plate/deck）和 `device`（可动设备如机械臂）
2. **Resource 类型**：记录 mesh 路径和 TF 变换信息，后续用于碰撞场景
3. **Device 类型**：通过 `xacro` 宏注入动态参数：
   - 设备名前缀（`device_name`）
   - 位置/旋转（从配置中提取）
   - 自定义设备参数（`device_config`）
4. **MoveIt 设备**（`class` 包含 `moveit.`）：额外加载：
   - `ros2_control` xacro 宏 → 生成 `ros2_control` 硬件接口描述
   - SRDF xacro 宏 → 生成运动学语义描述（move group 定义、自碰撞矩阵等）

#### 3.1.2 MoveIt2 配置初始化 (`moveit_init`)

对每个 MoveIt 设备，加载并合并以下配置文件（均加上设备名前缀）：

| 配置文件 | 生成目标 | 作用 |
|----------|----------|------|
| `ros2_controllers.yaml` | `ros2_controllers_yaml` | 定义 ros2_control 控制器（JointTrajectoryController 等） |
| `moveit_controllers.yaml` | `moveit_controllers_yaml` | 定义 MoveIt 控制器映射 |
| `kinematics.yaml` | `moveit_nodes_kinematics` | 定义运动学求解器参数 |

所有关节名和控制器名都加上设备 ID 前缀，确保多设备共存时不冲突。

#### 3.1.3 ROS 2 Launch 节点启动

`create_launch_description()` 方法启动以下 ROS 2 节点：

| 节点 | 包 | 作用 |
|------|-----|------|
| `ros2_control_node` | `controller_manager` | ros2_control 硬件管理器，加载 URDF 和控制器配置 |
| `spawner` (per controller) | `controller_manager` | 激活各 JointTrajectoryController |
| `spawner` (joint_state_broadcaster) | `controller_manager` | 广播关节状态到 `/joint_states` |
| `robot_state_publisher` | `robot_state_publisher` | 根据 URDF 和关节状态发布 TF |
| `move_group` | `moveit_ros_move_group` | **MoveIt2 核心节点**，提供运动规划服务 |
| `rviz2` (可选) | `rviz2` | 3D 可视化 |

`move_group` 节点的参数配置包括：

- `robot_description`: 动态生成的 URDF
- `robot_description_semantic`: 动态生成的 SRDF
- `robot_description_kinematics`: 合并后的运动学配置
- `planning_pipelines`: 从 `moveit_configs_utils` 加载的规划器配置（默认 OMPL）
- `moveit_controllers_yaml`: 控制器映射配置
- `robot_description_planning`: 速度/加速度限制

---

## 4. 三个文件的协作关系

### 4.1 启动阶段

```
resource_visalization.py
    │
    ├── 1. 解析设备/资源配置 → 生成 URDF + SRDF
    ├── 2. 合并控制器/运动学配置（带设备名前缀）
    ├── 3. 启动 ros2_control_node + controller spawners
    ├── 4. 启动 robot_state_publisher（发布 TF）
    ├── 5. 启动 move_group（提供规划服务）
    └── 6. (可选) 启动 rviz2
```

### 4.2 运行阶段

```
外部调用（如 base_device_node）
    │
    ▼
moveit_interface.py
    │
    ├── pick_and_place() ──┬── compute_fk()    ──→ moveit2.py → /compute_fk service
    │                      ├── compute_ik()    ──→ moveit2.py → /compute_ik service
    │                      ├── move_to_pose()  ──→ moveit2.py → /move_action or /plan + /execute
    │                      ├── move_to_config()──→ moveit2.py → /move_action or /plan + /execute
    │                      └── resource_manager()→ SendCmd Action → tf_update
    │
    ├── set_position() ────→ moveit_task()     ──→ moveit2.py → move_to_pose()
    │
    └── set_status() ──────→ moveit_joint_task()→ moveit2.py → move_to_configuration()
```

### 4.3 MoveIt2 消息类型使用一览

| 消息/服务/Action | 文件 | 用途 |
|-----------------|------|------|
| `moveit_msgs/action/MoveGroup` | moveit2.py | 一体化规划+执行 |
| `moveit_msgs/action/ExecuteTrajectory` | moveit2.py | 独立轨迹执行 |
| `moveit_msgs/srv/GetMotionPlan` | moveit2.py | 运动规划 |
| `moveit_msgs/srv/GetCartesianPath` | moveit2.py | 笛卡尔路径规划 |
| `moveit_msgs/srv/GetPositionFK` | moveit2.py | 正运动学 |
| `moveit_msgs/srv/GetPositionIK` | moveit2.py | 逆运动学 |
| `moveit_msgs/srv/GetPlanningScene` | moveit2.py | 获取规划场景 |
| `moveit_msgs/srv/ApplyPlanningScene` | moveit2.py | 应用规划场景 |
| `moveit_msgs/msg/CollisionObject` | moveit2.py | 碰撞物体管理 |
| `moveit_msgs/msg/AttachedCollisionObject` | moveit2.py | 附着碰撞物体 |
| `moveit_msgs/msg/Constraints` | moveit2.py, moveit_interface.py | 目标/路径约束 |
| `moveit_msgs/msg/JointConstraint` | moveit2.py, moveit_interface.py | 关节约束 |
| `moveit_msgs/msg/PlanningScene` | moveit2.py | 规划场景 |

---

## 5. 注册表中的 `model` 字段与 3D 模型加载

### 5.1 `model` 字段概述

在 Uni-Lab-OS 的设备/资源注册表 YAML 中，`model` 字段是连接**注册表定义**与**3D 可视化系统**的关键。它告诉 `ResourceVisualization` 如何为该设备或资源加载 3D 模型。

以 `arm_slider` 为例：

```yaml
# unilabos/registry/devices/robot_arm.yaml (L355-358)
model:
  mesh: arm_slider
  path: https://uni-lab.oss-cn-zhangjiakou.aliyuncs.com/uni-lab/devices/arm_slider/macro_device.xacro
  type: device
```

**三个字段的作用：**

| 字段 | 值 | 说明 |
|------|-----|------|
| `mesh` | `arm_slider` | 模型文件夹名，对应 `device_mesh/devices/arm_slider/` 目录 |
| `path` | `https://...macro_device.xacro` | 模型文件的远程下载地址（OSS），用于首次部署时下载模型资源 |
| `type` | `device` | 模型类型标识，决定 `ResourceVisualization` 的处理逻辑 |

### 5.2 `type` 字段的两种取值

`model.type` 决定了 `ResourceVisualization` 如何加载和处理 3D 模型，有两条完全不同的路径：

#### `type: device` — 动态设备（如机械臂、龙门架）

```python
# resource_visalization.py L121-163
if model_config['type'] == 'device':
    # 1. 通过 xacro include 加载设备的 URDF 宏
    #    → device_mesh/devices/{mesh}/macro_device.xacro
    
    # 2. 调用 xacro 宏，注入参数：
    #    parent_link, mesh_path, device_name, x/y/z, rx/ry/r, device_config...
    
    # 3. 若设备 class 包含 "moveit."：
    #    → 额外加载 ros2_control xacro 和 SRDF xacro
    #    → 注册为 moveit_nodes，后续由 moveit_init() 加载控制器配置
```

**处理流程：**

```
注册表 model.mesh = "arm_slider"
    │
    ├── 加载 URDF:  device_mesh/devices/arm_slider/macro_device.xacro
    │   → 生成完整的 link/joint 运动链，嵌入到全局 URDF 中
    │
    ├── 注入位置参数:  x, y, z, rx, ry, r (从节点配置 position/rotation 中读取, 单位 mm→m)
    │
    ├── 注入设备参数:  device_config 中的键值对作为 xacro 参数
    │
    └── 若 class 含 "moveit.":
        ├── 加载 ros2_control:  config/macro.ros2_control.xacro
        ├── 加载 SRDF:          config/macro.srdf.xacro
        └── 记录到 moveit_nodes → moveit_init() 加载 controllers/kinematics
```

#### `type: resource` — 静态资源（如微孔板、试管架）

```python
# resource_visalization.py L111-120
if model_config['type'] == 'resource':
    # 只记录 mesh 文件路径和 TF 偏移，用于碰撞场景
    # 不注入到 URDF 运动链中
    resource_model[node_id] = {
        'mesh': f"device_mesh/resources/{mesh}",
        'mesh_tf': model_config['mesh_tf']
    }
```

Resource 类型的 `model` 结构更丰富，包含 TF 偏移和子物体：

```yaml
# 例: registry/resources/opentrons/plates.yaml
model:
  mesh: tecan_nested_tip_rack/meshes/plate.stl      # 主体 mesh（STL 文件）
  mesh_tf: [0.064, 0.043, 0, -1.5708]               # 位姿偏移 [x, y, z, rotation]
  children_mesh: generic_labware_tube_10_75/meshes/0_base.stl   # 子物体 mesh
  children_mesh_tf: [0.0018, 0.0018, 0, -1.5708]               # 子物体偏移
```

### 5.3 两种类型的对比

| 对比项 | `type: device` | `type: resource` |
|--------|---------------|-----------------|
| **模型格式** | xacro 宏（动态参数化 URDF） | STL 静态 mesh 文件 |
| **加载方式** | xacro include → 嵌入全局 URDF | 记录路径 → 后续作为碰撞体添加 |
| **位置来源** | 节点配置中的 position/rotation | `mesh_tf` 偏移数组 |
| **是否有关节** | 是（prismatic/revolute） | 否（纯静态） |
| **支持 MoveIt** | 是（通过 class 名中的 `moveit.` 触发） | 否 |
| **子物体** | 无（运动链本身定义了所有部件） | 可选 `children_mesh`（如管架中的管子） |
| **远程路径** | `path` 字段指向 OSS 下载地址 | 类似，`children_mesh_path` 指向子物体 |
| **存放目录** | `device_mesh/devices/{mesh}/` | `device_mesh/resources/{mesh}` |
| **实际示例** | arm_slider, toyo_xyz, elite_robot | 微孔板, tip rack, 试管架 |

### 5.4 `arm_slider` 注册表完整结构

`arm_slider` 的注册表键名 `robotic_arm.SCARA_with_slider.moveit.virtual` 本身就编码了重要信息：

```
robotic_arm           → 设备大类（机械臂）
  .SCARA_with_slider  → 具体型号（SCARA 构型 + 线性滑轨）
  .moveit             → ★ 标记为 MoveIt 设备（class 名包含 "moveit."）
  .virtual            → 仿真/虚拟设备
```

**class 名中包含 `moveit.` 的关键作用**：`ResourceVisualization` 在 L151 通过 `node['class'].find('moveit.') != -1` 判断是否需要加载 MoveIt 配置。这是 **MoveIt 设备与普通 device 的唯一判别条件**——即使两者的 `model.type` 都是 `device`。

注册表中的关键部分：

```yaml
robotic_arm.SCARA_with_slider.moveit.virtual:
  # 设备驱动类
  class:
    module: unilabos.devices.ros_dev.moveit_interface:MoveitInterface
    type: python
    action_value_mappings:
      pick_and_place: ...    # UniLabJsonCommand（结构化 goal，与 Python 签名一致）
      set_position: ...      # SendCmd Action
      set_status: ...        # SendCmd Action
      auto-moveit_task: ...  # 自动发现的方法（UniLabJsonCommand）
      auto-moveit_joint_task: ...
      auto-resource_manager: ...
      auto-post_init: ...
  
  # 初始化参数
  init_param_schema:
    config:
      properties:
        moveit_type: ...     # → 对应 device_mesh/devices/{moveit_type}/ 文件夹
        joint_poses: ...     # → 预定义关节位姿查找表
        rotation: ...
        device_config: ...
  
  # ★ 3D 模型定义
  model:
    mesh: arm_slider         # → device_mesh/devices/arm_slider/
    path: https://...        # → OSS 远程下载地址
    type: device             # → ResourceVisualization 按 device 逻辑加载
```

### 5.5 从注册表到 MoveIt2 的完整链路

```
注册表 YAML
│
│  model.mesh = "arm_slider"
│  model.type = "device"
│  class = "robotic_arm.SCARA_with_slider.moveit.virtual"
│                                         ^^^^^^
│                                    class 含 "moveit."
▼
ResourceVisualization.__init__()
│
├── model.type == "device"
│   └── xacro include: devices/arm_slider/macro_device.xacro → URDF
│
├── class 含 "moveit."
│   ├── xacro include: devices/arm_slider/config/macro.ros2_control.xacro → URDF
│   ├── xacro include: devices/arm_slider/config/macro.srdf.xacro → SRDF
│   └── moveit_nodes["device_id"] = "arm_slider"
│
▼
ResourceVisualization.moveit_init()
│
├── 加载 devices/arm_slider/config/ros2_controllers.yaml
├── 加载 devices/arm_slider/config/moveit_controllers.yaml
├── 加载 devices/arm_slider/config/kinematics.yaml
└── 合并到全局配置（带设备名前缀）
│
▼
ResourceVisualization.create_launch_description()
│
├── 启动 ros2_control_node（加载 URDF + 控制器配置）
├── 启动 controller spawners（激活 arm_controller、gripper_controller）
├── 启动 robot_state_publisher（发布 TF）
├── 启动 move_group（MoveIt2 核心，加载 SRDF + kinematics + planners）
└── (可选) 启动 rviz2
│
▼
MoveitInterface.post_init()
│
├── 读取 devices/arm_slider/config/move_group.json
├── 为 "arm" 组创建 MoveIt2 实例
└── 等待 tf_update Action Server 就绪
│
▼
运行时: pick_and_place / set_position / set_status
```

---

## 6. 设备模型文件夹结构：MoveIt 设备 vs 非 MoveIt 设备

`device_mesh/devices/` 下的每个子文件夹代表一种设备类型的 3D 模型和配置。根据设备是否需要 MoveIt2 运动规划，文件夹内容有**显著差异**。

### 5.1 非 MoveIt 设备示例：`slide_w140` / `hplc_station`

非 MoveIt 设备只需要 3D 可视化和简单的关节控制（由 `liquid_handler_joint_publisher` 等自定义节点直接控制），**不需要运动规划**。

```
slide_w140/                        hplc_station/
├── macro_device.xacro             ├── macro_device.xacro
├── joint_config.json              ├── joint_config.json
├── param_config.json              ├── param_config.json
└── meshes/                        └── meshes/
    └── *.STL                          └── *.STL
```

**仅 3 个配置文件：**

| 文件 | 作用 |
|------|------|
| `macro_device.xacro` | URDF 模型（xacro 宏），定义 link/joint/visual/collision |
| `joint_config.json` | 关节名与轴向信息，供自定义关节发布器使用 |
| `param_config.json` | 设备尺寸等可配参数（如轨道长度），注入到 xacro 宏参数中 |

**特点：**
- 没有 `config/` 子文件夹
- 没有 SRDF、ros2_control、MoveIt 控制器等配置
- 关节由应用层直接发布 `JointState`，不经过 ros2_control 和 MoveIt2

---

### 5.2 MoveIt 设备示例：`arm_slider`

MoveIt 设备需要完整的运动规划支持——从 ros2_control 硬件抽象到 MoveIt2 运动学求解和碰撞矩阵。

```
arm_slider/
├── macro_device.xacro                 ← URDF 模型（xacro 宏）
├── joint_limit.yaml                   ← 关节物理限制（effort/velocity/position）
├── meshes/                            ← 3D 网格文件
│   ├── arm_slideway.STL
│   ├── arm_base.STL
│   ├── arm_link_1.STL
│   ├── arm_link_2.STL
│   ├── arm_link_3.STL
│   ├── gripper_base.STL
│   ├── gripper_right.STL
│   └── gripper_left.STL
│
└── config/                            ← ★ MoveIt 设备独有的配置目录
    ├── macro.ros2_control.xacro       ← ros2_control 硬件接口定义
    ├── macro.srdf.xacro               ← SRDF 语义描述（Move Group + 碰撞矩阵）
    ├── move_group.json                ← Move Group 定义（供 MoveitInterface 使用）
    ├── ros2_controllers.yaml          ← ros2_control 控制器配置
    ├── moveit_controllers.yaml        ← MoveIt ↔ 控制器映射
    ├── kinematics.yaml                ← 运动学求解器配置
    ├── joint_limits.yaml              ← MoveIt 用关节限制（速度/加速度缩放）
    ├── initial_positions.yaml         ← 仿真初始关节位置
    ├── pilz_cartesian_limits.yaml     ← Pilz 笛卡尔限制
    └── moveit_planners.yaml           ← 规划器配置
```

**比非 MoveIt 设备多出 10 个配置文件**，全部位于 `config/` 子目录。

---

### 5.3 `arm_slider` 各文件详解

#### 5.3.1 `macro_device.xacro` — URDF 运动链定义

定义了 arm_slider 的完整运动链，包含 8 个 link 和 7 个 joint：

```
world
  └── [fixed] base_link_joint
      └── device_link
          └── [fixed] device_link_joint
              └── arm_slideway (底座滑轨, 有 visual + collision mesh)
                  └── [prismatic, X轴] arm_base_joint (滑轨平移)
                      └── arm_base (机械臂底座)
                          └── [prismatic, Z轴] arm_link_1_joint (升降)
                              └── arm_link_1
                                  └── [revolute, Z轴] arm_link_2_joint (旋转关节1)
                                      └── arm_link_2
                                          └── [revolute, Z轴] arm_link_3_joint (旋转关节2)
                                              └── arm_link_3
                                                  └── [revolute, Z轴] gripper_base_joint (夹爪旋转)
                                                      └── gripper_base (夹爪底座)
                                                          ├── [prismatic, X轴] gripper_right_joint
                                                          │   └── gripper_right
                                                          └── [prismatic, X轴, mimic] gripper_left_joint
                                                              └── gripper_left
```

**关键设计点：**

- **混合关节类型**：包含 prismatic（滑轨平移 + 升降 + 夹爪）和 revolute（旋转）关节
- **Mimic 关节**：`gripper_left_joint` 通过 `<mimic>` 标签跟随 `gripper_right_joint`，实现对称夹爪联动
- **参数化前缀**：所有 link/joint 名都带 `${station_name}${device_name}` 前缀，支持多实例
- **外部关节限制**：从 `joint_limit.yaml` 加载 effort/velocity/position 范围
- **完整物理属性**：每个 link 都有 `<inertial>`（质量、惯量矩阵）、`<visual>`（STL mesh）和 `<collision>`（碰撞体）

#### 5.3.2 `joint_limit.yaml` — 关节物理限制

定义每个关节的运动范围和动力学参数，被 `macro_device.xacro` 引用：

| 关节 | 类型 | 范围 | 说明 |
|------|------|------|------|
| `arm_base_joint` | prismatic | 0 ~ 1.5m | 滑轨水平行程 |
| `arm_link_1_joint` | prismatic | 0 ~ 0.6m | 升降行程 |
| `arm_link_2_joint` | revolute | -95° ~ 95° | 第一旋转关节 |
| `arm_link_3_joint` | revolute | -195° ~ 195° | 第二旋转关节 |
| `gripper_base_joint` | revolute | -95° ~ 95° | 夹爪旋转 |
| `gripper_right/left_joint` | prismatic | 0 ~ 0.03m | 夹爪开合 |

#### 5.3.3 `config/macro.ros2_control.xacro` — ros2_control 硬件接口

定义 ros2_control 硬件抽象层，将关节映射到控制接口：

- **硬件插件**：`mock_components/GenericSystem`（仿真模式，可替换为真实硬件驱动）
- **每个关节声明**：
  - `command_interface: position` — 位置控制模式
  - `state_interface: position` — 位置反馈（含 `initial_value` 从 `initial_positions.yaml` 加载）
  - `state_interface: velocity` — 速度反馈
- **6 个关节**：`arm_base_joint` ~ `gripper_right_joint`（`gripper_left_joint` 因为是 mimic 关节，不需要独立控制接口）

#### 5.3.4 `config/macro.srdf.xacro` — SRDF 语义描述

MoveIt2 的语义机器人描述，定义了：

**Move Groups（规划组）：**

| 组名 | 类型 | 内容 |
|------|------|------|
| `{device_name}arm` | chain | `arm_slideway` → `gripper_base`（5 DOF 运动链） |
| `{device_name}arm_gripper` | joint | `gripper_right_joint`（夹爪控制） |

**Disable Collisions（自碰撞矩阵）：**

22 条 `<disable_collisions>` 规则，标记不可能碰撞的 link 对（Adjacent / Never），减少碰撞检测计算量。例如：

- `arm_base` ↔ `arm_slideway`：Adjacent（相邻 link，必然接触）
- `arm_link_1` ↔ `arm_link_3`：Never（物理上不可能碰撞）
- `gripper_left` ↔ `gripper_right`：Never

#### 5.3.5 `config/move_group.json` — MoveitInterface 配置

供 `MoveitInterface.post_init()` 使用，定义每个 Move Group 的关节和端点：

```json
{
    "arm": {
        "joint_names": ["arm_base_joint", "arm_link_1_joint",
                        "arm_link_2_joint", "arm_link_3_joint",
                        "gripper_base_joint"],
        "base_link_name": "device_link",
        "end_effector_name": "gripper_base"
    }
}
```

`MoveitInterface` 读取此文件后，为 `"arm"` 组创建一个 `MoveIt2` 实例，自动加上设备名前缀。

#### 5.3.6 `config/ros2_controllers.yaml` — 控制器定义

定义两个 `JointTrajectoryController`：

| 控制器 | 控制的关节 | 说明 |
|--------|-----------|------|
| `arm_controller` | arm_base_joint ~ gripper_base_joint (5个) | 机械臂主体 |
| `gripper_controller` | gripper_right_joint (1个) | 夹爪 |

被 `resource_visalization.py` 的 `moveit_init()` 读取，加上设备名前缀后合并到全局 `ros2_controllers_yaml` 中。

#### 5.3.7 `config/moveit_controllers.yaml` — MoveIt ↔ 控制器映射

告诉 MoveIt2 的 `move_group` 节点如何将规划好的轨迹发送到 ros2_control 控制器：

- `arm_controller` → FollowJointTrajectory Action（5 个关节）
- `gripper_controller` → FollowJointTrajectory Action（1 个关节）

#### 5.3.8 `config/kinematics.yaml` — 运动学求解器

```yaml
arm:
  kinematics_solver: lma_kinematics_plugin/LMAKinematicsPlugin
  kinematics_solver_search_resolution: 0.005
  kinematics_solver_timeout: 0.005
```

使用 **LMA (Levenberg-Marquardt Algorithm)** 运动学求解器进行正/逆运动学计算。这是 MoveIt2 的通用 IK 求解器，适用于任意运动链拓扑。

#### 5.3.9 其他配置文件

| 文件 | 作用 |
|------|------|
| `initial_positions.yaml` | 仿真启动时各关节初始角度/位置（所有为 0，夹爪张开 0.03m） |
| `joint_limits.yaml` | MoveIt 层面的速度/加速度缩放限制（覆盖 URDF 中的值） |
| `pilz_cartesian_limits.yaml` | Pilz 工业运动规划器的笛卡尔速度/加速度限制 |
| `moveit_planners.yaml` | 可用规划器列表（`ompl_interface/OMPLPlanner`） |

---

### 5.4 对比：`toyo_xyz`（另一个 MoveIt 设备）

`toyo_xyz` 是一个三轴直线运动平台（XYZ 龙门），也是 MoveIt 设备。与 `arm_slider` 对比：

| 对比项 | `arm_slider` | `toyo_xyz` |
|--------|-------------|------------|
| **自由度** | 5 DOF (2 prismatic + 3 revolute) + 夹爪 | 3 DOF (3 prismatic) |
| **运动链** | 混合链（平移+旋转） | 纯直线链（全 prismatic） |
| **Move Group** | `arm` (5 joints) + `arm_gripper` (1 joint) | `toyo_xyz` (3 joints) |
| **末端执行器** | `gripper_base` | `slider3_link` |
| **独有文件** | `joint_limit.yaml` | `joint_config.json` + `param_config.json` |
| **xacro 参数** | 固定尺寸 | 可配长度 (`length1/2/3`) + mesh scale 缩放 |
| **IK 求解器** | LMA | LMA |

`toyo_xyz` 的额外特点：
- `param_config.json`：定义三轴行程和滑块尺寸，通过 xacro 参数动态缩放 STL 模型
- `joint_config.json`：简单的关节名→轴向映射，供非 MoveIt 的关节发布器使用
- `config/full_dev.urdf.xacro`：额外的完整 URDF 文件（独立调试用）

两者的 **MoveIt 配置文件结构完全一致**（SRDF、ros2_control、controllers、kinematics），说明 Uni-Lab-OS 的 MoveIt 设备遵循统一的模板。

---

### 5.5 MoveIt vs 非 MoveIt 设备文件对比总结

```
非 MoveIt 设备 (slide_w140)           MoveIt 设备 (arm_slider)
───────────────────────────           ───────────────────────────
macro_device.xacro          ✓         macro_device.xacro           ✓
joint_config.json           ✓         joint_limit.yaml             ✓
param_config.json           ✓         
                                      config/
                                      ├── macro.ros2_control.xacro ★ ros2_control 硬件接口
                                      ├── macro.srdf.xacro         ★ SRDF (Move Group + 碰撞)
                                      ├── move_group.json          ★ MoveitInterface 配置
                                      ├── ros2_controllers.yaml    ★ 控制器定义
                                      ├── moveit_controllers.yaml  ★ MoveIt↔控制器映射
                                      ├── kinematics.yaml          ★ IK 求解器配置
                                      ├── joint_limits.yaml        ★ MoveIt 关节限制
                                      ├── initial_positions.yaml   ★ 仿真初始状态
                                      ├── pilz_cartesian_limits.yaml ★ 笛卡尔限制
                                      └── moveit_planners.yaml     ★ 规划器列表

文件数: 3                             文件数: 12 (3 + 10 MoveIt 专用)
```

**核心区别：MoveIt 设备多出的 `config/` 目录下的 10 个文件，构成了 MoveIt2 运动规划所需的完整配置栈：**

1. **硬件层** (`macro.ros2_control.xacro`): 关节如何被控制
2. **语义层** (`macro.srdf.xacro`): 哪些关节组成规划组，哪些碰撞可以忽略
3. **规划层** (`kinematics.yaml`, `moveit_planners.yaml`, `pilz_cartesian_limits.yaml`): 如何求解和规划
4. **执行层** (`ros2_controllers.yaml`, `moveit_controllers.yaml`): 轨迹如何下发到控制器
5. **桥接层** (`move_group.json`): Uni-Lab-OS 的 `MoveitInterface` 如何连接到 MoveIt2

---

## 6. 设计特点

1. **多设备支持**：通过设备名前缀机制（`device_id_`），所有关节名、link 名、控制器名都是唯一的，支持在同一 ROS 2 环境中运行多台机械臂。

2. **动态场景构建**：`ResourceVisualization` 根据实验室配置动态生成 URDF/SRDF，无需手动编写或维护静态模型文件。

3. **规划/执行分离**：`MoveIt2` 类支持 MoveGroup Action（合并模式）和 Plan+Execute（分离模式），可根据场景灵活选择。

4. **线程安全**：`MoveIt2` 类通过 `threading.Lock` 保护关节状态和执行状态的并发访问。

5. **碰撞场景集成**：支持完整的碰撞物体生命周期管理（添加/移动/附着/分离/删除），可在运行时动态更新规划场景。

6. **资源 TF 动态更新**：`MoveitInterface` 通过 `resource_manager()` 在 pick/place 时动态更新资源的 TF 父 link，实现物体在机器人和环境之间的"跟随"效果。

7. **统一设备模板**：MoveIt 设备遵循统一的 `config/` 目录结构（SRDF、ros2_control、controllers、kinematics），新增设备只需按模板创建配置文件即可接入 MoveIt2 运动规划。
