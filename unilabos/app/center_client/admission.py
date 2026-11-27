"""受理失败的安全公开原因：不把用户输入或远端正文复制进中心事件。"""
from uuid import UUID


def public_reason(code: str, message: str) -> str:
    """通用错误仅公开固定文案；库存来源只保留规范资源 UUID 与固定原因。"""
    from unilabos.workflow.service import WorkflowError
    if code == "canceled_before_creation":
        return "中心已在创建本地任务前取消调用"
    fallback = WorkflowError(code).message
    if code != "inventory_admission_failed":
        return fallback
    for prefix, suffix in [
        ("来源容器 ", " 没有试剂或当前内容物数量"),
        ("来源容器 ", " 存在多个数量型内容物"),
        ("数量需求引用的来源库位 ", " 当前为空"),
        ("数量需求引用的来源库位 ", " 不存在"),
    ]:
        if message.startswith(prefix) and message.endswith(suffix):
            raw = message[len(prefix):-len(suffix)]
            try:
                if str(UUID(raw)) == raw:
                    return prefix + raw + suffix
            except (ValueError, TypeError, AttributeError):
                pass
    return fallback
