"""工站侧的 Center 客户端。

与中心的关系分三层，刻意分开：

- `config`：从环境与文件加载登记后保存的凭据；私钥由工站本地生成。
- `transport`：mTLS HTTP，证书和对应机器密钥共同认证，严格解析响应。
- `client`：会话、命令补拉/确认、领取和结果回传。
- `events`：完整事件信封、RFC 8785 摘要及逐字段精确确认。
- `runtime` / `durable`：默认关闭的轮询入口，以及持久 inbox/outbox 和恢复。

永久去重不放在这里，而是放在 `unilabos.workflow` 的存储层：那份记录必须与调用身份
在同一个事务里写入，放在 HTTP 客户端里迟早会与真实执行状态脱节。
"""

from unilabos.app.center_client.client import CenterClient, CenterClientError
from unilabos.app.center_client.config import CenterClientConfig, CenterConfigError

__all__ = [
    "CenterClient",
    "CenterClientConfig",
    "CenterClientError",
    "CenterConfigError",
]
