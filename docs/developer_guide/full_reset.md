# 本机全量重置

此操作与普通 `scope=auto` 重启不同：普通重启只重启 Host，完整重置需要权威和 Host
全部停止、关闭数据库连接，之后才移动活动数据。仅默认本机分离部署提供该能力。

浏览器先 GET `/api/v1/reset` 预览范围和本进程确认令牌，再 POST 相同路径，提交
`confirmation_token` 与 `confirmation: "清空全部数据"`。202 只是受理，不是完成。
运行中作业、在线 Slave、安装操作或并发重启都会阻止操作；先停止它们。

清理范围：权威及 `<数据库根>/edge` 的 runtime/materials/telemetry/history 四库
及 WAL/SHM，工作目录的 device_processes.json 和 device_processes 目录。
包含设备、物料、设备图、工作流、任务、遥测、历史、布局及受管进程规格。
不改用户的源图文件，不删除驱动包源码及台账，不递归删除工作目录。

数据移入 `<数据库根>/reset-backups/<随机标识>`，manifest.json 记录原始路径与备份路径。
看到 `state=completed` 和控制台「重置完成」后，不带旧 `-g` 重新运行 unilab。
这是可恢复归档，不是安全擦除；需要恢复时先停机，按清单恢复对应文件，禁止覆盖运行中的库。
显式重新加载原图、重新接入外部 Slave 会重新创建设备，不属于重置残留。

若归档失败，reset-pending.json 留在数据库根，下次启动拒绝恢复旧任务。
根据其中 moved/next 与备份 manifest 核对文件，完成归档或恢复后再处理该标记。
API 不接收任意路径，也不支持自定义库路径、目录联接或远端库的全量擦除。
