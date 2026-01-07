# config.py
"""
配置文件 - 包含所有配置信息和映射关系
"""
import os

# ==================== API 基础配置 ====================
# BioyondCellWorkstation 默认配置（包含所有必需参数）
API_CONFIG = {
    # API 连接配置
    # 实机
    #"api_host": os.getenv("BIOYOND_API_HOST", "http://172.16.11.118:44389"),
    # 仿真机
    "api_host": os.getenv("BIOYOND_API_HOST", "http://172.16.11.219:44388"),
    "api_key": os.getenv("BIOYOND_API_KEY", "8A819E5C"),
    "timeout": int(os.getenv("BIOYOND_TIMEOUT", "30")),
    
    # 报送配置
    "report_token": os.getenv("BIOYOND_REPORT_TOKEN", "CHANGE_ME_TOKEN"),
    
    # HTTP 服务配置
    "HTTP_host": os.getenv("BIOYOND_HTTP_HOST", "172.16.11.206"),  # HTTP服务监听地址，监听计算机飞连ip地址
    "HTTP_port": int(os.getenv("BIOYOND_HTTP_PORT", "8080")),
    "debug_mode": False,# 调试模式
}

# 库位映射配置
WAREHOUSE_MAPPING = {
    "粉末加样头堆栈": {
        "uuid": "",
        "site_uuids": {
            "A01": "3a19da56-1379-ff7c-1745-07e200b44ce2",
            "B01": "3a19da56-1379-2424-d751-fe6e94cef938",
            "C01": "3a19da56-1379-271c-03e3-6bdb590e395e",
            "D01": "3a19da56-1379-277f-2b1b-0d11f7cf92c6",
            "E01": "3a19da56-1379-2f1c-a15b-e01db90eb39a",
            "F01": "3a19da56-1379-3fa1-846b-088158ac0b3d",
            "G01": "3a19da56-1379-5aeb-d0cd-d3b4609d66e1",
            "H01": "3a19da56-1379-6077-8258-bdc036870b78",
            "I01": "3a19da56-1379-863b-a120-f606baf04617",
            "J01": "3a19da56-1379-8a74-74e5-35a9b41d4fd5",
            "K01": "3a19da56-1379-b270-b7af-f18773918abe",
            "L01": "3a19da56-1379-ba54-6d78-fd770a671ffc",
            "M01": "3a19da56-1379-c22d-c96f-0ceb5eb54a04",
            "N01": "3a19da56-1379-d64e-c6c5-c72ea4829888",
            "O01": "3a19da56-1379-d887-1a3c-6f9cce90f90e",
            "P01": "3a19da56-1379-e77d-0e65-7463b238a3b9",
            "Q01": "3a19da56-1379-edf6-1472-802ddb628774",
            "R01": "3a19da56-1379-f281-0273-e0ef78f0fd97",
            "S01": "3a19da56-1379-f924-7f68-df1fa51489f4",
            "T01": "3a19da56-1379-ff7c-1745-07e200b44ce2"
        }
    },
    "配液站内试剂仓库": {
        "uuid": "",
        "site_uuids": {
            "A01": "3a19da43-57b5-294f-d663-154a1cc32270",
            "B01": "3a19da43-57b5-7394-5f49-54efe2c9bef2",
            "C01": "3a19da43-57b5-5e75-552f-8dbd0ad1075f",
            "A02": "3a19da43-57b5-8441-db94-b4d3875a4b6c",
            "B02": "3a19da43-57b5-3e41-c181-5119dddaf50c",
            "C02": "3a19da43-57b5-269b-282d-fba61fe8ce96",
            "A03": "3a19da43-57b5-7c1e-d02e-c40e8c33f8a1",
            "B03": "3a19da43-57b5-659f-621f-1dcf3f640363",
            "C03": "3a19da43-57b5-855a-6e71-f398e376dee1",
        }
    },
    "试剂替换仓库": {
        "uuid": "",
        "site_uuids": {
            "A01": "3a19da51-8f4e-30f3-ea08-4f8498e9b097",
            "B01": "3a19da51-8f4e-1da7-beb0-80a4a01e67a8",
            "C01": "3a19da51-8f4e-337d-2675-bfac46880b06",
            "D01": "3a19da51-8f4e-e514-b92c-9c44dc5e489d",
            "E01": "3a19da51-8f4e-22d1-dd5b-9774ddc80402",
            "F01": "3a19da51-8f4e-273a-4871-dff41c29bfd9",
            "G01": "3a19da51-8f4e-b32f-454f-74bc1a665653",
            "H01": "3a19da51-8f4e-8c93-68c9-0b4382320f59",
            "I01": "3a19da51-8f4e-360c-0149-291b47c6089b",
            "J01": "3a19da51-8f4e-4152-9bca-8d64df8c1af0"
        }
    },
    "自动堆栈-左": {
        "uuid": "",
        "site_uuids": {
            "A01": "3a19debc-84b5-4c1c-d3a1-26830cf273ff",
            "A02": "3a19debc-84b5-033b-b31f-6b87f7c2bf52",
            "B01": "3a19debc-84b5-3924-172f-719ab01b125c",
            "B02": "3a19debc-84b5-aad8-70c6-b8c6bb2d8750"
        }
    },
    "自动堆栈-右": {
        "uuid": "",
        "site_uuids": {
            "A01": "3a19debe-5200-7df2-1dd9-7d202f158864",
            "A02": "3a19debe-5200-573b-6120-8b51f50e1e50",
            "B01": "3a19debe-5200-7cd8-7666-851b0a97e309",
            "B02": "3a19debe-5200-e6d3-96a3-baa6e3d5e484"
        }
    },
    "手动堆栈": {
        "uuid": "",
        "site_uuids": {
            "A01": "3a19deae-2c7a-36f5-5e41-02c5b66feaea",
            "A02": "3a19deae-2c7a-dc6d-c41e-ef285d946cfe",
            "A03": "3a19deae-2c7a-5876-c454-6b7e224ca927",
            "B01": "3a19deae-2c7a-2426-6d71-e9de3cb158b1",
            "B02": "3a19deae-2c7a-79b0-5e44-efaafd1e4cf3",
            "B03": "3a19deae-2c7a-b9eb-f4e3-e308e0cf839a",
            "C01": "3a19deae-2c7a-32bc-768e-556647e292f3",
            "C02": "3a19deae-2c7a-e97a-8484-f5a4599447c4",
            "C03": "3a19deae-2c7a-3056-6504-10dc73fbc276",
            "D01": "3a19deae-2c7a-ffad-875e-8c4cda61d440",
            "D02": "3a19deae-2c7a-61be-601c-b6fb5610499a",
            "D03": "3a19deae-2c7a-c0f7-05a7-e3fe2491e560",
            "E01": "3a19deae-2c7a-a6f4-edd1-b436a7576363",
            "E02": "3a19deae-2c7a-4367-96dd-1ca2186f4910",
            "E03": "3a19deae-2c7a-b163-2219-23df15200311",
            "F01": "3a19deae-2c7a-d594-fd6a-0d20de3c7c4a",
            "F02": "3a19deae-2c7a-a194-ea63-8b342b8d8679",
            "F03": "3a19deae-2c7a-f7c4-12bd-425799425698",
            "G01": "3a19deae-2c7a-0b56-72f1-8ab86e53b955",
            "G02": "3a19deae-2c7a-204e-95ed-1f1950f28343",
            "G03": "3a19deae-2c7a-392b-62f1-4907c66343f8",
            "H01": "3a19deae-2c7a-5602-e876-d27aca4e3201",
            "H02": "3a19deae-2c7a-f15c-70e0-25b58a8c9702",
            "H03": "3a19deae-2c7a-780b-8965-2e1345f7e834",
            "I01": "3a19deae-2c7a-8849-e172-07de14ede928",
            "I02": "3a19deae-2c7a-4772-a37f-ff99270bafc0",
            "I03": "3a19deae-2c7a-cce7-6e4a-25ea4a2068c4",
            "J01": "3a19deae-2c7a-1848-de92-b5d5ed054cc6",
            "J02": "3a19deae-2c7a-1d45-b4f8-6f866530e205",
            "J03": "3a19deae-2c7a-f237-89d9-8fe19025dee9"
        }
    },
    "4号手套箱内部堆栈": {
        "uuid": "",
        "site_uuids": {
            "A01": "3a1baa20-a7b1-c665-8b9c-d8099d07d2f6",
            "A02": "3a1baa20-a7b1-93a7-c988-f9c8ad6c58c9",
            "A03": "3a1baa20-a7b1-00ee-f751-da9b20b6c464",
            "A04": "3a1baa20-a7b1-4712-c37b-0b5b658ef7b9",
            "B01": "3a1baa20-a7b1-9847-fc9c-96d604cd1a8e",
            "B02": "3a1baa20-a7b1-4ae9-e604-0601db06249c",
            "B03": "3a1baa20-a7b1-8329-ea75-81ca559d9ce1",
            "B04": "3a1baa20-a7b1-89c5-d96f-36e98a8f7268",
            "C01": "3a1baa20-a7b1-32ec-39e6-8044733839d6",
            "C02": "3a1baa20-a7b1-b573-e426-4c86040348b2",
            "C03": "3a1baa20-a7b1-cca7-781e-0522b729bf5d",
            "C04": "3a1baa20-a7b1-7c98-5fd9-5855355ae4b3"
        }
    },
    "大分液瓶堆栈": {
        "uuid": "",
        "site_uuids": {
            "A01": "3a19da3d-4f3d-bcac-2932-7542041e10e0",
            "A02": "3a19da3d-4f3d-4d75-38ac-fb58ad0687c3",
            "A03": "3a19da3d-4f3d-b25e-f2b1-85342a5b7eae",
            "B01": "3a19da3d-4f3d-fd3e-058a-2733a0925767",
            "B02": "3a19da3d-4f3d-37bd-a944-c391ad56857f",
            "B03": "3a19da3d-4f3d-e353-7862-c6d1d4bc667f",
            "C01": "3a19da3d-4f3d-9519-5da7-76179c958e70",
            "C02": "3a19da3d-4f3d-b586-d7ed-9ec244f6f937",
            "C03": "3a19da3d-4f3d-5061-249b-35dfef732811"
        }
    },
    "小分液瓶堆栈": {
        "uuid": "",
        "site_uuids": {
            "C03": "3a19da40-55bf-8943-d20d-a8b3ea0d16c0"
        }
    },
    "站内Tip头盒堆栈": {
        "uuid": "",
        "site_uuids": {
            "A01": "3a19deab-d5cc-be1e-5c37-4e9e5a664388",
            "A02": "3a19deab-d5cc-b394-8141-27cb3853e8ea",
            "B01": "3a19deab-d5cc-4dca-596e-ca7414d5f501",
            "B02": "3a19deab-d5cc-9bc0-442b-12d9d59aa62a",
            "C01": "3a19deab-d5cc-2eaf-b6a4-f0d54e4f1246",
            "C02": "3a19deab-d5cc-d9f4-25df-b8018c372bc7"
        }
    },
    "配液站内配液大板仓库(无需提前上料)": {
        "uuid": "",
        "site_uuids": {
            "A01": "3a1a21dc-06af-3915-9cb9-80a9dc42f386"
        }
    },
    "配液站内配液小板仓库(无需以前入料)": {
        "uuid": "",
        "site_uuids": {
            "A01": "3a1a21de-8e8b-7938-2d06-858b36c10e31"
        }
    },
    "移液站内大瓶板仓库(无需提前如料)": {
        "uuid": "",
        "site_uuids": {
            "A01": "3a1a224c-c727-fa62-1f2b-0037a84b9fca"
        }
    },
    "移液站内小瓶板仓库(无需提前入料)": {
        "uuid": "",
        "site_uuids": {
            "A01": "3a1a224d-ed49-710c-a9c3-3fc61d479cbb"
        }
    },
    "适配器位仓库": {
        "uuid": "",
        "site_uuids": {
            "A01": "3a1abd46-18fe-1f56-6ced-a1f7fe08e36c"
        }
    },
    "1号2号手套箱交接堆栈": {
        "uuid": "",
        "site_uuids": {
            "A01": "3a1baa49-7f77-35aa-60b1-e55a45d065fa"
        }
    },
    "2号手套箱内部堆栈": {
        "uuid": "",
        "site_uuids": {
            "A01": "3a1baa4b-393e-9f86-3921-7a18b0a8e371",
            "A02": "3a1baa4b-393e-9425-928b-ee0f6f679d44",
            "A03": "3a1baa4b-393e-0baf-632b-59dfdc931a3a",
            "B01": "3a1baa4b-393e-f8aa-c8a9-956f3132f05c",
            "B02": "3a1baa4b-393e-ef05-42f6-53f4c6e89d70",
            "B03": "3a1baa4b-393e-c07b-a924-a9f0dfda9711",
            "C01": "3a1baa4b-393e-4c2b-821a-16a7fe025c48",
            "C02": "3a1baa4b-393e-2eaf-61a1-9063c832d5a2",
            "C03": "3a1baa4b-393e-034e-8e28-8626d934a85f"
        }
    }
    
}

# 物料类型配置
MATERIAL_TYPE_MAPPINGS = {
    "100ml液体": ("YB_100ml_yeti", "d37166b3-ecaa-481e-bd84-3032b795ba07"),
    "液": ("YB_ye", "3a190ca1-2add-2b23-f8e1-bbd348b7f790"),
    "高粘液": ("YB_gaonianye", "abe8df30-563d-43d2-85e0-cabec59ddc16"),
    "加样头(大)": ("YB_jia_yang_tou_da_Carrier", "3a190ca0-b2f6-9aeb-8067-547e72c11469"),
    # "加样头(大)板": ("YB_jia_yang_tou_da", "a8e714ae-2a4e-4eb9-9614-e4c140ec3f16"),
    "5ml分液瓶板": ("YB_5ml_fenyepingban", "3a192fa4-007d-ec7b-456e-2a8be7a13f23"),
    "5ml分液瓶": ("YB_5ml_fenyeping", "3a192c2a-ebb7-58a1-480d-8b3863bf74f4"),
    "20ml分液瓶板": ("YB_20ml_fenyepingban", "3a192fa4-47db-3449-162a-eaf8aba57e27"),
    "20ml分液瓶": ("YB_20ml_fenyeping", "3a192c2b-19e8-f0a3-035e-041ca8ca1035"),
    "配液瓶(小)板": ("YB_peiyepingxiaoban", "3a190c8b-3284-af78-d29f-9a69463ad047"),
    "配液瓶(小)": ("YB_pei_ye_xiao_Bottle", "3a190c8c-fe8f-bf48-0dc3-97afc7f508eb"),
    "配液瓶(大)板": ("YB_peiyepingdaban", "53e50377-32dc-4781-b3c0-5ce45bc7dc27"),
    "配液瓶(大)": ("YB_pei_ye_da_Bottle", "19c52ad1-51c5-494f-8854-576f4ca9c6ca"),
    "适配器块": ("YB_shi_pei_qi_kuai", "efc3bb32-d504-4890-91c0-b64ed3ac80cf"),
    "枪头盒": ("YB_qiang_tou_he", "3a192c2e-20f3-a44a-0334-c8301839d0b3"),
    "枪头": ("YB_qiang_tou", "b6196971-1050-46da-9927-333e8dea062d"),
}

SOLID_LIQUID_MAPPINGS = {
    # 固体
    "LiDFOB": {
        "typeId": "3a190ca0-b2f6-9aeb-8067-547e72c11469",
        "code": "",
        "barCode": "",
        "name": "LiDFOB",
        "unit": "g",
        "parameters": "",
        "quantity": "2",
        "warningQuantity": "1",
        "details": []
    },
    # "LiPF6": {
    #     "typeId": "3a190ca0-b2f6-9aeb-8067-547e72c11469",
    #     "code": "",
    #     "barCode": "",
    #     "name": "LiPF6",
    #     "unit": "g",
    #     "parameters": "",
    #     "quantity": 2,
    #     "warningQuantity": 1,
    #     "details": []
    # },
    # "LiFSI": {
    #     "typeId": "3a190ca0-b2f6-9aeb-8067-547e72c11469",
    #     "code": "",
    #     "barCode": "",
    #     "name": "LiFSI",
    #     "unit": "g",
    #     "parameters": "",
    #     "quantity": 2,
    #     "warningQuantity": 1,
    #     "details": []
    # },
    # "DTC": {
    #     "typeId": "3a190ca0-b2f6-9aeb-8067-547e72c11469",
    #     "code": "",
    #     "barCode": "",
    #     "name": "DTC",
    #     "unit": "g",
    #     "parameters": "",
    #     "quantity": 2,
    #     "warningQuantity": 1,
    #     "details": []
    # },
    # "LiPO2F2": {
    #     "typeId": "3a190ca0-b2f6-9aeb-8067-547e72c11469",
    #     "code": "",
    #     "barCode": "",
    #     "name": "LiPO2F2",
    #     "unit": "g",
    #     "parameters": "",
    #     "quantity": 2,
    #     "warningQuantity": 1,
    #     "details": []
    # },
    # 液体
    # "SA": ("BIOYOND_PolymerStation_Solid_Stock", "3a190ca0-b2f6-9aeb-8067-547e72c11469"),
    # "EC": ("BIOYOND_PolymerStation_Solid_Stock", "3a190ca0-b2f6-9aeb-8067-547e72c11469"),
    # "VC": ("BIOYOND_PolymerStation_Solid_Stock", "3a190ca0-b2f6-9aeb-8067-547e72c11469"),
    # "AND": ("BIOYOND_PolymerStation_Solid_Stock", "3a190ca0-b2f6-9aeb-8067-547e72c11469"),
    # "HTCN": ("BIOYOND_PolymerStation_Solid_Stock", "3a190ca0-b2f6-9aeb-8067-547e72c11469"),
    # "DENE": ("BIOYOND_PolymerStation_Solid_Stock", "3a190ca0-b2f6-9aeb-8067-547e72c11469"),
    # "TMSP": ("BIOYOND_PolymerStation_Solid_Stock", "3a190ca0-b2f6-9aeb-8067-547e72c11469"),
    # "TMSB": ("BIOYOND_PolymerStation_Solid_Stock", "3a190ca0-b2f6-9aeb-8067-547e72c11469"),
    # "EP": ("BIOYOND_PolymerStation_Solid_Stock", "3a190ca0-b2f6-9aeb-8067-547e72c11469"),
    # "DEC": ("BIOYOND_PolymerStation_Solid_Stock", "3a190ca0-b2f6-9aeb-8067-547e72c11469"),
    # "EMC": ("BIOYOND_PolymerStation_Solid_Stock", "3a190ca0-b2f6-9aeb-8067-547e72c11469"),
    # "SN": ("BIOYOND_PolymerStation_Solid_Stock", "3a190ca0-b2f6-9aeb-8067-547e72c11469"),
    # "DMC": ("BIOYOND_PolymerStation_Solid_Stock", "3a190ca0-b2f6-9aeb-8067-547e72c11469"),
    # "FEC": ("BIOYOND_PolymerStation_Solid_Stock", "3a190ca0-b2f6-9aeb-8067-547e72c11469"),
}

WORKFLOW_MAPPINGS = {}

LOCATION_MAPPING = {}