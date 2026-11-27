"""Reservations must survive extracting their member actions into operations."""
from unilabos.workflow.authoring_ast import parse_authoring_source
from unilabos.workflow.composite_graph_rewrite import merge_expanded_resource_scopes

ENTRY = '9a086c5a-6eda-4e7d-9e6c-ac60eb3c5d3f'
CALL = 'f71f85b5-a3cb-5eb9-a138-8e4eb242ca43'


def test_reservation_accepts_operation_result():
    source = f'''from typing import TypedDict
from unilabos.workflow.authoring import workflow, resources
from example.operations import prepare
class Result(TypedDict):
    value: str
@workflow(workflow_uuid='{ENTRY}', displayname='Reservation example')
def example() -> Result:
    # unilab:node_uuid={CALL}
    prepared = prepare()
    with resources() as locks:
        locks.hold('tecan', nodes=[prepared], name='Preparation')
    return {{'value': prepared.value}}
'''
    program = parse_authoring_source(python_source=source, expected_workflow_uuid=ENTRY)
    assert program.resource_scopes[0].node_uuids == (CALL,)


def test_expanded_operation_reservation_keeps_parent_ownership():
    parent = {'scope_id': 'parent', 'kind': 'reservation', 'resources': ['tecan'],
              'parent_scope_id': None, 'node_uuids': ['load', CALL, 'unload']}
    child = {'scope_id': 'child', 'kind': 'reservation', 'resources': ['tecan'],
             'parent_scope_id': None, 'node_uuids': ['dispense']}
    scopes = merge_expanded_resource_scopes([parent], nested_invocations=[(CALL, ['dispense'], [child])])
    assert scopes[0]['node_uuids'] == ['load', 'dispense', 'unload']
    assert scopes[1]['parent_scope_id'] == 'parent'


def test_parent_reservation_survives_publication_source_rewrite_and_restart(tmp_path, monkeypatch):
    from tests.workflow import test_composite_publication_roundtrip as publication

    original = publication._parent_source
    original_pin = publication._pin

    def parent_source(identity):
        return original(identity).replace(
            'import workflow, workflow_output', 'import workflow, workflow_output, resources'
        ).replace('    return workflow_output()',
                  '    with resources() as locks:\n        locks.hold("robot", nodes=[result], name="Operation reservation")\n'
                  '    return workflow_output()')

    def pin(graph):
        scopes = graph['workflow']['meta_data']['unilab']['resource_scopes']
        own = [scope for scope in scopes if not scope.get('composite_invocation_uuid')]
        assert len(own) == 1
        assert own[0]['node_uuids']
        assert set(own[0]['node_uuids']) <= {node['uuid'] for node in graph['nodes']}
        return original_pin(graph)

    monkeypatch.setattr(publication, '_parent_source', parent_source)
    monkeypatch.setattr(publication, '_pin', pin)
    publication.test_consecutive_publications_keep_identity_through_source_and_restart(
        tmp_path, parent_source='python', publish_parent=True)
