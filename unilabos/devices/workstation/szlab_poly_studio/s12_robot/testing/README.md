# SZLab Robot Testing

STATION=S03 TASK=pick PRODUCT_TYPE=1 POSITION=1 CONFIRM=YES \
SKIP_ROBOT_PRECHECK_VARIABLES=Robot_Home \
SKIP_SENSOR_PRECHECK=1 \
unilabos/devices/workstation/szlab_poly_studio/s12_robot/robot_arm_step_test.sh

⬆️测试代码

我更新了上位机通讯和robot_only,主要是新增了S08倒料产品选择，需要给1和2作为250ml和500ml robot_only.xlsx 上位机通讯.csv
