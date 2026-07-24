"""列对齐取枪头测试（LiquidHandlerAbstract._acquire_tip_column）。

列式 8 通道硬件（``_pickup_column_aligned=True``）取整列枪头时，若当前列剩余不足以从
列首取整列，应跳过残余枪头、从下一整列开头取（被跳过的视为弃用）。``n_ch==1`` 或未开启
列对齐时行为不变。

纯抽象层逻辑测试：用 dummy spot + 带 ``num_items_y`` 的 stub rack，无需真实 PLR 资源。
依赖 PRCXI/PLR import 链，环境无 PLR 时整体 skip。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest

try:
    from unilabos.devices.liquid_handling.liquid_handler_abstract import (
        LiquidHandlerAbstract,
    )

    _PLR_AVAILABLE = True
    _PLR_IMPORT_ERROR: Optional[Exception] = None
except Exception as exc:  # pragma: no cover - 环境相关
    LiquidHandlerAbstract = None  # type: ignore[assignment, misc]
    _PLR_AVAILABLE = False
    _PLR_IMPORT_ERROR = exc


_skip_if_no_plr = pytest.mark.skipif(
    not _PLR_AVAILABLE,
    reason=f"pylabrobot not importable in this env: {_PLR_IMPORT_ERROR!r}",
)


class _StubRack:
    """仅提供 num_items_y（列高）。"""

    def __init__(self, num_items_y: int) -> None:
        self.num_items_y = num_items_y


def _make_handler(
    *,
    column_aligned: bool,
    ny: int = 8,
    pool_size: int = 80,
    next_index: int = 0,
) -> Any:
    """构造跳过 __init__ 的 LiquidHandlerAbstract，仅设置枪头池相关属性。"""
    inst: Any = LiquidHandlerAbstract.__new__(LiquidHandlerAbstract)
    inst._pickup_column_aligned = column_aligned
    inst._active_tip_type_key = "k"
    inst._tip_flat_spots: Dict[str, List[Any]] = {"k": [f"s{i}" for i in range(pool_size)]}
    inst._tip_racks_by_type = {"k": [_StubRack(ny)]}
    inst._tip_next_index = {"k": next_index}
    return inst


@_skip_if_no_plr
class TestAcquireTipColumn:
    def test_mid_column_skips_to_next_column(self) -> None:
        """idx=3（列中）+ n_ch=8 → 跳过 3..7 残余，取下一整列 flat[8:16]。"""
        h = _make_handler(column_aligned=True, ny=8, next_index=3)
        tips = h._acquire_tip_column(8)
        assert tips == [f"s{i}" for i in range(8, 16)]
        assert h._tip_next_index["k"] == 16

    def test_aligned_passthrough(self) -> None:
        """idx=0（列首）+ n_ch=8 → 直接取 flat[0:8]。"""
        h = _make_handler(column_aligned=True, ny=8, next_index=0)
        tips = h._acquire_tip_column(8)
        assert tips == [f"s{i}" for i in range(0, 8)]
        assert h._tip_next_index["k"] == 8

    def test_single_channel_no_align(self) -> None:
        """n_ch=1 → 不对齐，从当前 idx 取 1 个。"""
        h = _make_handler(column_aligned=True, ny=8, next_index=3)
        tips = h._acquire_tip_column(1)
        assert tips == ["s3"]
        assert h._tip_next_index["k"] == 4

    def test_flag_off_no_align(self) -> None:
        """未开启列对齐 → 即便 idx=3、n_ch=8 也不对齐（取 flat[3:11]）。"""
        h = _make_handler(column_aligned=False, ny=8, next_index=3)
        tips = h._acquire_tip_column(8)
        assert tips == [f"s{i}" for i in range(3, 11)]
        assert h._tip_next_index["k"] == 11

    def test_already_at_boundary_other_column(self) -> None:
        """idx=8（已是列首）+ n_ch=8 → 不跳过，取 flat[8:16]。"""
        h = _make_handler(column_aligned=True, ny=8, next_index=8)
        tips = h._acquire_tip_column(8)
        assert tips == [f"s{i}" for i in range(8, 16)]
        assert h._tip_next_index["k"] == 16


@_skip_if_no_plr
def test_tip_column_height_reads_num_items_y() -> None:
    h = _make_handler(column_aligned=True, ny=8)
    assert h._tip_column_height("k") == 8
    # 未知 key → 0
    assert h._tip_column_height("nope") == 0
