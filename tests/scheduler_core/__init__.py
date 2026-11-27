"""调度决策内核独立套件。

这个包标记不是可选的。缺少它时 pytest 会把本目录当作顶层目录导入 conftest，
被测仓库根目录不在 `sys.path` 上，`unilabos` 就会解析到 editable 安装指向的
另一个 worktree —— 套件表面全绿，实际验证的是别的分支代码。
"""
