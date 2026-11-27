"""把子进程原始输出交给独立接收器，保持工作区 Host 的进程收养语义。

业务进程仍由调用方直接启动；接收器只持有输出管道读端。调用方关闭自身写端
后即可退出，接收器在全部业务进程关闭写端后排空并退出。接收器内的有界队列
隔离磁盘故障，过载时丢弃日志并在磁盘恢复后报告。接收器被强制杀死时，业务
进程写入会收到 EPIPE/BrokenPipeError，调用方不能据此保证设备完全不受影响。
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import queue
import stat
import subprocess
import sys
import threading
import time
from collections.abc import Mapping
from dataclasses import asdict
from pathlib import Path
from typing import BinaryIO

from unilabos.utils.log_storage import LogPolicy, RotatingByteWriter, unique_log_path

_CHUNK_BYTES = 64 * 1024
_QUEUE_CHUNKS = 64
_START_TIMEOUT = 10.0
_DRAIN_TIMEOUT = 5.0
_RETRY_SECONDS = 1.0
_CAPTURE_MARKER = "UNILABOS_PROCESS_OUTPUT_CAPTURED"


def session_log_path(path: str | os.PathLike[str]) -> Path:
    """固定入口每轮生成独立会话名，避免重启和旧版写入者争用同一文件。"""

    path = Path(path)
    return unique_log_path(path.parent, prefix=f"{path.stem}-")


def _pipe_identity(fd: int) -> str | None:
    try:
        info = os.fstat(fd)
    except OSError:
        return None
    # Windows 匿名管道可能只提供 0:0，不能用它证明仍连接到原接收器。
    if not stat.S_ISFIFO(info.st_mode) or not info.st_ino:
        return None
    return f"{info.st_dev}:{info.st_ino}"


def is_process_output_captured() -> bool:
    """仅当 stderr 仍指向标记的受控管道时，复用上游接收器。"""

    marker = os.environ.get(_CAPTURE_MARKER)
    return bool(marker) and marker == _pipe_identity(2)


class ProcessOutput:
    """提供 Popen 可继承的 stdout；close 只释放调用方写端，不结束接收器。

    必须在业务 Popen 成功或失败后及时 close（推荐使用 with）。wait 用于已退出
    业务进程的日志排空；不要在仍运行的业务进程上无限等待。
    """

    def __init__(
        self,
        path: str | os.PathLike[str],
        policy: LogPolicy | None = None,
        *,
        cleanup_root: str | os.PathLike[str] | None = None,
        tee_stderr: int | BinaryIO | None = None,
    ) -> None:
        source = Path(path).absolute()
        self.path = source.parent.resolve() / source.name
        policy = policy or LogPolicy.from_env()
        command = [
            sys.executable,
            "-m",
            "unilabos.utils.process_output",
            "--path",
            str(self.path),
            "--policy",
            json.dumps(asdict(policy)),
        ]
        if cleanup_root is not None:
            command.extend(("--cleanup-root", str(Path(cleanup_root).resolve())))
        if tee_stderr is not None:
            command.append("--tee-stderr")
        environment = dict(os.environ)
        source_root = str(Path(__file__).resolve().parents[2])
        inherited = environment.get("PYTHONPATH")
        environment["PYTHONPATH"] = os.pathsep.join(
            value for value in (source_root, inherited) if value
        )
        self._process = subprocess.Popen(
            command,
            env=environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL if tee_stderr is None else tee_stderr,
            bufsize=0,
            start_new_session=os.name != "nt",
            creationflags=(subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0),
        )
        assert self._process.stdin is not None
        assert self._process.stdout is not None
        self.stream: BinaryIO = self._process.stdin
        self._pipe_marker = _pipe_identity(self.stream.fileno())
        self._closed = False
        ready = threading.Event()
        acknowledgement: list[bytes] = []

        def read_ready() -> None:
            try:
                acknowledgement.append(self._process.stdout.read(1))
            except OSError:
                pass
            finally:
                ready.set()

        reader = threading.Thread(target=read_ready, daemon=True)
        reader.start()
        if not ready.wait(_START_TIMEOUT) or acknowledgement != [b"1"]:
            self._process.kill()
            self.stream.close()
            self._process.wait(timeout=_START_TIMEOUT)
            reader.join(timeout=1.0)
            self._process.stdout.close()
            raise OSError(f"子进程日志接收器启动失败：{self.path}")
        self._process.stdout.close()

    @property
    def collector_pid(self) -> int:
        return self._process.pid

    def environment(self, environment: Mapping[str, str] | None = None) -> dict[str, str]:
        """给真实子进程标记受控 stderr，防止嵌套接收器重复落盘。"""

        result = dict(os.environ if environment is None else environment)
        if self._pipe_marker is not None:
            result[_CAPTURE_MARKER] = self._pipe_marker
        else:
            result.pop(_CAPTURE_MARKER, None)
        return result

    def __enter__(self) -> ProcessOutput:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.stream.close()
        # 只回收接收器的退出状态。Host 退出不会等待 daemon 线程，也不会关业务写端。
        threading.Thread(target=self._reap, daemon=True).start()

    def _reap(self) -> None:
        code = self._process.wait()
        if code:
            logging.getLogger(__name__).warning(
                "子进程日志接收器退出，部分日志可能未落盘：%s，退出码=%s", self.path, code
            )

    def wait(self, timeout: float = _DRAIN_TIMEOUT + 1.0) -> int:
        self.close()
        return self._process.wait(timeout=timeout)


class _LossCounter:
    def __init__(self) -> None:
        self._bytes = 0
        self._lock = threading.Lock()

    def add(self, size: int) -> None:
        with self._lock:
            self._bytes += size

    def value(self) -> int:
        with self._lock:
            return self._bytes

    def reported(self, size: int) -> None:
        with self._lock:
            self._bytes -= size


def _collect(
    stream: BinaryIO,
    path: Path,
    policy: LogPolicy,
    cleanup_root: Path | None = None,
    *,
    drain_timeout: float = _DRAIN_TIMEOUT,
    tee_stderr: bool = False,
    writer: RotatingByteWriter | None = None,
) -> int:
    """有界读取并异步落盘；返回非零表示退出时仍有未落盘日志。"""

    chunks: queue.Queue[bytes] = queue.Queue(maxsize=_QUEUE_CHUNKS)
    finished = threading.Event()
    losses = _LossCounter()
    writer_failed = threading.Event()
    # main 在 ready 前创建 writer 并取得占用锁；直接调用时也先明确验证目标。
    writer = writer or RotatingByteWriter(path, policy, cleanup_root=cleanup_root)

    def persist() -> None:
        retry_at = 0.0

        def report_losses() -> None:
            lost_bytes = losses.value()
            if lost_bytes:
                writer.write(
                    f"\n[日志接收器] 输出过载或磁盘故障，已丢弃 {lost_bytes} 字节\n".encode("utf-8")
                )
                losses.reported(lost_bytes)

        try:
            while not finished.is_set() or not chunks.empty():
                try:
                    chunk = chunks.get(timeout=0.05)
                except queue.Empty:
                    continue
                try:
                    if time.monotonic() < retry_at:
                        losses.add(len(chunk))
                        continue
                    report_losses()
                    writer.write(chunk)
                except Exception:
                    losses.add(len(chunk))
                    # 故障期间仍持有会话锁；不能让清理器误删仍在使用的日志。
                    retry_at = time.monotonic() + _RETRY_SECONDS
                finally:
                    chunks.task_done()
            report_losses()
        except BaseException:
            # 接收器读取线程仍继续排水，防止 writer 异常使设备写线程挂死。
            writer_failed.set()
        finally:
            try:
                writer.close()
            except Exception:
                writer_failed.set()

    worker = threading.Thread(target=persist, daemon=True)
    worker.start()
    read_chunk = getattr(stream, "read1", stream.read)
    next_loss_notice = 0.0

    def echo(chunk: bytes) -> None:
        if not tee_stderr:
            return
        try:
            view = memoryview(chunk)
            while view:
                written = os.write(2, view)
                if written <= 0:
                    break
                view = view[written:]
        except OSError:
            # 终端已关闭时仍排水和落盘，不能让 broken pipe 结束读取循环。
            pass

    try:
        while True:
            chunk = read_chunk(_CHUNK_BYTES)
            if not chunk:
                break
            # 保留原 stderr 的背压语义，磁盘队列丢弃不影响 console 回显。
            echo(chunk)
            if writer_failed.is_set():
                losses.add(len(chunk))
            else:
                try:
                    chunks.put_nowait(chunk)
                except queue.Full:
                    losses.add(len(chunk))
            if losses.value() and time.monotonic() >= next_loss_notice:
                echo(f"\n[日志接收器] 暂未落盘或已丢弃 {losses.value()} 字节\n".encode("utf-8"))
                next_loss_notice = time.monotonic() + 1.0
    finally:
        finished.set()
    worker.join(timeout=drain_timeout)
    incomplete = worker.is_alive() or losses.value() or writer_failed.is_set()
    if incomplete:
        echo("\n[日志接收器] 排空超时或磁盘故障，部分日志未落盘\n".encode("utf-8"))
    return 2 if incomplete else 0


def main() -> None:
    parser = argparse.ArgumentParser(description="独立的有界子进程日志接收器")
    parser.add_argument("--path", type=Path, required=True)
    parser.add_argument("--policy", required=True)
    parser.add_argument("--cleanup-root", type=Path)
    parser.add_argument("--tee-stderr", action="store_true")
    args = parser.parse_args()
    policy = LogPolicy(**json.loads(args.policy))
    writer = RotatingByteWriter(args.path, policy, cleanup_root=args.cleanup_root)
    sys.stdout.buffer.write(b"1")
    sys.stdout.buffer.flush()
    try:
        code = _collect(
            sys.stdin.buffer, args.path, policy, args.cleanup_root,
            tee_stderr=args.tee_stderr, writer=writer,
        )
    except BaseException:
        # 读取路径异常时立即关闭进程持有的读端，让写入方收到 EPIPE 而非永久阻塞。
        code = 2
    # 磁盘系统调用可能永不返回，不能在解释器清理时再等待 writer 的锁。
    os._exit(code)


if __name__ == "__main__":
    main()
