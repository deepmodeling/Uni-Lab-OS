"""工作流源码（Workflow Source）工作区的共享错误分类。"""


class SourceWorkspaceError(RuntimeError):
    """表示授权源码工作区无法被安全读取。"""

    def __init__(self, code: str):
        """保存稳定工作区错误码。

        参数：``code`` 表示无效包目录、声明文件或源码。返回：无；错误不携带
        文件内容或主机路径。
        """

        self.code = code
        super().__init__(code)


class SourceWorkspaceConflict(RuntimeError):
    """表示工作流源码 CAS 条件与当前物理文件不一致。"""


__all__ = ["SourceWorkspaceConflict", "SourceWorkspaceError"]
