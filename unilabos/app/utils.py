"""
UniLabOS 应用工具函数

提供清理、重启等工具函数
"""

import gc
import os
import threading
import time

from unilabos.utils.banner_print import print_status


def cleanup_for_restart() -> bool:
    """
    Clean up all resources for restart without exiting the process.

    This function prepares the system for re-initialization by:
    1. Stopping all communication clients
    2. Destroying ROS nodes
    3. Resetting singletons
    4. Waiting for threads to finish

    Returns:
        bool: True if cleanup was successful, False otherwise
    """
    print_status("[Restart] Starting cleanup for restart...", "info")

    # Step 1: Stop WebSocket communication client
    print_status("[Restart] Step 1: Stopping WebSocket client...", "info")
    try:
        from unilabos.app.communication import get_communication_client

        comm_client = get_communication_client()
        if comm_client is not None:
            comm_client.stop()
            print_status("[Restart] WebSocket client stopped", "info")
    except Exception as e:
        print_status(f"[Restart] Error stopping WebSocket: {e}", "warning")

    # Step 2: Get HostNode and cleanup ROS
    print_status("[Restart] Step 2: Cleaning up ROS nodes...", "info")
    try:
        from unilabos.ros.nodes.presets.host_node import HostNode
        import rclpy
        from rclpy.timer import Timer

        host_instance = HostNode.get_instance(timeout=5)
        if host_instance is not None:
            print_status(f"[Restart] Found HostNode: {host_instance.device_id}", "info")

            # Gracefully shutdown background threads
            print_status("[Restart] Shutting down background threads...", "info")
            HostNode.shutdown_background_threads(timeout=5.0)
            print_status("[Restart] Background threads shutdown complete", "info")

            # Stop discovery timer
            if hasattr(host_instance, "_discovery_timer") and isinstance(host_instance._discovery_timer, Timer):
                host_instance._discovery_timer.cancel()
                print_status("[Restart] Discovery timer cancelled", "info")

            # Destroy device nodes
            device_count = len(host_instance.devices_instances)
            print_status(f"[Restart] Destroying {device_count} device instances...", "info")
            for device_id, device_node in list(host_instance.devices_instances.items()):
                try:
                    if hasattr(device_node, "ros_node_instance") and device_node.ros_node_instance is not None:
                        device_node.ros_node_instance.destroy_node()
                        print_status(f"[Restart] Device {device_id} destroyed", "info")
                except Exception as e:
                    print_status(f"[Restart] Error destroying device {device_id}: {e}", "warning")

            # Clear devices instances
            host_instance.devices_instances.clear()
            host_instance.devices_names.clear()

            # Destroy host node
            try:
                host_instance.destroy_node()
                print_status("[Restart] HostNode destroyed", "info")
            except Exception as e:
                print_status(f"[Restart] Error destroying HostNode: {e}", "warning")

            # Reset HostNode state
            HostNode.reset_state()
            print_status("[Restart] HostNode state reset", "info")

        # Shutdown executor first (to stop executor.spin() gracefully)
        if hasattr(rclpy, "__executor") and rclpy.__executor is not None:
            try:
                rclpy.__executor.shutdown()
                rclpy.__executor = None  # Clear for restart
                print_status("[Restart] ROS executor shutdown complete", "info")
            except Exception as e:
                print_status(f"[Restart] Error shutting down executor: {e}", "warning")

        # Shutdown rclpy
        if rclpy.ok():
            rclpy.shutdown()
            print_status("[Restart] rclpy shutdown complete", "info")

    except ImportError as e:
        print_status(f"[Restart] ROS modules not available: {e}", "warning")
    except Exception as e:
        print_status(f"[Restart] Error in ROS cleanup: {e}", "warning")
        return False

    # Step 3: Reset communication client singleton
    print_status("[Restart] Step 3: Resetting singletons...", "info")
    try:
        from unilabos.app import communication

        if hasattr(communication, "_communication_client"):
            communication._communication_client = None
            print_status("[Restart] Communication client singleton reset", "info")
    except Exception as e:
        print_status(f"[Restart] Error resetting communication singleton: {e}", "warning")

    # Step 4: Wait for threads to finish
    print_status("[Restart] Step 4: Waiting for threads to finish...", "info")
    time.sleep(3)  # Give threads time to finish

    # Check remaining threads
    remaining_threads = []
    for t in threading.enumerate():
        if t.name != "MainThread" and t.is_alive():
            remaining_threads.append(t.name)

    if remaining_threads:
        print_status(
            f"[Restart] Warning: {len(remaining_threads)} threads still running: {remaining_threads}", "warning"
        )
    else:
        print_status("[Restart] All threads stopped", "info")

    # Step 5: Force garbage collection
    print_status("[Restart] Step 5: Running garbage collection...", "info")
    gc.collect()
    gc.collect()  # Run twice for weak references
    print_status("[Restart] Garbage collection complete", "info")

    print_status("[Restart] Cleanup complete. Ready for re-initialization.", "info")
    return True
