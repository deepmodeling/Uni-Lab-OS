from datetime import datetime
import json
import time
from typing import Optional, Dict, Any, List
from typing_extensions import TypedDict
import requests
import pint
from unilabos.devices.workstation.bioyond_studio.config import API_CONFIG

from unilabos.devices.workstation.bioyond_studio.bioyond_rpc import BioyondException
from unilabos.devices.workstation.bioyond_studio.station import BioyondWorkstation
from unilabos.ros.nodes.base_device_node import ROS2DeviceNode, BaseROS2DeviceNode
import json
import sys
from pathlib import Path
import importlib

class ComputeExperimentDesignReturn(TypedDict):
    solutions: list
    titration: dict
    solvents: dict
    feeding_order: list
    return_info: str


class BioyondDispensingStation(BioyondWorkstation):
    def __init__(
        self,
        config,
            # 桌子
        deck,
        *args,
        **kwargs,
        ):
        super().__init__(config, deck, *args, **kwargs)
        # self.config = config
        # self.api_key = config["api_key"]
        # self.host = config["api_host"]
        #
        # # 使用简单的Logger替代原来的logger
        # self._logger = SimpleLogger()
        # self.is_running = False

        # 用于跟踪任务完成状态的字典: {orderCode: {status, order_id, timestamp}}
        self.order_completion_status = {}

        # 初始化 pint 单位注册表
        self.ureg = pint.UnitRegistry()

        # 化合物信息
        self.compound_info = {
            "MolWt": {
                "MDA": 108.14 * self.ureg.g / self.ureg.mol,
                "TDA": 122.16 * self.ureg.g / self.ureg.mol,
                "PAPP": 521.62 * self.ureg.g / self.ureg.mol,
                "BTDA": 322.23 * self.ureg.g / self.ureg.mol,
                "BPDA": 294.22 * self.ureg.g / self.ureg.mol,
                "6FAP": 366.26 * self.ureg.g / self.ureg.mol,
                "PMDA": 218.12 * self.ureg.g / self.ureg.mol,
                "MPDA": 108.14 * self.ureg.g / self.ureg.mol,
                "SIDA": 248.51 * self.ureg.g / self.ureg.mol,
                "ODA": 200.236 * self.ureg.g / self.ureg.mol,
                "4,4'-ODA": 200.236 * self.ureg.g / self.ureg.mol,
                "134": 292.34 * self.ureg.g / self.ureg.mol,
            },
            "FuncGroup": {
                "MDA": "Amine",
                "TDA": "Amine",
                "PAPP": "Amine",
                "BTDA": "Anhydride",
                "BPDA": "Anhydride",
                "6FAP": "Amine",
                "MPDA": "Amine",
                "SIDA": "Amine",
                "PMDA": "Anhydride",
                "ODA": "Amine",
                "4,4'-ODA": "Amine",
                "134": "Amine",
            }
        }

    def _post_project_api(self, endpoint: str, data: Any) -> Dict[str, Any]:
        """项目接口通用POST调用

        参数:
            endpoint: 接口路径（例如 /api/lims/order/brief-step-paramerers）
            data: 请求体中的 data 字段内容

        返回:
            dict: 服务端响应，失败时返回 {code:0,message,...}
        """
        request_data = {
            "apiKey": API_CONFIG["api_key"],
            "requestTime": self.hardware_interface.get_current_time_iso8601(),
            "data": data
        }
        try:
            response = requests.post(
                f"{self.hardware_interface.host}{endpoint}",
                json=request_data,
                headers={"Content-Type": "application/json"},
                timeout=30
            )
            result = response.json()
            return result if isinstance(result, dict) else {"code": 0, "message": "非JSON响应"}
        except json.JSONDecodeError:
            return {"code": 0, "message": "非JSON响应"}
        except requests.exceptions.Timeout:
            return {"code": 0, "message": "请求超时"}
        except requests.exceptions.RequestException as e:
            return {"code": 0, "message": str(e)}

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
        try:
            response = requests.delete(
                f"{self.hardware_interface.host}{endpoint}",
                json=request_data,
                headers={"Content-Type": "application/json"},
                timeout=30
            )
            result = response.json()
            return result if isinstance(result, dict) else {"code": 0, "message": "非JSON响应"}
        except json.JSONDecodeError:
            return {"code": 0, "message": "非JSON响应"}
        except requests.exceptions.Timeout:
            return {"code": 0, "message": "请求超时"}
        except requests.exceptions.RequestException as e:
            return {"code": 0, "message": str(e)}

    def compute_experiment_design(
        self,
        ratio: dict,
        wt_percent: str = "0.25",
        m_tot: str = "70",
        titration_percent: str = "0.03",
    ) -> ComputeExperimentDesignReturn:
        try:
            if isinstance(ratio, str):
                try:
                    ratio = json.loads(ratio)
                except Exception:
                    ratio = {}
            try:
                wp = float(wt_percent) if isinstance(wt_percent, str) else wt_percent
                mt = float(m_tot) if isinstance(m_tot, str) else m_tot
                tp = float(titration_percent) if isinstance(titration_percent, str) else titration_percent
            except Exception as e:
                raise BioyondException(f"参数解析失败: {e}")

            # 2. 调用内部计算方法
            res = self._generate_experiment_design(
                ratio=ratio,
                wt_percent=wp,
                m_tot=mt,
                titration_percent=tp
            )

            # 3. 构造返回结果
            out = {
                "solutions": res.get("solutions", []),
                "titration": res.get("titration", {}),
                "solvents": res.get("solvents", {}),
                "feeding_order": res.get("feeding_order", []),
                "return_info": json.dumps(res, ensure_ascii=False)
            }
            return out

        except BioyondException:
            raise
        except Exception as e:
            raise BioyondException(str(e))

    def _generate_experiment_design(
        self,
        ratio: dict,
        wt_percent: float = 0.25,
        m_tot: float = 70,
        titration_percent: float = 0.03,
    ) -> dict:
        """内部方法：生成实验设计

        根据FuncGroup自动区分二胺和二酐，每种二胺单独配溶液，严格按照ratio顺序投料。

        参数:
            ratio: 化合物配比字典，格式: {"compound_name": ratio_value}
            wt_percent: 固体重量百分比
            m_tot: 反应混合物总质量(g)
            titration_percent: 滴定溶液百分比

        返回:
            包含实验设计详细参数的字典
        """
        # 溶剂密度
        ρ_solvent = 1.03 * self.ureg.g / self.ureg.ml
        # 二酐溶解度
        solubility = 0.02 * self.ureg.g / self.ureg.ml
        # 投入固体时最小溶剂体积
        V_min = 30 * self.ureg.ml
        m_tot = m_tot * self.ureg.g

        # 保持ratio中的顺序
        compound_names = list(ratio.keys())
        compound_ratios = list(ratio.values())

        # 验证所有化合物是否在 compound_info 中定义
        undefined_compounds = [name for name in compound_names if name not in self.compound_info["MolWt"]]
        if undefined_compounds:
            available = list(self.compound_info["MolWt"].keys())
            raise ValueError(
                f"以下化合物未在 compound_info 中定义: {undefined_compounds}。"
                f"可用的化合物: {available}"
            )

        # 获取各化合物的分子量和官能团类型
        molecular_weights = [self.compound_info["MolWt"][name] for name in compound_names]
        func_groups = [self.compound_info["FuncGroup"][name] for name in compound_names]

        # 记录化合物信息用于调试
        self.hardware_interface._logger.info(f"化合物名称: {compound_names}")
        self.hardware_interface._logger.info(f"官能团类型: {func_groups}")

        # 按原始顺序分离二胺和二酐
        ordered_compounds = list(zip(compound_names, compound_ratios, molecular_weights, func_groups))
        diamine_compounds = [(name, ratio_val, mw, i) for i, (name, ratio_val, mw, fg) in enumerate(ordered_compounds) if fg == "Amine"]
        anhydride_compounds = [(name, ratio_val, mw, i) for i, (name, ratio_val, mw, fg) in enumerate(ordered_compounds) if fg == "Anhydride"]

        if not diamine_compounds or not anhydride_compounds:
            raise ValueError(
                f"需要同时包含二胺(Amine)和二酐(Anhydride)化合物。"
                f"当前二胺: {[c[0] for c in diamine_compounds]}, "
                f"当前二酐: {[c[0] for c in anhydride_compounds]}"
            )

        # 计算加权平均分子量 (基于摩尔比)
        total_molar_ratio = sum(compound_ratios)
        weighted_molecular_weight = sum(ratio_val * mw for ratio_val, mw in zip(compound_ratios, molecular_weights))

        # 取最后一个二酐用于滴定
        titration_anhydride = anhydride_compounds[-1]
        solid_anhydrides = anhydride_compounds[:-1] if len(anhydride_compounds) > 1 else []

        # 二胺溶液配制参数 - 每种二胺单独配制
        diamine_solutions = []
        total_diamine_volume = 0 * self.ureg.ml

        # 计算反应物的总摩尔量
        n_reactant = m_tot * wt_percent / weighted_molecular_weight

        for name, ratio_val, mw, order_index in diamine_compounds:
            # 跳过 SIDA
            if name == "SIDA":
                continue

            # 计算该二胺需要的摩尔数
            n_diamine_needed = n_reactant * ratio_val

            # 二胺溶液配制参数 (每种二胺固定配制参数)
            m_diamine_solid = 5.0 * self.ureg.g  # 每种二胺固体质量
            V_solvent_for_this = 20 * self.ureg.ml  # 每种二胺溶剂体积
            m_solvent_for_this = ρ_solvent * V_solvent_for_this

            # 计算该二胺溶液的浓度
            c_diamine = (m_diamine_solid / mw) / V_solvent_for_this

            # 计算需要移取的溶液体积
            V_diamine_needed = n_diamine_needed / c_diamine

            diamine_solutions.append({
                "name": name,
                "order": order_index,
                "solid_mass": m_diamine_solid.magnitude,
                "solvent_volume": V_solvent_for_this.magnitude,
                "concentration": c_diamine.magnitude,
                "volume_needed": V_diamine_needed.magnitude,
                "molar_ratio": ratio_val
            })

            total_diamine_volume += V_diamine_needed

        # 按原始顺序排序
        diamine_solutions.sort(key=lambda x: x["order"])

        # 计算滴定二酐的质量
        titration_name, titration_ratio, titration_mw, _ = titration_anhydride
        m_titration_anhydride = n_reactant * titration_ratio * titration_mw
        m_titration_90 = m_titration_anhydride * (1 - titration_percent)
        m_titration_10 = m_titration_anhydride * titration_percent

        # 计算其他固体二酐的质量 (按顺序)
        solid_anhydride_masses = []
        for name, ratio_val, mw, order_index in solid_anhydrides:
            mass = n_reactant * ratio_val * mw
            solid_anhydride_masses.append({
                "name": name,
                "order": order_index,
                "mass": mass.magnitude,
                "molar_ratio": ratio_val
            })

        # 按原始顺序排序
        solid_anhydride_masses.sort(key=lambda x: x["order"])

        # 计算溶剂用量
        total_diamine_solution_mass = sum(
            sol["volume_needed"] * ρ_solvent for sol in diamine_solutions
        ) * self.ureg.ml

        # 预估滴定溶剂量、计算补加溶剂量
        m_solvent_titration = m_titration_10 / solubility * ρ_solvent
        m_solvent_add = m_tot * (1 - wt_percent) - total_diamine_solution_mass - m_solvent_titration

        # 检查最小溶剂体积要求
        total_liquid_volume = (total_diamine_solution_mass + m_solvent_add) / ρ_solvent
        m_tot_min = V_min / total_liquid_volume * m_tot

        # 如果需要，按比例放大
        scale_factor = 1.0
        if m_tot_min > m_tot:
            scale_factor = (m_tot_min / m_tot).magnitude
            m_titration_90 *= scale_factor
            m_titration_10 *= scale_factor
            m_solvent_add *= scale_factor
            m_solvent_titration *= scale_factor

            # 更新二胺溶液用量
            for sol in diamine_solutions:
                sol["volume_needed"] *= scale_factor

            # 更新固体二酐用量
            for anhydride in solid_anhydride_masses:
                anhydride["mass"] *= scale_factor

            m_tot = m_tot_min

        # 生成投料顺序
        feeding_order = []

        # 1. 固体二酐 (按顺序)
        for anhydride in solid_anhydride_masses:
            feeding_order.append({
                "step": len(feeding_order) + 1,
                "type": "solid_anhydride",
                "name": anhydride["name"],
                "amount": anhydride["mass"],
                "order": anhydride["order"]
            })

        # 2. 二胺溶液 (按顺序)
        for sol in diamine_solutions:
            feeding_order.append({
                "step": len(feeding_order) + 1,
                "type": "diamine_solution",
                "name": sol["name"],
                "amount": sol["volume_needed"],
                "order": sol["order"]
            })

        # 3. 主要二酐粉末
        feeding_order.append({
            "step": len(feeding_order) + 1,
            "type": "main_anhydride",
            "name": titration_name,
            "amount": m_titration_90.magnitude,
            "order": titration_anhydride[3]
        })

        # 4. 补加溶剂
        if m_solvent_add > 0:
            feeding_order.append({
                "step": len(feeding_order) + 1,
                "type": "additional_solvent",
                "name": "溶剂",
                "amount": m_solvent_add.magnitude,
                "order": 999
            })

        # 5. 滴定二酐溶液
        feeding_order.append({
            "step": len(feeding_order) + 1,
            "type": "titration_anhydride",
            "name": f"{titration_name} 滴定液",
            "amount": m_titration_10.magnitude,
            "titration_solvent": m_solvent_titration.magnitude,
            "order": titration_anhydride[3]
        })

        # 返回实验设计结果
        results = {
            "total_mass": m_tot.magnitude,
            "scale_factor": scale_factor,
            "solutions": diamine_solutions,
            "solids": solid_anhydride_masses,
            "titration": {
                "name": titration_name,
                "main_portion": m_titration_90.magnitude,
                "titration_portion": m_titration_10.magnitude,
                "titration_solvent": m_solvent_titration.magnitude,
            },
            "solvents": {
                "additional_solvent": m_solvent_add.magnitude,
                "total_liquid_volume": total_liquid_volume.magnitude
            },
            "feeding_order": feeding_order,
            "minimum_required_mass": m_tot_min.magnitude
        }

        return results

    # 90%10%小瓶投料任务创建方法
    def create_90_10_vial_feeding_task(self,
                                       order_name: str = None,
                                       speed: str = None,
                                       temperature: str = None,
                                       delay_time: str = None,
                                       percent_90_1_assign_material_name: str = None,
                                       percent_90_1_target_weigh: str = None,
                                       percent_90_2_assign_material_name: str = None,
                                       percent_90_2_target_weigh: str = None,
                                       percent_90_3_assign_material_name: str = None,
                                       percent_90_3_target_weigh: str = None,
                                       percent_10_1_assign_material_name: str = None,
                                       percent_10_1_target_weigh: str = None,
                                       percent_10_1_volume: str = None,
                                       percent_10_1_liquid_material_name: str = None,
                                       percent_10_2_assign_material_name: str = None,
                                       percent_10_2_target_weigh: str = None,
                                       percent_10_2_volume: str = None,
                                       percent_10_2_liquid_material_name: str = None,
                                       percent_10_3_assign_material_name: str = None,
                                       percent_10_3_target_weigh: str = None,
                                       percent_10_3_volume: str = None,
                                       percent_10_3_liquid_material_name: str = None,
                                       hold_m_name: str = None) -> dict:
        """
        创建90%10%小瓶投料任务

        参数说明:
        - order_name: 任务名称，如果为None则使用默认名称
        - speed: 搅拌速度，如果为None则使用默认值400
        - temperature: 温度，如果为None则使用默认值40
        - delay_time: 延迟时间，如果为None则使用默认值600
        - percent_90_1_assign_material_name: 90%_1物料名称
        - percent_90_1_target_weigh: 90%_1目标重量
        - percent_90_2_assign_material_name: 90%_2物料名称
        - percent_90_2_target_weigh: 90%_2目标重量
        - percent_90_3_assign_material_name: 90%_3物料名称
        - percent_90_3_target_weigh: 90%_3目标重量
        - percent_10_1_assign_material_name: 10%_1固体物料名称
        - percent_10_1_target_weigh: 10%_1固体目标重量
        - percent_10_1_volume: 10%_1液体体积
        - percent_10_1_liquid_material_name: 10%_1液体物料名称
        - percent_10_2_assign_material_name: 10%_2固体物料名称
        - percent_10_2_target_weigh: 10%_2固体目标重量
        - percent_10_2_volume: 10%_2液体体积
        - percent_10_2_liquid_material_name: 10%_2液体物料名称
        - percent_10_3_assign_material_name: 10%_3固体物料名称
        - percent_10_3_target_weigh: 10%_3固体目标重量
        - percent_10_3_volume: 10%_3液体体积
        - percent_10_3_liquid_material_name: 10%_3液体物料名称
        - hold_m_name: 库位名称，如"C01"，用于查找对应的holdMId

        返回: 任务创建结果

        异常:
        - BioyondException: 各种错误情况下的统一异常
        """
        try:
            # 1. 参数验证
            if not hold_m_name:
                raise BioyondException("hold_m_name 是必填参数")

            # 检查90%物料参数的完整性
            # 90%_1物料：如果有物料名称或目标重量，就必须有全部参数
            if percent_90_1_assign_material_name or percent_90_1_target_weigh:
                if not percent_90_1_assign_material_name:
                    raise BioyondException("90%_1物料：如果提供了目标重量，必须同时提供物料名称")
                if not percent_90_1_target_weigh:
                    raise BioyondException("90%_1物料：如果提供了物料名称，必须同时提供目标重量")

            # 90%_2物料：如果有物料名称或目标重量，就必须有全部参数
            if percent_90_2_assign_material_name or percent_90_2_target_weigh:
                if not percent_90_2_assign_material_name:
                    raise BioyondException("90%_2物料：如果提供了目标重量，必须同时提供物料名称")
                if not percent_90_2_target_weigh:
                    raise BioyondException("90%_2物料：如果提供了物料名称，必须同时提供目标重量")

            # 90%_3物料：如果有物料名称或目标重量，就必须有全部参数
            if percent_90_3_assign_material_name or percent_90_3_target_weigh:
                if not percent_90_3_assign_material_name:
                    raise BioyondException("90%_3物料：如果提供了目标重量，必须同时提供物料名称")
                if not percent_90_3_target_weigh:
                    raise BioyondException("90%_3物料：如果提供了物料名称，必须同时提供目标重量")

            # 检查10%物料参数的完整性
            # 10%_1物料：如果有物料名称、目标重量、体积或液体物料名称中的任何一个，就必须有全部参数
            if any([percent_10_1_assign_material_name, percent_10_1_target_weigh, percent_10_1_volume, percent_10_1_liquid_material_name]):
                if not percent_10_1_assign_material_name:
                    raise BioyondException("10%_1物料：如果提供了其他参数，必须同时提供固体物料名称")
                if not percent_10_1_target_weigh:
                    raise BioyondException("10%_1物料：如果提供了其他参数，必须同时提供固体目标重量")
                if not percent_10_1_volume:
                    raise BioyondException("10%_1物料：如果提供了其他参数，必须同时提供液体体积")
                if not percent_10_1_liquid_material_name:
                    raise BioyondException("10%_1物料：如果提供了其他参数，必须同时提供液体物料名称")

            # 10%_2物料：如果有物料名称、目标重量、体积或液体物料名称中的任何一个，就必须有全部参数
            if any([percent_10_2_assign_material_name, percent_10_2_target_weigh, percent_10_2_volume, percent_10_2_liquid_material_name]):
                if not percent_10_2_assign_material_name:
                    raise BioyondException("10%_2物料：如果提供了其他参数，必须同时提供固体物料名称")
                if not percent_10_2_target_weigh:
                    raise BioyondException("10%_2物料：如果提供了其他参数，必须同时提供固体目标重量")
                if not percent_10_2_volume:
                    raise BioyondException("10%_2物料：如果提供了其他参数，必须同时提供液体体积")
                if not percent_10_2_liquid_material_name:
                    raise BioyondException("10%_2物料：如果提供了其他参数，必须同时提供液体物料名称")

            # 10%_3物料：如果有物料名称、目标重量、体积或液体物料名称中的任何一个，就必须有全部参数
            if any([percent_10_3_assign_material_name, percent_10_3_target_weigh, percent_10_3_volume, percent_10_3_liquid_material_name]):
                if not percent_10_3_assign_material_name:
                    raise BioyondException("10%_3物料：如果提供了其他参数，必须同时提供固体物料名称")
                if not percent_10_3_target_weigh:
                    raise BioyondException("10%_3物料：如果提供了其他参数，必须同时提供固体目标重量")
                if not percent_10_3_volume:
                    raise BioyondException("10%_3物料：如果提供了其他参数，必须同时提供液体体积")
                if not percent_10_3_liquid_material_name:
                    raise BioyondException("10%_3物料：如果提供了其他参数，必须同时提供液体物料名称")

            # 2. 生成任务编码和设置默认值
            order_code = "task_vial_" + str(int(datetime.now().timestamp()))
            if order_name is None:
                order_name = "90%10%小瓶投料任务"
            if speed is None:
                speed = "400"
            if temperature is None:
                temperature = "40"
            if delay_time is None:
                delay_time = "600"

            # 3. 工作流ID
            workflow_id = "3a19310d-16b9-9d81-b109-0748e953694b"

            # 4. 查询工作流对应的holdMID
            material_info = self.hardware_interface.material_id_query(workflow_id)
            if not material_info:
                raise BioyondException(f"无法查询工作流 {workflow_id} 的物料信息")

            # 获取locations列表
            locations = material_info.get("locations", []) if isinstance(material_info, dict) else []
            if not locations:
                raise BioyondException(f"工作流 {workflow_id} 没有找到库位信息")

            # 查找指定名称的库位
            hold_mid = None
            for location in locations:
                if location.get("holdMName") == hold_m_name:
                    hold_mid = location.get("holdMId")
                    break

            if not hold_mid:
                raise BioyondException(f"未找到库位名称为 {hold_m_name} 的库位，请检查名称是否正确")

            extend_properties = f"{{\"{ hold_mid }\": {{}}}}"
            self.hardware_interface._logger.info(f"找到库位 {hold_m_name} 对应的holdMId: {hold_mid}")

            # 5. 构建任务参数
            order_data = {
                "orderCode": order_code,
                "orderName": order_name,
                "workflowId": workflow_id,
                "borderNumber": 1,
                "paramValues": {},
                "ExtendProperties": extend_properties
            }

            # 添加搅拌参数
            order_data["paramValues"]["e8264e47-c319-d9d9-8676-4dd5cb382b11"] = [
                {"m": 0, "n": 3, "Key": "speed", "Value": speed},
                {"m": 0, "n": 3, "Key": "temperature", "Value": temperature}
            ]

            # 添加延迟时间参数
            order_data["paramValues"]["dc5dba79-5e4b-8eae-cbc5-e93482e43b1f"] = [
                {"m": 0, "n": 4, "Key": "DelayTime", "Value": delay_time}
            ]

            # 添加90%_1参数
            if percent_90_1_assign_material_name is not None and percent_90_1_target_weigh is not None:
                order_data["paramValues"]["e7d3c0a3-25c2-c42d-c84b-860c4a5ef844"] = [
                    {"m": 15, "n": 1, "Key": "targetWeigh", "Value": percent_90_1_target_weigh},
                    {"m": 15, "n": 1, "Key": "assignMaterialName", "Value": percent_90_1_assign_material_name}
                ]

            # 添加90%_2参数
            if percent_90_2_assign_material_name is not None and percent_90_2_target_weigh is not None:
                order_data["paramValues"]["50b912c4-6c81-0734-1c8b-532428b2a4a5"] = [
                    {"m": 18, "n": 1, "Key": "targetWeigh", "Value": percent_90_2_target_weigh},
                    {"m": 18, "n": 1, "Key": "assignMaterialName", "Value": percent_90_2_assign_material_name}
                ]

            # 添加90%_3参数
            if percent_90_3_assign_material_name is not None and percent_90_3_target_weigh is not None:
                order_data["paramValues"]["9c3674b3-c7cb-946e-fa03-fa2861d8aec4"] = [
                    {"m": 21, "n": 1, "Key": "targetWeigh", "Value": percent_90_3_target_weigh},
                    {"m": 21, "n": 1, "Key": "assignMaterialName", "Value": percent_90_3_assign_material_name}
                ]

            # 添加10%_1固体参数
            if percent_10_1_assign_material_name is not None and percent_10_1_target_weigh is not None:
                order_data["paramValues"]["73a0bfd8-1967-45e9-4bab-c07ccd1a2727"] = [
                    {"m": 3, "n": 1, "Key": "targetWeigh", "Value": percent_10_1_target_weigh},
                    {"m": 3, "n": 1, "Key": "assignMaterialName", "Value": percent_10_1_assign_material_name}
                ]

            # 添加10%_1液体参数
            if percent_10_1_liquid_material_name is not None and percent_10_1_volume is not None:
                order_data["paramValues"]["39634d40-c623-473a-8e5f-bc301aca2522"] = [
                    {"m": 3, "n": 3, "Key": "volume", "Value": percent_10_1_volume},
                    {"m": 3, "n": 3, "Key": "assignMaterialName", "Value": percent_10_1_liquid_material_name}
                ]

            # 添加10%_2固体参数
            if percent_10_2_assign_material_name is not None and percent_10_2_target_weigh is not None:
                order_data["paramValues"]["2d9c16fa-2a19-cd47-a67b-3cadff9e3e3d"] = [
                    {"m": 7, "n": 1, "Key": "targetWeigh", "Value": percent_10_2_target_weigh},
                    {"m": 7, "n": 1, "Key": "assignMaterialName", "Value": percent_10_2_assign_material_name}
                ]

            # 添加10%_2液体参数
            if percent_10_2_liquid_material_name is not None and percent_10_2_volume is not None:
                order_data["paramValues"]["e60541bb-ed68-e839-7305-2b4abe38a13d"] = [
                    {"m": 7, "n": 3, "Key": "volume", "Value": percent_10_2_volume},
                    {"m": 7, "n": 3, "Key": "assignMaterialName", "Value": percent_10_2_liquid_material_name}
                ]

            # 添加10%_3固体参数
            if percent_10_3_assign_material_name is not None and percent_10_3_target_weigh is not None:
                order_data["paramValues"]["27494733-0f71-a916-7cd2-1929a0125f17"] = [
                    {"m": 11, "n": 1, "Key": "targetWeigh", "Value": percent_10_3_target_weigh},
                    {"m": 11, "n": 1, "Key": "assignMaterialName", "Value": percent_10_3_assign_material_name}
                ]

            # 添加10%_3液体参数
            if percent_10_3_liquid_material_name is not None and percent_10_3_volume is not None:
                order_data["paramValues"]["c8798c29-786f-6858-7d7f-5330b890f2a6"] = [
                    {"m": 11, "n": 3, "Key": "volume", "Value": percent_10_3_volume},
                    {"m": 11, "n": 3, "Key": "assignMaterialName", "Value": percent_10_3_liquid_material_name}
                ]

            # 6. 转换为JSON字符串并创建任务
            json_str = json.dumps([order_data], ensure_ascii=False)
            self.hardware_interface._logger.info(f"创建90%10%小瓶投料任务参数: {json_str}")

            # 7. 调用create_order方法创建任务
            result = self.hardware_interface.create_order(json_str)
            self.hardware_interface._logger.info(f"创建90%10%小瓶投料任务结果: {result}")

            # 8. 解析结果获取order_id
            order_id = None
            if isinstance(result, str):
                # result 格式: "{'3a1d895c-4d39-d504-1398-18f5a40bac1e': [{'id': '...', ...}]}"
                # 第一个键就是order_id (UUID)
                try:
                    # 尝试解析字符串为字典
                    import ast
                    result_dict = ast.literal_eval(result)
                    # 获取第一个键作为order_id
                    if result_dict and isinstance(result_dict, dict):
                        first_key = list(result_dict.keys())[0]
                        order_id = first_key
                        self.hardware_interface._logger.info(f"✓ 成功提取order_id: {order_id}")
                    else:
                        self.hardware_interface._logger.warning(f"result_dict格式异常: {result_dict}")
                except Exception as e:
                    self.hardware_interface._logger.error(f"✗ 无法从结果中提取order_id: {e}, result类型={type(result)}")
            elif isinstance(result, dict):
                # 如果已经是字典
                if result:
                    first_key = list(result.keys())[0]
                    order_id = first_key
                    self.hardware_interface._logger.info(f"✓ 成功提取order_id(dict): {order_id}")

            if not order_id:
                self.hardware_interface._logger.warning(
                    f"⚠ 未能提取order_id，result={result[:100] if isinstance(result, str) else result}"
                )

            # 返回成功结果和构建的JSON数据
            return json.dumps({
                "suc": True,
                "order_code": order_code,
                "order_id": order_id,
                "result": result,
                "order_params": order_data
            })

        except BioyondException:
            # 重新抛出BioyondException
            raise
        except Exception as e:
            # 捕获其他未预期的异常，转换为BioyondException
            error_msg = f"创建90%10%小瓶投料任务时发生未预期的错误: {str(e)}"
            self.hardware_interface._logger.error(error_msg)
            raise BioyondException(error_msg)

    # 二胺溶液配置任务创建方法
    def create_diamine_solution_task(self,
                                    order_name: str = None,
                                    material_name: str = None,
                                    target_weigh: str = None,
                                    volume: str = None,
                                    liquid_material_name: str = "NMP",
                                    speed: str = None,
                                    temperature: str = None,
                                    delay_time: str = None,
                                    hold_m_name: str = None) -> dict:
        """
        创建二胺溶液配置任务

        参数说明:
        - order_name: 任务名称，如果为None则使用默认名称
        - material_name: 固体物料名称，必填
        - target_weigh: 固体目标重量，必填
        - volume: 液体体积，必填
        - liquid_material_name: 液体物料名称，默认为NMP
        - speed: 搅拌速度，如果为None则使用默认值400
        - temperature: 温度，如果为None则使用默认值20
        - delay_time: 延迟时间，如果为None则使用默认值600
        - hold_m_name: 库位名称，如"ODA-1"，用于查找对应的holdMId

        返回: 任务创建结果

        异常:
        - BioyondException: 各种错误情况下的统一异常
        """
        try:
            # 1. 参数验证
            if not material_name:
                raise BioyondException("material_name 是必填参数")
            if not target_weigh:
                raise BioyondException("target_weigh 是必填参数")
            if not volume:
                raise BioyondException("volume 是必填参数")
            if not hold_m_name:
                raise BioyondException("hold_m_name 是必填参数")


            # 2. 生成任务编码和设置默认值
            order_code = "task_oda_" + str(int(datetime.now().timestamp()))
            if order_name is None:
                order_name = f"二胺溶液配置-{material_name}"
            if speed is None:
                speed = "400"
            if temperature is None:
                temperature = "20"
            if delay_time is None:
                delay_time = "600"

            # 3. 工作流ID - 二胺溶液配置工作流
            workflow_id = "3a15d4a1-3bbe-76f9-a458-292896a338f5"

            # 4. 查询工作流对应的holdMID
            material_info = self.hardware_interface.material_id_query(workflow_id)
            if not material_info:
                raise BioyondException(f"无法查询工作流 {workflow_id} 的物料信息")

            # 获取locations列表
            locations = material_info.get("locations", []) if isinstance(material_info, dict) else []
            if not locations:
                raise BioyondException(f"工作流 {workflow_id} 没有找到库位信息")

            # 查找指定名称的库位
            hold_mid = None
            for location in locations:
                if location.get("holdMName") == hold_m_name:
                    hold_mid = location.get("holdMId")
                    break

            if not hold_mid:
                raise BioyondException(f"未找到库位名称为 {hold_m_name} 的库位，请检查名称是否正确")

            extend_properties = f"{{\"{ hold_mid }\": {{}}}}"
            self.hardware_interface._logger.info(f"找到库位 {hold_m_name} 对应的holdMId: {hold_mid}")

            # 5. 构建任务参数
            order_data = {
                "orderCode": order_code,
                "orderName": order_name,
                "workflowId": workflow_id,
                "borderNumber": 1,
                "paramValues": {
                    # 固体物料参数
                    "3a15d4a1-3bde-f5bc-053f-1ae0bf1f357e": [
                        {"m": 3, "n": 2, "Key": "targetWeigh", "Value": target_weigh},
                        {"m": 3, "n": 2, "Key": "assignMaterialName", "Value": material_name}
                    ],
                    # 液体物料参数
                    "3a15d4a1-3bde-d584-b309-e661ae8f1c01": [
                        {"m": 3, "n": 3, "Key": "volume", "Value": volume},
                        {"m": 3, "n": 3, "Key": "assignMaterialName", "Value": liquid_material_name}
                    ],
                    # 搅拌参数
                    "3a15d4a1-3bde-8ec4-1ced-92efc97ed73d": [
                        {"m": 3, "n": 6, "Key": "speed", "Value": speed},
                        {"m": 3, "n": 6, "Key": "temperature", "Value": temperature}
                    ],
                    # 延迟时间参数
                    "3a15d4a1-3bde-3b92-83ff-8923a0addbbc": [
                        {"m": 3, "n": 7, "Key": "DelayTime", "Value": delay_time}
                    ]
                },
                "ExtendProperties": extend_properties
            }

            # 6. 转换为JSON字符串并创建任务
            json_str = json.dumps([order_data], ensure_ascii=False)
            self.hardware_interface._logger.info(f"创建二胺溶液配置任务参数: {json_str}")

            # 7. 调用create_order方法创建任务
            result = self.hardware_interface.create_order(json_str)
            self.hardware_interface._logger.info(f"创建二胺溶液配置任务结果: {result}")

            # 8. 解析结果获取order_id
            order_id = None
            if isinstance(result, str):
                try:
                    import ast
                    result_dict = ast.literal_eval(result)
                    if result_dict and isinstance(result_dict, dict):
                        first_key = list(result_dict.keys())[0]
                        order_id = first_key
                        self.hardware_interface._logger.info(f"✓ 成功提取order_id: {order_id}")
                    else:
                        self.hardware_interface._logger.warning(f"result_dict格式异常: {result_dict}")
                except Exception as e:
                    self.hardware_interface._logger.error(f"✗ 无法从结果中提取order_id: {e}")
            elif isinstance(result, dict):
                if result:
                    first_key = list(result.keys())[0]
                    order_id = first_key
                    self.hardware_interface._logger.info(f"✓ 成功提取order_id(dict): {order_id}")

            if not order_id:
                self.hardware_interface._logger.warning(f"⚠ 未能提取order_id")

            # 返回成功结果和构建的JSON数据
            return json.dumps({
                "suc": True,
                "order_code": order_code,
                "order_id": order_id,
                "result": result,
                "order_params": order_data
            })

        except BioyondException:
            # 重新抛出BioyondException
            raise
        except Exception as e:
            # 捕获其他未预期的异常，转换为BioyondException
            error_msg = f"创建二胺溶液配置任务时发生未预期的错误: {str(e)}"
            self.hardware_interface._logger.error(error_msg)
            raise BioyondException(error_msg)

    # 批量创建二胺溶液配置任务
    def batch_create_diamine_solution_tasks(self,
                                           solutions,
                                           liquid_material_name: str = "NMP",
                                           speed: str = None,
                                           temperature: str = None,
                                           delay_time: str = None) -> str:
        """
        批量创建二胺溶液配置任务

        参数说明:
        - solutions: 溶液列表（数组）或JSON字符串，格式如下:
          [
              {
                  "name": "MDA",
                  "order": 0,
                  "solid_mass": 5.0,
                  "solvent_volume": 20,
                  ...
              },
              ...
          ]
        - liquid_material_name: 液体物料名称，默认为"NMP"
        - speed: 搅拌速度，如果为None则使用默认值400
        - temperature: 温度，如果为None则使用默认值20
        - delay_time: 延迟时间，如果为None则使用默认值600

        返回: JSON字符串格式的任务创建结果

        异常:
        - BioyondException: 各种错误情况下的统一异常
        """
        try:
            # 参数类型转换：如果是字符串则解析为列表
            if isinstance(solutions, str):
                try:
                    solutions = json.loads(solutions)
                except json.JSONDecodeError as e:
                    raise BioyondException(f"solutions JSON解析失败: {str(e)}")

            # 参数验证
            if not isinstance(solutions, list):
                raise BioyondException("solutions 必须是列表类型或有效的JSON数组字符串")

            if not solutions:
                raise BioyondException("solutions 列表不能为空")

            # 批量创建任务
            results = []
            success_count = 0
            failed_count = 0

            for idx, solution in enumerate(solutions):
                try:
                    # 提取参数
                    name = solution.get("name")
                    solid_mass = solution.get("solid_mass")
                    solvent_volume = solution.get("solvent_volume")
                    order = solution.get("order")

                    if not all([name, solid_mass is not None, solvent_volume is not None]):
                        self.hardware_interface._logger.warning(
                            f"跳过第 {idx + 1} 个溶液：缺少必要参数"
                        )
                        results.append({
                            "index": idx + 1,
                            "name": name,
                            "success": False,
                            "error": "缺少必要参数"
                        })
                        failed_count += 1
                        continue

                    # 生成库位名称（直接使用物料名称）
                    # 如果需要其他命名规则，可以在这里调整
                    hold_m_name = name

                    # 调用单个任务创建方法
                    result = self.create_diamine_solution_task(
                        order_name=f"二胺溶液配置-{name}",
                        material_name=name,
                        target_weigh=str(solid_mass),
                        volume=str(solvent_volume),
                        liquid_material_name=liquid_material_name,
                        speed=speed,
                        temperature=temperature,
                        delay_time=delay_time,
                        hold_m_name=hold_m_name
                    )

                    # 解析返回结果以获取order_code和order_id
                    result_data = json.loads(result) if isinstance(result, str) else result
                    order_code = result_data.get("order_code")
                    order_id = result_data.get("order_id")
                    order_params = result_data.get("order_params", {})

                    results.append({
                        "index": idx + 1,
                        "name": name,
                        "success": True,
                        "order_code": order_code,
                        "order_id": order_id,
                        "hold_m_name": hold_m_name,
                        "order_params": order_params
                    })
                    success_count += 1
                    self.hardware_interface._logger.info(
                        f"成功创建二胺溶液配置任务: {name}, order_code={order_code}, order_id={order_id}"
                    )

                except BioyondException as e:
                    results.append({
                        "index": idx + 1,
                        "name": solution.get("name", "unknown"),
                        "success": False,
                        "error": str(e)
                    })
                    failed_count += 1
                    self.hardware_interface._logger.error(
                        f"创建第 {idx + 1} 个任务失败: {str(e)}"
                    )
                except Exception as e:
                    results.append({
                        "index": idx + 1,
                        "name": solution.get("name", "unknown"),
                        "success": False,
                        "error": f"未知错误: {str(e)}"
                    })
                    failed_count += 1
                    self.hardware_interface._logger.error(
                        f"创建第 {idx + 1} 个任务时发生未知错误: {str(e)}"
                    )

            # 提取所有成功任务的order_code和order_id
            order_codes = [r["order_code"] for r in results if r["success"]]
            order_ids = [r["order_id"] for r in results if r["success"]]

            # 返回汇总结果
            summary = {
                "total": len(solutions),
                "success": success_count,
                "failed": failed_count,
                "order_codes": order_codes,
                "order_ids": order_ids,
                "details": results
            }

            self.hardware_interface._logger.info(
                f"批量创建二胺溶液配置任务完成: 总数={len(solutions)}, "
                f"成功={success_count}, 失败={failed_count}"
            )

            # 构建返回结果
            summary["return_info"] = {
                "order_codes": order_codes,
                "order_ids": order_ids,
            }

            return summary

        except BioyondException:
            raise
        except Exception as e:
            error_msg = f"批量创建二胺溶液配置任务时发生未预期的错误: {str(e)}"
            self.hardware_interface._logger.error(error_msg)
            raise BioyondException(error_msg)

    def brief_step_parameters(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """获取简要步骤参数（站点项目接口）

        参数:
            data: 查询参数字典

        返回值:
            dict: 接口返回数据
        """
        return self._post_project_api("/api/lims/order/brief-step-paramerers", data)

    def project_order_report(self, order_id: str) -> Dict[str, Any]:
        """查询项目端订单报告（兼容旧路径）

        参数:
            order_id: 订单ID

        返回值:
            dict: 报告数据
        """
        return self._post_project_api("/api/lims/order/project-order-report", order_id)

    def workflow_sample_locations(self, workflow_id: str) -> Dict[str, Any]:
        """查询工作流样品库位（站点项目接口）

        参数:
            workflow_id: 工作流ID

        返回值:
            dict: 位置信息数据
        """
        return self._post_project_api("/api/lims/storage/workflow-sample-locations", workflow_id)


    # 批量创建90%10%小瓶投料任务
    def batch_create_90_10_vial_feeding_tasks(self,
                                              titration,
                                              hold_m_name: str = None,
                                              speed: str = None,
                                              temperature: str = None,
                                              delay_time: str = None,
                                              liquid_material_name: str = "NMP") -> str:
        """
        批量创建90%10%小瓶投料任务（仅创建1个任务，但包含所有90%和10%物料）

        参数说明:
        - titration: 滴定信息的字典或JSON字符串，格式如下:
          {
              "name": "BTDA",
              "main_portion": 1.9152351915461294,  # 主称固体质量(g) -> 90%物料
              "titration_portion": 0.05923407808905555,  # 滴定固体质量(g) -> 10%物料固体
              "titration_solvent": 3.050555021586361  # 滴定溶液体积(mL) -> 10%物料液体
          }
        - hold_m_name: 库位名称，如"C01"。必填参数
        - speed: 搅拌速度，如果为None则使用默认值400
        - temperature: 温度，如果为None则使用默认值40
        - delay_time: 延迟时间，如果为None则使用默认值600
        - liquid_material_name: 10%物料的液体物料名称，默认为"NMP"

        返回: JSON字符串格式的任务创建结果

        异常:
        - BioyondException: 各种错误情况下的统一异常
        """
        try:
            # 参数类型转换：如果是字符串则解析为字典
            if isinstance(titration, str):
                try:
                    titration = json.loads(titration)
                except json.JSONDecodeError as e:
                    raise BioyondException(f"titration参数JSON解析失败: {str(e)}")

            # 参数验证
            if not isinstance(titration, dict):
                raise BioyondException("titration 必须是字典类型或有效的JSON字符串")

            if not hold_m_name:
                raise BioyondException("hold_m_name 是必填参数")

            if not titration:
                raise BioyondException("titration 参数不能为空")

            # 提取滴定数据
            name = titration.get("name")
            main_portion = titration.get("main_portion")  # 主称固体质量
            titration_portion = titration.get("titration_portion")  # 滴定固体质量
            titration_solvent = titration.get("titration_solvent")  # 滴定溶液体积

            if not all([name, main_portion is not None, titration_portion is not None, titration_solvent is not None]):
                raise BioyondException("titration 数据缺少必要参数")

            # 调用单个任务创建方法
            result = self.create_90_10_vial_feeding_task(
                order_name=f"90%10%小瓶投料-{name}",
                speed=speed,
                temperature=temperature,
                delay_time=delay_time,
                # 90%物料 - 主称固体直接使用main_portion
                percent_90_1_assign_material_name=name,
                percent_90_1_target_weigh=str(round(main_portion, 6)),
                # 10%物料 - 滴定固体 + 滴定溶剂（只使用第1个10%小瓶）
                percent_10_1_assign_material_name=name,
                percent_10_1_target_weigh=str(round(titration_portion, 6)),
                percent_10_1_volume=str(round(titration_solvent, 6)),
                percent_10_1_liquid_material_name=liquid_material_name,
                hold_m_name=hold_m_name
            )

            # 解析返回结果以获取order_code和order_id
            result_data = json.loads(result) if isinstance(result, str) else result
            order_code = result_data.get("order_code")
            order_id = result_data.get("order_id")
            order_params = result_data.get("order_params", {})

            # 构建详细信息（保持原有结构）
            detail = {
                "index": 1,
                "name": name,
                "success": True,
                "order_code": order_code,
                "order_id": order_id,
                "hold_m_name": hold_m_name,
                "90_vials": {
                    "count": 1,
                    "weight_per_vial": round(main_portion, 6),
                    "total_weight": round(main_portion, 6)
                },
                "10_vials": {
                    "count": 1,
                    "solid_weight": round(titration_portion, 6),
                    "liquid_volume": round(titration_solvent, 6)
                },
                "order_params": order_params
            }

            # 构建批量结果格式（与diamine_solution_tasks保持一致）
            summary = {
                "total": 1,
                "success": 1,
                "failed": 0,
                "order_codes": [order_code],
                "order_ids": [order_id],
                "details": [detail]
            }

            self.hardware_interface._logger.info(
                f"成功创建90%10%小瓶投料任务: {name}, order_code={order_code}, order_id={order_id}"
            )

            # 构建返回结果
            summary["return_info"] = {
                "order_codes": [order_code],
                "order_ids": [order_id],
            }

            return summary

        except BioyondException:
            raise
        except Exception as e:
            error_msg = f"批量创建90%10%小瓶投料任务时发生未预期的错误: {str(e)}"
            self.hardware_interface._logger.error(error_msg)
            raise BioyondException(error_msg)

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

    def _simplify_report(self, report) -> Dict[str, Any]:
        """简化实验报告，只保留关键信息，去除冗余的工作流参数"""
        if not isinstance(report, dict):
            return report

        data = report.get('data', {})
        if not isinstance(data, dict):
            return report

        # 提取关键信息
        simplified = {
            'name': data.get('name'),
            'code': data.get('code'),
            'requester': data.get('requester'),
            'workflowName': data.get('workflowName'),
            'workflowStep': data.get('workflowStep'),
            'requestTime': data.get('requestTime'),
            'startPreparationTime': data.get('startPreparationTime'),
            'completeTime': data.get('completeTime'),
            'useTime': data.get('useTime'),
            'status': data.get('status'),
            'statusName': data.get('statusName'),
        }

        # 提取物料信息（简化版）
        pre_intakes = data.get('preIntakes', [])
        if pre_intakes and isinstance(pre_intakes, list):
            first_intake = pre_intakes[0]
            sample_materials = first_intake.get('sampleMaterials', [])

            # 简化物料信息
            simplified_materials = []
            for material in sample_materials:
                if isinstance(material, dict):
                    mat_info = {
                        'materialName': material.get('materialName'),
                        'materialTypeName': material.get('materialTypeName'),
                        'materialCode': material.get('materialCode'),
                        'materialLocation': material.get('materialLocation'),
                    }

                    # 解析parameters中的关键信息（如密度、加料历史等）
                    params_str = material.get('parameters', '{}')
                    try:
                        params = json.loads(params_str) if isinstance(params_str, str) else params_str
                        if isinstance(params, dict):
                            # 只保留关键参数
                            if 'density' in params:
                                mat_info['density'] = params['density']
                            if 'feedingHistory' in params:
                                mat_info['feedingHistory'] = params['feedingHistory']
                            if 'liquidVolume' in params:
                                mat_info['liquidVolume'] = params['liquidVolume']
                            if 'm_diamine_tot' in params:
                                mat_info['m_diamine_tot'] = params['m_diamine_tot']
                            if 'wt_diamine' in params:
                                mat_info['wt_diamine'] = params['wt_diamine']
                    except:
                        pass

                    simplified_materials.append(mat_info)

            simplified['sampleMaterials'] = simplified_materials

            # 提取extraProperties中的实际值
            extra_props = first_intake.get('extraProperties', {})
            if isinstance(extra_props, dict):
                simplified_extra = {}
                for key, value in extra_props.items():
                    try:
                        parsed_value = json.loads(value) if isinstance(value, str) else value
                        simplified_extra[key] = parsed_value
                    except:
                        simplified_extra[key] = value
                simplified['extraProperties'] = simplified_extra

        return {
            'data': simplified,
            'code': report.get('code'),
            'message': report.get('message'),
            'timestamp': report.get('timestamp')
        }

    def scheduler_start(self) -> dict:
        """启动调度器 - 启动Bioyond工作站的任务调度器，开始执行队列中的任务

        Returns:
            dict: 包含return_info的字典，return_info为整型(1=成功)

        Raises:
            BioyondException: 调度器启动失败时抛出异常
        """
        result = self.hardware_interface.scheduler_start()
        self.hardware_interface._logger.info(f"调度器启动结果: {result}")

        if result != 1:
            error_msg = "启动调度器失败: 有未处理错误，调度无法启动。请检查Bioyond系统状态。"
            self.hardware_interface._logger.error(error_msg)
            raise BioyondException(error_msg)

        return {"return_info": result}

    # 等待多个任务完成并获取实验报告
    def wait_for_multiple_orders_and_get_reports(self,
                                                  batch_create_result: str = None,
                                                  timeout: int = 7200,
                                                  check_interval: int = 10) -> Dict[str, Any]:
        """
        同时等待多个任务完成并获取实验报告

        参数说明:
        - batch_create_result: 批量创建任务的返回结果JSON字符串，包含order_codes和order_ids数组
        - timeout: 超时时间（秒），默认7200秒（2小时）
        - check_interval: 检查间隔（秒），默认10秒

        返回: 包含所有任务状态和报告的字典
        {
            "total": 2,
            "completed": 2,
            "timeout": 0,
            "elapsed_time": 120.5,
            "reports": [
                {
                    "order_code": "task_vial_1",
                    "order_id": "uuid1",
                    "status": "completed",
                    "completion_status": 30,
                    "report": {...}
                },
                ...
            ]
        }

        异常:
        - BioyondException: 所有任务都超时或发生错误
        """
        try:
            # 参数类型转换
            timeout = int(timeout) if timeout else 7200
            check_interval = int(check_interval) if check_interval else 10

            # 验证batch_create_result参数
            if not batch_create_result or batch_create_result == "":
                raise BioyondException(
                    "batch_create_result参数为空，请确保:\n"
                    "1. batch_create节点与wait节点之间正确连接了handle\n"
                    "2. batch_create节点成功执行并返回了结果\n"
                    "3. 检查上游batch_create任务是否成功创建了订单"
                )

            # 解析batch_create_result JSON对象
            try:
                # 清理可能存在的截断标记 [...]
                if isinstance(batch_create_result, str) and '[...]' in batch_create_result:
                    batch_create_result = batch_create_result.replace('[...]', '[]')

                result_obj = json.loads(batch_create_result) if isinstance(batch_create_result, str) else batch_create_result

                # 兼容外层包装格式 {error, suc, return_value}
                if isinstance(result_obj, dict) and "return_value" in result_obj:
                    inner = result_obj.get("return_value")
                    if isinstance(inner, str):
                        result_obj = json.loads(inner)
                    elif isinstance(inner, dict):
                        result_obj = inner

                # 从summary对象中提取order_codes和order_ids
                order_codes = result_obj.get("order_codes", [])
                order_ids = result_obj.get("order_ids", [])

            except json.JSONDecodeError as e:
                raise BioyondException(f"解析batch_create_result失败: {e}")
            except Exception as e:
                raise BioyondException(f"处理batch_create_result时出错: {e}")

            # 验证提取的数据
            if not order_codes:
                self.hardware_interface._logger.error(
                    f"batch_create任务未生成任何订单。batch_create_result内容: {batch_create_result}"
                )
                raise BioyondException(
                    "batch_create_result中未找到order_codes或为空。\n"
                    "可能的原因:\n"
                    "1. batch_create任务执行失败（检查任务是否报错）\n"
                    "2. 物料配置问题（如'物料样品板分配失败'）\n"
                    "3. Bioyond系统状态异常\n"
                    f"请检查batch_create任务的执行结果"
                )
            if not order_ids:
                raise BioyondException("batch_create_result中未找到order_ids字段或为空")

            # 确保order_codes和order_ids是列表类型
            if not isinstance(order_codes, list):
                order_codes = [order_codes] if order_codes else []
            if not isinstance(order_ids, list):
                order_ids = [order_ids] if order_ids else []

            codes_list = order_codes
            ids_list = order_ids

            if len(codes_list) != len(ids_list):
                raise BioyondException(
                    f"order_codes数量({len(codes_list)})与order_ids数量({len(ids_list)})不匹配"
                )

            if not codes_list or not ids_list:
                raise BioyondException("order_codes和order_ids不能为空")

            # 初始化跟踪变量
            total = len(codes_list)
            pending_orders = {code: {"order_id": ids_list[i], "completed": False}
                            for i, code in enumerate(codes_list)}
            reports = []

            start_time = time.time()
            self.hardware_interface._logger.info(
                f"开始等待 {total} 个任务完成: {', '.join(codes_list)}"
            )

            # 轮询检查任务状态
            while pending_orders:
                elapsed_time = time.time() - start_time

                # 检查超时
                if elapsed_time > timeout:
                    # 收集超时任务
                    timeout_orders = list(pending_orders.keys())
                    self.hardware_interface._logger.error(
                        f"等待任务完成超时，剩余未完成任务: {', '.join(timeout_orders)}"
                    )

                    # 为超时任务添加记录
                    for order_code in timeout_orders:
                        reports.append({
                            "order_code": order_code,
                            "order_id": pending_orders[order_code]["order_id"],
                            "status": "timeout",
                            "completion_status": None,
                            "report": None,
                            "extracted": None,
                            "elapsed_time": elapsed_time
                        })

                    break

                # 检查每个待完成的任务
                completed_in_this_round = []
                for order_code in list(pending_orders.keys()):
                    order_id = pending_orders[order_code]["order_id"]

                    # 检查任务是否完成
                    if order_code in self.order_completion_status:
                        completion_info = self.order_completion_status[order_code]
                        self.hardware_interface._logger.info(
                            f"检测到任务 {order_code} 已完成，状态: {completion_info.get('status')}"
                        )

                        # 获取实验报告
                        try:
                            report = self.project_order_report(order_id)

                            if not report:
                                self.hardware_interface._logger.warning(
                                    f"任务 {order_code} 已完成但无法获取报告"
                                )
                                report = {"error": "无法获取报告"}
                            else:
                                self.hardware_interface._logger.info(
                                    f"成功获取任务 {order_code} 的实验报告"
                                )
                                # 简化报告，去除冗余信息
                                report = self._simplify_report(report)

                            reports.append({
                                "order_code": order_code,
                                "order_id": order_id,
                                "status": "completed",
                                "completion_status": completion_info.get('status'),
                                "report": report,
                                "extracted": self._extract_actuals_from_report(report),
                                "elapsed_time": elapsed_time
                            })

                            # 标记为已完成
                            completed_in_this_round.append(order_code)

                            # 清理完成状态记录
                            del self.order_completion_status[order_code]

                        except Exception as e:
                            self.hardware_interface._logger.error(
                                f"查询任务 {order_code} 报告失败: {str(e)}"
                            )
                            reports.append({
                                "order_code": order_code,
                                "order_id": order_id,
                                "status": "error",
                                "completion_status": completion_info.get('status'),
                                "report": None,
                                "extracted": None,
                                "error": str(e),
                                "elapsed_time": elapsed_time
                            })
                            completed_in_this_round.append(order_code)

                # 从待完成列表中移除已完成的任务
                for order_code in completed_in_this_round:
                    del pending_orders[order_code]

                # 如果还有待完成的任务，等待后继续
                if pending_orders:
                    time.sleep(check_interval)

                    # 每分钟记录一次等待状态
                    new_elapsed_time = time.time() - start_time
                    if int(new_elapsed_time) % 60 == 0 and new_elapsed_time > 0:
                        self.hardware_interface._logger.info(
                            f"批量等待任务中... 已完成 {len(reports)}/{total}, "
                            f"待完成: {', '.join(pending_orders.keys())}, "
                            f"已等待 {int(new_elapsed_time/60)} 分钟"
                        )

            # 统计结果
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

            self.hardware_interface._logger.info(
                f"批量等待任务完成: 总数={total}, 成功={completed_count}, "
                f"超时={timeout_count}, 错误={error_count}, 耗时={final_elapsed_time:.1f}秒"
            )

            # 返回字典格式，在顶层包含统计信息
            return {
                "return_info": json.dumps(summary, ensure_ascii=False)
            }

        except BioyondException:
            raise
        except Exception as e:
            error_msg = f"批量等待任务完成时发生未预期的错误: {str(e)}"
            self.hardware_interface._logger.error(error_msg)
            raise BioyondException(error_msg)

    def process_order_finish_report(self, report_request, used_materials) -> Dict[str, Any]:
        """
        重写父类方法，处理任务完成报送并记录到 order_completion_status

        Args:
            report_request: WorkstationReportRequest 对象，包含任务完成信息
            used_materials: 物料使用记录列表

        Returns:
            Dict[str, Any]: 处理结果
        """
        try:
            # 调用父类方法
            result = super().process_order_finish_report(report_request, used_materials)

            # 记录任务完成状态
            data = report_request.data
            order_code = data.get('orderCode')

            if order_code:
                self.order_completion_status[order_code] = {
                    'status': data.get('status'),
                    'order_name': data.get('orderName'),
                    'timestamp': datetime.now().isoformat(),
                    'start_time': data.get('startTime'),
                    'end_time': data.get('endTime')
                }

                self.hardware_interface._logger.info(
                    f"已记录任务完成状态: {order_code}, status={data.get('status')}"
                )

            return result

        except Exception as e:
            self.hardware_interface._logger.error(f"处理任务完成报送失败: {e}")
            return {"processed": False, "error": str(e)}

    def transfer_materials_to_reaction_station(
        self,
        target_device_id: str,
        transfer_groups: list
    ) -> dict:
        """
        将配液站完成的物料转移到指定反应站的堆栈库位
        支持多组转移任务,每组包含物料名称、目标堆栈和目标库位

        Args:
            target_device_id: 目标反应站设备ID(所有转移组使用同一个设备)
            transfer_groups: 转移任务组列表,每组包含:
                - materials: 物料名称(字符串,将通过RPC查询)
                - target_stack: 目标堆栈名称(如"堆栈1左")
                - target_sites: 目标库位(如"A01")

        Returns:
            dict: 转移结果
                {
                    "success": bool,
                    "total_groups": int,
                    "successful_groups": int,
                    "failed_groups": int,
                    "target_device_id": str,
                    "details": [...]
                }
        """
        try:
            # 验证参数
            if not target_device_id:
                raise ValueError("目标设备ID不能为空")

            if not transfer_groups:
                raise ValueError("转移任务组列表不能为空")

            if not isinstance(transfer_groups, list):
                raise ValueError("transfer_groups必须是列表类型")

            # 标准化设备ID格式: 确保以 /devices/ 开头
            if not target_device_id.startswith("/devices/"):
                if target_device_id.startswith("/"):
                    target_device_id = f"/devices{target_device_id}"
                else:
                    target_device_id = f"/devices/{target_device_id}"

            self.hardware_interface._logger.info(
                f"目标设备ID标准化为: {target_device_id}"
            )

            self.hardware_interface._logger.info(
                f"开始执行批量物料转移: {len(transfer_groups)}组任务 -> {target_device_id}"
            )

            from .config import WAREHOUSE_MAPPING
            results = []
            successful_count = 0
            failed_count = 0

            for idx, group in enumerate(transfer_groups, 1):
                try:
                    # 提取参数
                    material_name = group.get("materials", "")
                    target_stack = group.get("target_stack", "")
                    target_sites = group.get("target_sites", "")

                    # 验证必填参数
                    if not material_name:
                        raise ValueError(f"第{idx}组: 物料名称不能为空")
                    if not target_stack:
                        raise ValueError(f"第{idx}组: 目标堆栈不能为空")
                    if not target_sites:
                        raise ValueError(f"第{idx}组: 目标库位不能为空")

                    self.hardware_interface._logger.info(
                        f"处理第{idx}组转移: {material_name} -> "
                        f"{target_device_id}/{target_stack}/{target_sites}"
                    )

                    # 通过物料名称从deck获取ResourcePLR对象
                    try:
                        material_resource = self.deck.get_resource(material_name)
                        if not material_resource:
                            raise ValueError(f"在deck中未找到物料: {material_name}")

                        self.hardware_interface._logger.info(
                            f"从deck获取到物料 {material_name}: {material_resource}"
                        )
                    except Exception as e:
                        raise ValueError(
                            f"获取物料 {material_name} 失败: {str(e)}，请确认物料已正确加载到deck中"
                        )

                    # 验证目标堆栈是否存在
                    if target_stack not in WAREHOUSE_MAPPING:
                        raise ValueError(
                            f"未知的堆栈名称: {target_stack}，"
                            f"可选值: {list(WAREHOUSE_MAPPING.keys())}"
                        )

                    # 验证库位是否有效
                    stack_sites = WAREHOUSE_MAPPING[target_stack].get("site_uuids", {})
                    if target_sites not in stack_sites:
                        raise ValueError(
                            f"库位 {target_sites} 不存在于堆栈 {target_stack} 中，"
                            f"可选库位: {list(stack_sites.keys())}"
                        )

                    # 获取目标库位的UUID
                    target_site_uuid = stack_sites[target_sites]
                    if not target_site_uuid:
                        raise ValueError(
                            f"库位 {target_sites} 的 UUID 未配置，请在 WAREHOUSE_MAPPING 中完善"
                        )

                    # 目标位点（包含UUID）
                    future = ROS2DeviceNode.run_async_func(
                        self._ros_node.get_resource_with_dir,
                        True,
                        **{
                            "resource_id": f"/reaction_station_bioyond/Bioyond_Deck/{target_stack}",
                            "with_children": True,
                        },
                    )
                    # 等待异步完成后再获取结果
                    if not future:
                        raise ValueError(f"获取目标堆栈资源future无效: {target_stack}")
                    while not future.done():
                        time.sleep(0.1)
                    target_site_resource = future.result()

                    # 调用父类的 transfer_resource_to_another 方法
                    # 传入ResourcePLR对象和目标位点资源
                    future = self.transfer_resource_to_another(
                        resource=[material_resource],
                        mount_resource=[target_site_resource],
                        sites=[target_sites],
                        mount_device_id=target_device_id
                    )

                    # 等待异步任务完成（轮询直到完成，再取结果）
                    if future:
                        try:
                            while not future.done():
                                time.sleep(0.1)
                            future.result()
                            self.hardware_interface._logger.info(
                                f"异步转移任务已完成: {material_name}"
                            )
                        except Exception as e:
                            raise ValueError(f"转移任务执行失败: {str(e)}")

                    self.hardware_interface._logger.info(
                        f"第{idx}组转移成功: {material_name} -> "
                        f"{target_device_id}/{target_stack}/{target_sites}"
                    )

                    successful_count += 1
                    results.append({
                        "group_index": idx,
                        "success": True,
                        "material_name": material_name,
                        "target_stack": target_stack,
                        "target_site": target_sites,
                        "message": "转移成功"
                    })

                except Exception as e:
                    error_msg = f"第{idx}组转移失败: {str(e)}"
                    self.hardware_interface._logger.error(error_msg)
                    failed_count += 1
                    results.append({
                        "group_index": idx,
                        "success": False,
                        "material_name": group.get("materials", ""),
                        "error": str(e)
                    })

            # 返回汇总结果
            return {
                "success": failed_count == 0,
                "total_groups": len(transfer_groups),
                "successful_groups": successful_count,
                "failed_groups": failed_count,
                "target_device_id": target_device_id,
                "details": results,
                "message": f"完成 {len(transfer_groups)} 组转移任务到 {target_device_id}: "
                          f"{successful_count} 成功, {failed_count} 失败"
            }

        except Exception as e:
            error_msg = f"批量转移物料失败: {str(e)}"
            self.hardware_interface._logger.error(error_msg)
            return {
                "success": False,
                "total_groups": len(transfer_groups) if transfer_groups else 0,
                "successful_groups": 0,
                "failed_groups": len(transfer_groups) if transfer_groups else 0,
                "target_device_id": target_device_id if target_device_id else "",
                "error": error_msg
            }

    def query_resource_by_name(self, material_name: str):
        """
        通过物料名称查询资源对象(适用于Bioyond系统)

        Args:
            material_name: 物料名称

        Returns:
            物料ID或None
        """
        try:
            # Bioyond系统使用material_cache存储物料信息
            if not hasattr(self.hardware_interface, 'material_cache'):
                self.hardware_interface._logger.error(
                    "hardware_interface没有material_cache属性"
                )
                return None

            material_cache = self.hardware_interface.material_cache

            self.hardware_interface._logger.info(
                f"查询物料 '{material_name}', 缓存中共有 {len(material_cache)} 个物料"
            )

            # 调试: 打印前几个物料信息
            if material_cache:
                cache_items = list(material_cache.items())[:5]
                for name, material_id in cache_items:
                    self.hardware_interface._logger.debug(
                        f"缓存物料: name={name}, id={material_id}"
                    )

            # 直接从缓存中查找
            if material_name in material_cache:
                material_id = material_cache[material_name]
                self.hardware_interface._logger.info(
                    f"找到物料: {material_name} -> ID: {material_id}"
                )
                return material_id

            self.hardware_interface._logger.warning(
                f"未找到物料: {material_name} (缓存中无此物料)"
            )

            # 打印所有可用物料名称供参考
            available_materials = list(material_cache.keys())
            if available_materials:
                self.hardware_interface._logger.info(
                    f"可用物料列表(前10个): {available_materials[:10]}"
                )

            return None

        except Exception as e:
            self.hardware_interface._logger.error(
                f"查询物料失败 {material_name}: {str(e)}"
            )
            return None


if __name__ == "__main__":
    bioyond = BioyondDispensingStation(config={
        "api_key": "DE9BDDA0",
        "api_host": "http://192.168.1.200:44388"
    })

    # ============ 原有示例代码 ============

    # 示例1：使用material_id_query查询工作流对应的holdMID
    workflow_id_1 = "3a15d4a1-3bbe-76f9-a458-292896a338f5"  # 二胺溶液配置工作流ID
    workflow_id_2 = "3a19310d-16b9-9d81-b109-0748e953694b"  # 90%10%小瓶投料工作流ID

    #示例2：创建二胺溶液配置任务 - ODA，指定库位名称
    # bioyond.create_diamine_solution_task(
    #         order_code="task_oda_" + str(int(datetime.now().timestamp())),
    #         order_name="二胺溶液配置-ODA",
    #         material_name="ODA-1",
    #         target_weigh="12.000",
    #         volume="60",
    #         liquid_material_name= "NMP",
    #         speed="400",
    #         temperature="20",
    #         delay_time="600",
    #         hold_m_name="烧杯ODA"
    #     )

    # bioyond.create_diamine_solution_task(
    #         order_code="task_pda_" + str(int(datetime.now().timestamp())),
    #         order_name="二胺溶液配置-PDA",
    #         material_name="PDA-1",
    #         target_weigh="4.178",
    #         volume="60",
    #         liquid_material_name= "NMP",
    #         speed="400",
    #         temperature="20",
    #         delay_time="600",
    #         hold_m_name="烧杯PDA-2"
    #     )

    # bioyond.create_diamine_solution_task(
    #         order_code="task_mpda_" + str(int(datetime.now().timestamp())),
    #         order_name="二胺溶液配置-MPDA",
    #         material_name="MPDA-1",
    #         target_weigh="3.298",
    #         volume="50",
    #         liquid_material_name= "NMP",
    #         speed="400",
    #         temperature="20",
    #         delay_time="600",
    #         hold_m_name="烧杯MPDA"
    #     )

    bioyond.material_id_query("3a19310d-16b9-9d81-b109-0748e953694b")
    bioyond.material_id_query("3a15d4a1-3bbe-76f9-a458-292896a338f5")


    #示例4：创建90%10%小瓶投料任务
    # vial_result = bioyond.create_90_10_vial_feeding_task(
    #     order_code="task_vial_" + str(int(datetime.now().timestamp())),
    #     order_name="90%10%小瓶投料-1",
    #     percent_90_1_assign_material_name="BTDA-1",
    #     percent_90_1_target_weigh="7.392",
    #     percent_90_2_assign_material_name="BTDA-1",
    #     percent_90_2_target_weigh="7.392",
    #     percent_90_3_assign_material_name="BTDA-2",
    #     percent_90_3_target_weigh="7.392",
    #     percent_10_1_assign_material_name="BTDA-2",
    #     percent_10_1_target_weigh="1.500",
    #     percent_10_1_volume="20",
    #     percent_10_1_liquid_material_name="NMP",
    #     # percent_10_2_assign_material_name="BTDA-c",
    #     # percent_10_2_target_weigh="1.2",
    #     # percent_10_2_volume="20",
    #     # percent_10_2_liquid_material_name="NMP",
    #     speed="400",
    #     temperature="60",
    #     delay_time="1200",
    #     hold_m_name="8.4分装板-1"
    #     )

    # vial_result = bioyond.create_90_10_vial_feeding_task(
    #     order_code="task_vial_" + str(int(datetime.now().timestamp())),
    #     order_name="90%10%小瓶投料-2",
    #     percent_90_1_assign_material_name="BPDA-1",
    #     percent_90_1_target_weigh="5.006",
    #     percent_90_2_assign_material_name="PMDA-1",
    #     percent_90_2_target_weigh="3.810",
    #     percent_90_3_assign_material_name="BPDA-1",
    #     percent_90_3_target_weigh="8.399",
    #     percent_10_1_assign_material_name="BPDA-1",
    #     percent_10_1_target_weigh="1.200",
    #     percent_10_1_volume="20",
    #     percent_10_1_liquid_material_name="NMP",
    #     percent_10_2_assign_material_name="BPDA-1",
    #     percent_10_2_target_weigh="1.200",
    #     percent_10_2_volume="20",
    #     percent_10_2_liquid_material_name="NMP",
    #     speed="400",
    #     temperature="60",
    #     delay_time="1200",
    #     hold_m_name="8.4分装板-2"
    #     )

    #启动调度器
    #bioyond.scheduler_start()

    #继续调度器
    #bioyond.scheduler_continue()

    result0 = bioyond.stock_material('{"typeMode": 0, "includeDetail": true}')
    result1 = bioyond.stock_material('{"typeMode": 1, "includeDetail": true}')
    result2 = bioyond.stock_material('{"typeMode": 2, "includeDetail": true}')

    matpos1 = bioyond.query_warehouse_by_material_type("3a14196e-b7a0-a5da-1931-35f3000281e9")
    matpos2 = bioyond.query_warehouse_by_material_type("3a14196e-5dfe-6e21-0c79-fe2036d052c4")
    matpos3 = bioyond.query_warehouse_by_material_type("3a14196b-24f2-ca49-9081-0cab8021bf1a")

    #样品板（里面有样品瓶）
    material_data_yp = {
    "typeId": "3a14196e-b7a0-a5da-1931-35f3000281e9",
    #"code": "物料编码001",
    #"barCode": "物料条码001",
    "name": "8.4样品板",
    "unit": "个",
    "quantity": 1,
    "details": [
        {
        "typeId": "3a14196a-cf7d-8aea-48d8-b9662c7dba94",
        #"code": "物料编码001",
        "name": "BTDA-1",
        "quantity": 20,
        "x": 1,
        "y": 1,
        #"unit": "单位"
        "molecular": 1,
        "Parameters":"{\"molecular\": 1}"
        },
        {
        "typeId": "3a14196a-cf7d-8aea-48d8-b9662c7dba94",
        #"code": "物料编码001",
        "name": "BPDA-1",
        "quantity": 20,
        "x": 2,
        "y": 1, #x1y2是A02
        #"unit": "单位"
        "molecular": 1,
        "Parameters":"{\"molecular\": 1}"
        },
        {
        "typeId": "3a14196a-cf7d-8aea-48d8-b9662c7dba94",
        #"code": "物料编码001",
        "name": "BTDA-2",
        "quantity": 20,
        "x": 1,
        "y": 2, #x1y2是A02
        #"unit": "单位"
        "molecular": 1,
        "Parameters":"{\"molecular\": 1}"
        },
        {
        "typeId": "3a14196a-cf7d-8aea-48d8-b9662c7dba94",
        #"code": "物料编码001",
        "name": "PMDA-1",
        "quantity": 20,
        "x": 2,
        "y": 2, #x1y2是A02
        #"unit": "单位"
        "molecular": 1,
        "Parameters":"{\"molecular\": 1}"
        }
    ],
    "Parameters":"{}"
    }

    material_data_yp = {
    "typeId": "3a14196e-b7a0-a5da-1931-35f3000281e9",
    #"code": "物料编码001",
    #"barCode": "物料条码001",
    "name": "8.7样品板",
    "unit": "个",
    "quantity": 1,
    "details": [
        {
        "typeId": "3a14196a-cf7d-8aea-48d8-b9662c7dba94",
        #"code": "物料编码001",
        "name": "mianfen",
        "quantity": 13,
        "x": 1,
        "y": 1,
        #"unit": "单位"
        "molecular": 1,
        "Parameters":"{\"molecular\": 1}"
        },
        {
        "typeId": "3a14196a-cf7d-8aea-48d8-b9662c7dba94",
        #"code": "物料编码001",
        "name": "mianfen2",
        "quantity": 13,
        "x": 1,
        "y": 2, #x1y2是A02
        #"unit": "单位"
        "molecular": 1,
        "Parameters":"{\"molecular\": 1}"
        }
    ],
    "Parameters":"{}"
    }

    #分装板
    material_data_fzb_1 = {
    "typeId": "3a14196e-5dfe-6e21-0c79-fe2036d052c4",
    #"code": "物料编码001",
    #"barCode": "物料条码001",
    "name": "8.7分装板",
    "unit": "个",
    "quantity": 1,
    "details": [
        {
        "typeId": "3a14196c-76be-2279-4e22-7310d69aed68",
        #"code": "物料编码001",
        "name": "10%小瓶1",
        "quantity": 1,
        "x": 1,
        "y": 1,
        #"unit": "单位"
        "molecular": 1,
        "Parameters":"{\"molecular\": 1}"
        },
        {
        "typeId": "3a14196c-76be-2279-4e22-7310d69aed68",
        #"code": "物料编码001",
        "name": "10%小瓶2",
        "quantity": 1,
        "x": 1,
        "y": 2,
        #"unit": "单位"
        "molecular": 1,
        "Parameters":"{\"molecular\": 1}"
        },
        {
        "typeId": "3a14196c-76be-2279-4e22-7310d69aed68",
        #"code": "物料编码001",
        "name": "10%小瓶3",
        "quantity": 1,
        "x": 1,
        "y": 3,
        #"unit": "单位"
        "molecular": 1,
        "Parameters":"{\"molecular\": 1}"
        },
        {
        "typeId": "3a14196c-cdcf-088d-dc7d-5cf38f0ad9ea",
        #"code": "物料编码001",
        "name": "90%小瓶1",
        "quantity": 1,
        "x": 2,
        "y": 1, #x1y2是A02
        #"unit": "单位"
        "molecular": 1,
        "Parameters":"{\"molecular\": 1}"
        },
        {
        "typeId": "3a14196c-cdcf-088d-dc7d-5cf38f0ad9ea",
        #"code": "物料编码001",
        "name": "90%小瓶2",
        "quantity": 1,
        "x": 2,
        "y": 2,
        #"unit": "单位"
        "molecular": 1,
        "Parameters":"{\"molecular\": 1}"
        },
        {
        "typeId": "3a14196c-cdcf-088d-dc7d-5cf38f0ad9ea",
        #"code": "物料编码001",
        "name": "90%小瓶3",
        "quantity": 1,
        "x": 2,
        "y": 3,
        "molecular": 1,
        "Parameters":"{\"molecular\": 1}"
        }
    ],
    "Parameters":"{}"
    }

    material_data_fzb_2 = {
    "typeId": "3a14196e-5dfe-6e21-0c79-fe2036d052c4",
    #"code": "物料编码001",
    #"barCode": "物料条码001",
    "name": "8.4分装板-2",
    "unit": "个",
    "quantity": 1,
    "details": [
        {
        "typeId": "3a14196c-76be-2279-4e22-7310d69aed68",
        #"code": "物料编码001",
        "name": "10%小瓶1",
        "quantity": 1,
        "x": 1,
        "y": 1,
        #"unit": "单位"
        "molecular": 1,
        "Parameters":"{\"molecular\": 1}"
        },
        {
        "typeId": "3a14196c-76be-2279-4e22-7310d69aed68",
        #"code": "物料编码001",
        "name": "10%小瓶2",
        "quantity": 1,
        "x": 1,
        "y": 2,
        #"unit": "单位"
        "molecular": 1,
        "Parameters":"{\"molecular\": 1}"
        },
        {
        "typeId": "3a14196c-76be-2279-4e22-7310d69aed68",
        #"code": "物料编码001",
        "name": "10%小瓶3",
        "quantity": 1,
        "x": 1,
        "y": 3,
        #"unit": "单位"
        "molecular": 1,
        "Parameters":"{\"molecular\": 1}"
        },
        {
        "typeId": "3a14196c-cdcf-088d-dc7d-5cf38f0ad9ea",
        #"code": "物料编码001",
        "name": "90%小瓶1",
        "quantity": 1,
        "x": 2,
        "y": 1, #x1y2是A02
        #"unit": "单位"
        "molecular": 1,
        "Parameters":"{\"molecular\": 1}"
        },
        {
        "typeId": "3a14196c-cdcf-088d-dc7d-5cf38f0ad9ea",
        #"code": "物料编码001",
        "name": "90%小瓶2",
        "quantity": 1,
        "x": 2,
        "y": 2,
        #"unit": "单位"
        "molecular": 1,
        "Parameters":"{\"molecular\": 1}"
        },
        {
        "typeId": "3a14196c-cdcf-088d-dc7d-5cf38f0ad9ea",
        #"code": "物料编码001",
        "name": "90%小瓶3",
        "quantity": 1,
        "x": 2,
        "y": 3,
        "molecular": 1,
        "Parameters":"{\"molecular\": 1}"
        }
    ],
    "Parameters":"{}"
    }

    #烧杯
    material_data_sb_oda = {
    "typeId": "3a14196b-24f2-ca49-9081-0cab8021bf1a",
    #"code": "物料编码001",
    #"barCode": "物料条码001",
    "name": "mianfen1",
    "unit": "个",
    "quantity": 1,
    "Parameters":"{}"
    }

    material_data_sb_pda_2 = {
    "typeId": "3a14196b-24f2-ca49-9081-0cab8021bf1a",
    #"code": "物料编码001",
    #"barCode": "物料条码001",
    "name": "mianfen2",
    "unit": "个",
    "quantity": 1,
    "Parameters":"{}"
    }

    # material_data_sb_mpda = {
    # "typeId": "3a14196b-24f2-ca49-9081-0cab8021bf1a",
    # #"code": "物料编码001",
    # #"barCode": "物料条码001",
    # "name": "烧杯MPDA",
    # "unit": "个",
    # "quantity": 1,
    # "Parameters":"{}"
    # }


    #result_1 = bioyond.add_material(json.dumps(material_data_yp, ensure_ascii=False))
    #result_2 = bioyond.add_material(json.dumps(material_data_fzb_1, ensure_ascii=False))
    # result_3 = bioyond.add_material(json.dumps(material_data_fzb_2, ensure_ascii=False))
    # result_4 = bioyond.add_material(json.dumps(material_data_sb_oda, ensure_ascii=False))
    # result_5 = bioyond.add_material(json.dumps(material_data_sb_pda_2, ensure_ascii=False))
    # #result会返回id
    # #样品板1id：3a1b3e7d-339d-0291-dfd3-13e2a78fe521


    # #将指定物料入库到指定库位
    #bioyond.material_inbound(result_1, "3a14198e-6929-31f0-8a22-0f98f72260df")
    #bioyond.material_inbound(result_2, "3a14198e-6929-46fe-841e-03dd753f1e4a")
    # bioyond.material_inbound(result_3, "3a14198e-6929-72ac-32ce-9b50245682b8")
    # bioyond.material_inbound(result_4, "3a14198e-d724-e036-afdc-2ae39a7f3383")
    # bioyond.material_inbound(result_5, "3a14198e-d724-d818-6d4f-5725191a24b5")

    #bioyond.material_outbound(result_1, "3a14198e-6929-31f0-8a22-0f98f72260df")

    # bioyond.stock_material('{"typeMode": 2, "includeDetail": true}')

    query_order = {"status":"100", "pageCount": "10"}
    bioyond.order_query(json.dumps(query_order, ensure_ascii=False))

    # id = "3a1bce3c-4f31-c8f3-5525-f3b273bc34dc"
    # bioyond.sample_waste_removal(id)
