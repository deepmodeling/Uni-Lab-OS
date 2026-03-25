import logging
import smtplib
import ssl
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import List

from .notification_settings import NotificationSettings


class EmailChannel:
    """
    功能:
        邮件通知渠道, 基于 SMTP 协议发送 HTML 格式邮件.
        兼容 QQ邮箱(SSL 465)、163邮箱(SSL 465)、Gmail(SSL 465)、Outlook(STARTTLS 587).
    参数:
        settings: NotificationSettings, 包含 SMTP 连接与认证配置.
    """

    def __init__(self, settings: NotificationSettings):
        self._settings = settings
        self._logger = logging.getLogger(self.__class__.__name__)

    def is_available(self) -> bool:
        """
        功能:
            检查 SMTP 参数是否完整配置, 缺少关键字段则不可用.
        参数:
            无.
        返回:
            bool, True 表示可发送邮件.
        """
        s = self._settings
        if not s.smtp_host:
            return False
        if not s.smtp_user:
            return False
        if not s.smtp_password:
            return False
        if not s.email_from:
            return False
        if not s.get_recipients():
            return False
        return True

    def send(self, subject: str, html_body: str) -> bool:
        """
        功能:
            发送 HTML 格式邮件到所有配置的收件人.
            smtp_ssl=True 时使用 SMTP_SSL 直连 (QQ/163/Gmail 端口465);
            smtp_ssl=False 时使用 SMTP + STARTTLS (Outlook 端口587).
        参数:
            subject: str, 邮件主题.
            html_body: str, HTML 格式的邮件正文.
        返回:
            bool, True 表示发送成功, False 表示发送失败(已记录日志).
        """
        if not self.is_available():
            self._logger.warning("邮件渠道未正确配置, 跳过发送")
            return False

        s = self._settings
        recipients = s.get_recipients()

        # 构建 MIME 邮件
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = s.email_from
        msg["To"] = ", ".join(recipients)
        msg.attach(MIMEText(html_body, "html", "utf-8"))

        try:
            if s.smtp_ssl:
                # SSL 直连 (QQ邮箱/163邮箱/Gmail, 端口465)
                context = ssl.create_default_context()
                with smtplib.SMTP_SSL(s.smtp_host, s.smtp_port, context=context, timeout=30) as server:
                    server.login(s.smtp_user, s.smtp_password)
                    server.sendmail(s.email_from, recipients, msg.as_string())
            else:
                # STARTTLS (Outlook, 端口587)
                with smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=30) as server:
                    server.ehlo()
                    context = ssl.create_default_context()
                    server.starttls(context=context)
                    server.ehlo()
                    server.login(s.smtp_user, s.smtp_password)
                    server.sendmail(s.email_from, recipients, msg.as_string())

            self._logger.info(
                "邮件发送成功, 收件人=%s, 主题=%s",
                recipients, subject,
            )
            return True

        except smtplib.SMTPAuthenticationError as e:
            self._logger.error(
                "SMTP 认证失败, 请检查用户名和密码/授权码, host=%s, user=%s, err=%s",
                s.smtp_host, s.smtp_user, e,
            )
            return False
        except smtplib.SMTPException as e:
            self._logger.error("SMTP 发送异常, err=%s", e)
            return False
        except OSError as e:
            # 网络连接错误
            self._logger.error(
                "邮件服务器连接失败, host=%s, port=%s, err=%s",
                s.smtp_host, s.smtp_port, e,
            )
            return False
