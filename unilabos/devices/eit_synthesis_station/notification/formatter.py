import datetime
from typing import Any, Dict, List, Tuple

# Notice API type 字段到中文名称的映射
_NOTICE_TYPE_NAME = {
    0: "通知",
    1: "故障",
    2: "告警",
}

# Notice API status 字段到中文名称的映射
_NOTICE_STATUS_NAME = {
    1: "异常",
    2: "修复中",
    3: "已恢复",
}


class NoticeFormatter:
    """
    功能:
        将 Notice API 返回的原始通知数据格式化为邮件内容 (主题 + HTML 正文).
    """

    @staticmethod
    def format_email(notices: List[Dict[str, Any]]) -> Tuple[str, str]:
        """
        功能:
            将通知列表格式化为邮件主题和 HTML 正文.
            正文包含一个详细信息表格, 列出每条通知的时间、类型、错误码、影响范围与状态.
        参数:
            notices: List[Dict], Notice API 返回的通知列表, 每项包含
                id, type, error_code, created_at, abnormal_impact_range, status, task_id, data.
        返回:
            Tuple[str, str], (邮件主题, HTML 正文).
        """
        count = len(notices)

        # 统计故障和告警数量, 用于主题
        fault_count = sum(1 for n in notices if n.get("type") == 1)
        alarm_count = sum(1 for n in notices if n.get("type") == 2)
        type_parts = []
        if fault_count > 0:
            type_parts.append(f"{fault_count} 条故障")
        if alarm_count > 0:
            type_parts.append(f"{alarm_count} 条告警")
        # 兜底: 如果都不是故障/告警
        if not type_parts:
            type_parts.append(f"{count} 条通知")

        subject = f"[EIT合成工站] 检测到 {'、'.join(type_parts)}"

        # 构建 HTML 表格
        rows_html = ""
        for notice in notices:
            time_str = NoticeFormatter._format_timestamp(notice.get("created_at"))
            type_name = _NOTICE_TYPE_NAME.get(notice.get("type"), str(notice.get("type", "")))
            error_code = str(notice.get("error_code", ""))
            impact_range = str(notice.get("abnormal_impact_range", ""))
            status_name = _NOTICE_STATUS_NAME.get(notice.get("status"), str(notice.get("status", "")))
            task_id = str(notice.get("task_id", ""))
            notice_id = str(notice.get("id", ""))

            rows_html += f"""
            <tr>
                <td style="padding: 8px; border: 1px solid #ddd; text-align: center;">{notice_id}</td>
                <td style="padding: 8px; border: 1px solid #ddd; text-align: center;">{time_str}</td>
                <td style="padding: 8px; border: 1px solid #ddd; text-align: center;">
                    <span style="color: {NoticeFormatter._type_color(notice.get('type'))}; font-weight: bold;">
                        {type_name}
                    </span>
                </td>
                <td style="padding: 8px; border: 1px solid #ddd; text-align: center;">{error_code}</td>
                <td style="padding: 8px; border: 1px solid #ddd;">{impact_range}</td>
                <td style="padding: 8px; border: 1px solid #ddd; text-align: center;">{status_name}</td>
                <td style="padding: 8px; border: 1px solid #ddd; text-align: center;">{task_id}</td>
            </tr>"""

        html_body = f"""
        <html>
        <body style="font-family: 'Microsoft YaHei', Arial, sans-serif; color: #333;">
            <h2 style="color: #d32f2f;">{subject}</h2>
            <p>以下是最新检测到的异常通知详情:</p>
            <table style="border-collapse: collapse; width: 100%; margin: 16px 0;">
                <thead>
                    <tr style="background-color: #f5f5f5;">
                        <th style="padding: 8px; border: 1px solid #ddd;">ID</th>
                        <th style="padding: 8px; border: 1px solid #ddd;">时间</th>
                        <th style="padding: 8px; border: 1px solid #ddd;">类型</th>
                        <th style="padding: 8px; border: 1px solid #ddd;">错误码</th>
                        <th style="padding: 8px; border: 1px solid #ddd;">影响范围</th>
                        <th style="padding: 8px; border: 1px solid #ddd;">状态</th>
                        <th style="padding: 8px; border: 1px solid #ddd;">任务ID</th>
                    </tr>
                </thead>
                <tbody>
                    {rows_html}
                </tbody>
            </table>
            <p style="color: #888; font-size: 12px;">
                此邮件由 EIT 合成工站异常通知系统自动发送, 请勿直接回复.
            </p>
        </body>
        </html>
        """

        return subject, html_body

    @staticmethod
    def _format_timestamp(ts: Any) -> str:
        """
        功能:
            将 Unix 时间戳转换为可读的日期时间字符串.
        参数:
            ts: Any, Unix 时间戳(秒), 可以是 int/float/str.
        返回:
            str, 格式为 "YYYY-MM-DD HH:MM:SS", 解析失败返回原始值.
        """
        if ts is None:
            return ""
        try:
            ts_float = float(ts)
            dt = datetime.datetime.fromtimestamp(ts_float)
            return dt.strftime("%Y-%m-%d %H:%M:%S")
        except (ValueError, TypeError, OSError):
            return str(ts)

    @staticmethod
    def _type_color(notice_type: Any) -> str:
        """
        功能:
            根据通知类型返回对应的 HTML 颜色值, 用于邮件中突出显示.
        参数:
            notice_type: int, 通知类型 (0=通知, 1=故障, 2=告警).
        返回:
            str, CSS 颜色值.
        """
        colors = {
            0: "#1976d2",   # 蓝色 - 通知
            1: "#d32f2f",   # 红色 - 故障
            2: "#f57c00",   # 橙色 - 告警
        }
        return colors.get(notice_type, "#333")
