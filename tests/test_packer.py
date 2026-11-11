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

    plans = optimize_order("SO1", [item], [small, large], PackConfig())
    assert plans
    assert plans[0].cartons[0].carton.code == "L"
    assert plans[0].carton_count == 1


def test_oversize_item_marked():
    carton = Carton(code="S", length=100, width=100, height=100)
    item = Item(sku="BIG", kind="box", qty=1, length=500, width=500, height=500)
    plans = optimize_order("SO1", [item], [carton], PackConfig())

    assert plans
    assert plans[0].carton_count == 0
    assert len(plans[0].oversize) == 1


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

    plans = optimize_order("SO-MIX", [flat_item, tall_item], [wide, tall], PackConfig())

    assert plans
    assert plans[0].carton_count == 2
    assert plans[0].unplaced_count == 0
    codes = {c.carton.code for c in plans[0].cartons}
    assert codes == {"WIDE", "TALL"}


def test_rotate_box_disabled_requires_alignment():
    from app.packer import item_fits_carton

    carton = Carton(code="C", length=100, width=300, height=300)
    item = Item(sku="K", kind="box", qty=1, length=250, width=90, height=90)

    assert item_fits_carton(item, carton, PackConfig(rotate_box=True)) is True
    assert item_fits_carton(item, carton, PackConfig(rotate_box=False)) is False


# ---------------------------------------------------------------------------
# 冷链：占比、用量、冷媒位置
# ---------------------------------------------------------------------------

from dataclasses import replace  # noqa: E402

from app.coldchain import (  # noqa: E402
    BLUE_ICE,
    DRY_ICE,
    DEFAULT_BLUE_ICE,
    DEFAULT_DRY_ICE,
    ColdChainConfig,
    default_cold_chain,
)


def test_coolant_quantity_from_ratios():
    """货物 70% / 蓝冰 20% / 干冰 10%，货物 10kg → 蓝冰 3 块、干冰 2 块。"""
    cold = ColdChainConfig(
        enabled=True,
        temp_min=2,
        temp_max=8,
        goods_ratio=0.7,
        coolants=[
            replace(DEFAULT_BLUE_ICE, ratio=0.2),
            replace(DEFAULT_DRY_ICE, ratio=0.1),
        ],
    )
    reqs = cold.coolant_requirements(10000, 1_000_000)
    by_kind = {spec.kind: qty for spec, qty in reqs}
    assert by_kind[BLUE_ICE] == 3  # 10 × 0.2/0.7 = 2.857 → 3
    assert by_kind[DRY_ICE] == 2  # 10 × 0.1/0.7 = 1.429 → 2
    assert cold.coolant_weight(10000, 1_000_000) == pytest.approx(5000)


def test_coolant_volume_basis():
    """按体积计算：货物 10L、蓝冰占比 20/80，单块 1.5L → 2 块。"""
    cold = ColdChainConfig(
        enabled=True,
        ratio_basis="volume",
        goods_ratio=0.8,
        coolants=[replace(DEFAULT_BLUE_ICE, ratio=0.2)],
    )
    reqs = cold.coolant_requirements(10000, 10_000_000)
    assert reqs and reqs[0][1] == 2


def test_coolant_placed_above_goods():
    """冷媒必须码在货物上方（z 不低于货物最高点）。"""
    carton = Carton(code="C1", length=400, width=300, height=500, max_weight=50000)
    item = Item(sku="K", kind="box", qty=6, length=100, width=100, height=100, weight=1000)
    cold = ColdChainConfig(
        enabled=True,
        goods_ratio=0.8,
        coolants=[replace(DEFAULT_BLUE_ICE, ratio=0.2, height=40)],
    )
    plan = pack_carton(carton, [[item, 6]], PackConfig(), cold=cold)

    goods_top = max(p.top for p in plan.placements if not p.is_coolant)
    coolant = [p for p in plan.placements if p.is_coolant]
    assert coolant, "应当放入冷媒"
    assert min(p.z for p in coolant) >= goods_top - 1e-6
    assert plan.coolant_ok is True
    assert plan.coolant_weight > 0


def test_cold_chain_disabled_has_no_coolant():
    carton = Carton(code="C1", length=400, width=300, height=400, max_weight=50000)
    item = Item(sku="K", kind="box", qty=4, length=100, width=100, height=100, weight=1000)
    cold = ColdChainConfig(enabled=False, coolants=[replace(DEFAULT_BLUE_ICE, ratio=0.3)])
    plan = pack_carton(carton, [[item, 4]], PackConfig(), cold=cold)
    assert plan.coolant_weight == 0
    assert not [p for p in plan.placements if p.is_coolant]


# ---------------------------------------------------------------------------
# Top-K / 承重过滤 / Excel 导出
# ---------------------------------------------------------------------------


def test_top_k_candidates():
    small = Carton(code="S", length=250, width=200, height=150)
    large = Carton(code="L", length=600, width=500, height=450)
    item = Item(sku="K", kind="box", qty=60, length=100, width=100, height=100, weight=10)

    plans = optimize_order("SO1", [item], [small, large], PackConfig(top_k=3))
    assert 1 <= len(plans) <= 3
    assert [p.rank for p in plans] == list(range(1, len(plans) + 1))
    assert all(p.label == f"方案{p.rank}" for p in plans)

    single = optimize_order("SO1", [item], [small, large], PackConfig(top_k=1))
    assert len(single) == 1


def test_weight_filter_prefers_fitting_carton():
    tight = Carton(code="TIGHT", length=300, width=300, height=300, max_weight=5000)
    roomy = Carton(code="ROOMY", length=300, width=300, height=300, max_weight=50000)
    item = Item(sku="K", kind="box", qty=5, length=100, width=100, height=100, weight=900)
    cold = ColdChainConfig(
        enabled=True,
        temp_min=2,
        temp_max=8,
        goods_ratio=0.75,
        coolants=[replace(DEFAULT_BLUE_ICE, ratio=0.25)],
    )

    plans = optimize_order("SO", [item], [tight, roomy], PackConfig(weight_filter=True), cold)
    assert plans[0].carton_count > 0
    assert plans[0].weight_ok is True
    assert plans[0].coolant_ok is True
    assert plans[0].cartons[0].carton.code == "ROOMY"

    # 只有小承重箱：算法会主动多分几箱，保证每箱（含冷媒）都不超重
    only_tight = optimize_order("SO", [item], [tight], PackConfig(weight_filter=True), cold)
    assert only_tight[0].carton_count >= 2
    assert only_tight[0].weight_ok is True
    assert all(plan.total_weight <= 5000 + 1e-6 for plan in only_tight[0].cartons)


def test_weight_filter_flags_actual_overweight():
    """关闭承重约束时可以造出超重方案，weight_ok 应为 False。"""
    tight = Carton(code="TIGHT", length=300, width=300, height=300, max_weight=5000)
    item = Item(sku="K", kind="box", qty=5, length=100, width=100, height=100, weight=900)
    cold = ColdChainConfig(
        enabled=True,
        goods_ratio=0.75,
        coolants=[replace(DEFAULT_BLUE_ICE, ratio=0.25)],
    )
    plans = optimize_order(
        "SO",
        [item],
        [tight],
        PackConfig(weight_filter=True, respect_weight=False),
        cold,
    )
    assert plans[0].carton_count > 0
    assert plans[0].weight_ok is False


def test_export_adds_recommendation_columns():
    from openpyxl import load_workbook

    from app.excel_io import build_template, export_with_recommendations
    from app.service import run_estimation

    boxes_buf = build_template(io.BytesIO(), "cartons")
    orders_buf = build_template(io.BytesIO(), "orders")
    orders_bytes = orders_buf.getvalue()

    result = run_estimation(
        io.BytesIO(boxes_buf.getvalue()),
        io.BytesIO(orders_bytes),
        PackConfig(),
        default_cold_chain(enabled=True),
    )
    out = export_with_recommendations(io.BytesIO(orders_bytes), result)

    wb = load_workbook(out)
    assert "装箱方案明细" in wb.sheetnames

    ws = wb["订单明细"]
    headers = [c.value for c in ws[1]]
    for title in ("推荐装箱方案1", "推荐装箱方案2", "推荐装箱方案3"):
        assert title in headers

    col = headers.index("推荐装箱方案1") + 1
    text = ws.cell(row=2, column=col).value
    assert text and "箱" in text


def test_order_type_from_excel():
    from app.excel_io import build_template, read_orders

    orders, warnings = read_orders(build_template(io.BytesIO(), "orders"))
    assert warnings == []
    types = {o.order_no: o.order_type for o in orders}
    assert types["SO20241107001"] == "冷藏"
    assert types["SO20241107002"] == "冷冻"
    assert types["SO20241107004"] == "常温"


def test_order_type_preset_ratios():
    cold = default_cold_chain(enabled=True).for_order_type("冷冻")
    assert cold.enabled is True
    assert cold.temp_min == -20 and cold.temp_max == -10
    blue = cold.find(BLUE_ICE)
    dry = cold.find(DRY_ICE)
    assert blue is not None and dry is not None
    assert blue.ratio == 0 and dry.ratio == 0.25
