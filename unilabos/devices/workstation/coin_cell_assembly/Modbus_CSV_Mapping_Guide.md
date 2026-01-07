# Modbus CSV 地址映射说明

本文档说明 `coin_cell_assembly_a.csv` 文件如何将命名节点映射到实际的 Modbus 地址，以及如何在代码中使用它们。

## 1. CSV 文件结构

地址表文件位于同级目录下：`coin_cell_assembly_a.csv`

每一行定义了一个 Modbus 节点，包含以下关键列：

| 列名 | 说明 | 示例 |
|------|------|------|
| **Name** | **节点名称** (代码中引用的 Key) | `COIL_ALUMINUM_FOIL` |
| **DataType** | 数据类型 (BOOL, INT16, FLOAT32, STRING) | `BOOL` |
| **Comment** | 注释说明 | `使用铝箔垫` |
| **Attribute** | 属性 (通常留空或用于额外标记) | |
| **DeviceType** | Modbus 寄存器类型 (`coil`, `hold_register`) | `coil` |
| **Address** | **Modbus 地址** (十进制) | `8340` |

### 示例行 (铝箔垫片)

```csv
COIL_ALUMINUM_FOIL,BOOL,,使用铝箔垫,,coil,8340,
```

- **名称**: `COIL_ALUMINUM_FOIL`
- **类型**: `coil` (线圈，读写单个位)
- **地址**: `8340`

---

## 2. 加载与注册流程

在 `coin_cell_assembly.py` 的初始化代码中：

1. **加载 CSV**: `BaseClient.load_csv()` 读取 CSV 并解析每行定义。
2. **注册节点**: `modbus_client.register_node_list()` 将解析后的节点注册到 Modbus 客户端实例中。

```python
# 代码位置: coin_cell_assembly.py (L174-175)
self.nodes = BaseClient.load_csv(os.path.join(os.path.dirname(__file__), 'coin_cell_assembly_a.csv'))                            
self.client = modbus_client.register_node_list(self.nodes)
```

---

## 3. 代码中的使用方式

注册后，通过 `self.client.use_node('节点名称')` 即可获取该节点对象并进行读写操作，无需关心具体地址。

### 控制铝箔垫片 (COIL_ALUMINUM_FOIL)

```python
# 代码位置: qiming_coin_cell_code 函数 (L1048)
self.client.use_node('COIL_ALUMINUM_FOIL').write(not lvbodian)
```

- **写入 True**: 对应 Modbus 功能码 05 (Write Single Coil)，向地址 `8340` 写入 `1` (ON)。
- **写入 False**: 向地址 `8340` 写入 `0` (OFF)。

> **注意**: 代码中使用了 `not lvbodian`，这意味着逻辑是反转的。如果 `lvbodian` 参数为 `True` (默认)，写入的是 `False` (不使用铝箔垫)。

---

## 4. 地址转换注意事项 (Modbus vs PLC)

CSV 中的 `Address` 列（如 `8340`）是 **Modbus 协议地址**。

如果使用 InoProShop (汇川 PLC 编程软件)，看到的可能是 **PLC 内部地址** (如 `%QX...` 或 `%MW...`)。这两者之间通常需要转换。

### 常见的转换规则 (示例)

- **Coil (线圈) %QX**: 
  - `Modbus地址 = 字节地址 * 8 + 位偏移`
  - *例子*: `%QX834.0` -> `834 * 8 + 0` = `6672`
  - *注意*: 如果 CSV 中配置的是 `8340`，这可能是一个自定义映射，或者是基于不同规则（如直接对应 Word 地址的某种映射，或者可能就是地址写错了/使用了非标准映射）。

- **Register (寄存器) %MW**:
  - 通常直接对应，或者有偏移量 (如 Modbus 40001 = PLC MW0)。

### 验证方法
由于 `test_unilab_interact.py` 中发现 `8450` (CSV风格) 不工作，而 `6760` (%QX845.0 计算值) 工作正常，**建议对 CSV 中的其他地址也进行核实**，特别是像 `8340` 这样以 0 结尾看起来像是 "字节地址+0" 的数值，可能实际上应该是 `%QX834.0` 对应的 `6672`。

如果发现设备控制无反应，请尝试按照标准的 Modbus 计算方式转换 PLC 地址。
