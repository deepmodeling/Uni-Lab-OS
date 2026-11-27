"""显式任务级物料事项；原生设备/节点错误继续使用原协议。"""
from typing import Any
from uuid import UUID


def validate_task_attention(item: dict[str, Any]) -> bool:
    try:
        if (item.get('decision_kind') not in {'material_export', 'handoff_reconciliation'}
                or 'workflow_node_job_uuid' in item
                or type(item.get('revision')) is not int or not 1 <= item['revision'] <= 2**53-1
                or not isinstance(item.get('meta_data'), dict) or not isinstance(item.get('options'), list)
                or not all(isinstance(item.get(k), str) and item[k].strip()
                           for k in ('decision_id', 'opened_at'))):
            return False
        if item['decision_kind'] == 'material_export':
            UUID(item['workflow_task_uuid'])
            UUID(item['invocation_uuid'])
            return (item.get('phase') in {'prepare', 'evidence', 'boundary', 'output'}
                and item['decision_id'] == item['invocation_uuid'] + ':' + item['phase']
                and item.get('status') in {'blocked', 'exhausted', 'retrying'}
                and item['options'] == ([] if item['phase'] == 'output' else [{'id': 'resume_export'}])
                and isinstance(item['meta_data'].get('last_error'), str))
        UUID(item['handoff_uuid'])
        return (item['decision_id'] == item['handoff_uuid'] and item.get('status') == 'required'
                and item['options'] == [] and isinstance(item['meta_data'].get('progress'), dict))
    except (ValueError, TypeError, KeyError, AttributeError):
        return False
