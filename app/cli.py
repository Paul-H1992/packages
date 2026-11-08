"""命令行入口：批量跑预估并输出 JSON 方案。

用法::

    python -m app.cli --boxes data/boxes.xlsx --orders data/orders.xlsx --out plan.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .models import PackConfig
from .service import run_estimation


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.cli",
        description="包装预估：读取包装箱列表与订单明细，输出装箱方案 JSON",
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
    parser.add_argument(
        "--no-placements",
        action="store_true",
        help="不输出逐件坐标（只出汇总，JSON 更小）",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    cfg = PackConfig(pattern=args.pattern, gap=args.gap, objective=args.objective)

    try:
        result = run_estimation(
            args.boxes,
            args.orders,
            cfg,
            include_placements=not args.no_placements,
        )
    except (ValueError, FileNotFoundError) as exc:
        print(f"[错误] {exc}", file=sys.stderr)
        return 1

    output = Path(args.out)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    summary = result["summary"]
    print("=" * 56)
    print(f"订单数量 : {summary['order_count']}")
    print(f"货品件数 : {summary['item_count']}")
    print(f"使用箱数 : {summary['carton_count']}")
    print(f"总填充率 : {summary['fill_rate'] * 100:.1f}%")
    print(f"总重量   : {summary['total_weight'] / 1000:.2f} kg")
    if summary["unplaced_count"]:
        print(f"未装箱   : {summary['unplaced_count']} 件")
    print("=" * 56)

    for order in result["orders"]:
        codes = "+".join(sorted({c["carton"]["code"] for c in order["cartons"]})) or "-"
        print(
            f"  {order['order_no']:<22} {order['carton_count']:>3} 箱  "
            f"{order['item_count']:>4} 件  填充 {order['fill_rate'] * 100:5.1f}%  [{codes}]"
        )
    for warning in result["warnings"]:
        print(f"  ! {warning}")

    print(f"\n方案已写入：{output.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
