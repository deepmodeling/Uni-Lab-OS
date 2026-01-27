using System;
using System.Threading.Tasks;
using ROS2;
using test_msgs.action;
using System.Reflection;
using RosDriver.Utils;



namespace ros_driver
{
    /// <summary>
    /// A publisher that periodically publishes property values as ROS messages
    /// </summary>
    public class PropertyPublisher<T> where T : IRosMessage, new()
    {
        private readonly Node _node;
        private readonly string _name;
        private readonly bool _printPublish;
        private readonly object _device = null;
        
        private T _value;
        private Publisher<T> _publisher;
        private Timer _timer;
        private TimeSpan _timerPeriod;
        

        public PropertyPublisher(
            Node node,
            object device,
            string name,
            double initialPeriod = 1.0,
            bool printPublish = true)
        {
            _node = node;
            _device = device;
            _name = name;
            _timerPeriod = TimeSpan.FromSeconds(initialPeriod);
            _printPublish = printPublish;
            
            _publisher = node.CreatePublisher<T>(_name);
            _timer = node.CreateTimer(_timerPeriod, PublishProperty);

            Console.WriteLine($"创建发布者 {name}, {typeof(T)}");

            _value = new T();
            InitializeValue();
        }

        private void InitializeValue()
        {
            if (_value is std_msgs.msg.String strData)
            {
                strData.Data = "hello world";
            }

            if (_value is std_msgs.msg.Int32 intData)
            {
                intData.Data = 1;
            }

            if (_value is std_msgs.msg.Float64 floatData)
            {
                floatData.Data = 1.00f;
            }

            if (_value is std_msgs.msg.Float64MultiArray floatArrayData)
            {
                floatArrayData.Data = new System.Collections.Generic.List<double> { 1.0, 2.0, 3.0, 4.0 };
                floatArrayData.Layout = new std_msgs.msg.MultiArrayLayout();
            }
        }

        private bool HasMethod(object obj, string methodName)
        {
            if (obj == null)
                return false;
                
            Type type = obj.GetType();
            return type.GetMethod(methodName) != null;
        }

        // 检查对象是否有特定名称的属性
        private bool HasProperty(object obj, string propertyName)
        {
            if (obj == null)
                return false;
                
            Type type = obj.GetType();
            return type.GetProperty(propertyName) != null;
        }

        public object SafeGetPropertyValue(string propertyName)
        {
            if (!HasProperty(_device, propertyName))
            {
                return null; // 或抛出异常，或返回默认值
            }

            PropertyInfo propInfo = _device.GetType().GetProperty(propertyName);
            MethodInfo getMethod = propInfo.GetGetMethod(nonPublic: true);
            
            if (getMethod == null)
                return null; // 没有 getter 方法
            
            return getMethod.Invoke(_device, null);
        }

        private T getProperty()
        {

            var result = SafeGetPropertyValue(_name);
            if (result != null)
            {
                Console.WriteLine($"level: info, Property '{_name}' not found in device '{_device.GetType().Name}', result: {result}");
                return Converter2RosMessage.ConvertToRosMessage<T>(result);
            }

            return mockData();
        }

        private T mockData()
        {
            // Get the full name with namespace
            string typeName = typeof(T).FullName;
            
            if (typeName.EndsWith(".Float64"))
                return Converter2RosMessage.ConvertToRosMessage<T>(1.0);
            else if (typeName.EndsWith(".String"))
                return Converter2RosMessage.ConvertToRosMessage<T>("hello world");
            else if (typeName.EndsWith(".Int32"))
                return Converter2RosMessage.ConvertToRosMessage<T>(1);
            else if (typeName.EndsWith(".Float64MultiArray"))
                return Converter2RosMessage.ConvertToRosMessage<T>(new double[] { 1.0, 2.0, 3.0, 4.0 });
            else
                throw new NotSupportedException($"Type {typeName} is not supported.");
        }


        private async Task<T> getPropertyAsync()
        {
            await Task.Yield();
            var result = SafeGetPropertyValue(_name);
            if (result != null)
            {
                // Console.WriteLine($"level: info, Property '{_name}' found in device '{_device.GetType().Name}', result: {result}");
                return Converter2RosMessage.ConvertToRosMessage<T>(result);
            }
            return mockData();
        }
        
        /// <summary>
        /// Gets the property value synchronously
        /// </summary>
        /// <returns>The property value</returns>
        public T GetProperty()
        {

            // If async method is available, run it on a task and wait for result
            return Task.Run(async () => await getPropertyAsync()).Result;
            

            // // Otherwise call the synchronous method
            // return getProperty();
        }
        
        
        /// <summary>
        /// Publishes the property value
        /// </summary>
        private void PublishProperty(TimeSpan elapsed)
        {
            try
            {
                T value = GetProperty();
                if (_printPublish)
                {
                    //_node.Logger.Info($"Publishing {typeof(T).Name}: {value}");
                    Console.WriteLine($"level: info,prop name: {_name}, {typeof(T).Name}: {value} normal msg: {Converter2RosMessage.ConvertFromRosMessage(value)}");
                }
                
                if (value != null)
                {
                    // Console.WriteLine($"level: info, Publishing type: {typeof(T)}, value: {value.Data}");
                    _publisher.Publish(value);
                }
            }
            catch (Exception ex)
            {
                //_node.Logger.Error($"Error in PublishProperty: {ex.Message}\n{ex.StackTrace}");
                Console.WriteLine($"level: error, PublishProperty: {ex.Message}\n{ex.StackTrace}");
            }
        }






        
        /// <summary>
        /// Changes the frequency of publication
        /// </summary>
        /// <param name="period">New period in seconds</param>
        public void ChangeFrequency(double period)
        {
            _timerPeriod = TimeSpan.FromSeconds(period);
            // _node.Logger.Info($"Changing {_name} timer period to: {_timerPeriod.TotalSeconds} seconds");
            Console.WriteLine($"level: info, Changing {_name} timer period to: {_timerPeriod.TotalSeconds} seconds");

            
            // Reset timer
            _timer.Cancel();
            _timer = _node.CreateTimer(_timerPeriod, PublishProperty);
        }
    }
}
