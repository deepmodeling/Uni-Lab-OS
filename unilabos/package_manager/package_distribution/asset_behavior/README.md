# 资产行为构建（Asset Behavior Build）

## 职责

领域包构建期工具：校验固定工程包，把与模型修订绑定的行为源码原样固化到
`<包>/_asset_code/<资产token>/`，并为领域包生成自有的静态 `@device` 转发声明。
厂家协议 adapter、PLR 通信与 virtual 驱动不经过这里，仍由领域包自己声明。

## 公开 Interface

入口集中在 `__init__.py`：

- `verify_package(root, expected)`：按 NVIDIA 清单、元数据与 BOM 校验工程包，返回 `unilab.json`。
- `materialize(root, package, assets, locks)` / `verify_generated(root, lock)`：固化与复核行为源码。
- `generate_forwarder(...)` / `write_forwarder(...)`：生成转发声明；`ForwarderOptions` 默认即中立合同。
- `relative_model_entry(...)`：按包目录（PackageCatalog）合同计算相对 `model.entry`。
- `build_locked(lock, cache, output, package)`：从已校验缓存构建到不存在的新目录；只对显式
  `declaration` 生成设备声明。无声明的机构控制器只固化源码，不猜测动作。
- `check_bindings(...)`：通过注入的 OS 扫描器核对锁行 `os_binding`；返回静态绑定报告。
- `resolve_cached(...)`：以固定 ID/摘要解析唯一缓存；下载与行为构建复用同一个原生包校验器。

## 按锁接入

```text
unilab package assets fetch --lock assets.lock.json --cache CACHE --mirror MIRROR
unilab package assets verify --lock assets.lock.json --cache CACHE
unilab package assets build --lock assets.lock.json --cache CACHE --output NEW_DIR --python-package station_assets
unilab package assets check-bindings --lock assets.lock.json --cache CACHE --project PROJECT --output report.json --preserve graph.json
```

省略 `--mirror` 时 fetch 使用现有 OSS 存储 Adapter。build 不联网，不执行作者代码，不安装
`requires`，不修改原设备声明或安装变换；输出是构建中间产物，需要领域包完成模型入口布局和
package-data 声明。模型 `entry` 没有重定位时拒绝自动生成设备声明。
已有设备优先保留其 registry owner/action，由领域适配器调用固化的机构行为；新机构可显式
声明独立设备导出。同一 ID 不允许同时由原设备与生成类注册。

`os_binding` 是现有资产锁行的可选消费绑定，不能替代资产身份、源码或 OS 声明：

```json
{
  "definition_file": "station/devices/motor.py",
  "class_name": "Motor",
  "contract_sha256": "<contract_digest(OS 扫描结果)>",
  "actions": {
    "move": {"implementation": "motion:Motion", "method": "move", "adapter": "direct"}
  }
}
```

owner 来自同一锁行的 `owner_registry_id`。核对设备、动作、状态、初始化参数和库位合同，
`direct` 还核对参数/默认值/返回注解和同步/异步签名。需要单位/参数转换时，adapter 指向项目内
真实适配文件，并提供 `adapter_sha256` 与非空 `argument_mapping`；静态检查不证明映射逻辑正确，
必须运行消费动作并观测模型。没有 os_binding 的运动脚本报告 incomplete；无行为资产报告
model_only；declared_binding 仅表示静态声明通过，execution 仍为 not_run。

`--baseline old-report.json` 比较固定包、owner、公开合同、映射以及 `--preserve` 指定文件的摘要。
报告文件由调用者放在输出区。bindings 未通过时 CLI 非零退出，不把缺失项提升为已执行。

被动导轨/上料机构可在 `os_binding.action_source` 指定上级动作声明的
`owner_registry_id`、`definition_file`、`class_name` 和 `contract_sha256`。
资产锁行的 owner 仍表示该模型设备；动作从明确的上级声明校验，不给被动机构虚构动作。
`evidence_files` 可固定动作配方、坐标对齐、轨迹及声明实现文件的相对路径和 SHA-256；
这些文件漂移时拒绝绑定。上级合同与依据文件也参加 baseline 对比。

批量离线回归：`python -m tests.package_manager.asset_behavior.run_locked_batch --help`。
它从显式本地镜像冷拉取，构建后在审阅命名空间测试真实封存行为；原项目的设备/action/配置只读。
审阅转发类的成功不能替代已有工站动作接入；未确认行程保留 incomplete。脚本需要 OpenUSD，
只有显式 `--execute-behavior` 才执行受信任资产源码，不是代码沙箱。

## 依赖方向

仅用标准库和同层 asset_fetch 的原生包校验，不直接导入包目录、注册表、工作区或驱动运行时；
与它们的一致性由测试对照，而不是运行时依赖。

## 不变量

- 不导入、不执行资产代码与包作者代码；固化只复制 BOM 已封存、显式声明的 `.py` 文件。
- 资产实现不得自注册 `@device`/`@resource`；每个 registry ID 仍只有领域包一个 owner。
- 转发类在自己的类体内重声明注册表静态扫描会登记的全部成员：同步与 async 的 `@action`/`@legacy_action`、
  `@property`（含 setter/deleter）、`@topic_config`、无参 `get_*`、`__init__` 签名和未装饰公开方法；
  `@not_action`、`@subscribe` 与私有方法不重声明。方法 docstring 保留。
- registry ID 默认不变；只有显式传 `registry_id_prefix` 才加前缀。实现类自己已用同一 ID 注册时拒绝生成，
  因为运行时会被判为重复登记；资产行为实现本身不得注册，正好满足。
- `behavior_binding` 逐类处理：`asset_python` 固化；`asset_reference` 要求被引用 owner 同时选中并锁定；
  无绑定或 `implementation: null` 视为无行为；其他形态关闭式失败，不静默跳过。
- `model.entry` 相对声明文件目录、不得含 `..`、必须在导入包内，与 `package_catalog/material_models.py` 一致。

## 修改路由

- 路径与摘要：`paths.py`。
- 固定锁构建、显式绑定检查及基线比较：`locked.py`；CLI 注入实际 OS 静态扫描器。
- 工程包校验：`package.py`。
- 源码固化与复核：`materialize.py`。
- 转发声明生成：`forwarder.py`。
- 注册表扫描规则变化时，先改 `tests/package_manager/asset_behavior/test_os_scanner_contract.py` 的对照，
  再调整 `forwarder.py` 的成员选择。
