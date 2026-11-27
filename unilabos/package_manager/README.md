# 包管理（Package Manager）

## 职责

这里是包目录（PackageCatalog）、包分发（Package Distribution）、工作区运行时
（Workspace Runtime）和驱动运行时（Driver Runtime）的总导航。根目录只保留惰性
正式门面 `__init__.py` 与命令行 Adapter `cli.py`；实现必须进入所属子 Module。

## 公开 Interface

- Python 调用者优先从 `unilabos.package_manager` 使用惰性门面。
- 产品命令从 `unilab package ...` 进入 `cli.py`。
- 内部调用者直接依赖职责所有者，禁止借根门面绕回上层。

## 依赖方向

```text
package_catalog
      ↑
package_distribution
      ↑
workspace_runtime

driver_runtime -> package_catalog
```

`package_catalog` 不依赖其他三层；`package_distribution` 不依赖运行时；
`driver_runtime` 不依赖分发、工作区、ROS2、调度器（Scheduler）、库存（Inventory）
或 Backend；`workspace_runtime` 负责产品组合但不依赖 `driver_runtime`。

## 不变量

- 发现、编译和检查不执行包作者代码。
- 同一候选代只使用一个包目录（PackageCatalog）编译 Interface。
- 包工具、注册表（Registry）和工作区启动共享项目元数据解析。
- 根目录不新增实现文件或兼容包装。

## 修改路由

- 定义、Schema、项目元数据、物料（Material）资产：`package_catalog/`。
- 安装、依赖锁、归档、云端广场发布：`package_distribution/`。
- 工作区监听、候选代、激活和生命周期：`workspace_runtime/`。
- 驱动进程协议与语言 Adapter：`driver_runtime/`。
- 只在新增或调整公共命令时修改 `cli.py`；只在公开入口变化时修改门面。

未来 C# 与 Rust 驱动包继续复用同一包目录和分发合同，只在
`driver_runtime` 增加进程协议 Adapter；不得把语言安装器、运行进程或 ROS2 细节
塞入静态目录编译器。
