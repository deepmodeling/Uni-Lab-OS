#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Keithley 2400 Series Source Measure Unit (SMU) Driver for Uni-Lab-OS

This driver provides standard interface for Keithley 2400 series SMU operations
including voltage sourcing, current sourcing, and measurement functions.

Supported models:
- Keithley 2400 - General-purpose SMU
- Keithley 2401 - Low-cost SMU
- Keithley 2410 - High-voltage SMU (1100V)
- Keithley 2420 - High-current SMU (3A)
- Keithley 2425 - High-power SMU (100W)
- Keithley 2430 - Pulse mode SMU
- Keithley 2440 - High-power SMU (50W)
- Keithley 2450 - Graphical SMU (modern interface)
- Keithley 2460 - High-current SMU (7A, graphical)
- Keithley 2470 - High-voltage SMU (1100V, graphical)

Reference: https://github.com/tektronix/keithley
"""

import enum
import logging
import time
from typing import Tuple, Optional, Union

try:
    import pyvisa
    PYVISA_AVAILABLE = True
except ImportError:
    PYVISA_AVAILABLE = False

# Import UniversalDriver - handle import error gracefully
try:
    from unilabos.device_comms.universal_driver import UniversalDriver
except ImportError:
    # Fallback for standalone testing
    class UniversalDriver:
        """Fallback UniversalDriver for standalone testing"""
        def __init__(self):
            self.success = False


class SourceMode(enum.Enum):
    """Source operation mode enumeration"""
    VOLTAGE = "VOLTAGE"
    CURRENT = "CURRENT"


class MeasureMode(enum.Enum):
    """Measurement mode enumeration"""
    VOLTAGE = "VOLTAGE"
    CURRENT = "CURRENT"
    RESISTANCE = "RESISTANCE"


class Keithley2400(UniversalDriver):
    """Keithley 2400 Series Source Measure Unit (SMU) Driver
    
    Provides standard interface for SMU operations including:
    - Voltage source mode (with current limit)
    - Current source mode (with voltage limit)
    - Voltage/Current/Resistance measurement
    - I-V sweep capabilities
    - Four-quadrant operation
    
    Communication interfaces:
    - TCP/IP (VISA resource string: TCPIP::<ip>::INSTR)
    - USB (VISA resource string: USB::<vendor>::<serial>::INSTR)
    - GPIB (VISA resource string: GPIB::<address>::INSTR)
    
    Example usage:
        >>> smu = Keithley2400(resource="TCPIP::192.168.1.10::INSTR")
        >>> smu.set_voltage(5.0, current_limit=0.1)  # 5V, 100mA limit
        >>> smu.output_on()
        >>> voltage, current = smu.measure()
        >>> print(f"V={voltage}V, I={current}A")
        >>> smu.output_off()
    """
    
    def __init__(self, resource: str = "TCPIP::192.168.1.10::INSTR",
                 timeout: int = 10, reset_on_init: bool = True):
        """Initialize the Keithley 2400 SMU driver
        
        Args:
            resource: VISA resource string
                - TCP/IP: "TCPIP::<ip>::INSTR"
                - USB: "USB::0x05E6::<model>::<serial>::INSTR"
                - GPIB: "GPIB::<address>::INSTR"
            timeout: Communication timeout in seconds
            reset_on_init: Whether to reset device on initialization
        """
        super().__init__()
        
        self.resource = resource
        self.timeout = timeout
        self.reset_on_init = reset_on_init
        
        # Status properties
        self._status = "Disconnected"
        self._source_mode = SourceMode.VOLTAGE
        self._output_enabled = False
        self._voltage_limit = 210.0  # Max voltage (model dependent)
        self._current_limit = 1.05   # Max current (model dependent)
        
        # ROS2 action result properties
        self.success = False
        self.return_info = ""
        
        # VISA objects
        self.rm = None
        self.instrument = None
        
        # Setup logging
        self.logger = logging.getLogger(f"Keithley2400-{resource}")
        
        # Initialize connection
        self._connect()
        
        if reset_on_init and self.success:
            self.reset()
    
    def _connect(self):
        """Establish connection to the SMU device"""
        if not PYVISA_AVAILABLE:
            self.logger.error("PyVISA not available. Install with: pip install pyvisa")
            self.success = False
            self.return_info = "PyVISA not installed"
            return
        
        try:
            # Create resource manager
            self.rm = pyvisa.ResourceManager()
            
            # Open instrument connection
            self.instrument = self.rm.open_resource(self.resource)
            self.instrument.timeout = self.timeout * 1000  # Convert to ms
            
            # Query device identification
            idn = self.instrument.query("*IDN?")
            self.logger.info(f"Connected to: {idn.strip()}")
            
            self._status = "Connected"
            self.success = True
            self.return_info = f"Connected to {idn.strip()}"
            
        except Exception as e:
            self.logger.error(f"Failed to connect: {e}")
            self._status = "Error"
            self.success = False
            self.return_info = str(e)
    
    def _send_command(self, command: str):
        """Send SCPI command to the device
        
        Args:
            command: SCPI command string
        """
        if not self.instrument:
            raise RuntimeError("Device not connected")
        
        self.logger.debug(f"Sending command: {command}")
        self.instrument.write(command)
    
    def _query(self, command: str) -> str:
        """Send SCPI query and return response
        
        Args:
            command: SCPI query string
            
        Returns:
            Response string from device
        """
        if not self.instrument:
            raise RuntimeError("Device not connected")
        
        self.logger.debug(f"Querying: {command}")
        response = self.instrument.query(command)
        self.logger.debug(f"Response: {response.strip()}")
        return response.strip()
    
    def reset(self):
        """Reset the device to default settings"""
        self._send_command("*RST")
        self._send_command("*CLS")  # Clear status registers
        self._status = "Reset"
        self.logger.info("Device reset to default settings")
    
    def set_voltage(self, voltage: float, current_limit: float = None):
        """Set voltage source mode with optional current limit
        
        Args:
            voltage: Output voltage in Volts
            current_limit: Current compliance limit in Amps (optional)
        """
        if abs(voltage) > self._voltage_limit:
            raise ValueError(f"Voltage {voltage}V exceeds limit {self._voltage_limit}V")
        
        # Set source function to voltage
        self._send_command(":SOUR:FUNC VOLT")
        self._source_mode = SourceMode.VOLTAGE
        
        # Set voltage level
        self._send_command(f":SOUR:VOLT:LEV {voltage}")
        
        # Set current compliance limit
        if current_limit is not None:
            if abs(current_limit) > self._current_limit:
                raise ValueError(f"Current limit {current_limit}A exceeds max {self._current_limit}A")
            self._send_command(f":SENS:CURR:PROT {current_limit}")
        
        self.logger.info(f"Set voltage source: {voltage}V (compliance: {current_limit}A)")
    
    def set_current(self, current: float, voltage_limit: float = None):
        """Set current source mode with optional voltage limit
        
        Args:
            current: Output current in Amps
            voltage_limit: Voltage compliance limit in Volts (optional)
        """
        if abs(current) > self._current_limit:
            raise ValueError(f"Current {current}A exceeds limit {self._current_limit}A")
        
        # Set source function to current
        self._send_command(":SOUR:FUNC CURR")
        self._source_mode = SourceMode.CURRENT
        
        # Set current level
        self._send_command(f":SOUR:CURR:LEV {current}")
        
        # Set voltage compliance limit
        if voltage_limit is not None:
            if abs(voltage_limit) > self._voltage_limit:
                raise ValueError(f"Voltage limit {voltage_limit}V exceeds max {self._voltage_limit}V")
            self._send_command(f":SENS:VOLT:PROT {voltage_limit}")
        
        self.logger.info(f"Set current source: {current}A (compliance: {voltage_limit}V)")
    
    def output_on(self):
        """Turn on output"""
        self._send_command(":OUTP ON")
        self._output_enabled = True
        self._status = "Output ON"
        self.logger.info("Output enabled")
    
    def output_off(self):
        """Turn off output"""
        self._send_command(":OUTP OFF")
        self._output_enabled = False
        self._status = "Output OFF"
        self.logger.info("Output disabled")
    
    def measure(self) -> Tuple[float, float]:
        """Measure voltage and current simultaneously
        
        Returns:
            Tuple of (voltage, current) readings
        """
        # Enable both voltage and current measurement
        self._send_command(":SENS:FUNC:CONC ON")
        self._send_command(":SENS:FUNC:ON 'VOLT','CURR'")
        
        # Trigger measurement and read
        response = self._query(":READ?")
        values = response.split(',')
        
        voltage = float(values[0])
        current = float(values[1])
        
        self.logger.debug(f"Measured: V={voltage}V, I={current}A")
        return voltage, current
    
    def measure_voltage(self) -> float:
        """Measure voltage
        
        Returns:
            Voltage reading in Volts
        """
        self._send_command(":SENS:FUNC 'VOLT'")
        voltage = float(self._query(":MEAS:VOLT?"))
        self.logger.debug(f"Measured voltage: {voltage}V")
        return voltage
    
    def measure_current(self) -> float:
        """Measure current
        
        Returns:
            Current reading in Amps
        """
        self._send_command(":SENS:FUNC 'CURR'")
        current = float(self._query(":MEAS:CURR?"))
        self.logger.debug(f"Measured current: {current}A")
        return current
    
    def measure_resistance(self) -> float:
        """Measure resistance using ohms function
        
        Returns:
            Resistance reading in Ohms
        """
        self._send_command(":SENS:FUNC 'RES'")
        resistance = float(self._query(":MEAS:RES?"))
        self.logger.debug(f"Measured resistance: {resistance}Ω")
        return resistance
    
    def set_voltage_range(self, voltage_range: float):
        """Set voltage measurement range
        
        Args:
            voltage_range: Voltage range in Volts (auto if 0)
        """
        if voltage_range == 0:
            self._send_command(":SENS:VOLT:RANG:AUTO ON")
        else:
            self._send_command(f":SENS:VOLT:RANG {voltage_range}")
        self.logger.info(f"Set voltage range: {voltage_range}V")
    
    def set_current_range(self, current_range: float):
        """Set current measurement range
        
        Args:
            current_range: Current range in Amps (auto if 0)
        """
        if current_range == 0:
            self._send_command(":SENS:CURR:RANG:AUTO ON")
        else:
            self._send_command(f":SENS:CURR:RANG {current_range}")
        self.logger.info(f"Set current range: {current_range}A")
    
    def configure_sweep(self, start: float, stop: float, steps: int,
                       mode: SourceMode = SourceMode.VOLTAGE,
                       sweep_type: str = "LIN"):
        """Configure linear or logarithmic sweep
        
        Args:
            start: Start value
            stop: Stop value
            steps: Number of sweep points
            mode: Source mode (VOLTAGE or CURRENT)
            sweep_type: Sweep type - "LIN" (linear) or "LOG" (logarithmic)
        """
        if mode == SourceMode.VOLTAGE:
            self._send_command(":SOUR:FUNC VOLT")
            self._send_command(f":SOUR:VOLT:STAR {start}")
            self._send_command(f":SOUR:VOLT:STOP {stop}")
            self._send_command(f":SOUR:VOLT:POIN {steps}")
        else:
            self._send_command(":SOUR:FUNC CURR")
            self._send_command(f":SOUR:CURR:STAR {start}")
            self._send_command(f":SOUR:CURR:STOP {stop}")
            self._send_command(f":SOUR:CURR:POIN {steps}")
        
        # Set sweep mode
        if sweep_type == "LOG":
            self._send_command(":SOUR:SWE:SPAC LOG")
        else:
            self._send_command(":SOUR:SWE:SPAC LIN")
        
        self.logger.info(f"Configured {sweep_type} sweep: {start} to {stop} in {steps} steps")
    
    def run_sweep(self) -> list:
        """Execute configured sweep and return measurement data
        
        Returns:
            List of (voltage, current) tuples
        """
        self._send_command(":SENS:FUNC:CONC ON")
        self._send_command(":SENS:FUNC:ON 'VOLT','CURR'")
        
        # Trigger sweep and read all data
        response = self._query(":READ?")
        values = [float(x) for x in response.split(',')]
        
        # Parse into voltage-current pairs
        data = []
        for i in range(0, len(values), 2):
            if i+1 < len(values):
                data.append((values[i], values[i+1]))
        
        self.logger.info(f"Sweep completed: {len(data)} points")
        return data
    
    def get_error(self) -> Tuple[int, str]:
        """Query device error
        
        Returns:
            Tuple of (error_code, error_message)
        """
        response = self._query(":SYST:ERR?")
        parts = response.split(',')
        error_code = int(parts[0])
        error_msg = parts[1].strip().strip('"')
        return error_code, error_msg
    
    def close(self):
        """Close connection to the device"""
        if self.instrument:
            try:
                self.output_off()
                self.instrument.close()
                self.logger.info("Device connection closed")
            except Exception as e:
                self.logger.error(f"Error closing connection: {e}")
        
        if self.rm:
            self.rm.close()
        
        self._status = "Disconnected"
        self.success = False
    
    def __del__(self):
        """Destructor - ensure device is closed"""
        self.close()
    
    def __enter__(self):
        """Context manager entry"""
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit"""
        self.close()
        return False


# Convenience aliases for specific models
class Keithley2401(Keithley2400):
    """Keithley 2401 Low-cost SMU"""
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._voltage_limit = 210.0
        self._current_limit = 1.05


class Keithley2410(Keithley2400):
    """Keithley 2410 High-voltage SMU (1100V)"""
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._voltage_limit = 1100.0
        self._current_limit = 0.02


class Keithley2420(Keithley2400):
    """Keithley 2420 High-current SMU (3A)"""
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._voltage_limit = 60.0
        self._current_limit = 3.0


class Keithley2450(Keithley2400):
    """Keithley 2450 Graphical SMU"""
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._voltage_limit = 210.0
        self._current_limit = 1.05


class Keithley2460(Keithley2400):
    """Keithley 2460 High-current SMU (7A)"""
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._voltage_limit = 100.0
        self._current_limit = 7.0


class Keithley2470(Keithley2400):
    """Keithley 2470 High-voltage SMU (1100V)"""
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._voltage_limit = 1100.0
        self._current_limit = 0.105