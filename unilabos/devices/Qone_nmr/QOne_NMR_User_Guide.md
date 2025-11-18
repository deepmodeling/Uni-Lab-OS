# QOne NMR 用户指南

## 概述

Qone NMR 设备支持多字符串数据处理功能。该设备可以接收包含多个字符串的输入数据，并将每个字符串转换为独立的 TXT 文件，支持灵活的数据格式化和输出。

## 核心功能

- **多字符串处理**: 支持逗号分隔或换行分隔的多个字符串输入
- **自动文件生成**: 每个输入字符串生成一个对应的 TXT 文件
- **文件夹监督**: 自动监督指定目录，检测新内容生成
- **错误处理**: 完善的输入验证和错误处理机制

## 参数说明

### 输入参数

- **string** (str): 包含多个字符串的输入数据，支持格式：
  - **逗号分隔**: `"字符串1, 字符串2, 字符串3"`

### 输出参数

- **return_info** (str): 处理结果信息，包含监督功能的执行结果
- **success** (bool): 处理是否成功
- **files_generated** (int): 生成的 TXT 文件数量

## 输入数据格式

### 支持的输入格式

1. **逗号分隔格式**:
   ```
   "A 1 B 1 C 1 D 1 E 1 F 1 G 1 H 1 END, A 2 B 2 C 2 D 2 E 2 F 2 G 2 H 2 END"
   ```

   ```

### 数据项格式

每个字符串内的数据项应该用空格分隔，例如：
- `A 1 B 2 C 3 D 4 E 5 F 6 G 7 H 8 END`
- `Sample 001 Method A Volume 10.5 Temp 25.0`

## 输出文件说明

### 文件命名

生成的 TXT 文件将按照row_字符串顺序命名，例如：
- `row_1.txt`
- `row_2.txt`

### 文件格式

每个 TXT 文件包含对应字符串的格式化数据，格式为：
```
A 1
B 2
C 3
D 4
E 5
F 6
G 7
H 8
END
```

### 输出目录

默认输出目录为 `D:/setup/txt`，可以在 `device.json` 中配置 `output_dir` 参数。

## 文件夹监督功能

### 监督机制

设备在完成字符串到TXT文件的转换后，会自动启动文件夹监督功能：

- **监督目录**: 默认监督 `D:/Data/MyPC/Automation` 目录
- **检查间隔**: 每60秒检查一次新生成的.nmr文件
- **检测内容**: 新文件生成或现有文件大小变化
- **停止条件**: 连续三次文件大小没有变化，则检测完成

## 文件夹监督功能详细说明

Oxford NMR设备驱动集成了智能文件夹监督功能，用于监测.nmr结果文件的生成完成状态。该功能通过监测文件大小变化来判断文件是否已完成写入。

### 工作机制

1. **文件大小监测**: 监督功能专门监测指定目录中新生成的.nmr文件的大小变化
2. **稳定性检测**: 当文件大小在连续多次检查中保持不变时，认为文件已完成写入
3. **批量处理支持**: 根据输入的.txt文件数量，自动确定需要监测的.nmr文件数量
4. **实时反馈**: 提供详细的监测进度和文件状态信息
5. **自动停止**: 当所有期望的.nmr文件都达到稳定状态时，监督功能自动停止，start函数执行完毕

### 配置参数

可以通过`device.json`配置文件调整监督功能的行为：

```json
{
    "config": {
        "output_dir": "D:/setup/txt",
        "monitor_dir": "D:\\Data\\MyPC\\Automation",
        "stability_checks": 3,
        "check_interval": 60
    }
}
```

- `monitor_dir`: 监督的目录路径，默认为`D:\Data\MyPC\Automation`
- `stability_checks`: 文件大小稳定性检查次数，默认为3次（连续2次检查大小不变则认为文件完成）
- `check_interval`: 检查间隔时间（秒），默认为60秒

### 监测逻辑

1. **初始状态记录**: 记录监督开始时目录中已存在的.nmr文件及其大小
2. **新文件检测**: 持续检测新生成的.nmr文件
3. **大小变化跟踪**: 为每个新文件维护大小变化历史记录
4. **稳定性判断**: 当文件大小在连续`stability_checks`次检查中保持不变且大小大于0时，认为文件完成
5. **完成条件**: 当达到期望数量的.nmr文件都完成时，监督功能结束

### 配置监督目录

可以在 `device.json` 中配置 `monitor_dir` 参数来指定监督目录：

```json
{
  "config": {
    "output_dir": "D:/setup/txt",
    "monitor_dir": "D:/Data/MyPC/Automation"
  }
}
```

## 使用示例

### 示例 1: 基本多字符串处理

```python
from unilabos.devices.Qone_nmr.Qone_nmr import Qone_nmr

# 创建设备实例
device = Qone_nmr(output_directory="D:/setup/txt")

# 输入多个字符串（逗号分隔）
input_data = "A 1 B 1 C 1 D 1 E 1 F 1 G 1 H 1 END, A 2 B 2 C 2 D 2 E 2 F 2 G 2 H 2 END"

# 处理数据
result = device.start(string=input_data)

print(f"处理结果: {result}")
# 输出: {'return_info': 'Oxford NMR处理完成: 已生成 3 个 txt 文件，保存在: ./output | 监督完成: 成功检测到 3 个.nmr文件已完成生成', 'success': True, 'files_generated': 3}

### 输出示例

当设备成功处理输入并完成文件监督后，会返回如下格式的结果：

```json
{
    "return_info": "Oxford NMR处理完成: 已生成 3 个 txt 文件，保存在: D:/setup/txt | 监督完成: 成功检测到 3 个.nmr文件已完成生成，start函数执行完毕",
    "success": true,
    "files_generated": 3
}
```

监督过程中的日志输出示例：
```
[INFO] 开始监督目录: D:/Data/MyPC/Automation，检查间隔: 30秒，期望.nmr文件数量: 3，稳定性检查: 2次
[INFO] 初始状态: 0 个.nmr文件
[INFO] 检测到 3 个新.nmr文件，还需要 0 个...
[DEBUG] 文件大小监测中: D:/Data/MyPC/Automation/sample1.nmr (当前: 1024 字节, 检查次数: 1/3)
[DEBUG] 文件大小监测中: D:/Data/MyPC/Automation/sample2.nmr (当前: 2048 字节, 检查次数: 1/3)
[DEBUG] 文件大小监测中: D:/Data/MyPC/Automation/sample3.nmr (当前: 1536 字节, 检查次数: 1/3)
[INFO] 文件大小已稳定: D:/Data/MyPC/Automation/sample1.nmr (大小: 1024 字节)
[INFO] 文件大小已稳定: D:/Data/MyPC/Automation/sample2.nmr (大小: 2048 字节)
[INFO] 文件大小已稳定: D:/Data/MyPC/Automation/sample3.nmr (大小: 1536 字节)
[INFO] 所有期望的.nmr文件都已完成生成! 完成文件数: 3/3
[INFO] 完成的.nmr文件: D:/Data/MyPC/Automation/sample1.nmr (最终大小: 1024 字节)
[INFO] 完成的.nmr文件: D:/Data/MyPC/Automation/sample2.nmr (最终大小: 2048 字节)
[INFO] 完成的.nmr文件: D:/Data/MyPC/Automation/sample3.nmr (最终大小: 1536 字节)
[INFO] 停止文件夹监测，所有文件已完成
```
```

## 错误处理

设备具有完善的错误处理机制：

- **空输入**: 如果输入为空或 None，返回错误信息
- **无效格式**: 如果输入格式不正确，返回相应错误
- **文件系统错误**: 如果输出目录不存在或无权限，返回错误信息

## 注意事项

1. **目录权限**: 确保监督目录具有读取权限，以便设备能够检测文件变化
2. **文件大小监测**: 监督功能现在基于文件大小变化来判断.nmr文件是否完成，而不是简单的文件存在性检查
3. **稳定性检查**: 文件大小需要在连续多次检查中保持不变才被认为完成，默认为3次检查
4. **自动停止**: 监督功能会在检测到期望数量的.nmr文件都达到稳定状态后自动停止，避免无限循环
5. **配置灵活性**: 可以通过`device.json`调整稳定性检查次数和检查间隔，以适应不同的使用场景
6. **文件类型**: 监督功能专门针对.nmr文件，忽略其他类型的文件变化
7. **批量处理**: 支持同时监测多个.nmr文件的完成状态，适合批量处理场景