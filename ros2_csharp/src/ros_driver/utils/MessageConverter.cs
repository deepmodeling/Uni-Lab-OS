using System;
using System.Collections.Generic;
using System.Globalization;
using System.Linq;
using ROS2;
using test_msgs.action;
using builtin_interfaces.msg;
using ilabos_msgs.action;

namespace RosDriver.Utils
{
    // 静态方法1 C# 根据数据std_msg.msg 类型把数据转 std_msg.msg 类型数据。

    public static class Converter2RosMessage
    {

        // 帮我写一个静态函数，接受一个 string 返回一个 std_msgs.msg 类型，例如接受 String 返回 std_msgs.msg.String 类型，而不是具体的实例
        public static Type GetRosMessageType(string typeName)
        {
            return typeName switch
            {
                "String" => typeof(std_msgs.msg.String),
                "Int32" => typeof(std_msgs.msg.Int32),
                "Float32" => typeof(std_msgs.msg.Float32),
                "Float64" => typeof(std_msgs.msg.Float64),
                "Double" => typeof(std_msgs.msg.Float64),
                "Bool" => typeof(std_msgs.msg.Bool),
                "Byte" => typeof(std_msgs.msg.Byte),
                "Float64MultiArray" => typeof(std_msgs.msg.Float64MultiArray),
                _ => throw new ArgumentException($"Unsupported type name: {typeName}", nameof(typeName))
            };
        }

        // 根据名字返回类型，例如Fibonacci 字符串就返回四个类型 Fibonacci, Fibonacci_Goal, Fibonacci_Result, Fibonacci_Feedback
        public static List<Type> GetActionMessageType(string typeName)
        {
            switch (typeName)
            {
                case "Fibonacci":
                    return new List<Type> { typeof(Fibonacci), typeof(Fibonacci_Goal), typeof(Fibonacci_Result), typeof(Fibonacci_Feedback) };
                case "SendCmd":
                    return new List<Type> { typeof(SendCmd), typeof(SendCmd_Goal), typeof(SendCmd_Result), typeof(SendCmd_Feedback) };
                case "Stir":
                    return new List<Type> { typeof(Stir), typeof(Stir_Goal), typeof(Stir_Result), typeof(Stir_Feedback) };
                case "HeatChill":
                    return new List<Type> { typeof(HeatChill), typeof(HeatChill_Goal), typeof(HeatChill_Result), typeof(HeatChill_Feedback) };
                default:
                    throw new ArgumentException($"Unsupported type name: {typeName}", nameof(typeName));
            }
        }


        // 新增方法: 使用 switch type 识别不同类型的数据并转换为对应的 std_msgs.msg 类型
        public static T ConvertToRosMessage<T>(object data) where T : IRosMessage, new()
        {
            T rosMessage = new T();
            
            switch (rosMessage)
            {
                case std_msgs.msg.String stringMsg when data is string strData:
                    stringMsg.Data = strData;
                    return rosMessage;
                    
                case std_msgs.msg.Int32 int32Msg when data is int intData:
                    int32Msg.Data = intData;
                    return rosMessage;
                    
                case std_msgs.msg.Float32 float32Msg when data is float floatData:
                    float32Msg.Data = floatData;
                    return rosMessage;
                    
                case std_msgs.msg.Float64 float64Msg when data is double doubleData:
                    float64Msg.Data = doubleData;
                    return rosMessage;
                    
                case std_msgs.msg.Bool boolMsg when data is bool boolData:
                    boolMsg.Data = boolData;
                    return rosMessage;
                    
                case std_msgs.msg.Byte byteMsg when data is byte byteData:
                    byteMsg.Data = byteData;
                    return rosMessage;
                    
                case std_msgs.msg.Char charMsg when data is char charData:
                    charMsg.Data = (byte)charData;
                    return rosMessage;
                    
                case std_msgs.msg.ByteMultiArray byteArrayMsg when data is byte[] byteArrayData:
                    byteArrayMsg.Data = byteArrayData.ToList();
                    byteArrayMsg.Layout = new std_msgs.msg.MultiArrayLayout();
                    return rosMessage;
                    
                case std_msgs.msg.Float64MultiArray float64ArrayMsg when data is double[] doubleArrayData:
                    float64ArrayMsg.Data = doubleArrayData.ToList();
                    float64ArrayMsg.Layout = new std_msgs.msg.MultiArrayLayout();
                    return rosMessage;
                    
                case std_msgs.msg.Float64MultiArray float64ArrayMsg when data is List<double> doubleListData:
                    float64ArrayMsg.Data = doubleListData;
                    float64ArrayMsg.Layout = new std_msgs.msg.MultiArrayLayout();
                    return rosMessage;
                    
                case builtin_interfaces.msg.Time timeMsg when data is DateTime dateTimeData:
                    timeMsg.Sec = (int)(dateTimeData.ToUniversalTime().Subtract(new DateTime(1970, 1, 1))).TotalSeconds;
                    timeMsg.Nanosec = (uint)(dateTimeData.ToUniversalTime().Subtract(new DateTime(1970, 1, 1))).Milliseconds * 1000000;
                    return rosMessage;
                    
                case builtin_interfaces.msg.Duration durationMsg when data is TimeSpan timeSpanData:
                    durationMsg.Sec = (int)timeSpanData.TotalSeconds;
                    durationMsg.Nanosec = (uint)(timeSpanData.Milliseconds * 1000000);
                    return rosMessage;
                    
                default:
                    throw new InvalidOperationException(
                        $"Cannot convert data of type '{data.GetType().Name}' to ROS message type '{typeof(T).Name}'");
            }
        }

        // 将ROS消息转换回C#数据类型的方法
        public static object ConvertFromRosMessage(IRosMessage rosMessage)
        {
            return rosMessage switch
            {
                std_msgs.msg.String strMsg => strMsg.Data,
                std_msgs.msg.Int32 int32Msg => int32Msg.Data,
                std_msgs.msg.Float32 float32Msg => float32Msg.Data,
                std_msgs.msg.Float64 float64Msg => float64Msg.Data,
                std_msgs.msg.Bool boolMsg => boolMsg.Data,
                std_msgs.msg.Byte byteMsg => byteMsg.Data,
                std_msgs.msg.Char charMsg => charMsg.Data,
                std_msgs.msg.ByteMultiArray byteArrayMsg => byteArrayMsg.Data,
                std_msgs.msg.Float64MultiArray float64ArrayMsg => float64ArrayMsg.Data,
                builtin_interfaces.msg.Time timeMsg => new DateTime(1970, 1, 1, 0, 0, 0, DateTimeKind.Utc)
                    .AddSeconds(timeMsg.Sec)
                    .AddMilliseconds(timeMsg.Nanosec / 1000000.0),
                builtin_interfaces.msg.Duration durationMsg => TimeSpan.FromSeconds(durationMsg.Sec)
                    .Add(TimeSpan.FromMilliseconds(durationMsg.Nanosec / 1000000.0)),
                // 可以根据需要添加更多类型的匹配
                _ => throw new ArgumentException($"Unsupported ROS message type: {rosMessage.GetType().Name}", nameof(rosMessage))
            };
        }

        // 泛型版本，当客户端代码知道期望的返回类型时使用
        public static T ConvertFromRosMessage<T>(IRosMessage rosMessage)
        {
            object result = ConvertFromRosMessage(rosMessage);
            if (result is T typedResult)
            {
                return typedResult;
            }
            throw new InvalidCastException($"Cannot convert from {rosMessage.GetType().Name} to {typeof(T).Name}");
        }
    } 
}