from unilabos.registry.decorators import resource
from unilabos.resources.warehouse import WareHouse, warehouse_factory


@resource(
    id="szlab_poly_s1_loading_buffer_warehouse",
    category=["szlab_poly_studio", "warehouse"],
    description="苏州实验室 S1 上料过渡仓",
)
def s1_loading_buffer_warehouse(name: str = "S1上料过渡仓") -> WareHouse:
    return warehouse_factory(
        name=name,
        num_items_x=4,
        num_items_y=1,
        num_items_z=1,
        dx=10.0,
        dy=10.0,
        dz=10.0,
        item_dx=137.0,
        item_dy=96.0,
        item_dz=120.0,
        layout="row-major",
        category="warehouse",
    )


@resource(
    id="szlab_poly_s3_unused_beaker_warehouse",
    category=["szlab_poly_studio", "warehouse"],
    description="苏州实验室 S3 未使用烧杯仓，3组x6位",
)
def s3_unused_beaker_warehouse(name: str = "S3未使用烧杯仓") -> WareHouse:
    return warehouse_factory(
        name=name,
        num_items_x=6,
        num_items_y=3,
        num_items_z=1,
        dx=10.0,
        dy=10.0,
        dz=10.0,
        item_dx=80.0,
        item_dy=96.0,
        item_dz=120.0,
        layout="row-major",
        category="warehouse",
    )


@resource(
    id="szlab_poly_s3_unused_sample_vial_warehouse",
    category=["szlab_poly_studio", "warehouse"],
    description="苏州实验室 S3 未使用样品瓶仓，3组x6位",
)
def s3_unused_sample_vial_warehouse(name: str = "S3未使用样品瓶仓") -> WareHouse:
    return warehouse_factory(
        name=name,
        num_items_x=6,
        num_items_y=3,
        num_items_z=1,
        dx=10.0,
        dy=10.0,
        dz=10.0,
        item_dx=60.0,
        item_dy=80.0,
        item_dz=120.0,
        layout="row-major",
        category="warehouse",
    )


@resource(
    id="szlab_poly_s11_used_beaker_warehouse",
    category=["szlab_poly_studio", "warehouse"],
    description="苏州实验室 S11 使用烧杯成品仓，3组x6位",
)
def s11_used_beaker_warehouse(name: str = "S11使用烧杯成品仓") -> WareHouse:
    return warehouse_factory(
        name=name,
        num_items_x=6,
        num_items_y=3,
        num_items_z=1,
        dx=10.0,
        dy=10.0,
        dz=10.0,
        item_dx=80.0,
        item_dy=96.0,
        item_dz=120.0,
        layout="row-major",
        category="warehouse",
    )


@resource(
    id="szlab_poly_s11_used_sample_vial_warehouse",
    category=["szlab_poly_studio", "warehouse"],
    description="苏州实验室 S11 使用样品瓶成品仓，3组x6位",
)
def s11_used_sample_vial_warehouse(name: str = "S11使用样品瓶成品仓") -> WareHouse:
    return warehouse_factory(
        name=name,
        num_items_x=6,
        num_items_y=3,
        num_items_z=1,
        dx=10.0,
        dy=10.0,
        dz=10.0,
        item_dx=60.0,
        item_dy=80.0,
        item_dz=120.0,
        layout="row-major",
        category="warehouse",
    )


@resource(
    id="szlab_poly_s2_tip_placeholder_warehouse",
    category=["szlab_poly_studio", "warehouse"],
    description="苏州实验室 S2 枪头仓占位，6位",
)
def s2_tip_placeholder_warehouse(name: str = "S2枪头仓占位") -> WareHouse:
    return warehouse_factory(
        name=name,
        num_items_x=6,
        num_items_y=1,
        num_items_z=1,
        dx=10.0,
        dy=10.0,
        dz=10.0,
        item_dx=60.0,
        item_dy=80.0,
        item_dz=120.0,
        layout="row-major",
        category="warehouse",
    )


@resource(
    id="szlab_poly_powder_container_placeholder_warehouse",
    category=["szlab_poly_studio", "warehouse"],
    description="苏州实验室固体粉桶仓占位，2组x3位",
)
def powder_container_placeholder_warehouse(name: str = "固体粉桶仓占位") -> WareHouse:
    return warehouse_factory(
        name=name,
        num_items_x=3,
        num_items_y=2,
        num_items_z=1,
        dx=10.0,
        dy=10.0,
        dz=10.0,
        item_dx=90.0,
        item_dy=110.0,
        item_dz=120.0,
        layout="row-major",
        category="warehouse",
    )


@resource(
    id="szlab_poly_s10_liquid_reagent_placeholder_warehouse",
    category=["szlab_poly_studio", "warehouse"],
    description="苏州实验室 S10 液体试剂瓶仓占位，4组x5位",
)
def s10_liquid_reagent_placeholder_warehouse(name: str = "S10液体试剂瓶仓占位") -> WareHouse:
    return warehouse_factory(
        name=name,
        num_items_x=5,
        num_items_y=4,
        num_items_z=1,
        dx=10.0,
        dy=10.0,
        dz=10.0,
        item_dx=70.0,
        item_dy=90.0,
        item_dz=120.0,
        layout="row-major",
        category="warehouse",
    )
