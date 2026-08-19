"""微后端与上游调度 Backend 之间的 ``control.v1`` 控制协议。

这里不是设备通信 backend（设备通信只有 HostLink/ROS2），也不是可配置的传输
选择器。正常模式固定由 :mod:`.control` 发送 WS 轻通知，业务正文通过 HTTP 拉取；
旧完整载荷协议只存在于 :mod:`unilabos.legacy_support`。
"""
