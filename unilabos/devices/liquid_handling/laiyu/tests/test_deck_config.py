#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
测试脚本：验证更新后的deck配置是否正常工作
"""

import sys
import os
import json

# 添加项目根目录到Python路径
project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, project_root)

def test_config_loading():
    """测试配置文件加载功能"""
    print("=" * 50)
    print("测试配置文件加载功能")
    print("=" * 50)
    
    try:
        # 直接测试配置文件加载
        config_path = os.path.join(os.path.dirname(__file__), "controllers", "deckconfig.json")
        fallback_path = os.path.join(os.path.dirname(__file__), "config", "deck.json")
        
        config = None
        config_source = ""
        
        if os.path.exists(config_path):
            with open(config_path, 'r', encoding='utf-8') as f:
                config = json.load(f)
            config_source = "config/deckconfig.json"
        elif os.path.exists(fallback_path):
            with open(fallback_path, 'r', encoding='utf-8') as f:
                config = json.load(f)
            config_source = "config/deck.json"
        else:
            print("❌ 配置文件不存在")
            return False
        
        print(f"✅ 配置文件加载成功: {config_source}")
        print(f"   - 甲板尺寸: {config.get('size_x', 'N/A')} x {config.get('size_y', 'N/A')} x {config.get('size_z', 'N/A')}")
        print(f"   - 子模块数量: {len(config.get('children', []))}")
        
        # 检查各个模块是否存在
        modules = config.get('children', [])
        module_types = [module.get('type') for module in modules]
        module_names = [module.get('name') for module in modules]
        
        print(f"   - 模块类型: {', '.join(set(filter(None, module_types)))}")
        print(f"   - 模块名称: {', '.join(filter(None, module_names))}")
        
        return config
    except Exception as e:
        print(f"❌ 配置文件加载失败: {e}")
        return None

def test_module_coordinates(config):
    """测试各模块的坐标信息"""
    print("\n" + "=" * 50)
    print("测试模块坐标信息")
    print("=" * 50)
    
    if not config:
        print("❌ 配置为空，无法测试")
        return False
    
    modules = config.get('children', [])
    
    for module in modules:
        module_name = module.get('name', '未知模块')
        module_type = module.get('type', '未知类型')
        position = module.get('position', {})
        size = module.get('size', {})
        
        print(f"\n模块: {module_name} ({module_type})")
        print(f"   - 位置: ({position.get('x', 0)}, {position.get('y', 0)}, {position.get('z', 0)})")
        print(f"   - 尺寸: {size.get('x', 0)} x {size.get('y', 0)} x {size.get('z', 0)}")
        
        # 检查孔位信息
        wells = module.get('wells', [])
        if wells:
            print(f"   - 孔位数量: {len(wells)}")
            
            # 显示前几个和后几个孔位的坐标
            sample_wells = wells[:3] + wells[-3:] if len(wells) > 6 else wells
            for well in sample_wells:
                well_id = well.get('id', '未知')
                well_pos = well.get('position', {})
                print(f"     {well_id}: ({well_pos.get('x', 0)}, {well_pos.get('y', 0)}, {well_pos.get('z', 0)})")
        else:
            print(f"   - 无孔位信息")
    
    return True

def test_coordinate_ranges(config):
    """测试坐标范围的合理性"""
    print("\n" + "=" * 50)
    print("测试坐标范围合理性")
    print("=" * 50)
    
    if not config:
        print("❌ 配置为空，无法测试")
        return False
    
    deck_size = {
        'x': config.get('size_x', 340),
        'y': config.get('size_y', 250),
        'z': config.get('size_z', 160)
    }
    
    print(f"甲板尺寸: {deck_size['x']} x {deck_size['y']} x {deck_size['z']}")
    
    modules = config.get('children', [])
    all_coordinates = []
    
    for module in modules:
        module_name = module.get('name', '未知模块')
        wells = module.get('wells', [])
        
        for well in wells:
            well_pos = well.get('position', {})
            x, y, z = well_pos.get('x', 0), well_pos.get('y', 0), well_pos.get('z', 0)
            all_coordinates.append((x, y, z, f"{module_name}:{well.get('id', '未知')}"))
    
    if not all_coordinates:
        print("❌ 没有找到任何坐标信息")
        return False
    
    # 计算坐标范围
    x_coords = [coord[0] for coord in all_coordinates]
    y_coords = [coord[1] for coord in all_coordinates]
    z_coords = [coord[2] for coord in all_coordinates]
    
    x_range = (min(x_coords), max(x_coords))
    y_range = (min(y_coords), max(y_coords))
    z_range = (min(z_coords), max(z_coords))
    
    print(f"X坐标范围: {x_range[0]:.2f} ~ {x_range[1]:.2f}")
    print(f"Y坐标范围: {y_range[0]:.2f} ~ {y_range[1]:.2f}")
    print(f"Z坐标范围: {z_range[0]:.2f} ~ {z_range[1]:.2f}")
    
    # 检查是否超出甲板范围
    issues = []
    if x_range[1] > deck_size['x']:
        issues.append(f"X坐标超出甲板范围: {x_range[1]} > {deck_size['x']}")
    if y_range[1] > deck_size['y']:
        issues.append(f"Y坐标超出甲板范围: {y_range[1]} > {deck_size['y']}")
    if z_range[1] > deck_size['z']:
        issues.append(f"Z坐标超出甲板范围: {z_range[1]} > {deck_size['z']}")
    
    if x_range[0] < 0:
        issues.append(f"X坐标为负值: {x_range[0]}")
    if y_range[0] < 0:
        issues.append(f"Y坐标为负值: {y_range[0]}")
    if z_range[0] < 0:
        issues.append(f"Z坐标为负值: {z_range[0]}")
    
    if issues:
        print("⚠️  发现坐标问题:")
        for issue in issues:
            print(f"   - {issue}")
        return False
    else:
        print("✅ 所有坐标都在合理范围内")
        return True

def test_well_spacing(config):
    """测试孔位间距的一致性"""
    print("\n" + "=" * 50)
    print("测试孔位间距一致性")
    print("=" * 50)
    
    if not config:
        print("❌ 配置为空，无法测试")
        return False
    
    modules = config.get('children', [])
    
    for module in modules:
        module_name = module.get('name', '未知模块')
        module_type = module.get('type', '未知类型')
        wells = module.get('wells', [])
        
        if len(wells) < 2:
            continue
        
        print(f"\n模块: {module_name} ({module_type})")
        
        # 计算相邻孔位的间距
        spacings_x = []
        spacings_y = []
        
        # 按行列排序孔位
        wells_by_row = {}
        for well in wells:
            well_id = well.get('id', '')
            if len(well_id) >= 3:  # 如A01格式
                row = well_id[0]
                col = int(well_id[1:])
                if row not in wells_by_row:
                    wells_by_row[row] = {}
                wells_by_row[row][col] = well
        
        # 计算同行相邻孔位的X间距
        for row, cols in wells_by_row.items():
            sorted_cols = sorted(cols.keys())
            for i in range(len(sorted_cols) - 1):
                col1, col2 = sorted_cols[i], sorted_cols[i + 1]
                if col2 == col1 + 1:  # 相邻列
                    pos1 = cols[col1].get('position', {})
                    pos2 = cols[col2].get('position', {})
                    spacing = abs(pos2.get('x', 0) - pos1.get('x', 0))
                    spacings_x.append(spacing)
        
        # 计算同列相邻孔位的Y间距
        cols_by_row = {}
        for well in wells:
            well_id = well.get('id', '')
            if len(well_id) >= 3:
                row = ord(well_id[0]) - ord('A')
                col = int(well_id[1:])
                if col not in cols_by_row:
                    cols_by_row[col] = {}
                cols_by_row[col][row] = well
        
        for col, rows in cols_by_row.items():
            sorted_rows = sorted(rows.keys())
            for i in range(len(sorted_rows) - 1):
                row1, row2 = sorted_rows[i], sorted_rows[i + 1]
                if row2 == row1 + 1:  # 相邻行
                    pos1 = rows[row1].get('position', {})
                    pos2 = rows[row2].get('position', {})
                    spacing = abs(pos2.get('y', 0) - pos1.get('y', 0))
                    spacings_y.append(spacing)
        
        # 检查间距一致性
        if spacings_x:
            avg_x = sum(spacings_x) / len(spacings_x)
            max_diff_x = max(abs(s - avg_x) for s in spacings_x)
            print(f"   - X方向平均间距: {avg_x:.2f}mm, 最大偏差: {max_diff_x:.2f}mm")
            
        if spacings_y:
            avg_y = sum(spacings_y) / len(spacings_y)
            max_diff_y = max(abs(s - avg_y) for s in spacings_y)
            print(f"   - Y方向平均间距: {avg_y:.2f}mm, 最大偏差: {max_diff_y:.2f}mm")
    
    return True

def main():
    """主测试函数"""
    print("LaiYu液体处理设备配置测试")
    print("测试时间:", os.popen('date').read().strip())
    
    # 运行所有测试
    tests = [
        ("配置文件加载", test_config_loading),
    ]
    
    config = None
    results = []
    
    for test_name, test_func in tests:
        try:
            if test_name == "配置文件加载":
                result = test_func()
                config = result if result else None
                results.append((test_name, bool(result)))
            else:
                result = test_func(config)
                results.append((test_name, result))
        except Exception as e:
            print(f"❌ 测试 {test_name} 执行失败: {e}")
            results.append((test_name, False))
    
    # 如果配置加载成功，运行其他测试
    if config:
        additional_tests = [
            ("模块坐标信息", test_module_coordinates),
            ("坐标范围合理性", test_coordinate_ranges),
            ("孔位间距一致性", test_well_spacing)
        ]
        
        for test_name, test_func in additional_tests:
            try:
                result = test_func(config)
                results.append((test_name, result))
            except Exception as e:
                print(f"❌ 测试 {test_name} 执行失败: {e}")
                results.append((test_name, False))
    
    # 输出测试总结
    print("\n" + "=" * 50)
    print("测试总结")
    print("=" * 50)
    
    passed = sum(1 for _, result in results if result)
    total = len(results)
    
    for test_name, result in results:
        status = "✅ 通过" if result else "❌ 失败"
        print(f"   {test_name}: {status}")
    
    print(f"\n总计: {passed}/{total} 个测试通过")
    
    if passed == total:
        print("🎉 所有测试通过！配置更新成功。")
        return True
    else:
        print("⚠️  部分测试失败，需要进一步检查。")
        return False

if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)