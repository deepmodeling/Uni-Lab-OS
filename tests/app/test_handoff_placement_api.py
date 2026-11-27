"""提前选择目标位只验证真实库存，不预建收货和占位。"""
import json
from dataclasses import asdict
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI, Header, HTTPException
from fastapi.testclient import TestClient

from tests.app.placement_test_support import inventory_rows
from tests.app.test_station_handoff import setup  # noqa: F401
from unilabos.app.center_client.client import ControlSession
from unilabos.app.center_client.handoff import HandoffBridge
from unilabos.app.scheduler.inventory.handoff_api import create_handoff_router
from unilabos.app.scheduler.inventory.placement import validate_inventory_placement


def request(s,role='target',destinations=False):
 item={'selection_uuid':str(uuid4()),'source_site_uuid':s['sites']['source'],'source_material_uuid':s['carrier']['uuid'],'source_material_revision':s['carrier']['revision'],'container_uuid':s['ids']['container'],'expected_container_state_version':1,'target_site_uuid':s['sites']['target'],'target_resource_template_uuid':s['templates']['target']['test.carrier']}
 body={'schema_version':1,'role':role,'source_station_uuid':s['ids']['source'],'target_station_uuid':s['ids']['target'],'transfer_definition_uuid':str(uuid4()),'selection':{'schema_version':1,'items':[item]}}
 if destinations:
  body['destinations']={'schema_version':1,'items':[{k:item[k]for k in ['selection_uuid','target_site_uuid','target_resource_template_uuid']}|{'source_port':'sample'}]};del body['selection']
 return body

def session(s,role):return ControlSession(str(uuid4()),str(uuid4()),1,s['ids'][role],s['ids'][role+'_instance'])
def business_rows(s):
 return inventory_rows(s['stores']['target'])

def test_target_can_be_selected_before_source_workflow_has_produced_material(setup):
 s=setup;before=business_rows(s)
 result=validate_inventory_placement(s['auth']['target'],request(s,destinations=True),session=asdict(session(s,'target')))
 assert result['can_run'] is True and result['items'][0]['target_site_uuid']==s['sites']['target']
 assert business_rows(s)==before
 assert not s['auth']['target'].pending_intents()

def test_target_rejects_incompatible_template_and_reports_the_selection_row(setup):
 s=setup;body=request(s,destinations=True);body['destinations']['items'][0]['target_resource_template_uuid']=s['templates']['target']['test.rack']
 result=validate_inventory_placement(s['auth']['target'],body,session=asdict(session(s,'target')))
 assert result['can_run'] is False
 assert result['checks'][0]['selection_uuid']==body['destinations']['items'][0]['selection_uuid']
 assert result['checks'][0]['blocking'] is True

def test_validation_route_requires_operator_and_uses_its_own_session(setup):
 s=setup;current=session(s,'target');store=SimpleNamespace(station_uuid=s['ids']['target'],instance_uuid=s['ids']['target_instance'],event_epoch_uuid=s['ids']['target_epoch'],session=lambda:current)
 bridge=HandoffBridge(SimpleNamespace(),store,s['auth']['target'])
 def auth(authorization:str|None=Header(default=None)):
  if authorization!='Bearer operator':raise HTTPException(401,'需要操作者')
 app=FastAPI();app.include_router(create_handoff_router(bridge,operator_dependency=auth),prefix='/api/v1');client=TestClient(app);path='/api/v1/center-handoffs/transfer-plan/validate';body=request(s,destinations=True)
 assert client.post(path,json=body).status_code==401
 response=client.post(path,json=body,headers={'Authorization':'Bearer operator'});assert response.status_code==200,response.text
 assert response.json()['session_uuid']==current.session_uuid
 body['target_station_uuid']=s['ids']['source'];assert client.post(path,json=body,headers={'Authorization':'Bearer operator'}).status_code==422

def test_standard_center_inventory_router_exposes_the_same_read_only_validation(setup):
 from unilabos.app.center_client.inventory_api import create_router
 s=setup;current=session(s,'target');store=SimpleNamespace(station_uuid=s['ids']['target'],instance_uuid=s['ids']['target_instance'],event_epoch_uuid=s['ids']['target_epoch'],session=lambda:current)
 class AuthenticatedCenter:
  def authorize_transfer_plan(self,token,request_digest,event_epoch_uuid):
   assert token=='test-purpose-token'
 bridge=HandoffBridge(AuthenticatedCenter(),store,s['auth']['target']);runtime=SimpleNamespace(handoffs=bridge)
 app=FastAPI();app.include_router(create_router(lambda:runtime));client=TestClient(app)
 assert client.post('/api/v1/center-handoffs/transfer-plan/validate',json=request(s,destinations=True)).status_code==401
 response=client.post('/api/v1/center-handoffs/transfer-plan/validate',json=request(s,destinations=True),headers={'Authorization':'Bearer test-purpose-token'})
 assert response.status_code==200,response.text
 assert response.json()['data']['can_run'] is True
 assert not s['auth']['target'].pending_intents()

@pytest.mark.parametrize('role',['source','target'])
def test_operator_route_rejects_removed_inventory_selection(setup,role):
 s=setup;current=session(s,role)
 store=SimpleNamespace(station_uuid=s['ids'][role],instance_uuid=s['ids'][role+'_instance'],event_epoch_uuid=s['ids'][role+'_epoch'],session=lambda:current)
 bridge=HandoffBridge(SimpleNamespace(),store,s['auth'][role])
 app=FastAPI();app.include_router(create_handoff_router(bridge,operator_dependency=lambda:None),prefix='/api/v1')
 before=inventory_rows(s['stores'][role])
 response=TestClient(app).post('/api/v1/center-handoffs/transfer-plan/validate',json=request(s,role))
 assert response.status_code==422
 assert '库存直选已移除' in response.json()['detail']
 assert inventory_rows(s['stores'][role])==before
