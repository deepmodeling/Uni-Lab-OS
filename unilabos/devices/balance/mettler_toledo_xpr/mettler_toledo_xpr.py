#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Mettler Toledo XPR/XSR Balance Driver for Uni-Lab OS

This driver provides standard interface for Mettler Toledo XPR/XSR balance operations
including tare, zero, and weight reading functions.
"""

import enum
import base64
import hashlib
import logging
import time
from pathlib import Path
from decimal import Decimal
from typing import Tuple, Optional

from jinja2 import Template
from requests import Session
from zeep import Client
from zeep.transports import Transport
import pprp

# Import UniversalDriver - handle import error gracefully
try:
    from unilabos.device_comms.universal_driver import UniversalDriver
except ImportError:
    # Fallback for standalone testing
    class UniversalDriver:
        """Fallback UniversalDriver for standalone testing"""

        def __init__(self):
            self.success = False


class Outcome(enum.Enum):
    """Balance operation outcome enumeration"""
    SUCCESS = "Success"
    ERROR = "Error"


class MettlerToledoXPR(UniversalDriver):
    """Mettler Toledo XPR/XSR Balance Driver

    Provides standard interface for balance operations including:
    - Tare (去皮)
    - Zero (清零) 
    - Weight reading (读数)
    """

    def __init__(self, ip: str = "192.168.1.10", port: int = 81,
                 password: str = "123456", timeout: int = 10):
        """Initialize the balance driver

        Args:
            ip: Balance IP address
            port: Balance port number
            password: Balance password
            timeout: Connection timeout in seconds
        """
        super().__init__()

        self.ip = ip
        self.port = port
        self.password = password
        self.timeout = timeout
        self.api_path = "MT/Laboratory/Balance/XprXsr/V03"

        # Status properties
        self._status = "Disconnected"
        self._last_weight = 0.0
        self._last_unit = "g"
        self._is_stable = False
        self._error_message = ""

        # ROS2 action result properties
        self.success = False
        self.return_info = ""

        # Service objects
        self.client = None
        self.session_svc = None
        self.weighing_svc = None
        self.session_id = None

        # WSDL template path
        self.wsdl_template = Path(__file__).parent / "MT.Laboratory.Balance.XprXsr.V03.wsdl"

        # Bindings
        self.bindings = {
            "session": "{http://MT/Laboratory/Balance/XprXsr/V03}BasicHttpBinding_ISessionService",
            "weigh": "{http://MT/Laboratory/Balance/XprXsr/V03}BasicHttpBinding_IWeighingService",
        }

        # Setup logging
        self.logger = logging.getLogger(f"MettlerToledoXPR-{ip}")

        # Initialize connection
        self._connect()

    @property
    def status(self) -> str:
        """Current device status"""
        return self._status

    @property
    def weight(self) -> float:
        """Last measured weight value"""
        return self._last_weight

    @property
    def unit(self) -> str:
        """Weight unit (e.g., 'g', 'kg')"""
        return self._last_unit

    @property
    def is_stable(self) -> bool:
        """Whether the weight reading is stable"""
        return self._is_stable

    @property
    def error_message(self) -> str:
        """Last error message"""
        return self._error_message

    def _decrypt_session_id(self, pw: str, enc_sid: str, salt: str) -> str:
        """Decrypt session ID using password and salt"""
        key = hashlib.pbkdf2_hmac("sha1", pw.encode(),
                                  base64.b64decode(salt), 1000, dklen=32)
        plain = pprp.decrypt_sink(
            pprp.rijndael_decrypt_gen(
                key, pprp.data_source_gen(base64.b64decode(enc_sid))))
        return plain.decode()

    def _render_wsdl(self) -> Path:
        """Render WSDL template with current connection parameters"""
        if not self.wsdl_template.exists():
            raise FileNotFoundError(f"WSDL template not found: {self.wsdl_template}")

        text = Template(self.wsdl_template.read_text(encoding="utf-8")).render(
            host=self.ip, port=self.port, api_path=self.api_path)

        wsdl_path = self.wsdl_template.parent / f"rendered_{self.ip}_{self.port}.wsdl"
        wsdl_path.write_text(text, encoding="utf-8")

        return wsdl_path

    def _connect(self):
        """Establish connection to the balance"""
        try:
            self._status = "Connecting"

            # Render WSDL
            wsdl_path = self._render_wsdl()
            self.logger.info(f"WSDL rendered to {wsdl_path}")

            # Create SOAP client
            transport = Transport(session=Session(), timeout=self.timeout)
            self.client = Client(wsdl=str(wsdl_path), transport=transport)

            # Create service proxies
            base_url = f"http://{self.ip}:{self.port}/{self.api_path}"
            self.session_svc = self.client.create_service(
                self.bindings["session"], f"{base_url}/SessionService")
            self.weighing_svc = self.client.create_service(
                self.bindings["weigh"], f"{base_url}/WeighingService")

            self.logger.info("Zeep service proxies created")

            # Open session
            self.logger.info("Opening session...")
            reply = self.session_svc.OpenSession()
            if reply.Outcome != Outcome.SUCCESS.value:
                raise RuntimeError(f"OpenSession failed: {getattr(reply, 'ErrorMessage', '')}")

            self.session_id = self._decrypt_session_id(
                self.password, reply.SessionId, reply.Salt)

            self.logger.info(f"Session established successfully, SessionId={self.session_id}")
            self._status = "Connected"
            self._error_message = ""

        except Exception as e:
            self._status = "Error"
            self._error_message = str(e)
            self.logger.error(f"Connection failed: {e}")
            raise

    def _ensure_connected(self):
        """Ensure the device is connected"""
        if self._status != "Connected" or self.session_id is None:
            self._connect()

    def tare(self, immediate: bool = False) -> bool:
        """Perform tare operation (去皮)

        Args:
            immediate: Whether to perform immediate tare

        Returns:
            bool: True if successful, False otherwise
        """
        try:
            self._ensure_connected()
            self._status = "Taring"

            self.logger.info(f"Performing tare (immediate={immediate})...")
            reply = self.weighing_svc.Tare(self.session_id, immediate)

            if reply.Outcome != Outcome.SUCCESS.value:
                error_msg = getattr(reply, 'ErrorMessage', 'Unknown error')
                self.logger.error(f"Tare failed: {error_msg}")
                self._error_message = f"Tare failed: {error_msg}"
                self._status = "Error"
                return False

            self.logger.info("Tare completed successfully")
            self._status = "Connected"
            self._error_message = ""
            return True

        except Exception as e:
            self.logger.error(f"Tare operation failed: {e}")
            self._error_message = str(e)
            self._status = "Error"
            return False

    def zero(self, immediate: bool = False) -> bool:
        """Perform zero operation (清零)

        Args:
            immediate: Whether to perform immediate zero

        Returns:
            bool: True if successful, False otherwise
        """
        try:
            self._ensure_connected()
            self._status = "Zeroing"

            self.logger.info(f"Performing zero (immediate={immediate})...")
            reply = self.weighing_svc.Zero(self.session_id, immediate)

            if reply.Outcome != Outcome.SUCCESS.value:
                error_msg = getattr(reply, 'ErrorMessage', 'Unknown error')
                self.logger.error(f"Zero failed: {error_msg}")
                self._error_message = f"Zero failed: {error_msg}"
                self._status = "Error"
                return False

            self.logger.info("Zero completed successfully")
            self._status = "Connected"
            self._error_message = ""
            return True

        except Exception as e:
            self.logger.error(f"Zero operation failed: {e}")
            self._error_message = str(e)
            self._status = "Error"
            return False

    def get_weight(self) -> float:
        """Get current weight reading (读数)

        Returns:
            float: Weight value
        """
        try:
            self._ensure_connected()
            self._status = "Reading"

            self.logger.info("Getting weight...")
            reply = self.weighing_svc.GetWeight(self.session_id)

            if reply.Outcome != Outcome.SUCCESS.value:
                error_msg = getattr(reply, 'ErrorMessage', 'Unknown error')
                self.logger.error(f"GetWeight failed: {error_msg}")
                self._error_message = f"GetWeight failed: {error_msg}"
                self._status = "Error"
                return 0.0

            # Handle different response structures
            if hasattr(reply, 'WeightSample'):
                # Handle WeightSample structure (most common for XPR)
                weight_sample = reply.WeightSample
                if hasattr(weight_sample, 'NetWeight'):
                    weight_val = float(Decimal(weight_sample.NetWeight.Value))
                    weight_unit = weight_sample.NetWeight.Unit
                elif hasattr(weight_sample, 'GrossWeight'):
                    weight_val = float(Decimal(weight_sample.GrossWeight.Value))
                    weight_unit = weight_sample.GrossWeight.Unit
                else:
                    weight_val = 0.0
                    weight_unit = 'g'
                is_stable = getattr(weight_sample, 'Stable', True)
            elif hasattr(reply, 'Weight'):
                weight_val = float(Decimal(reply.Weight.Value))
                weight_unit = reply.Weight.Unit
                is_stable = getattr(reply.Weight, 'IsStable', True)
            elif hasattr(reply, 'Value'):
                weight_val = float(Decimal(reply.Value))
                weight_unit = getattr(reply, 'Unit', 'g')
                is_stable = getattr(reply, 'IsStable', True)
            else:
                # Try to extract from reply attributes
                weight_val = float(Decimal(getattr(reply, 'WeightValue', getattr(reply, 'Value', '0'))))
                weight_unit = getattr(reply, 'WeightUnit', getattr(reply, 'Unit', 'g'))
                is_stable = getattr(reply, 'IsStable', True)

            # Convert to grams for consistent output (ROS2 requirement)
            if weight_unit.lower() in ['milligram', 'mg']:
                weight_val_grams = weight_val / 1000.0
            elif weight_unit.lower() in ['kilogram', 'kg']:
                weight_val_grams = weight_val * 1000.0
            elif weight_unit.lower() in ['gram', 'g']:
                weight_val_grams = weight_val
            else:
                # Default to assuming grams if unit is unknown
                weight_val_grams = weight_val
                self.logger.warning(f"Unknown weight unit: {weight_unit}, assuming grams")

            # Update internal state (keep original values for reference)
            self._last_weight = weight_val
            self._last_unit = weight_unit
            self._is_stable = is_stable

            self.logger.info(f"Weight: {weight_val_grams} g (original: {weight_val} {weight_unit})")
            self._status = "Connected"
            self._error_message = ""

            return weight_val_grams

        except Exception as e:
            self.logger.error(f"Get weight failed: {e}")
            self._error_message = str(e)
            self._status = "Error"
            return 0.0

    def get_weight_with_unit(self) -> Tuple[float, str]:
        """Get current weight reading with unit (读数含单位)

        Returns:
            Tuple[float, str]: Weight value and unit
        """
        try:
            self._ensure_connected()
            self._status = "Reading"

            self.logger.info("Getting weight with unit...")
            reply = self.weighing_svc.GetWeight(self.session_id)

            if reply.Outcome != Outcome.SUCCESS.value:
                error_msg = getattr(reply, 'ErrorMessage', 'Unknown error')
                self.logger.error(f"GetWeight failed: {error_msg}")
                self._error_message = f"GetWeight failed: {error_msg}"
                self._status = "Error"
                return 0.0, ""

            # Handle different response structures
            if hasattr(reply, 'WeightSample'):
                # Handle WeightSample structure (most common for XPR)
                weight_sample = reply.WeightSample
                if hasattr(weight_sample, 'NetWeight'):
                    weight_val = float(Decimal(weight_sample.NetWeight.Value))
                    weight_unit = weight_sample.NetWeight.Unit
                elif hasattr(weight_sample, 'GrossWeight'):
                    weight_val = float(Decimal(weight_sample.GrossWeight.Value))
                    weight_unit = weight_sample.GrossWeight.Unit
                else:
                    weight_val = 0.0
                    weight_unit = 'g'
                is_stable = getattr(weight_sample, 'Stable', True)
            elif hasattr(reply, 'Weight'):
                weight_val = float(Decimal(reply.Weight.Value))
                weight_unit = reply.Weight.Unit
                is_stable = getattr(reply.Weight, 'IsStable', True)
            elif hasattr(reply, 'Value'):
                weight_val = float(Decimal(reply.Value))
                weight_unit = getattr(reply, 'Unit', 'g')
                is_stable = getattr(reply, 'IsStable', True)
            else:
                # Try to extract from reply attributes
                weight_val = float(Decimal(getattr(reply, 'WeightValue', getattr(reply, 'Value', '0'))))
                weight_unit = getattr(reply, 'WeightUnit', getattr(reply, 'Unit', 'g'))
                is_stable = getattr(reply, 'IsStable', True)

            # Update internal state
            self._last_weight = weight_val
            self._last_unit = weight_unit
            self._is_stable = is_stable

            self.logger.info(f"Weight: {weight_val} {weight_unit}")
            self._status = "Connected"
            self._error_message = ""

            return weight_val, weight_unit

        except Exception as e:
            self.logger.error(f"Get weight with unit failed: {e}")
            self._error_message = str(e)
            self._status = "Error"
            return 0.0, ""

    def send_cmd(self, command: str) -> dict:
        """ROS2 SendCmd action handler

        Args:
            command: JSON string containing command and parameters

        Returns:
            dict: Result containing success status and return_info
        """
        return self.execute_command_from_outer(command)

    def execute_command_from_outer(self, command: str) -> dict:
        """Execute command from ROS2 SendCmd action

        Args:
            command: JSON string containing command and parameters

        Returns:
            dict: Result containing success status and return_info
        """
        try:
            import json
            # Parse JSON command
            cmd_data = json.loads(command.replace("'", '"').replace("False", "false").replace("True", "true"))

            # Extract command name and parameters
            cmd_name = cmd_data.get('command', '')
            params = cmd_data.get('params', {})

            self.logger.info(f"Executing command: {cmd_name} with params: {params}")

            # Execute different commands
            if cmd_name == 'tare':
                immediate = params.get('immediate', False)
                success = self.tare(immediate)
                result = {
                    'success': success,
                    'return_info': f"Tare operation {'successful' if success else 'failed'}"
                }
                # Update instance attributes for ROS2 action system
                self.success = result['success']
                self.return_info = result['return_info']
                return result

            elif cmd_name == 'zero':
                immediate = params.get('immediate', False)
                success = self.zero(immediate)
                result = {
                    'success': success,
                    'return_info': f"Zero operation {'successful' if success else 'failed'}"
                }
                # Update instance attributes for ROS2 action system
                self.success = result['success']
                self.return_info = result['return_info']
                return result

            elif cmd_name == 'read' or cmd_name == 'get_weight':
                try:
                    self.logger.info(f"Executing {cmd_name} command via ROS2...")
                    self.logger.info(f"Current status: {self._status}")

                    # Use get_weight to get weight value (returns float in grams)
                    weight_grams = self.get_weight()
                    self.logger.info(f"get_weight() returned: {weight_grams} g")

                    # Get the original weight and unit for display
                    original_weight = getattr(self, '_last_weight', weight_grams)
                    original_unit = getattr(self, '_last_unit', 'g')
                    self.logger.info(f"Original reading: {original_weight} {original_unit}")

                    result = {
                        'success': True,
                        'return_info': f"Weight: {original_weight} {original_unit}"
                    }
                except Exception as e:
                    self.logger.error(f"Exception in {cmd_name}: {str(e)}")
                    self.logger.error(f"Exception type: {type(e).__name__}")
                    import traceback
                    self.logger.error(f"Traceback: {traceback.format_exc()}")
                    result = {
                        'success': False,
                        'return_info': f"Failed to read weight: {str(e)}"
                    }
                # Update instance attributes for ROS2 action system
                self.success = result['success']
                self.return_info = result['return_info']
                return result

            else:
                result = {
                    'success': False,
                    'return_info': f"Unknown command: {cmd_name}. Available commands: tare, zero, read"
                }
                # Update instance attributes for ROS2 action system
                self.success = result['success']
                self.return_info = result['return_info']
                return result

        except json.JSONDecodeError as e:
            self.logger.error(f"JSON parsing failed: {e}")
            result = {
                'success': False,
                'return_info': f"JSON parsing failed: {str(e)}"
            }
            # Update instance attributes for ROS2 action system
            self.success = result['success']
            self.return_info = result['return_info']
            return result
        except Exception as e:
            self.logger.error(f"Command execution failed: {e}")
            result = {
                'success': False,
                'return_info': f"Command execution failed: {str(e)}"
            }
            # Update instance attributes for ROS2 action system
            self.success = result['success']
            self.return_info = result['return_info']
            return result

    def __del__(self):
        """Cleanup when object is destroyed"""
        self.disconnect()


if __name__ == "__main__":
    # Test the driver
    import argparse

    parser = argparse.ArgumentParser(description="Mettler Toledo XPR Balance Driver Test")
    parser.add_argument("--ip", default="192.168.1.10", help="Balance IP address")
    parser.add_argument("--port", type=int, default=81, help="Balance port")
    parser.add_argument("--password", default="123456", help="Balance password")
    parser.add_argument("action", choices=["tare", "zero", "read"],
                        nargs="?", default="read", help="Action to perform")
    parser.add_argument("--immediate", action="store_true", help="Use immediate mode")

    args = parser.parse_args()

    # Setup logging
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s: %(message)s")

    # Create driver instance
    balance = MettlerToledoXPR(ip=args.ip, port=args.port, password=args.password)

    try:
        if args.action == "tare":
            success = balance.tare(args.immediate)
            print(f"Tare {'successful' if success else 'failed'}")
        elif args.action == "zero":
            success = balance.zero(args.immediate)
            print(f"Zero {'successful' if success else 'failed'}")
        else:  # read
            # Perform tare first, then read weight
            if balance.tare(args.immediate):
                weight, unit = balance.get_weight_with_unit()
                print(f"Weight: {weight} {unit}")
            else:
                print("Tare operation failed, cannot read weight")

    finally:
        balance.disconnect()
