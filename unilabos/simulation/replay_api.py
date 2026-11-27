"""回放播放控制：查看进度、暂停、继续、倍速、按时间或命令跳转。

只作用于 ``UNILABOS_OBSERVATION_REPLAY_FILE`` 启动的显示回放（来源模式 replay，不执行动作、
不结算库存）；没有配置回放时返回 503。改变的只是回放发布哪一时刻的观测值。
"""

from __future__ import annotations

from typing import Callable

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from unilabos.runtime.observations import StateError


class Seek(BaseModel):
    t: float | None = Field(default=None, ge=0)
    command: str | None = None


class RecordingStart(BaseModel):
    task_uuid: str | None = None
    path: str | None = None


class Load(BaseModel):
    task_uuid: str | None = None
    sources: str | None = None
    loop: bool = False


class Speed(BaseModel):
    speed: float = Field(gt=0, le=100)


def create_replay_router(get_feed: Callable[[], object | None], *, prefix="/device-state/replay") -> APIRouter:
    router = APIRouter(prefix=prefix, tags=["device-state"])

    def feed():
        instance = get_feed()
        if instance is None:
            raise HTTPException(503, "observation replay not configured")
        return instance

    def call(function, **kwargs):
        try:
            return {"code": 0, "data": function(**kwargs)}
        except KeyError as error:
            raise HTTPException(404, str(error.args[0] if error.args else error)) from error
        except ValueError as error:
            raise HTTPException(422, str(error)) from error

    @router.get("")
    def status():
        return call(feed().status)

    @router.post("/pause")
    def pause():
        return call(feed().pause)

    @router.post("/resume")
    def resume():
        return call(feed().resume)

    @router.post("/speed")
    def speed(body: Speed):
        return call(feed().set_speed, speed=body.speed)

    @router.post("/seek")
    def seek(body: Seek):
        return call(feed().seek, t=body.t, command=body.command)

    # ---- 运行时加载回放（不重启后端）----
    @router.get("/recordings")
    def recordings():
        from unilabos.simulation.task_recording import list_recordings

        return {"code": 0, "data": list_recordings()}

    @router.post("/load")
    def load(body: Load):
        """按任务（``<task>.mcap`` + 数据库里的作业与物料）或显式回放源加载回放。"""
        from unilabos.simulation import replay_feed

        try:
            if body.task_uuid:
                from unilabos.app.scheduler.integration import get_device_observations, get_edge_backend
                from unilabos.simulation.task_recording import prepare_task_replay
                from unilabos.workflow.composition import get_workflow_service

                if get_workflow_service() is None:
                    raise HTTPException(503, "工作流服务尚未就绪，稍后再试")
                backend = get_edge_backend()
                spec = prepare_task_replay(body.task_uuid, get_device_observations(),
                                           getattr(backend, "device_state", None), get_workflow_service())
            elif body.sources:
                spec = body.sources
            else:
                raise ValueError("需要 task_uuid 或 sources")
            feed = replay_feed.load(spec, loop=body.loop)
        except (ValueError, KeyError) as error:
            raise HTTPException(422, str(error)) from error
        except StateError as error:
            raise HTTPException(409, error.reason) from error
        return {"code": 0, "data": feed.status()}

    @router.post("/unload")
    def unload():
        from unilabos.simulation import replay_feed

        return {"code": 0, "data": {"unloaded": replay_feed.unload()}}

    # ---- 实时录制（高频观测写 MCAP）----
    @router.post("/recordings/start")
    def recording_start(body: RecordingStart):
        import os
        import time
        from pathlib import Path

        from unilabos.simulation import mcap_recorder

        root = Path(os.environ.get("ULAB_RECORDING_DIR") or Path.home() / ".unilabos" / "recordings").expanduser()
        name = (body.task_uuid or time.strftime("%Y%m%d-%H%M%S")) + ".mcap"
        path = Path(body.path).expanduser() if body.path else root / name
        try:
            recorder = mcap_recorder.start(path, task_uuid=body.task_uuid)
        except RuntimeError as error:
            raise HTTPException(409, str(error)) from error
        return {"code": 0, "data": {"path": str(recorder.path), "task_uuid": body.task_uuid}}

    @router.post("/recordings/stop")
    def recording_stop():
        from unilabos.simulation import mcap_recorder

        result = mcap_recorder.stop()
        if result is None:
            raise HTTPException(404, "没有进行中的录制")
        return {"code": 0, "data": result}

    return router
