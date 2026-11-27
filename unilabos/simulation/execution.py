"""OS 的单世界动作执行循环；设备包提供控制、扫描响应和完成判据。"""
from __future__ import annotations

import json
import socket
import threading
from copy import deepcopy


class WorldClient:
    """有代际的物理 RPC；调用失败不重发动作，不回退真实设备。"""

    def __init__(self, path, identity, *, timeout=120):
        """记录 Unix socket、世界身份和超时秒数；从第零步开始，不在构造时连接。"""
        self.path, self.identity, self.timeout = str(path), dict(identity), timeout
        self.tick = 0
        self.sequence = 0
        self.faulted = False

    def _call(self, request):
        """执行一次 JSON RPC 并返回 result；任何异常均锁定未知状态，后续调用不重发。"""
        if self.faulted:
            raise RuntimeError("world_result_unknown")
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(self.timeout)
                client.connect(self.path)
                client.sendall(json.dumps(request, allow_nan=False).encode() + b"\n")
                with client.makefile("rb") as stream:
                    response = json.loads(stream.readline(16 * 1024 * 1024))
            if response.get("ok") is not True:
                raise RuntimeError(response.get("error", "invalid_world_response"))
            return response["result"]
        except BaseException:
            self.faulted = True
            raise

    def observe(self):
        """读取当前步观测并核对世界身份及 tick；不匹配时锁定客户端并抛错。"""
        result = self._call({"op": "observe"})
        if any(result.get(k) != v for k, v in {**self.identity, "tick": self.tick}.items()):
            self.faulted = True
            raise ValueError("world_observation_identity_mismatch")
        return result["observation"]

    def command(self, name, **parameters):
        """为控制命令分配递增标识并发送；命令回执不代表动作或物理步已完成。"""
        self.sequence += 1
        return self._call({"op": "command", **self.identity, "tick": self.tick,
                           "control_id": f"control-{self.sequence}", "name": name, "parameters": parameters})

    def scan(self):
        """请求一个 50 ms、12 子步的物理扫描，确认令牌一致后才递增本地 tick。"""
        token = {**self.identity, "tick": self.tick + 1, "dt_ns": 50_000_000}
        result = self._call({"op": "step", "token": token})
        if result.get("token") != token or result.get("substeps") != 12:
            self.faulted = True
            raise ValueError("world_step_identity_mismatch")
        self.tick += 1
        return result["observation"]

    def invalidate(self):
        """永久撤销当前客户端的调用许可；已发送命令的物理结果保持未知。"""
        self.faulted=True


class DeviceWorldExecutor:
    """同一世界串行推进，完成证据与库存回执都齐全后才返回成功。"""

    def __init__(self, world, model, *, settle, authorize, record, observe=None):
        """注入世界、设备模型、授权／结算及记录函数，初始化单世界互斥和结果缓存。"""
        self.world, self.model = world, model
        self.settle, self.authorize, self.record = settle, authorize, record
        self.observe = observe
        self.lock = threading.Lock()
        self.results = {}
        self.faulted = False

    def execute(self, request, *, max_ticks=6000):
        """执行一个已授权请求，取得设备完成证据及规范库存回执后返回结果副本。

        同一 Job 的相同请求复用已完成结果；参数冲突、并行动作或故障状态
        均被拒绝。提交前置模型先结算并确认回执，再开放完成判定。
        max_ticks 限制确认扫描次数；模型、扫描或结算异常将锁定执行器
        并使世界失效，不重试动作，也不宣称物理回滚。"""
        if not self.lock.acquire(blocking=False):
            raise RuntimeError("world_action_busy")
        try:
            if self.faulted:
                raise RuntimeError("world_action_unknown")
            identity = request["identity"]
            self.authorize(request)
            key = identity["node_job_uuid"]
            encoded = json.dumps(request, sort_keys=True, allow_nan=False)
            if key in self.results:
                previous, result = self.results[key]
                if previous != encoded:
                    raise ValueError("action_replay_conflict")
                return deepcopy(result)
            self.record({"event": "action.accepted", "request": request})
            try:
                self.model.command(request["operation"], key, identity, request["parameters"])
                receipt = None
                settlement_evidence = None
                for _ in range(max_ticks):
                    state = self.world.scan()
                    self.model.accept_state(state)
                    if self.observe is not None:
                        self.observe(state, request)
                    pending_effect = getattr(self.model, "pending_effect", None)
                    effect = pending_effect() if pending_effect is not None and receipt is None else None
                    if effect is not None:
                        receipt = self.settle(request, effect)
                        self.model.confirm_receipt(receipt)
                        settlement_evidence = deepcopy(effect)
                    proof = self.model.completion()
                    if proof is not None:
                        self.model.reset_command()
                        final_state = self.world.scan()
                        self.model.accept_state(final_state)
                        if self.observe is not None:
                            self.observe(final_state, request)
                        receipt = receipt or self.settle(request, proof)
                        if not isinstance(receipt, dict) or not receipt.get("receipt_id"):
                            raise RuntimeError("canonical_inventory_receipt_missing")
                        result = {"success": True, "proof": proof, "inventory_receipt": receipt,
                                  "material_aliquot_receipts": proof.get("material_aliquot_receipts", [])}
                        if settlement_evidence is not None:
                            result["settlement_evidence"] = settlement_evidence
                        self.record({"event": "action.completed", "job_id": key, "result": result})
                        self.results[key] = (encoded, deepcopy(result))
                        return result
                raise TimeoutError("physical_completion_not_observed")
            except BaseException:
                self.faulted = True
                invalidate=getattr(self.world,'invalidate',None)
                if invalidate is not None:invalidate()
                raise
        finally:
            self.lock.release()
