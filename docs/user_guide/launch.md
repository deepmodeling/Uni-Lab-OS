# Uni-Lab 启动指南

安装完毕后，可以通过 `unilab` 命令行启动：

```bash
Start Uni-Lab Edge server.

options:
  -h, --help            show this help message and exit
  -g GRAPH, --graph GRAPH
                        Physical setup graph file path.
  -c CONTROLLERS, --controllers CONTROLLERS
                        Controllers config file path.
  --registry_path REGISTRY_PATH
                        Path to the registry directory
  --working_dir WORKING_DIR
                        Path to the working directory
  --backend {hostlink,ros2}
                        Communication backend: hostlink (distributed, no DDS) or
                        ros2 (default).
  --legacy              兼容旧 Backend 的完整载荷 WebSocket 与旧 HTTP API；2026-12 移除
  --is_slave, --is-slave
                        Run the backend as slave node (without host privileges).
  --slave_no_host, --slave-no-host
                        Skip waiting for host service in slave mode
  --upload_registry     通过旧 Backend HTTP API 上报注册表（需要 --legacy）
  --config CONFIG       Configuration file path, supports .py format Python config files
  --port_management PORT_MANAGEMENT, --port-management PORT_MANAGEMENT, --port PORT_MANAGEMENT
                        微后端 HTTP API 与前端导航页端口，默认 8002
  --disable_browser, --disable-browser
                        只禁止自动打开浏览器，管理端服务仍会启动
  --2d_vis              Enable 2D visualization when starting pylabrobot instance
  --visual {rviz,web,disable}
                        Choose visualization tool: rviz, web, or disable
  --ak AK               Access key for laboratory requests
  --sk SK               Secret key for laboratory requests
  --addr ADDR           Laboratory backend address
  --skip_env_check      Skip environment dependency check on startup
  --complete_registry   Complete registry information
```

## 启动流程详解

Uni-Lab 的启动过程分为以下几个阶段：

### 1. 参数解析阶段

- 解析命令行参数
- 处理参数格式转换（支持 dash 和 underscore 格式）

### 2. 环境检查阶段 (可选)

- 默认进行环境依赖检查并自动安装必需包
- 使用 `--skip_env_check` 可跳过此步骤

### 3. 配置文件处理阶段

您可以直接跟随 unilabos 的提示进行，无需查阅本节

- **工作目录设置**：
  - 如果当前目录以 `unilabos_data` 结尾，则使用当前目录
  - 否则使用 `当前目录/unilabos_data` 作为工作目录
  - 可通过 `--working_dir` 指定自定义工作目录

- **配置文件查找顺序**：
  1. 使用 `--config` 参数指定的配置文件
  2. 在工作目录中查找 `local_config.py`
  3. 首次使用时会引导创建配置文件

### 4. 服务器地址配置

支持多种后端环境：

- `--addr test`：测试环境 (`https://leap-lab.test.bohrium.com/api/v1`)
- `--addr uat`：UAT 环境 (`https://leap-lab.uat.bohrium.com/api/v1`)
- `--addr local`：本地环境 (`http://127.0.0.1:48197/api/v1`)
- 自定义地址：直接指定完整 URL

### 5. 认证配置

- **必需参数**：`--ak` 和 `--sk` 必须同时提供
- 命令行参数优先于配置文件中的设置
- 未提供认证信息会导致启动失败并提示注册实验室

### 6. 设备图谱加载

支持两种方式：

- **本地文件**：使用 `-g` 指定图谱文件（支持 JSON 和 GraphML 格式）
- **远程资源**：不指定本地文件即可

### 7. 注册表构建

- 构建设备和资源注册表
- 支持自定义注册表路径 (`--registry_path`)
- 可选择补全注册表信息 (`--complete_registry`)

### 8. 设备验证和注册

- 验证设备连接和端点配置
- 自动注册设备到云端服务

### 9. 通信桥接配置

- **WebSocket**：实时通信和任务下发
- **FastAPI**：HTTP API 服务和物料更新

### 10. 可视化和服务启动

- 可选启动可视化工具 (`--visual`)
- 启动 Web 信息服务 (默认端口 8002)
- 启动后端通信服务

## 使用配置文件

Uni-Lab 支持使用 Python 格式的配置文件进行系统设置。通过 `--config` 参数指定配置文件路径：

```bash
# 使用配置文件启动
unilab --config path/to/your/config.py
```

配置文件包含实验室和 WebSocket 连接等设置。有关配置文件的详细信息，请参阅[配置指南](../advanced_usage/configuration.md)。

## 初始化信息来源

启动 Uni-Lab 时，可以选用两种方式之一配置实验室设备：

### 1. 组态&拓扑图

使用 `-g` 时，组态&拓扑图应包含实验室所有信息，详见{ref}`graph`。目前支持 GraphML 和 node-link JSON 两种格式。格式可参照 `tests/experiments` 下的启动文件。

### 2. 分别指定控制逻辑

使用 `-c` 传入控制逻辑配置。

不管使用哪一种初始化方式，设备/物料字典均需包含 `class` 属性，用于查找注册表信息。默认查找范围都是 Uni-Lab 内部注册表 `unilabos/registry/{devices,device_comms,resources}`。要添加额外的注册表路径，可以使用 `--registry_path` 加入 `<your-registry-path>/{devices,device_comms,resources}`，只输入<your-registry-path>即可，支持多次--registry_path指定多个目录。

## 通信中间件 `--backend`

Uni-Lab 对外提供两个通信 backend。名称、能力和实现入口由
`unilabos.app.backend.BACKEND_PROFILES` 统一管理：

| Backend | 定位 | 默认 App bridges | Host/Slave | 可视化 |
|---|---|---|---|---|
| **hostlink** | 本地 Python 驱动通过 HostLink TCP 组网，不启动 rclpy/DDS；可加载 ROS message 包并以 JSON 传输；支持设备发现、双向动作调用、Topic、状态，以及经 Host 代理微后端的权威物料 CRUD | 无 | 支持 | 不支持 |
| **ros2**（默认） | 完整 ROS 2 分布式运行时 | `websocket fastapi` | 支持 | 支持 |

典型启动命令：

```bash
# Python Link Host：监听 7302，并运行 host.json 中的本地驱动
unilab -g host.json --backend hostlink --hostlink-port 7302

# Python Link Slave：连接 Host，并发布 slave.json 中的设备/状态
unilab -g slave.json --backend hostlink --is-slave \
  --host-node-ip 192.168.1.10 --hostlink-port 7302

# 完整 ROS 2 运行时；不写 --backend 时也使用 ros2
unilab -g graph.json --backend ros2
```

`BasicRuntime` 仍作为 HostLink 内部的本地 Python 驱动执行引擎，但不能通过
`--backend basic` 独立选择。Dora 代码仅保留作实验，不属于公开部署 backend。
旧名称 `simple`、`ros` 以及 `basic`、`dora` 都不会被 CLI 接受。

## 端云通信与 `--legacy`

Host 的端云传输固定使用 WebSocket：正常模式只发送轻量变更通知，正文通过 HTTP
拉取。Host 本地微后端 HTTP API 固定启动，不再作为可选 bridge。连接仍使用完整
WebSocket payload 和 `/lab/*` 等旧 HTTP API 的旧 Backend 时，显式增加
`--legacy`；配置文件不再保存协议或 bridge 选择。`--legacy` 已废弃，计划在
2026-12-01 删除。

## 分布式组网

Host/Slave 可选择 `ros2` 或不启动 DDS 的 `hostlink` backend。启动时加入
`--is_slave` 将作为从站，不加将作为主站：

- **主站 (host)**：持有物料修改权以及对云端的通信
- **从站 (slave)**：无主机权限，可选择跳过等待主机服务 (`--slave_no_host`)

`ros2` 使用 DDS 上的 ROS Action/Topic；`hostlink` 直接在 TCP 长连接上同步注册表声明的设备描述、
状态，执行动作并转发 JSON Topic。设备代码可以继续使用通用节点的
`create_publisher(...).publish(...)` 和 `create_subscription(...)` 写法。

HostLink 可以加载 `std_msgs`、`geometry_msgs`、`unilabos_msgs` 等 ROS message Python 包，
使用其中的消息类和字段定义做类型解析。Topic、Action 参数、结果、feedback 和状态在发送时都会
递归转换为 UTF-8 JSON，因此消息类型本身不要求使用 DDS。驱动直接依赖 ROS graph、TF、RViz
插件或某个 rclpy Node/Service 时，仍需使用 `ros2`，或者先把该调用接入通用节点接口。

驱动需要在后台安排异步函数时，使用 `node.run_async_func(async_function, **kwargs)`；它会根据
当前 backend 选择 ROS executor 或 Python asyncio loop。不要在驱动中直接引用
`ROS2DeviceNode.run_async_func`。

推荐由 Host 统一发布 ROS2 domain，Slave 只指定 Host IP：

```bash
# Host：8002 是微后端 HTTP API 和前端导航页端口，7302 是 HostLink TCP
unilab -g host.json --port-management 8002 \
  --hostlink-port 7302 --ros-domain-id 42

# Slave：管理端口只影响本机 Web/API；HostLink 目标端口单独配置
unilab -g slave.json --is-slave \
  --host-node-ip 192.168.1.10 --hostlink-port 7302 --port-management 8003
```

主要组网参数：

- `--host-node-ip`：Slave 指定 Host IP/主机名。
- `--port-management` / `--port_management`：微后端 HTTP API 和前端导航页端口，默认 `8002`；`--port` 是兼容缩写。
- `--disable-browser` / `--disable_browser`：只禁止启动时自动打开浏览器，不会停止管理端口。
- `--hostlink-port`：HostLink TCP 端口，默认 `7302`，与管理端口独立。
- `--hostlink-bind` / `--hostlink-advertise-ip`：Host 监听地址与多网卡发布地址。
- `--ros-domain-id`：Host 下发给 Slave 的 ROS2 domain。
- `--ros-discovery-range` / `--ros-static-peers` / `--ros-discovery-server`：ROS2 发现策略。
- `--no-ros-assist`：仅用于 ROS2 backend；保留 HostLink 设备发现，不覆盖 Slave 的 ROS2 环境。
- `--disable-hostlink`：完全关闭 HostLink，使用原 ROS2 发现流程。

选择 `--backend hostlink` 时不能使用 `--disable-hostlink`，Slave 也必须提供
`--host-node-ip`。当前该 backend 不启动 `8002` 管理端或微前端；`7302` 只供
Host/Slave 进程通信。需要 Web/API 时仍使用 `ros2` backend。

浏览器和外部微前端访问 HTTP API 端口（默认 `8002`），不会访问 HostLink 的
`7302`。即使使用 `--disable-browser`，仍可手动访问 `http://<节点 IP>:8002/`
选择 API 工具或已登记的 GitHub Pages 前端。

## 可视化选项

### 2D 可视化

使用 `--2d_vis` 在 PyLabRobot 实例启动时同时启动 2D 可视化。

### 3D 可视化

通过 `--visual` 参数选择：

- **rviz**：使用 RViz 进行 3D 可视化
- **web**：使用 Web 界面进行可视化 (基于Pylabrobot)
- **disable** (默认)：禁用可视化

## 实验室管理

### 首次使用

如果是首次使用，系统会：

1. 提示前往 https://leap-lab.bohrium.com 注册实验室
2. 引导创建配置文件
3. 设置工作目录

### 认证设置

- `--ak`：实验室访问密钥
- `--sk`：实验室私钥
- 两者必须同时提供才能正常启动

## 完整启动示例

以下是一些常用的启动命令示例：

```bash
# 使用组态图启动，上传注册表
unilab --legacy --ak your_ak --sk your_sk -g path/to/graph.json --upload_registry

# 从旧 Backend 获取启动图
unilab --legacy --ak your_ak --sk your_sk

# 本地完整校验注册表
unilab --check_mode --complete_registry --skip_env_check

# 启动从站模式
unilab --ak your_ak --sk your_sk --is_slave

# 启用可视化
unilab --ak your_ak --sk your_sk --visual web --2d_vis

# 指定管理端口并禁止自动打开浏览器；HTTP/Web 服务仍在 8080 启动
unilab --ak your_ak --sk your_sk --port-management 8080 --disable-browser
```

## 常见问题

### 1. 认证失败

如果提示 "后续运行必须拥有一个实验室"，请确保：

- 已在 https://leap-lab.bohrium.com 注册实验室
- 正确设置了 `--ak` 和 `--sk` 参数
- 配置文件中包含正确的认证信息

### 2. 配置文件问题

如果配置文件加载失败：

- 确保配置文件是 `.py` 格式
- 检查配置文件语法是否正确
- 首次使用可让系统自动创建示例配置文件

### 3. 网络连接问题

如果无法连接到服务器：

- 检查网络连接
- 确认服务器地址是否正确
- 尝试使用不同的环境地址（test、uat、local）

### 4. 设备图谱问题

如果设备加载失败：

- 检查图谱文件格式是否正确
- 验证设备连接和端点配置
- 确保注册表路径正确
