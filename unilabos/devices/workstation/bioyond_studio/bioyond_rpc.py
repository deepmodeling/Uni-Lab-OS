# bioyond_rpc.py
"""
BioyondV1RPC类定义 - 负责HTTP接口通信和通用函数
仅包含基础的API调用、通用工具函数，不包含特定站点业务逻辑
"""

from enum import Enum
from datetime import datetime, timezone
from unilabos.device_comms.rpc import BaseRequest
from typing import Optional, List, Dict, Any
import json
from unilabos.devices.workstation.bioyond_studio.config import LOCATION_MAPPING


class SimpleLogger:
    """简单的日志记录器"""
    def info(self, msg): print(f"[INFO] {msg}")
    def error(self, msg): print(f"[ERROR] {msg}")
    def debug(self, msg): print(f"[DEBUG] {msg}")
    def warning(self, msg): print(f"[WARNING] {msg}")
    def critical(self, msg): print(f"[CRITICAL] {msg}")


class MachineState(Enum):
    INITIAL = 0
    STOPPED = 1
    RUNNING = 2
    PAUSED = 3
    ERROR_PAUSED = 4
    ERROR_STOPPED = 5


class MaterialType(Enum):
    Consumables = 0
    Sample = 1
    Reagent = 2
    Product = 3


class BioyondException(Exception):
    """Bioyond操作异常"""
    pass


class BioyondV1RPC(BaseRequest):
    def __init__(self, config):
        super().__init__()
        print("开始初始化 BioyondV1RPC")
        self.config = config
        self.api_key = config["api_key"]
        self.host = config["api_host"]
        self._logger = SimpleLogger()
        self.material_cache = {}
        self._load_material_cache()

    # ==================== 基础通用方法 ====================

    def get_current_time_iso8601(self) -> str:
        """
        获取当前时间，并格式化为 ISO 8601 格式（包含毫秒部分）。

        :return: 当前时间的 ISO 8601 格式字符串
        """
        current_time = datetime.now(timezone.utc).isoformat(
            timespec='milliseconds'
        )
        # 替换时区部分为 'Z'
        current_time = current_time.replace("+00:00", "Z")
        return current_time

    def get_logger(self):
        return self._logger

    # ==================== 物料查询相关接口 ====================

    def stock_material(self, json_str: str) -> list:
        """
            描述：返回所有当前在库的，已启用的物料
            json_str 字段介绍，格式为JSON字符串:
            '{"typeMode": 0, "filter": "样品", "includeDetail": true}'

            typeMode: 物料类型, 样品1、试剂2、耗材0
            filter: 过滤字段, 物料名称/物料编码
            includeDetail: 是否包含所在库位。true，false
        """
        try:
            params = json.loads(json_str)
        except json.JSONDecodeError:
            return []

        response = self.post(
            url=f'{self.host}/api/lims/storage/stock-material',
            params={
                "apiKey": self.api_key,
                "requestTime": self.get_current_time_iso8601(),
                "data": params
            })

        if not response or response['code'] != 1:
            return []
        return response.get("data", [])

    def query_warehouse_by_material_type(self, type_id: str) -> dict:
        """
            描述：查询物料类型可以入库的库位
            type_id: 物料类型ID
        """
        params = {
            "typeId": type_id
        }

        response = self.post(
            url=f'{self.host}/api/lims/storage/warehouse-info-by-mat-type-id',
            params={
                "apiKey": self.api_key,
                "requestTime": self.get_current_time_iso8601(),
                "data": params
            })

        if not response:
            return {}

        if response['code'] != 1:
            print(
                f"query warehouse by material type error: {response.get('message', '')}"
            )
            return {}

        return response.get("data", {})

    def material_id_query(self, json_str: str) -> dict:
        """
        查询物料id
        json_str 格式为JSON字符串:
        '{"material123"}'
        """
        params = json_str

        response = self.post(
            url=f'{self.host}/api/lims/storage/workflow-sample-locations',
            params={
                "apiKey": self.api_key,
                "requestTime": self.get_current_time_iso8601(),
                "data": params
            })

        if not response:
            return {}

        if response['code'] != 1:
            print(f"material_id_query error: {response.get('message')}")
            return {}

        print(f"material_id_query data: {response['data']}")
        return response.get("data", {})

    def add_material(self, params: dict) -> dict:
        """
            描述：添加新的物料
            json_str 格式为JSON字符串
        """

        response = self.post(
            url=f'{self.host}/api/lims/storage/material',
            params={
                "apiKey": self.api_key,
                "requestTime": self.get_current_time_iso8601(),
                "data": params
            })

        if not response:
            return {}

        if response['code'] != 1:
            print(f"add material error: {response.get('message', '')}")
            return {}

        print(f"add material data: {response['data']}")
        return response.get("data", {})

    def query_matial_type_id(self, data) -> list:
        """查找物料typeid"""
        response = self.post(
            url=f'{self.host}/api/lims/storage/material-types',
            params={
                "apiKey": self.api_key,
                "requestTime": self.get_current_time_iso8601(),
                "data": data
            })

        if not response or response['code'] != 1:
            return []
        return str(response.get("data", {}))

    def material_type_list(self) -> list:
        """查询物料类型列表

        返回值:
            list: 物料类型数组，失败返回空列表
        """
        response = self.post(
            url=f'{self.host}/api/lims/storage/material-type-list',
            params={
                "apiKey": self.api_key,
                "requestTime": self.get_current_time_iso8601(),
                "data": {},
            })
        if not response or response['code'] != 1:
            return []
        return response.get("data", [])

    def material_inbound(self, material_id: str, location_id: str) -> dict:
        """
            描述：指定库位入库一个物料
            material_id: 物料ID
            location_name: 库位名称（会自动映射到location_id）
        """
        params = {
            "materialId": material_id,
            "locationId": location_id
        }

        response = self.post(
            url=f'{self.host}/api/lims/storage/inbound',
            params={
                "apiKey": self.api_key,
                "requestTime": self.get_current_time_iso8601(),
                "data": params
            })

        if not response or response['code'] != 1:
            if response:
                error_msg = response.get('message', '未知错误')
                print(f"[ERROR] 物料入库失败: code={response.get('code')}, message={error_msg}")
            else:
                print(f"[ERROR] 物料入库失败: API 无响应")
            return {}
        # 入库成功时，即使没有 data 字段，也返回成功标识
        return response.get("data") or {"success": True}

    def batch_inbound(self, inbound_items: List[Dict[str, Any]]) -> int:
        """批量入库物料

        参数:
            inbound_items: 入库条目列表，每项包含 materialId/locationId/quantity 等

        返回值:
            int: 成功返回1，失败返回0
        """
        response = self.post(
            url=f'{self.host}/api/lims/storage/batch-inbound',
            params={
                "apiKey": self.api_key,
                "requestTime": self.get_current_time_iso8601(),
                "data": inbound_items,
            })
        if not response or response['code'] != 1:
            return 0
        return response.get("code", 0)

    def delete_material(self, material_id: str) -> dict:
        """
            描述：删除尚未入库的物料
            material_id: 物料ID
        """
        response = self.post(
            url=f'{self.host}/api/lims/storage/delete-material',
            params={
                "apiKey": self.api_key,
                "requestTime": self.get_current_time_iso8601(),
                "data": material_id
            })

        if not response or response['code'] != 1:
            return {}
        return response.get("data", {})

    def material_outbound(self, material_id: str, location_name: str, quantity: int) -> dict:
        """指定库位出库物料（通过库位名称）"""
        location_id = LOCATION_MAPPING.get(location_name, location_name)

        params = {
            "materialId": material_id,
            "locationId": location_id,
            "quantity": quantity
        }

        response = self.post(
            url=f'{self.host}/api/lims/storage/outbound',
            params={
                "apiKey": self.api_key,
                "requestTime": self.get_current_time_iso8601(),
                "data": params
            })

        if not response or response['code'] != 1:
            return None
        return response

    def material_outbound_by_id(self, material_id: str, location_id: str, quantity: int) -> dict:
        """指定库位出库物料（直接使用location_id）

        Args:
            material_id: 物料ID
            location_id: 库位ID（不是库位名称，是UUID）
            quantity: 数量

        Returns:
            dict: API响应，失败返回None
        """
        params = {
            "materialId": material_id,
            "locationId": location_id,
            "quantity": quantity
        }

        response = self.post(
            url=f'{self.host}/api/lims/storage/outbound',
            params={
                "apiKey": self.api_key,
                "requestTime": self.get_current_time_iso8601(),
                "data": params
            })

        if not response or response['code'] != 1:
            return None
        return response

    def batch_outbound(self, outbound_items: List[Dict[str, Any]]) -> int:
        """批量出库物料

        参数:
            outbound_items: 出库条目列表，每项包含 materialId/locationId/quantity 等

        返回值:
            int: 成功返回1，失败返回0
        """
        response = self.post(
            url=f'{self.host}/api/lims/storage/batch-outbound',
            params={
                "apiKey": self.api_key,
                "requestTime": self.get_current_time_iso8601(),
                "data": outbound_items,
            })
        if not response or response['code'] != 1:
            return 0
        return response.get("code", 0)

    def material_info(self, material_id: str) -> dict:
        """查询物料详情

        参数:
            material_id: 物料ID

        返回值:
            dict: 物料信息字典，失败返回空字典
        """
        response = self.post(
            url=f'{self.host}/api/lims/storage/material-info',
            params={
                "apiKey": self.api_key,
                "requestTime": self.get_current_time_iso8601(),
                "data": material_id,
            })
        if not response or response['code'] != 1:
            return {}
        return response.get("data", {})

    def reset_location(self, location_id: str) -> int:
        """复位库位

        参数:
            location_id: 库位ID

        返回值:
            int: 成功返回1，失败返回0
        """
        response = self.post(
            url=f'{self.host}/api/lims/storage/reset-location',
            params={
                "apiKey": self.api_key,
                "requestTime": self.get_current_time_iso8601(),
                "data": location_id,
            })
        if not response or response['code'] != 1:
            return 0
        return response.get("code", 0)

    # ==================== 工作流查询相关接口 ====================

    def query_workflow(self, json_str: str) -> dict:
        try:
            params = json.loads(json_str)
        except json.JSONDecodeError:
            print(f"无效的JSON字符串: {json_str}")
            return {}
        except Exception as e:
            print(f"处理JSON时出错: {str(e)}")
            return {}

        response = self.post(
            url=f'{self.host}/api/lims/workflow/work-flow-list',
            params={
                "apiKey": self.api_key,
                "requestTime": self.get_current_time_iso8601(),
                "data": params
            })

        if not response or response['code'] != 1:
            return {}
        return response.get("data", {})

    def workflow_step_query(self, workflow_id: str) -> dict:
        """
            描述：查询某一个子工作流的详细信息，包含所有步骤、参数信息
            json_str 格式为JSON字符串:
            '{"workflow_id": "workflow123"}'
        """

        response = self.post(
            url=f'{self.host}/api/lims/workflow/sub-workflow-step-parameters',
            params={
                "apiKey": self.api_key,
                "requestTime": self.get_current_time_iso8601(),
                "data": workflow_id,
            })

        if not response or response['code'] != 1:
            return {}
        return response.get("data", {})

    def split_workflow_list(self, params: Dict[str, Any]) -> dict:
        """查询可拆分工作流列表

        参数:
            params: 查询条件参数

        返回值:
            dict: 返回数据字典，失败返回空字典
        """
        response = self.post(
            url=f'{self.host}/api/lims/workflow/split-workflow-list',
            params={
                "apiKey": self.api_key,
                "requestTime": self.get_current_time_iso8601(),
                "data": params,
            })
        if not response or response['code'] != 1:
            return {}
        return response.get("data", {})

    def merge_workflow(self, data: Dict[str, Any]) -> dict:
        """合并工作流（无参数版）

        参数:
            data: 合并请求体，包含待合并的子工作流信息

        返回值:
            dict: 合并结果，失败返回空字典
        """
        response = self.post(
            url=f'{self.host}/api/lims/workflow/merge-workflow',
            params={
                "apiKey": self.api_key,
                "requestTime": self.get_current_time_iso8601(),
                "data": data,
            })
        if not response or response['code'] != 1:
            return {}
        return response.get("data", {})

    def merge_workflow_with_parameters(self, data: Dict[str, Any]) -> dict:
        """合并工作流（携带参数）

        参数:
            data: 合并请求体，包含 name、workflows 以及 stepParameters 等

        返回值:
            dict: 合并结果，失败返回空字典
        """
        response = self.post(
            url=f'{self.host}/api/lims/workflow/merge-workflow-with-parameters',
            params={
                "apiKey": self.api_key,
                "requestTime": self.get_current_time_iso8601(),
                "data": data,
            })
        if not response or response['code'] != 1:
            return {}
        return response.get("data", {})

    def validate_workflow_parameters(self, workflows: List[Dict[str, Any]]) -> Dict[str, Any]:
        """验证工作流参数格式"""
        try:
            validation_errors = []

            for i, workflow in enumerate(workflows):
                workflow_errors = []

                # 检查基本结构
                if not isinstance(workflow, dict):
                    workflow_errors.append("工作流必须是字典类型")
                    continue

                if "id" not in workflow:
                    workflow_errors.append("缺少必要的 'id' 字段")

                # 检查 stepParameters（如果存在）
                if "stepParameters" in workflow:
                    step_params = workflow["stepParameters"]

                    if not isinstance(step_params, dict):
                        workflow_errors.append("stepParameters 必须是字典类型")
                    else:
                        # 验证参数结构
                        for step_id, modules in step_params.items():
                            if not isinstance(modules, dict):
                                workflow_errors.append(f"步骤 {step_id} 的模块配置必须是字典类型")
                                continue

                            for module_name, params in modules.items():
                                if not isinstance(params, list):
                                    workflow_errors.append(f"步骤 {step_id} 模块 {module_name} 的参数必须是列表类型")
                                    continue

                                for j, param in enumerate(params):
                                    if not isinstance(param, dict):
                                        workflow_errors.append(f"步骤 {step_id} 模块 {module_name} 参数 {j} 必须是字典类型")
                                    elif "Key" not in param or "DisplayValue" not in param:
                                        workflow_errors.append(f"步骤 {step_id} 模块 {module_name} 参数 {j} 必须包含 Key 和 DisplayValue")

                if workflow_errors:
                    validation_errors.append({
                        "workflow_index": i,
                        "workflow_id": workflow.get("id", "unknown"),
                        "errors": workflow_errors
                    })

            if validation_errors:
                return {
                    "valid": False,
                    "errors": validation_errors,
                    "message": f"发现 {len(validation_errors)} 个工作流存在验证错误"
                }
            else:
                return {
                    "valid": True,
                    "message": f"所有 {len(workflows)} 个工作流验证通过"
                }

        except Exception as e:
            return {
                "valid": False,
                "errors": [{"general_error": str(e)}],
                "message": f"验证过程中发生异常: {str(e)}"
            }

    def get_workflow_parameter_template(self) -> Dict[str, Any]:
        """获取工作流参数模板"""
        return {
            "template": {
                "name": "拼接后的长工作流的名称",
                "workflows": [
                    {
                        "id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
                        "stepParameters": {
                            "步骤ID (UUID)": {
                                "模块名称": [
                                    {
                                        "key": "参数键名",
                                        "value": "参数值或变量引用 {{index-m-n}}"
                                    }
                                ]
                            }
                        }
                    }
                ]
            },
            "parameter_descriptions": {
                "name": "拼接后的长工作流名称",
                "workflows": "待合并的子工作流列表",
                "id": "子工作流 ID，对应工作流列表中 workflows 数组中每个对象的 id 字段",
                "stepParameters": "步骤参数配置，如果子工作流没有参数则不需要填写"
            }
        }

    # ==================== 任务订单相关接口 ====================

    def create_order(self, json_str: str) -> dict:
        """
            描述：新建并开始任务，返回需要的物料和入库的库位
            json_str 格式为JSON字符串，包含任务参数
        """
        try:
            params = json.loads(json_str)
            self._logger.info(f"创建任务参数: {params}")
            self._logger.info(f"参数类型: {type(params)}")

            response = self.post(
                url=f'{self.host}/api/lims/order/order',
                params={
                    "apiKey": self.api_key,
                    "requestTime": self.get_current_time_iso8601(),
                    "data": params
                })

            if not response:
                raise BioyondException("API调用失败：未收到响应")

            if response['code'] != 1:
                error_msg = f"创建任务失败: {response.get('message', '未知错误')}"
                self._logger.error(error_msg)
                raise BioyondException(error_msg)

            self._logger.info(f"创建任务成功，返回数据: {response['data']}")
            result = str(response.get("data", {}))
            return result

        except BioyondException:
            # 重新抛出BioyondException
            raise
        except json.JSONDecodeError as e:
            error_msg = f"JSON解析失败: {str(e)}"
            self._logger.error(error_msg)
            raise BioyondException(error_msg) from e
        except Exception as e:
            # 捕获其他未预期的异常，转换为BioyondException
            error_msg = f"创建任务时发生未预期的错误: {str(e)}"
            self._logger.error(error_msg)
            raise BioyondException(error_msg) from e

    def order_query(self, json_str: str) -> dict:
        """
            描述：查询任务列表
            json_str 格式为JSON字符串
        """
        try:
            params = json.loads(json_str)
        except json.JSONDecodeError:
            return {}

        response = self.post(
            url=f'{self.host}/api/lims/order/order-list',
            params={
                "apiKey": self.api_key,
                "requestTime": self.get_current_time_iso8601(),
                "data": params
            })

        if not response or response['code'] != 1:
            return {}
        return response.get("data", {})

    def order_report(self, order_id: str) -> dict:
        """查询订单报告

        参数:
            order_id: 订单ID

        返回值:
            dict: 报告数据，失败返回空字典
        """
        response = self.post(
            url=f'{self.host}/api/lims/order/order-report',
            params={
                "apiKey": self.api_key,
                "requestTime": self.get_current_time_iso8601(),
                "data": order_id,
            })
        if not response or response['code'] != 1:
            return {}
        return response.get("data", {})

    def order_takeout(self, json_str: str) -> int:
        """取出任务产物

        参数:
            json_str: JSON字符串，包含 order_id 与 preintake_id

        返回值:
            int: 成功返回1，失败返回0
        """
        try:
            data = json.loads(json_str)
            params = {
                "orderId": data.get("order_id", ""),
                "preintakeId": data.get("preintake_id", "")
            }
        except json.JSONDecodeError:
            return 0

        response = self.post(
            url=f'{self.host}/api/lims/order/order-takeout',
            params={
                "apiKey": self.api_key,
                "requestTime": self.get_current_time_iso8601(),
                "data": params,
            })

        if not response or response['code'] != 1:
            return 0
        return response.get("code", 0)


    def sample_waste_removal(self, order_id: str) -> dict:
        """样品/废料取出

        参数:
            order_id: 订单ID

        返回值:
            dict: 取出结果，失败返回空字典
        """
        params = {"orderId": order_id}

        response = self.post(
            url=f'{self.host}/api/lims/order/take-out',
            params={
                "apiKey": self.api_key,
                "requestTime": self.get_current_time_iso8601(),
                "data": params
            })

        if not response:
            return {}

        if response['code'] != 1:
            self._logger.error(f"样品废料取出错误: {response.get('message', '')}")
            return {}

        return response.get("data", {})

    def cancel_order(self, json_str: str) -> bool:
        """取消指定任务

        参数:
            json_str: JSON字符串，包含 order_id

        返回值:
            bool: 成功返回 True，失败返回 False
        """
        try:
            data = json.loads(json_str)
            order_id = data.get("order_id", "")
        except json.JSONDecodeError:
            return False

        response = self.post(
            url=f'{self.host}/api/lims/order/cancel-order',
            params={
                "apiKey": self.api_key,
                "requestTime": self.get_current_time_iso8601(),
                "data": order_id,
            })

        if not response or response['code'] != 1:
            return False
        return True

    def cancel_experiment(self, order_id: str) -> int:
        """取消指定实验

        参数:
            order_id: 订单ID

        返回值:
            int: 成功返回1，失败返回0
        """
        response = self.post(
            url=f'{self.host}/api/lims/order/cancel-experiment',
            params={
                "apiKey": self.api_key,
                "requestTime": self.get_current_time_iso8601(),
                "data": order_id,
            })
        if not response or response['code'] != 1:
            return 0
        return response.get("code", 0)

    def batch_cancel_experiment(self, order_ids: List[str]) -> int:
        """批量取消实验

        参数:
            order_ids: 订单ID列表

        返回值:
            int: 成功返回1，失败返回0
        """
        response = self.post(
            url=f'{self.host}/api/lims/order/batch-cancel-experiment',
            params={
                "apiKey": self.api_key,
                "requestTime": self.get_current_time_iso8601(),
                "data": order_ids,
            })
        if not response or response['code'] != 1:
            return 0
        return response.get("code", 0)

    def gantts_by_order_id(self, order_id: str) -> dict:
        """查询订单甘特图数据

        参数:
            order_id: 订单ID

        返回值:
            dict: 甘特数据，失败返回空字典
        """
        response = self.post(
            url=f'{self.host}/api/lims/order/gantts-by-order-id',
            params={
                "apiKey": self.api_key,
                "requestTime": self.get_current_time_iso8601(),
                "data": order_id,
            })
        if not response or response['code'] != 1:
            return {}
        return response.get("data", {})

    def simulation_gantt_by_order_id(self, order_id: str) -> dict:
        """查询订单模拟甘特图数据

        参数:
            order_id: 订单ID

        返回值:
            dict: 模拟甘特数据，失败返回空字典
        """
        response = self.post(
            url=f'{self.host}/api/lims/order/simulation-gantt-by-order-id',
            params={
                "apiKey": self.api_key,
                "requestTime": self.get_current_time_iso8601(),
                "data": order_id,
            })
        if not response or response['code'] != 1:
            return {}
        return response.get("data", {})

    def reset_order_status(self, order_id: str) -> int:
        """复位订单状态

        参数:
            order_id: 订单ID

        返回值:
            int: 成功返回1，失败返回0
        """
        response = self.post(
            url=f'{self.host}/api/lims/order/reset-order-status',
            params={
                "apiKey": self.api_key,
                "requestTime": self.get_current_time_iso8601(),
                "data": order_id,
            })
        if not response or response['code'] != 1:
            return 0
        return response.get("code", 0)

    def gantt_with_simulation_by_order_id(self, order_id: str) -> dict:
        """查询订单甘特与模拟联合数据

        参数:
            order_id: 订单ID

        返回值:
            dict: 联合数据，失败返回空字典
        """
        response = self.post(
            url=f'{self.host}/api/lims/order/gantt-with-simulation-by-order-id',
            params={
                "apiKey": self.api_key,
                "requestTime": self.get_current_time_iso8601(),
                "data": order_id,
            })
        if not response or response['code'] != 1:
            return {}
        return response.get("data", {})

    # ==================== 设备管理相关接口 ====================

    def device_list(self, json_str: str = "") -> list:
        """
            描述：获取所有设备列表
            json_str 格式为JSON字符串，可选
        """
        device_no = None
        if json_str:
            try:
                data = json.loads(json_str)
                device_no = data.get("device_no", None)
            except json.JSONDecodeError:
                pass

        url = f'{self.host}/api/lims/device/device-list'
        if device_no:
            url += f'/{device_no}'

        response = self.post(
            url=url,
            params={
                "apiKey": self.api_key,
                "requestTime": self.get_current_time_iso8601(),
            })

        if not response or response['code'] != 1:
            return []
        return response.get("data", [])

    def device_operation(self, json_str: str) -> int:
        """设备操作

        参数:
            json_str: JSON字符串，包含 device_no/operationType/operationParams

        返回值:
            int: 成功返回1，失败返回0
        """
        try:
            data = json.loads(json_str)
            params = {
                "deviceNo": data.get("device_no", ""),
                "operationType": data.get("operation_type", 0),
                "operationParams": data.get("operation_params", {})
            }
        except json.JSONDecodeError:
            return 0

        response = self.post(
            url=f'{self.host}/api/lims/device/execute-operation',
            params={
                "apiKey": self.api_key,
                "requestTime": self.get_current_time_iso8601(),
                "data": params,
            })

        if not response or response['code'] != 1:
            return 0
        return response.get("code", 0)

    def reset_devices(self) -> int:
        """复位设备集合

        返回值:
            int: 成功返回1，失败返回0
        """
        response = self.post(
            url=f'{self.host}/api/lims/device/reset-devices',
            params={
                "apiKey": self.api_key,
                "requestTime": self.get_current_time_iso8601(),
            })
        if not response or response['code'] != 1:
            return 0
        return response.get("code", 0)

    # ==================== 调度器相关接口 ====================

    def scheduler_status(self) -> dict:
        """查询调度器状态

        返回值:
            dict: 包含 schedulerStatus/hasTask/creationTime 等
        """
        response = self.post(
            url=f'{self.host}/api/lims/scheduler/scheduler-status',
            params={
                "apiKey": self.api_key,
                "requestTime": self.get_current_time_iso8601(),
            })

        if not response or response['code'] != 1:
            return {}
        return response.get("data", {})

    def scheduler_start(self) -> int:
        """启动调度器"""
        response = self.post(
            url=f'{self.host}/api/lims/scheduler/start',
            params={
                "apiKey": self.api_key,
                "requestTime": self.get_current_time_iso8601(),
            })

        if not response or response['code'] != 1:
            return 0
        return response.get("code", 0)

    def scheduler_pause(self) -> int:
        """暂停调度器"""
        response = self.post(
            url=f'{self.host}/api/lims/scheduler/pause',
            params={
                "apiKey": self.api_key,
                "requestTime": self.get_current_time_iso8601(),
            })

        if not response or response['code'] != 1:
            return 0
        return response.get("code", 0)

    def scheduler_smart_pause(self) -> int:
        """智能暂停调度器"""
        response = self.post(
            url=f'{self.host}/api/lims/scheduler/smart-pause',
            params={
                "apiKey": self.api_key,
                "requestTime": self.get_current_time_iso8601(),
            })

        if not response or response['code'] != 1:
            return 0
        return response.get("code", 0)

    def scheduler_continue(self) -> int:
        """继续调度器"""
        response = self.post(
            url=f'{self.host}/api/lims/scheduler/continue',
            params={
                "apiKey": self.api_key,
                "requestTime": self.get_current_time_iso8601(),
            })

        if not response or response['code'] != 1:
            return 0
        return response.get("code", 0)

    def scheduler_stop(self) -> int:
        """停止调度器"""
        response = self.post(
            url=f'{self.host}/api/lims/scheduler/stop',
            params={
                "apiKey": self.api_key,
                "requestTime": self.get_current_time_iso8601(),
            })

        if not response or response['code'] != 1:
            return 0
        return response.get("code", 0)

    def scheduler_reset(self) -> int:
        """复位调度器"""
        response = self.post(
            url=f'{self.host}/api/lims/scheduler/reset',
            params={
                "apiKey": self.api_key,
                "requestTime": self.get_current_time_iso8601(),
            })

        if not response or response['code'] != 1:
            return 0
        return response.get("code", 0)

    def scheduler_reply_error_handling(self, data: Dict[str, Any]) -> int:
        """调度错误处理回复

        参数:
            data: 错误处理参数

        返回值:
            int: 成功返回1，失败返回0
        """
        response = self.post(
            url=f'{self.host}/api/lims/scheduler/reply-error-handling',
            params={
                "apiKey": self.api_key,
                "requestTime": self.get_current_time_iso8601(),
                "data": data,
            })
        if not response or response['code'] != 1:
            return 0
        return response.get("code", 0)

    # ==================== 辅助方法 ====================

    def _load_material_cache(self):
        """预加载材料列表到缓存中"""
        try:
            print("正在加载材料列表缓存...")

            # 加载所有类型的材料：耗材(0)、样品(1)、试剂(2)
            material_types = [0, 1, 2]

            for type_mode in material_types:
                print(f"正在加载类型 {type_mode} 的材料...")
                stock_query = f'{{"typeMode": {type_mode}, "includeDetail": true}}'
                stock_result = self.stock_material(stock_query)

                if isinstance(stock_result, str):
                    stock_data = json.loads(stock_result)
                else:
                    stock_data = stock_result

                materials = stock_data
                for material in materials:
                    material_name = material.get("name")
                    material_id = material.get("id")
                    if material_name and material_id:
                        self.material_cache[material_name] = material_id

                    # 处理样品板等容器中的detail材料
                    detail_materials = material.get("detail", [])
                    for detail_material in detail_materials:
                        detail_name = detail_material.get("name")
                        detail_id = detail_material.get("detailMaterialId")
                        if detail_name and detail_id:
                            self.material_cache[detail_name] = detail_id
                            print(f"加载detail材料: {detail_name} -> ID: {detail_id}")

            print(f"材料列表缓存加载完成，共加载 {len(self.material_cache)} 个材料")

        except Exception as e:
            print(f"加载材料列表缓存时出错: {e}")
            self.material_cache = {}

    def _get_material_id_by_name(self, material_name_or_id: str) -> str:
        """根据材料名称获取材料ID"""
        if len(material_name_or_id) > 20 and '-' in material_name_or_id:
            return material_name_or_id

        if material_name_or_id in self.material_cache:
            material_id = self.material_cache[material_name_or_id]
            print(f"从缓存找到材料: {material_name_or_id} -> ID: {material_id}")
            return material_id

        print(f"警告: 未在缓存中找到材料名称 '{material_name_or_id}'，将使用原值")
        return material_name_or_id

    def refresh_material_cache(self):
        """刷新材料列表缓存"""
        print("正在刷新材料列表缓存...")
        self._load_material_cache()

    def get_available_materials(self):
        """获取所有可用的材料名称列表"""
        return list(self.material_cache.keys())

    def get_scheduler_state(self) -> Optional[MachineState]:
        """将调度状态字符串映射为枚举值

        返回值:
            Optional[MachineState]: 映射后的枚举，失败返回 None
        """
        data = self.scheduler_status()
        if not isinstance(data, dict):
            return None
        status = data.get("schedulerStatus")
        mapping = {
            "Init": MachineState.INITIAL,
            "Stop": MachineState.STOPPED,
            "Running": MachineState.RUNNING,
            "Pause": MachineState.PAUSED,
            "ErrorPause": MachineState.ERROR_PAUSED,
            "ErrorStop": MachineState.ERROR_STOPPED,
        }
        return mapping.get(status)
