使用plr_test.json启动，将Well加入Plate中

```bash
ros2 action send_goal /devices/host_node/create_resource_detailed unilabos_msgs/action/_resource_create_from_outer/ResourceCreateFromOuter "{ resources: [ { 'category': '', 'children': [], 'config': { 'type': 'Well', 'size_x': 6.86, 'size_y': 6.86, 'size_z': 10.67, 'rotation': { 'x': 0, 'y': 0, 'z': 0, 'type': 'Rotation' }, 'category': 'well', 'model': null, 'max_volume': 360, 'material_z_thickness': 0.5, 'compute_volume_from_height': null, 'compute_height_from_volume': null, 'bottom_type': 'flat', 'cross_section_type': 'circle' }, 'data': { 'liquids': [], 'pending_liquids': [], 'liquid_history': [] }, 'id': 'plate_well_11_7', 'name': 'plate_well_11_7', 'pose': { 'orientation': { 'w': 1.0, 'x': 0.0, 'y': 0.0, 'z': 0.0 }, 'position': { 'x': 0.0, 'y': 0.0, 'z': 0.0 } }, 'sample_id': '', 'parent': 'plate', 'type': 'device' } ], device_ids: [ 'PLR_STATION' ], bind_parent_ids: [ 'plate' ], bind_locations: [ { 'x': 0.0, 'y': 0.0, 'z': 0.0 } ], other_calling_params: [ '{}' ] }"
```

使用mock_all.json启动，重新捕获MockContainerForChiller1

```bash
ros2 action send_goal /devices/host_node/create_resource unilabos_msgs/action/_resource_create_from_outer_easy/ResourceCreateFromOuterEasy "{ 'res_id': 'MockContainerForChiller1', 'device_id': 'MockChiller1', 'class_name': 'container', 'parent': 'MockChiller1', 'bind_locations': { 'x': 0.0, 'y': 0.0, 'z': 0.0 }, 'liquid_input_slot': [ -1 ], 'liquid_type': [ 'CuCl2' ], 'liquid_volume': [ 100.0 ], 'slot_on_deck': '' }"
```