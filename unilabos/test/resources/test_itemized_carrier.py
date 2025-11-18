#!/usr/bin/env python3
"""
测试修改后的 get_child_identifier 函数
"""

from unilabos.resources.itemized_carrier import ItemizedCarrier, Bottle
from pylabrobot.resources.coordinate import Coordinate

def test_get_child_identifier_with_indices():
    """测试返回x,y,z索引的 get_child_identifier 函数"""
    
    # 创建一些测试瓶子
    bottle1 = Bottle("bottle1", diameter=25.0, height=50.0, max_volume=15.0)
    bottle1.location = Coordinate(10, 20, 5)
    
    bottle2 = Bottle("bottle2", diameter=25.0, height=50.0, max_volume=15.0) 
    bottle2.location = Coordinate(50, 20, 5)
    
    bottle3 = Bottle("bottle3", diameter=25.0, height=50.0, max_volume=15.0) 
    bottle3.location = Coordinate(90, 20, 5)
    
    # 创建载架，指定维度
    sites = {
        "A1": bottle1,
        "A2": bottle2, 
        "A3": bottle3,
        "B1": None,  # 空位
        "B2": None,
        "B3": None
    }
    
    carrier = ItemizedCarrier(
        name="test_carrier",
        size_x=150,
        size_y=100,
        size_z=30,
        num_items_x=3,  # 3列
        num_items_y=2,  # 2行  
        num_items_z=1,  # 1层
        sites=sites
    )
    
    print("测试载架维度:")
    print(f"num_items_x: {carrier.num_items_x}")
    print(f"num_items_y: {carrier.num_items_y}")
    print(f"num_items_z: {carrier.num_items_z}")
    print()
    
    # 测试获取bottle1的标识符信息 (A1 = idx:0, x:0, y:0, z:0)
    result1 = carrier.get_child_identifier(bottle1)
    print("测试bottle1 (A1):")
    print(f"  identifier: {result1['identifier']}")
    print(f"  idx: {result1['idx']}")
    print(f"  x index: {result1['x']}")
    print(f"  y index: {result1['y']}")
    print(f"  z index: {result1['z']}")
    
    # Assert 验证 bottle1 (A1) 的结果
    assert result1['identifier'] == 'A1', f"Expected identifier 'A1', got '{result1['identifier']}'"
    assert result1['idx'] == 0, f"Expected idx 0, got {result1['idx']}"
    assert result1['x'] == 0, f"Expected x index 0, got {result1['x']}"
    assert result1['y'] == 0, f"Expected y index 0, got {result1['y']}"
    assert result1['z'] == 0, f"Expected z index 0, got {result1['z']}"
    print("  ✓ bottle1 (A1) 测试通过")
    print()
    
    # 测试获取bottle2的标识符信息 (A2 = idx:1, x:1, y:0, z:0)
    result2 = carrier.get_child_identifier(bottle2)
    print("测试bottle2 (A2):")
    print(f"  identifier: {result2['identifier']}")
    print(f"  idx: {result2['idx']}")
    print(f"  x index: {result2['x']}")
    print(f"  y index: {result2['y']}")
    print(f"  z index: {result2['z']}")
    
    # Assert 验证 bottle2 (A2) 的结果
    assert result2['identifier'] == 'A2', f"Expected identifier 'A2', got '{result2['identifier']}'"
    assert result2['idx'] == 1, f"Expected idx 1, got {result2['idx']}"
    assert result2['x'] == 1, f"Expected x index 1, got {result2['x']}"
    assert result2['y'] == 0, f"Expected y index 0, got {result2['y']}"
    assert result2['z'] == 0, f"Expected z index 0, got {result2['z']}"
    print("  ✓ bottle2 (A2) 测试通过")
    print()
        
    # 测试获取bottle3的标识符信息 (A3 = idx:2, x:2, y:0, z:0)
    result3 = carrier.get_child_identifier(bottle3)
    print("测试bottle3 (A3):")
    print(f"  identifier: {result3['identifier']}")
    print(f"  idx: {result3['idx']}")
    print(f"  x index: {result3['x']}")
    print(f"  y index: {result3['y']}")
    print(f"  z index: {result3['z']}")
    
    # Assert 验证 bottle3 (A3) 的结果
    assert result3['identifier'] == 'A3', f"Expected identifier 'A3', got '{result3['identifier']}'"
    assert result3['idx'] == 2, f"Expected idx 2, got {result3['idx']}"
    assert result3['x'] == 2, f"Expected x index 2, got {result3['x']}"
    assert result3['y'] == 0, f"Expected y index 0, got {result3['y']}"
    assert result3['z'] == 0, f"Expected z index 0, got {result3['z']}"
    print("  ✓ bottle3 (A3) 测试通过")
    print()
    
    # 测试错误情况：查找不存在的资源
    bottle_not_exists = Bottle("bottle_not_exists", diameter=25.0, height=50.0, max_volume=15.0)
    try:
        carrier.get_child_identifier(bottle_not_exists)
        assert False, "应该抛出 ValueError 异常"
    except ValueError as e:
        print("✓ 正确抛出了 ValueError 异常：", str(e))
        assert "is not assigned to this carrier" in str(e), "异常消息应该包含预期的文本"
    
    print("\n🎉 所有测试都通过了！")

if __name__ == "__main__":
    test_get_child_identifier_with_indices()