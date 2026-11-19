"""跨层通用工具（不承载业务域逻辑；协议层 JSON 在 ``unilabos.protocol.utils``）。"""

from unilabos.utils.log import logger
from unilabos.utils.environment_check import check_environment, EnvironmentChecker

# 确保日志配置在导入utils包时自动应用
# 这样任何导入utils包或其子模块的代码都会自动配置好日志
__all__ = ["logger", "check_environment", "EnvironmentChecker"]
