# 工作目录详解

本文档详细介绍 Uni-Lab 工作目录（`working_dir`）的判断逻辑和详细用法。

## 什么是工作目录

工作目录是 Uni-Lab 存储配置文件、日志和运行数据的目录。默认情况下，工作目录为 `当前目录/unilabos_data`。

## 工作目录判断逻辑

系统按以下决策树自动确定工作目录：

### 第一步：初始判断

```python
# 检查当前目录
if 当前目录以 "unilabos_data" 结尾:
    working_dir = 当前目录的绝对路径
else:
    working_dir = 当前目录/unilabos_data
```

**解释：**
- 如果您已经在 `unilabos_data` 目录内启动，系统直接使用当前目录
- 否则，系统会在当前目录下创建或使用 `unilabos_data` 子目录

### 第二步：处理 `--working_dir` 参数

如果用户指定了 `--working_dir` 参数：

```python
working_dir = 用户指定的路径
```

此时还会检查配置文件：
- 如果同时指定了 `--config` 但该文件不存在
- 系统会尝试在 `working_dir/local_config.py` 查找
- 如果仍未找到，报错退出

### 第三步：处理 `--config` 参数

如果用户指定了 `--config` 且文件存在：

```python
# 工作目录改为配置文件所在目录
working_dir = config_path 的父目录
```

**重要：** 这意味着配置文件的位置会影响工作目录的判断。

## 使用场景示例

### 场景 1：默认场景（推荐）

```bash
# 当前目录：/home/user/project
unilab --ak your_ak --sk your_sk -g graph.json

# 结果：
# working_dir = /home/user/project/unilabos_data
# config_path = /home/user/project/unilabos_data/local_config.py
```

### 场景 2：在 unilabos_data 目录内启动

```bash
cd /home/user/project/unilabos_data
unilab --ak your_ak --sk your_sk -g graph.json

# 结果：
# working_dir = /home/user/project/unilabos_data
# config_path = /home/user/project/unilabos_data/local_config.py
```

### 场景 3：手动指定工作目录

```bash
unilab --working_dir /custom/path --ak your_ak --sk your_sk -g graph.json

# 结果：
# working_dir = /custom/path
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
cd ~/labs/lab_a
unilab --ak your_ak_a --sk your_sk_a -g graph_a.json
```

创建 `start_lab_b.sh`：

```bash
#!/bin/bash
cd ~/labs/lab_b
unilab --ak your_ak_b --sk your_sk_b -g graph_b.json
```

## 完整决策流程图

```
开始
  ↓
判断当前目录是否以 unilabos_data 结尾？
  ├─ 是 → working_dir = 当前目录
  └─ 否 → working_dir = 当前目录/unilabos_data
  ↓
用户是否指定 --working_dir？
  └─ 是 → working_dir = 指定路径
  ↓
用户是否指定 --config 且文件存在？
  └─ 是 → working_dir = config 文件所在目录
  ↓
检查 working_dir/local_config.py 是否存在？
  ├─ 是 → 加载配置文件 → 继续启动
  └─ 否 → 询问是否首次使用
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
- 日志文件
- 临时运行数据
- 缓存文件

### 4. 可以删除工作目录吗？

可以，但会丢失：
- 配置文件（需要重新创建）
- 历史日志
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

