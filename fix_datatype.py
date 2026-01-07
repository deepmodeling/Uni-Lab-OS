#!/usr/bin/env python
# -*- coding: utf-8 -*-
import re

filepath = r'd:\UniLab\Uni-Lab-OS\unilabos\device_comms\modbus_plc\modbus.py'

with open(filepath, 'r', encoding='utf-8') as f:
    content = f.read()

# Replace the DataType placeholder with actual enum
find_pattern = r'# DataType will be accessed via client instance.*?DataType = None  # Placeholder.*?\n'
replacement = '''# Define DataType enum for pymodbus 2.5.3 compatibility
class DataType(Enum):
    INT16 = "int16"
    UINT16 = "uint16"
    INT32 = "int32"
    UINT32 = "uint32"
    INT64 = "int64"
    UINT64 = "uint64"
    FLOAT32 = "float32"
    FLOAT64 = "float64"
    STRING = "string"
    BOOL = "bool"

'''

new_content = re.sub(find_pattern, replacement, content, flags=re.DOTALL)

with open(filepath, 'w', encoding='utf-8') as f:
    f.write(new_content)

print('File updated successfully!')
