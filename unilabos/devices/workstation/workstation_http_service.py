"""
工作站HTTP服务模块
Workstation HTTP Service Module

统一的工作站报送接收服务，基于LIMS协议规范：
1. 步骤完成报送 - POST /report/step_finish
2. 通量完成报送 - POST /report/sample_finish
3. 任务完成报送 - POST /report/order_finish
4. 批量更新报送 - POST /report/batch_update
5. 物料变更报送 - POST /report/material_change
6. 错误处理报送 - POST /report/error_handling
7. 健康检查和状态查询

统一使用LIMS协议字段规范，简化接口避免功能重复
"""
import json
import threading
import time
import traceback
from typing import Dict, Any, Optional, List
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path

from unilabos.utils.log import logger


@dataclass
class WorkstationReportRequest:
    """统一工作站报送请求（基于LIMS协议规范）"""
    token: str                     # 授权令牌
    request_time: str              # 请求时间，格式：2024-12-12 12:12:12.xxx
    data: Dict[str, Any]           # 报送数据


@dataclass
class MaterialUsage:
    """物料使用记录"""
    materialId: str                # 物料Id（GUID）
    locationId: str                # 库位Id（GUID）
    typeMode: str                  # 物料类型（样品1、试剂2、耗材0）
    usedQuantity: float            # 使用的数量（数字）


@dataclass
class HttpResponse:
    """HTTP响应"""
    success: bool
    message: str
    data: Optional[Dict[str, Any]] = None
    acknowledgment_id: Optional[str] = None


class WorkstationHTTPHandler(BaseHTTPRequestHandler):
    """工作站HTTP请求处理器"""

    def __init__(self, workstation_instance, *args, **kwargs):
        self.workstation = workstation_instance
        super().__init__(*args, **kwargs)

    def do_POST(self):
        """处理POST请求 - 统一的工作站报送接口"""
        try:
            # 解析请求路径
            parsed_path = urlparse(self.path)
            endpoint = parsed_path.path

            # 读取请求体
            content_length = int(self.headers.get('Content-Length', 0))
            if content_length > 0:
                post_data = self.rfile.read(content_length)
                request_data = json.loads(post_data.decode('utf-8'))
            else:
                request_data = {}

            logger.info(f"收到工作站报送: {endpoint} - {request_data.get('token', 'unknown')}")

            try:
                payload_for_log = {"method": "POST", **request_data}
                self._save_raw_request(endpoint, payload_for_log)
                if hasattr(self.workstation, '_reports_received_count'):
                    self.workstation._reports_received_count += 1
            except Exception:
                pass

            # 统一的报送端点路由（基于LIMS协议规范）
            if endpoint == '/report/step_finish':
                response = self._handle_step_finish_report(request_data)
            elif endpoint == '/report/sample_finish':
                response = self._handle_sample_finish_report(request_data)
            elif endpoint == '/report/order_finish':
                response = self._handle_order_finish_report(request_data)
            elif endpoint == '/report/batch_update':
                response = self._handle_batch_update_report(request_data)
            # 扩展报送端点
            elif endpoint == '/report/material_change':
                response = self._handle_material_change_report(request_data)
            elif endpoint == '/report/error_handling':
                response = self._handle_error_handling_report(request_data)
            elif endpoint == '/report/temperature-cutoff':
                response = self._handle_temperature_cutoff_report(request_data)
            # 保留LIMS协议端点以兼容现有系统
            elif endpoint == '/LIMS/step_finish':
                response = self._handle_step_finish_report(request_data)
            elif endpoint == '/LIMS/preintake_finish':
                response = self._handle_sample_finish_report(request_data)
            elif endpoint == '/LIMS/order_finish':
                response = self._handle_order_finish_report(request_data)
            else:
                response = HttpResponse(
                    success=False,
                    message=f"不支持的报送端点: {endpoint}",
                    data={"supported_endpoints": [
                        "/report/step_finish",
                        "/report/sample_finish",
                        "/report/order_finish",
                        "/report/batch_update",
                        "/report/material_change",
                        "/report/error_handling",
                        "/report/temperature-cutoff"
                    ]}
                )

            # 发送响应
            self._send_response(response)

        except Exception as e:
            logger.error(f"处理工作站报送失败: {e}\\n{traceback.format_exc()}")
            error_response = HttpResponse(
                success=False,
                message=f"请求处理失败: {str(e)}"
            )
            self._send_response(error_response)

    def do_GET(self):
        """处理GET请求 - 健康检查和状态查询"""
        try:
            parsed_path = urlparse(self.path)
            endpoint = parsed_path.path

            try:
                self._save_raw_request(endpoint, {"method": "GET"})
            except Exception:
                pass

            if endpoint == '/status':
                response = self._handle_status_check()
            elif endpoint == '/health':
                response = HttpResponse(success=True, message="服务健康")
            else:
                response = HttpResponse(
                    success=False,
                    message=f"不支持的查询端点: {endpoint}",
                    data={"supported_endpoints": ["/status", "/health"]}
                )

            self._send_response(response)

        except Exception as e:
            logger.error(f"GET请求处理失败: {e}")
            error_response = HttpResponse(
                success=False,
                message=f"GET请求处理失败: {str(e)}"
            )
            self._send_response(error_response)

    def do_OPTIONS(self):
        """处理OPTIONS请求 - CORS预检请求"""
        try:
            # 发送CORS响应头
            self.send_response(200)
            self.send_header('Access-Control-Allow-Origin', '*')
            self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
            self.send_header('Access-Control-Allow-Headers', 'Content-Type, Authorization')
            self.send_header('Access-Control-Max-Age', '86400')
            self.end_headers()

        except Exception as e:
            logger.error(f"OPTIONS请求处理失败: {e}")
            self.send_response(500)
            self.end_headers()

    def _handle_step_finish_report(self, request_data: Dict[str, Any]) -> HttpResponse:
        """处理步骤完成报送（统一LIMS协议规范）"""
        try:
            # 验证基本字段
            required_fields = ['token', 'request_time', 'data']
            if missing_fields := [field for field in required_fields if field not in request_data]:
                return HttpResponse(
                    success=False,
                    message=f"缺少必要字段: {', '.join(missing_fields)}"
                )

            # 验证data字段内容
            data = request_data['data']
            data_required_fields = ['orderCode', 'orderName', 'stepName', 'stepId', 'sampleId', 'startTime', 'endTime']
            if data_missing_fields := [field for field in data_required_fields if field not in data]:
                return HttpResponse(
                    success=False,
                    message=f"data字段缺少必要内容: {', '.join(data_missing_fields)}"
                )

            # 创建统一请求对象
            report_request = WorkstationReportRequest(
                token=request_data['token'],
                request_time=request_data['request_time'],
                data=data
            )

            # 调用工作站处理方法
            result = self.workstation.process_step_finish_report(report_request)

            return HttpResponse(
                success=True,
                message=f"步骤完成报送已处理: {data['stepName']} ({data['orderCode']})",
                acknowledgment_id=f"STEP_{int(time.time() * 1000)}_{data['stepId']}",
                data=result
            )

        except Exception as e:
            logger.error(f"处理步骤完成报送失败: {e}")
            return HttpResponse(
                success=False,
                message=f"步骤完成报送处理失败: {str(e)}"
            )

    def _handle_sample_finish_report(self, request_data: Dict[str, Any]) -> HttpResponse:
        """处理通量完成报送（统一LIMS协议规范）"""
        try:
            # 验证基本字段
            required_fields = ['token', 'request_time', 'data']
            if missing_fields := [field for field in required_fields if field not in request_data]:
                return HttpResponse(
                    success=False,
                    message=f"缺少必要字段: {', '.join(missing_fields)}"
                )

            # 验证data字段内容
            data = request_data['data']
            data_required_fields = ['orderCode', 'orderName', 'sampleId', 'startTime', 'endTime', 'status']
            if data_missing_fields := [field for field in data_required_fields if field not in data]:
                return HttpResponse(
                    success=False,
                    message=f"data字段缺少必要内容: {', '.join(data_missing_fields)}"
                )

            # 创建统一请求对象
            report_request = WorkstationReportRequest(
                token=request_data['token'],
                request_time=request_data['request_time'],
                data=data
            )

            # 调用工作站处理方法
            result = self.workstation.process_sample_finish_report(report_request)

            status_names = {
                "0": "待生产", "2": "进样", "10": "开始",
                "20": "完成", "-2": "异常停止", "-3": "人工停止"
            }
            status_desc = status_names.get(str(data['status']), f"状态{data['status']}")

            return HttpResponse(
                success=True,
                message=f"通量完成报送已处理: {data['sampleId']} ({data['orderCode']}) - {status_desc}",
                acknowledgment_id=f"SAMPLE_{int(time.time() * 1000)}_{data['sampleId']}",
                data=result
            )

        except Exception as e:
            logger.error(f"处理通量完成报送失败: {e}")
            return HttpResponse(
                success=False,
                message=f"通量完成报送处理失败: {str(e)}"
            )

    def _handle_order_finish_report(self, request_data: Dict[str, Any]) -> HttpResponse:
        """处理任务完成报送（统一LIMS协议规范）"""
        try:
            # 验证基本字段
            required_fields = ['token', 'request_time', 'data']
            if missing_fields := [field for field in required_fields if field not in request_data]:
                return HttpResponse(
                    success=False,
                    message=f"缺少必要字段: {', '.join(missing_fields)}"
                )

            # 验证data字段内容
            data = request_data['data']
            data_required_fields = ['orderCode', 'orderName', 'startTime', 'endTime', 'status']
            if data_missing_fields := [field for field in data_required_fields if field not in data]:
                return HttpResponse(
                    success=False,
                    message=f"data字段缺少必要内容: {', '.join(data_missing_fields)}"
                )

            # 处理物料使用记录
            used_materials = []
            if 'usedMaterials' in data:
                for material_data in data['usedMaterials']:
                    material = MaterialUsage(
                        materialId=material_data.get('materialId', ''),
                        locationId=material_data.get('locationId', ''),
                        typeMode=material_data.get('typeMode', ''),
                        usedQuantity=material_data.get('usedQuantity', 0.0)
                    )
                    used_materials.append(material)

            # 创建统一请求对象
            report_request = WorkstationReportRequest(
                token=request_data['token'],
                request_time=request_data['request_time'],
                data=data
            )

            # 调用工作站处理方法
            result = self.workstation.process_order_finish_report(report_request, used_materials)

            status_names = {"30": "完成", "-11": "异常停止", "-12": "人工停止"}
            status_desc = status_names.get(str(data['status']), f"状态{data['status']}")

            return HttpResponse(
                success=True,
                message=f"任务完成报送已处理: {data['orderName']} ({data['orderCode']}) - {status_desc}",
                acknowledgment_id=f"ORDER_{int(time.time() * 1000)}_{data['orderCode']}",
                data=result
            )

        except Exception as e:
            logger.error(f"处理任务完成报送失败: {e}")
            return HttpResponse(
                success=False,
                message=f"任务完成报送处理失败: {str(e)}"
            )

    def _handle_temperature_cutoff_report(self, request_data: Dict[str, Any]) -> HttpResponse:
        try:
            required_fields = ['token', 'request_time', 'data']
            if missing := [f for f in required_fields if f not in request_data]:
                return HttpResponse(success=False, message=f"缺少必要字段: {', '.join(missing)}")

            data = request_data['data']
            metrics = [
                'frameCode',
                'generateTime',
                'targetTemperature',
                'settingTemperature',
                'inTemperature',
                'outTemperature',
                'pt100Temperature',
                'sensorAverageTemperature',
                'speed',
                'force',
                'viscosity',
                'averageViscosity'
            ]
            if miss := [f for f in metrics if f not in data]:
                return HttpResponse(success=False, message=f"data字段缺少必要内容: {', '.join(miss)}")

            report_request = WorkstationReportRequest(
                token=request_data['token'],
                request_time=request_data['request_time'],
                data=data
            )

            result = {}
            if hasattr(self.workstation, 'process_temperature_cutoff_report'):
                result = self.workstation.process_temperature_cutoff_report(report_request)

            return HttpResponse(
                success=True,
                message=f"温度/粘度报送已处理: 帧{data['frameCode']}",
                acknowledgment_id=f"TEMP_CUTOFF_{int(time.time()*1000)}_{data['frameCode']}",
                data=result
            )
        except Exception as e:
            logger.error(f"处理温度/粘度报送失败: {e}\n{traceback.format_exc()}")
            return HttpResponse(success=False, message=f"温度/粘度报送处理失败: {str(e)}")

    def _handle_batch_update_report(self, request_data: Dict[str, Any]) -> HttpResponse:
        """处理批量报送"""
        try:
            step_updates = request_data.get('step_updates', [])
            sample_updates = request_data.get('sample_updates', [])
            order_updates = request_data.get('order_updates', [])

            results = {
                'step_results': [],
                'sample_results': [],
                'order_results': [],
                'total_processed': 0,
                'total_failed': 0
            }

            # 处理批量步骤更新
            for step_data in step_updates:
                try:
                    step_data['token'] = request_data.get('token', step_data.get('token'))
                    step_data['request_time'] = request_data.get('request_time', step_data.get('request_time'))
                    result = self._handle_step_finish_report(step_data)
                    results['step_results'].append(result)
                    if result.success:
                        results['total_processed'] += 1
                    else:
                        results['total_failed'] += 1
                except Exception as e:
                    results['step_results'].append(HttpResponse(success=False, message=str(e)))
                    results['total_failed'] += 1

            # 处理批量通量更新
            for sample_data in sample_updates:
                try:
                    sample_data['token'] = request_data.get('token', sample_data.get('token'))
                    sample_data['request_time'] = request_data.get('request_time', sample_data.get('request_time'))
                    result = self._handle_sample_finish_report(sample_data)
                    results['sample_results'].append(result)
                    if result.success:
                        results['total_processed'] += 1
                    else:
                        results['total_failed'] += 1
                except Exception as e:
                    results['sample_results'].append(HttpResponse(success=False, message=str(e)))
                    results['total_failed'] += 1

            # 处理批量任务更新
            for order_data in order_updates:
                try:
                    order_data['token'] = request_data.get('token', order_data.get('token'))
                    order_data['request_time'] = request_data.get('request_time', order_data.get('request_time'))
                    result = self._handle_order_finish_report(order_data)
                    results['order_results'].append(result)
                    if result.success:
                        results['total_processed'] += 1
                    else:
                        results['total_failed'] += 1
                except Exception as e:
                    results['order_results'].append(HttpResponse(success=False, message=str(e)))
                    results['total_failed'] += 1

            return HttpResponse(
                success=results['total_failed'] == 0,
                message=f"批量报送处理完成: {results['total_processed']} 成功, {results['total_failed']} 失败",
                acknowledgment_id=f"BATCH_{int(time.time() * 1000)}",
                data=results
            )

        except Exception as e:
            logger.error(f"处理批量报送失败: {e}")
            return HttpResponse(
                success=False,
                message=f"批量报送处理失败: {str(e)}"
            )

    def _handle_material_change_report(self, request_data: Dict[str, Any]) -> HttpResponse:
        """处理物料变更报送"""
        try:
            # 验证必需字段
            if 'brand' in request_data:
                if request_data['brand'] == "bioyond":  # 奔曜
                    material_data = request_data["text"]
                    logger.info(f"收到奔曜物料变更报送: {material_data}")
                    return HttpResponse(
                        success=True,
                        message=f"物料变更报送已收到: {material_data}",
                        acknowledgment_id=f"MATERIAL_{int(time.time() * 1000)}_{material_data.get('id', 'unknown')}",
                        data=None
                    )
            else:
                return HttpResponse(
                    success=False,
                    message=f"缺少厂家信息（brand字段）"
                )
            required_fields = ['workstation_id', 'timestamp', 'resource_id', 'change_type']
            if missing_fields := [field for field in required_fields if field not in request_data]:
                return HttpResponse(
                    success=False,
                    message=f"缺少必要字段: {', '.join(missing_fields)}"
                )

            # 调用工作站的处理方法
            result = self.workstation.process_material_change_report(request_data)

            return HttpResponse(
                success=True,
                message=f"物料变更报送已处理: {request_data['resource_id']} ({request_data['change_type']})",
                acknowledgment_id=f"MATERIAL_{int(time.time() * 1000)}_{request_data['resource_id']}",
                data=result
            )

        except Exception as e:
            logger.error(f"处理物料变更报送失败: {e}")
            return HttpResponse(
                success=False,
                message=f"物料变更报送处理失败: {str(e)}"
            )

    def _handle_error_handling_report(self, request_data: Dict[str, Any]) -> HttpResponse:
        """处理错误处理报送"""
        try:
            # 检查是否为奔曜格式的错误报送
            if 'brand' in request_data and str(request_data['brand']).lower() == "bioyond":
                # 奔曜格式处理
                if 'text' not in request_data:
                    return HttpResponse(
                        success=False,
                        message="奔曜格式缺少text字段"
                    )

                error_data = request_data["text"]
                logger.info(f"收到奔曜错误处理报送: {error_data}")

                # 调用工作站的处理方法
                result = self.workstation.handle_external_error(error_data)

                return HttpResponse(
                    success=True,
                    message=f"错误处理报送已收到: 任务{error_data.get('task', 'unknown')}, 错误代码{error_data.get('code', 'unknown')}",
                    acknowledgment_id=f"ERROR_{int(time.time() * 1000)}_{error_data.get('task', 'unknown')}",
                    data=result
                )
            else:
                # 标准格式处理
                required_fields = ['workstation_id', 'timestamp', 'error_type', 'error_message']
                if missing_fields := [field for field in required_fields if field not in request_data]:
                    return HttpResponse(
                        success=False,
                        message=f"缺少必要字段: {', '.join(missing_fields)}"
                    )

                # 调用工作站的处理方法
                result = self.workstation.handle_external_error(request_data)

                return HttpResponse(
                    success=True,
                    message=f"错误处理报送已处理: {request_data['error_type']} - {request_data['error_message']}",
                    acknowledgment_id=f"ERROR_{int(time.time() * 1000)}_{request_data.get('action_id', 'unknown')}",
                    data=result
                )

        except Exception as e:
            logger.error(f"处理错误处理报送失败: {e}")
            return HttpResponse(
                success=False,
                message=f"错误处理报送处理失败: {str(e)}"
            )

    def _handle_status_check(self) -> HttpResponse:
        """处理状态查询"""
        try:
            # 安全地获取 device_id
            device_id = "unknown"
            if hasattr(self.workstation, 'device_id'):
                device_id = self.workstation.device_id
            elif hasattr(self.workstation, '_ros_node') and hasattr(self.workstation._ros_node, 'device_id'):
                device_id = self.workstation._ros_node.device_id

            return HttpResponse(
                success=True,
                message="工作站报送服务正常运行",
                data={
                    "workstation_id": device_id,
                    "service_type": "unified_reporting_service",
                    "uptime": time.time() - getattr(self.workstation, '_start_time', time.time()),
                    "reports_received": getattr(self.workstation, '_reports_received_count', 0),
                    "supported_endpoints": [
                        "POST /report/step_finish",
                        "POST /report/sample_finish",
                        "POST /report/order_finish",
                        "POST /report/batch_update",
                        "POST /report/material_change",
                        "POST /report/error_handling",
                        "POST /report/temperature-cutoff",
                        "GET /status",
                        "GET /health"
                    ]
                }
            )
        except Exception as e:
            logger.error(f"处理状态查询失败: {e}")
            return HttpResponse(
                success=False,
                message=f"状态查询失败: {str(e)}"
            )

    def _send_response(self, response: HttpResponse):
        """发送响应"""
        try:
            # 设置响应状态码
            status_code = 200 if response.success else 400
            self.send_response(status_code)

            # 设置响应头
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
            self.send_header('Access-Control-Allow-Headers', 'Content-Type')
            self.end_headers()

            # 发送响应体
            response_json = json.dumps(asdict(response), ensure_ascii=False, indent=2)
            self.wfile.write(response_json.encode('utf-8'))

        except Exception as e:
            logger.error(f"发送响应失败: {e}")

    def log_message(self, format, *args):
        """重写日志方法"""
        logger.debug(f"HTTP请求: {format % args}")

    def _save_raw_request(self, endpoint: str, request_data: Dict[str, Any]) -> None:
        try:
            base_dir = Path(__file__).resolve().parents[3] / "unilabos_data" / "http_reports"
            base_dir.mkdir(parents=True, exist_ok=True)
            log_path = getattr(self.workstation, "_http_log_path", None)
            log_file = Path(log_path) if log_path else (base_dir / f"http_{int(time.time()*1000)}.log")
            payload = {
                "endpoint": endpoint,
                "received_at": datetime.now().isoformat(),
                "body": request_data
            }
            with open(log_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(payload, ensure_ascii=False) + "\n")
        except Exception:
            pass


class WorkstationHTTPService:
    """工作站HTTP服务"""

    def __init__(self, workstation_instance, host: str = "127.0.0.1", port: int = 8080):
        self.workstation = workstation_instance
        self.host = host
        self.port = port
        self.server = None
        self.server_thread = None
        self.running = False

        # 初始化统计信息
        self.workstation._start_time = time.time()
        self.workstation._reports_received_count = 0

    def start(self):
        """启动HTTP服务"""
        try:
            # 创建处理器工厂函数
            def handler_factory(*args, **kwargs):
                return WorkstationHTTPHandler(self.workstation, *args, **kwargs)

            # 创建HTTP服务器
            self.server = HTTPServer((self.host, self.port), handler_factory)
            base_dir = Path(__file__).resolve().parents[3] / "unilabos_data" / "http_reports"
            base_dir.mkdir(parents=True, exist_ok=True)
            session_log = base_dir / f"http_{int(time.time()*1000)}.log"
            setattr(self.workstation, "_http_log_path", str(session_log))

            # 安全地获取 device_id 用于线程命名
            device_id = "unknown"
            if hasattr(self.workstation, 'device_id'):
                device_id = self.workstation.device_id
            elif hasattr(self.workstation, '_ros_node') and hasattr(self.workstation._ros_node, 'device_id'):
                device_id = self.workstation._ros_node.device_id

            # 在单独线程中运行服务器
            self.server_thread = threading.Thread(
                target=self._run_server,
                daemon=True,
                name=f"WorkstationHTTP-{device_id}"
            )

            self.running = True
            self.server_thread.start()

            logger.info(f"工作站HTTP报送服务已启动: http://{self.host}:{self.port}")
            logger.info("统一的报送端点 (基于LIMS协议规范):")
            logger.info("  - POST /report/step_finish    # 步骤完成报送")
            logger.info("  - POST /report/sample_finish  # 通量完成报送")
            logger.info("  - POST /report/order_finish   # 任务完成报送")
            logger.info("  - POST /report/batch_update   # 批量更新报送")
            logger.info("扩展报送端点:")
            logger.info("  - POST /report/material_change # 物料变更报送")
            logger.info("  - POST /report/error_handling  # 错误处理报送")
            logger.info("  - POST /report/temperature-cutoff # 温度/粘度报送")
            logger.info("兼容端点:")
            logger.info("  - POST /LIMS/step_finish      # 兼容LIMS步骤完成")
            logger.info("  - POST /LIMS/preintake_finish # 兼容LIMS通量完成")
            logger.info("  - POST /LIMS/order_finish     # 兼容LIMS任务完成")
            logger.info("服务端点:")
            logger.info("  - GET  /status                # 服务状态查询")
            logger.info("  - GET  /health                # 健康检查")

        except Exception as e:
            logger.error(f"启动HTTP服务失败: {e}")
            raise

    def stop(self):
        """停止HTTP服务"""
        try:
            if self.running and self.server:
                logger.info("正在停止工作站HTTP报送服务...")
                self.running = False

                # 停止serve_forever循环
                self.server.shutdown()

                # 等待服务器线程结束
                if self.server_thread and self.server_thread.is_alive():
                    self.server_thread.join(timeout=5.0)

                # 关闭服务器套接字
                self.server.server_close()

                logger.info("工作站HTTP报送服务已停止")

        except Exception as e:
            logger.error(f"停止HTTP服务失败: {e}")

    def _run_server(self):
        """运行HTTP服务器"""
        try:
            # 使用serve_forever()让服务持续运行
            self.server.serve_forever()
        except Exception as e:
            if self.running:  # 只在非正常停止时记录错误
                logger.error(f"HTTP服务运行错误: {e}")
        finally:
            logger.info("HTTP服务器线程已退出")

    @property
    def is_running(self) -> bool:
        """检查服务是否正在运行"""
        return self.running and self.server_thread and self.server_thread.is_alive()

    @property
    def service_url(self) -> str:
        """获取服务URL"""
        return f"http://{self.host}:{self.port}"


# 导出主要类 - 保持向后兼容
@dataclass
class MaterialChangeReport:
    """已废弃：物料变更报送，请使用统一的WorkstationReportRequest"""
    pass


@dataclass
class TaskExecutionReport:
    """已废弃：任务执行报送，请使用统一的WorkstationReportRequest"""
    pass


# 导出列表
__all__ = [
    'WorkstationReportRequest',
    'MaterialUsage',
    'HttpResponse',
    'WorkstationHTTPService',
    # 向后兼容
    'MaterialChangeReport',
    'TaskExecutionReport'
]


if __name__ == "__main__":
    # 简单测试HTTP服务
    class BioyondWorkstation:
        device_id = "WS-001"

        def process_step_finish_report(self, report_request):
            return {"processed": True}

        def process_sample_finish_report(self, report_request):
            return {"processed": True}

        def process_order_finish_report(self, report_request, used_materials):
            return {"processed": True}

        def process_material_change_report(self, report_data):
            return {"processed": True}

        def handle_external_error(self, error_data):
            return {"handled": True}

        def process_temperature_cutoff_report(self, report_request):
            return {"processed": True, "metrics": report_request.data}

    workstation = BioyondWorkstation()
    http_service = WorkstationHTTPService(workstation)

    try:
        http_service.start()
        print(f"测试服务器已启动: {http_service.service_url}")
        print("按 Ctrl+C 停止服务器")
        print("服务将持续运行，等待接收HTTP请求...")

        # 保持服务器运行 - 使用更好的等待机制
        try:
            while http_service.is_running:
                time.sleep(1)
        except KeyboardInterrupt:
            print("\n接收到停止信号...")

    except KeyboardInterrupt:
        print("\n正在停止服务器...")
        http_service.stop()
        print("服务器已停止")
    except Exception as e:
        print(f"服务器运行错误: {e}")
        http_service.stop()
