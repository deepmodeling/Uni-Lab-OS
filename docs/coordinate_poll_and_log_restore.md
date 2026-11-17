# 坐标查询暂停 & 日志改回「每次启动新建文件」—— 改动说明与恢复指南

> 写给后续接手的 agent / 同事。本次改动是**临时的**：坐标查询只是"先注释掉"，日志则是"改回原始行为"。
> 改动日期：2026-11-10。

---

## 一、坐标查询已暂停（配液站 + 扣电站）

### 1. 暂停了什么

| 工站 | 对象 | 原轮询方式 | 对外 ROS 属性 |
|---|---|---|---|
| 配液站 `BioyondCellWorkstation` | 机械臂关节(2.43) + 龙门架三轴(2.44) | LIMS HTTP，每 2 秒（`_ROBOT_POS_PERIOD_S = 2.0`） | `robot_joint_positions`、`gantry_positions` |
| 扣电站 `CoinCellAssemblyWorkstation` | DUCO 六轴机械臂 | `GET http://172.16.28.102:8000/api/state`，每 0.5 秒（`_DUCO_PERIOD_S = 0.5`） | `duco_joint_positions`、`duco_tcp_pose` |

> `_ROBOT_POS_PERIOD_S` 已由 1.0 改成 **2.0**，恢复后沿用该值，无需再改。

### 2. 怎么找到所有改动点

所有注释处都带统一标记 **`[坐标查询暂停]`**：

```powershell
rg -n "坐标查询暂停" unilabos
```

共 4 个文件、22 处：

| 文件 | 处理 |
|---|---|
| `unilabos/devices/workstation/bioyond_studio/bioyond_cell/bioyond_cell_workstation.py` | ① `__init__` 里启动 `bioyond-robot-pos-poller` 线程的代码；② `post_init` 里 `change_frequency` 的 try 块；③ `robot_joint_positions` / `gantry_positions` 两个 `@property` |
| `unilabos/devices/workstation/coin_cell_assembly/coin_cell_assembly.py` | ① `__init__` 里启动 `duco-state-poller` 线程的代码（暂停期间 `self._duco_thread = None`）；② `post_init` 里 `change_frequency` 的 for 块；③ `duco_joint_positions` / `duco_tcp_pose` 两个 `@property` |
| `unilabos/registry/devices/bioyond_cell.yaml` | `status_types` 与 `schema.properties` 里的 `robot_joint_positions`、`gantry_positions` |
| `unilabos/registry/devices/coin_cell_workstation.yaml` | `status_types`、`schema.properties`、`schema.required` 里的 `duco_joint_positions`、`duco_tcp_pose` |

**没有**注释的部分（保留为死代码，恢复时无需动）：
`_fetch_robot_joints`、`_fetch_gantry_pos`、`_robot_pos_poll_loop`、`_device_list_payload`、`_axis_values`、
`robot_joint_list_info()`、`gantry_pos_list_info()`、`_duco_poll_loop`、`_duco_snapshot`，以及各类常量与缓存字段初始化。

### 3. 如何恢复

1. 对上表 4 个文件，把带 `[坐标查询暂停]` 标记的注释**还原**：
   - Python：去掉每行行首的 `# `（只去一层注释；原本就是注释的说明行保持原样）。
   - 注意 `bioyond_cell_workstation.py` `__init__` 里多了一行 `logger.info("[坐标查询暂停] ...未启动")`，
     以及 `coin_cell_assembly.py` 里的 `self._duco_thread = None`，**恢复时这两行要删掉**（否则 `_duco_thread` 会被覆盖为 None / 日志误导）。
   - YAML：去掉行首的 `# [坐标查询暂停] `，保留原有缩进。
2. **Python 与 YAML 必须同时恢复**，缺一不可：
   - 只恢复 YAML、不恢复 property → 注册表声明了属性但类里没有，该属性读不到值（未实测具体报错形式）；
   - 只恢复 property、不恢复 YAML → 属性不会被发布；`post_init` 里 `change_frequency` 找不到发布者，会刷 warning。
3. 重启工站（注册表在启动时加载，改 YAML 后需重启；`unilab` 启动时若带 `--upload_registry` 会同步到云端）。
4. 验证：启动日志里应出现 `机器人坐标轮询线程已启动（每 2.0 秒）`（配液站，非 debug_mode）；
   扣电站应不再出现 `[坐标查询暂停]`，且有 `[DUCO]` 相关日志。

### 4. 备注

- 配液站坐标轮询在 `debug_mode=True` 时本来就不启动（`_post_lims` 在 debug 下不发真实请求）。
- 连续失败 3 次（`_ROBOT_POS_MAX_FAIL` / `_DUCO_MAX_FAIL`）会清空缓存，恢复后仍是这个行为。

---

## 二、日志改回「每次启动新建一个文件」

### 1. 背景

之前为了让日志"重启追加"，把 `unilabos/utils/log.py` 改成固定文件名 + `mode="a"`，导致
`unilabos_data/logs/unilabos.log` 无限累加（已涨到约 475 万行）。现已改回原始行为：

| 日志 | 文件名 | 行为 |
|---|---|---|
| 主日志 | `logs/<YYYY-MM-DD HH-MM-SS>.log` | 每次启动新建 |
| WebSocket 通信日志 | `logs/ws_comm_<YYYY-MM-DD HH-MM-SS>.log` | 每次启动新建 |

`configure_logger()` 与 `configure_comm_logger()` 仍然**返回日志文件路径**（`unilabos/app/main.py` 依赖返回值）。

### 2. 改了什么（`unilabos/utils/log.py`）

只改了文件命名/打开方式，其余（`ColoredFormatter`、`_to_numeric_level`、各三方库日志级别压制等）原样保留：

- `configure_logger`：`logs/unilabos.log` + `mode="a"` → `datetime.now().strftime("%Y-%m-%d %H-%M-%S") + ".log"`，去掉 `mode="a"`（文件名带时间戳，本来就是新文件）；去掉了 "[LOG_FILE] 会话开始" 那行日志。
- `configure_comm_logger`：`logs/ws_comm.log` + `mode="a"` → `"ws_comm_" + 时间戳 + ".log"`；日志文案恢复为 "通信日志已初始化，文件: ..."。
- 注意该文件里 `COMM_LOGGER_NAME`…`configure_comm_logger` 这一整段历史上就被**重复定义了两遍**（合并遗留，从 HEAD 起就如此），后一份覆盖前一份，功能无影响，所以两份都做了同样的修改。

### 3. ⚠️ 为什么不能直接把 `log-1.py` 整个覆盖到 `log.py`

`unilabos/utils/log-1.py` 是**旧版**的 `log.py`（385 行），已经缺少现在代码依赖的东西。直接覆盖会启动即报错：

```
ImportError: cannot import name 'configure_comm_logger' from 'unilabos.utils.log'
  (File "unilabos/app/main.py", line 681)
```

`log-1.py` 缺失的内容：

1. `configure_comm_logger` / `get_comm_logger` / `COMM_LOGGER_NAME`（`main.py` 第 681 行 `import`，第 690 行调用）；
2. `ColoredFormatter.__init__` 的 `microseconds`、`show_thread` 参数（通信日志要用）；
3. `_to_numeric_level` 辅助函数；
4. `configure_logger` 的返回值（`main.py` 第 685 行 `file_path = configure_logger(...)`）；
5. 对 `pymodbus` / `websockets` / `host_node` 等嘈杂日志的级别压制。

**结论**：`log-1.py` 只能作为"新建文件命名方式"的参考，**不要整体覆盖**。需要改日志行为时，在当前 `log.py` 上做最小修改。

### 4. 如何恢复成「重启追加（固定文件名）」

备份文件 `unilabos/utils/log-matmaster.py` 就是**追加模式**的完整版本（与本次改动前的 `log.py` 逐字节相同）。恢复方法二选一：

**方法 A（推荐，一条命令）**

```powershell
Copy-Item unilabos/utils/log-matmaster.py unilabos/utils/log.py -Force
```

**方法 B（手改 3 处，`log.py` 内相同的片段需要全部替换）**

1. `configure_logger` 里：
   ```python
   log_filepath = os.path.join(logs_dir, "unilabos.log")
   file_handler = logging.FileHandler(log_filepath, mode="a", encoding="utf-8")
   ```
2. `configure_comm_logger`（有两份，都要改）里：
   ```python
   log_filepath = os.path.join(logs_dir, "ws_comm.log")
   file_handler = logging.FileHandler(log_filepath, mode="a", encoding="utf-8")
   ```
3. （可选）在 `root_logger.addHandler(file_handler)` 后补一行"会话开始"分隔日志，方便在累加文件里区分每次启动。

恢复后要注意：固定文件会无限增长，需要手动定期清理或自行加轮转。

### 5. 验证方法

在 `unilab` 环境（`C:\ProgramData\miniforge3\envs\unilab\python.exe`）下，连续调用两次：

```python
from unilabos.utils import log as L
a = L.configure_logger(loglevel="INFO", working_dir="<临时目录>")
b = L.configure_comm_logger(loglevel="INFO", working_dir="<临时目录>")
```

间隔 1 秒以上调用，`<临时目录>/logs/` 下应出现两对不同时间戳的文件：
`YYYY-MM-DD HH-MM-SS.log` 与 `ws_comm_YYYY-MM-DD HH-MM-SS.log`。

---

## 三、历史遗留文件说明

- `unilabos_data/logs/unilabos.log`（≈475 万行）、`ws_comm.log`：追加模式时期留下的大文件，现已不再写入，可按需删除/归档（均为未跟踪文件，未提交 git）。
- `unilabos/utils/log-1.py`：旧版 log.py，仅供参考，**不可直接使用**（见上文 3）。
- `unilabos/utils/log-matmaster.py`：追加模式版本备份，恢复用。
- 编辑器提示：改 `log.py` 时 Cursor 编辑器缓冲区若是旧内容（386 行那版），先 `File → Revert File` 重新加载磁盘内容，避免保存时把旧版覆盖回去。
