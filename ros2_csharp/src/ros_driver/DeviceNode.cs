using System;
using System.Threading.Tasks;
using ROS2;
using test_msgs.action;
using System.Collections.Generic;
using RosDriver.Utils;
using System.Reflection;


namespace ros_driver
{
    class DeviceNode : IDisposable
    {
        private readonly Node _node;
        private readonly object _device;        
        private readonly string _name;
        private bool _disposed = false;

        private readonly Dictionary<string, object> _propertyPublishers = new Dictionary<string, object>();
        private readonly Dictionary<string, object> _actionServers = new Dictionary<string, object>();

        public DeviceNode(Node node, string name, DeviceInfo deviceInfo)
        {
            _name = name;
            _node = node;

            _device = new MockGripper();
            Type dType = _device.GetType();
            PropertyInfo[] properties = dType.GetProperties(
                BindingFlags.Public | BindingFlags.Instance);

            var statusTypes = deviceInfo.StatusTypes;
            var actionMappings = deviceInfo.ActionValueMappings;
            foreach (var prop in properties)
            {
                MethodInfo setMethod = prop.GetSetMethod(nonPublic: true);
                MethodInfo getMethod = prop.GetGetMethod(nonPublic: true);

                // 如果 setMethod 不是 null 打印方法名和返回值名
                if (setMethod != null)
                {
                    ParameterInfo[] parameters = setMethod.GetParameters();
                    Type parameterType = parameters[0].ParameterType;

                    // 如果返回值是字符串，则actionMappings 添加一个 ActionValueMapping 对象
                    if (setMethod.ReturnType == typeof(string))
                    {
                        var actionValueMapping = new ActionValueMapping
                        {
                            Type = "SendCmd",
                            Goal = new Dictionary<string, string> { { "command", parameterType.Name } },
                            Feedback = new Dictionary<string, string> { { "status", "status" } },
                            Result = new Dictionary<string, string>(),
                        };
                        actionMappings.Add(prop.Name, new ActionValueMapping());
                    }
                    Console.WriteLine($"Property: {prop.Name}, Setter: {setMethod.Name},Parameter Type: {parameterType.Name}, Return Type: {setMethod.ReturnType.Name}");
                }

                // 如果 getMethod 不是 null 打印方法名和返回值名
                if (getMethod != null)
                {
                    // 判断 prop.Name 不在 statusTypes 中，则添加到 statusTypes 中
                    if (!statusTypes.ContainsKey(prop.Name))
                    {
                        statusTypes.Add(prop.Name, getMethod.ReturnType.Name);
                    }
                    Console.WriteLine($"Property: {prop.Name}, Getter: {getMethod.Name}, Return Type: {getMethod.ReturnType.Name}");
                }
            }

            foreach (var kv in statusTypes)
            {
                Console.WriteLine($"create publisher:  {kv.Key}:");
                CreatePublisher(kv.Key, kv.Value);
            }
            
            foreach (var kv in actionMappings)
            {
                Console.WriteLine($"create action server:  {kv.Key}:");
                CreateActionServer(kv.Key, kv.Value);
            }
        }

        private void CreatePublisher(string key, string msgType)
        {
            Type rosMessageType = Converter2RosMessage.GetRosMessageType(msgType);
            
            // Use reflection to create the appropriate publisher type
            var publisherType = typeof(PropertyPublisher<>).MakeGenericType(rosMessageType);
            var publisher = Activator.CreateInstance(publisherType, _node, _device, key, 1.0, true);
            _propertyPublishers.Add(key, publisher);
        }

        private void CreateActionServer(string key, ActionValueMapping actionValue)
        {
            try
            {
                List<Type> actionTypes = Converter2RosMessage.GetActionMessageType(actionValue.Type);
                if (actionTypes.Count != 4)
                {
                    throw new ArgumentException($"Expected 4 action types, but got {actionTypes.Count} for action {actionValue.Type}");
                }
                
                // 提取类型：Action, Goal, Result, Feedback
                Type actionType = actionTypes[0];
                Type goalType = actionTypes[1];
                Type resultType = actionTypes[2];
                Type feedbackType = actionTypes[3];
                
                // 使用反射创建正确泛型类型的DeviceActionServer
                Type deviceActionServerType = typeof(DeviceActionServer<,,,>).MakeGenericType(
                    actionType, goalType, resultType, feedbackType);
                
                // 创建DeviceActionServer实例
                object actionServer = Activator.CreateInstance(
                    deviceActionServerType, 
                    _node,                // 节点
                    _device,              // 设备对象
                    key,                  // 动作名称
                    actionValue           // 动作值映射
                );
                
                // 将动作服务器存储在字典中
                _actionServers.Add(key, actionServer);
                
                Console.WriteLine($"成功创建了类型为 {actionValue.Type} 的动作服务器 {key}");
            }
            catch (Exception ex)
            {
                Console.WriteLine($"创建动作服务器 {key} 时出错: {ex.Message}");
                if (ex.InnerException != null)
                {
                    Console.WriteLine($"内部异常: {ex.InnerException.Message}");
                }
                Console.WriteLine($"这可能是由于消息定义中缺少或不正确的类型支持。");
            }
        }

        public void Dispose()
        {
            Dispose(true);
            GC.SuppressFinalize(this);
        }

        protected virtual void Dispose(bool disposing)
        {
            if (!_disposed)
            {
                if (disposing)
                {
                    // Node doesn't have a Dispose method, so don't try to call it
                    // _node.Dispose();
                    
                    // Handle other resources that need to be released
                    foreach (var publisher in _propertyPublishers.Values)
                    {
                        if (publisher is IDisposable disposable)
                            disposable.Dispose();
                    }
                    
                    foreach (var server in _actionServers.Values)
                    {
                        if (server is IDisposable disposable)
                            disposable.Dispose();
                    }
                }
                // Dispose unmanaged resources if any
                _disposed = true;
            }
        }
    }
}