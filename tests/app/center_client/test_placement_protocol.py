import copy
import json
from pathlib import Path

import pytest

from unilabos.center_protocol.placement import (
 selection_digest,
 validate_transfer_destinations,
 validate_transfer_selection,
)

FIXTURE=json.loads((Path(__file__).parent/'fixtures/transfer-placement-v1-v2.json').read_text())
def test_selection_preserves_pairing_and_has_cross_language_digest():
 value=validate_transfer_selection(FIXTURE['selection']);assert selection_digest(value,FIXTURE['transfer_definition_uuid'])==FIXTURE['selection_digest']
 value['items'].reverse();assert selection_digest(value,FIXTURE['transfer_definition_uuid'])==FIXTURE['selection_digest']
 value['items'][0]['target_site_uuid'],value['items'][1]['target_site_uuid']=value['items'][1]['target_site_uuid'],value['items'][0]['target_site_uuid']
 assert selection_digest(value,FIXTURE['transfer_definition_uuid'])!=FIXTURE['selection_digest']
@pytest.mark.parametrize('key',['selection_uuid','container_uuid','source_material_uuid','source_site_uuid','target_site_uuid'])
def test_duplicate_selection_identity_or_site_is_rejected(key):
 value=copy.deepcopy(FIXTURE['selection']);value['items'][1][key]=value['items'][0][key]
 with pytest.raises(ValueError):validate_transfer_selection(value)
@pytest.mark.parametrize('value',[0,True,-1,2**53])
def test_revision_must_be_positive_safe_integer(value):
 body=copy.deepcopy(FIXTURE['selection']);body['items'][0]['source_material_revision']=value
 with pytest.raises(ValueError):validate_transfer_selection(body)
def test_source_output_destination_does_not_require_material_that_does_not_exist_yet():
 value={'schema_version':1,'items':[{'selection_uuid':FIXTURE['selection']['items'][0]['selection_uuid'],'source_port':'sample','target_site_uuid':FIXTURE['selection']['items'][0]['target_site_uuid'],'target_resource_template_uuid':FIXTURE['selection']['items'][0]['target_resource_template_uuid']}]}
 assert validate_transfer_destinations(value)==value

def context_v2():
 from tests.app.center_client.test_transport_workflow import context
 value=context();value['schema_version']=2;value['container_uuids']=[item['container_uuid']for item in FIXTURE['selection']['items']];value['placement_plan']=copy.deepcopy(FIXTURE['execution_plan']);return value

def test_v2_transport_binds_complete_position_plan_and_generates_bound_receipts():
 from unilabos.center_protocol.transport import (
  delivery_receipt,
  pickup_receipt,
  validate_pickup_receipt,
  validate_transport_context,
 )
 value=context_v2();assert validate_transport_context(json.dumps(value))==value
 pickup=json.loads(pickup_receipt(value,picked_up=True,evidence_refs=['scan:all-loaded']));assert pickup['execution_plan_digest']==FIXTURE['execution_plan']['execution_plan_digest'];assert validate_pickup_receipt(pickup,value)==pickup
 delivered=json.loads(delivery_receipt(value,delivered=True,evidence_refs=['scan:all-unloaded']));assert delivered['execution_plan_digest']==pickup['execution_plan_digest'] and delivered['schema_version']==2

@pytest.mark.parametrize('change',['missing_plan','extra_container','changed_site','old_schema'])
def test_v2_transport_rejects_partial_or_replaced_placement_plan(change):
 from unilabos.center_protocol.transport import validate_transport_context
 value=context_v2()
 if change=='missing_plan':del value['placement_plan']
 elif change=='extra_container':value['container_uuids']=value['container_uuids'][:1]
 elif change=='changed_site':value['placement_plan']['prepared']['items'][0]['target_site_uuid']=value['placement_plan']['prepared']['items'][1]['target_site_uuid']
 else:value['schema_version']=1
 with pytest.raises(ValueError):validate_transport_context(value)

def test_output_placement_does_not_invent_a_physical_source_site():
    from copy import deepcopy

    from unilabos.center_protocol.placement import validate_item_placement, validate_transfer_selection
    value = deepcopy(FIXTURE['selection'])
    value['items'][0].pop('source_site_uuid')
    # 库存选择仍强制真实来源位；输出绑定可仅使用实际根物料身份。
    with pytest.raises(ValueError):
        validate_transfer_selection(value)
    validate_item_placement({'transfer_definition_uuid': FIXTURE['transfer_definition_uuid'],
        'selection_digest': FIXTURE['selection_digest'], 'selection': value['items'][0]})

@pytest.mark.parametrize('count',[0,1,100,101])
def test_placement_batch_size_boundaries(count):
 from uuid import uuid4
 value={'schema_version':1,'items':[]}
 for _ in range(count):
  item=copy.deepcopy(FIXTURE['selection']['items'][0])
  for key in ('selection_uuid','source_site_uuid','source_material_uuid','container_uuid','target_site_uuid'):item[key]=str(uuid4())
  value['items'].append(item)
 if count in (1,100):assert len(validate_transfer_selection(value)['items'])==count
 else:
  with pytest.raises(ValueError):validate_transfer_selection(value)
