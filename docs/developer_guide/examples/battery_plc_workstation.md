# 实例：电池装配工站接入（PLC控制）

> **文档类型**：实际应用案例  
> **适用场景**：使用 PLC 控制的电池装配工站接入  
> **前置知识**：{doc}`../add_device` | {doc}`../add_registry`

本指南以电池装配工站为实际案例，引导你完成 PLC 控制设备的完整接入流程，包括新建工站文件、编写驱动与寄存器读写、生成注册表、上传及注意事项。

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

## 3. 本地生成注册表并校验

完成工站类与驱动后，需要生成（或更新）工站注册表供系统识别。


### 3.1 新增工站设备（或资源）首次生成注册表
首先通过以下命令启动unilab。进入unilab系统状态检查页面

```bash
python unilabos\app\main.py -g celljson.json --ak <user的AK> --sk <user的SK>
```

点击注册表编辑，进入注册表编辑页面

![系统状态页面](image_battery_plc/unilab_sys_status.png)

按照图示步骤填写自动生成注册表信息：

![注册表生成流程](image_battery_plc/unilab_registry_process.png)

步骤说明：
1. 选择新增的工站`coin_cell_assembly.py`文件
2. 点击分析按钮，分析`coin_cell_assembly.py`文件
3. 选择`coin_cell_assembly.py`文件中继承`WorkstationBase`类
4. 填写新增的工站.py文件与`unilabos`目录的距离。例如，新增的工站文件`coin_cell_assembly.py`路径为`unilabos\devices\workstation\coin_cell_assembly\coin_cell_assembly.py`，则此处填写`unilabos.devices.workstation.coin_cell_assembly`。
5. 此处填写新定义工站的类的名字（名称可以自拟）
6. 填写新的工站注册表备注信息
7. 生成注册表

以上操作步骤完成，则会生成的新的注册表YAML文件，如下图：

![生成的YAML文件](image_battery_plc/unilab_new_yaml.png)






### 3.2 添加新生成注册表
在`unilabos\registry\devices`目录下新建一个yaml文件，此处新建文件命名为`coincellassemblyworkstation_device.yaml`，将上面生成的新的注册表信息粘贴到`coincellassemblyworkstation_device.yaml`文件中。   

在终端输入以下命令进行注册表补全操作。
```bash
python unilabos\app\register.py --complete_registry
```


### 3.3 启动并上传注册表

新增设备之后，启动unilab需要增加`--upload_registry`参数，来上传注册表信息。

```bash
python unilabos\app\main.py -g celljson.json --ak <user的AK> --sk <user的SK> --upload_registry
```

## 4. 注意事项

### 4.1 验证模块路径

在新生成的 YAML 中，确认 `module` 指向新工站类。本例中需检查 `coincellassemblyworkstation_device.yaml` 文件中是否正确指向了 `CoinCellAssemblyWorkstation` 类：

```yaml
module: unilabos.devices.workstation.coin_cell_assembly.coin_cell_assembly:CoinCellAssemblyWorkstation
```

### 4.2 首次接入流程

首次新增设备（或资源）需要完整流程：
1. ✅ 在网页端生成注册表信息
2. ✅ 使用 `--complete_registry` 补全注册表
3. ✅ 使用 `--upload_registry` 上传注册表信息

### 4.3 驱动更新流程

如果不是新增设备，仅修改了工站驱动的 `.py` 文件：
1. ✅ 运行 `--complete_registry` 补全注册表
2. ✅ 运行 `--upload_registry` 上传注册表
3. ❌ 不需要在网页端重新生成注册表

### 4.4 PLC通信注意事项

- **握手机制**：若需参数下发，建议在 PLC 端设置标志寄存器并完成握手复位，避免粘连与竞争
- **字节序**：FLOAT32 等多字节数据类型需要正确指定字节序（如 `WorderOrder.LITTLE`）
- **寄存器映射**：确保 CSV 文件中的寄存器地址与 PLC 实际配置一致
- **连接稳定性**：在初始化时检查 PLC 连接状态，建议添加重连机制

## 5. 扩展阅读

### 相关文档

- {doc}`../add_device` - 设备驱动编写通用指南
- {doc}`../add_registry` - 注册表配置完整指南
- {doc}`../workstation_architecture` - 工站架构详解

### 技术要点

- **Modbus TCP 通信**：PLC 通信协议和寄存器读写
- **WorkstationBase**：工站基类的继承和使用
- **寄存器映射**：CSV 格式的寄存器配置
- **注册表生成**：自动化工具使用

## 6. 总结

通过本案例，你应该掌握：

1. ✅ 如何创建 PLC 控制的工站驱动
2. ✅ Modbus TCP 通信和寄存器读写
3. ✅ 使用可视化编辑器生成注册表
4. ✅ 注册表的补全和上传流程
5. ✅ 新增设备与更新驱动的区别

这个案例展示了完整的 PLC 设备接入流程，可以作为其他类似设备接入的参考模板。


