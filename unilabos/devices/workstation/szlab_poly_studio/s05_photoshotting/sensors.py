"""SZLab Poly Studio S05 拍照工位 OPC UA 变量。"""

from unilabos.devices.workstation.szlab_poly_studio.sensor import S05Sensors


S05_RESULT = "S05拍照结果"
S05_DONE = "S05加工完成"
S05_READY = "S05准备信号"
S05_MATERIAL_SENSOR = S05Sensors.MATERIAL

S05_PUBLIC_VARIABLES = [
    S05_READY,
    S05_DONE,
    S05_RESULT,
]

PHOTO_RESULT_LABELS = {
    1: "OK",
    2: "NG",
}
