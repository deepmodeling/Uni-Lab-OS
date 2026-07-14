# 确保所有算法在 import 时注册到 ALGORITHM_REGISTRY
import scheduler.core.step_algorithms as _step_algs  # noqa: F401
import scheduler.core.cp_solver as _cp  # noqa: F401
import scheduler.core.dp_solver as _dp  # noqa: F401
import scheduler.core.ga_solver as _ga  # noqa: F401
import scheduler.core.batch_factory as _bf  # noqa: F401

# 公共 API 导出
from scheduler.core.base import SchedulerBase
from scheduler.core.step_algorithms import (
    ALGORITHM_REGISTRY,
    get_algorithm,
    list_algorithms,
    register_algorithm,
)
from scheduler.core.batch_factory import make_batch_scheduler
from scheduler.core.cp_solver import CPSATScheduler
from scheduler.core.dp_solver import ExactDPScheduler
from scheduler.core.ga_solver import GeneticAlgorithmScheduler, decode_permutation
from scheduler.core.validators import validate_time_constraints

__all__ = [
    # 基类
    "SchedulerBase",
    # 算法注册
    "ALGORITHM_REGISTRY",
    "get_algorithm",
    "list_algorithms",
    "register_algorithm",
    # 工厂
    "make_batch_scheduler",
    # 求解器
    "CPSATScheduler",
    "ExactDPScheduler",
    "GeneticAlgorithmScheduler",
    "decode_permutation",
    # 验证器
    "validate_time_constraints",
]
