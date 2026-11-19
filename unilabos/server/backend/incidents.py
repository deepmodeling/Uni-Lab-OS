"""设备状态联锁的线程安全权威投影。"""

from __future__ import annotations

import threading
import time
import uuid
from collections import deque
from copy import deepcopy
from typing import Any, Callable, Deque, Dict, List, Mapping, Optional, Set, Tuple

from unilabos.registry.status_policy import evaluate_status


IncidentListener = Callable[[Dict[str, Any]], None]


class StatusIncidentManager:
    """把设备标量状态转换为 v8 Status Incident 与 Scheduler Hold。"""

    def __init__(self, history: int = 400) -> None:
        self._lock = threading.RLock()
        self._active: Dict[Tuple[str, str], Dict[str, Any]] = {}
        self._history: Deque[Dict[str, Any]] = deque(maxlen=history)
        self._listeners: List[IncidentListener] = []

    def add_listener(self, listener: IncidentListener) -> None:
        with self._lock:
            if listener not in self._listeners:
                self._listeners.append(listener)

    def remove_listener(self, listener: IncidentListener) -> None:
        with self._lock:
            self._listeners = [item for item in self._listeners if item != listener]

    @staticmethod
    def _new_incident(
        device_id: str,
        property_name: str,
        value: Any,
        config: Mapping[str, Any],
        ts: float,
    ) -> Dict[str, Any]:
        hold_enabled = bool(config.get("hold"))
        policy_id = str(config.get("code") or f"{device_id}.{property_name}")
        return {
            "incident_id": str(uuid.uuid4()),
            "policy_id": policy_id,
            "device_id": device_id,
            "property_name": property_name,
            "observed_value": value,
            "severity": str(config.get("severity") or "error"),
            "when": {"eq": value},
            "clear_when": {"ne": value},
            "state": "awaiting_decision",
            "scope": "device",
            "mode": "interlock" if hold_enabled else "notify",
            "hold": {"new_dispatch": hold_enabled, "running": "continue"},
            "hold_token": str(uuid.uuid4()) if hold_enabled else "",
            "message": str(config.get("message") or policy_id),
            "options": [
                {
                    "action": "resume",
                    "label": "人工确认并恢复调度",
                    "description": "现场已处理，释放当前调度联锁。",
                }
            ],
            "retry": {
                "max_attempts": 3,
                "backoff_seconds": 5.0,
                "attempts_used": 0,
            },
            "decision_timeout_seconds": 300.0,
            "default_on_timeout": "hold",
            "created_at": ts,
            "updated_at": ts,
            "expires_at": None,
            "require_confirmation": True,
        }

    def observe(
        self,
        device_id: str,
        property_name: str,
        value: Any,
        policy: Mapping[str, Any] | None,
        *,
        now: Optional[float] = None,
    ) -> Optional[Dict[str, Any]]:
        evaluation = evaluate_status(policy, value)
        if evaluation.healthy is None:
            return None
        key = (device_id, property_name)
        ts = time.time() if now is None else float(now)
        events: List[Dict[str, Any]] = []
        with self._lock:
            current = self._active.get(key)
            if evaluation.healthy:
                if current is None:
                    return None
                current["state"] = "cleared"
                current["observed_value"] = value
                current["updated_at"] = ts
                current["cleared_at"] = ts
                finished = deepcopy(current)
                self._history.append(finished)
                del self._active[key]
                events.append({
                    "type": "status_incident_cleared",
                    "incident": finished,
                    "data": {
                        "incident_id": finished["incident_id"],
                        "policy_id": finished["policy_id"],
                        "device_id": device_id,
                        "property_name": property_name,
                        "state": "cleared",
                        "observed_value": value,
                        "hold_token": finished["hold_token"],
                        "cleared_at": ts,
                    },
                })
            else:
                assert evaluation.incident is not None
                config = evaluation.incident
                policy_id = str(config.get("code") or "")
                if current is not None and current["policy_id"] == policy_id:
                    current["observed_value"] = value
                    current["updated_at"] = ts
                    return deepcopy(current)
                if current is not None:
                    current["state"] = "cleared"
                    current["observed_value"] = value
                    current["updated_at"] = ts
                    current["cleared_at"] = ts
                    finished = deepcopy(current)
                    self._history.append(finished)
                    events.append(
                        {
                            "type": "status_incident_cleared",
                            "incident": finished,
                            "data": {
                                "incident_id": finished["incident_id"],
                                "policy_id": finished["policy_id"],
                                "device_id": device_id,
                                "property_name": property_name,
                                "state": "cleared",
                                "observed_value": value,
                                "hold_token": finished["hold_token"],
                                "cleared_at": ts,
                            },
                        }
                    )
                incident = self._new_incident(
                    device_id, property_name, value, config, ts
                )
                self._active[key] = incident
                events.append(
                    {
                        "type": "status_incident_required",
                        "incident": deepcopy(incident),
                        "data": deepcopy(incident),
                    }
                )
        for event in events:
            self._notify(event)
        return deepcopy(events[-1]["incident"]) if events else None

    def decide(
        self,
        incident_id: str,
        *,
        action: str = "",
        option: Optional[Mapping[str, Any]] = None,
        reason: str = "",
        now: Optional[float] = None,
    ) -> Optional[Dict[str, Any]]:
        """提交 Host 已公布的 option；当前默认策略只公布 resume。"""

        selected_action = str(action or (option or {}).get("action") or "")
        ts = time.time() if now is None else float(now)
        with self._lock:
            key, incident = self._find_active_item_locked(incident_id)
            if key is None or incident is None:
                return None
            if incident["state"] == "recovering":
                raise RuntimeError("incident recovery is already running")
            allowed = {
                str(item.get("action"))
                for item in incident.get("options", [])
                if isinstance(item, Mapping)
            }
            if selected_action not in allowed:
                raise ValueError("action is not one of the incident options")
            if selected_action == "execute_recovery":
                incident["state"] = "recovering"
                incident["updated_at"] = ts
                result = deepcopy(incident)
                return {
                    "incident": result,
                    "ack": {
                        "incident_id": incident_id,
                        "status": "delivered",
                        "state": "recovering",
                    },
                }

            incident["state"] = "resolved"
            incident["updated_at"] = ts
            incident["resolved_at"] = ts
            incident["selected_action"] = selected_action
            incident["resolution_reason"] = str(reason or "")
            finished = deepcopy(incident)
            self._history.append(finished)
            del self._active[key]

        data = {
            "incident_id": incident_id,
            "policy_id": finished["policy_id"],
            "device_id": finished["device_id"],
            "property_name": finished["property_name"],
            "state": "resolved",
            "selected_action": selected_action,
            "reason": str(reason or ""),
            "hold_token": finished["hold_token"],
            "resolved_at": ts,
        }
        self._notify(
            {
                "type": "status_incident_resolved",
                "incident": finished,
                "data": data,
            }
        )
        return {
            "incident": finished,
            "ack": {
                "incident_id": incident_id,
                "status": "delivered",
                "state": "resolved",
            },
        }

    def list(
        self,
        *,
        device_id: str = "",
        include_terminal: bool = False,
        include_resolved: Optional[bool] = None,
    ) -> List[Dict[str, Any]]:
        include_terminal = (
            include_terminal
            if include_resolved is None
            else bool(include_resolved)
        )
        with self._lock:
            items = [deepcopy(item) for item in self._active.values()]
            if include_terminal:
                items.extend(deepcopy(list(self._history)))
        if device_id:
            items = [item for item in items if item["device_id"] == device_id]
        return sorted(items, key=lambda item: (item["updated_at"], item["incident_id"]), reverse=True)

    def holds(self) -> List[Dict[str, Any]]:
        with self._lock:
            incidents = [
                deepcopy(item)
                for item in self._active.values()
                if item["mode"] == "interlock" and item["hold"]["new_dispatch"]
            ]
        return [
            {
                "hold_token": item["hold_token"],
                "incident_id": item["incident_id"],
                "policy_id": item["policy_id"],
                "device_id": item["device_id"],
                "property_name": item["property_name"],
                "scope": item["scope"],
                "reason": item["message"],
                "created_at": item["created_at"],
            }
            for item in incidents
        ]

    def held_device_ids(self) -> Set[str]:
        return {item["device_id"] for item in self.holds()}

    def is_device_held(self, device_id: str) -> bool:
        return device_id in self.held_device_ids()

    def _find_active_item_locked(
        self, incident_id: str
    ) -> Tuple[Optional[Tuple[str, str]], Optional[Dict[str, Any]]]:
        for key, incident in self._active.items():
            if incident["incident_id"] == incident_id:
                return key, incident
        return None, None

    def _notify(self, event: Dict[str, Any]) -> None:
        payload = deepcopy(event)
        with self._lock:
            listeners = list(self._listeners)
        for listener in listeners:
            try:
                listener(deepcopy(payload))
            except Exception:  # noqa: BLE001 - 单个监听器不能阻塞联锁
                pass


__all__ = ["IncidentListener", "StatusIncidentManager"]
