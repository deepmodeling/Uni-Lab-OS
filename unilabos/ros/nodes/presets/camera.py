import rclpy
from rclpy.node import Node
import cv2
from sensor_msgs.msg import Image
from cv_bridge import CvBridge
from unilabos.ros.nodes.base_device_node import BaseROS2DeviceNode, DeviceNodeResourceTracker

class VideoPublisher(BaseROS2DeviceNode):
    def __init__(self, device_id='video_publisher', device_uuid='', camera_index=0, period: float = 0.1, resource_tracker: DeviceNodeResourceTracker = None):
        # 初始化BaseROS2DeviceNode，使用自身作为driver_instance
        BaseROS2DeviceNode.__init__(
            self,
            driver_instance=self,
            device_id=device_id,
            device_uuid=device_uuid,
            status_types={},
            action_value_mappings={},
            hardware_interface="camera",
            print_publish=False,
            resource_tracker=resource_tracker,
        )
        # 创建一个发布者，发布到 /video 话题，消息类型为 sensor_msgs/Image，队列长度设为 10
        self.publisher_ = self.create_publisher(Image, f'/{device_id}/video', 10)
        # 初始化摄像头（默认设备索引为 0）
        self.cap = cv2.VideoCapture(camera_index)
        if not self.cap.isOpened():
            self.get_logger().error("无法打开摄像头")
        # 用于将 OpenCV 的图像转换为 ROS 图像消息
        self.bridge = CvBridge()
        # 设置定时器，10 Hz 发布一次
        timer_period = period  # 单位：秒
        self.timer = self.create_timer(timer_period, self.timer_callback)

    def timer_callback(self):
        ret, frame = self.cap.read()
        if not ret:
            self.get_logger().error("读取视频帧失败")
            return
        # 将 OpenCV 图像转换为 ROS Image 消息，注意图像编码需与摄像头数据匹配，这里使用 bgr8
        img_msg = self.bridge.cv2_to_imgmsg(frame, encoding="bgr8")
        self.publisher_.publish(img_msg)
        # self.get_logger().info("已发布视频帧")

    def destroy_node(self):
        # 释放摄像头资源
        if self.cap.isOpened():
            self.cap.release()
        super().destroy_node()

def main(args=None):
    rclpy.init(args=args)
    node = VideoPublisher()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.shutdown()

if __name__ == '__main__':
    main()
