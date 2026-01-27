from unilabos.resources.warehouse import WareHouse, warehouse_factory

def eit_warehouse_W(name: str) -> WareHouse:
    """创建eit W仓库 (左侧W区堆栈: W-1-1～W-4-8)
    """
    return warehouse_factory(
        name=name,
        num_items_x=8,  # 8列
        num_items_y=4,  # 4行
        num_items_z=1,
        dx=10.0,
        dy=10.0,
        dz=10.0,
        item_dx=140.0,
        item_dy=98.0,
        item_dz=120.0,
        resource_size_x=127.8,
        resource_size_y=85.5,
        resource_size_z=40.0,
        category="warehouse",
        col_offset=0,  # 从01开始: A01, A02, A03, A04
        layout="row-major",  # ⭐ 改为行优先排序
        name_by_layout_code=True,
    )

def eit_warehouse_TB(name: str) -> WareHouse:
    """创建eit TB仓库 (右侧TB区堆栈)"""
    return warehouse_factory(
        name=name,
        num_items_x=4,
        num_items_y=2,
        num_items_z=1,
        dx=10.0,
        dy=10.0,
        dz=10.0,
        item_dx=140.0,
        item_dy=98.0,
        item_dz=120.0,
        resource_size_x=127.8,
        resource_size_y=85.5,
        resource_size_z=40.0,
        category="warehouse",
        col_offset=0, 
        layout="row-major",
        name_by_layout_code=True,
    )

def eit_warehouse_N(name: str) -> WareHouse:
    """创建eit N仓库 (右侧N区堆栈)"""
    return warehouse_factory(
        name=name,
        num_items_x=9,
        num_items_y=1,
        num_items_z=1,
        dx=10.0,
        dy=10.0,
        dz=10.0,
        item_dx=140.0,
        item_dy=98.0,
        item_dz=120.0,
        resource_size_x=127.8,
        resource_size_y=85.5,
        resource_size_z=40.0,
        category="warehouse",
        col_offset=0,
        layout="row-major",
        name_by_layout_code=True,
    )

def eit_warehouse_T(name: str) -> WareHouse:
    """创建eit T仓库 (T区堆栈)"""
    return warehouse_factory(
        name=name,
        num_items_x=3,
        num_items_y=2,
        num_items_z=1,
        dx=10.0,
        dy=10.0,
        dz=10.0,
        item_dx=140.0,
        item_dy=98.0,
        item_dz=120.0,
        resource_size_x=127.8,
        resource_size_y=85.5,
        resource_size_z=40.0,
        category="warehouse",
        col_offset=0,
        layout="row-major",  # ⭐ 改为行优先排序
        name_by_layout_code=True,
    )

def eit_warehouse_MSB(name: str) -> WareHouse:
    """创建eit MSB仓库"""
    return warehouse_factory(
        name=name,
        num_items_x=2,
        num_items_y=1,
        num_items_z=1,
        dx=10.0,
        dy=10.0,
        dz=10.0,
        item_dx=140.0,
        item_dy=98.0,
        item_dz=120.0,
        resource_size_x=127.8,
        resource_size_y=85.5,
        resource_size_z=40.0,
        category="warehouse",
        col_offset=0,
        layout="row-major",  # ⭐ 改为行优先排序
        name_by_layout_code=True,
    )

def eit_warehouse_SC(name: str) -> WareHouse:
    """创建eit SC仓库 (SC区堆栈)"""
    return warehouse_factory(
        name=name,
        num_items_x=2,
        num_items_y=1,
        num_items_z=1,
        dx=10.0,
        dy=10.0,
        dz=10.0,
        item_dx=140.0,
        item_dy=98.0,
        item_dz=120.0,
        resource_size_x=127.8,
        resource_size_y=85.5,
        resource_size_z=40.0,
        category="warehouse",
        col_offset=0,
        layout="row-major",  # ⭐ 改为行优先排序
        name_by_layout_code=True,
    )

def eit_warehouse_TS(name: str) -> WareHouse:
    """创建eit TS仓库 (TS区堆栈)"""
    return warehouse_factory(
        name=name,
        num_items_x=2,
        num_items_y=1,
        num_items_z=1,
        dx=10.0,
        dy=10.0,
        dz=10.0,
        item_dx=140.0,
        item_dy=98.0,
        item_dz=120.0,
        resource_size_x=127.8,
        resource_size_y=85.5,
        resource_size_z=40.0,
        category="warehouse",
        col_offset=0,
        layout="row-major",  # ⭐ 改为行优先排序
        name_by_layout_code=True,
    )

def eit_warehouse_MS(name: str) -> WareHouse:
    """创建eit MS仓库 (MS区堆栈)"""
    return warehouse_factory(
        name=name,
        num_items_x=3,
        num_items_y=1,
        num_items_z=1,
        dx=10.0,
        dy=10.0,
        dz=10.0,
        item_dx=140.0,
        item_dy=98.0,
        item_dz=120.0,
        resource_size_x=127.8,
        resource_size_y=85.5,
        resource_size_z=40.0,
        category="warehouse",
        col_offset=0,
        layout="row-major",  # ⭐ 改为行优先排序
        name_by_layout_code=True,
    )

def eit_warehouse_FF(name: str) -> WareHouse:
    """创建eit FF仓库 (FF区堆栈)"""
    return warehouse_factory(
        name=name,
        num_items_x=2,
        num_items_y=1,
        num_items_z=1,
        dx=10.0,
        dy=10.0,
        dz=10.0,
        item_dx=140.0,
        item_dy=98.0,
        item_dz=120.0,
        resource_size_x=127.8,
        resource_size_y=85.5,
        resource_size_z=40.0,
        category="warehouse",
        col_offset=0,
        layout="row-major",  # ⭐ 改为行优先排序
        name_by_layout_code=True,
    )

def eit_warehouse_AS(name: str) -> WareHouse:
    """创建eit AS仓库 (AS区堆栈)"""
    return warehouse_factory(
        name=name,
        num_items_x=2,
        num_items_y=1,
        num_items_z=1,
        dx=10.0,
        dy=10.0,
        dz=10.0,
        item_dx=140.0,
        item_dy=98.0,
        item_dz=120.0,
        resource_size_x=127.8,
        resource_size_y=85.5,
        resource_size_z=40.0,
        category="warehouse",
        col_offset=0,
        layout="row-major",  # ⭐ 改为行优先排序
        name_by_layout_code=True,
    )
