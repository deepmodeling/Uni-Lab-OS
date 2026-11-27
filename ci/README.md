# 本地校验

在已安装开发依赖的 Python 环境中，从仓库根目录执行静态检查和串行调度回归测试。

```bash
ruff check --target-version py311 --select E9,F821,E722,W605,I --ignore E501 unilabos tests ci scripts
python -m pytest -q tests/ci/test_public_snapshot.py tests/app/test_edge_scheduler_service.py tests/app/test_inventory_scheduler_link.py tests/workflow/test_suzhou_runtime_fixes.py
```

无 ROS 的完整 Python 测试范围见 `tests/ci_python_suite.txt`，需真实 ROS 或相应运行环境的用例见 `tests/ci_ros_required.txt`。
