from unilabos.resources.itemized_carrier import Bottle
import uuid

class EIT_Bottle(Bottle):
    """
    EIT 专用瓶子类：
    拦截条码的序列化与反序列化逻辑，解决 pylabrobot 基类不支持字符串条码的 Bug。
    """
    
    def serialize(self) -> dict:
        """【输出同步】将对象转为 JSON 时，隐藏字符串条码防止基类报错"""
        real_barcode = self.barcode
        self.barcode = None  # 临时隐藏
        data = super().serialize()
        self.barcode = real_barcode  # 恢复
        data["barcode"] = real_barcode  # 手动存入字符串
        return data

    @classmethod
    def deserialize(cls, data: dict, allow_marshal: bool = True):
        """【输入重建】从 JSON 转为对象时，拦截字符串条码防止基类解析崩溃"""
        # 1. 提取条码内容
        barcode_val = data.get("barcode")
        
        # 2. 如果条码是字符串，先将其设为 None，
        # 这样 super().deserialize (pylabrobot) 就不会尝试调用 Barcode.deserialize(str)
        if isinstance(barcode_val, str):
            data["barcode"] = None
            
        # 3. 调用基类逻辑重建资源对象
        resource = super().deserialize(data, allow_marshal=allow_marshal)
        
        # 4. 重建完成后，将字符串条码重新赋值给实例属性
        if isinstance(barcode_val, str):
            resource.barcode = barcode_val
            
        return resource

def EIT_REAGENT_BOTTLE_2ML(name: str, **kwargs) -> EIT_Bottle:
    kwargs.update({"model": "502000353"})
    res = EIT_Bottle(name=name, diameter=7.5, height=45.0, max_volume=2000.0, **kwargs)
    res.unilabos_uuid = str(uuid.uuid4())
    return res

def EIT_REAGENT_BOTTLE_8ML(name: str, **kwargs) -> EIT_Bottle:
    kwargs.update({"model": "220000005"})
    res = EIT_Bottle(name=name, diameter=15.0, height=60.0, max_volume=8000.0, **kwargs)
    res.unilabos_uuid = str(uuid.uuid4())
    return res

def EIT_REAGENT_BOTTLE_40ML(name: str, **kwargs) -> EIT_Bottle:
    kwargs.update({"model": "220000092"})
    res = EIT_Bottle(name=name, diameter=22.0, height=85.0, max_volume=40000.0, **kwargs)
    res.unilabos_uuid = str(uuid.uuid4())
    return res

def EIT_REAGENT_BOTTLE_125ML(name: str, **kwargs) -> EIT_Bottle:
    kwargs.update({"model": "220000008"})
    res = EIT_Bottle(name=name, diameter=34.0, height=120.0, max_volume=125000.0, **kwargs)
    res.unilabos_uuid = str(uuid.uuid4())
    return res

def EIT_POWDER_BUCKET_30ML(name: str, **kwargs) -> EIT_Bottle:
    kwargs.update({"model": "201000816"})
    res = EIT_Bottle(name=name, diameter=23.0, height=60.0, max_volume=30000.0, **kwargs)
    res.unilabos_uuid = str(uuid.uuid4())
    return res

def EIT_FLASH_FILTER_INNER_BOTTLE(name: str, **kwargs) -> EIT_Bottle:
    kwargs.update({"model": "220000320"})
    res = EIT_Bottle(name=name, diameter=8.0, height=55.0, max_volume=30000.0, **kwargs)
    res.unilabos_uuid = str(uuid.uuid4())
    return res

def EIT_FLASH_FILTER_OUTER_BOTTLE(name: str, **kwargs) -> EIT_Bottle:
    kwargs.update({"model": "220000321"})
    res = EIT_Bottle(name=name, diameter=9.0, height=60.0, max_volume=40000.0, **kwargs)
    res.unilabos_uuid = str(uuid.uuid4())
    return res

def EIT_REACTION_SEAL_CAP(name: str, **kwargs) -> EIT_Bottle:
    kwargs.update({"model": "211009427"})
    res = EIT_Bottle(name=name, diameter=45.0, height=12.0, max_volume=0.0, **kwargs)
    res.unilabos_uuid = str(uuid.uuid4())
    return res

def EIT_REACTION_TUBE_2ML(name: str, **kwargs) -> EIT_Bottle:
    kwargs.update({"model": "551000502"})
    res = EIT_Bottle(name=name, diameter=11.0, height=45.0, max_volume=2000.0, **kwargs)
    res.unilabos_uuid = str(uuid.uuid4())
    return res

def EIT_TEST_TUBE_MAGNET_2ML(name: str, **kwargs) -> EIT_Bottle:
    kwargs.update({"model": "220000322"})
    res = EIT_Bottle(name=name, diameter=11.0, height=45.0, max_volume=2000.0, **kwargs)
    res.unilabos_uuid = str(uuid.uuid4())
    return res
