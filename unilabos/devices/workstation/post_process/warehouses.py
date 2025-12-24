from unilabos.devices.workstation.post_process.post_process_warehouse import WareHouse, warehouse_factory



# =================== Other ===================


def post_process_warehouse_4x3x1(name: str) -> WareHouse:
    """创建post_process 4x3x1仓库"""
    return warehouse_factory(
        name=name,
        num_items_x=4,
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

def post_process_warehouse_4x3x1_2(name: str) -> WareHouse:
    """已弃用：创建post_process 4x3x1仓库"""
    return warehouse_factory(
        name=name,
        num_items_x=4,
        num_items_y=3,
        num_items_z=1,
        dx=12.0,
        dy=12.0,
        dz=12.0,
        item_dx=137.0,
        item_dy=96.0,
        item_dz=120.0,
        category="warehouse",
    )
