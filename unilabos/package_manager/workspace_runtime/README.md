# 工作区运行时（Workspace Runtime）

## 职责

把显式工作区和锁定外部包组合成稳定候选代，负责发现、文件监听、代际生成、注册表
快照（Registry Snapshot）激活和产品生命周期；不执行驱动实现。

## 公开 Interface

入口集中在 `__init__.py`。发现与统一编译在 `discovery.py`，候选代来源在
`package_source.py`，其余入口按监听、生成、激活和生命周期划分。

## 依赖方向

可依赖包目录（PackageCatalog）和包分发（Package Distribution），不得依赖驱动
运行时（Driver Runtime）、ROS2、调度器（Scheduler）或库存（Inventory）。

## 不变量

- 一个候选代固定主包、全部显式依赖、物理图和资产摘要。
- 文件变化产生新候选，不原地修改已发布代。
- 激活全有或全无；失败保留上一个稳定代。
- 发现和生成不执行作者代码。

## 修改路由

- 项目与来源发现：`discovery.py`。
- 主包/外部包同代组合：`package_source.py`。
- 文件稳定性与刷新协调：`monitor.py`。
- 候选代差异和生成：`generation.py`。
- 注册表发布：`activation.py`。
- 启停与恢复：`lifecycle.py`。
