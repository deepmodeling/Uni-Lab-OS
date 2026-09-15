# Keithley Source Measure Units (SMU) Driver

This module provides drivers for Keithley 2400 series Source Measure Units (SMUs) for Uni-Lab-OS.

## Supported Models

| Model | Description | Voltage Range | Current Range | Power |
|-------|-------------|---------------|---------------|-------|
| Keithley 2400 | General-purpose SMU | ±210V | ±1.05A | 22W |
| Keithley 2401 | Low-cost SMU | ±210V | ±1.05A | 22W |
| Keithley 2410 | High-voltage SMU | ±1100V | ±20mA | 22W |
| Keithley 2420 | High-current SMU | ±60V | ±3A | 60W |
| Keithley 2425 | High-power SMU | ±100V | ±3A | 100W |
| Keithley 2450 | Graphical SMU | ±210V | ±1.05A | 22W |
| Keithley 2460 | High-current SMU | ±100V | ±7A | 100W |
| Keithley 2470 | High-voltage SMU | ±1100V | ±105mA | 110W |

## Features

- ✅ Voltage source mode with current compliance
- ✅ Current source mode with voltage compliance
- ✅ Simultaneous voltage/current measurement
- ✅ Resistance measurement
- ✅ Linear and logarithmic sweep
- ✅ Auto-ranging support
- ✅ Multiple communication interfaces (TCP/IP, USB, GPIB)
- ✅ Context manager support

## Installation

### Prerequisites

```bash
# Install PyVISA for instrument communication
pip install pyvisa

# Install PyVISA-py backend (optional, for TCP/IP/USB without NI-VISA)
pip install pyvisa-py

# Or install NI-VISA for full hardware support
# Download from: https://www.ni.com/en-us/support/downloads/drivers/download.ni-visa.html
```

### Uni-Lab-OS Installation

This driver is included in Uni-Lab-OS. Install Uni-Lab-OS:

```bash
# Using conda/mamba (recommended)
mamba install uni-lab::unilabos -c robostack-staging -c conda-forge

# Or from source
git clone https://github.com/deepmodeling/Uni-Lab-OS.git
cd Uni-Lab-OS
pip install -e .
```

## Quick Start

### Basic Voltage Source

```python
from unilabos.devices.keithley import Keithley2400

# Connect to SMU via TCP/IP
smu = Keithley2400(resource="TCPIP::192.168.1.10::INSTR")

# Set voltage source mode (5V output, 100mA current limit)
smu.set_voltage(5.0, current_limit=0.1)

# Enable output
smu.output_on()

# Measure voltage and current
voltage, current = smu.measure()
print(f"V={voltage:.3f}V, I={current:.6f}A")

# Disable output
smu.output_off()

# Close connection
smu.close()
```

### Current Source Mode

```python
from unilabos.devices.keithley import Keithley2400

with Keithley2400(resource="TCPIP::192.168.1.10::INSTR") as smu:
    # Set current source mode (10mA output, 10V voltage limit)
    smu.set_current(0.01, voltage_limit=10.0)
    
    smu.output_on()
    
    voltage, current = smu.measure()
    print(f"V={voltage:.3f}V, I={current:.6f}A")
    
    smu.output_off()
```

### I-V Sweep

```python
from unilabos.devices.keithley import Keithley2400

with Keithley2400(resource="TCPIP::192.168.1.10::INSTR") as smu:
    # Configure voltage sweep: 0V to 5V, 11 points
    smu.configure_sweep(
        start=0.0,
        stop=5.0,
        steps=11,
        mode=SourceMode.VOLTAGE,
        sweep_type="LIN"
    )
    
    # Set current compliance
    smu.set_voltage(0, current_limit=0.1)
    
    # Run sweep
    smu.output_on()
    data = smu.run_sweep()
    smu.output_off()
    
    # Plot results
    import matplotlib.pyplot as plt
    voltages = [d[0] for d in data]
    currents = [d[1] for d in data]
    plt.plot(voltages, currents)
    plt.xlabel("Voltage (V)")
    plt.ylabel("Current (A)")
    plt.show()
```

### Resistance Measurement

```python
from unilabos.devices.keithley import Keithley2400

with Keithley2400(resource="TCPIP::192.168.1.10::INSTR") as smu:
    smu.output_on()
    resistance = smu.measure_resistance()
    print(f"Resistance: {resistance:.2f}Ω")
    smu.output_off()
```

## Communication Interfaces

### TCP/IP

```python
# VISA resource string format: TCPIP::<ip>::INSTR
smu = Keithley2400(resource="TCPIP::192.168.1.10::INSTR")
```

### USB

```python
# VISA resource string format: USB::<vendor>::<serial>::INSTR
# Keithley vendor ID: 0x05E6
smu = Keithley2400(resource="USB::0x05E6::2400::123456::INSTR")
```

### GPIB

```python
# VISA resource string format: GPIB::<address>::INSTR
smu = Keithley2400(resource="GPIB::24::INSTR")
```

## API Reference

### Core Methods

| Method | Description |
|--------|-------------|
| `set_voltage(voltage, current_limit)` | Set voltage source mode with current compliance |
| `set_current(current, voltage_limit)` | Set current source mode with voltage compliance |
| `output_on()` | Enable output |
| `output_off()` | Disable output |
| `measure()` | Measure voltage and current simultaneously |
| `measure_voltage()` | Measure voltage |
| `measure_current()` | Measure current |
| `measure_resistance()` | Measure resistance |
| `reset()` | Reset device to default settings |

### Range Configuration

| Method | Description |
|--------|-------------|
| `set_voltage_range(range)` | Set voltage measurement range (0 for auto) |
| `set_current_range(range)` | Set current measurement range (0 for auto) |

### Sweep Functions

| Method | Description |
|--------|-------------|
| `configure_sweep(start, stop, steps, mode, sweep_type)` | Configure sweep parameters |
| `run_sweep()` | Execute sweep and return data |

## Application Examples

### 1. Diode I-V Characterization

```python
from unilabos.devices.keithley import Keithley2400
import matplotlib.pyplot as plt

with Keithley2400(resource="TCPIP::192.168.1.10::INSTR") as smu:
    # Forward bias sweep (0V to 0.7V)
    smu.configure_sweep(0, 0.7, 71, mode=SourceMode.VOLTAGE)
    smu.set_voltage(0, current_limit=0.1)
    
    smu.output_on()
    forward_data = smu.run_sweep()
    smu.output_off()
    
    # Plot
    plt.semilogy([d[0] for d in forward_data], 
                 [abs(d[1]) for d in forward_data])
    plt.xlabel("Voltage (V)")
    plt.ylabel("Current (A)")
    plt.title("Diode I-V Characteristic")
    plt.grid(True)
    plt.show()
```

### 2. Solar Cell Testing

```python
from unilabos.devices.keithley import Keithley2400

with Keithley2400(resource="TCPIP::192.168.1.10::INSTR") as smu:
    # I-V sweep for solar cell
    smu.configure_sweep(0, 0.6, 61, mode=SourceMode.VOLTAGE)
    smu.set_voltage(0, current_limit=1.0)
    
    smu.output_on()
    data = smu.run_sweep()
    smu.output_off()
    
    # Calculate parameters
    voltages = [d[0] for d in data]
    currents = [d[1] for d in data]
    powers = [v * i for v, i in data]
    
    voc = voltages[currents.index(min(currents, key=abs))]  # Open circuit voltage
    isc = min(currents, key=abs)  # Short circuit current
    pmax = max(powers)  # Maximum power
    ff = pmax / (voc * abs(isc))  # Fill factor
    
    print(f"Voc = {voc:.3f}V")
    print(f"Isc = {abs(isc):.3f}A")
    print(f"Pmax = {pmax:.3f}W")
    print(f"FF = {ff:.3f}")
```

### 3. Transistor Testing

```python
from unilabos.devices.keithley import Keithley2400
import numpy as np

# Requires two SMUs for gate and drain
smu_gate = Keithley2400(resource="TCPIP::192.168.1.10::INSTR")
smu_drain = Keithley2400(resource="TCPIP::192.168.1.11::INSTR")

# MOSFET output characteristics
vgs_values = [2, 3, 4, 5]  # Gate voltages

for vgs in vgs_values:
    smu_gate.set_voltage(vgs, current_limit=0.01)
    smu_gate.output_on()
    
    smu_drain.configure_sweep(0, 10, 51, mode=SourceMode.VOLTAGE)
    smu_drain.set_voltage(0, current_limit=0.1)
    
    smu_drain.output_on()
    data = smu_drain.run_sweep()
    smu_drain.output_off()
    
    # Plot each curve
    plt.plot([d[0] for d in data], [d[1] for d in data], 
             label=f"Vgs={vgs}V")

smu_gate.output_off()
plt.xlabel("Vds (V)")
plt.ylabel("Id (A)")
plt.legend()
plt.grid(True)
plt.title("MOSFET Output Characteristics")
plt.show()

smu_gate.close()
smu_drain.close()
```

## Troubleshooting

### Connection Issues

1. **PyVISA not found**
   ```bash
   pip install pyvisa pyvisa-py
   ```

2. **Device not found**
   - Check resource string format
   - Verify device IP address (for TCP/IP)
   - Check USB connection (for USB)
   - Verify GPIB address (for GPIB)

3. **Timeout errors**
   - Increase timeout parameter: `Keithley2400(resource="...", timeout=30)`
   - Check device is powered on and responsive

### Measurement Issues

1. **Incorrect readings**
   - Check measurement range settings
   - Verify source/compliance limits
   - Use `reset()` to restore default settings

2. **Compliance triggered**
   - Increase compliance limit
   - Check device under test
   - Verify wiring and connections

## References

- [Keithley GitHub Repository](https://github.com/tektronix/keithley)
- [Keithley 2400 Series User Manual](https://www.tek.com/en/manual/keithley/2400-series-sourcemeter)
- [PyVISA Documentation](https://pyvisa.readthedocs.io/)
- [Uni-Lab-OS Documentation](https://deepmodeling.github.io/Uni-Lab-OS/)

## License

- Main Framework: GPL-3.0
- Device Drivers: DP Technology Proprietary License

See [NOTICE](../../NOTICE) for complete licensing details.