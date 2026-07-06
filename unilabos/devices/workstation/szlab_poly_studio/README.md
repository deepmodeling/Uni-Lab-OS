## 设备

### 堆栈(S01, S02, S03, S10, S11)

### S04

- `run_stirring`: 执行 S04 磁搅加工

### S05

- `take_photo`: 执行烧杯姿势拍照检测

### S06

- `transfer_liquid`: 执行 S06 单步转液
- `run_solvent_addition`: 执行 S06 泵加液完整流程

### S07

- `scan_powder_cartridges`: S07 粉罐扫码盘点
- `rotate_powder_cartridge_to_feed`: S07 替换粉罐旋转到进料位
- `dose_powder`: S07 注粉

### S08

- `process_cap`: S08 开/关盖

### S09

- `check_home_position`: 读取 S09 指定安全位原点信号
- `read_home_positions`: 读取 S09 四个安全位原点信号
- `go_to_safe_position`: 执行 S09 去安全位工艺并确认原点信号
- `prepare_liquid_station`: 确认 S09 唯一加液工位空闲
- `read_allow_process`: 读取 S09 允许加工信号
- `bind_sample_to_station`: 绑定样品到 S09 加液工位
- `release_station`: 释放 S09 加液工位绑定
- `run_process`: 执行 S09 单个 PLC 工艺
- `add_liquid`: 执行 S09 单次业务加液流程
- `add_liquid_to_beaker`: 执行 S09 烧杯加液流程
- `run_liquid_workflow`: 执行 S09 多步加液工作流
- `set_liquid_bottle_remaining_volume`: 写入 S09 单个液体瓶剩余液量
- `initialize_liquid_bottle_remaining_volumes`: 初始化 S09 1-5 号液体瓶剩余液量
- `read_balance`: 读取 S09 天平读数
- `get_pipetting_status`: 读取 S09 移液站状态

## 设备暂存位数量

1. S01: 缓存器(待定)
2. S02: TIP 盒 * 2
3. S03: 空烧杯 (6 * 3),空试剂瓶(6 * 3)
4. S04: 磁搅拌 (3 * 2)
5. S05: 拍照(1)
6. S06: 泵（1）
7. S07: 固体加样(10, 1), 粉桶暂存(6)
8. S08: 开关盖 (两个瓶子位置， 规格不对)
9. S09: 移液 (1个烧杯位置， 5个溶剂瓶位置)
10. S10: 20个成平瓶位置
11. S11: 已用固体瓶(6 * 3), 已用溶剂瓶(6 * 3)
12. S12: 机械臂

