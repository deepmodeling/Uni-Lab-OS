"""ROS 动作可达性检查容忍关闭中的客户端。"""

from unilabos.ros.nodes.presets.host_node import HostNode


def test_ready_probe_ignores_a_client_shutting_down():
    class Broken:
        def server_is_ready(self):
            raise RuntimeError("context already shut down")

    class Ready:
        def server_is_ready(self):
            return True

    assert HostNode._has_ready_action_client([Broken(), Ready()]) is True
