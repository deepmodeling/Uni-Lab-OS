from __future__ import annotations

import argparse
import base64
import queue
import struct
import threading
import zlib
from dataclasses import dataclass, field
from typing import Any

from unilabos.sim.backends.isaac.joint_control import DEFAULT_MVPPKUSHENGKE_JOINTS, JointControlService, JointSpec
from unilabos.sim.backends.isaac.worker_http import ThreadingHTTPServer, make_handler


def _png_chunk(tag: bytes, payload: bytes) -> bytes:
    checksum = zlib.crc32(tag)
    checksum = zlib.crc32(payload, checksum) & 0xFFFFFFFF
    return struct.pack(">I", len(payload)) + tag + payload + struct.pack(">I", checksum)


def encode_png_rgb(image: Any) -> bytes:
    import numpy as np

    array = np.asarray(image)
    if array.ndim == 2:
        array = np.repeat(array[:, :, None], 3, axis=2)
    if array.ndim != 3 or array.shape[2] < 3:
        raise ValueError(f"RGB image must have shape HxWxC with C>=3, got {array.shape}")
    if array.dtype != np.uint8:
        if np.issubdtype(array.dtype, np.floating) and float(np.nanmax(array)) <= 1.0:
            array = array * 255.0
        array = np.clip(array, 0, 255).astype(np.uint8)
    channels = 4 if array.shape[2] >= 4 else 3
    color_type = 6 if channels == 4 else 2
    array = np.ascontiguousarray(array[:, :, :channels])
    height, width = int(array.shape[0]), int(array.shape[1])
    raw = b"".join(b"\x00" + array[row].tobytes() for row in range(height))
    header = struct.pack(">IIBBBBB", width, height, 8, color_type, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", header)
        + _png_chunk(b"IDAT", zlib.compress(raw))
        + _png_chunk(b"IEND", b"")
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Uni-Lab-OS Isaac physics worker")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8091)
    parser.add_argument("--scene", default=None)
    parser.add_argument("--robot-prim", default=None)
    parser.add_argument("--camera", default="/World/Camera")
    parser.add_argument("--headless", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--warmup-steps", type=int, default=2)
    parser.add_argument("--rpc-timeout-s", type=float, default=600.0)
    parser.add_argument("--joint-control-ui", action="store_true", default=False)
    return parser.parse_args(argv)


@dataclass
class _WorkerJob:
    op: str
    args: dict[str, Any]
    event: threading.Event = field(default_factory=threading.Event)
    result: Any = None
    error: BaseException | None = None


class IsaacWorkerState:
    def __init__(
        self,
        controller: Any,
        *,
        dispatch_on_main_thread: bool = False,
        rpc_timeout_s: float = 600.0,
    ):
        self.controller = controller
        self.rpc_timeout_s = float(rpc_timeout_s)
        self._jobs: queue.Queue[_WorkerJob] | None = queue.Queue() if dispatch_on_main_thread else None

    def health(self) -> dict[str, Any]:
        pending = self._jobs.qsize() if self._jobs is not None else 0
        return {"ok": True, "backend": "isaac", "controller": type(self.controller).__name__, "pending": pending}

    def dispatch(self, op: str, args: dict[str, Any]) -> Any:
        if self._jobs is None:
            return self._dispatch_direct(op, args)

        job = _WorkerJob(op=op, args=dict(args))
        self._jobs.put(job)
        if not job.event.wait(self.rpc_timeout_s):
            raise TimeoutError(f"Isaac worker op timed out waiting for main thread: {op}")
        if job.error is not None:
            raise job.error
        return job.result

    def process_next(self, timeout: float = 0.05) -> bool:
        if self._jobs is None:
            return False
        try:
            job = self._jobs.get(timeout=timeout)
        except queue.Empty:
            return False
        try:
            job.result = self._dispatch_direct(job.op, job.args)
        except BaseException as exc:
            job.error = exc
        finally:
            job.event.set()
            self._jobs.task_done()
        return True

    def _dispatch_direct(self, op: str, args: dict[str, Any]) -> Any:
        if op == "reset":
            return self.controller.reset()
        if op == "step":
            return self.controller.step(float(args.get("dt", 0.0)))
        if op == "load_scene":
            return self.controller.load_scene(str(args["scene_path"]))
        if op == "get_observation":
            return self.controller.get_observation(str(args["entity_id"]))
        if op == "set_command":
            return self.controller.set_command(str(args["entity_id"]), dict(args.get("command") or {}))
        if op == "attach_rigid_body":
            return self.controller.attach_rigid_body(
                str(args["name"]),
                str(args["asset_path"]),
                dict(args.get("pose") or {}),
            )
        if op == "get_joint_states":
            return self.controller.get_joint_states(str(args["body_id"]))
        if op == "list_joint_controls":
            return _to_rpc_result(self.controller.list_joint_controls())
        if op == "get_joint_control_state":
            return _to_rpc_result(self.controller.get_joint_control_state())
        if op == "plan_joint_targets":
            return _to_rpc_result(
                self.controller.plan_joint_targets(
                    dict(args.get("targets") or {}),
                    dict(args.get("options") or {}),
                )
            )
        if op == "check_joint_plan":
            return _to_rpc_result(self.controller.check_joint_plan(str(args["plan_id"])))
        if op == "execute_joint_plan":
            return _to_rpc_result(self.controller.execute_joint_plan(str(args["plan_id"])))
        if op == "stop_joint_motion":
            return _to_rpc_result(self.controller.stop_joint_motion())
        if op == "set_collision_check_enabled":
            return _to_rpc_result(self.controller.set_collision_check_enabled(bool(args.get("enabled"))))
        if op == "apply_stable_drive_settings":
            return _to_rpc_result(self.controller.apply_stable_drive_settings())
        if op == "apply_wrench":
            return self.controller.apply_wrench(str(args["body_id"]), dict(args.get("wrench") or {}))
        if op == "render":
            image = self.controller.render(str(args["camera"]), int(args["width"]), int(args["height"]))
            return {"encoding": "base64", "data": base64.b64encode(image).decode("ascii")}
        raise ValueError(f"Unsupported Isaac worker op: {op}")


def _to_rpc_result(value: Any) -> Any:
    if hasattr(value, "to_dict"):
        return value.to_dict()
    if isinstance(value, list):
        return [_to_rpc_result(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _to_rpc_result(item) for key, item in value.items()}
    return value


class IsaacController:
    def __init__(
        self,
        headless: bool,
        camera: str,
        robot_prim: str | None,
        warmup_steps: int = 2,
        joint_control_ui: bool = False,
    ):
        from isaacsim import SimulationApp

        self.app = SimulationApp({"headless": bool(headless)})
        self.camera = camera
        self.robot_prim = robot_prim
        self.joint_control_ui = bool(joint_control_ui)
        self.scene_path: str | None = None
        self.commands: dict[str, dict[str, Any]] = {}
        self.observations: dict[str, dict[str, Any]] = {}
        self.joint_states: dict[str, dict[str, float]] = {}
        self.rigid_bodies: dict[str, dict[str, Any]] = {}
        self.wrenches: list[tuple[str, dict[str, Any]]] = []
        self.render_fallback: str | None = None
        self.render_error: str | None = None
        self._stage = None
        self._last_dt = 0.0
        self._rgb_annotators: dict[tuple[str, int, int], Any] = {}
        self._joint_control_service: JointControlService | None = None
        self._joint_control_window: Any = None
        self._joint_control_models: dict[str, Any] = {}
        self._joint_control_status_model: Any = None
        self._collision_mode_model: Any = None
        self._init_ui_action_queue()
        for _ in range(max(0, int(warmup_steps))):
            self.app.update()

    def reset(self) -> None:
        self.commands.clear()
        self.observations.clear()
        self.joint_states.clear()
        self.wrenches.clear()
        self._rgb_annotators.clear()
        self.app.update()

    def step(self, dt: float) -> None:
        self._last_dt = float(dt)
        self.app.update()

    def load_scene(self, scene_path: str) -> None:
        import omni.usd

        self.scene_path = str(scene_path)
        self._rgb_annotators.clear()
        omni.usd.get_context().open_stage(self.scene_path)
        for _ in range(2):
            self.app.update()
        self._stage = omni.usd.get_context().get_stage()
        self._joint_control_service = None
        try:
            self.apply_stable_drive_settings()
        except Exception as exc:
            print(f"[isaac worker] stable drive auto-apply failed: {exc}", flush=True)
        if self.joint_control_ui:
            self._create_joint_control_ui()

    def get_observation(self, entity_id: str) -> dict[str, Any]:
        observation = dict(self.observations.get(entity_id, {}))
        observation.setdefault("entity_id", entity_id)
        observation.setdefault("scene_path", self.scene_path)
        observation.setdefault("source", "isaac_worker")
        observation.setdefault("last_dt", self._last_dt)
        if entity_id in self.commands:
            observation["last_command"] = dict(self.commands[entity_id])
        if entity_id in self.joint_states:
            observation["joint_states"] = dict(self.joint_states[entity_id])
            observation["joint_names"] = list(self.joint_states[entity_id].keys())
            observation["joint_positions"] = list(self.joint_states[entity_id].values())
        if entity_id in self.rigid_bodies:
            observation.update(self.rigid_bodies[entity_id])
        prim_pose = self._query_prim_pose(entity_id)
        if prim_pose is not None:
            observation["pose"] = prim_pose
        if self.render_fallback is not None:
            observation["render_fallback"] = self.render_fallback
        if self.render_error is not None:
            observation["render_error"] = self.render_error
        return observation

    def set_command(self, entity_id: str, command: dict[str, Any]) -> None:
        self.commands[entity_id] = dict(command)
        joints = command.get("joint_positions") or command.get("q")
        if isinstance(joints, list):
            self.joint_states[entity_id] = {f"joint_{index + 1}": float(value) for index, value in enumerate(joints)}
        self.app.update()

    def attach_rigid_body(self, name: str, asset_path: str, pose: dict[str, Any]) -> str:
        body_id = str(name)
        self.rigid_bodies[body_id] = {"name": body_id, "asset_path": str(asset_path), "pose": dict(pose)}
        return body_id

    def get_joint_states(self, body_id: str) -> dict[str, float]:
        return dict(self.joint_states.get(body_id, {}))

    def list_joint_controls(self) -> list[dict[str, Any]]:
        return [spec.to_dict() for spec in self._get_joint_control_service().list_joints()]

    def get_joint_control_state(self) -> dict[str, Any]:
        return self._get_joint_control_service().get_joint_control_state()

    def plan_joint_targets(self, targets: dict[str, float], options: dict[str, Any] | None = None):
        return self._get_joint_control_service().plan_joint_targets(targets, options)

    def check_joint_plan(self, plan_id: str):
        return self._get_joint_control_service().check_joint_plan(plan_id)

    def execute_joint_plan(self, plan_id: str):
        return self._get_joint_control_service().execute_joint_plan(plan_id)

    def stop_joint_motion(self):
        return self._get_joint_control_service().stop_joint_motion()

    def set_collision_check_enabled(self, enabled: bool):
        return self._get_joint_control_service().set_collision_check_enabled(bool(enabled))

    def apply_stable_drive_settings(self):
        return self._get_joint_control_service().apply_stable_drive_settings()

    def apply_wrench(self, body_id: str, wrench: dict[str, Any]) -> None:
        self.wrenches.append((str(body_id), dict(wrench)))

    def idle(self) -> None:
        self._process_ui_actions()
        self.app.update()

    def render(self, camera: str, width: int, height: int) -> bytes:
        image = self._render_with_isaac(camera, width, height)
        if image is not None:
            self.render_fallback = None
            self.render_error = None
            return image
        self.render_fallback = "minimal_png"
        meta = f"isaac-worker-render camera={camera} width={int(width)} height={int(height)} scene={self.scene_path}".encode()
        return b"\x89PNG\r\n\x1a\n" + meta

    def close(self) -> None:
        self.app.close()

    def _get_joint_control_service(self) -> JointControlService:
        if self._joint_control_service is None:
            self._joint_control_service = JointControlService(_IsaacJointControlAdapter(self))
        return self._joint_control_service

    def _collision_mode_text(self) -> str:
        state = self.get_joint_control_state()
        if bool(state.get("collision_check_enabled", True)):
            return "Collision: ON - Check required"
        return "Collision: OFF - Execute bypasses contact checks"

    def _create_joint_control_ui(self) -> None:
        try:
            import omni.ui as ui

            service = self._get_joint_control_service()
            self._joint_control_window = ui.Window("UniLab Joint Control", width=460, height=620)
            self._joint_control_models = {}
            self._joint_control_status_model = ui.SimpleStringModel("Ready")
            self._collision_mode_model = ui.SimpleStringModel(self._collision_mode_text())
            with self._joint_control_window.frame:
                with ui.VStack(spacing=6):
                    ui.Label("UniLab Joint Control", height=24)
                    ui.Label(f"Scene: {self.scene_path or 'not loaded'}", height=20)
                    ui.Label("", model=self._collision_mode_model, height=24)
                    ui.Label("", model=self._joint_control_status_model, height=20)
                    for spec in service.list_joints():
                        current = service.get_joint_control_state()["positions"].get(spec.name, 0.0)
                        with ui.HStack(height=28):
                            ui.Label(spec.name, width=120)
                            model = ui.SimpleFloatModel(float(current))
                            self._joint_control_models[spec.name] = model
                            ui.FloatSlider(model=model, min=float(spec.lower_limit), max=float(spec.upper_limit))
                            ui.Label(spec.unit, width=36)
                    with ui.HStack(height=32):
                        ui.Button("Plan", clicked_fn=self._ui_plan_joint_targets)
                        ui.Button("Check", clicked_fn=self._ui_check_last_joint_plan)
                        ui.Button("Execute", clicked_fn=self._ui_execute_last_joint_plan)
                    with ui.HStack(height=32):
                        ui.Button("Stop", clicked_fn=self._ui_stop_joint_motion)
                        ui.Button("Reset Targets", clicked_fn=self._ui_reset_joint_targets)
                    with ui.HStack(height=32):
                        ui.Button("Collision ON/OFF", clicked_fn=self._ui_toggle_collision_check)
                        ui.Button("Apply Stable Drive", clicked_fn=self._ui_apply_stable_drive)
        except Exception as exc:
            print(f"[isaac worker] joint control UI unavailable: {exc}", flush=True)

    def _ui_targets(self) -> dict[str, float]:
        targets = {}
        for name, model in self._joint_control_models.items():
            if hasattr(model, "get_value_as_float"):
                targets[name] = float(model.get_value_as_float())
            else:
                targets[name] = float(model.as_float)
        return targets

    def _ui_set_status(self, text: str) -> None:
        if self._joint_control_status_model is not None:
            try:
                self._joint_control_status_model.set_value(str(text))
            except Exception:
                pass

    def _ui_set_collision_mode(self) -> None:
        model = getattr(self, "_collision_mode_model", None)
        if model is not None:
            try:
                model.set_value(self._collision_mode_text())
            except Exception:
                pass

    def _init_ui_action_queue(self) -> None:
        self._ui_action_queue: queue.Queue[tuple[str, Any]] = queue.Queue()

    def _enqueue_ui_action(self, label: str, action: Any) -> None:
        self._ui_set_status(f"{label} queued")
        self._ui_action_queue.put((str(label), action))

    def _process_ui_actions(self) -> bool:
        processed = False
        while True:
            try:
                label, action = self._ui_action_queue.get_nowait()
            except queue.Empty:
                return processed
            try:
                self._ui_set_status(f"{label} running")
                action()
            except Exception as exc:
                self._ui_set_status(f"{label} failed: {exc}")
            finally:
                self._ui_action_queue.task_done()
            processed = True

    def _ui_last_plan_id(self) -> str | None:
        state = self.get_joint_control_state()
        last_plan = state.get("last_plan") or {}
        return last_plan.get("plan_id")

    def _ui_plan_joint_targets(self) -> None:
        self._enqueue_ui_action("Plan", self._ui_plan_joint_targets_now)

    def _ui_plan_joint_targets_now(self) -> None:
        try:
            plan = self.plan_joint_targets(self._ui_targets())
            self._ui_set_status(f"Planned {plan.plan_id}: {len(plan.waypoints)} waypoints")
        except Exception as exc:
            self._ui_set_status(f"Plan failed: {exc}")

    def _ui_check_last_joint_plan(self) -> None:
        self._enqueue_ui_action("Check", self._ui_check_last_joint_plan_now)

    def _ui_check_last_joint_plan_now(self) -> None:
        try:
            plan_id = self._ui_last_plan_id()
            if not plan_id:
                self._ui_set_status("No plan to check")
                return
            result = self.check_joint_plan(plan_id)
            self._ui_set_status(f"Check {result.code}: {result.message}")
        except Exception as exc:
            self._ui_set_status(f"Check failed: {exc}")

    def _ui_execute_last_joint_plan(self) -> None:
        self._enqueue_ui_action("Execute", self._ui_execute_last_joint_plan_now)

    def _ui_execute_last_joint_plan_now(self) -> None:
        try:
            plan_id = self._ui_last_plan_id()
            if not plan_id:
                self._ui_set_status("No plan to execute")
                return
            result = self.execute_joint_plan(plan_id)
            self._ui_set_status(f"Execute {result.code}: {result.message}")
        except Exception as exc:
            self._ui_set_status(f"Execute failed: {exc}")

    def _ui_toggle_collision_check(self) -> None:
        self._enqueue_ui_action("Collision", self._ui_toggle_collision_check_now)

    def _ui_toggle_collision_check_now(self) -> None:
        try:
            state = self.get_joint_control_state()
            enabled = bool(state.get("collision_check_enabled", True))
            result = self.set_collision_check_enabled(not enabled)
            self._ui_set_collision_mode()
            mode = "ON" if result.get("collision_check_enabled") else "OFF"
            self._ui_set_status(f"Collision {mode}")
        except Exception as exc:
            self._ui_set_status(f"Collision toggle failed: {exc}")

    def _ui_apply_stable_drive(self) -> None:
        self._enqueue_ui_action("Stable Drive", self._ui_apply_stable_drive_now)

    def _ui_apply_stable_drive_now(self) -> None:
        try:
            result = self.apply_stable_drive_settings()
            self._ui_set_status(f"Stable drive applied: {len(result.get('settings', {}))} joints")
        except Exception as exc:
            self._ui_set_status(f"Stable drive failed: {exc}")

    def _ui_stop_joint_motion(self) -> None:
        self._enqueue_ui_action("Stop", self._ui_stop_joint_motion_now)

    def _ui_stop_joint_motion_now(self) -> None:
        result = self.stop_joint_motion()
        self._ui_set_status(result.message)

    def _ui_reset_joint_targets(self) -> None:
        self._enqueue_ui_action("Reset", self._ui_reset_joint_targets_now)

    def _ui_reset_joint_targets_now(self) -> None:
        positions = self.get_joint_control_state().get("positions", {})
        for name, model in self._joint_control_models.items():
            try:
                model.set_value(float(positions.get(name, 0.0)))
            except Exception:
                pass
        self._ui_set_status("Targets reset")

    def _query_prim_pose(self, entity_id: str) -> dict[str, Any] | None:
        if self._stage is None or not entity_id.startswith("/"):
            return None
        try:
            from pxr import Gf, UsdGeom

            prim = self._stage.GetPrimAtPath(entity_id)
            if not prim or not prim.IsValid():
                return None
            matrix = UsdGeom.Xformable(prim).ComputeLocalToWorldTransform(0.0)
            translation = matrix.ExtractTranslation()
            quat = Gf.Transform(matrix).GetRotation().GetQuat()
            imaginary = quat.GetImaginary()
            return {
                "xyz": [float(translation[0]), float(translation[1]), float(translation[2])],
                "quat_xyzw": [float(imaginary[0]), float(imaginary[1]), float(imaginary[2]), float(quat.GetReal())],
                "frame_id": "world",
            }
        except Exception:
            return None

    def _render_with_isaac(self, camera: str, width: int, height: int) -> bytes | None:
        image = self._render_with_replicator(camera, width, height)
        if image is not None:
            return image
        return self._render_with_viewport(camera, width, height)

    def _render_with_replicator(self, camera: str, width: int, height: int) -> bytes | None:
        try:
            import omni.replicator.core as rep
        except Exception as exc:
            self.render_error = f"replicator unavailable: {exc}"
            return None

        try:
            self._ensure_camera(camera)
            key = (str(camera), int(width), int(height))
            if key not in self._rgb_annotators:
                render_product = rep.create.render_product(str(camera), (int(width), int(height)), force_new=True)
                annotator = rep.AnnotatorRegistry.get_annotator("rgb")
                annotator.attach([render_product])
                self._rgb_annotators[key] = (render_product, annotator)
            _, annotator = self._rgb_annotators[key]
            data = None
            for _ in range(4):
                rep.orchestrator.step()
                self.app.update()
                data = annotator.get_data()
                if data is not None:
                    if isinstance(data, dict):
                        data = data.get("data") if data.get("data") is not None else data.get("rgb")
                    shape = getattr(data, "shape", None)
                    if shape is not None and len(shape) >= 2 and int(shape[0]) > 0 and int(shape[1]) > 0:
                        break
            if data is None:
                self.render_error = "replicator rgb annotator returned no data"
                return None
            return encode_png_rgb(data)
        except Exception as exc:
            self.render_error = f"replicator render failed: {exc}"
            return None

    def _render_with_viewport(self, camera: str, width: int, height: int) -> bytes | None:
        try:
            import omni.kit.viewport.utility
            from omni.kit.viewport.utility import capture_viewport_to_buffer
        except Exception as exc:
            self.render_error = f"viewport capture unavailable: {exc}"
            return None

        try:
            self._ensure_camera(camera)
            viewport = omni.kit.viewport.utility.get_active_viewport()
            if viewport is None:
                self.render_error = "active viewport unavailable"
                return None
            viewport.camera_path = camera
            viewport.resolution = (int(width), int(height))
            self.app.update()
            capture = capture_viewport_to_buffer(viewport)
            data = getattr(capture, "data", None)
            if isinstance(data, bytes):
                return data
        except Exception as exc:
            self.render_error = f"viewport render failed: {exc}"
            return None
        return None

    def _ensure_camera(self, camera: str) -> None:
        if self._stage is None:
            return
        try:
            from pxr import Gf, UsdGeom

            prim = self._stage.GetPrimAtPath(camera)
            if prim and prim.IsValid() and prim.IsA(UsdGeom.Camera):
                return
            camera_prim = UsdGeom.Camera.Define(self._stage, camera)
            camera_prim.GetFocalLengthAttr().Set(24.0)
            xform = UsdGeom.Xformable(camera_prim.GetPrim())
            xform.ClearXformOpOrder()
            xform.AddTranslateOp().Set(Gf.Vec3d(2.0, -3.0, 2.0))
            xform.AddRotateXYZOp().Set(Gf.Vec3f(60.0, 0.0, 35.0))
            self.app.update()
        except Exception as exc:
            self.render_error = f"camera setup failed: {exc}"


class _IsaacJointControlAdapter:
    def __init__(self, controller: IsaacController) -> None:
        self.controller = controller

    def list_joint_specs(self) -> list[JointSpec]:
        if self.controller._stage is None:
            return list(DEFAULT_MVPPKUSHENGKE_JOINTS)
        return [self._spec_from_stage(default_spec) for default_spec in DEFAULT_MVPPKUSHENGKE_JOINTS]

    def get_joint_positions(self) -> dict[str, float]:
        positions: dict[str, float] = {}
        fallback = self.controller.joint_states.get("joint_control", {})
        for spec in self.list_joint_specs():
            value = self._get_drive_target(spec)
            positions[spec.name] = float(fallback.get(spec.name, value))
        return positions

    def capture_state(self) -> dict[str, float]:
        return self.get_joint_positions()

    def restore_state(self, state: dict[str, float]) -> None:
        self.set_joint_targets({str(key): float(value) for key, value in dict(state).items()})
        self.step_simulation(1)

    def set_joint_targets(self, targets: dict[str, float]) -> None:
        clean_targets = {str(key): float(value) for key, value in targets.items()}
        self.controller.joint_states["joint_control"] = dict(clean_targets)
        if self.controller._stage is None:
            return
        specs = {spec.name: spec for spec in self.list_joint_specs()}
        for name, value in clean_targets.items():
            spec = specs[name]
            prim = self.controller._stage.GetPrimAtPath(spec.path)
            if not prim or not prim.IsValid():
                raise RuntimeError(f"Joint prim not found: {spec.path}")
            attr = prim.GetAttribute(f"drive:{spec.drive_axis}:physics:targetPosition")
            if not attr:
                from pxr import UsdPhysics

                drive = UsdPhysics.DriveAPI.Apply(prim, spec.drive_axis)
                attr = drive.CreateTargetPositionAttr()
            attr.Set(float(value))

    def step_simulation(self, steps: int) -> None:
        for _ in range(max(0, int(steps))):
            self.controller.app.update()

    def get_disallowed_contacts(self) -> list[dict[str, Any]]:
        report = self._read_contact_report()
        return self._parse_contacts(report)

    def apply_drive_settings(self, settings: dict[str, dict[str, float]]) -> dict[str, dict[str, float]]:
        applied: dict[str, dict[str, float]] = {}
        specs = {spec.name: spec for spec in self.list_joint_specs()}
        for name, values in settings.items():
            spec = specs[str(name)]
            clean_values = {
                "stiffness": float(values["stiffness"]),
                "damping": float(values["damping"]),
                "max_force": float(values["max_force"]),
            }
            applied[spec.name] = clean_values
            if self.controller._stage is None:
                continue
            prim = self.controller._stage.GetPrimAtPath(spec.path)
            if not prim or not prim.IsValid():
                raise RuntimeError(f"Joint prim not found: {spec.path}")
            self._set_drive_attr(prim, spec.drive_axis, "stiffness", clean_values["stiffness"])
            self._set_drive_attr(prim, spec.drive_axis, "damping", clean_values["damping"])
            self._set_drive_attr(prim, spec.drive_axis, "maxForce", clean_values["max_force"])
        return applied

    def _spec_from_stage(self, default_spec: JointSpec) -> JointSpec:
        prim = self.controller._stage.GetPrimAtPath(default_spec.path)
        if not prim or not prim.IsValid():
            return default_spec
        body0 = self._first_target(prim, "physics:body0") or default_spec.body0
        body1 = self._first_target(prim, "physics:body1") or default_spec.body1
        lower = self._attr_float(prim, "physics:lowerLimit", default_spec.lower_limit)
        upper = self._attr_float(prim, "physics:upperLimit", default_spec.upper_limit)
        drive_axis = "angular" if "Revolute" in prim.GetTypeName() else "linear"
        joint_type = "revolute" if drive_axis == "angular" else "prismatic"
        unit = "deg" if drive_axis == "angular" else "m"
        return JointSpec(
            name=default_spec.name,
            path=default_spec.path,
            joint_type=joint_type,
            drive_axis=drive_axis,
            unit=unit,
            lower_limit=lower,
            upper_limit=upper,
            body0=body0,
            body1=body1,
        )

    def _get_drive_target(self, spec: JointSpec) -> float:
        if self.controller._stage is None:
            return 0.0
        prim = self.controller._stage.GetPrimAtPath(spec.path)
        if not prim or not prim.IsValid():
            return 0.0
        attr = prim.GetAttribute(f"drive:{spec.drive_axis}:physics:targetPosition")
        if not attr:
            return 0.0
        value = attr.Get()
        return 0.0 if value is None else float(value)

    def _set_drive_attr(self, prim: Any, drive_axis: str, name: str, value: float) -> None:
        attr = prim.GetAttribute(f"drive:{drive_axis}:physics:{name}")
        if not attr:
            from pxr import UsdPhysics

            drive = UsdPhysics.DriveAPI.Apply(prim, drive_axis)
            if name == "stiffness":
                attr = drive.CreateStiffnessAttr()
            elif name == "damping":
                attr = drive.CreateDampingAttr()
            elif name == "maxForce":
                attr = drive.CreateMaxForceAttr()
            else:
                raise RuntimeError(f"Unsupported drive attr: {name}")
        attr.Set(float(value))

    def _read_contact_report(self) -> Any:
        try:
            import omni.physx

            interface = omni.physx.get_physx_simulation_interface()
            if hasattr(interface, "get_full_contact_report"):
                return interface.get_full_contact_report()
            if hasattr(interface, "get_contact_report"):
                return interface.get_contact_report()
        except Exception as exc:
            raise RuntimeError(f"contact report unavailable: {exc}") from exc
        raise RuntimeError("contact report unavailable: no supported PhysX contact API")

    def _parse_contacts(self, report: Any) -> list[dict[str, Any]]:
        if report is None:
            return []
        if isinstance(report, (list, tuple)):
            contacts: list[dict[str, Any]] = []
            for item in report:
                contacts.extend(self._parse_contacts(item))
            return contacts
        headers = getattr(report, "contact_headers", None) or getattr(report, "headers", None)
        if headers is not None:
            return self._parse_contacts(headers)
        body0 = self._object_path(report, ("body0", "actor0", "path0", "prim0"))
        body1 = self._object_path(report, ("body1", "actor1", "path1", "prim1"))
        if body0 or body1:
            return [{"body0": body0 or "", "body1": body1 or ""}]
        try:
            if len(report) == 0:
                return []
        except Exception:
            pass
        if bool(report) is False:
            return []
        raise RuntimeError(f"contact report format unsupported: {type(report).__name__}")

    def _first_target(self, prim: Any, rel_name: str) -> str | None:
        rel = prim.GetRelationship(rel_name)
        if not rel:
            return None
        targets = rel.GetTargets()
        return str(targets[0]) if targets else None

    def _attr_float(self, prim: Any, attr_name: str, default: float) -> float:
        attr = prim.GetAttribute(attr_name)
        if not attr:
            return float(default)
        value = attr.Get()
        return float(default) if value is None else float(value)

    def _object_path(self, item: Any, names: tuple[str, ...]) -> str | None:
        for name in names:
            value = getattr(item, name, None)
            if value:
                return str(value)
        return None


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    controller = IsaacController(
        headless=args.headless,
        camera=args.camera,
        robot_prim=args.robot_prim,
        warmup_steps=args.warmup_steps,
        joint_control_ui=args.joint_control_ui,
    )
    if args.scene:
        controller.load_scene(args.scene)
    state = IsaacWorkerState(
        controller,
        dispatch_on_main_thread=True,
        rpc_timeout_s=args.rpc_timeout_s,
    )
    server = ThreadingHTTPServer((args.host, args.port), make_handler(state))
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    print(f"[isaac worker] serving http://{args.host}:{args.port}/rpc", flush=True)
    try:
        while True:
            processed = state.process_next(timeout=0.05)
            if not processed:
                controller.idle()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=2.0)
        controller.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
