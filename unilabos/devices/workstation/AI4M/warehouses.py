from unilabos.devices.workstation.AI4M.AI4M_warehouse import WareHouse, warehouse_factory



# =================== Other ===================


def Hydrogel_warehouse_5x3x1(name: str) -> WareHouse:
    """创建水凝胶模块 5x3x1仓库"""
    return warehouse_factory(
        name=name,
        num_items_x=5,
        num_items_y=3,
        num_items_z=1,
        dx=10.0,
        dy=10.0,
        dz=10.0,
        item_dx=137.0,
        item_dy=96.0,
        item_dz=120.0,
        category="warehouse",
    )

def Station_1_warehouse_1x1x1(name: str) -> WareHouse:
    """创建检测工站 1x1x1仓库"""
    return warehouse_factory(
        name=name,
        num_items_x=1,
        num_items_y=1,
        num_items_z=1,
        dx=10.0,
        dy=10.0,
        dz=10.0,
        item_dx=137.0,
        item_dy=96.0,
        item_dz=120.0,
        category="warehouse",
        custom_keys=[1],  # 使用数字1作为编号
    )

def Station_2_warehouse_1x1x1(name: str) -> WareHouse:
    """创建检测工站 1x1x1仓库"""
    return warehouse_factory(
        name=name,
        num_items_x=1,
        num_items_y=1,
        num_items_z=1,
        dx=10.0,
        dy=10.0,
        dz=10.0,
        item_dx=137.0,
        item_dy=96.0,
        item_dz=120.0,
        category="warehouse",
        custom_keys=[2],  # 使用数字2作为编号
    )

def Station_3_warehouse_1x1x1(name: str) -> WareHouse:
    """创建检测工站 1x1x1仓库"""
    return warehouse_factory(
        name=name,
        num_items_x=1,
        num_items_y=1,
        num_items_z=1,
        dx=10.0,
        dy=10.0,
        dz=10.0,
        item_dx=137.0,
        item_dy=96.0,
        item_dz=120.0,
        category="warehouse",
        custom_keys=[3],  # 使用数字3作为编号
    )

