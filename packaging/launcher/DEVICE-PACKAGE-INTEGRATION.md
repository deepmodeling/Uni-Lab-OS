# 设备包桌面接入

设备包保留主线 `--workspace` 加载机制，根目录使用以下纯数据入口：

```toml
schema_version = 2
package_id = "example-lab"
display_name = "示例实验室"
[runtime]
file = "unilab-app.json"
```

```json
{
  "schemaVersion": 2,
  "configuration": [{
    "key": "deploymentGraph", "label": "设备图文件（JSON）", "type": "file",
    "required": true, "default": "deployment/graphs/local-sim.json", "extensions": ["json"]
  }],
  "startup": {
    "graph": "${options.deploymentGraph}", "config": "deployment/local_config.py",
    "backend": "ros", "actionMode": "real",
    "extraArgs": ["--external_devices_only", "--ros_discovery_server", "off"]
  }
}
```

当前桌面入口复用主线 ROS 工作区启动计划，支持选择包内 JSON 图、主线固定配置及 `real`/`simulate` 动作模式。配置、注册表和额外参数不匹配主线计划时明确报错，不生成另一套启动命令。`configuration` 支持 file/string/integer/boolean/select；只对声明参数插值，不修改设备图正文。路径必须属于设备包。

运行状态使用设备包内 `.unilabos`，GUI 与 CLI 共用 Host 锁、发现记录及数据库选择。当前已有 Host 时桌面拒绝重复启动。桌面常驻 OS 进程为 Host、Backend、Edge；没有适配器或服务 worker。

外部 Modbus、TCP、OPC 仿真服务由用户独立运行，声明不得包含非空 `services`，否则提示移除。启动器不启动、停止或独立探测这些服务。设备驱动照常访问图内端点。`real` 表示执行驱动；图内 `simulation=false` 表示使用 TCP，并不决定对端是真机还是仿真器。

OS ready 表示主线设备初始化阶段完成，不证明 PLC 已连接。连接和动作失败沿用驱动行为，不加入端口预检或自动降级。

正常停止使用主线排空流程。排空失败或停止结果未知时，保留三个 OS 进程及控制句柄，可重试；关闭窗口仅隐藏，正常停止成功后才能退出应用。
