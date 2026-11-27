"""目录式社区注册表测试使用的轻量驱动。"""


class MockBackend:
    def __init__(self, host: str, port: int):
        self.host = host
        self.port = port


class MockDeck:
    def __init__(self, name: str):
        self.name = name


class SharedDevice:
    def __init__(
        self,
        host: str,
        port: int,
        deck_name: str,
        channels: int,
        device_id: str = "",
        **_runtime_context,
    ):
        self.backend = MockBackend(host, port)
        self.deck = MockDeck(deck_name)
        self.name = device_id
        self.channels = channels
