import asyncio
from asyncio import events
import threading

import rclpy
from rclpy.impl.implementation_singleton import rclpy_implementation as _rclpy
from rclpy.executors import await_or_execute, Executor
from rclpy.action import ActionClient, ActionServer
from rclpy.action.server import ServerGoalHandle, GoalResponse, GoalInfo, GoalStatus
from std_msgs.msg import String
from action_tutorials_interfaces.action import Fibonacci


loop = None

def get_event_loop():
    global loop
    return loop


async def default_handle_accepted_callback_async(goal_handle):
    """Execute the goal."""
    await goal_handle.execute()


class ServerGoalHandleX(ServerGoalHandle):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    async def execute(self, execute_callback=None):
        # It's possible that there has been a request to cancel the goal prior to executing.
        # In this case we want to avoid the illegal state transition to EXECUTING
        # but still call the users execute callback to let them handle canceling the goal.
        if not self.is_cancel_requested:
            self._update_state(_rclpy.GoalEvent.EXECUTE)
        await self._action_server.notify_execute_async(self, execute_callback)


class ActionServerX(ActionServer):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.register_handle_accepted_callback(default_handle_accepted_callback_async)

    async def _execute_goal_request(self, request_header_and_message):
        request_header, goal_request = request_header_and_message
        goal_uuid = goal_request.goal_id
        goal_info = GoalInfo()
        goal_info.goal_id = goal_uuid

        self._node.get_logger().debug('New goal request with ID: {0}'.format(goal_uuid.uuid))

        # Check if goal ID is already being tracked by this action server
        with self._lock:
            goal_id_exists = self._handle.goal_exists(goal_info)

        accepted = False
        if not goal_id_exists:
            # Call user goal callback
            response = await await_or_execute(self._goal_callback, goal_request.goal)
            if not isinstance(response, GoalResponse):
                self._node.get_logger().warning(
                    'Goal request callback did not return a GoalResponse type. Rejecting goal.')
            else:
                accepted = GoalResponse.ACCEPT == response

        if accepted:
            # Stamp time of acceptance
            goal_info.stamp = self._node.get_clock().now().to_msg()

            # Create a goal handle
            try:
                with self._lock:
                    goal_handle = ServerGoalHandleX(self, goal_info, goal_request.goal)
            except RuntimeError as e:
                self._node.get_logger().error(
                    'Failed to accept new goal with ID {0}: {1}'.format(goal_uuid.uuid, e))
                accepted = False
            else:
                self._goal_handles[bytes(goal_uuid.uuid)] = goal_handle

        # Send response
        response_msg = self._action_type.Impl.SendGoalService.Response()
        response_msg.accepted = accepted
        response_msg.stamp = goal_info.stamp
        self._handle.send_goal_response(request_header, response_msg)

        if not accepted:
            self._node.get_logger().debug('New goal rejected: {0}'.format(goal_uuid.uuid))
            return

        self._node.get_logger().debug('New goal accepted: {0}'.format(goal_uuid.uuid))

        # Provide the user a reference to the goal handle
        # await await_or_execute(self._handle_accepted_callback, goal_handle)
        asyncio.create_task(self._handle_accepted_callback(goal_handle))

    async def notify_execute_async(self, goal_handle, execute_callback):
        # Use provided callback, defaulting to a previously registered callback
        if execute_callback is None:
            if self._execute_callback is None:
                return
            execute_callback = self._execute_callback

        # Schedule user callback for execution
        self._node.get_logger().info(f"{events.get_running_loop()}")
        asyncio.create_task(self._execute_goal(execute_callback, goal_handle))
        # loop = asyncio.new_event_loop()
        # asyncio.set_event_loop(loop)
        # task = loop.create_task(self._execute_goal(execute_callback, goal_handle))
        # await task


class ActionClientX(ActionClient):
    feedback_queue = asyncio.Queue()

    async def feedback_cb(self, msg):
        await self.feedback_queue.put(msg)

    async def send_goal_async(self, goal_msg):
        goal_future = super().send_goal_async(
            goal_msg,
            feedback_callback=self.feedback_cb
        )
        client_goal_handle = await asyncio.ensure_future(goal_future)
        if not client_goal_handle.accepted:
            raise Exception("Goal rejected.")
        result_future = client_goal_handle.get_result_async()
        while True:
            feedback_future = asyncio.ensure_future(self.feedback_queue.get())
            tasks = [result_future, feedback_future]
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            if result_future.done():
                result = result_future.result().result
                yield (None, result)
                break
            else:
                feedback = feedback_future.result().feedback
                yield (feedback, None)


async def main(node):
    print('Node started.')
    action_client = ActionClientX(node, Fibonacci, 'fibonacci')
    goal_msg = Fibonacci.Goal()
    goal_msg.order = 10
    async for (feedback, result) in action_client.send_goal_async(goal_msg):
        if feedback:
            print(f'Feedback: {feedback}')
        else:
            print(f'Result: {result}')
    print('Finished.')


async def ros_loop_node(node):
    while rclpy.ok():
        rclpy.spin_once(node, timeout_sec=0)
        await asyncio.sleep(1e-4)


async def ros_loop(executor: Executor):
    while rclpy.ok():
        executor.spin_once(timeout_sec=0)
        await asyncio.sleep(1e-4)


def run_event_loop():
    global loop
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_forever()
    

def run_event_loop_in_thread():
    thread = threading.Thread(target=run_event_loop, args=())
    thread.start()


if __name__ == "__main__":
    rclpy.init()
    node = rclpy.create_node('async_subscriber')
    future = asyncio.wait([ros_loop(node), main()])
    asyncio.get_event_loop().run_until_complete(future)