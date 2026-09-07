#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Tests for Keithley 2400 Series SMU Driver

This module provides unit tests for the Keithley 2400 series SMU driver.
Tests use mock objects to simulate device communication.
"""

import pytest
from unittest.mock import Mock, MagicMock, patch
import sys
import os

# Add parent directory to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', '..'))

from unilabos.devices.keithley.keithley_2400 import (
    Keithley2400,
    Keithley2401,
    Keithley2410,
    Keithley2420,
    Keithley2450,
    Keithley2460,
    Keithley2470,
    SourceMode,
    MeasureMode
)


class TestKeithley2400:
    """Test suite for Keithley2400 driver"""
    
    @patch('unilabos.devices.keithley.keithley_2400.pyvisa')
    def test_init_success(self, mock_pyvisa):
        """Test successful initialization"""
        # Mock PyVISA
        mock_rm = Mock()
        mock_inst = Mock()
        mock_inst.query.return_value = "KEITHLEY INSTRUMENTS INC.,MODEL 2400,12345678,A01"
        mock_rm.open_resource.return_value = mock_inst
        mock_pyvisa.ResourceManager.return_value = mock_rm
        
        # Create driver
        smu = Keithley2400(resource="TCPIP::192.168.1.10::INSTR", reset_on_init=False)
        
        # Verify
        assert smu.success is True
        assert smu._status == "Connected"
        mock_rm.open_resource.assert_called_once()
    
    @patch('unilabos.devices.keithley.keithley_2400.pyvisa')
    def test_set_voltage(self, mock_pyvisa):
        """Test setting voltage source mode"""
        # Mock PyVISA
        mock_rm = Mock()
        mock_inst = Mock()
        mock_inst.query.return_value = "KEITHLEY INSTRUMENTS INC.,MODEL 2400,12345678,A01"
        mock_rm.open_resource.return_value = mock_inst
        mock_pyvisa.ResourceManager.return_value = mock_rm
        
        # Create driver and set voltage
        smu = Keithley2400(resource="TCPIP::192.168.1.10::INSTR", reset_on_init=False)
        smu.set_voltage(5.0, current_limit=0.1)
        
        # Verify commands were sent
        assert any("SOUR:FUNC VOLT" in str(call) for call in mock_inst.write.call_args_list)
        assert any("SOUR:VOLT:LEV 5.0" in str(call) for call in mock_inst.write.call_args_list)
        assert any("SENS:CURR:PROT 0.1" in str(call) for call in mock_inst.write.call_args_list)
    
    @patch('unilabos.devices.keithley.keithley_2400.pyvisa')
    def test_set_current(self, mock_pyvisa):
        """Test setting current source mode"""
        # Mock PyVISA
        mock_rm = Mock()
        mock_inst = Mock()
        mock_inst.query.return_value = "KEITHLEY INSTRUMENTS INC.,MODEL 2400,12345678,A01"
        mock_rm.open_resource.return_value = mock_inst
        mock_pyvisa.ResourceManager.return_value = mock_rm
        
        # Create driver and set current
        smu = Keithley2400(resource="TCPIP::192.168.1.10::INSTR", reset_on_init=False)
        smu.set_current(0.01, voltage_limit=10.0)
        
        # Verify commands were sent
        assert any("SOUR:FUNC CURR" in str(call) for call in mock_inst.write.call_args_list)
        assert any("SOUR:CURR:LEV 0.01" in str(call) for call in mock_inst.write.call_args_list)
        assert any("SENS:VOLT:PROT 10.0" in str(call) for call in mock_inst.write.call_args_list)
    
    @patch('unilabos.devices.keithley.keithley_2400.pyvisa')
    def test_measure(self, mock_pyvisa):
        """Test measurement function"""
        # Mock PyVISA
        mock_rm = Mock()
        mock_inst = Mock()
        mock_inst.query.return_value = "5.123456,0.001234"
        mock_rm.open_resource.return_value = mock_inst
        mock_pyvisa.ResourceManager.return_value = mock_rm
        
        # Create driver and measure
        smu = Keithley2400(resource="TCPIP::192.168.1.10::INSTR", reset_on_init=False)
        voltage, current = smu.measure()
        
        # Verify
        assert voltage == 5.123456
        assert current == 0.001234
    
    @patch('unilabos.devices.keithley.keithley_2400.pyvisa')
    def test_output_control(self, mock_pyvisa):
        """Test output on/off control"""
        # Mock PyVISA
        mock_rm = Mock()
        mock_inst = Mock()
        mock_inst.query.return_value = "KEITHLEY INSTRUMENTS INC.,MODEL 2400,12345678,A01"
        mock_rm.open_resource.return_value = mock_inst
        mock_pyvisa.ResourceManager.return_value = mock_rm
        
        # Create driver
        smu = Keithley2400(resource="TCPIP::192.168.1.10::INSTR", reset_on_init=False)
        
        # Test output on
        smu.output_on()
        assert smu._output_enabled is True
        assert any("OUTP ON" in str(call) for call in mock_inst.write.call_args_list)
        
        # Test output off
        smu.output_off()
        assert smu._output_enabled is False
        assert any("OUTP OFF" in str(call) for call in mock_inst.write.call_args_list)
    
    @patch('unilabos.devices.keithley.keithley_2400.pyvisa')
    def test_voltage_limit(self, mock_pyvisa):
        """Test voltage limit checking"""
        # Mock PyVISA
        mock_rm = Mock()
        mock_inst = Mock()
        mock_inst.query.return_value = "KEITHLEY INSTRUMENTS INC.,MODEL 2400,12345678,A01"
        mock_rm.open_resource.return_value = mock_inst
        mock_pyvisa.ResourceManager.return_value = mock_rm
        
        # Create driver
        smu = Keithley2400(resource="TCPIP::192.168.1.10::INSTR", reset_on_init=False)
        
        # Test voltage beyond limit
        with pytest.raises(ValueError, match="exceeds limit"):
            smu.set_voltage(300.0)  # Exceeds 210V limit
    
    @patch('unilabos.devices.keithley.keithley_2400.pyvisa')
    def test_current_limit(self, mock_pyvisa):
        """Test current limit checking"""
        # Mock PyVISA
        mock_rm = Mock()
        mock_inst = Mock()
        mock_inst.query.return_value = "KEITHLEY INSTRUMENTS INC.,MODEL 2400,12345678,A01"
        mock_rm.open_resource.return_value = mock_inst
        mock_pyvisa.ResourceManager.return_value = mock_rm
        
        # Create driver
        smu = Keithley2400(resource="TCPIP::192.168.1.10::INSTR", reset_on_init=False)
        
        # Test current beyond limit
        with pytest.raises(ValueError, match="exceeds limit"):
            smu.set_current(2.0)  # Exceeds 1.05A limit
    
    @patch('unilabos.devices.keithley.keithley_2400.pyvisa')
    def test_context_manager(self, mock_pyvisa):
        """Test context manager usage"""
        # Mock PyVISA
        mock_rm = Mock()
        mock_inst = Mock()
        mock_inst.query.return_value = "KEITHLEY INSTRUMENTS INC.,MODEL 2400,12345678,A01"
        mock_rm.open_resource.return_value = mock_inst
        mock_pyvisa.ResourceManager.return_value = mock_rm
        
        # Use context manager
        with Keithley2400(resource="TCPIP::192.168.1.10::INSTR", reset_on_init=False) as smu:
            assert smu.success is True
        
        # Verify close was called
        mock_inst.close.assert_called_once()


class TestKeithleyVariants:
    """Test suite for different Keithley model variants"""
    
    @patch('unilabos.devices.keithley.keithley_2400.pyvisa')
    def test_2401_limits(self, mock_pyvisa):
        """Test Keithley 2401 limits"""
        mock_rm = Mock()
        mock_inst = Mock()
        mock_inst.query.return_value = "KEITHLEY INSTRUMENTS INC.,MODEL 2401,12345678,A01"
        mock_rm.open_resource.return_value = mock_inst
        mock_pyvisa.ResourceManager.return_value = mock_rm
        
        smu = Keithley2401(resource="TCPIP::192.168.1.10::INSTR", reset_on_init=False)
        assert smu._voltage_limit == 210.0
        assert smu._current_limit == 1.05
    
    @patch('unilabos.devices.keithley.keithley_2400.pyvisa')
    def test_2410_high_voltage(self, mock_pyvisa):
        """Test Keithley 2410 high-voltage limits"""
        mock_rm = Mock()
        mock_inst = Mock()
        mock_inst.query.return_value = "KEITHLEY INSTRUMENTS INC.,MODEL 2410,12345678,A01"
        mock_rm.open_resource.return_value = mock_inst
        mock_pyvisa.ResourceManager.return_value = mock_rm
        
        smu = Keithley2410(resource="TCPIP::192.168.1.10::INSTR", reset_on_init=False)
        assert smu._voltage_limit == 1100.0  # High voltage model
        assert smu._current_limit == 0.02    # Lower current
    
    @patch('unilabos.devices.keithley.keithley_2400.pyvisa')
    def test_2420_high_current(self, mock_pyvisa):
        """Test Keithley 2420 high-current limits"""
        mock_rm = Mock()
        mock_inst = Mock()
        mock_inst.query.return_value = "KEITHLEY INSTRUMENTS INC.,MODEL 2420,12345678,A01"
        mock_rm.open_resource.return_value = mock_inst
        mock_pyvisa.ResourceManager.return_value = mock_rm
        
        smu = Keithley2420(resource="TCPIP::192.168.1.10::INSTR", reset_on_init=False)
        assert smu._voltage_limit == 60.0   # Lower voltage
        assert smu._current_limit == 3.0    # High current
    
    @patch('unilabos.devices.keithley.keithley_2400.pyvisa')
    def test_2460_high_current(self, mock_pyvisa):
        """Test Keithley 2460 high-current limits"""
        mock_rm = Mock()
        mock_inst = Mock()
        mock_inst.query.return_value = "KEITHLEY INSTRUMENTS INC.,MODEL 2460,12345678,A01"
        mock_rm.open_resource.return_value = mock_inst
        mock_pyvisa.ResourceManager.return_value = mock_rm
        
        smu = Keithley2460(resource="TCPIP::192.168.1.10::INSTR", reset_on_init=False)
        assert smu._voltage_limit == 100.0
        assert smu._current_limit == 7.0     # Highest current
    
    @patch('unilabos.devices.keithley.keithley_2400.pyvisa')
    def test_2470_high_voltage(self, mock_pyvisa):
        """Test Keithley 2470 high-voltage limits"""
        mock_rm = Mock()
        mock_inst = Mock()
        mock_inst.query.return_value = "KEITHLEY INSTRUMENTS INC.,MODEL 2470,12345678,A01"
        mock_rm.open_resource.return_value = mock_inst
        mock_pyvisa.ResourceManager.return_value = mock_rm
        
        smu = Keithley2470(resource="TCPIP::192.168.1.10::INSTR", reset_on_init=False)
        assert smu._voltage_limit == 1100.0  # High voltage
        assert smu._current_limit == 0.105


if __name__ == '__main__':
    pytest.main([__file__, '-v'])