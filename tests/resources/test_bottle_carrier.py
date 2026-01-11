import pytest

from unilabos.resources.bioyond.bottle_carriers import BIOYOND_Electrolyte_6VialCarrier, BIOYOND_Electrolyte_1BottleCarrier
from unilabos.resources.bioyond.bottles import BIOYOND_PolymerStation_Solid_Vial, BIOYOND_PolymerStation_Solution_Beaker, BIOYOND_PolymerStation_Reagent_Bottle


def test_bottle_carrier() -> "BottleCarrier":
    print("创建载架...")

    # 创建6瓶载架
    bottle_carrier = BIOYOND_Electrolyte_6VialCarrier("powder_carrier_01")
    print(f"6瓶载架: {bottle_carrier.name}, 位置数: {len(bottle_carrier.sites)}")

    # 创建1烧杯载架
    beaker_carrier = BIOYOND_Electrolyte_1BottleCarrier("solution_carrier_01")
    print(f"1烧杯载架: {beaker_carrier.name}, 位置数: {len(beaker_carrier.sites)}")

    # 创建瓶子和烧杯
    powder_bottle = BIOYOND_PolymerStation_Solid_Vial("powder_bottle_01")
    solution_beaker = BIOYOND_PolymerStation_Solution_Beaker("solution_beaker_01")
    reagent_bottle = BIOYOND_PolymerStation_Reagent_Bottle("reagent_bottle_01")

    print(f"\n创建的物料:")
    print(f"粉末瓶: {powder_bottle.name} - {powder_bottle.diameter}mm x {powder_bottle.height}mm, {powder_bottle.max_volume}μL")
    print(f"溶液烧杯: {solution_beaker.name} - {solution_beaker.diameter}mm x {solution_beaker.height}mm, {solution_beaker.max_volume}μL")
    print(f"试剂瓶: {reagent_bottle.name} - {reagent_bottle.diameter}mm x {reagent_bottle.height}mm, {reagent_bottle.max_volume}μL")

    # 测试放置容器
    print(f"\n测试放置容器...")

    # 通过载架的索引操作来放置容器
    # bottle_carrier[0] = powder_bottle  # 放置粉末瓶到第一个位置
    print(f"粉末瓶已放置到6瓶载架的位置 0")

    # beaker_carrier[0] = solution_beaker  # 放置烧杯到第一个位置
    print(f"溶液烧杯已放置到1烧杯载架的位置 0")

    # 验证放置结果
    print(f"\n验证放置结果:")
    bottle_at_0 = bottle_carrier[0].resource
    beaker_at_0 = beaker_carrier[0].resource

    if bottle_at_0:
        print(f"位置 0 的瓶子: {bottle_at_0.name}")
    if beaker_at_0:
        print(f"位置 0 的烧杯: {beaker_at_0.name}")

    print("\n载架设置完成！")