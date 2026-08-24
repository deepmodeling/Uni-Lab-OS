try:
    import rtde_control
    import dashboard_client
    import rtde_receive
except ImportError as ex:
    print("Import Error, Please Install Packages in ur_arm_task.py First!", ex)
import time
import json
from unilabos.devices.agv.robotiq_gripper import RobotiqGripper

class UrArmTask():
    def __init__(self, host, retry=30):
        self.init_flag = False
        self.dash_c = None
        n = 0
        while self.dash_c is None:
            try:
                self.dash_c = dashboard_client.DashboardClient(host)
                if not self.dash_c.isConnected():
                    self.dash_c.connect()

                self.dash_c.loadURP('camera/250111_put_board.urp')
                self.arm_init()
                self.dash_c.running()
            except Exception as e:
                print(e)
                self.dash_c = None
            time.sleep(1)
            n += 1
            if n > retry:
                raise Exception('Can not connect to the robot dashboard server!')

        self.vel    = 0.1
        self.acc    = 0.1
        self.rtde_c = None
        self.rtde_r = None

        self.gripper = None
        self._pose = [0.0,0.0,0.0,0.0,0.0,0.0]
        self._gripper_pose = None
        self._status = 'IDLE'

        self._gripper_status    = 'AT_DEST'
        self.gripper_s_list     = ['MOVING','STOPPED_OUTER_OBJECT','STOPPED_INNER_OBJECT','AT_DEST']

        self.dash_c.loadURP('camera/250111_put_board.urp')

        self.arm_init()
        self.success = True
        self.init_flag = True

        
        n = 0
        while self.gripper is None:
            try:
                self.gripper = RobotiqGripper(host)
                self.gripper.activate()
                # self._gripper_status = self.gripper_s_list[self.gripper._get_var('OBJ')]
            except:
                self.gripper = None
                time.sleep(1)
                n += 1
                if n > retry:
                    raise Exception('Can not connect to the robot gripper server!')
                
        n = 0
        while self.rtde_r is None:
            try:
                self.rtde_r = rtde_receive.RTDEReceiveInterface(host)
                if not self.rtde_r.isConnected():
                    self.rtde_r.reconnect()
                self._pose = self.rtde_r.getActualTCPPose()
            except Exception as e:
                print(e)
                self.rtde_r = None
                time.sleep(1)
                n += 1
                if n > retry:
                    raise Exception('Can not connect to the arm info server!')
                
        self.dash_c.stop()

    def arm_init(self):
        self.dash_c.powerOn()
        self.dash_c.brakeRelease()
        self.dash_c.unlockProtectiveStop()
        running = self.dash_c.running()
        while running:
            running = self.dash_c.running()
            time.sleep(1)

    # def __del__(self):
    #     self.dash_c.disconnect()
    #     self.rtde_c.disconnect()
    #     self.rtde_r.disconnect()
    #     self.gripper.disconnect()

    def load_pose_file(self,file):
        self.pose_file = file
        self.reload_pose()

    def reload_pose(self):
        self.pose_data = json.load(open(self.pose_file))

    def load_pose_data(self,data):
        self.pose_data = json.loads(data)

    @property
    def arm_pose(self) -> list:
        try:
            if not self.rtde_r.isConnected():
                self.rtde_r.reconnect()
                print('_'*30,'Reconnect to the arm info server!')
            self._pose = self.rtde_r.getActualTCPPose()
            # print(self._pose)
        except Exception as e:
            self._pose = self._pose
            print('-'*20,'zhixing_arm\n',e)
        return self._pose
    
    @property
    def gripper_pose(self) -> float:
        if self.init_flag:
            try:
                self._gripper_status = self.gripper_s_list[self.gripper._get_var('OBJ')]
                self._gripper_pose = self.gripper.get_current_position()
            except Exception as e:
                self._gripper_status = self._gripper_status
                self._gripper_pose = self._gripper_pose
                print('-'*20,'zhixing_gripper\n',e)
            return self._gripper_pose
    
    @property
    def arm_status(self) -> str:
        return self._status

    @property
    def gripper_status(self) -> str:
        if self.init_flag:
            return self._gripper_status
    
    def move_pos_task(self,command):
        self.success = False
        task_name = json.loads(command)['task_name']

        self.dash_c.loadURP(task_name)
        self.dash_c.play()

        time.sleep(0.5)
        self._status = 'RUNNING'
        while self._status == 'RUNNING':
            running = self.dash_c.running()
            if not running:
                self._status = 'IDLE'
            time.sleep(1)

        self.success = True

    
if __name__ == "__main__":
    arm = UrArmTask("192.168.1.178")
    # arm.move_pos_task('t2_y4_transfer3.urp')
    # print(arm.arm_pose())
