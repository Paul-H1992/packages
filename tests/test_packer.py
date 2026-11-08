"""装箱算法与 Excel 解析的单元测试。"""

from __future__ import annotations

import io
import math
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.excel_io import build_template, read_cartons, read_orders  # noqa: E402
from app.models import Carton, Item, PackConfig  # noqa: E402
from app.packer import optimize_order, pack_carton  # noqa: E402

EPS = 1e-6


# ---------------------------------------------------------------------------
# 几何校验工具
# ---------------------------------------------------------------------------


def _z_overlap(a, b) -> bool:
    return not (a.z >= b.z + b.h - EPS or b.z >= a.z + a.h - EPS)


def _circle_rect_overlap(circle, rect) -> bool:
    cx = min(max(circle.cx, rect.x), rect.x + rect.w)
    cy = min(max(circle.cy, rect.y), rect.y + rect.l)
    dist = math.hypot(circle.cx - cx, circle.cy - cy)
    return dist < circle.d / 2.0 - 1e-6


def overlaps(a, b) -> bool:
    if not _z_overlap(a, b):
        return False
    if a.kind == "bottle" and b.kind == "bottle":
        dist = math.hypot(a.cx - b.cx, a.cy - b.cy)
        return dist < (a.d + b.d) / 2.0 - 1e-6
    if a.kind == "bottle":
        return _circle_rect_overlap(a, b)
    if b.kind == "bottle":
        return _circle_rect_overlap(b, a)
    return not (
        a.x >= b.x + b.w - EPS
        or b.x >= a.x + a.w - EPS
        or a.y >= b.y + b.l - EPS
        or b.y >= a.y + a.l - EPS
    )


def assert_valid(plan, carton: Carton) -> None:
    """校验：全部在箱内、互不重叠、瓶装直立。"""
    for p in plan.placements:
        assert p.x >= -EPS and p.y >= -EPS and p.z >= -EPS
        assert p.x + p.w <= carton.length + 1e-4, f"{p.sku} 超出内长"
        assert p.y + p.l <= carton.width + 1e-4, f"{p.sku} 超出内宽"
        assert p.z + p.h <= carton.height + 1e-4, f"{p.sku} 超出内高"
        if p.kind == "bottle":
            # 圆柱始终竖直：外接正方形边长 = 直径，层高 = 瓶高
            assert p.h > 0
            assert p.w == pytest.approx(p.d)
            assert p.l == pytest.approx(p.d)

    for i in range(len(plan.placements)):
        for j in range(i + 1, len(plan.placements)):
            a, b = plan.placements[i], plan.placements[j]
            assert not overlaps(a, b), f"物品重叠：{a.sku} 与 {b.sku}"


# ---------------------------------------------------------------------------
# 算法测试
# ---------------------------------------------------------------------------


def test_hex_bottle_layer_capacity():
    """100×100 底面、直径 10 的圆瓶：六边形排布每层 105 个。"""
    carton = Carton(code="C1", length=100, width=100, height=1000)
    item = Item(sku="B", kind="bottle", qty=105, diameter=10, height=100, weight=1)
    pool = [[item, item.qty]]
    plan = pack_carton(carton, pool, PackConfig(pattern="hex"))

    assert len(plan.placements) == 105
    assert pool[0][1] == 0
    for p in plan.placements:
        assert p.z == pytest.approx(0.0)
        assert p.h == pytest.approx(100.0)
    assert_valid(plan, carton)


def test_square_vs_hex_density():
    """六边形交错应当比方形对齐放得更多。"""
    carton = Carton(code="C1", length=600, width=400, height=1000)
    item = Item(sku="B", kind="bottle", qty=100000, diameter=10, height=100, weight=1)

    hex_pool = [[item, item.qty]]
    hex_plan = pack_carton(carton, hex_pool, PackConfig(pattern="hex"))

    square_pool = [[item, item.qty]]
    square_plan = pack_carton(carton, square_pool, PackConfig(pattern="square"))

    assert len(hex_plan.placements) > len(square_plan.placements), (
        f"hex={len(hex_plan.placements)} square={len(square_plan.placements)}"
    )


def test_bottle_never_rotated_and_stacked():
    """瓶装必须直立，且一层一层往上叠。"""
    carton = Carton(code="C1", length=200, width=200, height=1000)
    item = Item(sku="B", kind="bottle", qty=80, diameter=50, height=200, weight=1)
    plan = pack_carton(carton, [[item, item.qty]], PackConfig(pattern="square"))

    z_values = sorted({p.z for p in plan.placements})
    assert z_values == [0.0, 200.0, 400.0, 600.0, 800.0]

    for p in plan.placements:
        assert p.w == pytest.approx(50.0)
        assert p.l == pytest.approx(50.0)
        assert p.h == pytest.approx(200.0)
    assert_valid(plan, carton)


def test_box_packing_grid():
    """400×300×250 装 100×100×100 的方盒：每层 12 个，每箱 24 个。"""
    carton = Carton(code="C1", length=400, width=300, height=250)
    item = Item(sku="K", kind="box", qty=100, length=100, width=100, height=100, weight=10)
    plans, leftover = _pack_all(carton, item)

    assert plans[0].count == 24
    assert sum(1 for _, q in leftover if q) == 0
    for plan in plans:
        assert_valid(plan, carton)


def _pack_all(carton, item):
    from app.packer import pack_into

    return pack_into(carton, [item], PackConfig())


def test_weight_limit_respected():
    carton = Carton(code="C1", length=400, width=300, height=250, max_weight=1000)
    item = Item(sku="K", kind="box", qty=100, length=100, width=100, height=100, weight=100)
    plans, _ = _pack_all(carton, item)

    assert plans, "应当至少装出一箱"
    for plan in plans:
        assert plan.weight <= 1000 + EPS, f"单箱超重：{plan.weight}"
    # 每件 100g、限重 1000g → 每箱最多 10 件
    assert all(plan.count <= 10 for plan in plans)


def test_carton_choice_minimizes_count():
    """大箱应该比小箱用更少的箱子。"""
    small = Carton(code="S", length=250, width=200, height=150)
    large = Carton(code="L", length=600, width=500, height=450)
    item = Item(sku="K", kind="box", qty=60, length=100, width=100, height=100, weight=10)

    plan = optimize_order("SO1", [item], [small, large], PackConfig())
    assert plan.cartons[0].carton.code == "L"
    assert plan.carton_count == 1


def test_oversize_item_marked():
    carton = Carton(code="S", length=100, width=100, height=100)
    item = Item(sku="BIG", kind="box", qty=1, length=500, width=500, height=500)
    plan = optimize_order("SO1", [item], [carton], PackConfig())

    assert plan.carton_count == 0
    assert len(plan.oversize) == 1


def test_bottle_and_box_mixed_layer_gap_fill():
    """方盒层内剩余空间应被更矮的货品回填，且不重叠。"""
    carton = Carton(code="C1", length=400, width=300, height=500)
    big = Item(sku="BIG", kind="box", qty=4, length=200, width=200, height=100, weight=10)
    small = Item(sku="SMALL", kind="box", qty=50, length=50, width=50, height=50, weight=1)

    plan = pack_carton(carton, [[big, 4], [small, 50]], PackConfig())
    assert plan.count > 4  # 回填生效
    assert_valid(plan, carton)


# ---------------------------------------------------------------------------
# Excel 测试
# ---------------------------------------------------------------------------


def test_reads_generated_cartons():
    buffer = build_template(io.BytesIO(), "cartons")
    cartons, warnings = read_cartons(buffer)

    assert len(cartons) == 4
    assert warnings == []
    assert cartons[0].code == "CTN-S"
    assert cartons[0].length == 250


def test_reads_orders_and_merges_same_sku():
    buffer = build_template(io.BytesIO(), "orders")
    orders, warnings = read_orders(buffer)

    assert warnings == []
    assert len(orders) == 4

    first = orders[0]
    assert first.order_no == "SO20241107001"
    by_sku = {item.sku: item for item in first.items}
    # 模板里 BTL-500 分两行（24 + 12），合并后应为 36
    assert by_sku["BTL-500"].qty == 36
    assert by_sku["BTL-500"].kind == "bottle"
    assert by_sku["BTL-500"].diameter == 65
    assert by_sku["BOX-500"].kind == "box"


def test_unit_suffix_parsing():
    from app.excel_io import parse_number

    assert parse_number("400mm") == 400
    assert parse_number("40cm") == 400
    assert parse_number("1.2kg") == 1200
    assert parse_number("1,200") == 1200
    assert parse_number(None, 5) == 5


def test_mixed_carton_types_fallback():
    """单一箱型装不下全部时，应自动用其他箱型补齐。"""
    wide = Carton(code="WIDE", length=500, width=500, height=100)
    tall = Carton(code="TALL", length=100, width=100, height=500)
    flat_item = Item(sku="FLAT", kind="box", qty=1, length=400, width=400, height=50)
    tall_item = Item(sku="TALL", kind="box", qty=1, length=50, width=50, height=400)

    plan = optimize_order("SO-MIX", [flat_item, tall_item], [wide, tall], PackConfig())

    assert plan.carton_count == 2
    assert plan.unplaced_count == 0
    codes = {c.carton.code for c in plan.cartons}
    assert codes == {"WIDE", "TALL"}


def test_rotate_box_disabled_requires_alignment():
    from app.packer import item_fits_carton

    carton = Carton(code="C", length=100, width=300, height=300)
    item = Item(sku="K", kind="box", qty=1, length=250, width=90, height=90)

    assert item_fits_carton(item, carton, PackConfig(rotate_box=True)) is True
    assert item_fits_carton(item, carton, PackConfig(rotate_box=False)) is False
