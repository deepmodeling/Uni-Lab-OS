# 工作目录详解

本文档详细介绍 Uni-Lab 工作目录（`working_dir`）的判断逻辑和详细用法。

## 什么是工作目录

工作目录是 Uni-Lab 存储配置文件、日志和运行数据的目录，其下通常有：

- `local_config.py` - 配置文件
- `logs/` - 日志
- `database/` - 微后端的四个数据库（`runtime.db`、`materials.db`、`telemetry.db`、`history.db`），Host 子进程用 `database/edge/`
- 注册表缓存、驱动包源码树等运行数据

默认情况下，工作目录为 `~/.unilabos`。

## 工作目录判断逻辑

实现见 `unilabos/config/working_dir.py`，`unilab` 主命令与 `unilab workflow` 等子命令共用：

1. 指定了 `--working_dir` 时使用它；
2. 否则 `--config` 指向的文件存在时，使用其所在目录；
3. 否则看当前目录；
4. 上一步得到的目录本身名为 `.unilabos` 或 `unilabos_data` 时直接使用；其下有这样的子目录时使用该子目录；
5. 当前目录（第 3 步）不满足第 4 步时，使用默认的 `~/.unilabos`。显式指定的目录（第 1、2 步）原样使用。

`unilabos_data` 是旧版本在当前目录下自动生成的目录名，已有的 `unilabos_data` 会继续被识别和使用；
新的默认不再在当前目录下生成它。

## 使用场景示例

### 场景 1：默认场景（推荐）

```bash
# 当前目录：/home/user/project（其下没有 .unilabos / unilabos_data）
unilab --ak your_ak --sk your_sk -g graph.json

# 结果：
# working_dir = /home/user/.unilabos
# config_path = /home/user/.unilabos/local_config.py
# 数据库      = /home/user/.unilabos/database/
```

### 场景 2：沿用项目目录下已有的 unilabos_data

```bash
# 当前目录 /home/user/project 下已有 unilabos_data/（旧版本生成）
unilab --ak your_ak --sk your_sk -g graph.json

# 结果：
# working_dir = /home/user/project/unilabos_data
```

在 `unilabos_data` 或 `.unilabos` 目录内启动时同样直接使用该目录。

### 场景 3：手动指定工作目录

```bash
unilab --working_dir /custom/path --ak your_ak --sk your_sk -g graph.json

# 结果：
# working_dir = /custom/path（其下有 .unilabos / unilabos_data 时使用该子目录）
# config_path = /custom/path/local_config.py （如果存在）
```

### 场景 4：通过配置文件路径推断工作目录

```bash
unilab --config /data/lab_a/local_config.py --ak your_ak --sk your_sk -g graph.json

# 结果：
# working_dir = /data/lab_a
# config_path = /data/lab_a/local_config.py
```

## 高级用法：管理多个实验室配置

### 方法 1：使用不同的工作目录

```bash
# 实验室 A
unilab --working_dir ~/labs/lab_a --ak ak_a --sk sk_a -g graph_a.json

# 实验室 B
unilab --working_dir ~/labs/lab_b --ak ak_b --sk sk_b -g graph_b.json
```

### 方法 2：使用不同的配置文件

```bash
# 实验室 A
unilab --config ~/labs/lab_a/config.py --ak ak_a --sk sk_a -g graph_a.json

# 实验室 B
unilab --config ~/labs/lab_b/config.py --ak ak_b --sk sk_b -g graph_b.json
```

### 方法 3：使用shell脚本管理

创建 `start_lab_a.sh`：

```bash
#!/bin/bash
# 默认工作目录是 ~/.unilabos；多套实验室并存时用 --working_dir 区分
unilab --working_dir ~/labs/lab_a --ak your_ak_a --sk your_sk_a -g graph_a.json
```

创建 `start_lab_b.sh`：

```bash
#!/bin/bash
unilab --working_dir ~/labs/lab_b --ak your_ak_b --sk your_sk_b -g graph_b.json
```

## 完整决策流程图

```
开始
  ↓
指定了 --working_dir？
  ├─ 是 → 候选 = 指定路径
  └─ 否 → 指定了 --config 且文件存在？
            ├─ 是 → 候选 = config 文件所在目录
            └─ 否 → 候选 = 当前目录
  ↓
候选名为 .unilabos / unilabos_data，或其下有这样的子目录？
  ├─ 是 → working_dir = 该目录
  └─ 否 → 候选来自 --working_dir / --config？
            ├─ 是 → working_dir = 候选
            └─ 否 → working_dir = ~/.unilabos
  ↓
检查 working_dir/local_config.py 是否存在？
  ├─ 是 → 加载配置文件 → 继续启动
  └─ 否 → 询问是否首次使用（无交互输入时按默认「是」）
      ├─ 是 → 创建目录和配置文件 → 继续启动
      └─ 否 → 退出程序
```

## 常见问题

### 1. 如何查看当前使用的工作目录？

启动 Uni-Lab 时，系统会在控制台输出：

```
当前工作目录为 /path/to/working_dir
```

### 2. 可以在同一台机器上运行多个实验室吗？

可以。使用不同的工作目录或配置文件即可：

```bash
# 终端 1
unilab --working_dir ~/lab1 --ak ak1 --sk sk1 -g graph1.json

# 终端 2
unilab --working_dir ~/lab2 --ak ak2 --sk sk2 -g graph2.json
```

### 3. 工作目录中存储了什么？

- `local_config.py` - 配置文件
- `logs/` - 日志文件
- `database/` - 微后端数据库（见 [配置指南·数据库位置](configuration.md)）
- 注册表缓存、驱动包等运行数据

### 4. 可以删除工作目录吗？

可以，但会丢失：
- 配置文件（需要重新创建）
- 历史日志
- 数据库（工作流、任务、物料等）
- 缓存数据

建议定期备份配置文件。

### 5. 如何迁移到新的工作目录？

```bash
# 1. 复制旧的工作目录
cp -r ~/old_path/unilabos_data ~/new_path/unilabos_data

# 2. 在新位置启动
cd ~/new_path
unilab --ak your_ak --sk your_sk -g graph.json
```

## 最佳实践

1. **使用默认工作目录**：对于单一实验室，使用默认的 `./unilabos_data` 即可
2. **组织多实验室**：为每个实验室创建独立的目录结构
3. **版本控制**：将配置文件纳入版本控制，但排除日志和缓存
4. **备份配置**：定期备份 `local_config.py` 文件
5. **使用脚本**：为不同实验室创建启动脚本，简化操作

## 相关文档

- [配置文件指南](configuration.md)
- [启动参数详解](../user_guide/launch.md)

