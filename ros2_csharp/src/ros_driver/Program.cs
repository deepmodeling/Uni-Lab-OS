using System;
using System.Collections.Generic;
using System.Linq;
using System.Text;
using ROS2;
using RosDriver.Utils;

namespace ros_driver
{
    class Program
    {
        private static void Main(string[] args)
        {
            RCLdotnet.Init();
            
            // Get device configuration from environment variables
            string configFilePath = Environment.GetEnvironmentVariable("CONFIG_FILE_PATH");
            if (string.IsNullOrEmpty(configFilePath))
            {
                Console.WriteLine("Please set the CONFIG_FILE_PATH environment variable.");
                return;
            }
            
            var deviceDict = YamlConfigParser.GetDeviceInfoDict(configFilePath);

            // 创建列表存储所有节点和设备对象
            List<ROS2.Node> nodes = new List<ROS2.Node>();
            List<DeviceNode> deviceNodes = new List<DeviceNode>();

            foreach (var device in deviceDict)
            {
                string deviceId = device.Key;
                DeviceInfo deviceInfo = device.Value;
                string nodeName = deviceId.Split('/').Last();
                string nodeNamespace = $"/devices/{deviceId}";
                
                Console.WriteLine($"Starting node '{nodeName}' with namespace '{nodeNamespace}'");
                Console.WriteLine($"Using config file: {configFilePath}");
                
                var node = RCLdotnet.CreateNode(nodeName, nodeNamespace);
                var deviceNode = new DeviceNode(node, deviceId, deviceInfo);
                
                // 添加到列表中而不是立即spin
                nodes.Add(node);
                deviceNodes.Add(deviceNode);
            }

            while (RCLdotnet.Ok())
            {
                foreach (var node in nodes)
                {
                    RCLdotnet.SpinOnce(node, 100); // 100ms超时
                }
            }

            // 清理资源
            foreach (var deviceNode in deviceNodes)
            {
                deviceNode.Dispose();
            }
            
            RCLdotnet.Shutdown();
        }
    }
}