# 包分发 Adapter

## 职责

把包分发（Package Distribution）的稳定 Interface 连接到 Python 安装环境和云端
广场 HTTP 合同；这里不拥有包目录（PackageCatalog）编译或工作区生命周期。

## 公开 Interface

`directory.py` 提供安装入口，`cloud.py` 提供 `PublicationPort`、已审计构建发布与上传
编排；`__init__.py` 是本目录唯一公开索引。

## 依赖方向

Adapter 可依赖同层归档、错误和投影能力以及真实外部 SDK；不得依赖工作区运行时
（Workspace Runtime）、驱动运行时（Driver Runtime）或产品组合根。

## 不变量

- 外部网络异常保持原异常，稳定业务拒绝使用 `PackageCLIError`。
- 发布只消费同一已审计 wheel 产物，不重新扫描、编译或构建包。
- 安装后设备发现只做 AST 静态扫描，不执行作者驱动代码。

## 修改路由

- 云端地址、鉴权和上传合同：`cloud.py`。
- uv/pip 安装与已安装分发扫描：`directory.py`。
- 新外部分发方式必须增加独立 Adapter，并复用同层稳定 Interface。
