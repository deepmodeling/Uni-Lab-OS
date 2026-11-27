# OS 桌面运行包

运行包交付内置 Python/ROS、UniLabOS wheel 和 developer-web。桌面通过 `python -m unilabos.workspace_host.launcher` 调用 OS，不承担设备业务或 PLC 仿真服务管理。

macOS 构建使用相邻 Installer 仓库的 `scripts/build-macos.py`；本目录 `build-runtime.py` 接受 `--output`、`--frontend`、`--reuse-runtime` 和 `--skip-frontend-build`。Windows、Linux 使用各自 `build-runtime-*.py` 和原有环境锁。运行包固定 OS 源码提交、源码状态、wheel 摘要与前端来源。

已安装环境的命令示例：

```sh
python -m unilabos.workspace_host.launcher --workspace /path/to/device-package \
  --bundle-root /path/to/os-bundle --bundled-runtime --control-stdin --options-stdin
```

标准输入第一行是设备包配置 JSON；后续每行 `shutdown` 请求正常停止，可在失败后重试。stdout 仅发送协议版本 2 的 JSON 事件：description、budget、progress、ready、error、stop_failed、stopped。stopped 在主线工作区成功停止且 Host 资源释放后发送；stop_failed 保留会话，不代表进程退出。设备连接与 OS 初始化就绪是不同事实。

桌面默认打开 `http://127.0.0.1:8012`。页面、API 和实时事件由同一 Backend 直接提供；可用 `--web-host` 和 `--web-port` 指定监听地址和端口。

源码运行可使用 `--dist` 指定前端产物。`--describe` 只读取声明，不创建 Host、工作目录或设备进程。参见 [接入说明](DEVICE-PACKAGE-INTEGRATION.md)。

工作区状态始终由主线 `WorkspacePaths` 定位到设备包内 `.unilabos`，不支持桌面专属 data-dir/runtime-root。Host 公开本机 HTTP 接口供 CLI 使用；Backend 和 Edge 由主线操作队列启动、监控、恢复和停止。

仿真器须在独立环境手动启动。`smoke-runtime.py` 可验证真实打包运行时的设备列表、工作流列表和正常停止，使用临时设备包副本避免写入现有工作区状态。
