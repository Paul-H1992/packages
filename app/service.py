"""业务编排：读取 Excel → 合并订单 → 装箱预估 → 输出结果字典。"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any, Optional

from .excel_io import read_cartons, read_orders
from .models import PackConfig
from .packer import optimize_order


def run_estimation(
    cartons_source: str | Path | io.BytesIO,
    orders_source: str | Path | io.BytesIO,
    cfg: Optional[PackConfig] = None,
    include_placements: bool = True,
) -> dict[str, Any]:
    """执行完整预估流程。"""
    cfg = cfg or PackConfig()

    cartons, carton_warnings = read_cartons(cartons_source)
    orders, order_warnings = read_orders(orders_source)

    results: list[dict[str, Any]] = []
    for order in orders:
        plan = optimize_order(order.order_no, order.items, cartons, cfg)
        results.append(plan.as_dict(include_placements=include_placements))

    total_cartons = sum(r["carton_count"] for r in results)
    total_used = sum(r["used_volume_l"] for r in results)
    total_capacity = sum(r["carton_volume_l"] for r in results)
    total_weight = sum(r["total_weight"] for r in results)
    total_items = sum(r["item_count"] for r in results)
    unplaced = sum(sum(u["qty"] for u in r["unplaced"]) for r in results)
    oversize = sum(len(r["oversize"]) for r in results)

    warnings = list(carton_warnings) + list(order_warnings)
    if unplaced:
        warnings.append(f"共有 {unplaced} 件货品未能装入现有箱型，请检查箱型尺寸或增加箱型")
    if oversize:
        warnings.append(f"共有 {oversize} 种货品的单件尺寸超过所有包装箱，已标记为无法装箱")

    return {
        "config": cfg.as_dict(),
        "cartons": [c.as_dict() for c in cartons],
        "orders": results,
        "summary": {
            "order_count": len(results),
            "item_count": total_items,
            "carton_count": total_cartons,
            "total_weight": round(total_weight, 2),
            "used_volume_l": round(total_used, 4),
            "carton_volume_l": round(total_capacity, 4),
            "fill_rate": round(total_used / total_capacity, 4) if total_capacity else 0.0,
            "unplaced_count": unplaced,
            "oversize_count": oversize,
        },
        "warnings": warnings,
    }
