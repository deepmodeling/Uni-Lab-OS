"""
说明：
这里放一个“入口文件”，方便在 `unilabos/devices/liquid_handling` 目录下直接找到
`transfer_liquid` 的测试。

实际测试用例实现放在仓库标准测试目录：
`tests/devices/liquid_handling/test_transfer_liquid.py`
"""

# 让 pytest 能从这里发现同一套测试（避免复制两份测试代码）。
from tests.devices.liquid_handling.test_transfer_liquid import *  # noqa: F401,F403


