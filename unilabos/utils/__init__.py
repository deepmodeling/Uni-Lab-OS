from unilabos.utils.environment_check import EnvironmentChecker, check_environment
from unilabos.utils.log import logger

# 确保日志配置在导入utils包时自动应用
# 这样任何导入utils包或其子模块的代码都会自动配置好日志

# 导出logger和环境检查工具，使其可以直接导入
__all__ = ["logger", "check_environment", "EnvironmentChecker"]
