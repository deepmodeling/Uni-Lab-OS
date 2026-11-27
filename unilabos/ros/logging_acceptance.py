"""在目标 ROS 环境验证原生 rcutils、/rosout 和日志容量；不连接实验设备。"""

from __future__ import annotations

import argparse
import ctypes
import ctypes.util
import json
import os
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

from unilabos.ros.logging import close_ros_logging, prepare_ros_logging


class _LogLocation(ctypes.Structure):
    _fields_ = [
        ("function_name", ctypes.c_char_p),
        ("file_name", ctypes.c_char_p),
        ("line_number", ctypes.c_size_t),
    ]


def _native_logger() -> ctypes.CDLL:
    """按目标 ROS 安装位置加载原生库，不解包或猜测 va_list 的 ABI。"""

    from ament_index_python.packages import get_package_prefix

    prefix = Path(get_package_prefix("rcutils"))
    candidates = [ctypes.util.find_library("rcutils")]
    candidates.extend(str(prefix / relative) for relative in (
        "lib/librcutils.so", "lib/librcutils.dylib", "bin/rcutils.dll",
    ))
    for candidate in candidates:
        if not candidate:
            continue
        try:
            library = ctypes.CDLL(candidate)
            library.rcutils_log.argtypes = [
                ctypes.POINTER(_LogLocation), ctypes.c_int, ctypes.c_char_p, ctypes.c_char_p,
            ]
            library.rcutils_log.restype = None
            return library
        except (OSError, AttributeError):
            continue
    raise RuntimeError("无法加载目标 ROS 的 rcutils 原生日志入口")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--working-dir", type=Path)
    parser.add_argument("--domain-id", type=int, default=213)
    parser.add_argument("--records", type=int, default=1000)
    options = parser.parse_args()
    if options.records < 400:
        parser.error("--records 至少为 400，确保覆盖多次轮转")
    if options.working_dir is not None:
        options.working_dir.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix="unilab-ros-log-acceptance-", dir=options.working_dir))
    # 使用隔离目录和小阈值加速验收，生产默认值仍由 LogPolicy 控制。
    config = SimpleNamespace(
        log_level="INFO", working_dir=str(root), log_max_bytes=32768, log_backup_count=2,
    )
    os.environ.pop("UNILABOS_PROCESS_OUTPUT_CAPTURED", None)
    os.environ["ROS_LOG_DIR"] = str(root / "native-spdlog-must-stay-empty")
    import rclpy
    from rcl_interfaces.msg import Log
    from rclpy.qos import QoSProfile

    ros_args = prepare_ros_logging(config=config)

    node = None
    received: list[str] = []
    native_marker = f"unilab-native-error-{os.getpid()}"
    info_marker = f"unilab-info-final-{os.getpid()}"
    try:
        rclpy.init(args=ros_args, domain_id=options.domain_id)
        node = rclpy.create_node(f"unilab_log_acceptance_{os.getpid()}")
        # Humble 未导出 rosout 预设；默认可靠、volatile 订阅可接收验收期间的新消息。
        node.create_subscription(Log, "/rosout", lambda msg: received.append(msg.msg), QoSProfile(depth=1000))
        library = _native_logger()
        location = _LogLocation(b"native_acceptance", __file__.encode(), 1)
        logger_name = node.get_logger().name.encode()
        for index in range(options.records):
            node.get_logger().info(f"容量验收 {index:05d} " + "x" * 180)
            if index % 40 == 0:
                rclpy.spin_once(node, timeout_sec=0.001)
        deadline = time.monotonic() + 5.0
        next_emit = 0.0
        while time.monotonic() < deadline:
            if time.monotonic() >= next_emit:
                # 真正调用 C 可变参数日志入口，验证消息格式化、console 和 rosout 全链路。
                library.rcutils_log(
                    ctypes.byref(location), 40, logger_name, b"%s value=%d",
                    ctypes.c_char_p(native_marker.encode()), ctypes.c_int(73),
                )
                next_emit = time.monotonic() + 0.2
            rclpy.spin_once(node, timeout_sec=0.05)
            if any(native_marker in message for message in received):
                break
        node.get_logger().info(info_marker)
    finally:
        if node is not None:
            node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()
        close_ros_logging()
    files = [
        path for path in (root / "logs").glob("ros_console_*.log*")
        if path.name.endswith(".log") or path.suffix[1:].isdigit()
    ]
    content = b"".join(path.read_bytes() for path in files)
    native_files = list((root / "native-spdlog-must-stay-empty").rglob("*.log"))
    results = {
        "working_dir": str(root),
        "native_error_on_disk": f"{native_marker} value=73".encode() in content,
        "info_on_disk": info_marker.encode() in content,
        "native_error_on_rosout": any(native_marker in message for message in received),
        "size_limit_passed": bool(files) and all(path.stat().st_size <= 32768 for path in files),
        "rotation_passed": 1 < len(files) <= 3,
        "native_unbounded_files_absent": not native_files,
    }
    print(json.dumps(results, ensure_ascii=False, indent=2))
    return 0 if all(value for key, value in results.items() if key != "working_dir") else 1


if __name__ == "__main__":
    raise SystemExit(main())
