# 辅助函数：将UUID数组转换为字符串
def uuid_to_str(uuid_array) -> str:
    """将UUID字节数组转换为十六进制字符串"""
    return "".join(format(byte, "02x") for byte in uuid_array)