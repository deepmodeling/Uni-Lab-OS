# Windows 可编辑工作流源码合同

## 目标

Windows 上的 Uni-Lab Edge 必须能从显式授权的本地工作区完成以下闭环：

1. 按原始字节读取包含 CRLF 的 `package.yaml` 与工作流 Python 源码；
2. 发现清单声明的全部工作流；
3. 把原生 Windows 包根持久化到 `workflow_history.db`；
4. 重启后使用同一来源身份恢复工作流编辑状态。

## 两层独立兼容边界

### 原始文件读取

Windows CRT 的文本模式可能把 CRLF 转换为 LF，使读取长度与 `fstat().st_size`
不一致，并把稳定普通文件误报为 `unstable_regular_file`。所有工作流源码普通文件的
`os.open()` 入口必须显式加入 `O_BINARY`。目录描述符不需要该标志。

### 包根身份持久化

源码发现返回本机 `Path` 的字符串形态。Windows 上该值是原生驱动器路径或 UNC
路径，例如：

```text
C:\workspace\package
\\server\share\package
```

持久化校验同时接受规范 POSIX 绝对路径和规范 Windows 绝对路径，但仍然关闭式拒绝：

- 驱动器相对路径，例如 `C:workspace\package`；
- 无驱动器根路径、驱动器根和 UNC share 根；
- `..` 父级穿越；
- 正反斜线混用；
- Windows 设备命名空间；
- 控制字符、尾部分隔符等需要规范化改写的别名。

该校验只固定来源身份的词法形状，不授予文件系统权限。实际目录仍必须经过显式工作区
配置、普通目录/重解析点检查和提交前身份复核。

## 回归与验收

Windows CI 必须运行：

```text
tests/workflow/test_source_definition_bootstrap.py
tests/workflow/test_source_file_access_windows.py
tests/workflow/test_source_publication_windows.py
tests/workflow/test_source_workspace_security.py
```

真实工作区验收还应覆盖：发现数量与清单一致、空数据库首次安装、保存并应用草稿，以及
重启后来源身份和应用版本恢复。

前端若在拉取新增 workspace package 后报告 Rollup 无法解析 `@unilab/*`，先执行
`pnpm install --frozen-lockfile` 刷新 workspace 链接。不要把缺失包加入 Rollup
`external`，否则只会把依赖错误推迟到运行时。
