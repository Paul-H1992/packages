"""Excel 读写：包装箱列表、订单明细、订单合并。

支持的表格结构（列名可用中文/英文别名，程序自动识别）：

包装箱列表（一个 sheet）
    箱型编码 | 箱型名称 | 内长 | 内宽 | 内高 | 最大承重 | 单价
    CTN-M    | 中号箱   | 400  | 300  | 250  | 12000    | 3.5

订单明细（一个 sheet）
    订单号 | 货品编码 | 名称 | 类型 | 直径 | 长 | 宽 | 高 | 单重 | 数量
    SO001  | BOTTLE-500 | 500ml圆瓶 | 瓶装 | 65 | | | 220 | 550 | 12

可选：货品档案（另一个 sheet，同一文件或单独文件）
    货品编码 | 名称 | 类型 | 直径 | 长 | 宽 | 高 | 单重
"""

from __future__ import annotations

import io
import re
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from .models import Carton, Item


# ---------------------------------------------------------------------------
# 列名别名
# ---------------------------------------------------------------------------

CARTON_ALIASES: dict[str, set[str]] = {
    "code": {"箱型编码", "箱型编号", "包装箱编码", "包装箱编号", "箱子编码", "箱号", "箱型", "型号", "编码", "sku", "code", "carton", "箱型代码"},
    "name": {"箱型名称", "包装箱名称", "箱子名称", "名称", "描述", "说明", "name", "desc"},
    "length": {"内长", "长度", "长", "length", "l", "长(mm)", "内长(mm)"},
    "width": {"内宽", "宽度", "宽", "width", "w", "宽(mm)", "内宽(mm)"},
    "height": {"内高", "高度", "高", "height", "h", "高(mm)", "内高(mm)"},
    "max_weight": {"最大承重", "承重", "限重", "载重", "最大重量", "max_weight", "maxweight", "weight_limit", "承重(g)"},
    "price": {"单价", "价格", "成本", "箱价", "price", "cost"},
}

ORDER_ALIASES: dict[str, set[str]] = {
    "order_no": {"订单号", "订单编号", "单号", "订单", "order_no", "orderno", "order", "order_id", "so", "销售订单号"},
    "sku": {"货品编码", "商品编码", "货号", "物料编码", "物料号", "产品编码", "sku", "code", "item_code", "货品编号"},
    "name": {"货品名称", "商品名称", "名称", "品名", "product", "name", "item_name"},
    "kind": {"类型", "品类", "包装类型", "包装形式", "形状", "type", "kind", "shape"},
    "diameter": {"直径", "瓶径", "瓶身直径", "外径", "diameter", "d", "φ", "直径(mm)"},
    "length": {"长", "长度", "货品长", "货品长度", "length", "l", "长(mm)", "外长"},
    "width": {"宽", "宽度", "货品宽", "货品宽度", "width", "w", "宽(mm)", "外宽"},
    "height": {"高", "高度", "瓶高", "货品高", "货品高度", "height", "h", "高(mm)", "外高"},
    "weight": {"单重", "重量", "单件重量", "净重", "毛重", "weight", "unit_weight", "单重(g)"},
    "qty": {"数量", "件数", "订购数量", "订单数量", "qty", "quantity", "num", "count"},
}

ITEM_ALIASES = {k: v for k, v in ORDER_ALIASES.items() if k not in {"order_no", "qty"}}


# ---------------------------------------------------------------------------
# 通用工具
# ---------------------------------------------------------------------------


def _norm(value: Any) -> str:
    text = str(value or "").strip().lower()
    text = text.replace("\u3000", "").replace(" ", "")
    text = text.replace("（", "(").replace("）", ")").replace("／", "/")
    text = text.replace("*", "x")
    return text


def parse_number(value: Any, default: Optional[float] = None) -> Optional[float]:
    """把 ``400`` / ``"400mm"`` / ``"1.2kg"`` / ``"1,200"`` 解析成数字。

    长度类单位统一为 mm，重量类单位统一为 g。
    """
    if value is None:
        return default
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return float(value)

    text = str(value).strip().replace(",", "").replace("，", "").replace(" ", "")
    if not text:
        return default

    match = re.search(r"[-+]?\d+(?:\.\d+)?", text)
    if not match:
        return default

    number = float(match.group())
    tail = text[match.end():].lower()

    if "kg" in tail or "千克" in tail or "公斤" in tail:
        return number * 1000.0
    if "mg" in tail or "毫克" in tail:
        return number / 1000.0
    if "cm" in tail or "厘米" in tail:
        return number * 10.0
    if "mm" in tail or "毫米" in tail:
        return number
    if "m" in tail or "米" in tail:
        return number * 1000.0
    return number


def parse_kind(value: Any) -> Optional[str]:
    text = _norm(value)
    if not text:
        return None
    if any(k in text for k in ("瓶", "bottle", "cylind", "柱", "圆")):
        return "bottle"
    if any(k in text for k in ("盒", "box", "方", "cub", "carton")):
        return "box"
    return None


def _map_columns(row: Iterable[Any], aliases: dict[str, set[str]]) -> dict[str, Optional[int]]:
    """把表头行映射成 {字段: 列号}。先精确匹配，再包含匹配。"""
    normalized = [_norm(cell) for cell in row]
    mapping: dict[str, Optional[int]] = {}

    for key, names in aliases.items():
        found: Optional[int] = None
        for idx, name in enumerate(normalized):
            if name and name in names:
                found = idx
                break
        if found is None:
            for idx, name in enumerate(normalized):
                if not name:
                    continue
                if any(alias and alias in name for alias in names):
                    found = idx
                    break
        mapping[key] = found
    return mapping


def _load_table(ws, aliases: dict[str, set[str]]) -> tuple[list[dict[str, Any]], dict[str, Optional[int]]]:
    """读取一个 sheet，返回 (记录列表, 列映射)。自动定位表头行。"""
    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        return [], {}

    best_row = -1
    best_score = -1
    best_map: dict[str, Optional[int]] = {}
    for i, row in enumerate(rows[:20]):
        mapping = _map_columns(row, aliases)
        score = sum(1 for v in mapping.values() if v is not None)
        if score > best_score:
            best_row, best_score, best_map = i, score, mapping

    if best_row < 0 or best_score < 2:
        return [], {}

    records: list[dict[str, Any]] = []
    for row in rows[best_row + 1:]:
        if row is None:
            continue
        if all(cell is None or str(cell).strip() == "" for cell in row):
            continue
        record: dict[str, Any] = {}
        for key, idx in best_map.items():
            if idx is not None and idx < len(row):
                record[key] = row[idx]
        records.append(record)
    return records, best_map


def _pick_sheet(wb, keywords: list[str], exclude: list[str] | None = None):
    """按 sheet 名称关键字挑选工作表，找不到则返回第一个。"""
    exclude = exclude or []
    names = wb.sheetnames
    for keyword in keywords:
        for name in names:
            if keyword in name and not any(x in name for x in exclude):
                return wb[name]
    for name in names:
        if not any(x in name for x in exclude):
            return wb[name]
    return wb[names[0]]


# ---------------------------------------------------------------------------
# 包装箱
# ---------------------------------------------------------------------------


def read_cartons(source: str | Path | io.BytesIO) -> tuple[list[Carton], list[str]]:
    """读取包装箱列表，返回 (包装箱列表, 警告信息)。"""
    wb = load_workbook(source, data_only=True, read_only=False)
    ws = _pick_sheet(wb, ["包装箱", "箱型", "纸箱", "箱子", "carton", "box"])

    records, mapping = _load_table(ws, CARTON_ALIASES)
    warnings: list[str] = []

    required = ["code", "length", "width", "height"]
    missing = [key for key in required if mapping.get(key) is None]
    if missing:
        raise ValueError(
            "包装箱列表缺少必要列："
            + "、".join(
                {"code": "箱型编码", "length": "内长", "width": "内宽", "height": "内高"}[k]
                for k in missing
            )
            + f"（当前工作表：{ws.title}）"
        )

    cartons: list[Carton] = []
    seen: set[str] = set()
    for record in records:
        code = str(record.get("code") or "").strip()
        if not code:
            continue
        if code in seen:
            warnings.append(f"包装箱编码重复，已忽略：{code}")
            continue
        seen.add(code)

        length = parse_number(record.get("length"))
        width = parse_number(record.get("width"))
        height = parse_number(record.get("height"))
        if not length or not width or not height or length <= 0 or width <= 0 or height <= 0:
            warnings.append(f"包装箱 {code} 的内径不完整，已忽略")
            continue

        max_weight = parse_number(record.get("max_weight"))
        price = parse_number(record.get("price"))

        carton = Carton(
            code=code,
            name=str(record.get("name") or "").strip(),
            length=float(length),
            width=float(width),
            height=float(height),
            max_weight=float(max_weight) if max_weight and max_weight > 0 else float("inf"),
            price=float(price) if price else None,
        )
        try:
            carton.validate()
        except ValueError as exc:
            warnings.append(str(exc))
            continue
        cartons.append(carton)

    if not cartons:
        raise ValueError("包装箱列表中没有任何有效数据行")

    return cartons, warnings


# ---------------------------------------------------------------------------
# 订单
# ---------------------------------------------------------------------------


@dataclass
class MergedOrder:
    order_no: str
    items: list[Item] = field(default_factory=list)

    @property
    def total_qty(self) -> int:
        return sum(item.qty for item in self.items)


def _build_item(record: dict[str, Any], sku: str, qty: int, order_no: str) -> Item | None:
    name = str(record.get("name") or "").strip()
    kind = parse_kind(record.get("kind"))
    diameter = parse_number(record.get("diameter"))
    length = parse_number(record.get("length"))
    width = parse_number(record.get("width"))
    height = parse_number(record.get("height"))
    weight = parse_number(record.get("weight")) or 0.0

    if kind is None:
        if diameter and diameter > 0:
            kind = "bottle"
        elif length and width:
            kind = "box"
        else:
            return None

    if kind == "bottle":
        if not diameter or diameter <= 0:
            return None
        if not height or height <= 0:
            return None
        return Item(
            sku=sku,
            name=name,
            kind="bottle",
            qty=qty,
            height=float(height),
            weight=float(weight),
            diameter=float(diameter),
            order_no=order_no,
        )

    if not length or not width or not height or min(length, width, height) <= 0:
        return None
    return Item(
        sku=sku,
        name=name,
        kind="box",
        qty=qty,
        height=float(height),
        weight=float(weight),
        length=float(length),
        width=float(width),
        order_no=order_no,
    )


def read_orders(source: str | Path | io.BytesIO) -> tuple[list[MergedOrder], list[str]]:
    """读取订单明细并**按订单号合并**同 SKU 的多行记录。

    返回 (合并后的订单列表, 警告信息)。
    """
    wb = load_workbook(source, data_only=True, read_only=False)
    warnings: list[str] = []

    detail_ws = None
    detail_records: list[dict[str, Any]] = []
    best_score = -1

    for name in wb.sheetnames:
        ws = wb[name]
        records, mapping = _load_table(ws, ORDER_ALIASES)
        if not records:
            continue
        score = sum(1 for key in ("order_no", "sku", "qty") if mapping.get(key) is not None)
        if score > best_score:
            best_score, detail_ws, detail_records = score, ws, records

    if detail_ws is None or best_score < 3:
        raise ValueError("订单文件缺少必要列：订单号 / 货品编码 / 数量")

    # 货品档案（可选）
    master: dict[str, dict[str, Any]] = {}
    for name in wb.sheetnames:
        if detail_ws is not None and name == detail_ws.title:
            continue
        ws = wb[name]
        records, mapping = _load_table(ws, ITEM_ALIASES)
        if not records:
            continue
        if mapping.get("sku") is None:
            continue
        if mapping.get("order_no") is not None:
            continue
        if mapping.get("kind") is None and mapping.get("diameter") is None and mapping.get("length") is None:
            continue
        for record in records:
            sku = str(record.get("sku") or "").strip()
            if sku and sku not in master:
                master[sku] = record

    # 合并同订单同 SKU
    merged: "OrderedDict[str, OrderedDict[str, list]]" = OrderedDict()
    for record in detail_records:
        order_no = str(record.get("order_no") or "").strip()
        sku = str(record.get("sku") or "").strip()
        qty = parse_number(record.get("qty"))
        if not order_no or not sku:
            continue
        if not qty or qty <= 0:
            warnings.append(f"订单 {order_no} 的货品 {sku} 数量无效，已忽略")
            continue

        bucket = merged.setdefault(order_no, OrderedDict())
        if sku not in bucket:
            bucket[sku] = {"qty": 0, "record": {}}
        bucket[sku]["qty"] += int(round(qty))
        # 行内字段补全（档案缺失时使用）
        for key, value in record.items():
            if key in ("order_no", "qty"):
                continue
            if value is not None and str(value).strip() != "":
                bucket[sku]["record"].setdefault(key, value)

    orders: list[MergedOrder] = []
    for order_no, bucket in merged.items():
        items: list[Item] = []
        for sku, payload in bucket.items():
            merged_record: dict[str, Any] = {}
            merged_record.update(master.get(sku, {}))
            for key, value in payload["record"].items():
                merged_record.setdefault(key, value)
            item = _build_item(merged_record, sku, payload["qty"], order_no)
            if item is None:
                warnings.append(
                    f"订单 {order_no} 的货品 {sku} 缺少尺寸/类型，已跳过（请在货品档案中补全）"
                )
                continue
            try:
                item.validate()
            except ValueError as exc:
                warnings.append(str(exc))
                continue
            items.append(item)
        if items:
            orders.append(MergedOrder(order_no=order_no, items=items))
        else:
            warnings.append(f"订单 {order_no} 没有可用的货品，已跳过")

    if not orders:
        raise ValueError("订单文件中没有可用的订单数据")

    return orders, warnings


# ---------------------------------------------------------------------------
# 模板生成
# ---------------------------------------------------------------------------

_HEADER_FILL = PatternFill("solid", fgColor="E8F0FE")
_HEADER_FONT = Font(bold=True)


def _write_sheet(ws, headers: list[str], rows: list[list[Any]]) -> None:
    ws.append(headers)
    for cell in ws[1]:
        cell.fill = _HEADER_FILL
        cell.font = _HEADER_FONT
        cell.alignment = Alignment(horizontal="center", vertical="center")
    for row in rows:
        ws.append(row)
    for idx, _header in enumerate(headers, start=1):
        ws.column_dimensions[get_column_letter(idx)].width = max(12, len(str(headers[idx - 1])) * 2 + 6)
    ws.freeze_panes = "A2"


SAMPLE_CARTONS: list[list[Any]] = [
    ["CTN-S", "小号箱", 250, 200, 150, 6000, 2.2],
    ["CTN-M", "中号箱", 400, 300, 250, 12000, 3.5],
    ["CTN-L", "大号箱", 500, 400, 350, 20000, 5.8],
    ["CTN-XL", "超大箱", 600, 500, 450, 30000, 8.6],
]

SAMPLE_ITEMS: list[list[Any]] = [
    ["BTL-500", "500ml 圆瓶", "瓶装", 65, None, None, 220, 550],
    ["BTL-330", "330ml 圆瓶", "瓶装", 58, None, None, 180, 380],
    ["BTL-1000", "1L 圆瓶", "瓶装", 80, None, None, 280, 1050],
    ["BOX-200", "200g 方盒", "盒装", None, 120, 120, 60, 220],
    ["BOX-500", "500g 方盒", "盒装", None, 160, 160, 80, 520],
    ["BOX-1000", "1kg 方盒", "盒装", None, 200, 200, 100, 1080],
]

SAMPLE_ORDER_ROWS: list[list[Any]] = [
    ["SO20241107001", "BTL-500", None, None, None, None, 24],
    ["SO20241107001", "BTL-500", None, None, None, None, 12],  # 同一 SKU 两行 → 合并为 36
    ["SO20241107001", "BOX-500", None, None, None, None, 10],
    ["SO20241107002", "BTL-1000", None, None, None, None, 18],
    ["SO20241107002", "BOX-200", None, None, None, None, 24],
    ["SO20241107002", "BOX-1000", None, None, None, None, 6],
    ["SO20241107003", "BTL-330", None, None, None, None, 48],
    ["SO20241107003", "BOX-200", None, None, None, None, 30],
    ["SO20241107004", "BTL-500", None, None, None, None, 8],
    ["SO20241107004", "BOX-1000", None, None, None, None, 4],
]


def build_template(bytes_io: io.BytesIO, kind: str) -> io.BytesIO:
    """生成下载模板：``kind`` 为 ``cartons`` 或 ``orders``。"""
    wb = Workbook()

    if kind == "cartons":
        ws = wb.active
        ws.title = "包装箱列表"
        _write_sheet(
            ws,
            ["箱型编码", "箱型名称", "内长(mm)", "内宽(mm)", "内高(mm)", "最大承重(g)", "单价(元)"],
            SAMPLE_CARTONS,
        )
    elif kind == "orders":
        ws = wb.active
        ws.title = "订单明细"
        _write_sheet(ws, ["订单号", "货品编码", "数量"], [[r[0], r[1], r[6]] for r in SAMPLE_ORDER_ROWS])

        master = wb.create_sheet("货品档案")
        _write_sheet(
            master,
            ["货品编码", "名称", "类型", "直径(mm)", "长(mm)", "宽(mm)", "高(mm)", "单重(g)"],
            SAMPLE_ITEMS,
        )
    else:
        raise ValueError(f"未知模板类型：{kind}")

    buffer = io.BytesIO()
    wb.save(buffer)
    buffer.seek(0)
    return buffer
