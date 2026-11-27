# OS 资产消费与动作绑定

读取时机：发布资产后交接另一台机器、按锁下载、接入 OS 动作、升级资产或 package manager。
资产身份/分类按 catalog-intake，库位尺寸按 asset-library-os；本节只维护消费链路。

## 版本与身份

先记录实际运行 OS 的模块路径、commit/dirty 状态、领域包路径及资产锁摘要；不要仅凭目录名、
分支日期或 README 推断能力。确认同一运行版本包含 `package_distribution.asset_fetch` 与
`asset_behavior`，并检查 `unilab package assets --help` 的实际命令。集成目标不明确时先在当前
使用版本的隔离分支完成可移植变更，保留服务及未提交内容，报告目标分支尚未合入。

使用已有 `unilab.asset-dependencies/1` 锁。统一业务 UUID、工程 asset_id、OS registry ID 和
graph 实例 ID 是不同身份；从 `model.targets` 固定 asset_id/package_digest，锁行的
owner_registry_id 明确设备所有者，实例变换仍留在 graph。名称相同不能建立动作或物料等价关系。

## 消费顺序

1. **拉取与复核**：`unilab package assets fetch --lock LOCK --cache CACHE`，可用 `--mirror MIRROR`
   做本地离线镜像回归；缓存里已有的包不联网，`--offline` 完全不连存储；锁行也可以写本地工程包
   `path`。再执行 `assets verify`。缺失固定版本就失败，不回退最新版。fetch 不执行
   下载的 Python、不注册 device/action，也不安装 requires。包字节完整性与动作能力分开记录。
   加 `--project` 时同时把显示模型落到锁行 `model_dir` 并在模型间共享相同贴图，见
   [model-sources.md](model-sources.md)。
2. **显式构建**：支持该入口的版本使用 `assets build --lock LOCK --cache CACHE --output NEW_DIR
   --python-package IMPORT_NAME`。产物固化在新目录的 `_asset_code`，保存来源/派生摘要，源包不改。
   它是领域包构建中间产物，消费者仍须布置模型入口、package-data 和显式依赖。
3. **选择接入方式**：已有 OS 设备优先保留原 device/action/参数/状态，由已有或明确维护的适配器
   调用模型耦合行为；无现有 owner 的独立机构才根据显式 declaration 生成设备转发声明。
   JointMotion 一类控制器不是厂家驱动，不因存在 set_positions 就自动生成厂家动作。
4. **静态绑定检查**：`assets check-bindings --lock LOCK --cache CACHE --project PROJECT --output REPORT`。
   消费绑定放在锁行可选的 os_binding（字段以实际 OS README/API 为准），固定声明文件、类名、
   owner、公开合同摘要，以及 action→implementation/method/adapter。direct 要求签名一致；转换
   参数或单位时固定适配源码摘要并列出 argument_mapping，再用执行回归验证转换。
5. **不变量比较**：变更前保存合同报告；变更后使用 `--baseline`，并用重复的 `--preserve` 指定
   现有安装、graph、取枪参数等配置文件。配置允许的局部变更另用 P0 check-replacement 精确白名单；
   不能用本轮快照冒充历史基线。脚本报告只覆盖显式选定的文件/字段。
6. **实际执行**：至少覆盖静态资源、独立机构、已有设备、多模块总成。从空缓存到构建、OS 扫描和
   动作调用，观测关节/刚体/库位世界矩阵，验证非零安装变换、双实例隔离、复位与拒绝输入。
   继承方法必须在转发类体中显式重声明，才能被当前 OS 静态扫描登记；保留异步、状态与初始化合同。

没有 os_binding 的行为包输出 incomplete；无行为资产可为 model_only；declared_binding 只证明
静态声明成立，execution 仍需独立证据。不得因构建、静态扫描、离线审阅设备或回放成功，报告现有
工站动作、物理求解或硬件已经接通。未知行程、倾角和适配规则继续列缺口，不为了通过回归补猜测值。

## 发布与回归范围

集成 OS 代码不应修改已封包资产的字节或重复注册设备 ID。新增行为/模型内容需要追加资产修订，
更新锁时保留旧 pin 和前后回执；不得把缓存当编辑目录。

源实现位于 OS 的 asset_fetch/asset_behavior，技能只记录使用流程，不复制一份构建器。运行对应
模块测试及 CLI/分层合同测试。批量执行受信任资产可使用 OS 测试工具
`python -m tests.package_manager.asset_behavior.run_locked_batch --help`，显式传 `--execute-behavior`；
该工具不提供沙箱，审阅设备命名空间与已有站点动作分开报告。下载、构建、静态绑定、离线运动、
前端控制、硬件分别统计 passed/failed/incomplete/not_run，最终报告说明未达到的层级。
