from unilabos.resources.warehouse import WareHouse, warehouse_factory

# ================ 反应站相关堆栈 ================

def bioyond_warehouse_1x4x4(name: str) -> WareHouse:
    """创建BioYond 4x4x1仓库 (左侧堆栈: A01～D04)

    使用行优先排序，前端展示为:
    A01 | A02 | A03 | A04
    B01 | B02 | B03 | B04
    C01 | C02 | C03 | C04
    D01 | D02 | D03 | D04
    """
    return warehouse_factory(
        name=name,
        num_items_x=4,  # 4列
        num_items_y=4,  # 4行
        num_items_z=1,
        dx=10.0,
        dy=10.0,
        dz=10.0,
        item_dx=147.0,
        item_dy=106.0,
        item_dz=130.0,
        category="warehouse",
        col_offset=0,  # 从01开始: A01, A02, A03, A04
        layout="row-major",  # ⭐ 改为行优先排序
    )

def bioyond_warehouse_1x4x4_right(name: str) -> WareHouse:
    """创建BioYond 4x4x1仓库 (右侧堆栈: A05～D08)"""
    return warehouse_factory(
        name=name,
        num_items_x=4,
        num_items_y=4,
        num_items_z=1,
        dx=10.0,
        dy=10.0,
        dz=10.0,
        item_dx=147.0,
        item_dy=106.0,
        item_dz=130.0,
        category="warehouse",
        col_offset=4,  # 从05开始: A05, A06, A07, A08
        layout="row-major",  # ⭐ 改为行优先排序
    )

def bioyond_warehouse_density_vial(name: str) -> WareHouse:
    """创建测量小瓶仓库(测密度) - 竖向排列2列3行
    布局（从下到上，从左到右）：
    | A03 | B03 |  ← 顶部
    | A02 | B02 |  ← 中部
    | A01 | B01 |  ← 底部
    """
    return warehouse_factory(
        name=name,
        num_items_x=2,  # 2列（A, B）
        num_items_y=3,  # 3行（01-03，从下到上）
        num_items_z=1,  # 1层
        dx=10.0,
        dy=10.0,
        dz=10.0,
        item_dx=40.0,  # 列间距（A到B的横向距离）
        item_dy=40.0,  # 行间距（01到02到03的竖向距离）
        item_dz=50.0,
        # ⭐ 竖向warehouse：槽位尺寸也是竖向的（小瓶已经是正方形，无需调整）
        resource_size_x=30.0,
        resource_size_y=30.0,
        resource_size_z=12.0,
        category="warehouse",
        col_offset=0,
        layout="vertical-col-major",  # ⭐ 竖向warehouse专用布局
    )

def bioyond_warehouse_reagent_storage(name: str) -> WareHouse:
    """创建BioYond站内试剂存放堆栈 - 竖向排列1列2行
    布局（竖向，从下到上）：
    | A02 |  ← 顶部
    | A01 |  ← 底部
    """
    return warehouse_factory(
        name=name,
        num_items_x=1,  # 1列
        num_items_y=2,  # 2行（01-02，从下到上）
        num_items_z=1,  # 1层
        dx=10.0,
        dy=10.0,
        dz=10.0,
        item_dx=96.0,   # 列间距（这里只有1列，不重要）
        item_dy=137.0,  # 行间距（A01到A02的竖向距离）
        item_dz=120.0,
        # ⭐ 竖向warehouse：交换槽位尺寸，使槽位框也是竖向的
        resource_size_x=86.0,   # 原来的 resource_size_y
        resource_size_y=127.0,  # 原来的 resource_size_x
        resource_size_z=25.0,
        category="warehouse",
        layout="vertical-col-major",  # ⭐ 竖向warehouse专用布局
    )

def bioyond_warehouse_tipbox_storage(name: str) -> WareHouse:
    """创建BioYond站内Tip盒堆栈（A01～B03），用于存放枪头盒"""
    return warehouse_factory(
        name=name,
        num_items_x=3,  # 3列（01-03）
        num_items_y=2,  # 2行（A-B）
        num_items_z=1,  # 1层
        dx=10.0,
        dy=10.0,
        dz=10.0,
        item_dx=137.0,
        item_dy=96.0,
        item_dz=120.0,
        category="warehouse",
        col_offset=0,
        layout="row-major",
    )

def bioyond_warehouse_liquid_preparation(name: str) -> WareHouse:
    """已弃用,创建BioYond移液站内10%分装液体准备仓库（A01～B04）"""
    return warehouse_factory(
        name=name,
        num_items_x=4,  # 4列（01-04）
        num_items_y=2,  # 2行（A-B）
        num_items_z=1,  # 1层
        dx=10.0,
        dy=10.0,
        dz=10.0,
        item_dx=137.0,
        item_dy=96.0,
        item_dz=120.0,
        category="warehouse",
        col_offset=0,
        layout="row-major",
    )

# ================ 配液站相关堆栈 ================

def bioyond_warehouse_reagent_stack(name: str) -> WareHouse:
    """创建BioYond 试剂堆栈 2x4x1 (2行×4列: A01-A04, B01-B04)

    使用行优先排序，前端展示为:
    A01 | A02 | A03 | A04
    B01 | B02 | B03 | B04
    """
    return warehouse_factory(
        name=name,
        num_items_x=4,  # 4列 (01-04)
        num_items_y=2,  # 2行 (A-B)
        num_items_z=1,  # 1层
        dx=10.0,
        dy=10.0,
        dz=10.0,
        item_dx=147.0,
        item_dy=106.0,
        item_dz=130.0,
        category="warehouse",
        col_offset=0,  # 从01开始
        layout="row-major",  # ⭐ 使用行优先排序: A01,A02,A03,A04, B01,B02,B03,B04
    )

 # 定义bioyond的堆栈

# =================== Other ===================

def bioyond_warehouse_1x4x2(name: str) -> WareHouse:
    """创建BioYond 4x2x1仓库"""
    return warehouse_factory(
        name=name,
        num_items_x=1,
        num_items_y=4,
        num_items_z=2,
        dx=10.0,
        dy=10.0,
        dz=10.0,
        item_dx=137.0,
        item_dy=96.0,
        item_dz=120.0,
        category="warehouse",
        removed_positions=None
    )

def bioyond_warehouse_1x2x2(name: str) -> WareHouse:
    """创建BioYond 1x2x2仓库"""
    return warehouse_factory(
        name=name,
        num_items_x=1,
        num_items_y=2,
        num_items_z=2,
        dx=10.0,
        dy=10.0,
        dz=10.0,
        item_dx=137.0,
        item_dy=96.0,
        item_dz=120.0,
        category="warehouse",
    )

def bioyond_warehouse_10x1x1(name: str) -> WareHouse:
    """创建BioYond 10x1x1仓库"""
    return warehouse_factory(
        name=name,
        num_items_x=10,
        num_items_y=1,
        num_items_z=1,
        dx=10.0,
        dy=10.0,
        dz=10.0,
        item_dx=137.0,
        item_dy=96.0,
        item_dz=120.0,
        category="warehouse",
    )

def bioyond_warehouse_1x3x3(name: str) -> WareHouse:
    """创建BioYond 1x3x3仓库"""
    return warehouse_factory(
        name=name,
        num_items_x=1,
        num_items_y=3,
        num_items_z=3,
        dx=10.0,
        dy=10.0,
        dz=10.0,
        item_dx=137.0,
        item_dy=96.0,
        item_dz=120.0,
        category="warehouse",
    )

def bioyond_warehouse_2x1x3(name: str) -> WareHouse:
    """创建BioYond 2x1x3仓库"""
    return warehouse_factory(
        name=name,
        num_items_x=2,
        num_items_y=1,
        num_items_z=3,
        dx=10.0,
        dy=10.0,
        dz=10.0,
        item_dx=137.0,
        item_dy=96.0,
        item_dz=120.0,
        category="warehouse",
    )

def bioyond_warehouse_3x3x1(name: str) -> WareHouse:
    """创建BioYond 3x3x1仓库"""
    return warehouse_factory(
        name=name,
        num_items_x=3,
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

def bioyond_warehouse_5x1x1(name: str) -> WareHouse:
    """已弃用：创建BioYond 5x1x1仓库"""
    return warehouse_factory(
        name=name,
        num_items_x=5,
        num_items_y=1,
        num_items_z=1,
        dx=10.0,
        dy=10.0,
        dz=10.0,
        item_dx=137.0,
        item_dy=96.0,
        item_dz=120.0,
        category="warehouse",
    )

def bioyond_warehouse_3x3x1_2(name: str) -> WareHouse:
    """已弃用：创建BioYond 3x3x1仓库"""
    return warehouse_factory(
        name=name,
        num_items_x=3,
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

def bioyond_warehouse_liquid_and_lid_handling(name: str) -> WareHouse:
    """创建BioYond开关盖加液模块台面"""
    return warehouse_factory(
        name=name,
        num_items_x=2,
        num_items_y=5,
        num_items_z=1,
        dx=10.0,
        dy=10.0,
        dz=10.0,
        item_dx=137.0,
        item_dy=96.0,
        item_dz=120.0,
        category="warehouse",
        removed_positions=None
    )

def bioyond_warehouse_1x8x4(name: str) -> WareHouse:
    """创建BioYond 8x4x1反应站堆栈（A01～D08）"""
    return warehouse_factory(
        name=name,
        num_items_x=8,  # 8列（01-08）
        num_items_y=4,  # 4行（A-D）
        num_items_z=1,  # 1层
        dx=10.0,
        dy=10.0,
        dz=10.0,
        item_dx=147.0,
        item_dy=106.0,
        item_dz=130.0,
        category="warehouse",
    )
