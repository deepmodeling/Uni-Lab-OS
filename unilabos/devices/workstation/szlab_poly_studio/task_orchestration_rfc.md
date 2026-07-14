# SZLab Task 编排能力讨论稿

## 背景

SZLab Poly Studio 已接入多个设备 action，包括机械臂转运、S07 固体加料、S08 开关盖、S09 移液、S04 磁搅、S05 拍照等。单个 action 已具备调试基础，下一步问题集中在多样品、多设备、多工位协作时的任务编排和资源调度。

线性 workflow 对单样品调试直观，但在高通量场景中容易让机械臂或工位等待。例如 S07 注粉期间，机械臂已经空闲，理论上可以继续给 S08/S09 放瓶，或准备下一个样品。为避免把这些调度逻辑写死在 action 或线性 DAG 中，需要在现有 workflow 与 device action 之间增加 Task 编排层。

## 核心方案

```mermaid
flowchart LR
  A[Device Action<br/>稳定设备能力] --> B[Recipe DAG<br/>完整工艺流程]
  B --> C[Task Template<br/>框选 DAG 节点生成]
  C --> D[Task Instance<br/>样品级任务]
  D --> E[Scheduler<br/>按资源和传感器启动任务]
  E --> A
```

分层含义：

- **Device Action**：设备能力层，接入完成后尽量稳定。
- **Recipe DAG**：前端仍用 DAG 表达完整工艺流程，负责“人能看懂的 recipe”。
- **Task Template**：从 DAG 中框选一段节点，形成可调度的工艺任务。
- **Task Instance**：每个样品基于 Task Template 生成具体任务实例。
- **Scheduler**：根据前置关系、传感器状态、资源锁选择可启动任务。

## 当前模式与目标模式

当前更接近顺序执行：

```mermaid
flowchart LR
  A[前端 Workflow DAG] --> B[拓扑排序]
  B --> C[顺序执行节点]
  C --> D[调用 Device Action]
```

目标是引入 Task 调度：

```mermaid
flowchart LR
  A[前端 Recipe DAG] --> B[框选节点]
  B --> C[Task Template]
  C --> D[Task Queue]
  D --> E{可启动?}
  E -->|是| F[执行 Task]
  E -->|否| G[等待状态变化]
  F --> H[调用已有 Device Action]
```

## 前端交互示意

一个线性 DAG 可以被切成多个 Task：

```mermaid
flowchart LR
  A[Robot 放到 S07] --> B[S07 注粉]
  B --> C[Robot 转运到 S09]
  C --> D[S09 加液]
  D --> E[Robot 转运到 S04]
  E --> F[S04 磁搅]
  F --> G[S05 拍照]

  subgraph T1[Task: S07 固体加料]
    A
    B
  end

  subgraph T2[Task: S09 配液]
    C
    D
  end

  subgraph T3[Task: S04 反应记录]
    E
    F
    G
  end
```

## 调度判断

第一版 Scheduler 不需要复杂优化算法，先完成基础可启动判断：

```mermaid
flowchart TD
  A[Task Pool] --> B{前置 Task 完成?}
  B -- 否 --> X[Blocked]
  B -- 是 --> C{传感器空闲?}
  C -- 否 --> X
  C -- 是 --> D{资源锁可用?}
  D -- 否 --> X
  D -- 是 --> E[Ready]
  E --> F[启动 Task]
  F --> G[执行内部 action]
  G --> H[释放资源 / 更新状态]
```

第一版判断条件：

```text
can_start(task) =
  前置 task 已完成
  && 目标传感器空闲
  && robot / 工站 / 槽位资源未被占用
```

后续可扩展：

- 优先级。
- 等待时间。
- 瓶颈设备权重。
- 高通量设备填槽策略。
- 机械臂路径优化。
- 多机械臂调度。

## 边界划分

```mermaid
flowchart LR
  A[设备能力变化] --> B[修改 Device Action]
  C[实验流程变化] --> D[修改 Task / Recipe]
  E[调度策略变化] --> F[修改 Scheduler]
```

原则：

- Device Action 不绑定具体 recipe。
- Recipe DAG 负责表达完整工艺。
- Task 负责将一段工艺变成可调度单元。
- Scheduler 负责运行时资源协调。

## 本次会议目标

需要优先确认以下方向：

1. 是否认可 `Device Action -> Recipe DAG -> Task -> Scheduler` 的分层。
2. Unilab 本身是否其实已经具备了类似功能，或者计划做类似的功能