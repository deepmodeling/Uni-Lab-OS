#!/usr/bin/env python3
import asyncio
import json
import subprocess
import sys
import threading
from typing import Optional, Dict, Any
import logging

import requests
import websockets

logging.getLogger("zeep").setLevel(logging.WARNING)
logging.getLogger("zeep.xsd.schema").setLevel(logging.WARNING)
logging.getLogger("zeep.xsd.schema.schema").setLevel(logging.WARNING)
from onvif import ONVIFCamera  # 新增：ONVIF PTZ 控制


# ======================= 独立的 PTZController =======================
class PTZController:
    def __init__(self, host: str, port: int, user: str, password: str):
        """
        :param host: 摄像机 IP 或域名（和 RTSP 的一样即可）
        :param port: ONVIF 端口（多数为 80，看你的设备）
        :param user: 摄像机用户名
        :param password: 摄像机密码
        """
        self.host = host
        self.port = port
        self.user = user
        self.password = password

        self.cam: Optional[ONVIFCamera] = None
        self.media_service = None
        self.ptz_service = None
        self.profile = None

    def connect(self) -> bool:
        """
        建立 ONVIF 连接并初始化 PTZ 能力，失败返回 False（不抛异常）
        Note: 首先 pip install onvif-zeep
        """
        try:
            self.cam = ONVIFCamera(self.host, self.port, self.user, self.password)
            self.media_service = self.cam.create_media_service()
            self.ptz_service = self.cam.create_ptz_service()
            profiles = self.media_service.GetProfiles()
            if not profiles:
                print("[PTZ] No media profiles found on camera.", file=sys.stderr)
                return False
            self.profile = profiles[0]
            return True
        except Exception as e:
            print(f"[PTZ] Failed to init ONVIF PTZ: {e}", file=sys.stderr)
            return False

    def _continuous_move(self, pan: float, tilt: float, zoom: float, duration: float) -> bool:
        """
        连续移动一段时间（秒），之后自动停止。
        此函数为阻塞模式：只有在 Stop 调用结束后，才返回 True/False。
        """
        if not self.ptz_service or not self.profile:
            print("[PTZ] _continuous_move: ptz_service or profile not ready", file=sys.stderr)
            return False

        # 进入前先强行停一下，避免前一次残留动作
        self._force_stop()

        req = self.ptz_service.create_type("ContinuousMove")
        req.ProfileToken = self.profile.token

        req.Velocity = {
            "PanTilt": {"x": pan, "y": tilt},
            "Zoom": {"x": zoom},
        }

        try:
            print(f"[PTZ] ContinuousMove start: pan={pan}, tilt={tilt}, zoom={zoom}, duration={duration}", file=sys.stderr)
            self.ptz_service.ContinuousMove(req)
        except Exception as e:
            print(f"[PTZ] ContinuousMove failed: {e}", file=sys.stderr)
            return False

        # 阻塞等待：这里决定“运动时间”
        import time
        wait_seconds = max(2 * duration, 0.0)
        time.sleep(wait_seconds)

        # 运动完成后强制停止
        return self._force_stop()

    def stop(self) -> bool:
        """
        阻塞调用 Stop（带重试），成功 True，失败 False。
        """
        return self._force_stop()

    # ------- 对外动作接口（给 CameraController 调用） -------
    # 所有接口都为“阻塞模式”：只有在运动 + Stop 完成后才返回 True/False

    def move_up(self, speed: float = 0.5, duration: float = 1.0) -> bool:
        print(f"[PTZ] move_up called, speed={speed}, duration={duration}", file=sys.stderr)
        return self._continuous_move(pan=0.0, tilt=+speed, zoom=0.0, duration=duration)

    def move_down(self, speed: float = 0.5, duration: float = 1.0) -> bool:
        print(f"[PTZ] move_down called, speed={speed}, duration={duration}", file=sys.stderr)
        return self._continuous_move(pan=0.0, tilt=-speed, zoom=0.0, duration=duration)

    def move_left(self, speed: float = 0.2, duration: float = 1.0) -> bool:
        print(f"[PTZ] move_left called, speed={speed}, duration={duration}", file=sys.stderr)
        return self._continuous_move(pan=-speed, tilt=0.0, zoom=0.0, duration=duration)

    def move_right(self, speed: float = 0.2, duration: float = 1.0) -> bool:
        print(f"[PTZ] move_right called, speed={speed}, duration={duration}", file=sys.stderr)
        return self._continuous_move(pan=+speed, tilt=0.0, zoom=0.0, duration=duration)

    # ------- 占位的变倍接口（当前设备不支持） -------
    def zoom_in(self, speed: float = 0.2, duration: float = 1.0) -> bool:
        """
        当前设备不支持变倍；保留方法只是避免上层调用时报错。
        """
        print("[PTZ] zoom_in is disabled for this device.", file=sys.stderr)
        return False

    def zoom_out(self, speed: float = 0.2, duration: float = 1.0) -> bool:
        """
        当前设备不支持变倍；保留方法只是避免上层调用时报错。
        """
        print("[PTZ] zoom_out is disabled for this device.", file=sys.stderr)
        return False

    def _force_stop(self, retries: int = 3, delay: float = 0.1) -> bool:
        """
        尝试多次调用 Stop，作为“强制停止”手段。
        :param retries: 重试次数
        :param delay: 每次重试间隔（秒）
        """
        if not self.ptz_service or not self.profile:
            print("[PTZ] _force_stop: ptz_service or profile not ready", file=sys.stderr)
            return False

        import time
        last_error = None
        for i in range(retries):
            try:
                print(f"[PTZ] _force_stop: calling Stop(), attempt={i+1}", file=sys.stderr)
                self.ptz_service.Stop({"ProfileToken": self.profile.token})
                print("[PTZ] _force_stop: Stop() returned OK", file=sys.stderr)
                return True
            except Exception as e:
                last_error = e
                print(f"[PTZ] _force_stop: Stop() failed at attempt {i+1}: {e}", file=sys.stderr)
                time.sleep(delay)

        print(f"[PTZ] _force_stop: all {retries} attempts failed, last error: {last_error}", file=sys.stderr)
        return False

# ======================= CameraController（加入 PTZ） =======================

class CameraController:
    """
    Uni-Lab-OS 摄像头驱动（driver 形式）
    启动 Uni-Lab-OS 后，立即开始推流

    - WebSocket 信令：通过 signal_backend_url 连接到后端
      例如: wss://sciol.ac.cn/api/realtime/signal/host/<host_id>
    - 媒体服务器：通过 rtmp_url / webrtc_api / webrtc_stream_url
      当前配置为 SRS，与独立 HostSimulator 独立运行脚本保持一致。
    """

    def __init__(
        self,
        host_id: str = "demo-host",

        # （1）信令后端（WebSocket）
        signal_backend_url: str = "wss://sciol.ac.cn/api/realtime/signal/host",

        # （2）媒体后端（RTMP + WebRTC API）
        rtmp_url: str = "rtmp://srs.sciol.ac.cn:4499/live/camera-01",
        webrtc_api: str = "https://srs.sciol.ac.cn/rtc/v1/play/",
        webrtc_stream_url: str = "webrtc://srs.sciol.ac.cn:4500/live/camera-01",
        camera_rtsp_url: str = "",

        # （3）PTZ 控制相关（ONVIF）
        ptz_host: str = "",           # 一般就是摄像头 IP，比如 "192.168.31.164"
        ptz_port: int = 80,           # ONVIF 端口，不一定是 80，按实际情况改
        ptz_user: str = "",           # admin
        ptz_password: str = "",       # admin123
    ):
        self.host_id = host_id
        self.camera_rtsp_url = camera_rtsp_url

        # 拼接最终的 WebSocket URL：.../host/<host_id>
        signal_backend_url = signal_backend_url.rstrip("/")
        if not signal_backend_url.endswith("/host"):
            signal_backend_url = signal_backend_url + "/host"
        self.signal_backend_url = f"{signal_backend_url}/{host_id}"

        # 媒体服务器配置
        self.rtmp_url = rtmp_url
        self.webrtc_api = webrtc_api
        self.webrtc_stream_url = webrtc_stream_url

        # PTZ 控制
        self.ptz_host = ptz_host
        self.ptz_port = ptz_port
        self.ptz_user = ptz_user
        self.ptz_password = ptz_password
        self._ptz: Optional[PTZController] = None
        self._init_ptz_if_possible()

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

    # ------------------------ PTZ 初始化 ------------------------

    # ------------------------ PTZ 公开动作方法（一个动作一个函数） ------------------------

    def ptz_move_up(self, speed: float = 0.5, duration: float = 1.0) -> bool:
        print(f"[CameraController] ptz_move_up called, speed={speed}, duration={duration}")
        return self._ptz.move_up(speed=speed, duration=duration)

    def ptz_move_down(self, speed: float = 0.5, duration: float = 1.0) -> bool:
        print(f"[CameraController] ptz_move_down called, speed={speed}, duration={duration}")
        return self._ptz.move_down(speed=speed, duration=duration)

    def ptz_move_left(self, speed: float = 0.2, duration: float = 1.0) -> bool:
        print(f"[CameraController] ptz_move_left called, speed={speed}, duration={duration}")
        return self._ptz.move_left(speed=speed, duration=duration)

    def ptz_move_right(self, speed: float = 0.2, duration: float = 1.0) -> bool:
        print(f"[CameraController] ptz_move_right called, speed={speed}, duration={duration}")
        return self._ptz.move_right(speed=speed, duration=duration)

    def zoom_in(self, speed: float = 0.2, duration: float = 1.0) -> bool:
        """
        当前设备不支持变倍；保留方法只是避免上层调用时报错。
        """
        print("[PTZ] zoom_in is disabled for this device.", file=sys.stderr)
        return False

    def zoom_out(self, speed: float = 0.2, duration: float = 1.0) -> bool:
        """
        当前设备不支持变倍；保留方法只是避免上层调用时报错。
        """
        print("[PTZ] zoom_out is disabled for this device.", file=sys.stderr)
        return False

    def ptz_stop(self):
        if self._ptz is None:
            print("[CameraController] PTZ not initialized.", file=sys.stderr)
            return
        self._ptz.stop()

    def _init_ptz_if_possible(self):
        """
        根据 ptz_host / user / password 初始化 PTZ；
        如果配置信息不全则不启用 PTZ（静默）。
        """
        if not (self.ptz_host and self.ptz_user and self.ptz_password):
            return
        ctrl = PTZController(
            host=self.ptz_host,
            port=self.ptz_port,
            user=self.ptz_user,
            password=self.ptz_password,
        )
        if ctrl.connect():
            self._ptz = ctrl
        else:
            self._ptz = None

    # ---------------------------------------------------------------------
    # 对外暴露的方法：供 Uni-Lab-OS 调用
    # ---------------------------------------------------------------------

    def start(self, config: Optional[Dict[str, Any]] = None):
        """
        启动 Camera 连接 & 消息循环，并在启动时就开启 FFmpeg 推流，
        """

        if self._running:
            return {"status": "already_running", "host_id": self.host_id}

        # 应用 config 覆盖（如果有）
        if config:
            self.camera_rtsp_url = config.get("camera_rtsp_url", self.camera_rtsp_url)
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
            self.webrtc_stream_url = config.get(
                "webrtc_stream_url", self.webrtc_stream_url
            )

            # PTZ 相关配置也允许通过 config 注入
            self.ptz_host = config.get("ptz_host", self.ptz_host)
            self.ptz_port = int(config.get("ptz_port", self.ptz_port))
            self.ptz_user = config.get("ptz_user", self.ptz_user)
            self.ptz_password = config.get("ptz_password", self.ptz_password)
            self._init_ptz_if_possible()

        self._running = True

        # === start 时启动 FFmpeg 推流 ===
        self._start_ffmpeg()

        # 创建新的事件循环和线程（用于 WebSocket 信令）
        self._loop = asyncio.new_event_loop()

        def loop_runner(loop: asyncio.AbstractEventLoop):
            asyncio.set_event_loop(loop)
            try:
                loop.run_forever()
            except Exception as e:
                print(f"[CameraController] event loop error: {e}", file=sys.stderr)

        self._loop_thread = threading.Thread(
            target=loop_runner, args=(self._loop,), daemon=True
        )
        self._loop_thread.start()

        self._loop_task = asyncio.run_coroutine_threadsafe(
            self._run_main_loop(), self._loop
        )

        return {
            "status": "started",
            "host_id": self.host_id,
            "signal_backend_url": self.signal_backend_url,
            "rtmp_url": self.rtmp_url,
            "webrtc_api": self.webrtc_api,
            "webrtc_stream_url": self.webrtc_stream_url,
        }

    def stop(self) -> Dict[str, Any]:
        """
        停止推流 & 断开 WebSocket，并关闭事件循环线程。
        """
        self._running = False

        self._stop_ffmpeg()

        if self._ws and self._loop is not None:
            async def close_ws():
                try:
                    await self._ws.close()
                except Exception as e:
                    print(
                        f"[CameraController] error when closing WebSocket: {e}",
                        file=sys.stderr,
                    )

            asyncio.run_coroutine_threadsafe(close_ws(), self._loop)

        if self._loop_task is not None:
            if not self._loop_task.done():
                self._loop_task.cancel()
            try:
                self._loop_task.result()
            except asyncio.CancelledError:
                pass
            except Exception as e:
                print(
                    f"[CameraController] main loop task error in stop(): {e}",
                    file=sys.stderr,
                )
            finally:
                self._loop_task = None

        if self._loop is not None:
            try:
                self._loop.call_soon_threadsafe(self._loop.stop)
            except Exception as e:
                print(
                    f"[CameraController] error when stopping event loop: {e}",
                    file=sys.stderr,
                )

        if self._loop_thread is not None:
            try:
                self._loop_thread.join(timeout=5)
            except Exception as e:
                print(
                    f"[CameraController] error when joining loop thread: {e}",
                    file=sys.stderr,
                )
            finally:
                self._loop_thread = None

        self._ws = None
        self._loop = None

        return {"status": "stopped", "host_id": self.host_id}

    def get_status(self) -> Dict[str, Any]:
        """
        查询当前状态，方便在 Uni-Lab-OS 中做监控。
        """
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
            "ffmpeg_running": bool(
                self._ffmpeg_process and self._ffmpeg_process.poll() is None
            ),
            "signal_backend_url": self.signal_backend_url,
            "rtmp_url": self.rtmp_url,
        }

    # ---------------------------------------------------------------------
    # 内部实现逻辑：WebSocket 循环 / FFmpeg / WebRTC Offer 处理
    # ---------------------------------------------------------------------

    async def _run_main_loop(self):
        try:
            while self._running:
                try:
                    async with websockets.connect(self.signal_backend_url) as ws:
                        self._ws = ws
                        await self._recv_loop()
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    if self._running:
                        print(
                            f"[CameraController] WebSocket connection error: {e}",
                            file=sys.stderr,
                        )
                        await asyncio.sleep(3)
        except asyncio.CancelledError:
            pass

    async def _recv_loop(self):
        assert self._ws is not None
        ws = self._ws

        async for message in ws:
            try:
                data = json.loads(message)
            except json.JSONDecodeError:
                print(
                    f"[CameraController] received non-JSON message: {message}",
                    file=sys.stderr,
                )
                continue

            try:
                await self._handle_message(data)
            except Exception as e:
                print(
                    f"[CameraController] error while handling message {data}: {e}",
                    file=sys.stderr,
                )

    async def _handle_message(self, data: Dict[str, Any]):
        """
        处理来自信令后端的消息：
        - command: start_stream / stop_stream / ptz_xxx
        - type: offer (WebRTC)
        """
        cmd = data.get("command")

        # ---------- 推流控制 ----------
        if cmd == "start_stream":
            try:
                self._start_ffmpeg()
            except Exception as e:
                print(
                    f"[CameraController] error when starting FFmpeg on start_stream: {e}",
                    file=sys.stderr,
                )
            return

        if cmd == "stop_stream":
            try:
                self._stop_ffmpeg()
            except Exception as e:
                print(
                    f"[CameraController] error when stopping FFmpeg on stop_stream: {e}",
                    file=sys.stderr,
                )
            return

        # # ---------- PTZ 控制 ----------
        # # 例如信令可以发：
        # # {"command": "ptz_move", "direction": "down", "speed": 0.5, "duration": 0.5}
        # if cmd == "ptz_move":
        #     if self._ptz is None:
        #         # 没有初始化 PTZ，静默忽略或打印一条
        #         print("[CameraController] PTZ not initialized.", file=sys.stderr)
        #         return

        #     direction = data.get("direction", "")
        #     speed = float(data.get("speed", 0.5))
        #     duration = float(data.get("duration", 0.5))

        #     try:
        #         if direction == "up":
        #             self._ptz.move_up(speed=speed, duration=duration)
        #         elif direction == "down":
        #             self._ptz.move_down(speed=speed, duration=duration)
        #         elif direction == "left":
        #             self._ptz.move_left(speed=speed, duration=duration)
        #         elif direction == "right":
        #             self._ptz.move_right(speed=speed, duration=duration)
        #         elif direction == "zoom_in":
        #             self._ptz.zoom_in(speed=speed, duration=duration)
        #         elif direction == "zoom_out":
        #             self._ptz.zoom_out(speed=speed, duration=duration)
        #         elif direction == "stop":
        #             self._ptz.stop()
        #         else:
        #             # 未知方向，忽略
        #             pass
        #     except Exception as e:
        #         print(
        #             f"[CameraController] error when handling PTZ move: {e}",
        #             file=sys.stderr,
        #         )
        #     return

        # ---------- WebRTC Offer ----------
        if data.get("type") == "offer":
            offer_sdp = data.get("sdp", "")
            camera_id = data.get("cameraId", "camera-01")
            try:
                answer_sdp = await self._handle_webrtc_offer(offer_sdp)
            except Exception as e:
                print(
                    f"[CameraController] error when handling WebRTC offer: {e}",
                    file=sys.stderr,
                )
                return

            if self._ws:
                answer_payload = {
                    "type": "answer",
                    "sdp": answer_sdp,
                    "cameraId": camera_id,
                    "hostId": self.host_id,
                }
                try:
                    await self._ws.send(json.dumps(answer_payload))
                except Exception as e:
                    print(
                        f"[CameraController] error when sending WebRTC answer: {e}",
                        file=sys.stderr,
                    )

    # ------------------------ FFmpeg 相关 ------------------------

    def _start_ffmpeg(self):
        if self._ffmpeg_process and self._ffmpeg_process.poll() is None:
            return

        cmd = [
            "ffmpeg",
            "-rtsp_transport", "tcp",
            "-i", self.camera_rtsp_url,

            "-c:v", "libx264",
            "-preset", "ultrafast",
            "-tune", "zerolatency",
            "-profile:v", "baseline",
            "-b:v", "1M",
            "-maxrate", "1M",
            "-bufsize", "2M",
            "-g", "10",
            "-keyint_min", "10",
            "-sc_threshold", "0",
            "-pix_fmt", "yuv420p",
            "-x264-params", "bframes=0",

            "-c:a", "aac",
            "-ar", "44100",
            "-ac", "1",
            "-b:a", "64k",

            "-f", "flv",
            self.rtmp_url,
        ]

        logf = open("/tmp/ffmpeg_camera.log", "ab", buffering=0)
        self._ffmpeg_process = subprocess.Popen(
            cmd,
            stdout=logf,
            stderr=logf,
            shell=False,
        )

        try:
            self._ffmpeg_process = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.STDOUT,
                shell=False,
            )
        except Exception as e:
            print(f"[CameraController] failed to start FFmpeg: {e}", file=sys.stderr)
            self._ffmpeg_process = None
            raise

    def _stop_ffmpeg(self):
        proc = self._ffmpeg_process

        if proc and proc.poll() is None:
            try:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    try:
                        proc.kill()
                        try:
                            proc.wait(timeout=2)
                        except subprocess.TimeoutExpired:
                            print(
                                f"[CameraController] FFmpeg process did not exit even after kill (pid={proc.pid})",
                                file=sys.stderr,
                            )
                    except Exception as e:
                        print(
                            f"[CameraController] failed to kill FFmpeg process: {e}",
                            file=sys.stderr,
                        )
            except Exception as e:
                print(
                    f"[CameraController] error when stopping FFmpeg: {e}",
                    file=sys.stderr,
                )

        self._ffmpeg_process = None

    # ------------------------ WebRTC Offer 相关 ------------------------

    async def _handle_webrtc_offer(self, offer_sdp: str) -> str:
        payload = {
            "api": self.webrtc_api,
            "streamurl": self.webrtc_stream_url,
            "sdp": offer_sdp,
        }

        headers = {"Content-Type": "application/json"}

        def _do_request():
            return requests.post(
                self.webrtc_api,
                json=payload,
                headers=headers,
                timeout=10,
            )

        try:
            loop = asyncio.get_running_loop()
            resp = await loop.run_in_executor(None, _do_request)
        except Exception as e:
            print(
                f"[CameraController] failed to send offer to media server: {e}",
                file=sys.stderr,
            )
            raise

        try:
            resp.raise_for_status()
        except Exception as e:
            print(
                f"[CameraController] media server HTTP error: {e}, "
                f"status={resp.status_code}, body={resp.text[:200]}",
                file=sys.stderr,
            )
            raise

        try:
            data = resp.json()
        except Exception as e:
            print(
                f"[CameraController] failed to parse media server JSON: {e}, "
                f"raw={resp.text[:200]}",
                file=sys.stderr,
            )
            raise

        answer_sdp = data.get("sdp", "")
        if not answer_sdp:
            msg = f"empty SDP from media server: {data}"
            print(f"[CameraController] {msg}", file=sys.stderr)
            raise RuntimeError(msg)

        return answer_sdp