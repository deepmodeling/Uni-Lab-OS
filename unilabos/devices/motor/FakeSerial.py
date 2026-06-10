class FakeSerial:
    def __init__(self):
        self.data = b''

    def write(self, data):
        print("发送数据: ", end="")
        for i in data:
            print(f"{i:02x}", end=" ")
        print()  # 换行
        # 这里可模拟把假数据写到某个内部缓存
        # self.data = ...

    def setRTS(self, b):
        pass

    def read(self, n):
        # 这里可返回预设的响应，例如 b'\x01\x03\x02\x00\x19\x79\x8E'
        return b'\x01\x03\x02\x00\x19\x79\x8E'

    def close(self):
        pass
