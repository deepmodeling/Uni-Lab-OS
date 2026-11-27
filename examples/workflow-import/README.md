# 工作流导入示例

- `import_demo_operation.py`：通过 Local 模式的 Python 静态编译器导入，声明为 `experiment_operation`。
- `import_demo_operation.json`：通过兼容 JSON 图接口导入，包含一个 `host_node/get_command` 示例节点。

在“实验操作”页面导入时，JSON 会按实验操作类型导入；在“工作流”页面导入时，JSON 会按普通工作流类型导入。Python 文件的类型以装饰器中的 `workflow_type` 为准。

Python 示例使用 `szlab_mixer_pump` 的 `run_solvent_addition` Action，因此需要先加载包含该设备模板和 Action 的 SZLab 环境。
