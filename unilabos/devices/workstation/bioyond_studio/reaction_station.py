import json
import time
import requests
from typing import List, Dict, Any
from pathlib import Path
from datetime import datetime
from unilabos.devices.workstation.bioyond_studio.station import BioyondWorkstation
from unilabos.devices.workstation.bioyond_studio.bioyond_rpc import MachineState
from unilabos.ros.msgs.message_converter import convert_to_ros_msg, Float64, String
from unilabos.devices.workstation.bioyond_studio.config import (
    WORKFLOW_STEP_IDS,
    WORKFLOW_TO_SECTION_MAP,
    ACTION_NAMES
)
from unilabos.devices.workstation.bioyond_studio.config import API_CONFIG


class BioyondReactor:
    def __init__(self, config: dict = None, deck=None, protocol_type=None, **kwargs):
        self.in_temperature = 0.0
        self.out_temperature = 0.0
        self.pt100_temperature = 0.0
        self.sensor_average_temperature = 0.0
        self.target_temperature = 0.0
        self.setting_temperature = 0.0
        self.viscosity = 0.0
        self.average_viscosity = 0.0
        self.speed = 0.0
        self.force = 0.0

    def update_metrics(self, payload: Dict[str, Any]):
        def _f(v):
            try:
                return float(v)
            except Exception:
                return 0.0
        self.target_temperature = _f(payload.get("targetTemperature"))
        self.setting_temperature = _f(payload.get("settingTemperature"))
        self.in_temperature = _f(payload.get("inTemperature"))
        self.out_temperature = _f(payload.get("outTemperature"))
        self.pt100_temperature = _f(payload.get("pt100Temperature"))
        self.sensor_average_temperature = _f(payload.get("sensorAverageTemperature"))
        self.speed = _f(payload.get("speed"))
        self.force = _f(payload.get("force"))
        self.viscosity = _f(payload.get("viscosity"))
        self.average_viscosity = _f(payload.get("averageViscosity"))


class BioyondReactionStation(BioyondWorkstation):
    """Bioyond反应站类

    继承自BioyondWorkstation，提供反应站特定的业务方法
    """

    def __init__(self, config: dict = None, deck=None, protocol_type=None, **kwargs):
        """初始化反应站

        Args:
            config: 配置字典，应包含workflow_mappings等配置
            deck: Deck对象
            protocol_type: 协议类型（由ROS系统传递，此处忽略）
            **kwargs: 其他可能的参数
        """
        if deck is None and config:
            deck = config.get('deck')

        print(f"BioyondReactionStation初始化 - config包含workflow_mappings: {'workflow_mappings' in (config or {})}")
        if config and 'workflow_mappings' in config:
            print(f"workflow_mappings内容: {config['workflow_mappings']}")

        super().__init__(bioyond_config=config, deck=deck)

        print(f"BioyondReactionStation初始化完成 - workflow_mappings: {self.workflow_mappings}")
        print(f"workflow_mappings长度: {len(self.workflow_mappings)}")

        self.in_temperature = 0.0
        self.out_temperature = 0.0
        self.pt100_temperature = 0.0
        self.sensor_average_temperature = 0.0
        self.target_temperature = 0.0
        self.setting_temperature = 0.0
        self.viscosity = 0.0
        self.average_viscosity = 0.0
        self.speed = 0.0
        self.force = 0.0

        self._frame_to_reactor_id = {1: "reactor_1", 2: "reactor_2", 3: "reactor_3", 4: "reactor_4", 5: "reactor_5"}

    # ==================== 工作流方法 ====================

    def reactor_taken_out(self):
        """反应器取出"""
        self.append_to_workflow_sequence('{"web_workflow_name": "reactor_taken_out"}')
        reactor_taken_out_params = {"param_values": {}}
        self.pending_task_params.append(reactor_taken_out_params)
        print(f"成功添加反应器取出工作流")
        print(f"当前队列长度: {len(self.pending_task_params)}")
        return json.dumps({"suc": True})

    def reactor_taken_in(
        self,
        assign_material_name: str,
        cutoff: str = "900000",
        temperature: float = -10.00
    ):
        """反应器放入

        Args:
            assign_material_name: 物料名称（不能为空）
            cutoff: 粘度上限（需为有效数字字符串，默认 "900000"）
            temperature: 温度设定（°C，范围：-50.00 至 100.00）

        Returns:
            str: JSON 字符串，格式为 {"suc": True}

        Raises:
            ValueError: 若物料名称无效或 cutoff 格式错误
        """
        if not assign_material_name:
            raise ValueError("物料名称不能为空")
        try:
            float(cutoff)
        except ValueError:
            raise ValueError("cutoff 必须是有效的数字字符串")

        self.append_to_workflow_sequence('{"web_workflow_name": "reactor_taken_in"}')
        material_id = self.hardware_interface._get_material_id_by_name(assign_material_name)
        if material_id is None:
            raise ValueError(f"无法找到物料 {assign_material_name} 的 ID")

        if isinstance(temperature, str):
            temperature = float(temperature)

        step_id = WORKFLOW_STEP_IDS["reactor_taken_in"]["config"]
        reactor_taken_in_params = {
            "param_values": {
                step_id: {
                    ACTION_NAMES["reactor_taken_in"]["config"]: [
                        {"m": 0, "n": 3, "Key": "cutoff", "Value": cutoff},
                        {"m": 0, "n": 3, "Key": "assignMaterialName", "Value": material_id}
                    ],
                    ACTION_NAMES["reactor_taken_in"]["stirring"]: [
                        {"m": 0, "n": 3, "Key": "temperature", "Value": f"{temperature:.2f}"}
                    ]
                }
            }
        }

        self.pending_task_params.append(reactor_taken_in_params)
        print(f"成功添加反应器放入参数: material={assign_material_name}->ID:{material_id}, cutoff={cutoff}, temp={temperature:.2f}")
        print(f"当前队列长度: {len(self.pending_task_params)}")
        return json.dumps({"suc": True})

    def solid_feeding_vials(
        self,
        material_id: str,
        time: str = "0",
        torque_variation: int = 1,
        assign_material_name: str = None,
        temperature: float = 25.00
    ):
        """固体进料小瓶

        Args:
            material_id: 粉末类型ID，1=盐（21分钟），2=面粉（27分钟），3=BTDA（38分钟）
            time: 观察时间(分钟)
            torque_variation: 是否观察(int类型, 1=否, 2=是)
            assign_material_name: 物料名称(用于获取试剂瓶位ID)
            temperature: 温度设定(°C)
        """
        self.append_to_workflow_sequence('{"web_workflow_name": "Solid_feeding_vials"}')
        material_id_m = self.hardware_interface._get_material_id_by_name(assign_material_name) if assign_material_name else None

        if isinstance(temperature, str):
            temperature = float(temperature)

        feeding_step_id = WORKFLOW_STEP_IDS["solid_feeding_vials"]["feeding"]
        observe_step_id = WORKFLOW_STEP_IDS["solid_feeding_vials"]["observe"]

        solid_feeding_vials_params = {
            "param_values": {
                feeding_step_id: {
                    ACTION_NAMES["solid_feeding_vials"]["feeding"]: [
                        {"m": 0, "n": 3, "Key": "materialId", "Value": material_id},
                        {"m": 0, "n": 3, "Key": "assignMaterialName", "Value": material_id_m} if material_id_m else {}
                    ]
                },
                observe_step_id: {
                    ACTION_NAMES["solid_feeding_vials"]["observe"]: [
                        {"m": 1, "n": 0, "Key": "time", "Value": time},
                        {"m": 1, "n": 0, "Key": "torqueVariation", "Value": str(torque_variation)},
                        {"m": 1, "n": 0, "Key": "temperature", "Value": f"{temperature:.2f}"}
                    ]
                }
            }
        }

        self.pending_task_params.append(solid_feeding_vials_params)
        print(f"成功添加固体进料小瓶参数: material_id={material_id}, time={time}min, torque={torque_variation}, temp={temperature:.2f}°C")
        print(f"当前队列长度: {len(self.pending_task_params)}")
        return json.dumps({"suc": True})

    def liquid_feeding_vials_non_titration(
        self,
        volume_formula: str,
        assign_material_name: str,
        titration_type: str = "1",
        time: str = "0",
        torque_variation: int = 1,
        temperature: float = 25.00
    ):
        """液体进料小瓶(非滴定)

        Args:
            volume_formula: 分液公式(μL)
            assign_material_name: 物料名称
            titration_type: 是否滴定(1=否, 2=是)
            time: 观察时间(分钟)
            torque_variation: 是否观察(int类型, 1=否, 2=是)
            temperature: 温度(°C)
        """
        self.append_to_workflow_sequence('{"web_workflow_name": "Liquid_feeding_vials(non-titration)"}')
        material_id = self.hardware_interface._get_material_id_by_name(assign_material_name)
        if material_id is None:
            raise ValueError(f"无法找到物料 {assign_material_name} 的 ID")

        if isinstance(temperature, str):
            temperature = float(temperature)

        liquid_step_id = WORKFLOW_STEP_IDS["liquid_feeding_vials_non_titration"]["liquid"]
        observe_step_id = WORKFLOW_STEP_IDS["liquid_feeding_vials_non_titration"]["observe"]

        params = {
            "param_values": {
                liquid_step_id: {
                    ACTION_NAMES["liquid_feeding_vials_non_titration"]["liquid"]: [
                        {"m": 0, "n": 3, "Key": "volumeFormula", "Value": volume_formula},
                        {"m": 0, "n": 3, "Key": "assignMaterialName", "Value": material_id},
                        {"m": 0, "n": 3, "Key": "titrationType", "Value": titration_type}
                    ]
                },
                observe_step_id: {
                    ACTION_NAMES["liquid_feeding_vials_non_titration"]["observe"]: [
                        {"m": 1, "n": 0, "Key": "time", "Value": time},
                        {"m": 1, "n": 0, "Key": "torqueVariation", "Value": str(torque_variation)},
                        {"m": 1, "n": 0, "Key": "temperature", "Value": f"{temperature:.2f}"}
                    ]
                }
            }
        }

        self.pending_task_params.append(params)
        print(f"成功添加液体进料小瓶(非滴定)参数: volume={volume_formula}μL, material={assign_material_name}->ID:{material_id}")
        print(f"当前队列长度: {len(self.pending_task_params)}")
        return json.dumps({"suc": True})

    def liquid_feeding_solvents(
        self,
        assign_material_name: str,
        volume: str = None,
        solvents = None,
        titration_type: str = "1",
        time: str = "360",
        torque_variation: int = 2,
        temperature: float = 25.00
    ):
        """液体进料-溶剂

        Args:
            assign_material_name: 物料名称
            volume: 分液量(μL),直接指定体积(可选,如果提供solvents则自动计算)
            solvents: 溶剂信息的字典或JSON字符串(可选),格式如下:
              {
                  "additional_solvent": 33.55092503597727,  # 溶剂体积(mL)
                  "total_liquid_volume": 48.00916988195499
              }
              如果提供solvents,则从中提取additional_solvent并转换为μL
            titration_type: 是否滴定(1=否, 2=是)
            time: 观察时间(分钟)
            torque_variation: 是否观察(int类型, 1=否, 2=是)
            temperature: 温度设定(°C)
        """
        # 处理 volume 参数:优先使用直接传入的 volume,否则从 solvents 中提取
        if not volume and solvents is not None:
            # 参数类型转换:如果是字符串则解析为字典
            if isinstance(solvents, str):
                try:
                    solvents = json.loads(solvents)
                except json.JSONDecodeError as e:
                    raise ValueError(f"solvents参数JSON解析失败: {str(e)}")

            # 参数验证
            if not isinstance(solvents, dict):
                raise ValueError("solvents 必须是字典类型或有效的JSON字符串")

            # 提取 additional_solvent 值
            additional_solvent = solvents.get("additional_solvent")
            if additional_solvent is None:
                raise ValueError("solvents 中没有找到 additional_solvent 字段")

            # 转换为微升(μL) - 从毫升(mL)转换
            volume = str(float(additional_solvent) * 1000)
        elif volume is None:
            raise ValueError("必须提供 volume 或 solvents 参数之一")

        self.append_to_workflow_sequence('{"web_workflow_name": "Liquid_feeding_solvents"}')
        material_id = self.hardware_interface._get_material_id_by_name(assign_material_name)
        if material_id is None:
            raise ValueError(f"无法找到物料 {assign_material_name} 的 ID")

        if isinstance(temperature, str):
            temperature = float(temperature)

        liquid_step_id = WORKFLOW_STEP_IDS["liquid_feeding_solvents"]["liquid"]
        observe_step_id = WORKFLOW_STEP_IDS["liquid_feeding_solvents"]["observe"]

        params = {
            "param_values": {
                liquid_step_id: {
                    ACTION_NAMES["liquid_feeding_solvents"]["liquid"]: [
                        {"m": 0, "n": 1, "Key": "titrationType", "Value": titration_type},
                        {"m": 0, "n": 1, "Key": "volume", "Value": volume},
                        {"m": 0, "n": 1, "Key": "assignMaterialName", "Value": material_id}
                    ]
                },
                observe_step_id: {
                    ACTION_NAMES["liquid_feeding_solvents"]["observe"]: [
                        {"m": 1, "n": 0, "Key": "time", "Value": time},
                        {"m": 1, "n": 0, "Key": "torqueVariation", "Value": str(torque_variation)},
                        {"m": 1, "n": 0, "Key": "temperature", "Value": f"{temperature:.2f}"}
                    ]
                }
            }
        }

        self.pending_task_params.append(params)
        print(f"成功添加液体进料溶剂参数: material={assign_material_name}->ID:{material_id}, volume={volume}μL")
        print(f"当前队列长度: {len(self.pending_task_params)}")
        return json.dumps({"suc": True})

    def liquid_feeding_titration(
        self,
        assign_material_name: str,
        volume_formula: str = None,
        x_value: str = None,
        feeding_order_data: str = None,
        extracted_actuals: str = None,
        titration_type: str = "2",
        time: str = "90",
        torque_variation: int = 2,
        temperature: float = 25.00
    ):
        """液体进料(滴定)

        支持两种模式:
        1. 直接提供 volume_formula (传统方式)
        2. 自动计算公式: 提供 x_value, feeding_order_data, extracted_actuals (新方式)

        Args:
            assign_material_name: 物料名称
            volume_formula: 分液公式(μL),如果提供则直接使用,否则自动计算
            x_value: 手工输入的x值,格式如 "1-2-3"
            feeding_order_data: feeding_order JSON字符串或对象,用于获取m二酐值
            extracted_actuals: 从报告提取的实际加料量JSON字符串,包含actualTargetWeigh和actualVolume
            titration_type: 是否滴定(1=否, 2=是),默认2
            time: 观察时间(分钟)
            torque_variation: 是否观察(int类型, 1=否, 2=是)
            temperature: 温度(°C)

        自动公式模板: 1000*(m二酐-x)*V二酐滴定/m二酐滴定
        其中:
        - m二酐滴定 = actualTargetWeigh (从extracted_actuals获取)
        - V二酐滴定 = actualVolume (从extracted_actuals获取)
        - x = x_value (手工输入)
        - m二酐 = feeding_order中type为"main_anhydride"的amount值
        """
        self.append_to_workflow_sequence('{"web_workflow_name": "Liquid_feeding(titration)"}')
        material_id = self.hardware_interface._get_material_id_by_name(assign_material_name)
        if material_id is None:
            raise ValueError(f"无法找到物料 {assign_material_name} 的 ID")

        if isinstance(temperature, str):
            temperature = float(temperature)

        # 如果没有直接提供volume_formula,则自动计算
        if not volume_formula and x_value and feeding_order_data and extracted_actuals:
            # 1. 解析 feeding_order_data 获取 m二酐
            if isinstance(feeding_order_data, str):
                try:
                    feeding_order_data = json.loads(feeding_order_data)
                except json.JSONDecodeError as e:
                    raise ValueError(f"feeding_order_data JSON解析失败: {str(e)}")

            # 支持两种格式:
            # 格式1: 直接是数组 [{...}, {...}]
            # 格式2: 对象包裹 {"feeding_order": [{...}, {...}]}
            if isinstance(feeding_order_data, list):
                feeding_order_list = feeding_order_data
            elif isinstance(feeding_order_data, dict):
                feeding_order_list = feeding_order_data.get("feeding_order", [])
            else:
                raise ValueError("feeding_order_data 必须是数组或包含feeding_order的字典")

            # 从feeding_order中找到main_anhydride的amount
            m_anhydride = None
            for item in feeding_order_list:
                if item.get("type") == "main_anhydride":
                    m_anhydride = item.get("amount")
                    break

            if m_anhydride is None:
                raise ValueError("在feeding_order中未找到type为'main_anhydride'的条目")

            # 2. 解析 extracted_actuals 获取 actualTargetWeigh 和 actualVolume
            if isinstance(extracted_actuals, str):
                try:
                    extracted_actuals_obj = json.loads(extracted_actuals)
                except json.JSONDecodeError as e:
                    raise ValueError(f"extracted_actuals JSON解析失败: {str(e)}")
            else:
                extracted_actuals_obj = extracted_actuals

            # 获取actuals数组
            actuals_list = extracted_actuals_obj.get("actuals", [])
            if not actuals_list:
                # actuals为空,无法自动生成公式,回退到手动模式
                print(f"警告: extracted_actuals中actuals数组为空,无法自动生成公式,请手动提供volume_formula")
                volume_formula = None  # 清空,触发后续的错误检查
            else:
                # 根据assign_material_name匹配对应的actual数据
                # 假设order_code中包含物料名称
                matched_actual = None
                for actual in actuals_list:
                    order_code = actual.get("order_code", "")
                    # 简单匹配:如果order_code包含物料名称
                    if assign_material_name in order_code:
                        matched_actual = actual
                        break

                # 如果没有匹配到,使用第一个
                if not matched_actual and actuals_list:
                    matched_actual = actuals_list[0]

                if not matched_actual:
                    raise ValueError("无法从extracted_actuals中获取实际加料量数据")

                m_anhydride_titration = matched_actual.get("actualTargetWeigh")  # m二酐滴定
                v_anhydride_titration = matched_actual.get("actualVolume")       # V二酐滴定

                if m_anhydride_titration is None or v_anhydride_titration is None:
                    raise ValueError(f"实际加料量数据不完整: actualTargetWeigh={m_anhydride_titration}, actualVolume={v_anhydride_titration}")

                # 3. 构建公式: 1000*(m二酐-x)*V二酐滴定/m二酐滴定
                # x_value 格式如 "{{1-2-3}}",保留完整格式(包括花括号)直接替换到公式中
                volume_formula = f"1000*({m_anhydride}-{x_value})*{v_anhydride_titration}/{m_anhydride_titration}"

                print(f"自动生成滴定公式: {volume_formula}")
                print(f"  m二酐={m_anhydride}, x={x_value}, V二酐滴定={v_anhydride_titration}, m二酐滴定={m_anhydride_titration}")

        elif not volume_formula:
            raise ValueError("必须提供 volume_formula 或 (x_value + feeding_order_data + extracted_actuals)")

        liquid_step_id = WORKFLOW_STEP_IDS["liquid_feeding_titration"]["liquid"]
        observe_step_id = WORKFLOW_STEP_IDS["liquid_feeding_titration"]["observe"]

        params = {
            "param_values": {
                liquid_step_id: {
                    ACTION_NAMES["liquid_feeding_titration"]["liquid"]: [
                        {"m": 0, "n": 3, "Key": "volumeFormula", "Value": volume_formula},
                        {"m": 0, "n": 3, "Key": "titrationType", "Value": titration_type},
                        {"m": 0, "n": 3, "Key": "assignMaterialName", "Value": material_id}
                    ]
                },
                observe_step_id: {
                    ACTION_NAMES["liquid_feeding_titration"]["observe"]: [
                        {"m": 1, "n": 0, "Key": "time", "Value": time},
                        {"m": 1, "n": 0, "Key": "torqueVariation", "Value": str(torque_variation)},
                        {"m": 1, "n": 0, "Key": "temperature", "Value": f"{temperature:.2f}"}
                    ]
                }
            }
        }

        self.pending_task_params.append(params)
        print(f"成功添加液体进料滴定参数: volume={volume_formula}μL, material={assign_material_name}->ID:{material_id}")
        print(f"当前队列长度: {len(self.pending_task_params)}")
        return json.dumps({"suc": True})

    def _extract_actuals_from_report(self, report) -> Dict[str, Any]:
        data = report.get('data') if isinstance(report, dict) else None
        actual_target_weigh = None
        actual_volume = None
        if data:
            extra = data.get('extraProperties') or {}
            if isinstance(extra, dict):
                for v in extra.values():
                    obj = None
                    try:
                        obj = json.loads(v) if isinstance(v, str) else v
                    except Exception:
                        obj = None
                    if isinstance(obj, dict):
                        tw = obj.get('targetWeigh')
                        vol = obj.get('volume')
                        if tw is not None:
                            try:
                                actual_target_weigh = float(tw)
                            except Exception:
                                pass
                        if vol is not None:
                            try:
                                actual_volume = float(vol)
                            except Exception:
                                pass
        return {
            'actualTargetWeigh': actual_target_weigh,
            'actualVolume': actual_volume
        }

    def extract_actuals_from_batch_reports(self, batch_reports_result: str) -> dict:
        print(f"[DEBUG] extract_actuals 收到原始数据: {batch_reports_result[:500]}...")  # 打印前500字符
        try:
            obj = json.loads(batch_reports_result) if isinstance(batch_reports_result, str) else batch_reports_result
            if isinstance(obj, dict) and "return_info" in obj:
                inner = obj["return_info"]
                obj = json.loads(inner) if isinstance(inner, str) else inner
            reports = obj.get("reports", []) if isinstance(obj, dict) else []
            print(f"[DEBUG] 解析后的 reports 数组长度: {len(reports)}")
        except Exception as e:
            print(f"[DEBUG] 解析异常: {e}")
            reports = []

        actuals = []
        for i, r in enumerate(reports):
            print(f"[DEBUG] 处理 report[{i}]: order_code={r.get('order_code')}, has_extracted={r.get('extracted') is not None}, has_report={r.get('report') is not None}")
            order_code = r.get("order_code")
            order_id = r.get("order_id")
            ex = r.get("extracted")
            if isinstance(ex, dict) and (ex.get("actualTargetWeigh") is not None or ex.get("actualVolume") is not None):
                print(f"[DEBUG] 从 extracted 字段提取: actualTargetWeigh={ex.get('actualTargetWeigh')}, actualVolume={ex.get('actualVolume')}")
                actuals.append({
                    "order_code": order_code,
                    "order_id": order_id,
                    "actualTargetWeigh": ex.get("actualTargetWeigh"),
                    "actualVolume": ex.get("actualVolume")
                })
                continue
            report = r.get("report")
            vals = self._extract_actuals_from_report(report) if report else {"actualTargetWeigh": None, "actualVolume": None}
            print(f"[DEBUG] 从 report 字段提取: {vals}")
            actuals.append({
                "order_code": order_code,
                "order_id": order_id,
                **vals
            })

        print(f"[DEBUG] 最终提取的 actuals 数组长度: {len(actuals)}")
        result = {
            "return_info": json.dumps({"actuals": actuals}, ensure_ascii=False)
        }
        print(f"[DEBUG] 返回结果: {result}")
        return result

    def process_temperature_cutoff_report(self, report_request) -> Dict[str, Any]:
        try:
            data = report_request.data
            def _f(v):
                try:
                    return float(v)
                except Exception:
                    return 0.0
            self.target_temperature = _f(data.get("targetTemperature"))
            self.setting_temperature = _f(data.get("settingTemperature"))
            self.in_temperature = _f(data.get("inTemperature"))
            self.out_temperature = _f(data.get("outTemperature"))
            self.pt100_temperature = _f(data.get("pt100Temperature"))
            self.sensor_average_temperature = _f(data.get("sensorAverageTemperature"))
            self.speed = _f(data.get("speed"))
            self.force = _f(data.get("force"))
            self.viscosity = _f(data.get("viscosity"))
            self.average_viscosity = _f(data.get("averageViscosity"))

            try:
                if hasattr(self, "_ros_node") and self._ros_node is not None:
                    props = [
                        "in_temperature","out_temperature","pt100_temperature","sensor_average_temperature",
                        "target_temperature","setting_temperature","viscosity","average_viscosity",
                        "speed","force"
                    ]
                    for name in props:
                        pub = self._ros_node._property_publishers.get(name)
                        if pub:
                            pub.publish_property()
                    frame = data.get("frameCode")
                    reactor_id = None
                    try:
                        reactor_id = self._frame_to_reactor_id.get(int(frame))
                    except Exception:
                        reactor_id = None
                    if reactor_id and hasattr(self._ros_node, "sub_devices"):
                        child = self._ros_node.sub_devices.get(reactor_id)
                        if child and hasattr(child, "driver_instance"):
                            child.driver_instance.update_metrics(data)
                            pubs = getattr(child.ros_node_instance, "_property_publishers", {})
                            for name in props:
                                p = pubs.get(name)
                                if p:
                                    p.publish_property()
            except Exception:
                pass
            event = {
                "frameCode": data.get("frameCode"),
                "generateTime": data.get("generateTime"),
                "targetTemperature": data.get("targetTemperature"),
                "settingTemperature": data.get("settingTemperature"),
                "inTemperature": data.get("inTemperature"),
                "outTemperature": data.get("outTemperature"),
                "pt100Temperature": data.get("pt100Temperature"),
                "sensorAverageTemperature": data.get("sensorAverageTemperature"),
                "speed": data.get("speed"),
                "force": data.get("force"),
                "viscosity": data.get("viscosity"),
                "averageViscosity": data.get("averageViscosity"),
                "request_time": report_request.request_time,
                "timestamp": datetime.now().isoformat(),
                "reactor_id": self._frame_to_reactor_id.get(int(data.get("frameCode", 0))) if str(data.get("frameCode", "")).isdigit() else None,
            }

            base_dir = Path(__file__).resolve().parents[3] / "unilabos_data"
            base_dir.mkdir(parents=True, exist_ok=True)
            out_file = base_dir / "temperature_cutoff_events.json"
            try:
                existing = json.loads(out_file.read_text(encoding="utf-8")) if out_file.exists() else []
                if not isinstance(existing, list):
                    existing = []
            except Exception:
                existing = []
            existing.append(event)
            out_file.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")

            if hasattr(self, "_ros_node") and self._ros_node is not None:
                ns = self._ros_node.namespace
                topics = {
                    "targetTemperature": f"{ns}/metrics/temperature_cutoff/target_temperature",
                    "settingTemperature": f"{ns}/metrics/temperature_cutoff/setting_temperature",
                    "inTemperature": f"{ns}/metrics/temperature_cutoff/in_temperature",
                    "outTemperature": f"{ns}/metrics/temperature_cutoff/out_temperature",
                    "pt100Temperature": f"{ns}/metrics/temperature_cutoff/pt100_temperature",
                    "sensorAverageTemperature": f"{ns}/metrics/temperature_cutoff/sensor_average_temperature",
                    "speed": f"{ns}/metrics/temperature_cutoff/speed",
                    "force": f"{ns}/metrics/temperature_cutoff/force",
                    "viscosity": f"{ns}/metrics/temperature_cutoff/viscosity",
                    "averageViscosity": f"{ns}/metrics/temperature_cutoff/average_viscosity",
                }
                for k, t in topics.items():
                    v = data.get(k)
                    if v is not None:
                        pub = self._ros_node.create_publisher(Float64, t, 10)
                        pub.publish(convert_to_ros_msg(Float64, float(v)))

                evt_pub = self._ros_node.create_publisher(String, f"{ns}/events/temperature_cutoff", 10)
                evt_pub.publish(convert_to_ros_msg(String, json.dumps(event, ensure_ascii=False)))

            return {"processed": True, "frame": data.get("frameCode")}
        except Exception as e:
            return {"processed": False, "error": str(e)}

    def wait_for_multiple_orders_and_get_reports(self, batch_create_result: str = None, timeout: int = 7200, check_interval: int = 10) -> Dict[str, Any]:
        try:
            timeout = int(timeout) if timeout else 7200
            check_interval = int(check_interval) if check_interval else 10
            if not batch_create_result or batch_create_result == "":
                raise ValueError("batch_create_result为空")
            try:
                if isinstance(batch_create_result, str) and '[...]' in batch_create_result:
                    batch_create_result = batch_create_result.replace('[...]', '[]')
                result_obj = json.loads(batch_create_result) if isinstance(batch_create_result, str) else batch_create_result
                if isinstance(result_obj, dict) and "return_value" in result_obj:
                    inner = result_obj.get("return_value")
                    if isinstance(inner, str):
                        result_obj = json.loads(inner)
                    elif isinstance(inner, dict):
                        result_obj = inner
                order_codes = result_obj.get("order_codes", [])
                order_ids = result_obj.get("order_ids", [])
            except Exception as e:
                raise ValueError(f"解析batch_create_result失败: {e}")
            if not order_codes or not order_ids:
                raise ValueError("缺少order_codes或order_ids")
            if not isinstance(order_codes, list):
                order_codes = [order_codes]
            if not isinstance(order_ids, list):
                order_ids = [order_ids]
            if len(order_codes) != len(order_ids):
                raise ValueError("order_codes与order_ids数量不匹配")
            total = len(order_codes)
            pending = {c: {"order_id": order_ids[i], "completed": False} for i, c in enumerate(order_codes)}
            reports = []
            start_time = time.time()
            while pending:
                elapsed_time = time.time() - start_time
                if elapsed_time > timeout:
                    for oc in list(pending.keys()):
                        reports.append({
                            "order_code": oc,
                            "order_id": pending[oc]["order_id"],
                            "status": "timeout",
                            "completion_status": None,
                            "report": None,
                            "extracted": None,
                            "elapsed_time": elapsed_time
                        })
                    break
                completed_round = []
                for oc in list(pending.keys()):
                    oid = pending[oc]["order_id"]
                    if oc in self.order_completion_status:
                        info = self.order_completion_status[oc]
                        try:
                            rep = self.hardware_interface.order_report(oid)
                            if not rep:
                                rep = {"error": "无法获取报告"}
                            reports.append({
                                "order_code": oc,
                                "order_id": oid,
                                "status": "completed",
                                "completion_status": info.get('status'),
                                "report": rep,
                                "extracted": self._extract_actuals_from_report(rep),
                                "elapsed_time": elapsed_time
                            })
                            completed_round.append(oc)
                            del self.order_completion_status[oc]
                        except Exception as e:
                            reports.append({
                                "order_code": oc,
                                "order_id": oid,
                                "status": "error",
                                "completion_status": info.get('status') if 'info' in locals() else None,
                                "report": None,
                                "extracted": None,
                                "error": str(e),
                                "elapsed_time": elapsed_time
                            })
                            completed_round.append(oc)
                for oc in completed_round:
                    del pending[oc]
                if pending:
                    time.sleep(check_interval)
            completed_count = sum(1 for r in reports if r['status'] == 'completed')
            timeout_count = sum(1 for r in reports if r['status'] == 'timeout')
            error_count = sum(1 for r in reports if r['status'] == 'error')
            final_elapsed_time = time.time() - start_time
            summary = {
                "total": total,
                "completed": completed_count,
                "timeout": timeout_count,
                "error": error_count,
                "elapsed_time": round(final_elapsed_time, 2),
                "reports": reports
            }
            return {
                "return_info": json.dumps(summary, ensure_ascii=False)
            }
        except Exception as e:
            raise

    def liquid_feeding_beaker(
        self,
        volume: str = "350",
        assign_material_name: str = "BAPP",
        time: str = "0",
        torque_variation: int = 1,
        titration_type: str = "1",
        temperature: float = 25.00
    ):
        """液体进料烧杯

        Args:
            volume: 分液质量(g)
            assign_material_name: 物料名称(试剂瓶位)
            time: 观察时间(分钟)
            torque_variation: 是否观察(int类型, 1=否, 2=是)
            titration_type: 是否滴定(1=否, 2=是)
            temperature: 温度设定(°C)
        """
        self.append_to_workflow_sequence('{"web_workflow_name": "liquid_feeding_beaker"}')
        material_id = self.hardware_interface._get_material_id_by_name(assign_material_name)
        if material_id is None:
            raise ValueError(f"无法找到物料 {assign_material_name} 的 ID")

        if isinstance(temperature, str):
            temperature = float(temperature)

        liquid_step_id = WORKFLOW_STEP_IDS["liquid_feeding_beaker"]["liquid"]
        observe_step_id = WORKFLOW_STEP_IDS["liquid_feeding_beaker"]["observe"]

        params = {
            "param_values": {
                liquid_step_id: {
                    ACTION_NAMES["liquid_feeding_beaker"]["liquid"]: [
                        {"m": 0, "n": 2, "Key": "volume", "Value": volume},
                        {"m": 0, "n": 2, "Key": "assignMaterialName", "Value": material_id},
                        {"m": 0, "n": 2, "Key": "titrationType", "Value": titration_type}
                    ]
                },
                observe_step_id: {
                    ACTION_NAMES["liquid_feeding_beaker"]["observe"]: [
                        {"m": 1, "n": 0, "Key": "time", "Value": time},
                        {"m": 1, "n": 0, "Key": "torqueVariation", "Value": str(torque_variation)},
                        {"m": 1, "n": 0, "Key": "temperature", "Value": f"{temperature:.2f}"}
                    ]
                }
            }
        }

        self.pending_task_params.append(params)
        print(f"成功添加液体进料烧杯参数: volume={volume}μL, material={assign_material_name}->ID:{material_id}")
        print(f"当前队列长度: {len(self.pending_task_params)}")
        return json.dumps({"suc": True})

    def drip_back(
        self,
        assign_material_name: str,
        volume: str,
        titration_type: str = "1",
        time: str = "90",
        torque_variation: int = 2,
        temperature: float = 25.00
    ):
        """滴回去

        Args:
            assign_material_name: 物料名称(液体种类)
            volume: 分液量(μL)
            titration_type: 是否滴定(1=否, 2=是)
            time: 观察时间(分钟)
            torque_variation: 是否观察(int类型, 1=否, 2=是)
            temperature: 温度(°C)
        """
        self.append_to_workflow_sequence('{"web_workflow_name": "drip_back"}')
        material_id = self.hardware_interface._get_material_id_by_name(assign_material_name)
        if material_id is None:
            raise ValueError(f"无法找到物料 {assign_material_name} 的 ID")

        if isinstance(temperature, str):
            temperature = float(temperature)

        liquid_step_id = WORKFLOW_STEP_IDS["drip_back"]["liquid"]
        observe_step_id = WORKFLOW_STEP_IDS["drip_back"]["observe"]

        params = {
            "param_values": {
                liquid_step_id: {
                    ACTION_NAMES["drip_back"]["liquid"]: [
                        {"m": 0, "n": 1, "Key": "titrationType", "Value": titration_type},
                        {"m": 0, "n": 1, "Key": "assignMaterialName", "Value": material_id},
                        {"m": 0, "n": 1, "Key": "volume", "Value": volume}
                    ]
                },
                observe_step_id: {
                    ACTION_NAMES["drip_back"]["observe"]: [
                        {"m": 1, "n": 0, "Key": "time", "Value": time},
                        {"m": 1, "n": 0, "Key": "torqueVariation", "Value": str(torque_variation)},
                        {"m": 1, "n": 0, "Key": "temperature", "Value": f"{temperature:.2f}"}
                    ]
                }
            }
        }

        self.pending_task_params.append(params)
        print(f"成功添加滴回去参数: material={assign_material_name}->ID:{material_id}, volume={volume}μL")
        print(f"当前队列长度: {len(self.pending_task_params)}")
        return json.dumps({"suc": True})

    # ==================== 工作流管理方法 ====================

    def get_workflow_sequence(self) -> List[str]:
        """获取当前工作流执行顺序

        Returns:
            工作流名称列表
        """
        id_to_name = {workflow_id: name for name, workflow_id in self.workflow_mappings.items()}
        workflow_names = []
        for workflow_id in self.workflow_sequence:
            workflow_name = id_to_name.get(workflow_id, workflow_id)
            workflow_names.append(workflow_name)
        print(f"工作流序列: {workflow_names}")
        return workflow_names

    def workflow_step_query(self, workflow_id: str) -> dict:
        """查询工作流步骤参数

        Args:
            workflow_id: 工作流ID

        Returns:
            工作流步骤参数字典
        """
        return self.hardware_interface.workflow_step_query(workflow_id)

    def create_order(self, json_str: str) -> dict:
        """创建订单

        Args:
            json_str: 订单参数的JSON字符串

        Returns:
            创建结果
        """
        return self.hardware_interface.create_order(json_str)

    def hard_delete_merged_workflows(self, workflow_ids: List[str]) -> Dict[str, Any]:
        """
        调用新接口：硬删除合并后的工作流

        Args:
            workflow_ids: 要删除的工作流ID数组

        Returns:
            删除结果
        """
        try:
            if not isinstance(workflow_ids, list):
                raise ValueError("workflow_ids必须是字符串数组")
            return self._delete_project_api("/api/lims/order/workflows", workflow_ids)
        except Exception as e:
            print(f"❌ 硬删除异常: {str(e)}")
            return {"code": 0, "message": str(e), "timestamp": int(time.time())}

    # ==================== 项目接口通用方法 ====================

    def _post_project_api(self, endpoint: str, data: Any) -> Dict[str, Any]:
        """项目接口通用POST调用

        参数:
            endpoint: 接口路径（例如 /api/lims/order/skip-titration-steps）
            data: 请求体中的 data 字段内容

        返回:
            dict: 服务端响应，失败时返回 {code:0,message,...}
        """
        request_data = {
            "apiKey": API_CONFIG["api_key"],
            "requestTime": self.hardware_interface.get_current_time_iso8601(),
            "data": data
        }
        print(f"\n📤 项目POST请求: {self.hardware_interface.host}{endpoint}")
        print(json.dumps(request_data, indent=4, ensure_ascii=False))
        try:
            response = requests.post(
                f"{self.hardware_interface.host}{endpoint}",
                json=request_data,
                headers={"Content-Type": "application/json"},
                timeout=30
            )
            result = response.json()
            if result.get("code") == 1:
                print("✅ 请求成功")
            else:
                print(f"❌ 请求失败: {result.get('message','未知错误')}")
            return result
        except json.JSONDecodeError:
            print("❌ 非JSON响应")
            return {"code": 0, "message": "非JSON响应", "timestamp": int(time.time())}
        except requests.exceptions.Timeout:
            print("❌ 请求超时")
            return {"code": 0, "message": "请求超时", "timestamp": int(time.time())}
        except requests.exceptions.RequestException as e:
            print(f"❌ 网络异常: {str(e)}")
            return {"code": 0, "message": str(e), "timestamp": int(time.time())}

    def _delete_project_api(self, endpoint: str, data: Any) -> Dict[str, Any]:
        """项目接口通用DELETE调用

        参数:
            endpoint: 接口路径（例如 /api/lims/order/workflows）
            data: 请求体中的 data 字段内容

        返回:
            dict: 服务端响应，失败时返回 {code:0,message,...}
        """
        request_data = {
            "apiKey": API_CONFIG["api_key"],
            "requestTime": self.hardware_interface.get_current_time_iso8601(),
            "data": data
        }
        print(f"\n📤 项目DELETE请求: {self.hardware_interface.host}{endpoint}")
        print(json.dumps(request_data, indent=4, ensure_ascii=False))
        try:
            response = requests.delete(
                f"{self.hardware_interface.host}{endpoint}",
                json=request_data,
                headers={"Content-Type": "application/json"},
                timeout=30
            )
            result = response.json()
            if result.get("code") == 1:
                print("✅ 请求成功")
            else:
                print(f"❌ 请求失败: {result.get('message','未知错误')}")
            return result
        except json.JSONDecodeError:
            print("❌ 非JSON响应")
            return {"code": 0, "message": "非JSON响应", "timestamp": int(time.time())}
        except requests.exceptions.Timeout:
            print("❌ 请求超时")
            return {"code": 0, "message": "请求超时", "timestamp": int(time.time())}
        except requests.exceptions.RequestException as e:
            print(f"❌ 网络异常: {str(e)}")
            return {"code": 0, "message": str(e), "timestamp": int(time.time())}

    # ==================== 工作流执行核心方法 ====================

    def process_web_workflows(self, web_workflow_json: str) -> List[Dict[str, str]]:
        """处理网页工作流列表

        Args:
            web_workflow_json: JSON 格式的网页工作流列表

        Returns:
            List[Dict[str, str]]: 包含工作流 ID 和名称的字典列表
        """
        try:
            web_workflow_data = json.loads(web_workflow_json)
            web_workflow_list = web_workflow_data.get("web_workflow_list", [])
            workflows_result = []
            for name in web_workflow_list:
                workflow_id = self.workflow_mappings.get(name, "")
                if not workflow_id:
                    print(f"警告：未找到工作流名称 {name} 对应的 ID")
                    continue
                workflows_result.append({"id": workflow_id, "name": name})
            print(f"process_web_workflows 输出: {workflows_result}")
            return workflows_result
        except json.JSONDecodeError as e:
            print(f"错误：无法解析 web_workflow_json: {e}")
            return []
        except Exception as e:
            print(f"错误：处理工作流失败: {e}")
            return []

    def _build_workflows_with_parameters(self, workflows_result: list) -> list:
        """
        构建带参数的工作流列表

        Args:
            workflows_result: 处理后的工作流列表（应为包含 id 和 name 的字典列表）

        Returns:
            符合新接口格式的工作流参数结构
        """
        workflows_with_params = []
        total_params = 0
        successful_params = 0
        failed_params = []

        for idx, workflow_info in enumerate(workflows_result):
            if not isinstance(workflow_info, dict):
                print(f"错误：workflows_result[{idx}] 不是字典，而是 {type(workflow_info)}: {workflow_info}")
                continue
            workflow_id = workflow_info.get("id")
            if not workflow_id:
                print(f"警告：workflows_result[{idx}] 缺少 'id' 键")
                continue
            workflow_name = workflow_info.get("name", "")
            # print(f"\n🔧 处理工作流 [{idx}]: {workflow_name} (ID: {workflow_id})")

            if idx >= len(self.pending_task_params):
                # print(f"   ⚠️ 无对应参数，跳过")
                workflows_with_params.append({"id": workflow_id})
                continue

            param_data = self.pending_task_params[idx]
            param_values = param_data.get("param_values", {})
            if not param_values:
                # print(f"   ⚠️ 参数为空，跳过")
                workflows_with_params.append({"id": workflow_id})
                continue

            step_parameters = {}
            for step_id, actions_dict in param_values.items():
                # print(f"   📍 步骤ID: {step_id}")
                for action_name, param_list in actions_dict.items():
                    # print(f"      🔹 模块: {action_name}, 参数数量: {len(param_list)}")
                    if step_id not in step_parameters:
                        step_parameters[step_id] = {}
                    if action_name not in step_parameters[step_id]:
                        step_parameters[step_id][action_name] = []
                    for param_item in param_list:
                        param_key = param_item.get("Key", "")
                        param_value = param_item.get("Value", "")
                        total_params += 1
                        step_parameters[step_id][action_name].append({
                            "Key": param_key,
                            "DisplayValue": param_value,
                            "Value": param_value
                        })
                        successful_params += 1
                        # print(f"         ✓ {param_key} = {param_value}")

            workflows_with_params.append({
                "id": workflow_id,
                "stepParameters": step_parameters
            })

        self._print_mapping_stats(total_params, successful_params, failed_params)
        return workflows_with_params

    def _print_mapping_stats(self, total: int, success: int, failed: list):
        """打印参数映射统计"""
        print(f"\n{'='*20} 参数映射统计 {'='*20}")
        print(f"📊 总参数数量: {total}")
        print(f"✅ 成功映射: {success}")
        print(f"❌ 映射失败: {len(failed)}")
        if not failed:
            print("🎉 成功映射所有参数！")
        else:
            print(f"⚠️ 失败的参数: {', '.join(failed)}")
        success_rate = (success/total*100) if total > 0 else 0
        print(f"📈 映射成功率: {success_rate:.1f}%")
        print("="*60)

    def _create_error_result(self, error_msg: str, step: str) -> str:
        """创建统一的错误返回格式"""
        print(f"❌ {error_msg}")
        return json.dumps({
            "success": False,
            "error": f"process_and_execute_workflow: {error_msg}",
            "method": "process_and_execute_workflow",
            "step": step
        })

    def merge_workflow_with_parameters(self, json_str: str) -> dict:
        """
        调用新接口：合并工作流并传递参数

        Args:
            json_str: JSON格式的字符串，包含:
                - name: 工作流名称
                - workflows: [{"id": "工作流ID", "stepParameters": {...}}]

        Returns:
            合并后的工作流信息
        """
        try:
            data = json.loads(json_str)

            # 在工作流名称后面添加时间戳，避免重复
            if "name" in data and data["name"]:
                timestamp = self.hardware_interface.get_current_time_iso8601().replace(":", "-").replace(".", "-")
                original_name = data["name"]
                data["name"] = f"{original_name}_{timestamp}"
                print(f"🕒 工作流名称已添加时间戳: {original_name} -> {data['name']}")

            request_data = {
                "apiKey": API_CONFIG["api_key"],
                "requestTime": self.hardware_interface.get_current_time_iso8601(),
                "data": data
            }
            print(f"\n📤 发送合并请求:")
            print(f"   工作流名称: {data.get('name')}")
            print(f"   子工作流数量: {len(data.get('workflows', []))}")

            # 打印完整的POST请求内容
            print(f"\n🔍 POST请求详细内容:")
            print(f"   URL: {self.hardware_interface.host}/api/lims/workflow/merge-workflow-with-parameters")
            print(f"   Headers: {{'Content-Type': 'application/json'}}")
            print(f"   Request Data:")
            print(f"   {json.dumps(request_data, indent=4, ensure_ascii=False)}")
            #
            response = requests.post(
                f"{self.hardware_interface.host}/api/lims/workflow/merge-workflow-with-parameters",
                json=request_data,
                headers={"Content-Type": "application/json"},
                timeout=30
            )

            # # 打印响应详细内容
            # print(f"\n📥 POST响应详细内容:")
            # print(f"   状态码: {response.status_code}")
            # print(f"   响应头: {dict(response.headers)}")
            # print(f"   响应体: {response.text}")
            # #
            try:
                result = response.json()
                # #
                # print(f"\n📋 解析后的响应JSON:")
                # print(f"   {json.dumps(result, indent=4, ensure_ascii=False)}")
                # #
            except json.JSONDecodeError:
                print(f"❌ 服务器返回非 JSON 格式响应: {response.text}")
                return None

            if result.get("code") == 1:
                print(f"✅ 工作流合并成功（带参数）")
                return result.get("data", {})
            else:
                error_msg = result.get('message', '未知错误')
                print(f"❌ 工作流合并失败: {error_msg}")
                return None

        except requests.exceptions.Timeout:
            print(f"❌ 合并工作流请求超时")
            return None
        except requests.exceptions.RequestException as e:
            print(f"❌ 合并工作流网络异常: {str(e)}")
            return None
        except json.JSONDecodeError as e:
            print(f"❌ 合并工作流响应解析失败: {str(e)}")
            return None
        except Exception as e:
            print(f"❌ 合并工作流异常: {str(e)}")
            return None

    def _validate_and_refresh_workflow_if_needed(self, workflow_name: str) -> bool:
        """验证工作流ID是否有效，如果无效则重新合并

        Args:
            workflow_name: 工作流名称

        Returns:
            bool: 验证或刷新是否成功
        """
        print(f"\n🔍 验证工作流ID有效性...")
        if not self.workflow_sequence:
            print(f"   ⚠️ 工作流序列为空，需要重新合并")
            return False
        first_workflow_id = self.workflow_sequence[0]
        try:
            structure = self.workflow_step_query(first_workflow_id)
            if structure:
                print(f"   ✅ 工作流ID有效")
                return True
            else:
                print(f"   ⚠️ 工作流ID已过期，需要重新合并")
                return False
        except Exception as e:
            print(f"   ❌ 工作流ID验证失败: {e}")
            print(f"   💡 将重新合并工作流")
            return False

    def process_and_execute_workflow(self, workflow_name: str, task_name: str) -> dict:
        """
        一站式处理工作流程：解析网页工作流列表，合并工作流(带参数)，然后发布任务

        Args:
            workflow_name: 合并后的工作流名称
            task_name: 任务名称

        Returns:
            任务创建结果
        """
        web_workflow_list = self.get_workflow_sequence()
        print(f"\n{'='*60}")
        print(f"📋 处理网页工作流列表: {web_workflow_list}")
        print(f"{'='*60}")

        web_workflow_json = json.dumps({"web_workflow_list": web_workflow_list})
        workflows_result = self.process_web_workflows(web_workflow_json)

        if not workflows_result:
            return self._create_error_result("处理网页工作流列表失败", "process_web_workflows")

        print(f"workflows_result 类型: {type(workflows_result)}")
        print(f"workflows_result 内容: {workflows_result}")

        workflows_with_params = self._build_workflows_with_parameters(workflows_result)

        merge_data = {
            "name": workflow_name,
            "workflows": workflows_with_params
        }

        # print(f"\n🔄 合并工作流（带参数），名称: {workflow_name}")
        merged_workflow = self.merge_workflow_with_parameters(json.dumps(merge_data))

        if not merged_workflow:
            return self._create_error_result("合并工作流失败", "merge_workflow_with_parameters")

        workflow_id = merged_workflow.get("subWorkflows", [{}])[0].get("id", "")
        # print(f"\n📤 使用工作流创建任务: {workflow_name} (ID: {workflow_id})")

        order_params = [{
            "orderCode": f"task_{self.hardware_interface.get_current_time_iso8601()}",
            "orderName": task_name,
            "workFlowId": workflow_id,
            "borderNumber": 1,
            "paramValues": {}
        }]

        result = self.create_order(json.dumps(order_params))

        if not result:
            return self._create_error_result("创建任务失败", "create_order")

        # 清空工作流序列和参数，防止下次执行时累积重复
        self.pending_task_params = []
        self.clear_workflows()  # 清空工作流序列，避免重复累积

        # print(f"\n✅ 任务创建成功: {result}")
        # print(f"\n✅ 任务创建成功")
        print(f"{'='*60}\n")

        # 返回结果，包含合并后的工作流数据和订单参数
        return json.dumps({
            "success": True,
            "result": result,
            "merged_workflow": merged_workflow,
            "order_params": order_params
        })

    # ==================== 反应器操作接口 ====================

    def skip_titration_steps(self, preintake_id: str) -> Dict[str, Any]:
        """跳过当前正在进行的滴定步骤

        Args:
            preintake_id: 通量ID

        Returns:
            Dict[str, Any]: 服务器响应，包含状态码、消息和时间戳
        """
        try:
            return self._post_project_api("/api/lims/order/skip-titration-steps", preintake_id)
        except Exception as e:
            print(f"❌ 跳过滴定异常: {str(e)}")
            return {"code": 0, "message": str(e), "timestamp": int(time.time())}
