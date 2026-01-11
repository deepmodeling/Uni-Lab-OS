import json
import pathlib
from typing import Sequence, Optional, List, Union, Literal

import requests
from geometry_msgs.msg import Point
from pylabrobot.liquid_handling import LiquidHandler
from pylabrobot.resources import (
    TipRack,
    Container,
    Coordinate,
)
import copy
from unilabos_msgs.msg import Resource

from unilabos.resources.resource_tracker import DeviceNodeResourceTracker  # type: ignore


class LiquidHandlerBiomek:
    """
    Biomek液体处理器的实现类，继承自LiquidHandlerAbstract。
    该类用于处理Biomek液体处理器的特定操作。
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._status = "Idle"  # 初始状态为 Idle
        self._success = False  # 初始成功状态为 False
        self._status_queue = kwargs.get("status_queue", None)  # 状态队列
        self.temp_protocol = {}
        self.py32_path = "/opt/py32"  # Biomek的Python 3.2路径
        
        # 预定义的仪器分类
        self.tip_racks = [
            "BC230", "BC1025F", "BC50", "TipRack200", "TipRack1000",
            "tip", "tips", "Tip", "Tips"
        ]
        
        self.reservoirs = [
            "AgilentReservoir", "nest_12_reservoir_15ml", "nest_1_reservoir_195ml",
            "reservoir", "Reservoir", "waste", "Waste"
        ]
        
        self.plates_96 = [
            "BCDeep96Round", "Matrix96_750uL", "NEST 2ml Deep Well Plate", "nest_96_wellplate_100ul_pcr_full_skirt",
            "nest_96_wellplate_200ul_flat", "Matrix96", "96", "plate", "Plate"
        ]
        
        self.aspirate_techniques = {
            'MC P300 high':{       
                            'Position': 'P1', 
                            'Height': -2.0, 
                            'Volume': '50', 
                            'liquidtype': 'Well Contents', 
                            'WellsX': 12, 
                            'LabwareClass': 'Matrix96_750uL', 
                            'AutoSelectPrototype': True, 
                            'ColsFirst': True, 
                            'CustomHeight': False, 
                            'DataSetPattern': False, 
                            'HeightFrom': 0, 
                            'LocalPattern': True, 
                            'Operation': 'Aspirate', 
                            'OverrideHeight': False, 
                            'Pattern': (True, True, True, True, True, True, True, True, True, True, True, True, True, True, True, True, True, True, True, True, True, True, True, True, True, True, True, True, True, True, True, True, True, True, True, True, True, True, True, True, True, True, True, True, True, True, True, True, True, True, True, True, True, True, True, True, True, True, True, True, True, True, True, True, True, True, True, True, True, True, True, True, True, True, True, True, True, True, True, True, True, True, True, True, True, True, True, True, True, True, True, True, True, True, True, True),
                            'Prototype': 'MC P300 High', 
                            'ReferencedPattern': '', 
                            'RowsFirst': False, 
                            'SectionExpression': '', 
                            'SelectionInfo': (1,), 
                            'SetMark': True, 
                            'Source': True, 
                            'StartAtMark': False, 
                            'StartAtSelection': True, 
                            'UseExpression': False},
            }
      
        self.dispense_techniques = {
            'MC P300 high':{       
                          'Position': 'P11', 
                          'Height': -2.0, 
                          'Volume': '50', 
                          'liquidtype': 'Tip Contents', 
                          'WellsX': 12, 
                          'LabwareClass': 'Matrix96_750uL', 
                          'AutoSelectPrototype': True, 
                          'ColsFirst': True, 
                          'CustomHeight': False, 
                          'DataSetPattern': False, 
                          'HeightFrom': 0, 
                          'LocalPattern': True, 
                          'Operation': 'Dispense', 
                          'OverrideHeight': False, 
                          'Pattern': (True, True, True, True, True, True, True, True, True, True, True, True, True, True, True, True, True, True, True, True, True, True, True, True, True, True, True, True, True, True, True, True, True, True, True, True, True, True, True, True, True, True, True, True, True, True, True, True, True, True, True, True, True, True, True, True, True, True, True, True, True, True, True, True, True, True, True, True, True, True, True, True, True, True, True, True, True, True, True, True, True, True, True, True, True, True, True, True, True, True, True, True, True, True, True, True), 
                          'Prototype': 'MC P300 High', 
                          'ReferencedPattern': '', 
                          'RowsFirst': False, 
                          'SectionExpression': '', 
                          'SelectionInfo': (1,), 
                          'SetMark': True, 
                          'Source': False, 
                          'StartAtMark': False, 
                          'StartAtSelection': True, 
                          'UseExpression': False}
        }

    def _get_instrument_type(self, class_name: str) -> str:
        """
        根据class_name判断仪器类型
        
        Returns:
            str: "tip_rack", "reservoir", "plate_96", 或 "unknown"
        """
        # 检查是否是枪头架
        for tip_name in self.tip_racks:
            if tip_name in class_name:
                return "tip_rack"
        
        # 检查是否是储液槽
        for reservoir_name in self.reservoirs:
            if reservoir_name in class_name:
                return "reservoir"
        
        # 检查是否是96孔板
        for plate_name in self.plates_96:
            if plate_name in class_name:
                return "plate_96"
        
        return "unknown"

    @classmethod
    def deserialize(cls, data: dict, allow_marshal: bool = False) -> LiquidHandler:
        return LiquidHandler.deserialize(data, allow_marshal)

    @property
    def success(self):
        """
        获取操作是否成功的状态。

        Returns:
            bool: 如果操作成功，返回True；否则返回False。
        """
        return self._success

    def create_protocol(
        self,
        protocol_name: str,
        protocol_description: str,
        protocol_version: str,
        protocol_author: str,
        protocol_date: str,
        protocol_type: str,
        none_keys: List[str] = [],
    ):
        """
        创建一个新的协议。

        Args:
            protocol_name (str): 协议名称
            protocol_description (str): 协议描述
            protocol_version (str): 协议版本
            protocol_author (str): 协议作者
            protocol_date (str): 协议日期
            protocol_type (str): 协议类型
            none_keys (List[str]): 需要设置为None的键列表

        Returns:
            dict: 创建的协议字典
        """
        self.temp_protocol = {
            "meta": {
                "name": protocol_name,
                "description": protocol_description,
                "version": protocol_version,
                "author": protocol_author,
                "date": protocol_date,
                "type": protocol_type,
            },
            "labwares": {},
            "steps": [],
        }
        return self.temp_protocol

    def run_protocol(self):
        """
        执行创建的实验流程。
        工作站的完整执行流程是，
        从 create_protocol 开始，创建新的 method，
        随后执行 transfer_liquid 等操作向实验流程添加步骤，
        最后 run_protocol 执行整个方法。

        Returns:
            dict: 执行结果
        """
        #use popen or subprocess to create py32 process and communicate send the temp protocol to it
        if not self.temp_protocol:
            raise ValueError("No protocol created. Please create a protocol first.")

        # 模拟执行协议
        self._status = "Running"
        self._success = True
        # 在这里可以添加实际执行协议的逻辑

        response = requests.post("localhost:5000/api/protocols", json=self.temp_protocol)

    def create_resource(
        self,
        resource_tracker: DeviceNodeResourceTracker,
        resources: list[Resource],
        bind_parent_id: str,
        bind_location: dict[str, float],
        liquid_input_slot: list[int],
        liquid_type: list[str],
        liquid_volume: list[int],
        slot_on_deck: int,
    ):
        """
        创建一个新的资源。

        Args:
            device_id (str): 设备ID
            res_id (str): 资源ID
            class_name (str): 资源类名
            parent (str): 父级ID
            bind_locations (Point): 绑定位置
            liquid_input_slot (list[int]): 液体输入槽列表
            liquid_type (list[str]): 液体类型列表
            liquid_volume (list[int]): 液体体积列表
            slot_on_deck (int): 甲板上的槽位

        Returns:
            dict: 创建的资源字典
        """
        # TODO：需要对好接口，下面这个是临时的
        for resource in resources:
            res_id = resource.id
            class_name = resource.name
            parent = bind_parent_id
            liquid_input_slot = liquid_input_slot
            liquid_type = liquid_type
            liquid_volume = liquid_volume
            slot_on_deck = slot_on_deck

            resource = {
                "id": res_id,
                "class": class_name,
                "parent": parent,
                "bind_locations": bind_location,
                "liquid_input_slot": liquid_input_slot,
                "liquid_type": liquid_type,
                "liquid_volume": liquid_volume,
                "slot_on_deck": slot_on_deck,
            }
            self.temp_protocol["labwares"].append(resource)
        return resources

    def transfer_liquid(
        self,
        sources: Sequence[Container],
        targets: Sequence[Container],
        tip_racks: Sequence[TipRack],
        *,
        use_channels: Optional[List[int]] = None,
        asp_vols: Union[List[float], float],
        dis_vols: Union[List[float], float],
        asp_flow_rates: Optional[List[Optional[float]]] = None,
        dis_flow_rates: Optional[List[Optional[float]]] = None,
        offsets: Optional[List[Coordinate]] = None,
        touch_tip: bool = False,
        liquid_height: Optional[List[Optional[float]]] = None,
        blow_out_air_volume: Optional[List[Optional[float]]] = None,
        spread: Literal["wide", "tight", "custom"] = "wide",
        is_96_well: bool = False,
        mix_stage: Optional[Literal["none", "before", "after", "both"]] = "none",
        mix_times: Optional[int] = None,
        mix_vol: Optional[int] = None,
        mix_rate: Optional[int] = None,
        mix_liquid_height: Optional[float] = None,
        delays: Optional[List[int]] = None,
        none_keys: List[str] = []
    ):

        transfer_params = {
            "Span8": False,
            "Pod": "Pod1",
            "items": {},                      
            "Wash": False,
            "Dynamic?": True,
            "AutoSelectActiveWashTechnique": False,
            "ActiveWashTechnique": "",
            "ChangeTipsBetweenDests": False,
            "ChangeTipsBetweenSources": False,
            "DefaultCaption": "",             
            "UseExpression": False,
            "LeaveTipsOn": False,
            "MandrelExpression": "",
            "Repeats": "1",
            "RepeatsByVolume": False,
            "Replicates": "1",
            "ShowTipHandlingDetails": False,
            "ShowTransferDetails": True,
            "Solvent": "Well Content",
            "Span8Wash": False,
            "Span8WashVolume": "2",
            "Span8WasteVolume": "1",
            "SplitVolume": False,
            "SplitVolumeCleaning": False,
            "Stop": "Destinations",
            "TipLocation": "BC1025F",
            "UseCurrentTips": False,    
            "UseDisposableTips": True,
            "UseFixedTips": False,
            "UseJIT": True,
            "UseMandrelSelection": True,
            "UseProbes": [True, True, True, True, True, True, True, True],
            "WashCycles": "1",
            "WashVolume": "110%",
            "Wizard": False
        }

        items: dict = {}
        for idx, (src, dst) in enumerate(zip(sources, targets)):
            items[str(idx)] = {
                "Source": str(src),
                "Destination": str(dst),
                "Volume": dis_vols[idx] 
            }
        transfer_params["items"] = items

        transfer_params["Solvent"] = "Water"
        TipLocation = tip_racks[0].name
        transfer_params["TipLocation"] = TipLocation

        if len(tip_racks) == 1:
                transfer_params['UseCurrentTips'] = True
        elif len(tip_racks) > 1:
            transfer_params["ChangeTipsBetweenDests"] = True

        self.temp_protocol["steps"].append(transfer_params)

        return 

    def instrument_setup_biomek(
        self,
        id: str,
        parent: str,
        slot_on_deck: str,
        class_name: str,
        liquid_type: list[str],
        liquid_volume: list[int],
        liquid_input_wells: list[str],
    ):
        """
        设置Biomek仪器的参数配置，按照DeckItems格式
        
        根据不同的仪器类型（容器、tip rack等）设置相应的参数结构
        位置作为键，配置列表作为值
        """
        
        # 判断仪器类型
        instrument_type = self._get_instrument_type(class_name)
        
        config = None  # 初始化为None

        if instrument_type == "reservoir":
            # 储液槽类型配置
            config = {
                "Properties": {
                    "Name": id, # 使用id作为名称
                    "Device": "",
                    "liquidtype": liquid_type[0] if liquid_type else "Water",
                    "BarCode": "",
                    "SenseEveryTime": False
                },
                "Known": True,
                "Class": f"LabwareClasses\\{class_name}",
                "DataSets": {"Volume": {}},
                "RuntimeDataSets": {"Volume": {}},
                "EvalAmounts": (float(liquid_volume[0]),) if liquid_volume else (0,),
                "Nominal": False,
                "EvalLiquids": (liquid_type[0],) if liquid_type else ("Water",)
            }
            
        elif instrument_type == "plate_96":
            # 96孔板类型配置
            volume_per_well = float(liquid_volume[0]) if liquid_volume else 0
            liquid_per_well = liquid_type[0] if liquid_type else "Water"
            
            config = {
                "Properties": {
                    "Name": id, # 使用id作为名称
                    "Device": "",
                    "liquidtype": liquid_per_well,
                    "BarCode": "",
                    "SenseEveryTime": False
                },
                "Known": True,
                "Class": f"LabwareClasses\\{class_name}",
                "DataSets": {"Volume": {}},
                "RuntimeDataSets": {"Volume": {}},
                "EvalAmounts": tuple([volume_per_well] * 96),
                "Nominal": False,
                "EvalLiquids": tuple([liquid_per_well] * 96)
            }
            
        elif instrument_type == "tip_rack":
            # 枪头架类型配置
            tip_config = {
                "Class": "TipClasses\\T230",
                "Contents": [],
                "_RT_Contents": [],
                "Used": False,
                "RT_Used": False,
                "Dirty": False,
                "RT_Dirty": False,
                "MaxVolumeUsed": 0.0,
                "RT_MaxVolumeUsed": 0.0
            }
            
            config = {
                "Tips": tip_config,
                "RT_Tips": tip_config.copy(),
                "Properties": {},
                "Known": False,
                "Class": f"LabwareClasses\\{class_name}",
                "DataSets": {"Volume": {}},
                "RuntimeDataSets": {"Volume": {}}
            }
        
        # 按照DeckItems格式存储：位置作为键，配置列表作为值
        if config is not None:
            self.temp_protocol["labwares"][slot_on_deck] = [config]
        else:
            # 空位置
            self.temp_protocol["labwares"][slot_on_deck] = []
        
        return

    def transfer_biomek(
        self,
        source: str,
        target: str,
        tip_rack: str,
        volume: float,
        aspirate_techniques: str,
        dispense_techniques: str,
    ):
        """
        处理Biomek的液体转移操作。

        """
        items = []

        asp_params = copy.deepcopy(self.aspirate_techniques[aspirate_techniques])
        dis_params = copy.deepcopy(self.dispense_techniques[dispense_techniques])

        asp_params['Position'] = source
        dis_params['Position'] = target
        asp_params['Volume'] = str(volume)
        dis_params['Volume'] = str(volume)

        items.append(asp_params)
        items.append(dis_params)

        transfer_params = {
            "Span8": False,
            "Pod": "Pod1",
            "items": [],                      
            "Wash": False,
            "Dynamic?": True,
            "AutoSelectActiveWashTechnique": False,
            "ActiveWashTechnique": "",
            "ChangeTipsBetweenDests": True,
            "ChangeTipsBetweenSources": False,
            "DefaultCaption": "",             
            "UseExpression": False,
            "LeaveTipsOn": False,
            "MandrelExpression": "",
            "Repeats": "1",
            "RepeatsByVolume": False,
            "Replicates": "1",
            "ShowTipHandlingDetails": False,
            "ShowTransferDetails": True,
            "Solvent": "Water",
            "Span8Wash": False,
            "Span8WashVolume": "2",
            "Span8WasteVolume": "1",
            "SplitVolume": False,
            "SplitVolumeCleaning": False,
            "Stop": "Destinations",
            "TipLocation": "BC230",
            "UseCurrentTips": False,    
            "UseDisposableTips": False,
            "UseFixedTips": False,
            "UseJIT": True,
            "UseMandrelSelection": True,
            "UseProbes": [True, True, True, True, True, True, True, True],
            "WashCycles": "4",
            "WashVolume": "110%",
            "Wizard": False
        }
        transfer_params["items"] = items
        transfer_params["Solvent"] = 'Water'
        transfer_params["TipLocation"] = tip_rack
        tmp={'transfer': transfer_params}
        self.temp_protocol["steps"].append(tmp)

        return


    def move_biomek(
        self,
        source: str,
        target: str,
    ):
        """
        处理Biomek移动板子的操作。

        """

        move_params = {
            "Pod": "Pod1",
            "GripSide": "A1 near",
            "Source": source,
            "Target": target,
            "LeaveBottomLabware": False,
        }
        self.temp_protocol["steps"].append(move_params)

        return
    
    def incubation_biomek(
        self,
        time: int,
    ):
        """
        处理Biomek的孵育操作。
        """
        incubation_params = {
            "Message": "Paused",
            "Location": "the whole system",
            "Time": time,
            "Mode": "TimedResource"
        }
        self.temp_protocol["steps"].append(incubation_params)

        return
    
    def oscillation_biomek(
        self,
        rpm: int,
        time: int,
    ):
        """
        处理Biomek的振荡操作。
        """   
        oscillation_params = {
            'Device': 'OrbitalShaker0',
            'Parameters': (str(rpm), '2', str(time), 'CounterClockwise'), 
            'Command': 'Timed Shake'
        }
        self.temp_protocol["steps"].append(oscillation_params)

        return


if __name__ == "__main__":

    print("=== Biomek完整流程测试 ===")
    print("包含: 仪器设置 + 完整实验步骤")
    
    # 完整的步骤信息（从biomek.py复制）
    steps_info = '''
    {
  "steps": [
    {
      "step_number": 1,
      "operation": "transfer",
      "description": "转移PCR产物或酶促反应液至0.5ml 96孔板中",
      "parameters": {
        "source": "P1",
        "target": "P11",
        "tip_rack": "BC230",
        "volume": 50
      }
    },
    {
      "step_number": 2,
      "operation": "transfer",
      "description": "加入2倍体积的Bind Beads BC至产物中",
      "parameters": {
        "source": "P2",
        "target": "P11",
        "tip_rack": "BC230",
        "volume": 100
      }
    },
    {
      "step_number": 3,
      "operation": "oscillation",
      "description": "振荡混匀300秒",
      "parameters": {
        "rpm": 800,
        "time": 300
      }
    },
    {
      "step_number": 4,
      "operation": "move_labware",
      "description": "转移至96孔磁力架上吸附3分钟",
      "parameters": {
        "source": "P11",
        "target": "P12"
      }
    },
    {
      "step_number": 5,
      "operation": "incubation",
      "description": "吸附3分钟",
      "parameters": {
        "time": 180
      }
    },
    {
      "step_number": 6,
      "operation": "transfer",
      "description": "吸弃或倒除上清液",
      "parameters": {
        "source": "P12",
        "target": "P22",
        "tip_rack": "BC230",
        "volume": 150
      }
    },
    {
      "step_number": 7,
      "operation": "transfer",
      "description": "加入300-500μl 75%乙醇",
      "parameters": {
        "source": "P3",
        "target": "P12",
        "tip_rack": "BC230",
        "volume": 400
      }
    },
    {
      "step_number": 8,
      "operation": "move_labware",
      "description": "移动至振荡器进行振荡混匀",
      "parameters": {
        "source": "P12",
        "target": "Orbital1"
      }
    },
    {
      "step_number": 9,
      "operation": "oscillation",
      "description": "振荡混匀60秒",
      "parameters": {
        "rpm": 800,
        "time": 60
      }
    },
    {
      "step_number": 10,
      "operation": "move_labware",
      "description": "转移至96孔磁力架上吸附3分钟",
      "parameters": {
        "source": "Orbital1",
        "target": "P12"
      }
    },
    {
      "step_number": 11,
      "operation": "incubation",
      "description": "吸附3分钟",
      "parameters": {
        "time": 180
      }
    },
    {
      "step_number": 12,
      "operation": "transfer",
      "description": "吸弃或倒弃废液",
      "parameters": {
        "source": "P12",
        "target": "P22",
        "tip_rack": "BC230",
        "volume": 400
      }
    },
    {
      "step_number": 13,
      "operation": "transfer",
      "description": "重复加入75%乙醇",
      "parameters": {
        "source": "P3",
        "target": "P12",
        "tip_rack": "BC230",
        "volume": 400
      }
    },
    {
      "step_number": 14,
      "operation": "move_labware",
      "description": "移动至振荡器进行振荡混匀",
      "parameters": {
        "source": "P12",
        "target": "Orbital1"
      }
    },
    {
      "step_number": 15,
      "operation": "oscillation",
      "description": "振荡混匀60秒",
      "parameters": {
        "rpm": 800,
        "time": 60
      }
    },
    {
      "step_number": 16,
      "operation": "move_labware",
      "description": "转移至96孔磁力架上吸附3分钟",
      "parameters": {
        "source": "Orbital1",
        "target": "P12"
      }
    },
    {
      "step_number": 17,
      "operation": "incubation",
      "description": "吸附3分钟",
      "parameters": {
        "time": 180
      }
    },
    {
      "step_number": 18,
      "operation": "transfer",
      "description": "吸弃或倒弃废液",
      "parameters": {
        "source": "P12",
        "target": "P22",
        "tip_rack": "BC230",
        "volume": 400
      }
    },
    {
      "step_number": 19,
      "operation": "move_labware",
      "description": "正放96孔板，空气干燥15分钟",
      "parameters": {
        "source": "P12",
        "target": "P13"
      }
    },
    {
      "step_number": 20,
      "operation": "incubation",
      "description": "空气干燥15分钟",
      "parameters": {
        "time": 900
      }
    },
    {
      "step_number": 21,
      "operation": "transfer",
      "description": "加入30-50μl Elution Buffer",
      "parameters": {
        "source": "P4",
        "target": "P13",
        "tip_rack": "BC230",
        "volume": 40
      }
    },
    {
      "step_number": 22,
      "operation": "move_labware",
      "description": "移动至振荡器进行振荡混匀",
      "parameters": {
        "source": "P13",
        "target": "Orbital1"
      }
    },
    {
      "step_number": 23,
      "operation": "oscillation",
      "description": "振荡混匀60秒",
      "parameters": {
        "rpm": 800,
        "time": 60
      }
    },
    {
      "step_number": 24,
      "operation": "move_labware",
      "description": "室温静置3分钟",
      "parameters": {
        "source": "Orbital1",
        "target": "P13"
      }
    },
    {
      "step_number": 25,
      "operation": "incubation",
      "description": "室温静置3分钟",
      "parameters": {
        "time": 180
      }
    },
    {
      "step_number": 26,
      "operation": "move_labware",
      "description": "转移至96孔磁力架上吸附2分钟",
      "parameters": {
        "source": "P13",
        "target": "P12"
      }
    },
    {
      "step_number": 27,
      "operation": "incubation",
      "description": "吸附2分钟",
      "parameters": {
        "time": 120
      }
    },
    {
      "step_number": 28,
      "operation": "transfer",
      "description": "将DNA转移至新的板中",
      "parameters": {
        "source": "P12",
        "target": "P14",
        "tip_rack": "BC230",
        "volume": 40
          }
        }
      ]
    }
'''
    # 完整的labware配置信息
    labware_with_liquid = '''
    [
    {
        "id": "Tip Rack BC230 TL1",
        "parent": "deck",
        "slot_on_deck": "TL1",
        "class_name": "BC230",
        "liquid_type": [],
        "liquid_volume": [],
        "liquid_input_wells": []
    },
    {
        "id": "Tip Rack BC230 TL2",
        "parent": "deck",
        "slot_on_deck": "TL2",
        "class_name": "BC230",
        "liquid_type": [],
        "liquid_volume": [],
        "liquid_input_wells": []
    },
    {
        "id": "Tip Rack BC230 TL3",
        "parent": "deck",
        "slot_on_deck": "TL3",
        "class_name": "BC230",
        "liquid_type": [],
        "liquid_volume": [],
        "liquid_input_wells": []
    },
    {
        "id": "Tip Rack BC230 TL4",
        "parent": "deck",
        "slot_on_deck": "TL4",
        "class_name": "BC230",
        "liquid_type": [],
        "liquid_volume": [],
        "liquid_input_wells": []
    },
    {
        "id": "Tip Rack BC230 TL5",
        "parent": "deck",
        "slot_on_deck": "TL5",
        "class_name": "BC230",
        "liquid_type": [],
        "liquid_volume": [],
        "liquid_input_wells": []
    },
    {
        "id": "Tip Rack BC230 P5",
        "parent": "deck",
        "slot_on_deck": "P5",
        "class_name": "BC230",
        "liquid_type": [],
        "liquid_volume": [],
        "liquid_input_wells": []
    },
    {
        "id": "Tip Rack BC230 P6",
        "parent": "deck",
        "slot_on_deck": "P6",
        "class_name": "BC230",
        "liquid_type": [],
        "liquid_volume": [],
        "liquid_input_wells": []
    },
    {
        "id": "Tip Rack BC230 P15",
        "parent": "deck",
        "slot_on_deck": "P15",
        "class_name": "BC230",
        "liquid_type": [],
        "liquid_volume": [],
        "liquid_input_wells": []
    },
    {
        "id": "Tip Rack BC230 P16",
        "parent": "deck",
        "slot_on_deck": "P16",
        "class_name": "BC230",
        "liquid_type": [],
        "liquid_volume": [],
        "liquid_input_wells": []
    },
    {
        "id": "stock plate on P1",
        "parent": "deck",
        "slot_on_deck": "P1",
        "class_name": "AgilentReservoir",
        "liquid_type": ["PCR product"],
        "liquid_volume": [5000],
        "liquid_input_wells": ["A1"]
    },
    {
        "id": "stock plate on P2",
        "parent": "deck",
        "slot_on_deck": "P2",
        "class_name": "AgilentReservoir",
        "liquid_type": ["bind beads"],
        "liquid_volume": [100000],
        "liquid_input_wells": ["A1"]
    },
    {
        "id": "stock plate on P3",
        "parent": "deck",
        "slot_on_deck": "P3",
        "class_name": "AgilentReservoir",
        "liquid_type": ["75% ethanol"],
        "liquid_volume": [100000],
        "liquid_input_wells": ["A1"]
    },
    {
        "id": "stock plate on P4",
        "parent": "deck",
        "slot_on_deck": "P4",
        "class_name": "AgilentReservoir",
        "liquid_type": ["Elution Buffer"],
        "liquid_volume": [5000],
        "liquid_input_wells": ["A1"]
    },
    {
        "id": "working plate on P11",
        "parent": "deck",
        "slot_on_deck": "P11",
        "class_name": "BCDeep96Round",
        "liquid_type": [],
        "liquid_volume": [],
        "liquid_input_wells": []
    },
    {
        "id": "working plate on P13",
        "parent": "deck",
        "slot_on_deck": "P13",
        "class_name": "BCDeep96Round",
        "liquid_type": [],
        "liquid_volume": [],
        "liquid_input_wells": []
    },
    {
        "id": "working plate on P14",
        "parent": "deck",
        "slot_on_deck": "P14",
        "class_name": "BCDeep96Round",
        "liquid_type": [],
        "liquid_volume": [],
        "liquid_input_wells": []
    },
    {
        "id": "waste on P22",
        "parent": "deck",
        "slot_on_deck": "P22",
        "class_name": "AgilentReservoir",
        "liquid_type": [],
        "liquid_volume": [],
        "liquid_input_wells": []
    },
    {
        "id": "oscillation",
        "parent": "deck",
        "slot_on_deck": "Orbital1",
        "class_name": "Orbital",
        "liquid_type": [],
        "liquid_volume": [],
        "liquid_input_wells": []
    }
    ]
    '''

    # 创建handler实例
    handler = LiquidHandlerBiomek()
    
    # 创建协议
    protocol = handler.create_protocol(
        protocol_name="DNA纯化完整流程",
        protocol_description="使用磁珠进行DNA纯化的完整自动化流程",
        protocol_version="1.0",
        protocol_author="Biomek系统",
        protocol_date="2024-01-01",
        protocol_type="DNA_purification"
    )

    print("\n=== 第一步：设置所有仪器 ===")
    # 解析labware配置
    labwares = json.loads(labware_with_liquid)
    
    # 设置所有仪器
    instrument_count = 0
    for labware in labwares:
        print(f"设置仪器: {labware['id']} ({labware['class_name']}) 在位置 {labware['slot_on_deck']}")
        handler.instrument_setup_biomek(
            id=labware['id'],
            parent=labware['parent'],
            slot_on_deck=labware['slot_on_deck'],
            class_name=labware['class_name'],
            liquid_type=labware['liquid_type'],
            liquid_volume=labware['liquid_volume'],
            liquid_input_wells=labware['liquid_input_wells']
        )
        instrument_count += 1
    
    print(f"总共设置了 {instrument_count} 个仪器位置")

    print("\n=== 第二步：执行实验步骤 ===")
    # 解析步骤信息
    input_steps = json.loads(steps_info)
    
    # 执行所有步骤
    step_count = 0
    for step in input_steps['steps']:
        operation = step['operation']
        parameters = step['parameters']
        description = step['description']
        
        print(f"步骤 {step['step_number']}: {description}")
        
        if operation == 'transfer':
            handler.transfer_biomek(
                source=parameters['source'],
                target=parameters['target'],
                volume=parameters['volume'],
                tip_rack=parameters['tip_rack'],
                aspirate_techniques='MC P300 high',
                dispense_techniques='MC P300 high'
            )
        elif operation == 'move_labware':
            handler.move_biomek(
                source=parameters['source'],
                target=parameters['target']
            )
        elif operation == 'oscillation':
            handler.oscillation_biomek(
                rpm=parameters['rpm'],
                time=parameters['time']
            )
        elif operation == 'incubation':
            handler.incubation_biomek(
                time=parameters['time']
            )
        
        step_count += 1
    
    print(f"总共执行了 {step_count} 个步骤")

    print("\n=== 第三步：保存完整协议 ===")
    # 获取脚本目录
    script_dir = pathlib.Path(__file__).parent
    
    # 保存完整协议
    complete_output_path = script_dir / "complete_biomek_protocol_0608.json"
    with open(complete_output_path, 'w', encoding='utf-8') as f:
        json.dump(handler.temp_protocol, f, indent=4, ensure_ascii=False)
    
    print(f"完整协议已保存到: {complete_output_path}")
  
    print("\n=== 测试完成 ===")
    print("完整的DNA纯化流程已成功转换为Biomek格式！")
