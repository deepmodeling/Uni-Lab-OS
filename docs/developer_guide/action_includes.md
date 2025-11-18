## 基础通用操作

### `SendCmd`

```{literalinclude} ../../unilabos_msgs/action/SendCmd.action
:language: yaml
```

---

### `FloatSingleInput`

```{literalinclude} ../../unilabos_msgs/action/FloatSingleInput.action
:language: yaml
```

---

### `IntSingleInput`

```{literalinclude} ../../unilabos_msgs/action/IntSingleInput.action
:language: yaml
```

---

### `Point3DSeparateInput`

```{literalinclude} ../../unilabos_msgs/action/Point3DSeparateInput.action
:language: yaml
```

---

### `StrSingleInput`

```{literalinclude} ../../unilabos_msgs/action/StrSingleInput.action
:language: yaml
```

---

### `Wait`

```{literalinclude} ../../unilabos_msgs/action/Wait.action
:language: yaml
```

---

## 化学实验操作

Uni-Lab 化学操作指令集多数来自 [XDL](https://croningroup.gitlab.io/chemputer/xdl/standard/full_steps_specification.html#)，包含有机合成实验中常见的操作。

### 物料添加

#### `Add`

```{literalinclude} ../../unilabos_msgs/action/Add.action
:language: yaml
```

---

#### `AddSolid`

```{literalinclude} ../../unilabos_msgs/action/AddSolid.action
:language: yaml
```

---

### 液体转移与泵控制

#### `PumpTransfer`

```{literalinclude} ../../unilabos_msgs/action/PumpTransfer.action
:language: yaml
```

---

#### `SetPumpPosition`

```{literalinclude} ../../unilabos_msgs/action/SetPumpPosition.action
:language: yaml
```

---

#### `Transfer`

```{literalinclude} ../../unilabos_msgs/action/Transfer.action
:language: yaml
```

---

### 温度控制

#### `HeatChill`

```{literalinclude} ../../unilabos_msgs/action/HeatChill.action
:language: yaml
```

---

#### `HeatChillStart`

```{literalinclude} ../../unilabos_msgs/action/HeatChillStart.action
:language: yaml
```

---

#### `HeatChillStop`

```{literalinclude} ../../unilabos_msgs/action/HeatChillStop.action
:language: yaml
```

---

### 搅拌控制

#### `StartStir`

```{literalinclude} ../../unilabos_msgs/action/StartStir.action
:language: yaml
```

---

#### `Stir`

```{literalinclude} ../../unilabos_msgs/action/Stir.action
:language: yaml
```

---

#### `StopStir`

```{literalinclude} ../../unilabos_msgs/action/StopStir.action
:language: yaml
```

---

### 气体与真空控制

#### `EvacuateAndRefill`

```{literalinclude} ../../unilabos_msgs/action/EvacuateAndRefill.action
:language: yaml
```

---

#### `Purge`

```{literalinclude} ../../unilabos_msgs/action/Purge.action
:language: yaml
```

---

#### `StartPurge`

```{literalinclude} ../../unilabos_msgs/action/StartPurge.action
:language: yaml
```

---

#### `StopPurge`

```{literalinclude} ../../unilabos_msgs/action/StopPurge.action
:language: yaml
```

---

### 分离与过滤

#### `Centrifuge`

```{literalinclude} ../../unilabos_msgs/action/Centrifuge.action
:language: yaml
```

---

#### `Filter`

```{literalinclude} ../../unilabos_msgs/action/Filter.action
:language: yaml
```

---

#### `FilterThrough`

```{literalinclude} ../../unilabos_msgs/action/FilterThrough.action
:language: yaml
```

---

#### `RunColumn`

```{literalinclude} ../../unilabos_msgs/action/RunColumn.action
:language: yaml
```

---

#### `Separate`

```{literalinclude} ../../unilabos_msgs/action/Separate.action
:language: yaml
```

---

### 化学处理

#### `AdjustPH`

```{literalinclude} ../../unilabos_msgs/action/AdjustPH.action
:language: yaml
```

---

#### `Crystallize`

```{literalinclude} ../../unilabos_msgs/action/Crystallize.action
:language: yaml
```

---

#### `Dissolve`

```{literalinclude} ../../unilabos_msgs/action/Dissolve.action
:language: yaml
```

---

#### `Dry`

```{literalinclude} ../../unilabos_msgs/action/Dry.action
:language: yaml
```

---

#### `Evaporate`

```{literalinclude} ../../unilabos_msgs/action/Evaporate.action
:language: yaml
```

---

#### `Hydrogenate`

```{literalinclude} ../../unilabos_msgs/action/Hydrogenate.action
:language: yaml
```

---

#### `Recrystallize`

```{literalinclude} ../../unilabos_msgs/action/Recrystallize.action
:language: yaml
```

---

#### `WashSolid`

```{literalinclude} ../../unilabos_msgs/action/WashSolid.action
:language: yaml
```

---

### 清洁与维护

#### `Clean`

```{literalinclude} ../../unilabos_msgs/action/Clean.action
:language: yaml
```

---

#### `CleanVessel`

```{literalinclude} ../../unilabos_msgs/action/CleanVessel.action
:language: yaml
```

---

#### `EmptyIn`

```{literalinclude} ../../unilabos_msgs/action/EmptyIn.action
:language: yaml
```

---

#### `ResetHandling`

```{literalinclude} ../../unilabos_msgs/action/ResetHandling.action
:language: yaml
```

---

## 生物自动化操作

Uni-Lab 生物操作指令集多数来自 [PyLabRobot](https://docs.pylabrobot.org/user_guide/index.html)，包含移液工作站的各类操作。

### `LiquidHandlerAdd`

```{literalinclude} ../../unilabos_msgs/action/LiquidHandlerAdd.action
:language: yaml
```

---

### `LiquidHandlerAspirate`

```{literalinclude} ../../unilabos_msgs/action/LiquidHandlerAspirate.action
:language: yaml
```

---

### `LiquidHandlerDiscardTips`

```{literalinclude} ../../unilabos_msgs/action/LiquidHandlerDiscardTips.action
:language: yaml
```

---

### `LiquidHandlerDispense`

```{literalinclude} ../../unilabos_msgs/action/LiquidHandlerDispense.action
:language: yaml
```

---

### `LiquidHandlerDropTips`

```{literalinclude} ../../unilabos_msgs/action/LiquidHandlerDropTips.action
:language: yaml
```

---

### `LiquidHandlerDropTips96`

```{literalinclude} ../../unilabos_msgs/action/LiquidHandlerDropTips96.action
:language: yaml
```

---

### `LiquidHandlerIncubateBiomek`

```{literalinclude} ../../unilabos_msgs/action/LiquidHandlerIncubateBiomek.action
:language: yaml
```

---

### `LiquidHandlerMix`

```{literalinclude} ../../unilabos_msgs/action/LiquidHandlerMix.action
:language: yaml
```

---

### `LiquidHandlerMoveBiomek`

```{literalinclude} ../../unilabos_msgs/action/LiquidHandlerMoveBiomek.action
:language: yaml
```

---

### `LiquidHandlerMoveLid`

```{literalinclude} ../../unilabos_msgs/action/LiquidHandlerMoveLid.action
:language: yaml
```

---

### `LiquidHandlerMovePlate`

```{literalinclude} ../../unilabos_msgs/action/LiquidHandlerMovePlate.action
:language: yaml
```

---

### `LiquidHandlerMoveResource`

```{literalinclude} ../../unilabos_msgs/action/LiquidHandlerMoveResource.action
:language: yaml
```

---

### `LiquidHandlerMoveTo`

```{literalinclude} ../../unilabos_msgs/action/LiquidHandlerMoveTo.action
:language: yaml
```

---

### `LiquidHandlerOscillateBiomek`

```{literalinclude} ../../unilabos_msgs/action/LiquidHandlerOscillateBiomek.action
:language: yaml
```

---

### `LiquidHandlerPickUpTips`

```{literalinclude} ../../unilabos_msgs/action/LiquidHandlerPickUpTips.action
:language: yaml
```

---

### `LiquidHandlerPickUpTips96`

```{literalinclude} ../../unilabos_msgs/action/LiquidHandlerPickUpTips96.action
:language: yaml
```

---

### `LiquidHandlerProtocolCreation`

```{literalinclude} ../../unilabos_msgs/action/LiquidHandlerProtocolCreation.action
:language: yaml
```

---

### `LiquidHandlerRemove`

```{literalinclude} ../../unilabos_msgs/action/LiquidHandlerRemove.action
:language: yaml
```

---

### `LiquidHandlerReturnTips`

```{literalinclude} ../../unilabos_msgs/action/LiquidHandlerReturnTips.action
:language: yaml
```

---

### `LiquidHandlerReturnTips96`

```{literalinclude} ../../unilabos_msgs/action/LiquidHandlerReturnTips96.action
:language: yaml
```

---

### `LiquidHandlerSetGroup`

```{literalinclude} ../../unilabos_msgs/action/LiquidHandlerSetGroup.action
:language: yaml
```

---

### `LiquidHandlerSetLiquid`

```{literalinclude} ../../unilabos_msgs/action/LiquidHandlerSetLiquid.action
:language: yaml
```

---

### `LiquidHandlerSetTipRack`

```{literalinclude} ../../unilabos_msgs/action/LiquidHandlerSetTipRack.action
:language: yaml
```

---

### `LiquidHandlerStamp`

```{literalinclude} ../../unilabos_msgs/action/LiquidHandlerStamp.action
:language: yaml
```

---

### `LiquidHandlerTransfer`

```{literalinclude} ../../unilabos_msgs/action/LiquidHandlerTransfer.action
:language: yaml
```

---

### `LiquidHandlerTransferBiomek`

```{literalinclude} ../../unilabos_msgs/action/LiquidHandlerTransferBiomek.action
:language: yaml
```

---

### `LiquidHandlerTransferGroup`

```{literalinclude} ../../unilabos_msgs/action/LiquidHandlerTransferGroup.action
:language: yaml
```

---

## 专用工作站操作

### 反应工作站

#### `ReactionStationDripBack`

```{literalinclude} ../../unilabos_msgs/action/ReactionStationDripBack.action
:language: yaml
```

---

#### `ReactionStationLiquidFeedBeaker`

```{literalinclude} ../../unilabos_msgs/action/ReactionStationLiquidFeedBeaker.action
:language: yaml
```

---

#### `ReactionStationLiquidFeedSolvents`

```{literalinclude} ../../unilabos_msgs/action/ReactionStationLiquidFeedSolvents.action
:language: yaml
```

---

#### `ReactionStationLiquidFeedTitration`

```{literalinclude} ../../unilabos_msgs/action/ReactionStationLiquidFeedTitration.action
:language: yaml
```

---

#### `ReactionStationLiquidFeedVialsNonTitration`

```{literalinclude} ../../unilabos_msgs/action/ReactionStationLiquidFeedVialsNonTitration.action
:language: yaml
```

---

#### `ReactionStationProExecu`

```{literalinclude} ../../unilabos_msgs/action/ReactionStationProExecu.action
:language: yaml
```

---

#### `ReactionStationReactorTakenOut`

```{literalinclude} ../../unilabos_msgs/action/ReactionStationReactorTakenOut.action
:language: yaml
```

---

#### `ReactionStationReaTackIn`

```{literalinclude} ../../unilabos_msgs/action/ReactionStationReaTackIn.action
:language: yaml
```

---

#### `ReactionStationSolidFeedVial`

```{literalinclude} ../../unilabos_msgs/action/ReactionStationSolidFeedVial.action
:language: yaml
```

---

### 固体分配站

#### `SolidDispenseAddPowderTube`

```{literalinclude} ../../unilabos_msgs/action/SolidDispenseAddPowderTube.action
:language: yaml
```

---

### 分液工作站

#### `DispenStationSolnPrep`

```{literalinclude} ../../unilabos_msgs/action/DispenStationSolnPrep.action
:language: yaml
```

---

#### `DispenStationVialFeed`

```{literalinclude} ../../unilabos_msgs/action/DispenStationVialFeed.action
:language: yaml
```

---

### 后处理工作站

#### `PostProcessGrab`

```{literalinclude} ../../unilabos_msgs/action/PostProcessGrab.action
:language: yaml
```

---

#### `PostProcessTriggerClean`

```{literalinclude} ../../unilabos_msgs/action/PostProcessTriggerClean.action
:language: yaml
```

---

#### `PostProcessTriggerPostPro`

```{literalinclude} ../../unilabos_msgs/action/PostProcessTriggerPostPro.action
:language: yaml
```

---

## 系统管理与资源调度

### 资源与布局管理

#### `DefaultLayoutRecommendLayout`

```{literalinclude} ../../unilabos_msgs/action/DefaultLayoutRecommendLayout.action
:language: yaml
```

---

#### `ResourceCreateFromOuter`

```{literalinclude} ../../unilabos_msgs/action/ResourceCreateFromOuter.action
:language: yaml
```

---

#### `ResourceCreateFromOuterEasy`

```{literalinclude} ../../unilabos_msgs/action/ResourceCreateFromOuterEasy.action
:language: yaml
```

---

### 多工作站协调

#### `AGVTransfer`

```{literalinclude} ../../unilabos_msgs/action/AGVTransfer.action
:language: yaml
```

---

#### `WorkStationRun`

```{literalinclude} ../../unilabos_msgs/action/WorkStationRun.action
:language: yaml
```

---

## 机器人控制（ROS2 标准）

Uni-Lab 机械臂、机器人、夹爪和导航指令集沿用 ROS2 的 `control_msgs` 和 `nav2_msgs`。

### 机械臂与关节控制

#### `FollowJointTrajectory`

```yaml
# The trajectory for all revolute, continuous or prismatic joints
trajectory_msgs/JointTrajectory trajectory
# The trajectory for all planar or floating joints (i.e. individual joints with more than one DOF)
trajectory_msgs/MultiDOFJointTrajectory multi_dof_trajectory

# Tolerances for the trajectory.  If the measured joint values fall
# outside the tolerances the trajectory goal is aborted.  Any
# tolerances that are not specified (by being omitted or set to 0) are
# set to the defaults for the action server (often taken from the
# parameter server).

# Tolerances applied to the joints as the trajectory is executed.  If
# violated, the goal aborts with error_code set to
# PATH_TOLERANCE_VIOLATED.
JointTolerance[] path_tolerance
JointComponentTolerance[] component_path_tolerance

# To report success, the joints must be within goal_tolerance of the
# final trajectory value.  The goal must be achieved by time the
# trajectory ends plus goal_time_tolerance.  (goal_time_tolerance
# allows some leeway in time, so that the trajectory goal can still
# succeed even if the joints reach the goal some time after the
# precise end time of the trajectory).
#
# If the joints are not within goal_tolerance after "trajectory finish
# time" + goal_time_tolerance, the goal aborts with error_code set to
# GOAL_TOLERANCE_VIOLATED
JointTolerance[] goal_tolerance
JointComponentTolerance[] component_goal_tolerance
builtin_interfaces/Duration goal_time_tolerance

---
int32 error_code
int32 SUCCESSFUL = 0
int32 INVALID_GOAL = -1
int32 INVALID_JOINTS = -2
int32 OLD_HEADER_TIMESTAMP = -3
int32 PATH_TOLERANCE_VIOLATED = -4
int32 GOAL_TOLERANCE_VIOLATED = -5

# Human readable description of the error code. Contains complementary
# information that is especially useful when execution fails, for instance:
# - INVALID_GOAL: The reason for the invalid goal (e.g., the requested
#   trajectory is in the past).
# - INVALID_JOINTS: The mismatch between the expected controller joints
#   and those provided in the goal.
# - PATH_TOLERANCE_VIOLATED and GOAL_TOLERANCE_VIOLATED: Which joint
#   violated which tolerance, and by how much.
string error_string

---
std_msgs/Header header
string[] joint_names
trajectory_msgs/JointTrajectoryPoint desired
trajectory_msgs/JointTrajectoryPoint actual
trajectory_msgs/JointTrajectoryPoint error

string[] multi_dof_joint_names
trajectory_msgs/MultiDOFJointTrajectoryPoint multi_dof_desired
trajectory_msgs/MultiDOFJointTrajectoryPoint multi_dof_actual
trajectory_msgs/MultiDOFJointTrajectoryPoint multi_dof_error

```

---

#### `JointTrajectory`

```yaml
trajectory_msgs/JointTrajectory trajectory
---

---
```

---

#### `PointHead`

```yaml
geometry_msgs/PointStamped target
geometry_msgs/Vector3 pointing_axis
string pointing_frame
builtin_interfaces/Duration min_duration
float64 max_velocity
---

---
float64 pointing_angle_error
```

---

#### `SingleJointPosition`

```yaml
float64 position
builtin_interfaces/Duration min_duration
float64 max_velocity
---

---
std_msgs/Header header
float64 position
float64 velocity
float64 error
```

---

### 夹爪控制

#### `GripperCommand`

```yaml
GripperCommand command
---
float64 position  # The current gripper gap size (in meters)
float64 effort    # The current effort exerted (in Newtons)
bool stalled      # True iff the gripper is exerting max effort and not moving
bool reached_goal # True iff the gripper position has reached the commanded setpoint
---
float64 position  # The current gripper gap size (in meters)
float64 effort    # The current effort exerted (in Newtons)
bool stalled      # True iff the gripper is exerting max effort and not moving
bool reached_goal # True iff the gripper position has reached the commanded setpoint

```

---

#### `ParallelGripperCommand`

```yaml
# Parallel grippers refer to an end effector where two opposing fingers grasp an object from opposite sides.
sensor_msgs/JointState command
# name: the name(s) of the joint this command is requesting
# position: desired position of each gripper joint (radians or meters)
# velocity: (optional, not used if empty) max velocity of the joint allowed while moving (radians or meters / second)
# effort: (optional, not used if empty) max effort of the joint allowed while moving (Newtons or Newton-meters)
---
sensor_msgs/JointState state # The current gripper state.
# position of each joint (radians or meters)
# optional: velocity of each joint (radians or meters / second)
# optional: effort of each joint (Newtons or Newton-meters)
bool stalled      # True if the gripper is exerting max effort and not moving
bool reached_goal # True if the gripper position has reached the commanded setpoint
---
sensor_msgs/JointState state # The current gripper state.
# position of each joint (radians or meters)
# optional: velocity of each joint (radians or meters / second)
# optional: effort of each joint (Newtons or Newton-meters)

```

---

### 导航与路径规划

#### `AssistedTeleop`

```yaml
#goal definition
builtin_interfaces/Duration time_allowance
---
#result definition
builtin_interfaces/Duration total_elapsed_time
---
#feedback
builtin_interfaces/Duration current_teleop_duration
```

---

#### `BackUp`

```yaml
#goal definition
geometry_msgs/Point target
float32 speed
builtin_interfaces/Duration time_allowance
---
#result definition
builtin_interfaces/Duration total_elapsed_time
---
#feedback definition
float32 distance_traveled
```

---

#### `ComputePathThroughPoses`

```yaml
#goal definition
geometry_msgs/PoseStamped[] goals
geometry_msgs/PoseStamped start
string planner_id
bool use_start # If false, use current robot pose as path start, if true, use start above instead
---
#result definition
nav_msgs/Path path
builtin_interfaces/Duration planning_time
---
#feedback definition
```

---

#### `ComputePathToPose`

```yaml
#goal definition
geometry_msgs/PoseStamped goal
geometry_msgs/PoseStamped start
string planner_id
bool use_start # If false, use current robot pose as path start, if true, use start above instead
---
#result definition
nav_msgs/Path path
builtin_interfaces/Duration planning_time
---
#feedback definition
```

---

#### `DriveOnHeading`

```yaml
#goal definition
geometry_msgs/Point target
float32 speed
builtin_interfaces/Duration time_allowance
---
#result definition
builtin_interfaces/Duration total_elapsed_time
---
#feedback definition
float32 distance_traveled
```

---

#### `DummyBehavior`

```yaml
#goal definition
std_msgs/String command
---
#result definition
builtin_interfaces/Duration total_elapsed_time
---
#feedback definition
```

---

#### `FollowPath`

```yaml
#goal definition
nav_msgs/Path path
string controller_id
string goal_checker_id
---
#result definition
std_msgs/Empty result
---
#feedback definition
float32 distance_to_goal
float32 speed
```

---

#### `FollowWaypoints`

```yaml
#goal definition
geometry_msgs/PoseStamped[] poses
---
#result definition
int32[] missed_waypoints
---
#feedback definition
uint32 current_waypoint
```

---

#### `NavigateThroughPoses`

```yaml
#goal definition
geometry_msgs/PoseStamped[] poses
string behavior_tree
---
#result definition
std_msgs/Empty result
---
#feedback definition
geometry_msgs/PoseStamped current_pose
builtin_interfaces/Duration navigation_time
builtin_interfaces/Duration estimated_time_remaining
int16 number_of_recoveries
float32 distance_remaining
int16 number_of_poses_remaining
```

---

#### `NavigateToPose`

```yaml
#goal definition
geometry_msgs/PoseStamped pose
string behavior_tree
---
#result definition
std_msgs/Empty result
---
#feedback definition
geometry_msgs/PoseStamped current_pose
builtin_interfaces/Duration navigation_time
builtin_interfaces/Duration estimated_time_remaining
int16 number_of_recoveries
float32 distance_remaining
```

---

#### `SmoothPath`

```yaml
#goal definition
nav_msgs/Path path
string smoother_id
builtin_interfaces/Duration max_smoothing_duration
bool check_for_collisions
---
#result definition
nav_msgs/Path path
builtin_interfaces/Duration smoothing_duration
bool was_completed
---
#feedback definition
```

---

#### `Spin`

```yaml
#goal definition
float32 target_yaw
builtin_interfaces/Duration time_allowance
---
#result definition
builtin_interfaces/Duration total_elapsed_time
---
#feedback definition
float32 angular_distance_traveled
```

---

#### `Wait` (Nav2)

> **注意**：这是 ROS2 nav2_msgs 的标准 Wait action，与 unilabos_msgs 的 Wait action 不同。

```yaml
#goal definition
builtin_interfaces/Duration time
---
#result definition
builtin_interfaces/Duration total_elapsed_time
---
#feedback definition
builtin_interfaces/Duration time_left
```

---
