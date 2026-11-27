"""所选动作预留经组合发布、源码回写与冷启动后保持稳定身份。"""
from tests.workflow import test_composite_publication_roundtrip as publication


def test_selected_reservation_survives_composite_publication_roundtrip(tmp_path, monkeypatch):
    original_source = publication._source_with_steps
    original_pin = publication._pin
    seen_scopes = []

    def source(count):
        text = original_source(count)
        text = 'from unilabos.workflow.authoring import resources\n' + text
        selected = ', '.join(f'step_{index}' for index in range(count))
        return text.replace('    return workflow_output()',
                            f'    with resources() as locks:\n        locks.hold("robot", nodes=[{selected}], name="设备预留")\n    return workflow_output()')

    def pin(graph):
        scopes = graph['workflow']['meta_data']['unilab']['resource_scopes']
        reservations = [scope for scope in scopes if scope['kind'] == 'reservation']
        assert len(reservations) == 1
        scope = reservations[0]
        assert scope['composite_invocation_uuid'] == publication.INVOCATION_UUID
        assert set(scope['node_uuids']).issubset({node['uuid'] for node in graph['nodes']})
        seen_scopes.append(scope)
        return original_pin(graph)

    monkeypatch.setattr(publication, '_source_with_steps', source)
    monkeypatch.setattr(publication, '_pin', pin)
    publication.test_consecutive_publications_keep_identity_through_source_and_restart(
        tmp_path, parent_source='python', publish_parent=True,
    )
    assert {len(scope['node_uuids']) for scope in seen_scopes} == {1, 2, 3, 4}
