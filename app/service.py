"""业务编排：读取 Excel → 合并订单 → 冷链/装箱预估 → 输出结果字典。"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any, Optional

from .coldchain import ColdChainConfig, default_cold_chain
from .excel_io import read_cartons, read_orders
from .models import mm3_to_liter, PackConfig
from .packer import optimize_order


def _empty_order(order: Any, cold: ColdChainConfig) -> dict[str, Any]:
    """没有任何箱型可用时的占位结果。"""
    return {
        "order_no": order.order_no,
        "rank": 0,
        "label": "",
        "order_type": cold.order_type,
        "temperature": {"min": cold.temp_min, "max": cold.temp_max},
        "items": [item.as_dict() for item in order.items],
        "carton_count": 0,
        "item_count": 0,
        "total_weight": 0.0,
        "total_with_tare": 0.0,
        "coolant_weight": 0.0,
        "coolant_usage": [],
        "weight_ok": False,
        "coolant_ok": False,
        "fill_rate": 0.0,
        "used_volume_l": 0.0,
        "carton_volume_l": 0.0,
        "strategy": "无可用箱型",
        "reason": "货品尺寸超过所有包装箱",
        "cartons": [],
        "candidates": [],
        "unplaced": [{"sku": i.sku, "name": i.name, "qty": i.qty} for i in order.items],
        "oversize": [],
    }


def run_estimation(
    cartons_source: str | Path | io.BytesIO,
    orders_source: str | Path | io.BytesIO,
    cfg: Optional[PackConfig] = None,
    cold: Optional[ColdChainConfig] = None,
    include_placements: bool = True,
) -> dict[str, Any]:
    """执行完整预估流程，返回前端/导出共用的结果字典。"""
    cfg = cfg or PackConfig()
    cold = cold or default_cold_chain()

    cartons, carton_warnings = read_cartons(cartons_source)
    orders, order_warnings = read_orders(orders_source)

    warnings: list[str] = []
    warnings.extend(carton_warnings)
    warnings.extend(order_warnings)
    warnings.extend(cold.warnings())

    results: list[dict[str, Any]] = []
    for order in orders:
        # 订单自带类型时，按类型预设覆盖温度与占比
        order_cold = cold.for_order_type(order.order_type) if order.order_type else cold
        plans = optimize_order(order.order_no, order.items, cartons, cfg, order_cold)

        if not plans:
            results.append(_empty_order(order, order_cold))
            warnings.append(f"订单 {order.order_no} 没有可用箱型，请检查箱型尺寸")
            continue

        candidates = [plan.as_dict(include_placements=include_placements) for plan in plans]
        entry = dict(candidates[0])
        entry["candidates"] = candidates
        entry["order_type"] = order_cold.order_type
        results.append(entry)

        if not entry.get("weight_ok", True):
            warnings.append(f"订单 {order.order_no} 的最佳方案有箱子超出最大承重，已按承重过滤")
        if not entry.get("coolant_ok", True):
            warnings.append(f"订单 {order.order_no} 的冷媒未能全部放入箱子，建议换更大箱型")

    total_cartons = sum(r["carton_count"] for r in results)
    total_used = sum(r["used_volume_l"] for r in results)
    total_capacity = sum(r["carton_volume_l"] for r in results)
    total_weight = sum(r["total_weight"] for r in results)
    total_with_tare = sum(r.get("total_with_tare", 0.0) for r in results)
    total_coolant = sum(r.get("coolant_weight", 0.0) for r in results)
    total_items = sum(r["item_count"] for r in results)
    unplaced = sum(sum(u["qty"] for u in r["unplaced"]) for r in results)
    oversize = sum(len(r["oversize"]) for r in results)
    overweight = sum(1 for r in results if not r.get("weight_ok", True))

    if unplaced:
        warnings.append(f"共有 {unplaced} 件货品未能装入现有箱型，请检查箱型尺寸或增加箱型")
    if oversize:
        warnings.append(f"共有 {oversize} 种货品的单件尺寸超过所有包装箱，已标记为无法装箱")

    return {
        "config": {**cfg.as_dict(), "cold_chain": cold.as_dict()},
        "cartons": [c.as_dict() for c in cartons],
        "orders": results,
        "summary": {
            "order_count": len(results),
            "item_count": total_items,
            "carton_count": total_cartons,
            "total_weight": round(total_weight, 2),
            "total_with_tare": round(total_with_tare, 2),
            "coolant_weight": round(total_coolant, 2),
            "used_volume_l": round(total_used, 4),
            "carton_volume_l": round(total_capacity, 4),
            "fill_rate": round(total_used / total_capacity, 4) if total_capacity else 0.0,
            "unplaced_count": unplaced,
            "oversize_count": oversize,
            "overweight_orders": overweight,
            "candidate_count": cfg.top_k,
        },
        "warnings": warnings,
    }


def summary_text(result: dict[str, Any]) -> str:
    """一行文字概括，用于 CLI 输出。"""
    s = result.get("summary", {})
    return (
        f"{s.get('order_count', 0)} 单 / {s.get('item_count', 0)} 件 / "
        f"{s.get('carton_count', 0)} 箱 / 填充 {s.get('fill_rate', 0) * 100:.1f}% / "
        f"总重 {s.get('total_weight', 0) / 1000:.2f}kg"
    )
