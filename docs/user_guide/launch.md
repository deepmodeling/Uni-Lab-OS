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
                        可选运行目录；工作区启动默认 <workspace>/.unilabos
  --backend {ros,simple,automancer}
                        Choose the backend to run with: 'ros', 'simple', or 'automancer'.
  --app_bridges APP_BRIDGES [APP_BRIDGES ...]
                        Bridges to connect to: 'websocket' and 'fastapi'.
  --is_slave            Run the backend as slave node (without host privileges).
  --hostlink_addr HOSTLINK_ADDR
                        Slave 连接的 Host 微后端或 Host 监听地址（默认端口 7302）。
  --ros_domain_id ROS_DOMAIN_ID
                        Host 通过微后端握手下发给 Slave 的 ROS domain id。
  --ros_discovery_port ROS_DISCOVERY_PORT
                        Host 托管的 Fast DDS UDP 端口；0 表示复用 HostLink 数字端口。
  --ros_discovery_server ROS_DISCOVERY_SERVER
                        外部 Fast DDS ip:port；传 off 可禁用 Host 托管服务。
  --slave_no_host       显式允许 Slave 离线启动；HostLink 仍在后台重连。
  --use_remote_resource 兼容旧云端版本，复用已上传的远程资源图。
  --config CONFIG       Configuration file path, supports .py format Python config files
  --port PORT           Port for web service information page
  --disable_browser     Disable opening information page on startup
  --visual {rviz,web,disable}
                        Choose visualization tool: rviz, web, or disable
  --ak AK               Access key for laboratory requests
  --sk SK               Secret key for laboratory requests
  --addr ADDR           Laboratory backend address
  --action_mode {real,simulate}
                        动作执行模式；simulate 不调用真实硬件，但仍推进任务状态。
  --complete_registry   Complete registry information
```

## 启动流程详解

Uni-Lab 的启动过程分为以下几个阶段：

### 1. 参数解析阶段

- 解析命令行参数
- 处理参数格式转换（支持 dash 和 underscore 格式）

### 2. 环境依赖保障阶段

- 默认进行环境依赖检查并自动安装必需包
- 工作区（Workspace）确需跳过时，在 `pyproject.toml` 中显式声明：

  ```toml
  [tool.unilabos.startup]
  ensure_dependencies = false
  ```

  该策略只关闭 Python 环境依赖检查和自动补齐，不改变 ROS2、OPC 或动作
  （Action）执行模式。

### 3. 配置文件处理阶段

您可以直接跟随 unilabos 的提示进行，无需查阅本节

- **工作目录设置**：
  - 工作区（Workspace）启动默认使用 `<workspace>/.unilabos`
  - 无工作区、无配置启动默认使用 `当前目录/.unilabos`
  - 新目录不存在但旧 `unilabos_data` 已存在时继续兼容旧目录
  - 可通过 `--working_dir` 指定自定义工作目录

- **配置文件查找顺序**：
  1. 使用 `--config` 参数指定的配置文件
  2. 在工作目录中查找 `local_config.py`
  3. 首次使用时会引导创建配置文件

### 4. 旧云端与独立同步子命令地址

`--addr` 不改变 OS 的本地后端权威，也不会让 OS 常驻进程代理正式后端
（Backend）。它只供显式 `--use_remote_resource` 旧云端兼容和独立同步子命令使用：

- `--addr test`：测试环境 (`https://leap-lab.test.bohrium.com/api/v1`)
- `--addr uat`：UAT 环境 (`https://leap-lab.uat.bohrium.com/api/v1`)
- `--addr local`：本地环境 (`http://127.0.0.1:48197/api/v1`)
- 自定义地址：直接指定完整 URL

### 5. 旧云端认证配置

- `--ak` 和 `--sk` 只在上述旧云端或独立上传流程中同时提供
- 命令行参数优先于配置文件中的设置
- 普通本地 OS 启动不需要认证信息，也不会因此连接远端

### 5.1 OS 本地后端权威

Host 固定启用 `app/scheduler` 和本地库存权威（Inventory Authority），无需额外参数：

- Edge Scheduler：DAG 拆解、锁和重排；
- `inventory.db`：物料、关系、预留和账本；
- `device_state.db`：设备属性当前值和变化历史；
- `workflow_history.db`：工作流和 Job 执行审计。
- HostLink：监听全部 Slave、维护连接与心跳、代理物料请求；
- ROS 网络策略：由微后端在握手中统一下发 domain、发现范围、静态对端和
  Discovery Server。

前端默认连接 Host `:18003` 即可使用调度、实体、设备状态和
工作流历史接口：

```bash
unilab -g graph.json
```

三个数据库路径由同一个运行目录自动确定：

```bash
unilab -g graph.json --working_dir /data/unilabos-runtime
# /data/unilabos-runtime/{inventory,device_state,workflow_history}.db
```

后端权威只在前端连接配置中选择一次：选择 OS 时，全部服务读取 OS 的
后端形态契约（Backend-shaped Contract）；选择正式后端（Backend）时，前端直接
请求正式后端。OS 不接受正式后端作为物料（Material）查询上游，也不存在本地未
命中后自动回源。

Slave 不启动物料服务，也不会打开运行目录中的 SQLite；它只能经 HostLink 请求
Host 微后端持有的物料服务：

```bash
unilab --is_slave --hostlink_addr 192.168.1.10:7302 -g slave-graph.json
```

Host 微后端先于 ROS HostNode 启动并监听 `0.0.0.0:7302`。HostNode 创建后只挂接
运行时资源树，不再创建 TCP 服务或决定 ROS 网络策略。普通 Slave 会持续等待 Host，
在 `rclpy.init` 前完成握手并应用 Host 下发配置，因而不会出现“先按本地 ROS 启动、
后连上 Host 却无法应用配置”的半连接状态。仅显式传入 `--slave_no_host` 时允许离线
启动；该模式使用本地 ROS 配置，同时 HostLink 在后台持续重连。

默认端口模式是定向且便于部署的：HostLink 在 TCP `7302` 监听控制面，同时 Fast DDS
Discovery Server 在 UDP `7302` 监听 ROS 发现；Slave 只需一个
`--hostlink_addr 192.168.1.10:7302` 即可得到 IP、端口和 domain。TCP 与 UDP 可使用
同一数字端口。如网络策略要求分开，可在 Host 增加 `--ros_discovery_port 11811`；已有
独立发现服务时用 `--ros_discovery_server ip:port`；排障时用
`--ros_discovery_server off` 保留原 ROS 发现方式。

Slave 会在握手中报送本次启动图里所有 `type=device` 的设备 ID。设备 ID 全网唯一，因此
Host 以 `device_ids` 作为逻辑 Slave 身份：同一设备集合重连不会产生重复 peer，同一
台机器启动不同设备集合则会显示为不同 Slave。运行时 Slave 的启动图至少要包含一个
设备；空图会直接启动失败，测试应使用 `virtual_*` / `*.mock` 设备。Host 空图仍然合法，
因为它可以只承担微后端、调度和多 Slave 管理。协议层的旧客户端才回退到机器名。

### 6. 设备图谱加载

支持两种方式：

- **本地文件**：使用 `-g` 指定图谱文件（支持 JSON 和 GraphML 格式）
- **旧云端远程资源**：显式提供 `--use_remote_resource`；普通启动不会因缺少
  本地文件而隐式连接后端（Backend）

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

配置文件包含实验室和 WebSocket 连接等设置。有关配置文件的详细信息，请参阅[配置指南](configuration.md)。

## 初始化信息来源

启动 Uni-Lab 时，可以选用两种方式之一配置实验室设备：

### 1. 组态&拓扑图

使用 `-g` 时，组态&拓扑图应包含实验室所有信息，详见{ref}`graph`。目前支持 GraphML 和 node-link JSON 两种格式。格式可参照 `tests/experiments` 下的启动文件。

### 2. 分别指定控制逻辑

使用 `-c` 传入控制逻辑配置。

不管使用哪一种初始化方式，设备/物料字典均需包含 `class` 属性，用于查找注册表信息。默认查找范围都是 Uni-Lab 内部注册表 `unilabos/registry/{devices,device_comms,resources}`。要添加额外的注册表路径，可以使用 `--registry_path` 加入 `<your-registry-path>/{devices,device_comms,resources}`，只输入<your-registry-path>即可，支持多次--registry_path指定多个目录。

## 通信中间件 `--backend`

目前 Uni-Lab 支持以下通信中间件：

- **ros** (默认)：基于 ROS2 的通信
- **automancer**：Automancer 兼容模式 (实验性)

## 端云桥接 `--app_bridges`

目前 Uni-Lab 提供以下端云通信方式：

- **websocket**：旧协议和 Edge 独立运行测试使用
- **FastAPI**：提供 OS 本地后端形态 HTTP API

`unilab` 主启动不提供 `edge_control` 或正式后端（Backend）代理模式。旧云端
资源复用只保留显式 `--use_remote_resource` 遗留兼容入口。

## 分布式组网

启动 Uni-Lab 时，加入 `--is_slave` 将作为从站，不加将作为主站：

- **主站 (host)**：Edge 微后端持有数据库、监听所有 Slave、下发 ROS 配置；
  普通 OS 模式不连接正式后端（Backend）
- **从站 (slave)**：不持有数据库文件；物料查询只能经 HostLink 访问 Host 微后端。
  默认必须等待 Host；仅故障排查或隔离测试时用 `--slave_no_host` 离线降级

为 Slave 配置 Host 的 `--hostlink_addr` 后，局域网内的 Host 与多个 Slave 会建立
统一组网。HostLink 负责身份、心跳、物料查询和定向发现参数；设备指令与结果仍由
HostNode 通过 ROS Action 收发。只有 ROS Action endpoint 已匹配的设备才会显示为在线。

## 可视化选项

### 2D 可视化

在部署配置的 `BasicConfig` 中设置 `vis_2d_enable = True`，即可在 PyLabRobot
实例启动时同时启动 2D 可视化。

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
# 独立初始化步骤 1：以开发者身份事务性同步设备和器材模板
UNILAB_TEMPLATE_SYNC_DEVELOPER_TOKEN=developer-token \
  unilab --addr https://backend.example/api/v1 template-sync

# 独立初始化步骤 2：通过正式后端接口创建组态图中的设备和器材实例
UNILAB_INSTANCE_SYNC_TOKEN=instance-token \
  unilab --addr https://backend.example/api/v1 \
  --graph path/to/graph.json instance-sync

# 生产 Edge 启动前只读检查实例；常驻进程不持有上述写入 Token
unilab --addr https://backend.example/api/v1 \
  --graph path/to/graph.json instance-sync --check_only

# 使用远程资源启动
unilab --use_remote_resource --ak your_ak --sk your_sk

# 更新注册表
unilab --ak your_ak --sk your_sk --complete_registry

# 启动从站模式
unilab --ak your_ak --sk your_sk --is_slave

# 启用 3D 可视化
unilab --ak your_ak --sk your_sk --visual web

# 指定本地信息网页服务端口和禁用自动跳出浏览器
unilab --ak your_ak --sk your_sk --port 8080 --disable_browser
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
