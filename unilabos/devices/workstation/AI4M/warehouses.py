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
        custom_keys=["Station_1"],  # 使用数字1作为编号
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
        custom_keys=["Station_2"],  # 使用数字2作为编号
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
        custom_keys=["Station_3"],  # 使用数字3作为编号
    )


def Raw_electrode_warehouse_3x5x1(name: str) -> WareHouse:
    """创建原始电极 3x5x1仓库（序号从右往左：右=1, 中=2, 左=3）"""
    return warehouse_factory(
        name=name,
        num_items_x=3,
        num_items_y=5,
        num_items_z=1,
        dx=10.0,
        dy=10.0,
        dz=10.0,
        item_dx=137.0,
        item_dy=96.0,
        item_dz=120.0,
        category="warehouse",
        naming_mode="continuous_number",
        reverse_col_order=True,
    )

def Finished_electrode_warehouse_3x5x1(name: str) -> WareHouse:
    """创建完成电极 3x5x1仓库（序号从右往左：右=1, 中=2, 左=3）"""
    return warehouse_factory(
        name=name,
        num_items_x=3,
        num_items_y=5,
        num_items_z=1,
        dx=10.0,
        dy=10.0,
        dz=10.0,
        item_dx=137.0,
        item_dy=96.0,
        item_dz=120.0,
        category="warehouse",
        naming_mode="continuous_number",
        reverse_col_order=True,
    )

def Stir_1_warehouse_1x1x1(name: str) -> WareHouse:
    """创建搅拌仪 1x1x1仓库"""
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
        custom_keys=["Stir_1"],  # 使用数字0作为编号
    )

def Stir_2_warehouse_1x1x1(name: str) -> WareHouse:
    """创建搅拌仪 1x1x1仓库"""
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
        custom_keys=["Stir_2"],  # 使用数字0作为编号
    )

def Water_wash_warehouse_1x1x1(name: str) -> WareHouse:
    """创建水洗 1x1x1仓库"""
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
        custom_keys=["Wash"],  # 使用数字0作为编号
    )

def Acid_wash_warehouse_1x1x1(name: str) -> WareHouse:
    """创建酸洗 1x1x1仓库"""
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
        custom_keys=["Acid"],  # 使用数字0作为编号
    )