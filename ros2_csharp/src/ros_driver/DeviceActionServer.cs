using System;
using System.Threading;
using System.Threading.Tasks;
using ROS2;
using test_msgs.action;
using System.Collections.Generic;
using ilabos_msgs.action;
using RosDriver.Utils;

namespace ros_driver
{
    /// <summary>
    /// Generic action server that can be used with any ROS2 action type
    /// </summary>
    public class DeviceActionServer<TAction, TGoal, TResult, TFeedback> : IDisposable
            where TAction : IRosActionDefinition<TGoal, TResult, TFeedback>
            where TGoal : IRosMessage, new()
            where TResult : IRosMessage, new()
            where TFeedback : IRosMessage, new()
    {
        private readonly Node _node;
        private readonly string _actionName;
        private bool _disposed = false;
        private ActionValueMapping _actionValue = null;
        private readonly object _device = null;
        

        public DeviceActionServer(
            Node node, 
            object device,
            string actionName, 
            ActionValueMapping actionValue)
        {
            _node = node;
            _actionName = actionName;
            _actionValue = actionValue;
            _device = device;

            _node.CreateActionServer<TAction, TGoal, TResult, TFeedback>(
                _actionName, 
                HandleAccepted, 
                cancelCallback: HandleCancel);

            Console.WriteLine($"Created Action Server: {_actionName}");
        }
  
        
        private void HandleAccepted(ActionServerGoalHandle<TAction, TGoal, TResult, TFeedback> goalHandle)
        {
            // Don't block in the callback.
            // -> Don't wait for the returned Task.
            _ = DoWorkWithGoal(goalHandle);
        }

        private CancelResponse HandleCancel(ActionServerGoalHandle<TAction, TGoal, TResult, TFeedback> goalHandle)
        {
            return CancelResponse.Accept;
        }

        private async Task DoWorkWithGoal(ActionServerGoalHandle<TAction, TGoal, TResult, TFeedback> goalHandle)
        {

            Console.WriteLine("Executing goal...");
            // 检查 TAction 是否是 Fibonacci 类型
            if (typeof(TAction) == typeof(Fibonacci))
            {
                Console.WriteLine("确认 TAction 是 Fibonacci 类型");
                // 注意：这种转换是不安全的，因为泛型参数在编译时已确定
                // 这里仅作为示例，实际使用时需要谨慎
                var fibonacciGoalHandle = goalHandle as ActionServerGoalHandle<Fibonacci, Fibonacci_Goal, Fibonacci_Result, Fibonacci_Feedback>;
                if (fibonacciGoalHandle != null)
                {
                    Console.WriteLine("成功转换为 Fibonacci 类型的 GoalHandle");
                    var feedback = new Fibonacci_Feedback();

                    feedback.Sequence = new List<int> { 0, 1 };

                    for (int i = 1; i < fibonacciGoalHandle.Goal.Order; i++)
                    {
                        if (fibonacciGoalHandle.IsCanceling)
                        {
                            var cancelResult = new Fibonacci_Result();
                            cancelResult.Sequence = feedback.Sequence;

                            Console.WriteLine($"Canceled Result: {string.Join(", ", cancelResult.Sequence)}");
                            fibonacciGoalHandle.Canceled(cancelResult);
                            return;
                        }

                        feedback.Sequence.Add(feedback.Sequence[i] + feedback.Sequence[i - 1]);

                        Console.WriteLine($"Feedback: {string.Join(", ", feedback.Sequence)}");
                        fibonacciGoalHandle.PublishFeedback(feedback);

                        // NOTE: This causes the code to resume in an background worker Thread.
                        // Consider this when copying code from the example if additional synchronization is needed.
                        await Task.Delay(1000);
                    }

                    var result = new Fibonacci_Result();
                    result.Sequence = feedback.Sequence;

                    Console.WriteLine($"Result: {string.Join(", ", result.Sequence)}");
                    fibonacciGoalHandle.Succeed(result);
                }
            }
            else
            {
                Console.WriteLine($"TAction 不是 Fibonacci 类型，而是 {typeof(TAction).Name}");
                return;
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
                    // Release managed resources if any
                }
                
                _disposed = true;
            }
        }
    }
}
