"""精确收货位优先于旧候选配置，失败不能偷偷换位。"""
import json
from copy import deepcopy
from uuid import uuid4

import pytest

from tests.app.placement_test_support import inventory_rows
from tests.app.test_station_handoff import package, setup  # noqa: F401
from unilabos.app.scheduler.inventory.handoff import StationHandoffAuthority
from unilabos.app.scheduler.inventory.ingress import IngressReservationError
from unilabos.center_protocol.placement import selection_digest


def placement(s,target=None):
 tid=str(uuid4());selection={'schema_version':1,'items':[{'selection_uuid':str(uuid4()),'source_site_uuid':s['sites']['source'],'source_material_uuid':s['carrier']['uuid'],'source_material_revision':s['carrier']['revision'],'container_uuid':s['ids']['container'],'expected_container_state_version':1,'target_site_uuid':target or s['sites']['target'],'target_resource_template_uuid':s['templates']['target']['test.carrier']}]}
 s['payload'].update(global_task_uuid=str(uuid4()),transfer_definition_uuid=tid,transfer_source_kind='station_material')
 s['payload']['items'][0]['placement_plan']={'transfer_definition_uuid':tid,'selection_digest':selection_digest(selection,tid),'selection':selection['items'][0]}
 return selection

def extra_site(s,occupied=None):
 sid=str(uuid4())
 with s['stores']['target'].transaction()as c:
  owner=c.execute('SELECT material_uuid FROM site WHERE uuid=?',(s['sites']['target'],)).fetchone()[0]
  c.execute("INSERT INTO site(uuid,create_time,update_time,material_uuid,name,allowed_resource_template_uuids,occupied_material_uuid) VALUES (?,'2026-01-01','2026-01-01',?,'chosen',?,?)",(sid,owner,json.dumps([s['templates']['target']['test.carrier']]),occupied))
 return sid

def test_preselected_target_is_reserved_without_prompt_or_fallback(setup):
 s=setup;chosen=extra_site(s);selection=placement(s,chosen)
 s['plans'][s['ids']['item']]['candidate_site_uuids']=[s['sites']['target'],chosen]
 event=s['auth']['target'].prepare_incoming(package(s['payload']),container_details=s['details'],plans=s['plans'])
 proof=event['payload']['placement_reservations'];assert proof['selection_digest']==selection_digest(selection,s['payload']['transfer_definition_uuid'])
 assert proof['items'][0]['target_site_uuid']==chosen
 with s['stores']['target'].read_connection()as c:
  row=c.execute('SELECT site_uuid FROM station_ingress_reservation WHERE site_active=1').fetchone();assert row[0]==chosen
 original=deepcopy(event)
 # 重建本地权威模拟重启，仍复用同一位置与 prepared intent。
 authority=StationHandoffAuthority(s['stores']['target'],station_uuid=s['ids']['target'],instance_uuid=s['ids']['target_instance'],event_epoch_uuid=s['ids']['target_epoch'],lab_uuid=s['ids']['lab'],now_ms=lambda:s['clock'][0])
 assert authority.prepare_incoming(package(s['payload']),container_details=s['details'],plans=s['plans'])==original

def test_occupied_selected_target_rolls_back_incoming_and_does_not_choose_other_empty_site(setup):
 s=setup;selected=extra_site(s);placement(s,selected)
 # 真实目标库存占据指定位置，旧默认入口仍为空。
 from unilabos.app.scheduler.inventory.backend_contract import BackendResourceService
 backend=BackendResourceService(s['stores']['target']);backend.create_material({'resource_template_uuid':s['templates']['target']['test.carrier'],'barcode':'existing','name':'existing target','site_placement':{'action':'place','site_uuid':selected}})
 s['plans'][s['ids']['item']]['candidate_site_uuids']=[s['sites']['target'],selected]
 before=inventory_rows(s['stores']['target'])
 with pytest.raises(IngressReservationError):s['auth']['target'].prepare_incoming(package(s['payload']),container_details=s['details'],plans=s['plans'])
 assert inventory_rows(s['stores']['target'])==before
 assert not s['auth']['target'].pending_intents()

def test_planned_handoff_prepares_without_user_resubmitting_receiver_position(setup):
 from types import SimpleNamespace

 from unilabos.app.center_client.handoff import HandoffBridge
 s=setup;placement(s)
 class Center:
  def handoff_package(self,*args):return package(s['payload'])
  def container(self,identity,*args,**kwargs):return s['details'][identity]
 store=SimpleNamespace(station_uuid=s['ids']['target'],instance_uuid=s['ids']['target_instance'],event_epoch_uuid=s['ids']['target_epoch'])
 bridge=HandoffBridge(Center(),store,s['auth']['target'])
 packed=package(s['payload']);notice={'command':{'type':'handoff.available','payload':{'handoff_uuid':s['ids']['handoff'],'handoff_revision':1,'handoff_package_path':f"/api/v1/handoffs/{s['ids']['handoff']}/packages/1",'payload_digest':packed['payload_digest']}}}
 assert bridge.handle_command(notice) is True
 intents=s['auth']['target'].pending_intents();assert len(intents)==1
 assert intents[0]['payload']['placement_reservations']['items'][0]['target_site_uuid']==s['sites']['target']

def second_item(s,selection,*,target=None):
 old=s['payload']['items'][0]
 item=deepcopy(old);item['handoff_item_uuid']=str(uuid4());item['container_uuid']=str(uuid4())
 item['source_material_refs'][0]['local_material_uuid']=str(uuid4());item['source_material_refs'][0]['material_lineage_uuid']=str(uuid4())
 selected={**selection['items'][0],'selection_uuid':str(uuid4()),'source_site_uuid':str(uuid4()),'source_material_uuid':item['source_material_refs'][0]['local_material_uuid'],'container_uuid':item['container_uuid'],'target_site_uuid':target or extra_site(s)}
 selection['items'].append(selected)
 item['placement_plan']['selection']=selected
 s['payload']['items'].append(item)
 for row in s['payload']['items']:row['placement_plan']['selection_digest']=selection_digest(selection,s['payload']['transfer_definition_uuid'])
 s['plans'][item['handoff_item_uuid']]=deepcopy(s['plans'][old['handoff_item_uuid']]);s['plans'][item['handoff_item_uuid']]['carrier_lineage_uuid']=item['source_material_refs'][0]['material_lineage_uuid'];s['plans'][item['handoff_item_uuid']]['lineage_template_uuids']={item['source_material_refs'][0]['material_lineage_uuid']:s['templates']['target']['test.carrier']}
 detail=deepcopy(s['details'][old['container_uuid']]);detail.update(container_uuid=item['container_uuid'],barcode='BKR-2',active_handoff_item_uuid=item['handoff_item_uuid']);s['details'][item['container_uuid']]=detail
 return item


def test_two_items_keep_pairing_when_rows_reordered_and_restart(setup):
 s=setup;selected=placement(s);second_item(s,selected)
 s['payload']['items'].reverse()
 event=s['auth']['target'].prepare_incoming(package(s['payload']),container_details=s['details'],plans=s['plans'])
 proof=event['payload']['placement_reservations'];assert len(proof['items'])==2
 assert {(r['selection_uuid'],r['target_site_uuid'])for r in proof['items']}=={(r['selection_uuid'],r['target_site_uuid'])for r in selected['items']}
 authority=StationHandoffAuthority(s['stores']['target'],station_uuid=s['ids']['target'],instance_uuid=s['ids']['target_instance'],event_epoch_uuid=s['ids']['target_epoch'],lab_uuid=s['ids']['lab'],now_ms=lambda:s['clock'][0])
 assert authority.prepare_incoming(package(s['payload']),container_details=s['details'],plans=s['plans'])==event


def test_last_item_failure_rolls_back_entire_batch(setup):
 from unilabos.app.scheduler.inventory.backend_contract import BackendResourceService
 s=setup;selected=placement(s);last=second_item(s,selected)
 target=last['placement_plan']['selection']['target_site_uuid']
 BackendResourceService(s['stores']['target']).create_material({'resource_template_uuid':s['templates']['target']['test.carrier'],'barcode':'occupied-last','name':'occupied','site_placement':{'action':'place','site_uuid':target}})
 before=inventory_rows(s['stores']['target'])
 with pytest.raises(IngressReservationError):s['auth']['target'].prepare_incoming(package(s['payload']),container_details=s['details'],plans=s['plans'])
 assert inventory_rows(s['stores']['target'])==before

def test_pickup_cannot_replace_prepared_execution_plan(setup):
 from unilabos.center_protocol.digest import digest
 from unilabos.center_protocol.handoff import HandoffProtocolError, validate_handoff_snapshot
 from unilabos.center_protocol.placement import execution_from_handoff
 s=setup;placement(s);event=s['auth']['target'].prepare_incoming(package(s['payload']),container_details=s['details'],plans=s['plans'])
 p=s['payload'];p.update(status='prepared',revision=2,prepared={'event_epoch_uuid':s['ids']['target_epoch'],**{k:v for k,v in event['payload'].items()if k in {'protection_uuid','reservation_until_ms','evidence_refs','placement_reservations'}}})
 plan=execution_from_handoff(p)
 permit={'authorization_uuid':str(uuid4()),'logistics_subject_uuid':str(uuid4())};p.update(status='in_transit',revision=4,permit=permit)
 p['pickup']={'schema_version':2,'execution_plan_digest':plan['execution_plan_digest'],'event_uuid':str(uuid4()),'event_digest':digest({'event':1}),'station_invocation_uuid':str(uuid4()),'logistics_station_uuid':permit['logistics_subject_uuid'],'instance_uuid':str(uuid4()),'local_task_uuid':str(uuid4()),'task_payload_digest':digest({'task':1}),'handoff_uuid':p['handoff_uuid'],'authorization_uuid':permit['authorization_uuid'],'vehicle_id':'agv-1','container_uuids':[s['ids']['container']],'job_uuid':str(uuid4()),'node_uuid':str(uuid4()),'picked_up_at_ms':s['clock'][0],'evidence_refs':['load:actual']}
 validate_handoff_snapshot(p)
 p['pickup']['execution_plan_digest']=digest({'wrong':1})
 with pytest.raises(HandoffProtocolError):validate_handoff_snapshot(p)


def test_automatic_preparation_uses_the_real_inventory_adapter(setup):
 from types import SimpleNamespace

 from unilabos.app.center_client.handoff import HandoffBridge
 from unilabos.app.scheduler.inventory.handoff_adapter import inventory_snapshot
 s=setup;placement(s)
 with s['stores']['source'].read_connection()as c:
  s['payload']['items'][0]['source_snapshot']=inventory_snapshot(s['auth']['source'],c,s['carrier']['uuid'])
 class Center:
  def handoff_package(self,*args):return package(s['payload'])
  def container(self,identity,*args,**kwargs):return s['details'][identity]
 store=SimpleNamespace(station_uuid=s['ids']['target'],instance_uuid=s['ids']['target_instance'],event_epoch_uuid=s['ids']['target_epoch'])
 bridge=HandoffBridge(Center(),store,s['auth']['target'])
 packed=package(s['payload']);notice={'command':{'type':'handoff.available','payload':{'handoff_uuid':s['ids']['handoff'],'handoff_revision':1,'handoff_package_path':f"/api/v1/handoffs/{s['ids']['handoff']}/packages/1",'payload_digest':packed['payload_digest']}}}
 assert bridge.handle_command(notice) is True

def test_partial_receipt_restart_and_late_cancel_keep_received_fact_and_remaining_protection(setup):
 from unilabos.center_protocol.digest import digest
 s=setup;selected=placement(s);last=second_item(s,selected)
 event=s['auth']['target'].prepare_incoming(package(s['payload']),container_details=s['details'],plans=s['plans'])
 p=s['payload'];p.update(status='in_transit',revision=4,prepared={'event_epoch_uuid':s['ids']['target_epoch'],**{k:v for k,v in event['payload'].items()if k in {'protection_uuid','reservation_until_ms','evidence_refs','placement_reservations'}}},released={'event_epoch_uuid':s['ids']['source_epoch'],'release_uuid':str(uuid4()),'evidence_refs':['source:ready']},permit={'authorization_uuid':str(uuid4()),'logistics_subject_uuid':str(uuid4())})
 authority=s['auth']['target'];authority.apply_center_snapshot(package(p))
 receipt=authority.receive(s['ids']['handoff'],s['ids']['item'],barcode_namespace='lab',barcode='BKR-1',evidence_refs=['scan:first'])
 authority=StationHandoffAuthority(s['stores']['target'],station_uuid=s['ids']['target'],instance_uuid=s['ids']['target_instance'],event_epoch_uuid=s['ids']['target_epoch'],lab_uuid=s['ids']['lab'],now_ms=lambda:s['clock'][0])
 p.update(status='canceling',revision=5);authority.apply_center_snapshot(package(p))
 first=s['stores']['target'].query_one('SELECT * FROM station_handoff_item WHERE handoff_item_uuid=?',(s['ids']['item'],))
 remaining=s['stores']['target'].query_one('SELECT * FROM station_handoff_item WHERE handoff_item_uuid=?',(last['handoff_item_uuid'],))
 assert digest(json.loads(first['receipt_json']))==digest(receipt)
 assert authority.ingress.get(remaining['reservation_uuid'])['state']=='reserved'
 assert all(intent['payload']['kind']!='cancel_ack'for intent in authority.pending_intents())

def claim_source_location(s,scope='material_site'):
 from unilabos.app.scheduler.inventory.dispatch_admission import (
  DispatchAdmissionRequest,
  DispatchResource,
  acquire_dispatch_permit,
 )
 from unilabos.app.scheduler.resource_lock import device_lock_key, site_lock_key
 with s['stores']['source'].transaction()as c:
  owner=c.execute('SELECT material_uuid FROM site WHERE uuid=?',(s['sites']['source'],)).fetchone()[0]
  resource=DispatchResource(lock_key=site_lock_key(owner,s['sites']['source'])if scope=='material_site'else device_lock_key(owner),scope=scope,material_uuid=owner,site_uuid=s['sites']['source']if scope=='material_site'else '')
  return acquire_dispatch_permit(c,DispatchAdmissionRequest(effect_uuid=str(uuid4()),task_uuid=str(uuid4()),job_uuid=str(uuid4()),attempt=1,parameter_hash='source-site-test',expected_change_set={},resources=(resource,)))

def prepare_planned(s):
 event=s['auth']['target'].prepare_incoming(package(s['payload']),container_details=s['details'],plans=s['plans'])
 s['payload'].update(status='prepared',revision=2,prepared={'event_epoch_uuid':s['ids']['target_epoch'],**{k:v for k,v in event['payload'].items()if k in {'protection_uuid','reservation_until_ms','evidence_refs','placement_reservations'}}})

@pytest.mark.parametrize('scope',['material_site','device'])
def test_source_release_rechecks_site_and_owner_job_claim_after_preflight(setup,scope):
 from unilabos.app.scheduler.inventory.handoff import HandoffInventoryError
 s=setup;source_rack_is_device(s);placement(s);prepare_planned(s)
 assert claim_source_location(s,scope).acquired
 before=inventory_rows(s['stores']['source'])
 with pytest.raises((ValueError,HandoffInventoryError)):
  s['auth']['source'].freeze_source(package(s['payload']),physical_ready=True,evidence_refs=['operator:ready'])
 assert inventory_rows(s['stores']['source'])==before

@pytest.mark.parametrize('scope',['material_site','device'])
def test_released_source_location_stays_protected_until_trusted_pickup(setup,scope):
 s=setup;source_rack_is_device(s);placement(s);prepare_planned(s)
 s['auth']['source'].freeze_source(package(s['payload']),physical_ready=True,evidence_refs=['operator:ready'])
 assert not claim_source_location(s,scope).acquired


def source_rack_is_device(s):
 # 在测试库存夹具中表达真实设备所属库位，避免把普通货架当设备 Claim。
 with s['stores']['source'].transaction()as c:
  owner=c.execute('SELECT material_uuid FROM site WHERE uuid=?',(s['sites']['source'],)).fetchone()[0]
  c.execute("UPDATE material SET type='device',class='device' WHERE uuid=?",(owner,))

def test_trusted_pickup_releases_only_source_location_and_late_snapshot_does_not_relock(setup):
 from unilabos.center_protocol.digest import digest
 from unilabos.center_protocol.placement import execution_from_handoff
 s=setup;source_rack_is_device(s);placement(s);prepare_planned(s)
 released=s['auth']['source'].freeze_source(package(s['payload']),physical_ready=True,evidence_refs=['operator:ready'])
 p=s['payload'];p.update(status='in_transit',revision=4,released={'event_epoch_uuid':s['ids']['source_epoch'],**{k:v for k,v in released['payload'].items()if k in {'release_uuid','evidence_refs'}}},permit={'authorization_uuid':str(uuid4()),'logistics_subject_uuid':str(uuid4())})
 execution=execution_from_handoff(p)
 p['pickup']={'schema_version':2,'execution_plan_digest':execution['execution_plan_digest'],'event_uuid':str(uuid4()),'event_digest':digest({'pickup':1}),'station_invocation_uuid':str(uuid4()),'logistics_station_uuid':p['permit']['logistics_subject_uuid'],'instance_uuid':str(uuid4()),'local_task_uuid':str(uuid4()),'task_payload_digest':digest({'task':1}),'handoff_uuid':p['handoff_uuid'],'authorization_uuid':p['permit']['authorization_uuid'],'vehicle_id':'agv-1','container_uuids':[s['ids']['container']],'job_uuid':str(uuid4()),'node_uuid':str(uuid4()),'picked_up_at_ms':s['clock'][0],'evidence_refs':['loaded:actual']}
 s['auth']['source'].apply_center_snapshot(package(p))
 from unilabos.app.scheduler.inventory.handoff_pickup import source_location_holds
 with s['stores']['source'].read_connection()as c:assert source_location_holds(c)==[]
 p.update(attention=True,revision=5,attention_reasons=['late-check'])
 s['auth']['source'].apply_center_snapshot(package(p))
 with s['stores']['source'].read_connection()as c:assert source_location_holds(c)==[]
 assert claim_source_location(s).acquired

def test_concurrent_handoffs_compete_for_exact_target_without_fallback(setup):
 from concurrent.futures import ThreadPoolExecutor

 from unilabos.center_protocol.placement import selection_digest
 s=setup;placement(s)
 first=deepcopy(s['payload']);second=deepcopy(first)
 second.update(handoff_uuid=str(uuid4()),global_task_uuid=str(uuid4()),transfer_definition_uuid=str(uuid4()))
 item=second['items'][0];old_item=item['handoff_item_uuid'];old_container=item['container_uuid']
 item.update(handoff_item_uuid=str(uuid4()),container_uuid=str(uuid4()))
 item['source_material_refs'][0].update(local_material_uuid=str(uuid4()),material_lineage_uuid=str(uuid4()))
 selected=item['placement_plan']['selection'];selected.update(selection_uuid=str(uuid4()),source_site_uuid=str(uuid4()),source_material_uuid=item['source_material_refs'][0]['local_material_uuid'],container_uuid=item['container_uuid'])
 item['placement_plan'].update(transfer_definition_uuid=second['transfer_definition_uuid'],selection_digest=selection_digest({'schema_version':1,'items':[selected]},second['transfer_definition_uuid']))
 details=deepcopy(s['details']);detail=details.pop(old_container);detail.update(container_uuid=item['container_uuid'],barcode='BKR-2',active_handoff_item_uuid=item['handoff_item_uuid']);details[item['container_uuid']]=detail
 plans={item['handoff_item_uuid']:deepcopy(s['plans'][old_item])};plan=plans[item['handoff_item_uuid']];plan['carrier_lineage_uuid']=item['source_material_refs'][0]['material_lineage_uuid'];plan['lineage_template_uuids']={plan['carrier_lineage_uuid']:plan['carrier_template_uuid']}
 # 两个独立连接/权威并发，不能靠同一个 Store 的进程内锁证明互斥。
 from unilabos.app.scheduler.inventory.store import InventoryStore
 factory=s.get('open_inventory',InventoryStore)
 competing_store=factory(s['stores']['target'].path)
 competing=StationHandoffAuthority(competing_store,station_uuid=s['ids']['target'],instance_uuid=s['ids']['target_instance'],event_epoch_uuid=s['ids']['target_epoch'],lab_uuid=s['ids']['lab'],now_ms=lambda:s['clock'][0])
 def prepare(args):
  authority,payload,local_details,local_plans=args
  try:return authority.prepare_incoming(package(payload),container_details=local_details,plans=local_plans)
  except IngressReservationError:return None
 try:
  with ThreadPoolExecutor(max_workers=2)as pool:
   results=list(pool.map(prepare,[(s['auth']['target'],first,s['details'],s['plans']),(competing,second,details,plans)]))
 finally:competing_store.close()
 assert sum(row is not None for row in results)==1
 with s['stores']['target'].read_connection()as c:
  assert c.execute('SELECT COUNT(*) FROM station_ingress_reservation WHERE site_active=1').fetchone()[0]==1
