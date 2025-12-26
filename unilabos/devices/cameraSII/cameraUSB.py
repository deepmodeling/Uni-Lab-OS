#!/usr/bin/env python3
import asyncio
import json
import subprocess
import sys
import threading
from typing import Optional, Dict, Any

import requests
import websockets


class CameraController:
    """
    Uni-Lab-OS 摄像头驱动（Linux USB 摄像头版，无 PTZ）

    - WebSocket 信令：signal_backend_url 连接到后端
      例如: wss://sciol.ac.cn/api/realtime/signal/host/<host_id>
    - 媒体服务器：RTMP 推流到 rtmp_url；WebRTC offer 转发到 SRS 的 webrtc_api
    - 视频源：本地 USB 摄像头（V4L2，默认 /dev/video0）
    """

    def __init__(
        self,
        host_id: str = "demo-host",
        signal_backend_url: str = "wss://sciol.ac.cn/api/realtime/signal/host",
        rtmp_url: str = "rtmp://srs.sciol.ac.cn:4499/live/camera-01",
        webrtc_api: str = "https://srs.sciol.ac.cn/rtc/v1/play/",
        webrtc_stream_url: str = "webrtc://srs.sciol.ac.cn:4500/live/camera-01",
        video_device: str = "/dev/video0",
        width: int = 1280,
        height: int = 720,
        fps: int = 30,
        video_bitrate: str = "1500k",
        audio_device: Optional[str] = None,  # 比如 "hw:1,0"，没有音频就保持 None
        audio_bitrate: str = "64k",
    ):
        self.host_id = host_id

        # 拼接最终 WebSocket URL：.../host/<host_id>
        signal_backend_url = signal_backend_url.rstrip("/")
        if not signal_backend_url.endswith("/host"):
            signal_backend_url = signal_backend_url + "/host"
        self.signal_backend_url = f"{signal_backend_url}/{host_id}"

        # 媒体服务器配置
        self.rtmp_url = rtmp_url
        self.webrtc_api = webrtc_api
        self.webrtc_stream_url = webrtc_stream_url

        # 本地采集配置
        self.video_device = video_device
        self.width = int(width)
        self.height = int(height)
        self.fps = int(fps)
        self.video_bitrate = video_bitrate
        self.audio_device = audio_device
        self.audio_bitrate = audio_bitrate

        # 运行时状态
        self._ws: Optional[object] = None
        self._ffmpeg_process: Optional[subprocess.Popen] = None
        self._running = False
        self._loop_task: Optional[asyncio.Future] = None

        # 事件循环 & 线程
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._loop_thread: Optional[threading.Thread] = None

        try:
            self.start()
        except Exception as e:
            print(f"[CameraController] __init__ auto start failed: {e}", file=sys.stderr)

    # ---------------------------------------------------------------------
    # 对外方法
    # ---------------------------------------------------------------------

    def start(self, config: Optional[Dict[str, Any]] = None):
        if self._running:
            return {"status": "already_running", "host_id": self.host_id}

        # 应用 config 覆盖（如果有）
        if config:
            cfg_host_id = config.get("host_id")
            if cfg_host_id:
                self.host_id = cfg_host_id

            signal_backend_url = config.get("signal_backend_url")
            if signal_backend_url:
                signal_backend_url = signal_backend_url.rstrip("/")
                if not signal_backend_url.endswith("/host"):
                    signal_backend_url = signal_backend_url + "/host"
                self.signal_backend_url = f"{signal_backend_url}/{self.host_id}"

            self.rtmp_url = config.get("rtmp_url", self.rtmp_url)
            self.webrtc_api = config.get("webrtc_api", self.webrtc_api)
            self.webrtc_stream_url = config.get("webrtc_stream_url", self.webrtc_stream_url)

            self.video_device = config.get("video_device", self.video_device)
            self.width = int(config.get("width", self.width))
            self.height = int(config.get("height", self.height))
            self.fps = int(config.get("fps", self.fps))
            self.video_bitrate = config.get("video_bitrate", self.video_bitrate)
            self.audio_device = config.get("audio_device", self.audio_device)
            self.audio_bitrate = config.get("audio_bitrate", self.audio_bitrate)

        self._running = True

        print("[CameraController] start(): starting FFmpeg streaming...", file=sys.stderr)
        self._start_ffmpeg()

        self._loop = asyncio.new_event_loop()

        def loop_runner(loop: asyncio.AbstractEventLoop):
            asyncio.set_event_loop(loop)
            try:
                loop.run_forever()
            except Exception as e:
                print(f"[CameraController] event loop error: {e}", file=sys.stderr)

        self._loop_thread = threading.Thread(target=loop_runner, args=(self._loop,), daemon=True)
        self._loop_thread.start()

        self._loop_task = asyncio.run_coroutine_threadsafe(self._run_main_loop(), self._loop)

        return {
            "status": "started",
            "host_id": self.host_id,
            "signal_backend_url": self.signal_backend_url,
            "rtmp_url": self.rtmp_url,
            "webrtc_api": self.webrtc_api,
            "webrtc_stream_url": self.webrtc_stream_url,
            "video_device": self.video_device,
            "width": self.width,
            "height": self.height,
            "fps": self.fps,
            "video_bitrate": self.video_bitrate,
            "audio_device": self.audio_device,
        }

    def stop(self) -> Dict[str, Any]:
        self._running = False

        # 先取消主任务（让 ws connect/sleep 尽快退出）
        if self._loop_task is not None and not self._loop_task.done():
            self._loop_task.cancel()

        # 停止推流
        self._stop_ffmpeg()

        # 关闭 WebSocket（在 loop 中执行）
        if self._ws and self._loop is not None:

            async def close_ws():
                try:
                    await self._ws.close()
                except Exception as e:
                    print(f"[CameraController] error closing WebSocket: {e}", file=sys.stderr)

            try:
                asyncio.run_coroutine_threadsafe(close_ws(), self._loop)
            except Exception:
                pass

        # 停止事件循环
        if self._loop is not None:
            try:
                self._loop.call_soon_threadsafe(self._loop.stop)
            except Exception as e:
                print(f"[CameraController] error stopping loop: {e}", file=sys.stderr)

        # 等待线程退出
        if self._loop_thread is not None:
            try:
                self._loop_thread.join(timeout=5)
            except Exception as e:
                print(f"[CameraController] error joining loop thread: {e}", file=sys.stderr)

        self._ws = None
        self._loop_task = None
        self._loop = None
        self._loop_thread = None

        return {"status": "stopped", "host_id": self.host_id}

    def get_status(self) -> Dict[str, Any]:
        ws_closed = None
        if self._ws is not None:
            ws_closed = getattr(self._ws, "closed", None)

        if ws_closed is None:
            websocket_connected = self._ws is not None
        else:
            websocket_connected = (self._ws is not None) and (not ws_closed)

        return {
            "host_id": self.host_id,
            "running": self._running,
            "websocket_connected": websocket_connected,
            "ffmpeg_running": bool(self._ffmpeg_process and self._ffmpeg_process.poll() is None),
            "signal_backend_url": self.signal_backend_url,
            "rtmp_url": self.rtmp_url,
            "video_device": self.video_device,
            "width": self.width,
            "height": self.height,
            "fps": self.fps,
            "video_bitrate": self.video_bitrate,
        }

    # ---------------------------------------------------------------------
    # WebSocket / 信令
    # ---------------------------------------------------------------------

    async def _run_main_loop(self):
        print("[CameraController] main loop started", file=sys.stderr)
        try:
            while self._running:
                try:
                    async with websockets.connect(self.signal_backend_url) as ws:
                        self._ws = ws
                        print(f"[CameraController] WebSocket connected: {self.signal_backend_url}", file=sys.stderr)
                        await self._recv_loop()
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    if self._running:
                        print(f"[CameraController] WebSocket connection error: {e}", file=sys.stderr)
                        await asyncio.sleep(3)
        except asyncio.CancelledError:
            pass
        finally:
            print("[CameraController] main loop exited", file=sys.stderr)

    async def _recv_loop(self):
        assert self._ws is not None
        ws = self._ws

        async for message in ws:
            try:
                data = json.loads(message)
            except json.JSONDecodeError:
                print(f"[CameraController] non-JSON message: {message}", file=sys.stderr)
                continue

            try:
                await self._handle_message(data)
            except Exception as e:
                print(f"[CameraController] error handling message {data}: {e}", file=sys.stderr)

    async def _handle_message(self, data: Dict[str, Any]):
        cmd = data.get("command")

        if cmd == "start_stream":
            self._start_ffmpeg()
            return

        if cmd == "stop_stream":
            self._stop_ffmpeg()
            return

        if data.get("type") == "offer":
            offer_sdp = data.get("sdp", "")
            camera_id = data.get("cameraId", "camera-01")

            answer_sdp = await self._handle_webrtc_offer(offer_sdp)

            if self._ws:
                answer_payload = {
                    "type": "answer",
                    "sdp": answer_sdp,
                    "cameraId": camera_id,
                    "hostId": self.host_id,
                }
                await self._ws.send(json.dumps(answer_payload))

    # ---------------------------------------------------------------------
    # FFmpeg 推流（V4L2 USB 摄像头）
    # ---------------------------------------------------------------------

    def _start_ffmpeg(self):
        if self._ffmpeg_process and self._ffmpeg_process.poll() is None:
            return

        # 兼容性优先：不强制输入像素格式；失败再通过外部调整 width/height/fps
        video_size = f"{self.width}x{self.height}"

        cmd = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "warning",

            # video input
            "-f", "v4l2",
            "-framerate", str(self.fps),
            "-video_size", video_size,
            "-i", self.video_device,
        ]

        # optional audio input
        if self.audio_device:
            cmd += [
                "-f", "alsa",
                "-i", self.audio_device,
                "-c:a", "aac",
                "-b:a", self.audio_bitrate,
                "-ar", "44100",
                "-ac", "1",
            ]
        else:
            cmd += ["-an"]

        # video encode + rtmp out
        cmd += [
            "-c:v", "libx264",
            "-preset", "ultrafast",
            "-tune", "zerolatency",
            "-profile:v", "baseline",
            "-pix_fmt", "yuv420p",
            "-b:v", self.video_bitrate,
            "-maxrate", self.video_bitrate,
            "-bufsize", "2M",
            "-g", str(max(self.fps, 10)),
            "-keyint_min", str(max(self.fps, 10)),
            "-sc_threshold", "0",
            "-x264-params", "bframes=0",

            "-f", "flv",
            self.rtmp_url,
        ]

        print(f"[CameraController] starting FFmpeg: {' '.join(cmd)}", file=sys.stderr)

        try:
            # 不再丢弃日志，至少能看到 ffmpeg 报错（调试很关键）
            self._ffmpeg_process = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=sys.stderr,
                shell=False,
            )
        except Exception as e:
            self._ffmpeg_process = None
            print(f"[CameraController] failed to start FFmpeg: {e}", file=sys.stderr)

    def _stop_ffmpeg(self):
        proc = self._ffmpeg_process
        if proc and proc.poll() is None:
            try:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
            except Exception as e:
                print(f"[CameraController] error stopping FFmpeg: {e}", file=sys.stderr)
        self._ffmpeg_process = None

    # ---------------------------------------------------------------------
    # WebRTC offer -> SRS
    # ---------------------------------------------------------------------

    async def _handle_webrtc_offer(self, offer_sdp: str) -> str:
        payload = {
            "api": self.webrtc_api,
            "streamurl": self.webrtc_stream_url,
            "sdp": offer_sdp,
        }
        headers = {"Content-Type": "application/json"}

        def _do_post():
            return requests.post(self.webrtc_api, json=payload, headers=headers, timeout=10)

        loop = asyncio.get_running_loop()
        resp = await loop.run_in_executor(None, _do_post)

        resp.raise_for_status()
        data = resp.json()
        answer_sdp = data.get("sdp", "")
        if not answer_sdp:
            raise RuntimeError(f"empty SDP from media server: {data}")
        return answer_sdp


if __name__ == "__main__":
    # 直接运行用于手动测试
    c = CameraController(
        host_id="demo-host",
        video_device="/dev/video0",
        width=1280,
        height=720,
        fps=30,
        video_bitrate="1500k",
        audio_device=None,
    )
    try:
        while True:
            asyncio.sleep(1)
    except KeyboardInterrupt:
        c.stop()