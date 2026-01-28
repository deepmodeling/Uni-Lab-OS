# Liquid handling 集成测试

`test_transfer_liquid.py` 现在会调用 PRCXI 的 RViz 仿真 backend，运行前请确保：

1. 已安装包含 `pylabrobot`、`rclpy` 的运行环境；
2. 启动 ROS 依赖（`rviz` 可选，但是 `rviz_backend` 会创建 ROS 节点）；
3. 在 shell 中设置 `UNILAB_SIM_TEST=1`，否则 pytest 会自动跳过这些慢速用例：

```bash
export UNILAB_SIM_TEST=1
pytest tests/devices/liquid_handling/test_transfer_liquid.py -m slow
```

如果只需验证逻辑层（不依赖仿真），可以直接运行 `tests/devices/liquid_handling/unit_test.py`，该文件使用 Fake backend，适合作为 CI 的快速测试。***

