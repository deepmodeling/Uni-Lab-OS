# SZLab S06 加液测试说明

当前 `szlab_mixer` 调试入口只包含 S06 加液泵设备 `szlab_mixer_pump`，UI 左侧只应出现 `run_solvent_addition` 动作。

## 启动本地 UI

```bash
PYTHONPATH=. python -m scripts.workflow_ui \
  --host 0.0.0.0 \
  --port 8014 \
  --preset szlab_mixer \
  --runtime-config tests/szlab_poly_studio/runtime_configs/szlab_mixer_pump_runtime.json
```

浏览器打开 `http://127.0.0.1:8014/`。真机调试时 OPC UA URL 填 `opc.tcp://192.168.1.10:4840/`。

## 虚拟 OPC 联调

终端 1 — CSV server：

```bash
PYTHONPATH=. python tests/pseudo_devices/common/opcua_csv_server.py \
  --csv unilabos/devices/workstation/szlab_poly_studio/s06_pump/pump_nodes.csv \
  --endpoint opc.tcp://127.0.0.1:48506/
```

终端 2 — flow daemon：

```bash
PYTHONPATH=. python tests/pseudo_devices/common/opcua_flow_daemon.py \
  --url opc.tcp://127.0.0.1:48506/ \
  --flow unilabos/devices/workstation/szlab_poly_studio/s06_pump/pump_flow.json
```

终端 3 — 复位变量：

```bash
PYTHONPATH=. python tests/szlab_poly_studio/reset_virtual_pump_opcua.py
```

UI 中 OPC UA URL 填 `opc.tcp://127.0.0.1:48506/`，运行 `run_solvent_addition`。

常用参数：

```text
process: 1
volume: 8
volume_pump_1: 0
volume_pump_2: 0
skip_level_check: false
beaker_true_means_present: true
```

## 命令行测试

```bash
PYTHONPATH=. pytest tests/szlab_poly_studio/test_szlab_mixer_pump.py -q
PYTHONPATH=. pytest tests/szlab_poly_studio/test_szlab_mixer_pump_opcua_ci.py -q
```

如需连外部 OPC UA：

```bash
export UNILABOS_TEST_SZLAB_MIXER_OPCUA_URL=opc.tcp://192.168.1.10:4840/
PYTHONPATH=. pytest tests/szlab_poly_studio/test_szlab_mixer_pump_opcua_ci.py -q
```

## 相关文件

| 文件 | 用途 |
|---|---|
| `unilabos/devices/workstation/szlab_poly_studio/s06_pump/pump_nodes.csv` | S06 加液泵 OPC 变量表 |
| `unilabos/devices/workstation/szlab_poly_studio/s06_pump/pump_flow.json` | 虚拟 PLC 完成信号规则 |
| `unilabos/devices/workstation/szlab_poly_studio/s06_pump/pump_debug.json` | 单独调试参数 |
| `tests/szlab_poly_studio/runtime_configs/szlab_mixer_pump_runtime.json` | pump-only 运行配置 |
| `tests/szlab_poly_studio/presets/szlab_mixer.json` | UI preset |
