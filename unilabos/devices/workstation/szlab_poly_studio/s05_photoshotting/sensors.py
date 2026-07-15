"""SZLab Poly Studio S05 拍照工位 OPC UA 变量。"""

S05_RESULT = "S05拍照结果"
S05_DONE = "S05加工完成"
S05_MATERIAL_SENSOR = "传感器状态_上位机[3].NO[0]"

S05_PUBLIC_VARIABLES = [
    S05_DONE,
    S05_RESULT,
]

PHOTO_RESULT_LABELS = {
    1: "OK",
    2: "NG",
}
