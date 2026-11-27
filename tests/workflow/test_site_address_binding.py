"""任务创建时把库位地址换成物理库位：成对参数只认合同声明，调度拿到的已是载架和载架库位。"""
from unilabos.workflow.execution_plan import ExecutionPlanBuilder
from unilabos.workflow.site_address_binding import bind_site_addresses

DECK, NEST, STACK, CASSETTE = "11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222", \
    "33333333-3333-4333-8333-333333333333", "44444444-4444-4444-8444-444444444444"
SITE_UUID = "55555555-5555-4555-8555-555555555555"
TABLE = {(DECK, "position_x23_y01"): {"owner_material_uuid": NEST, "site_uuid": "s23", "site_name": "Nest61mm_Pos:3"},
         (DECK, "position_x15_y01"): {"owner_material_uuid": NEST, "site_uuid": "s15", "site_name": "Nest61mm_Pos:1"},
         (STACK, "cassette_07_layer_04"): {"owner_material_uuid": CASSETTE, "site_uuid": "c74", "site_name": "level_4"}}


def _template(uuid, resource_contract):
    return {"uuid": uuid, "meta_data": {"unilab": {"action_contract_schema": {
        "x-unilabos-action-contract": {"resource_contract": resource_contract}}}}}


TRANSFER = _template("t-transfer", {"transfer": {
    "material_param": "material", "source_owner_param": "source_owner", "source_site_name_param": "source_site",
    "target_owner_param": "target_owner", "target_site_name_param": "target_site"}})
STEP = _template("t-step", {"transfer_step": {"operation": "place", "material_param": "material",
                                             "owner_param": "owner", "site_param": "site", "carrier_params": ["gripper"]}})
PLAIN = _template("t-plain", {})


def _graph():
    return {"node_templates": [TRANSFER, STEP, PLAIN], "nodes": [
        {"uuid": "move", "workflow_node_template_uuid": "t-transfer", "param": {
            "material": {"uuid": "m"}, "source_owner": {"uuid": DECK}, "source_site": "position_x23_y01",
            "target_owner": {"uuid": DECK}, "target_site": "position_x15_y01", "sourcePosition": 23, "targetPosition": 15}},
        {"uuid": "place", "workflow_node_template_uuid": "t-step", "param": {
            "material": {"uuid": "m"}, "owner": {"uuid": STACK}, "site": "cassette_07_layer_04", "gripper": {"uuid": "g"}}},
        {"uuid": "source", "param": {"mode": "existing", "mount": {"uuid": DECK}, "site": "position_x23_y01"}},
        {"uuid": "physical", "workflow_node_template_uuid": "t-transfer", "param": {
            "source_owner": {"uuid": NEST}, "source_site": "Nest61mm_Pos:3", "target_owner": {"uuid": DECK}, "target_site": SITE_UUID}},
        {"uuid": "unrelated", "workflow_node_template_uuid": "t-plain", "param": {
            "source_owner": {"uuid": DECK}, "source_site": "position_x23_y01"}},
        {"uuid": "dynamic", "workflow_node_template_uuid": "t-step", "param": {"site": "position_x23_y01"}},
    ]}


def _bind(graph):
    return bind_site_addresses(graph, lambda owner, name: TABLE.get((owner, name)),
                               ExecutionPlanBuilder._frozen_action_resource_contract)


def test_declared_pairs_become_the_carrier_and_its_site():
    graph = _graph()
    bound = _bind(graph)
    nodes = {node["uuid"]: node["param"] for node in graph["nodes"]}
    # 搬运两端：所属物料变成载架；库位名保留作者写的地址交给驱动（库存按“载架 + 地址”反查），位置号不动。
    assert nodes["move"] == {"material": {"uuid": "m"}, "source_owner": {"uuid": NEST}, "source_site": "position_x23_y01",
                             "target_owner": {"uuid": NEST}, "target_site": "position_x15_y01",
                             "sourcePosition": 23, "targetPosition": 15}
    # 堆栈同一套：料架号 + 层号是地址，物理库位在料架上。
    assert (nodes["place"]["owner"], nodes["place"]["site"]) == ({"uuid": CASSETTE}, "cassette_07_layer_04")
    # 物料来源不进驱动：库位名一并换成载架库位名。
    assert (nodes["source"]["mount"], nodes["source"]["site"]) == ({"uuid": NEST}, "Nest61mm_Pos:3")
    assert [(item["node_uuid"], item["address"], item["site_uuid"]) for item in bound] == [
        ("move", "position_x23_y01", "s23"), ("move", "position_x15_y01", "s15"),
        ("place", "cassette_07_layer_04", "c74"), ("source", "position_x23_y01", "s23")]
    assert all(item["address_owner_material_uuid"] in {DECK, STACK} for item in bound)


def test_what_is_not_a_declared_address_pair_is_left_alone():
    graph = _graph()
    before = {node["uuid"]: dict(node["param"]) for node in graph["nodes"]}
    _bind(graph)
    after = {node["uuid"]: node["param"] for node in graph["nodes"]}
    assert after["physical"] == before["physical"]      # 已是物理库位名；库位 UUID 也不动
    assert after["unrelated"] == before["unrelated"]    # 参数名像，但合同没有声明成对
    assert after["dynamic"] == before["dynamic"]        # 所属物料来自上游输出，创建任务时没有值
    # 没有地址表的站：什么都不换。
    untouched = _graph()
    assert bind_site_addresses(untouched, lambda owner, name: None, ExecutionPlanBuilder._frozen_action_resource_contract) == []
    assert untouched == _graph()


def test_station_contract_binding_translates_through_the_inventory_resolver():
    """任务创建补齐身份时一并翻译；工作流合同里的写法不变，翻译记录留在任务自己的图上。"""
    from unilabos.workflow.station_contract import bind_station_contract

    def resolver(resource_id):
        return None

    resolver.resolve_site_address = lambda owner, name: TABLE.get((owner, name))
    contract = {"graph_snapshot": {**_graph(), "workflow": {}}, "executor_requirements": [], "executor_binding_mapping": {}}
    graph = bind_station_contract(contract, resolver)
    assert {node["uuid"]: node["param"] for node in graph["nodes"]}["move"]["target_owner"] == {"uuid": NEST}
    assert len(graph["workflow"]["meta_data"]["site_address_bindings"]) == 4
    assert contract["graph_snapshot"]["nodes"][0]["param"]["target_owner"] == {"uuid": DECK}     # 合同本身没被改
    # 没有地址能力的解析器（旧组合根、纯编译）：行为与之前完全一样。
    plain = bind_station_contract(contract, lambda resource_id: None)
    assert plain["nodes"] == contract["graph_snapshot"]["nodes"] and "meta_data" not in plain["workflow"]


def test_plan_and_jobs_are_translated_after_inputs_and_composites_are_in():
    """地址来自任务输入（在作业参数里）或组合调用展开（在计划节点参数里）时，计划和作业一起换。"""
    from unilabos.workflow.site_address_binding import bind_plan_site_addresses

    transfer = TRANSFER["meta_data"]["unilab"]["action_contract_schema"]["x-unilabos-action-contract"]["resource_contract"]
    plan = {"nodes": [
        # 所属物料写在节点上，层号来自任务输入（只在作业参数里）。
        {"uuid": "fetch", "kind": "device_action", "action_resource_contract": transfer,
         "param": {"source_owner": {"uuid": STACK}, "target_owner": {"uuid": NEST}, "target_site": "Nest61mm_Pos:1"}},
        # 组合调用展开进来的参数：都在计划节点上。
        {"uuid": "inner", "kind": "device_action", "action_resource_contract": transfer,
         "param": {"source_owner": {"uuid": NEST}, "source_site": "Nest61mm_Pos:1",
                   "target_owner": {"uuid": DECK}, "target_site": "position_x15_y01"}},
        {"uuid": "source", "kind": "material_source", "action_resource_contract": {},
         "param": {"mount": {"uuid": DECK}, "site": "position_x23_y01"}},
    ]}
    jobs = [{"workflow_node_uuid": "fetch", "param": {"source_site": "cassette_07_layer_04", "posX": 4, "posY": 7}},
            {"workflow_node_uuid": "inner", "param": {}}]

    def resolver(resource_id):
        return None

    resolver.resolve_site_address = lambda owner, name: TABLE.get((owner, name))
    bound = bind_plan_site_addresses(plan, jobs, resolver)
    nodes = {node["uuid"]: node["param"] for node in plan["nodes"]}
    # 堆栈：料架是所属物料；作业和计划两处一致，厂家用的层号、料架号不动。
    assert nodes["fetch"]["source_owner"] == {"uuid": CASSETTE} and nodes["fetch"]["source_site"] == "cassette_07_layer_04"
    assert jobs[0]["param"] == {"source_site": "cassette_07_layer_04", "source_owner": {"uuid": CASSETTE}, "posX": 4, "posY": 7}
    assert (nodes["inner"]["target_owner"], nodes["inner"]["target_site"]) == ({"uuid": NEST}, "position_x15_y01")
    assert jobs[1]["param"] == {}                                   # 作业里没有这对参数就不凭空加
    assert (nodes["source"]["mount"], nodes["source"]["site"]) == ({"uuid": NEST}, "Nest61mm_Pos:3")
    assert [item["address"] for item in bound] == ["cassette_07_layer_04", "position_x15_y01", "position_x23_y01"]
    assert bind_plan_site_addresses(plan, jobs, resolver) == []     # 再跑一遍不再变
    assert bind_plan_site_addresses(plan, jobs, lambda resource_id: None) == []


def test_material_source_site_binding_reference_follows_the_translated_site():
    """物料来源的启动库位绑定（meta_data 里另存的库位名）跟着 mount/site 一起换成载架库位。"""
    from uuid import uuid4

    from unilabos.workflow.site_address_binding import bind_site_addresses

    hotel, rack, site_uuid = str(uuid4()), str(uuid4()), str(uuid4())
    node = {"uuid": str(uuid4()), "param": {"mount": {"uuid": hotel}, "site": "cassette_03_layer_02"},
            "meta_data": {"unilab": {"material_source_site_binding": {"reference": "cassette_03_layer_02"}}}}
    graph = {"nodes": [node], "node_templates": []}

    def resolve(owner, name):
        if (owner, name) == (hotel, "cassette_03_layer_02"):
            return {"owner_material_uuid": rack, "site_uuid": site_uuid, "site_name": "level_2"}
        return None

    bound = bind_site_addresses(graph, resolve, lambda template: {})
    assert len(bound) == 1
    assert node["param"] == {"mount": {"uuid": rack}, "site": "level_2"}
    assert node["meta_data"]["unilab"]["material_source_site_binding"]["reference"] == "level_2"
