# 驱动运行时（Driver Runtime）

## 职责

消费已选定且有包证据的设备定义，解析驱动实现、合并运行配置并返回可供产品组合根
使用的激活结果。它是语言运行进程与 Uni-Lab 定义之间的最小稳定接缝。

## 公开 Interface

`model.py` 定义稳定激活结果和失败；`python_activation.py` 实现当前 Python Adapter；
`__init__.py` 只导出这组小 Interface。

## 依赖方向

只依赖包目录（PackageCatalog）和通用加载能力；不得依赖包分发（Package
Distribution）、工作区运行时（Workspace Runtime）、ROS2、调度器（Scheduler）、
库存（Inventory）或 Backend。

## 不变量

- 静态导入不执行作者驱动代码。
- 只有显式激活才加载已选实现。
- 包定义身份、来源身份和内容摘要在激活结果中保持可审计。
- 驱动运行时不取得工作流任务（WorkflowTask）或作业执行占用
  （JobExecutionClaim）权威。

## 修改路由

- 稳定进程无关模型：`model.py`。
- Python 类加载与配置合并：`python_activation.py`。
- 未来 C#、Rust 驱动新增独立进程协议 Adapter 和监督实现；保持同一激活结果
  Interface，不把语言运行细节扩散到注册表或工作区。
