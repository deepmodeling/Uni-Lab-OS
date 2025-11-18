# Uni-Lab 配置指南

本文档详细介绍 Uni-Lab 配置文件的结构、配置项、命令行覆盖和环境变量的使用方法。

## 配置文件概述

Uni-Lab 使用 Python 格式的配置文件（`.py`），默认为 `unilabos_data/local_config.py`。配置文件采用类属性的方式定义各种配置项，比 YAML 或 JSON 提供更多的灵活性，包括支持注释、条件逻辑和复杂数据结构。

## 获取实验室密钥

在配置文件或启动命令中，您需要提供实验室的访问密钥（ak）和私钥（sk）。

**获取方式：**

进入 [Uni-Lab 实验室](https://uni-lab.bohrium.com)，点击左下角的头像，在实验室详情中获取所在实验室的 ak 和 sk：

![copy_aksk.gif](image/copy_aksk.gif)

## 配置文件格式

### 默认配置示例

首次使用时，系统会自动创建一个基础配置文件 `local_config.py`：

```python
# unilabos的配置文件

class BasicConfig:
    ak = ""  # 实验室网页给您提供的ak代码
    sk = ""  # 实验室网页给您提供的sk代码


# WebSocket配置，一般无需调整
class WSConfig:
    reconnect_interval = 5  # 重连间隔（秒）
    max_reconnect_attempts = 999  # 最大重连次数
    ping_interval = 30  # ping间隔（秒）
```

### 完整配置示例

您可以根据需要添加更多配置选项：

```python
#!/usr/bin/env python
# coding=utf-8
"""Uni-Lab 配置文件"""

# 基础配置
class BasicConfig:
    ak = ""  # 实验室访问密钥
    sk = ""  # 实验室私钥
    working_dir = ""  # 工作目录（通常自动设置）
    config_path = ""  # 配置文件路径（自动设置）
    is_host_mode = True  # 是否为主站模式
    slave_no_host = False  # 从站模式下是否跳过等待主机服务
    upload_registry = False  # 是否上传注册表
    machine_name = "undefined"  # 机器名称（自动获取）
    vis_2d_enable = False  # 是否启用2D可视化
    enable_resource_load = True  # 是否启用资源加载
    communication_protocol = "websocket"  # 通信协议
    log_level = "DEBUG"  # 日志级别：TRACE, DEBUG, INFO, WARNING, ERROR, CRITICAL

# WebSocket配置
class WSConfig:
    reconnect_interval = 5  # 重连间隔（秒）
    max_reconnect_attempts = 999  # 最大重连次数
    ping_interval = 30  # ping间隔（秒）

# OSS上传配置
class OSSUploadConfig:
    api_host = ""  # API主机地址
    authorization = ""  # 授权信息
    init_endpoint = ""  # 初始化端点
    complete_endpoint = ""  # 完成端点
    max_retries = 3  # 最大重试次数

# HTTP配置
class HTTPConfig:
    remote_addr = "https://uni-lab.bohrium.com/api/v1"  # 远程服务器地址

# ROS配置
class ROSConfig:
    modules = [
        "std_msgs.msg",
        "geometry_msgs.msg",
        "control_msgs.msg",
        "control_msgs.action",
        "nav2_msgs.action",
        "unilabos_msgs.msg",
        "unilabos_msgs.action",
    ]  # 需要加载的ROS模块
```

## 配置优先级

配置项的生效优先级从高到低为：

1. **命令行参数**：最高优先级
2. **环境变量**：中等优先级
3. **配置文件**：基础优先级

这意味着命令行参数会覆盖环境变量和配置文件，环境变量会覆盖配置文件。

## 推荐配置方式

根据参数特性，不同配置项有不同的推荐配置方式：

### 建议通过命令行指定的参数（不需要写入配置文件）

以下参数推荐通过命令行或环境变量指定，**一般不需要在配置文件中配置**：

| 参数              | 命令行参数          | 原因                                 |
| ----------------- | ------------------- | ------------------------------------ |
| `ak` / `sk`       | `--ak` / `--sk`     | **安全考虑**：避免敏感信息泄露       |
| `working_dir`     | `--working_dir`     | **灵活性**：不同环境可能使用不同目录 |
| `is_host_mode`    | `--is_slave`        | **运行模式**：由启动场景决定，不固定 |
| `slave_no_host`   | `--slave_no_host`   | **运行模式**：从站特殊配置，按需使用 |
| `upload_registry` | `--upload_registry` | **临时操作**：仅首次启动或更新时需要 |
| `vis_2d_enable`   | `--2d_vis`          | **调试功能**：按需临时启用           |
| `remote_addr`     | `--addr`            | **环境切换**：测试/生产环境快速切换  |

**推荐用法示例：**

```bash
# 标准启动命令（所有必要参数通过命令行指定）
unilab --ak your_ak --sk your_sk -g graph.json

# 测试环境
unilab --addr test --ak your_ak --sk your_sk -g graph.json

# 从站模式
unilab --is_slave --ak your_ak --sk your_sk

# 首次启动上传注册表
unilab --ak your_ak --sk your_sk -g graph.json --upload_registry
```

### 适合在配置文件中配置的参数

以下参数适合在配置文件中配置，通常不会频繁更改：

| 参数                     | 配置类      | 说明                   |
| ------------------------ | ----------- | ---------------------- |
| `log_level`              | BasicConfig | 日志级别配置           |
| `reconnect_interval`     | WSConfig    | WebSocket 重连间隔     |
| `max_reconnect_attempts` | WSConfig    | WebSocket 最大重连次数 |
| `ping_interval`          | WSConfig    | WebSocket 心跳间隔     |
| `modules`                | ROSConfig   | ROS 模块列表           |

**配置文件示例（推荐最小配置）：**

```python
# unilabos的配置文件

class BasicConfig:
    log_level = "INFO"  # 生产环境建议 INFO，调试时用 DEBUG

# WebSocket配置，一般保持默认即可
class WSConfig:
    reconnect_interval = 5
    max_reconnect_attempts = 999
    ping_interval = 30
```

**注意：** `ak` 和 `sk` 不建议写在配置文件中，始终通过命令行参数或环境变量传递。

## 命令行参数覆盖配置

Uni-Lab 允许通过命令行参数覆盖配置文件中的设置，提供更灵活的配置方式。

### 支持命令行覆盖的配置项

| 配置类        | 配置字段          | 命令行参数          | 说明                             |
| ------------- | ----------------- | ------------------- | -------------------------------- |
| `BasicConfig` | `ak`              | `--ak`              | 实验室访问密钥                   |
| `BasicConfig` | `sk`              | `--sk`              | 实验室私钥                       |
| `BasicConfig` | `working_dir`     | `--working_dir`     | 工作目录路径                     |
| `BasicConfig` | `is_host_mode`    | `--is_slave`        | 主站模式（参数为从站模式，取反） |
| `BasicConfig` | `slave_no_host`   | `--slave_no_host`   | 从站模式下跳过等待主机服务       |
| `BasicConfig` | `upload_registry` | `--upload_registry` | 启动时上传注册表信息             |
| `BasicConfig` | `vis_2d_enable`   | `--2d_vis`          | 启用 2D 可视化                   |
| `HTTPConfig`  | `remote_addr`     | `--addr`            | 远程服务地址                     |

### 特殊命令行参数

除了直接覆盖配置项的参数外，还有一些特殊的命令行参数：

| 参数                | 说明                                 |
| ------------------- | ------------------------------------ |
| `--config`          | 指定配置文件路径                     |
| `--port`            | Web 服务端口（不影响配置文件）       |
| `--disable_browser` | 禁用自动打开浏览器（不影响配置文件） |
| `--visual`          | 可视化工具选择（不影响配置文件）     |
| `--skip_env_check`  | 跳过环境检查（不影响配置文件）       |

### 命令行覆盖使用示例

```bash
# 通过命令行覆盖认证信息
unilab --ak "new_access_key" --sk "new_secret_key" -g graph.json

# 覆盖服务器地址
unilab --ak ak --sk sk --addr "https://custom.server.com/api/v1" -g graph.json

# 启用从站模式并跳过等待主机
unilab --is_slave --slave_no_host --ak ak --sk sk

# 启用上传注册表和2D可视化
unilab --upload_registry --2d_vis --ak ak --sk sk -g graph.json

# 组合使用多个覆盖参数
unilab --ak "key" --sk "secret" --addr "test" --upload_registry --2d_vis -g graph.json
```

### 预设环境地址

`--addr` 参数支持以下预设值，会自动转换为对应的完整 URL：

- `test` → `https://uni-lab.test.bohrium.com/api/v1`
- `uat` → `https://uni-lab.uat.bohrium.com/api/v1`
- `local` → `http://127.0.0.1:48197/api/v1`
- 其他值 → 直接使用作为完整 URL

## 配置选项详解

### 1. BasicConfig - 基础配置

基础配置包含了系统运行的核心参数：

| 参数                     | 类型 | 默认值        | 说明                                       |
| ------------------------ | ---- | ------------- | ------------------------------------------ |
| `ak`                     | str  | `""`          | 实验室访问密钥（必需）                     |
| `sk`                     | str  | `""`          | 实验室私钥（必需）                         |
| `working_dir`            | str  | `""`          | 工作目录，通常自动设置                     |
| `config_path`            | str  | `""`          | 配置文件路径，自动设置                     |
| `is_host_mode`           | bool | `True`        | 是否为主站模式                             |
| `slave_no_host`          | bool | `False`       | 从站模式下是否跳过等待主机服务             |
| `upload_registry`        | bool | `False`       | 启动时是否上传注册表信息                   |
| `machine_name`           | str  | `"undefined"` | 机器名称，自动从 hostname 获取（不可配置） |
| `vis_2d_enable`          | bool | `False`       | 是否启用 2D 可视化                         |
| `enable_resource_load`   | bool | `True`        | 是否启用资源加载                           |
| `communication_protocol` | str  | `"websocket"` | 通信协议，固定为 websocket                 |
| `log_level`              | str  | `"DEBUG"`     | 日志级别                                   |

#### 日志级别选项

- `TRACE` - 追踪级别（最详细）
- `DEBUG` - 调试级别（默认）
- `INFO` - 信息级别
- `WARNING` - 警告级别
- `ERROR` - 错误级别
- `CRITICAL` - 严重错误级别（最简略）

#### 认证配置（ak / sk）

`ak` 和 `sk` 是必需的认证参数：

1. **获取方式**：在 [Uni-Lab 官网](https://uni-lab.bohrium.com) 注册实验室后获得
2. **配置方式**：
   - **命令行参数**：`--ak "your_key" --sk "your_secret"`（最高优先级，推荐）
   - **环境变量**：`UNILABOS_BASICCONFIG_AK` 和 `UNILABOS_BASICCONFIG_SK`
   - **配置文件**：在 `BasicConfig` 类中设置（不推荐，安全风险）
3. **安全注意**：请妥善保管您的密钥信息，不要提交到版本控制

**推荐做法**：

- **开发环境**：使用命令行参数或环境变量
- **生产环境**：使用环境变量
- **临时测试**：使用命令行参数

### 2. WSConfig - WebSocket 配置

WebSocket 是 Uni-Lab 的主要通信方式：

| 参数                     | 类型 | 默认值 | 说明               |
| ------------------------ | ---- | ------ | ------------------ |
| `reconnect_interval`     | int  | `5`    | 断线重连间隔（秒） |
| `max_reconnect_attempts` | int  | `999`  | 最大重连次数       |
| `ping_interval`          | int  | `30`   | 心跳检测间隔（秒） |

### 3. HTTPConfig - HTTP 配置

HTTP 客户端配置用于与云端服务通信：

| 参数          | 类型 | 默认值                                 | 说明         |
| ------------- | ---- | -------------------------------------- | ------------ |
| `remote_addr` | str  | `"https://uni-lab.bohrium.com/api/v1"` | 远程服务地址 |

**预设环境地址**：

- 生产环境：`https://uni-lab.bohrium.com/api/v1`（默认）
- 测试环境：`https://uni-lab.test.bohrium.com/api/v1`
- UAT 环境：`https://uni-lab.uat.bohrium.com/api/v1`
- 本地环境：`http://127.0.0.1:48197/api/v1`

### 4. OSSUploadConfig - OSS 上传配置

对象存储服务配置，用于文件上传功能：

| 参数                | 类型 | 默认值 | 说明                 |
| ------------------- | ---- | ------ | -------------------- |
| `api_host`          | str  | `""`   | OSS API 主机地址     |
| `authorization`     | str  | `""`   | 授权认证信息         |
| `init_endpoint`     | str  | `""`   | 上传初始化端点       |
| `complete_endpoint` | str  | `""`   | 上传完成端点         |
| `max_retries`       | int  | `3`    | 上传失败最大重试次数 |

### 5. ROSConfig - ROS 配置

配置 ROS 消息转换器需要加载的模块：

| 配置项    | 类型 | 默认值     | 说明         |
| --------- | ---- | ---------- | ------------ |
| `modules` | list | 见下方示例 | ROS 模块列表 |

**默认模块列表：**

```python
class ROSConfig:
    modules = [
        "std_msgs.msg",           # 标准消息类型
        "geometry_msgs.msg",      # 几何消息类型
        "control_msgs.msg",       # 控制消息类型
        "control_msgs.action",    # 控制动作类型
        "nav2_msgs.action",       # 导航动作类型
        "unilabos_msgs.msg",      # UniLab 自定义消息类型
        "unilabos_msgs.action",   # UniLab 自定义动作类型
    ]
```

您可以根据实际使用的设备和功能添加其他 ROS 模块。

## 环境变量配置

Uni-Lab 支持通过环境变量覆盖配置文件中的设置。

### 环境变量命名规则

```
UNILABOS_<配置类名>_<配置项名>
```

**注意：**

- 环境变量名不区分大小写
- 配置类名和配置项名都会转换为大写进行匹配

### 设置环境变量

#### Linux / macOS

```bash
# 临时设置（当前终端）
export UNILABOS_BASICCONFIG_LOG_LEVEL=INFO
export UNILABOS_BASICCONFIG_AK="your_access_key"
export UNILABOS_BASICCONFIG_SK="your_secret_key"

# 永久设置（添加到 ~/.bashrc 或 ~/.zshrc）
echo 'export UNILABOS_BASICCONFIG_LOG_LEVEL=INFO' >> ~/.bashrc
source ~/.bashrc
```

#### Windows (cmd)

```cmd
# 临时设置
set UNILABOS_BASICCONFIG_LOG_LEVEL=INFO
set UNILABOS_BASICCONFIG_AK=your_access_key

# 永久设置（系统环境变量）
setx UNILABOS_BASICCONFIG_LOG_LEVEL INFO
```

#### Windows (PowerShell)

```powershell
# 临时设置
$env:UNILABOS_BASICCONFIG_LOG_LEVEL="INFO"
$env:UNILABOS_BASICCONFIG_AK="your_access_key"

# 永久设置
[Environment]::SetEnvironmentVariable("UNILABOS_BASICCONFIG_LOG_LEVEL", "INFO", "User")
```

### 环境变量类型转换

系统会根据配置项的原始类型自动转换环境变量值：

| 原始类型 | 转换规则                                |
| -------- | --------------------------------------- |
| `bool`   | "true", "1", "yes" → True；其他 → False |
| `int`    | 转换为整数                              |
| `float`  | 转换为浮点数                            |
| `str`    | 直接使用字符串值                        |

**示例：**

```bash
# 布尔值
export UNILABOS_BASICCONFIG_IS_HOST_MODE=true  # 将设置为 True
export UNILABOS_BASICCONFIG_IS_HOST_MODE=false  # 将设置为 False

# 整数
export UNILABOS_WSCONFIG_RECONNECT_INTERVAL=10  # 将设置为 10

# 字符串
export UNILABOS_BASICCONFIG_LOG_LEVEL=INFO  # 将设置为 "INFO"
```

### 环境变量示例

```bash
# 设置基础配置
export UNILABOS_BASICCONFIG_AK="your_access_key"
export UNILABOS_BASICCONFIG_SK="your_secret_key"
export UNILABOS_BASICCONFIG_IS_HOST_MODE="true"

# 设置WebSocket配置
export UNILABOS_WSCONFIG_RECONNECT_INTERVAL="10"
export UNILABOS_WSCONFIG_MAX_RECONNECT_ATTEMPTS="500"

# 设置HTTP配置
export UNILABOS_HTTPCONFIG_REMOTE_ADDR="https://uni-lab.test.bohrium.com/api/v1"
```

## 配置文件使用方法

### 1. 使用默认配置文件（推荐）

系统会自动查找并加载配置文件：

```bash
# 直接启动，使用默认的 unilabos_data/local_config.py
unilab --ak your_ak --sk your_sk -g graph.json
```

查找顺序：

1. 环境变量 `UNILABOS_BASICCONFIG_CONFIG_PATH` 指定的路径
2. 工作目录下的 `local_config.py`
3. 首次使用时会引导创建配置文件

### 2. 指定配置文件启动

```bash
# 使用指定配置文件启动
unilab --config /path/to/your/config.py --ak ak --sk sk -g graph.json
```

### 3. 配置文件验证

系统启动时会自动验证配置文件：

- **语法检查**：确保 Python 语法正确
- **类型检查**：验证配置项类型是否匹配
- **加载确认**：控制台输出加载成功信息

## 常用配置场景

### 场景 1：调整日志级别

**配置文件方式：**

```python
class BasicConfig:
    log_level = "INFO"  # 生产环境建议使用 INFO 或 WARNING
```

**环境变量方式：**

```bash
export UNILABOS_BASICCONFIG_LOG_LEVEL=INFO
unilab --ak ak --sk sk -g graph.json
```

**命令行方式**（需要配置文件已包含）：

```bash
# 配置文件无直接命令行参数，需通过环境变量
UNILABOS_BASICCONFIG_LOG_LEVEL=INFO unilab --ak ak --sk sk -g graph.json
```

### 场景 2：配置 WebSocket 重连

**配置文件方式：**

```python
class WSConfig:
    reconnect_interval = 10  # 增加重连间隔到 10 秒
    max_reconnect_attempts = 100  # 减少最大重连次数到 100 次
```

**环境变量方式：**

```bash
export UNILABOS_WSCONFIG_RECONNECT_INTERVAL=10
export UNILABOS_WSCONFIG_MAX_RECONNECT_ATTEMPTS=100
```

### 场景 3：切换服务器环境

**配置文件方式：**

```python
class HTTPConfig:
    remote_addr = "https://uni-lab.test.bohrium.com/api/v1"
```

**环境变量方式：**

```bash
export UNILABOS_HTTPCONFIG_REMOTE_ADDR=https://uni-lab.test.bohrium.com/api/v1
```

**命令行方式（推荐）：**

```bash
unilab --addr test --ak your_ak --sk your_sk -g graph.json
```

### 场景 4：从站模式配置

**配置文件方式：**

```python
class BasicConfig:
    is_host_mode = False  # 从站模式
    slave_no_host = True  # 不等待主机服务
```

**命令行方式（推荐）：**

```bash
unilab --is_slave --slave_no_host --ak your_ak --sk your_sk
```

## 最佳实践

### 1. 安全配置

**不要在配置文件中存储敏感信息**

- ❌ **不推荐**：在配置文件中明文存储 ak/sk
- ✅ **推荐**：使用环境变量或命令行参数

```bash
# 生产环境 - 使用环境变量（推荐）
export UNILABOS_BASICCONFIG_AK="your_access_key"
export UNILABOS_BASICCONFIG_SK="your_secret_key"
unilab -g graph.json

# 或使用命令行参数
unilab --ak "your_access_key" --sk "your_secret_key" -g graph.json
```

**其他安全建议：**

- 不要将包含密钥的配置文件提交到版本控制系统
- 限制配置文件权限：`chmod 600 local_config.py`
- 定期更换访问密钥
- 使用 `.gitignore` 排除配置文件

### 2. 多环境配置

为不同环境创建不同的配置文件：

```
configs/
├── base_config.py       # 基础配置（非敏感）
├── dev_config.py        # 开发环境
├── test_config.py       # 测试环境
├── prod_config.py       # 生产环境
└── example_config.py    # 示例配置
```

**环境切换示例**：

```bash
# 本地开发环境
unilab --config configs/dev_config.py --addr local --ak ak --sk sk -g graph.json

# 测试环境
unilab --config configs/test_config.py --addr test --ak ak --sk sk --upload_registry -g graph.json

# 生产环境
unilab --config configs/prod_config.py --ak "$PROD_AK" --sk "$PROD_SK" -g graph.json
```

### 3. 配置管理

**配置文件最佳实践：**

- 保持配置文件简洁，只包含需要修改的配置项
- 为配置项添加注释说明其作用
- 定期检查和更新配置文件
- 版本控制仅保存示例配置，不包含实际密钥

**命令行参数优先使用场景：**

- 临时测试不同配置
- CI/CD 流水线中的动态配置
- 不同环境间快速切换
- 敏感信息的安全传递

### 4. 灵活配置策略

**基础配置文件 + 命令行覆盖**的推荐方式：

```python
# base_config.py - 基础配置（非敏感信息）
class BasicConfig:
    # 非敏感配置写在文件中
    is_host_mode = True
    upload_registry = False
    vis_2d_enable = False
    log_level = "INFO"

class WSConfig:
    reconnect_interval = 5
    max_reconnect_attempts = 999
    ping_interval = 30
```

```bash
# 启动时通过命令行覆盖关键参数
unilab --config base_config.py \
       --ak "$AK" \
       --sk "$SK" \
       --addr "test" \
       --upload_registry \
       --2d_vis \
       -g graph.json
```

## 故障排除

### 1. 配置文件加载失败

**错误信息**：`[ENV] 配置文件 xxx 不存在`

**解决方法**：

- 确认配置文件路径正确
- 检查文件权限是否可读
- 确保配置文件是 `.py` 格式
- 使用绝对路径或相对于当前目录的路径

### 2. 语法错误

**错误信息**：`[ENV] 加载配置文件 xxx 失败`

**解决方法**：

- 检查 Python 语法是否正确
- 确认类名和字段名拼写正确
- 验证缩进是否正确（使用空格而非制表符）
- 确保字符串使用引号包裹

### 3. 认证失败

**错误信息**：`后续运行必须拥有一个实验室`

**解决方法**：

- 确认 `ak` 和 `sk` 已正确配置
- 检查密钥是否有效（未过期或撤销）
- 确认网络连接正常
- 验证密钥是否来自正确的实验室

### 4. 环境变量不生效

**解决方法**：

- 确认环境变量名格式正确（`UNILABOS_<类名>_<字段名>`）
- 检查环境变量是否已正确设置（`echo $VARIABLE_NAME`）
- 重启终端或重新加载环境变量
- 确认环境变量值的类型正确

### 5. 命令行参数不生效

**错误现象**：设置了命令行参数但配置没有生效

**解决方法**：

- 确认参数名拼写正确（如 `--ak` 而不是 `--access_key`）
- 检查参数格式是否正确（布尔参数如 `--is_slave` 不需要值）
- 确认参数位置正确（所有参数都应在 `unilab` 之后）
- 查看启动日志确认参数是否被正确解析
- 检查是否有配置文件或环境变量与之冲突

### 6. 配置优先级混淆

**错误现象**：不确定哪个配置生效

**解决方法**：

- 记住优先级：**命令行参数 > 环境变量 > 配置文件**
- 使用 `--ak` 和 `--sk` 参数时会看到提示信息："传入了 ak 参数，优先采用传入参数！"
- 检查启动日志中的配置加载信息
- 临时移除低优先级配置来测试高优先级配置是否生效
- 使用 `printenv | grep UNILABOS` 查看所有相关环境变量

## 配置验证

### 检查配置是否生效

启动 Uni-Lab 时，控制台会输出配置加载信息：

```
[ENV] 配置文件 /path/to/config.py 加载成功
[ENV] 设置 BasicConfig.log_level = INFO
传入了ak参数，优先采用传入参数！
传入了sk参数，优先采用传入参数！
```

### 常见配置错误

1. **配置文件格式错误**

   ```
   [ENV] 加载配置文件 /path/to/config.py 失败
   ```

   **解决方案**：检查 Python 语法，确保配置类定义正确

2. **环境变量格式错误**

   ```
   [ENV] 环境变量格式不正确：UNILABOS_INVALID_VAR
   ```

   **解决方案**：确保环境变量遵循 `UNILABOS_<类名>_<字段名>` 格式

3. **类或字段不存在**
   ```
   [ENV] 未找到类：UNKNOWNCONFIG
   [ENV] 类 BasicConfig 中未找到字段：UNKNOWN_FIELD
   ```
   **解决方案**：检查配置类名和字段名是否正确

## 相关文档

- [工作目录详解](working_directory.md)
- [启动参数详解](../user_guide/launch.md)
- [快速安装指南](../user_guide/quick_install_guide.md)
