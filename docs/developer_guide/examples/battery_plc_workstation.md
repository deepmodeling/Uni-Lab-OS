# 实例：电池装配工站接入（PLC 控制）

> **文档类型**：实际应用案例  
> **适用场景**：使用 PLC 控制的电池装配工站接入  
> **前置知识**：{doc}`../add_device` | {doc}`../add_registry`
> **驱动位置**：本文涉及的厂商驱动与物料预设（纽扣电池装配工站、电池物料）已迁出主仓库，放在外部驱动包中；文中的 `unilabos.devices.*`、`unilabos.resources.presets.{bioyond,battery}` 路径请换成对应驱动包里的模块路径。框架基类 `unilabos.devices.workstation.workstation_base` 仍在主仓库。

本指南以电池装配工站为例，说明 PLC 驱动、寄存器读写、Registry 校验和启动配置。

## 案例概述

**设备类型**：电池装配工站  
**通信方式**：Modbus TCP (PLC)  
**工站基类**：`WorkstationBase`  
**主要功能**：电池组装、寄存器读写、数据采集

## 1. 新建工站文件

### 1.1 创建工站文件

在 `unilabos/devices/workstation/coin_cell_assembly` 目录下新建工站文件，如 `coin_cell_assembly.py`。工站类需继承 `WorkstationBase`，并在构造函数中初始化通信客户端与寄存器映射。

```python
from typing import Optional
# 工站基类
from unilabos.devices.workstation.workstation_base import WorkstationBase
# Modbus 通讯与寄存器 CSV 支持
from unilabos.device_comms.modbus_plc.client import TCPClient, BaseClient

class CoinCellAssemblyWorkstation(WorkstationBase):
    def __init__(
        self,
        station_resource,
        address: str = "192.168.1.20",
        port: str = "502",
        *args,
        **kwargs,
    ):
        super().__init__(station_resource=station_resource, *args, **kwargs)
        self.station_resource = station_resource  # 物料台面（Deck）
        self.success: bool = False
        self.allow_data_read: bool = False
        self.csv_export_thread = None
        self.csv_export_running = False
        self.csv_export_file: Optional[str] = None

        # 连接 PLC，并注册寄存器节点
        tcp = TCPClient(addr=address, port=port)
        tcp.client.connect()
        self.nodes = BaseClient.load_csv(".../PLC_register.csv")
        self.client = tcp.register_node_list(self.nodes)
```

## 2. 编写驱动与寄存器读写

### 2.1 寄存器示例

- `COIL_SYS_START_CMD`（BOOL，地址 8010）：启动命令（脉冲式）
- `COIL_SYS_START_STATUS`（BOOL，地址 8210）：启动状态
- `REG_DATA_OPEN_CIRCUIT_VOLTAGE`（FLOAT32，地址 10002）：开路电压
- `REG_DATA_ASSEMBLY_PRESSURE`（INT16，地址 10014）：压制扣电压力

### 2.2 最小驱动示例

```python
from unilabos.device_comms.modbus_plc.modbus import WorderOrder

def start_and_read_metrics(self):
    # 1) 下发启动（置 True 再复位 False）
    self.client.use_node('COIL_SYS_START_CMD').write(True)
    self.client.use_node('COIL_SYS_START_CMD').write(False)

    # 2) 等待进入启动状态
    while True:
        status, _ = self.client.use_node('COIL_SYS_START_STATUS').read(1)
        if bool(status[0]):
            break

    # 3) 读取关键数据（FLOAT32 需读 2 个寄存器并指定字节序）
    voltage, _ = self.client.use_node('REG_DATA_OPEN_CIRCUIT_VOLTAGE').read(
        2, word_order=WorderOrder.LITTLE
    )
    pressure, _ = self.client.use_node('REG_DATA_ASSEMBLY_PRESSURE').read(1)

    return {
        'open_circuit_voltage': voltage,
        'assembly_pressure': pressure,
    }
```

> 提示：若需参数下发，可在 PLC 端设置标志寄存器并完成握手复位，避免粘连与竞争。

## 3. Registry 校验与同步

本例的设备定义位于
`unilabos/registry/devices/coin_cell_workstation.yaml`。设备条目的 `class.module`
必须指向可导入的工作站类：

```yaml
module: unilabos.devices.workstation.coin_cell_assembly.coin_cell_assembly:CoinCellAssemblyWorkstation
```

使用完整 Registry 检查模块、动作和状态定义：

```bash
unilab --check_mode --complete_registry --skip_env_check
```

设备图中的 `class` 应与 Registry 顶层设备名一致。正常启动后，Registry 会同步到
本地微后端：

```bash
unilab -g celljson.json --ak <AK> --sk <SK>
```

## 4. 注意事项

### 4.1 PLC 通信注意事项

- **握手机制**：若需参数下发，建议在 PLC 端设置标志寄存器并完成握手复位，避免粘连与竞争
- **字节序**：FLOAT32 等多字节数据类型需要正确指定字节序（如 `WorderOrder.LITTLE`）
- **寄存器映射**：确保 CSV 文件中的寄存器地址与 PLC 实际配置一致
- **连接稳定性**：在初始化时检查 PLC 连接状态，建议添加重连机制

## 5. 扩展阅读

### 相关文档

- {doc}`../add_device` - 设备驱动编写通用指南
- {doc}`../add_registry` - 注册表配置完整指南
- {doc}`workstation_architecture` - 工站架构详解

### 技术要点

- **Modbus TCP 通信**：PLC 通信协议和寄存器读写
- **WorkstationBase**：工站基类的继承和使用
- **寄存器映射**：CSV 格式的寄存器配置
- **注册表生成**：自动化工具使用

## 6. 总结

通过本案例，你应该掌握：

1. ✅ 如何创建 PLC 控制的工站驱动
2. ✅ Modbus TCP 通信和寄存器读写
3. ✅ 校验 Registry 模块、动作与状态定义
4. ✅ 启动微后端并确认 Registry 同步

这个案例展示了完整的 PLC 设备接入流程，可以作为其他类似设备接入的参考模板。
