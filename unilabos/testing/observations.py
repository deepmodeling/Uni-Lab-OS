"""测试装配真实 OS 观测服务；测试直接调用各自职责接口。"""
from types import SimpleNamespace

from unilabos.app.scheduler.device_observations import DeviceObservationService
from unilabos.app.scheduler.device_state import DeviceStateStore
from unilabos.app.scheduler.inventory.runtime_view import ObservationInventoryView
from unilabos.app.scheduler.observation_context import ObservationContext
from unilabos.app.scheduler.observation_sessions import ObservationSessions


def observation_services(inventory, db_path=":memory:", **kwargs):
    store = DeviceStateStore(db_path)
    try:
        context = ObservationContext(ObservationInventoryView(inventory), store, **kwargs)
        observations = DeviceObservationService(context)
        sources = ObservationSessions(context, observations)
        return SimpleNamespace(inventory=inventory, store=store, context=context,
                               observations=observations, sources=sources)
    except BaseException:
        store.close()
        raise
