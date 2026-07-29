# S07 固体加料工位

S07 通过 `szlab_poly_plc` 网关读写 PLC，支持粉罐扫码、旋转到进料位、注粉，以及注粉过程中的天平记录。

## 目录结构

```text
s07_solid_addition/
├── s07.py                  # 设备驱动
├── sensors.py              # PLC 变量与工艺常量
├── balance_history.py      # 天平采样 CSV 与 SVG 曲线
├── s07_powder_params.json  # 粗/精注粉配方
├── debug_s07.py            # 单次 CLI 调试入口
├── s07_debug.json          # 单次调试配置
├── s07_nodes.csv           # 虚拟 OPC 变量
└── s07_flow.json           # 虚拟 PLC 时序
```

## 设备动作

- `scan_powder_cartridges`：扫码盘点粉罐。
- `read_s07_balance`：读取实时天平值。
- `rotate_powder_cartridge_to_feed`：把指定粉罐旋转到进料位。
- `dose_powder`：加载配方参数并执行注粉。

设备 ID 为 `szlab_s07_solid_addition`，类路径保持为：

```text
unilabos.devices.workstation.szlab_poly_studio.s07_solid_addition.s07.SZLabS07SolidAdditionDevice
```

## 注粉执行链路

1. 从 `s07_powder_params.json` 加载 `recipe_name` 对应的粗/精注粉参数。
2. 等待 `S07原点信号=True` 和 `S07允许加工=True`。
3. 写入仓位、目标重量、配方参数、`S07工艺选择=3` 和 `S07参数写入完成=True`。
4. 持续等待 PLC 返回 `S07工艺完成=3`，同时记录天平读数。
5. 清除 Uni-Lab 写入的参数，并等待 PLC 将工艺完成信号复位为 `0`。

PC 侧不设置工艺完成超时；设备异常与工艺超时由 PLC 处理。

## 天平记录

`balance_history.py` 被 `dose_powder` 调用，不负责启动批量任务。

默认输出到 `workflow_artifacts/s07_balance/`：

- `samples.csv`：按 `run_id` 追加每次注粉的时间、目标重量和天平读数。
- `balance_curves.svg`：多次运行的叠加曲线，包含 `0 g` 基线。

## 单次调试

虚拟 OPC 与单次动作：

```bash
PYTHONPATH=. python \
  unilabos/devices/workstation/szlab_poly_studio/s07_solid_addition/debug_s07.py \
  --mode all
```

仅启动虚拟 OPC：

```bash
PYTHONPATH=. python \
  unilabos/devices/workstation/szlab_poly_studio/s07_solid_addition/debug_s07.py \
  --mode serve
```

PLC 变量复位：

```bash
PYTHONPATH=. python \
  unilabos/devices/workstation/szlab_poly_studio/s07_solid_addition/debug_s07.py \
  --mode reset
```

## 验证

```bash
pytest \
  tests/szlab_poly_studio/test_s07_solid_addition.py \
  tests/szlab_poly_studio/test_s07_balance_history.py
```

真机执行动作前，应先确认原点、允许加工、工艺选择、参数写入和工艺完成信号均处于就绪状态。
