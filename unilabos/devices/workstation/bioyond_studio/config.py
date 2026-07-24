# config.py
"""
配置文件 - 包含所有配置信息和映射关系
"""

# API配置
API_CONFIG = {
    "api_key": "",
    "api_host": ""
}

# 工作流映射配置
WORKFLOW_MAPPINGS = {
    "reactor_taken_out": "",
    "reactor_taken_in": "",
    "Solid_feeding_vials": "",
    "Liquid_feeding_vials(non-titration)": "",
    "Liquid_feeding_solvents": "",
    "Liquid_feeding(titration)": "",
    "liquid_feeding_beaker": "",
    "Drip_back": "",
}

# 工作流名称到DisplaySectionName的映射
WORKFLOW_TO_SECTION_MAP = {
    'reactor_taken_in': '反应器放入',
    'liquid_feeding_beaker': '液体投料-烧杯',
    'Liquid_feeding_vials(non-titration)': '液体投料-小瓶（非滴定）',
    'Liquid_feeding_solvents': '液体投料-溶剂',
    'Solid_feeding_vials': '固体投料-小瓶',
    'Liquid_feeding(titration)': '液体投料-滴定',
    'reactor_taken_out': '反应器取出'
}

# 库位映射配置
WAREHOUSE_MAPPING = {
    "粉末堆栈": {
        "uuid": "",
        "site_uuids": {
            # 样品板
            "A1": "3a14198e-6929-31f0-8a22-0f98f72260df",
            "A2": "3a14198e-6929-4379-affa-9a2935c17f99",
            "A3": "3a14198e-6929-56da-9a1c-7f5fbd4ae8af",
            "A4": "3a14198e-6929-5e99-2b79-80720f7cfb54",
            "B1": "3a14198e-6929-f525-9a1b-1857552b28ee",
            "B2": "3a14198e-6929-bf98-0fd5-26e1d68bf62d",
            "B3": "3a14198e-6929-2d86-a468-602175a2b5aa",
            "B4": "3a14198e-6929-1a98-ae57-e97660c489ad",
            # 分装板
            "C1": "3a14198e-6929-46fe-841e-03dd753f1e4a",
            "C2": "3a14198e-6929-1bc9-a9bd-3b7ca66e7f95",
            "C3": "3a14198e-6929-72ac-32ce-9b50245682b8",
            "C4": "3a14198e-6929-3bd8-e6c7-4a9fd93be118",
            "D1": "3a14198e-6929-8a0b-b686-6f4a2955c4e2",
            "D2": "3a14198e-6929-dde1-fc78-34a84b71afdf",
            "D3": "3a14198e-6929-a0ec-5f15-c0f9f339f963",
            "D4": "3a14198e-6929-7ac8-915a-fea51cb2e884"
        }
    },
    "溶液堆栈": {
        "uuid": "",
        "site_uuids": {
            "A1": "3a14198e-d724-e036-afdc-2ae39a7f3383",
            "A2": "3a14198e-d724-afa4-fc82-0ac8a9016791",
            "A3": "3a14198e-d724-ca48-bb9e-7e85751e55b6",
            "A4": "3a14198e-d724-df6d-5e32-5483b3cab583",
            "B1": "3a14198e-d724-d818-6d4f-5725191a24b5",
            "B2": "3a14198e-d724-be8a-5e0b-012675e195c6",
            "B3": "3a14198e-d724-cc1e-5c2c-228a130f40a8",
            "B4": "3a14198e-d724-1e28-c885-574c3df468d0",
            "C1": "3a14198e-d724-b5bb-adf3-4c5a0da6fb31",
            "C2": "3a14198e-d724-ab4e-48cb-817c3c146707",
            "C3": "3a14198e-d724-7f18-1853-39d0c62e1d33",
            "C4": "3a14198e-d724-28a2-a760-baa896f46b66",
            "D1": "3a14198e-d724-d378-d266-2508a224a19f",
            "D2": "3a14198e-d724-f56e-468b-0110a8feb36a",
            "D3": "3a14198e-d724-0cf1-dea9-a1f40fe7e13c",
            "D4": "3a14198e-d724-0ddd-9654-f9352a421de9"
        }
    },
    "试剂堆栈": {
        "uuid": "",
        "site_uuids": {
            "A1": "3a14198c-c2cf-8b40-af28-b467808f1c36",
            "A2": "3a14198c-c2d0-f3e7-871a-e470d144296f",
            "A3": "3a14198c-c2d0-dc7d-b8d0-e1d88cee3094",
            "A4": "3a14198c-c2d0-2070-efc8-44e245f10c6f",
            "B1": "3a14198c-c2d0-354f-39ad-642e1a72fcb8",
            "B2": "3a14198c-c2d0-1559-105d-0ea30682cab4",
            "B3": "3a14198c-c2d0-725e-523d-34c037ac2440",
            "B4": "3a14198c-c2d0-efce-0939-69ca5a7dfd39"
        }
    }
}

# 物料类型配置
MATERIAL_TYPE_MAPPINGS = {
    "烧杯": ("BIOYOND_PolymerStation_1FlaskCarrier", "3a14196b-24f2-ca49-9081-0cab8021bf1a"),
    "试剂瓶": ("BIOYOND_PolymerStation_1BottleCarrier", ""),
    "样品板": ("BIOYOND_PolymerStation_6StockCarrier", "3a14196e-b7a0-a5da-1931-35f3000281e9"),
    "分装板": ("BIOYOND_PolymerStation_6VialCarrier", "3a14196e-5dfe-6e21-0c79-fe2036d052c4"),
    "样品瓶": ("BIOYOND_PolymerStation_Solid_Stock", "3a14196a-cf7d-8aea-48d8-b9662c7dba94"),
    "90%分装小瓶": ("BIOYOND_PolymerStation_Solid_Vial", "3a14196c-cdcf-088d-dc7d-5cf38f0ad9ea"),
    "10%分装小瓶": ("BIOYOND_PolymerStation_Liquid_Vial", "3a14196c-76be-2279-4e22-7310d69aed68"),
}

# 步骤参数配置（各工作流的步骤UUID）
WORKFLOW_STEP_IDS = {
    "reactor_taken_in": {
        "config": ""
    },
    "liquid_feeding_beaker": {
        "liquid": "",
        "observe": ""
    },
    "liquid_feeding_vials_non_titration": {
        "liquid": "",
        "observe": ""
    },
    "liquid_feeding_solvents": {
        "liquid": "",
        "observe": ""
    },
    "solid_feeding_vials": {
        "feeding": "",
        "observe": ""
    },
    "liquid_feeding_titration": {
        "liquid": "",
        "observe": ""
    },
    "drip_back": {
        "liquid": "",
        "observe": ""
    }
}

LOCATION_MAPPING = {}

ACTION_NAMES = {}

HTTP_SERVICE_CONFIG = {}