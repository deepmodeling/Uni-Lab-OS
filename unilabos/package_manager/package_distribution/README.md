# 包分发（Package Distribution）

## 职责

管理显式依赖、锁文件、安装事务、已审计 wheel 和云端广场兼容投影。它消费包目录
（PackageCatalog），但不决定工作区候选代或驱动激活。

## 公开 Interface

入口集中在 `__init__.py`：`PackageDependencyManager`、`build_workspace_package`、
`audit_package_wheel`、锁模型、安装与发布 Adapter。软件包检查编排位于
`inspection.py`；构建和审计的 `compile_catalog` 以及上传 Adapter 的
`package_builder` 必须由组合根显式注入。

## 依赖方向

只可依赖包目录（PackageCatalog）和通用基础设施；不得导入工作区运行时
（Workspace Runtime）或驱动运行时（Driver Runtime）。外部系统调用进入
`adapters/`。

## 不变量

- 依赖来源必须显式声明并与锁文件完整一致。
- 任一依赖冲突时不发布部分代际。
- 归档排除规则由 `archive.py` 单点拥有。
- 构建只在临时暂存树嵌入包目录（PackageCatalog），不得修改作者源码。
- wheel 必须从自身内容重编译，并与暂存源码目录保持 canonical parity。
- 上传只消费已审计构建产物，不得退回软件包检查（Package Inspect）归档。
- 遗留 DTO 只是发布投影，不成为注册表（Registry）或物料（Material）权威。

## 修改路由

- 依赖锁与事务：`models.py`、`lock_codec.py`、`transaction.py`。
- 依赖解析：`dependency_manager.py`。
- 检查编排：`inspection.py`。
- 标准 wheel 暂存、构建与自审计：`build.py`。
- 归档：`archive.py`。
- 遗留注册表发现：`registry_discovery.py`。
- 遗留上传 DTO：`legacy_projection.py`。
- 外部传输或安装：`adapters/`。
