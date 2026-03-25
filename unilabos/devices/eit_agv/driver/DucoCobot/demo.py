from array import array
import sys
import time
import threading

sys.path.append('gen_py')
sys.path.append('lib')
from DucoCobot import DucoCobot
from thrift import Thrift
from thrift.transport import TSocket
from thrift.transport import TTransport
from thrift.protocol import TBinaryProtocol
from gen_py.robot.ttypes import StateRobot, StateProgram, OperationMode,TaskState,Op,MoveJogTaskParam,PointOp


ip='127.0.0.1'
stopheartthread = False


def hearthread_fun():
    duco_heartbeat = DucoCobot(ip, 7003)
    duco_heartbeat.open()
    while not stopheartthread:
        duco_heartbeat.rpc_heartbeat()
        time.sleep(1)
    duco_heartbeat.close()
    

def thread_fun():
    duco_cobot = DucoCobot(ip, 7003)
    # Connect!
    duco_cobot.open()
    while not stopheartthread:
        tcp_pose = []
        tcp_pose = duco_cobot.get_robot_state()
        print("state: ", tcp_pose)
        time.sleep(1)
    duco_cobot.close()

def main():
    thd_A = threading.Thread(target=thread_fun)
    thd_A.start()
    thd_B = threading.Thread(target=hearthread_fun)
    thd_B.start()
    
    duco_cobot = DucoCobot(ip,7003)
    op = Op()
    op.time_or_dist_1 = 0
    op.trig_io_1 = 1
    op.trig_value_1 = False
    op.trig_time_1 = 0.0
    op.trig_dist_1 = 0.0
    op.trig_event_1 = ''
    op.time_or_dist_2 = 0
    op.trig_io_2 = 1
    op.trig_value_2 = False
    op.trig_time_2 = 0.0
    op.trig_dist_2 = 0.0
    op.trig_event_2 = ''
    
    list1=Op(3,1,True,3000,0.0,"",3,2,True,3000,0.0,"")
    list2=Op(3,3,True, 3000, 10, "", 3, 4, True, 3000, 0.0, "")
    # Connect!
    rlt = duco_cobot.open()
    print("open:", rlt)
    rlt = duco_cobot.power_on(True)
    print("power_on:", rlt)
    rlt = duco_cobot.enable(True)
    print("enable:", rlt)
    # rlt = duco_cobot.set_tool_data("default",[0,0,0,0,0,0],[1,0,0,0],[0,0,0,0,0,0])
    # print("set tool:", rlt)
    rlt = duco_cobot.movej2([0,0,1.57,0,-1.57,0],1.0,1.0,0,True)
    print("movej2", rlt)
    point1 = PointOp([0.692000, 0.164000, 0.660000, -3.141592, 0.000000, -1.570796], list1)
    point2 = PointOp([0.692000, 0.164000, 0.529717, -3.141592, 0.000000, -1.570796], list2)
    # point3 = PointOp([0.692000, 0.164000, 0.380282, -3.141592, 0.000000, -1.570796], op)
    # point4 = PointOp([0.791035, 0.164000, 0.380282, -3.141592, 0.000000, -1.570796], op)
    # point5 = PointOp([0.866886, 0.164000, 0.380282, -3.141592, 0.000000, -1.570796], op)
    pointList = [point1, point2]
    op.time_or_dist_1=0
    op.time_or_dist_2=0
    # rlt = duco_cobot.tcp_move([0.35,0.09,0.2,1.7,0.45,-0.13],0.5,0.5,0,"default",True)
    # print("tcp move:",rlt)
    # rlt=duco_cobot.movej2([-1,-0.25,2,-3.4,1.57,2.24],1,2,0,True)
    # print("movej2",rlt)

    # print("spline")
    # rlt=duco_cobot.spline_op(pointList, 1, 2, "", "", True,op,0,False)
    # print("spline",rlt)
    # Pp = [[0.692000, 0.164000, 0.660000, -3.141592, 0.000000, -1.570796], [3, 1, True, 0, 0, "", 3, 1, False, 0, 0, ""]]

    rlt = duco_cobot.trackClearQueue()
    print("clear queue: ", rlt)
    rlt = duco_cobot.getQueueSize()
    print("queue size: ", rlt)
    rlt = duco_cobot.trackEnqueueOp(pointList,True)
    print("enqueue: ", rlt)
    rlt = duco_cobot.getQueueSize()
    print("queue size: ", rlt)
    rlt=duco_cobot.trackCartMotion(0.1, 0.2, True, "", "")
    print("cart",rlt)

    # # Connect!
    # rlt = duco_cobot.open()
    # print("open:", rlt)
    # rlt = duco_cobot.power_on(True)
    # print("power_on:", rlt)
    # rlt = duco_cobot.enable(True)
    # print("enable:", rlt)
    # rlt = duco_cobot.set_tool_data("default",[0,0,0,0,0,0],[1,0,0,0],[0,0,0,0,0,0])
    # print("set tool:", rlt)
    # rlt = duco_cobot.movej2([0,0,1.57,0,-1.57,0],1.0,1.0,0,True)
    # print("movej2", rlt)
    # pp1 = PointOp()
    # pp1.pos = [0.692000,0.164000,0.329717,-3.141592,0.000000,-1.570796]
    # pp1.op = op
    # pp_list = [pp1]
 
    # duco_cobot.spline_op(pp_list,1,1,"","",True,op,0,True)

    # input()
    stopheartthread = True
    time.sleep(1)
    rlt = duco_cobot.close()
    print("close:", rlt)

if __name__ == '__main__':
    try:
        main()
    except Thrift.TException as tx:
        print('%s' % tx.message)
