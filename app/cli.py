"""命令行入口：批量跑预估，输出 JSON 方案，可选导出带推荐列的 Excel。

用法::

    python -m app.cli --boxes data/boxes.xlsx --orders data/orders.xlsx --out plan.json
    python -m app.cli --boxes data/boxes.xlsx --orders data/orders.xlsx \
        --order-type 冷藏 --blue-ice-ratio 0.2 --top-k 3 --export-xlsx 推荐方案.xlsx
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .coldchain import BLUE_ICE, DRY_ICE, PRESETS, default_cold_chain
from .excel_io import export_with_recommendations
from .models import PackConfig
from .service import run_estimation


def _ratio(value: float) -> float:
    """把 20 或 0.2 都统一成 0.2。"""
    return value / 100.0 if value > 1 else value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.cli",
        description="包装预估：读取包装箱列表与订单明细，输出装箱方案 JSON / 推荐 Excel",
    )
    parser.add_argument("--boxes", required=True, help="包装箱列表 xlsx 路径")
    parser.add_argument("--orders", required=True, help="订单明细 xlsx 路径")
    parser.add_argument("--out", default="plan.json", help="输出 JSON 路径（默认 plan.json）")
    parser.add_argument(
        "--pattern",
        choices=["hex", "square"],
        default="hex",
        help="瓶装层内排布：hex=六边形交错（默认），square=方形对齐",
    )
    parser.add_argument("--gap", type=float, default=0.0, help="物品间隙 mm，默认 0（紧密放置）")
    parser.add_argument(
        "--objective",
        choices=["cartons", "volume", "cost"],
        default="cartons",
        help="选箱目标：cartons=箱数最少（默认），volume=总容积最小，cost=总成本最低",
    )
    parser.add_argument("--top-k", type=int, default=3, help="每个订单输出几套候选方案（默认 3）")
    parser.add_argument(
        "--no-weight-filter",
        action="store_true",
        help="关闭「按箱型最大承重过滤」（默认开启）",
    )

    # ---- 冷链 ----
    parser.add_argument(
        "--order-type",
        default="常温",
        choices=list(PRESETS),
        help="订单类型（没有按订单指定时的默认值）",
    )
    parser.add_argument("--temp-min", type=float, default=None, help="温度下限 ℃")
    parser.add_argument("--temp-max", type=float, default=None, help="温度上限 ℃")
    parser.add_argument("--goods-ratio", type=float, default=None, help="货物占比，如 0.7 或 70")
    parser.add_argument("--blue-ice-ratio", type=float, default=None, help="蓝冰占比")
    parser.add_argument("--dry-ice-ratio", type=float, default=None, help="干冰占比")
    parser.add_argument(
        "--basis",
        choices=["weight", "volume"],
        default="weight",
        help="占比基准：weight=按重量（默认），volume=按体积",
    )
    parser.add_argument(
        "--no-cold-chain",
        action="store_true",
        help="完全关闭冷链（不计算冷媒）",
    )

    parser.add_argument(
        "--export-xlsx",
        default="",
        help="导出带「推荐装箱方案1/2/3」列的 Excel 路径",
    )
    parser.add_argument(
        "--no-placements",
        action="store_true",
        help="JSON 不输出逐件坐标（只出汇总，文件更小）",
    )
    return parser


def _build_cold(args: argparse.Namespace):
    cold = default_cold_chain(enabled=False, order_type=args.order_type)
    cold = cold.for_order_type(args.order_type)

    if args.temp_min is not None:
        cold.temp_min = args.temp_min
    if args.temp_max is not None:
        cold.temp_max = args.temp_max
    if args.goods_ratio is not None:
        cold.goods_ratio = _ratio(args.goods_ratio)

    for spec in cold.coolants:
        if spec.kind == BLUE_ICE and args.blue_ice_ratio is not None:
            spec.ratio = _ratio(args.blue_ice_ratio)
        if spec.kind == DRY_ICE and args.dry_ice_ratio is not None:
            spec.ratio = _ratio(args.dry_ice_ratio)

    cold.ratio_basis = args.basis
    if args.no_cold_chain:
        cold.enabled = False
    elif args.blue_ice_ratio or args.dry_ice_ratio:
        cold.enabled = True
    return cold


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    cfg = PackConfig(
        pattern=args.pattern,
        gap=args.gap,
        objective=args.objective,
        top_k=max(1, args.top_k),
        weight_filter=not args.no_weight_filter,
    )
    cold = _build_cold(args)

    try:
        result = run_estimation(
            args.boxes,
            args.orders,
            cfg,
            cold,
            include_placements=not args.no_placements,
        )
    except (ValueError, FileNotFoundError) as exc:
        print(f"[错误] {exc}", file=sys.stderr)
        return 1

    output = Path(args.out)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    summary = result["summary"]
    print("=" * 64)
    print(f"订单数量 : {summary['order_count']}")
    print(f"货品件数 : {summary['item_count']}")
    print(f"使用箱数 : {summary['carton_count']}")
    print(f"总填充率 : {summary['fill_rate'] * 100:.1f}%")
    print(f"总重量   : {summary['total_weight'] / 1000:.2f} kg（含冷媒）")
    if summary.get("coolant_weight"):
        print(f"冷媒重量 : {summary['coolant_weight'] / 1000:.2f} kg")
    if summary["unplaced_count"]:
        print(f"未装箱   : {summary['unplaced_count']} 件")
    if summary.get("overweight_orders"):
        print(f"超重订单 : {summary['overweight_orders']} 个")
    print("=" * 64)

    for order in result["orders"]:
        print(f"\n{order['order_no']}  [{order.get('order_type', '')}]")
        for cand in order.get("candidates", [])[:3]:
            codes = "+".join(sorted({c["carton"]["code"] for c in cand.get("cartons", [])})) or "-"
            flag = "承重OK" if cand.get("weight_ok") else "超重"
            print(
                f"  {cand.get('label', '方案')}: {codes} × {cand['carton_count']} 箱  "
                f"{cand['item_count']:>4} 件  填充 {cand['fill_rate'] * 100:5.1f}%  "
                f"{cand['total_weight'] / 1000:6.2f}kg  {flag}"
            )
        if order.get("coolant_usage"):
            usage = "、".join(
                f"{u.get('kind_label')}{u.get('qty')}块" for u in order["coolant_usage"]
            )
            print(f"  冷媒：{usage}")

    for warning in result["warnings"][:10]:
        print(f"  ! {warning}")

    if args.export_xlsx:
        try:
            buffer = export_with_recommendations(args.orders, result)
            Path(args.export_xlsx).write_bytes(buffer.read())
            print(f"\n推荐 Excel 已写入：{Path(args.export_xlsx).resolve()}")
        except ValueError as exc:
            print(f"[错误] 导出失败：{exc}", file=sys.stderr)
            return 1

    print(f"\n方案 JSON 已写入：{output.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
