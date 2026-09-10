"""包分发（Package Distribution）CLI Adapter 使用的稳定错误边界。"""


class PackageCLIError(RuntimeError):
    """表示软件包子命令可安全呈现给用户的预期错误。"""


__all__ = ["PackageCLIError"]
