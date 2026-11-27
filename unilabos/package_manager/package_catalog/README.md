# 包目录（PackageCatalog）

## 职责

从显式软件包来源静态产生规范定义、项目身份、注册表快照（Registry Snapshot）、
物料（Material）模型与外形投影。这里描述包里“有什么”，不安装、不发布、不激活。

## 公开 Interface

入口集中在 `__init__.py`：`WorkspaceSource`、需要冻结启动计划的纯
`compile_package_source(source, *, startup_plan)`、`compile_registry_snapshot`、
项目元数据解析及物料资产编译函数。

## 依赖方向

本 Module 是包管理最底层，只可依赖通用库和仓库中的静态定义编译能力；不得依赖
`package_distribution`、`workspace_runtime` 或 `driver_runtime`。

## 不变量

- 编译不导入或执行作者模块。
- 相同来源字节产生相同目录摘要和定义身份。
- 项目声明只有 `project_metadata.py` 一个解析权威。
- 物料资产只能从显式来源读取且必须关闭式校验路径。

## 修改路由

- 数据模型与规范化：`model.py`。
- Python 静态编译：`compilers/python/`。
- 注册表快照：`registry_snapshot.py`。
- 项目声明：`project_metadata.py`。
- 物料模型或外形：`material_models.py`、`material_shapes.py`。
