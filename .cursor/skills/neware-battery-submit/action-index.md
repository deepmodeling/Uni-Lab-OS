# Neware Battery Test System — Action Index

设备 ID: `neware_battery_test_system`
源码: 新威电池测试驱动包中的 `neware_battery_test_system.py`（原 `unilabos/devices/neware_battery_test_system/`，已迁出主仓库）
设备类: `NewareBatteryTestSystem`

---

## 核心动作（提交与上传）

### `submit_from_csv`

从 CSV 文件批量提交 Neware 测试任务。读取 CSV 中的电池参数，为每个电池生成对应体系的 XML 配方文件，通过 TCP 协议提交到新威设备。

- **Schema**: [`actions/submit_from_csv.json`](actions/submit_from_csv.json)
- **action_type**: `UniLabJsonCommand`
- **核心参数**: `csv_path`（CSV 文件绝对路径，**必填**）
- **可选参数**: `output_dir`（XML 和备份文件输出目录，默认 `.`）
- **CSV 必需列**: `Battery_Code`, `Electrolyte_Code`, `Pole_Weight`, `集流体质量`, `活性物质含量`, `克容量mah/g`, `电池体系`, `设备号`, `排号`, `通道号`

### `upload_backup_to_oss`

上传备份目录中的文件到阿里云 OSS。通常在 `submit_from_csv` 之后调用，需要设备 `oss_upload_enabled=true`。

- **Schema**: [`actions/upload_backup_to_oss.json`](actions/upload_backup_to_oss.json)
- **action_type**: `UniLabJsonCommand`
- **可选参数**: `backup_dir`（默认使用最近一次 submit 的备份目录）, `file_pattern`（默认 `*`）, `oss_prefix`
- **输出 handle**: `uploaded_files`（上传成功的文件 URL 列表）

---

## 状态查询动作

### `export_status_json`

导出当前所有通道状态到 JSON 文件。

- **Schema**: [`actions/export_status_json.json`](actions/export_status_json.json)
- **action_type**: `UniLabJsonCommand`
- **可选参数**: `filepath`（输出文件路径，默认 `bts_status.json`）

### `get_plate_status`

获取指定盘或所有盘的状态统计信息（working/stop/finish/protect 数量）。

- **Schema**: [`actions/get_plate_status.json`](actions/get_plate_status.json)
- **action_type**: `UniLabJsonCommand`
- **可选参数**: `plate_num`（1 或 2，不传则返回全部）

### `query_plate_action`

查询指定盘的详细通道信息（含每个通道的 devid/subdevid/chlid/voltage/status）。

- **Schema**: [`actions/query_plate_action.json`](actions/query_plate_action.json)
- **action_type**: `StrSingleInput`
- **参数**: `string`（盘号标识 `P1` 或 `P2`，对应 `plate_id`）

### `get_device_summary`

获取设备级别的通道数量统计（按 devid 分组）。

- **Schema**: [`actions/get_device_summary.json`](actions/get_device_summary.json)
- **action_type**: `UniLabJsonCommand`
- **无参数**

---

## 连接与调试动作

### `test_connection_action`

测试 TCP 连接是否正常。

- **Schema**: [`actions/test_connection_action.json`](actions/test_connection_action.json)
- **action_type**: `UniLabJsonCommand`
- **无参数**

### `auto_test_connection`

自动测试连接（自动化调用版本）。

- **Schema**: [`actions/auto_test_connection.json`](actions/auto_test_connection.json)
- **action_type**: `UniLabJsonCommand`
- **无参数**

### `print_status_summary_action`

打印通道状态摘要到控制台。

- **Schema**: [`actions/print_status_summary_action.json`](actions/print_status_summary_action.json)
- **action_type**: `UniLabJsonCommand`
- **无参数**

### `auto_print_status_summary`

自动打印状态摘要（自动化调用版本）。

- **Schema**: [`actions/auto_print_status_summary.json`](actions/auto_print_status_summary.json)
- **action_type**: `UniLabJsonCommand`
- **无参数**

### `debug_resource_names`

调试方法：列出所有资源的实际名称（P1_xxx, P2_xxx 等）。

- **Schema**: [`actions/debug_resource_names.json`](actions/debug_resource_names.json)
- **action_type**: `UniLabJsonCommand`
- **无参数**
