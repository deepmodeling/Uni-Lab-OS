import logging
import os
from dataclasses import dataclass, field
from typing import List


@dataclass
class NotificationSettings:
    """
    功能:
        异常通知子系统配置, 控制轮询行为、防刷策略及邮件 SMTP 参数.
        采用与 Settings 相同的 dataclass + from_env() 模式.
    参数:
        enabled: 总开关, 默认关闭.
        poll_interval_s: 轮询 Notice API 的间隔(秒).
        notice_types: 监控的通知类型列表, 1=FAULT, 2=ALARM.
        cooldown_s: 同一 notice id 的通知冷却时间(秒), 避免重复发送.
        max_notifications_per_hour: 每小时最大通知数, 超限后暂停发送.
        smtp_host: SMTP 服务器地址, 例如 "smtp.qq.com".
        smtp_port: SMTP 端口, SSL 通常为 465, STARTTLS 通常为 587.
        smtp_ssl: 是否使用 SSL 直连(True) 或 STARTTLS(False).
        smtp_user: SMTP 认证用户名.
        smtp_password: SMTP 认证密码/授权码 (QQ邮箱填授权码, 非QQ密码).
        email_from: 发件人邮箱地址.
        email_to: 收件人邮箱地址, 多个用逗号分隔.
    返回:
        NotificationSettings.
    """

    # --- 总开关与轮询 ---
    enabled: bool = True
    poll_interval_s: float = 10.0
    notice_types: List[int] = field(default_factory=lambda: [1, 2])

    # --- 防刷 ---
    cooldown_s: float = 300.0
    max_notifications_per_hour: int = 30

    # --- 邮件 (SMTP) ---
    smtp_host: str = "smtp.qq.com"
    smtp_port: int = 465
    smtp_ssl: bool = True
    smtp_user: str = "718118082@qq.com"
    smtp_password: str = "ppwcaplfqghfbcdh"
    email_from: str = "718118082@qq.com"
    email_to: str = "718118082@qq.com"

    @staticmethod
    def from_env() -> "NotificationSettings":
        """
        功能:
            从环境变量读取通知配置, 前缀为 SYN_STATION_NOTIFY_.
        参数:
            无.
        返回:
            NotificationSettings.
        环境变量:
            SYN_STATION_NOTIFY_ENABLED, SYN_STATION_NOTIFY_POLL_INTERVAL_S,
            SYN_STATION_NOTIFY_NOTICE_TYPES, SYN_STATION_NOTIFY_COOLDOWN_S,
            SYN_STATION_NOTIFY_MAX_NOTIFICATIONS_PER_HOUR,
            SYN_STATION_NOTIFY_SMTP_HOST, SYN_STATION_NOTIFY_SMTP_PORT,
            SYN_STATION_NOTIFY_SMTP_SSL, SYN_STATION_NOTIFY_SMTP_USER,
            SYN_STATION_NOTIFY_SMTP_PASSWORD, SYN_STATION_NOTIFY_EMAIL_FROM,
            SYN_STATION_NOTIFY_EMAIL_TO.
        """
        defaults = NotificationSettings()

        # --- 总开关与轮询 ---
        enabled_str = os.getenv("SYN_STATION_NOTIFY_ENABLED", str(defaults.enabled))
        enabled = enabled_str.strip().lower() in ("1", "true", "yes", "y", "on")

        poll_str = os.getenv("SYN_STATION_NOTIFY_POLL_INTERVAL_S", str(defaults.poll_interval_s))
        try:
            poll_interval_s = float(poll_str)
        except ValueError:
            poll_interval_s = defaults.poll_interval_s

        # notice_types: 逗号分隔的整数, 例如 "1,2"
        types_str = os.getenv("SYN_STATION_NOTIFY_NOTICE_TYPES", "1,2")
        notice_types = []
        for t in types_str.split(","):
            t = t.strip()
            if t.isdigit():
                notice_types.append(int(t))
        if not notice_types:
            notice_types = list(defaults.notice_types)

        # --- 防刷 ---
        cooldown_str = os.getenv("SYN_STATION_NOTIFY_COOLDOWN_S", str(defaults.cooldown_s))
        try:
            cooldown_s = float(cooldown_str)
        except ValueError:
            cooldown_s = defaults.cooldown_s

        max_per_hour_str = os.getenv(
            "SYN_STATION_NOTIFY_MAX_NOTIFICATIONS_PER_HOUR",
            str(defaults.max_notifications_per_hour),
        )
        try:
            max_notifications_per_hour = int(max_per_hour_str)
        except ValueError:
            max_notifications_per_hour = defaults.max_notifications_per_hour

        # --- 邮件 (SMTP) ---
        smtp_host = os.getenv("SYN_STATION_NOTIFY_SMTP_HOST", defaults.smtp_host)
        smtp_port_str = os.getenv("SYN_STATION_NOTIFY_SMTP_PORT", str(defaults.smtp_port))
        try:
            smtp_port = int(smtp_port_str)
        except ValueError:
            smtp_port = defaults.smtp_port

        smtp_ssl_str = os.getenv("SYN_STATION_NOTIFY_SMTP_SSL", str(defaults.smtp_ssl))
        smtp_ssl = smtp_ssl_str.strip().lower() in ("1", "true", "yes", "y", "on")

        smtp_user = os.getenv("SYN_STATION_NOTIFY_SMTP_USER", defaults.smtp_user)
        smtp_password = os.getenv("SYN_STATION_NOTIFY_SMTP_PASSWORD", defaults.smtp_password)
        email_from = os.getenv("SYN_STATION_NOTIFY_EMAIL_FROM", defaults.email_from)
        email_to = os.getenv("SYN_STATION_NOTIFY_EMAIL_TO", defaults.email_to)

        return NotificationSettings(
            enabled=enabled,
            poll_interval_s=poll_interval_s,
            notice_types=notice_types,
            cooldown_s=cooldown_s,
            max_notifications_per_hour=max_notifications_per_hour,
            smtp_host=smtp_host,
            smtp_port=smtp_port,
            smtp_ssl=smtp_ssl,
            smtp_user=smtp_user,
            smtp_password=smtp_password,
            email_from=email_from,
            email_to=email_to,
        )

    def get_recipients(self) -> List[str]:
        """
        功能:
            解析 email_to 字段, 返回收件人地址列表.
        参数:
            无.
        返回:
            List[str], 非空收件人邮箱列表.
        """
        if not self.email_to:
            return []
        return [addr.strip() for addr in self.email_to.split(",") if addr.strip()]
