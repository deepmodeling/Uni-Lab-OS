"""Registry 构建工具子包（不做 re-export，按模块深路径引用）。

- ``tools``：docstring 解析、类型注解 → JSON Schema、Slot/Handle 规范化、
  deep_diff 等纯工具函数；
- ``yaml_ref``：YAML 注册表的 ``$ref`` 展开；
- ``backend_metadata``：``supported_backends`` 能力元数据的规范化。

保持本 ``__init__`` 为空以避免 decorators ↔ tools 的循环 import：
``decorators`` 依赖 ``backend_metadata``，而 ``tools`` 又依赖 ``decorators``。
"""
