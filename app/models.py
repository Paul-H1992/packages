"""领域模型：包装箱、货品、装箱结果。

单位约定
--------
* 长度 / 直径：毫米（mm）
* 重量：克（g）
* 体积：内部按 mm³ 计算，对外展示统一换算为升（L）

坐标约定（右手系）
------------------
* 包装箱内腔：x ∈ [0, length]，y ∈ [0, width]，z ∈ [0, height]
* 物品位置取「外接长方体」的左下后角（x, y, z）
* 瓶装为直立圆柱：轴线平行 z 轴，直径 diameter，高度 height，**不允许旋转**
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Literal, Optional

Kind = Literal["bottle", "box"]
Pattern = Literal["hex", "square"]

EPS = 1e-6

#: 升 = 1e6 mm³
MM3_PER_LITER = 1_000_000.0


def mm3_to_liter(v: float) -> float:
    return round(v / MM3_PER_LITER, 4)


def to_bottle_name(kind: str) -> str:
    return "瓶装" if kind == "bottle" else "盒装"


@dataclass
class Carton:
    """包装箱（内径尺寸）。"""

    code: str
    name: str = ""
    length: float = 0.0  # 内长 mm，对应 x
    width: float = 0.0  # 内宽 mm，对应 y
    height: float = 0.0  # 内高 mm，对应 z
    max_weight: float = float("inf")  # 最大承重 g（含箱体自重）
    price: Optional[float] = None  # 单箱成本（可选）
    tare_weight: float = 0.0  # 空箱自重 g

    @property
    def volume(self) -> float:
        return self.length * self.width * self.height

    @property
    def floor_area(self) -> float:
        return self.length * self.width

    @property
    def display_name(self) -> str:
        return self.name or self.code

    def validate(self) -> None:
        for field_name, label in (
            ("length", "内长"),
            ("width", "内宽"),
            ("height", "内高"),
        ):
            value = getattr(self, field_name)
            if not isinstance(value, (int, float)) or value <= 0:
                raise ValueError(f"包装箱 {self.code} 的{label}必须大于 0，当前为 {value!r}")
        if not self.code:
            raise ValueError("包装箱编码不能为空")

    def as_dict(self) -> dict:
        return {
            "code": self.code,
            "name": self.name,
            "length": round(self.length, 2),
            "width": round(self.width, 2),
            "height": round(self.height, 2),
            "max_weight": None if math.isinf(self.max_weight) else self.max_weight,
            "volume_l": mm3_to_liter(self.volume),
            "price": self.price,
            "tare_weight": round(self.tare_weight, 2),
        }


@dataclass
class Item:
    """货品（单件尺寸）。"""

    sku: str
    name: str = ""
    kind: Kind = "box"
    qty: int = 1
    height: float = 0.0  # 单件高度 mm
    weight: float = 0.0  # 单件重量 g
    diameter: float = 0.0  # 瓶装直径 mm
    length: float = 0.0  # 盒装长 mm
    width: float = 0.0  # 盒装宽 mm
    order_no: str = ""
    is_coolant: bool = False  # 是否为冷媒（蓝冰/干冰）

    @property
    def footprint(self) -> tuple[float, float]:
        """底面外接矩形（宽 w, 深 l）。"""
        if self.kind == "bottle":
            return (self.diameter, self.diameter)
        return (self.length, self.width)

    @property
    def volume(self) -> float:
        """单件体积（mm³）。瓶装按圆柱体积计算，盒装按长方体计算。"""
        if self.kind == "bottle":
            return math.pi * (self.diameter / 2.0) ** 2 * self.height
        return self.length * self.width * self.height

    @property
    def bounding_volume(self) -> float:
        """外接长方体体积（mm³），用于装箱利用率展示。"""
        if self.kind == "bottle":
            return self.diameter * self.diameter * self.height
        return self.length * self.width * self.height

    def validate(self) -> None:
        if not self.sku:
            raise ValueError("货品编码不能为空")
        if self.qty < 0:
            raise ValueError(f"货品 {self.sku} 的数量不能为负")
        if self.height <= 0:
            raise ValueError(f"货品 {self.sku} 的高度必须大于 0")
        if self.kind == "bottle" and self.diameter <= 0:
            raise ValueError(f"瓶装货品 {self.sku} 必须提供直径")
        if self.kind == "box" and (self.length <= 0 or self.width <= 0):
            raise ValueError(f"盒装货品 {self.sku} 必须提供长和宽")

    def clone(self, qty: int) -> "Item":
        return Item(
            sku=self.sku,
            name=self.name,
            kind=self.kind,
            qty=qty,
            height=self.height,
            weight=self.weight,
            diameter=self.diameter,
            length=self.length,
            width=self.width,
            order_no=self.order_no,
            is_coolant=self.is_coolant,
        )

    def as_dict(self) -> dict:
        w, l = self.footprint
        return {
            "sku": self.sku,
            "name": self.name,
            "kind": self.kind,
            "kind_label": to_bottle_name(self.kind),
            "qty": self.qty,
            "height": round(self.height, 2),
            "weight": round(self.weight, 3),
            "diameter": round(self.diameter, 2) if self.kind == "bottle" else None,
            "length": round(w, 2),
            "width": round(l, 2),
            "unit_volume_l": mm3_to_liter(self.volume),
            "is_coolant": self.is_coolant,
        }


@dataclass
class PackConfig:
    """算法参数。"""

    #: 瓶装层内排布方式：hex=六边形交错（更密，默认），square=方形对齐
    pattern: Pattern = "hex"
    #: 物品之间的安全间隙 mm（默认 0，即紧密放置）
    gap: float = 0.0
    #: 盒装是否允许在水平面内旋转 90°（瓶装永远不允许旋转）
    rotate_box: bool = True
    #: 是否遵守包装箱最大承重
    respect_weight: bool = True
    #: 排序偏好：layer=同 SKU 成层（默认，贴合人工堆放）
    strategy: str = "layer"
    #: 选箱目标：cartons=箱数最少(默认) / volume=总箱容积最小 / cost=总箱成本最低
    objective: str = "cartons"
    #: 输出候选方案数量（默认 3，即方案1/2/3）
    top_k: int = 3
    #: 是否按箱型最大承重过滤（超重方案不参与排序）
    weight_filter: bool = True

    def as_dict(self) -> dict:
        return {
            "pattern": self.pattern,
            "gap": self.gap,
            "rotate_box": self.rotate_box,
            "respect_weight": self.respect_weight,
            "strategy": self.strategy,
            "objective": self.objective,
            "top_k": self.top_k,
            "weight_filter": self.weight_filter,
        }


@dataclass
class Placement:
    """一个已放置物品。"""

    sku: str
    name: str
    kind: Kind
    x: float
    y: float
    z: float
    w: float
    l: float
    h: float
    d: float = 0.0  # 瓶装直径
    layer: int = 0
    seq: int = 0
    weight: float = 0.0
    is_coolant: bool = False

    @property
    def cx(self) -> float:
        return self.x + self.w / 2.0

    @property
    def cy(self) -> float:
        return self.y + self.l / 2.0

    @property
    def top(self) -> float:
        return self.z + self.h

    def as_dict(self) -> dict:
        data = {
            "sku": self.sku,
            "name": self.name,
            "kind": self.kind,
            "x": round(self.x, 2),
            "y": round(self.y, 2),
            "z": round(self.z, 2),
            "w": round(self.w, 2),
            "l": round(self.l, 2),
            "h": round(self.h, 2),
            "cx": round(self.cx, 2),
            "cy": round(self.cy, 2),
            "layer": self.layer,
            "seq": self.seq,
            "weight": round(self.weight, 3),
            "is_coolant": self.is_coolant,
        }
        if self.kind == "bottle":
            data["d"] = round(self.d, 2)
        return data


@dataclass
class LayerInfo:
    """一层的信息（用于前端展示 & 打印装箱单）。"""

    index: int
    z: float
    height: float
    count: int
    skus: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "index": self.index,
            "z": round(self.z, 2),
            "height": round(self.height, 2),
            "count": self.count,
            "skus": self.skus,
        }


@dataclass
class CartonPlan:
    """单个包装箱的装箱方案。"""

    carton: Carton
    index: int
    placements: list[Placement]
    weight: float  # 箱内物品总重（货物 + 冷媒），不含箱体自重
    used_volume: float
    used_height: float
    layers: list[LayerInfo] = field(default_factory=list)
    goods_weight: float = 0.0
    coolant_weight: float = 0.0
    #: 冷媒明细：[{code, name, kind, required, placed, weight}]
    coolant_items: list[dict] = field(default_factory=list)
    #: 冷媒是否全部放下（放不下说明这一箱装不了冷链）
    coolant_ok: bool = True

    @property
    def fill_rate(self) -> float:
        return self.used_volume / self.carton.volume if self.carton.volume else 0.0

    @property
    def count(self) -> int:
        return len(self.placements)

    @property
    def goods_count(self) -> int:
        return sum(1 for p in self.placements if not p.is_coolant)

    @property
    def coolant_count(self) -> int:
        return sum(1 for p in self.placements if p.is_coolant)

    @property
    def total_weight(self) -> float:
        """含箱体自重的实际重量。"""
        return self.weight + self.carton.tare_weight

    @property
    def weight_ok(self) -> bool:
        """是否满足箱型最大承重。"""
        return self.total_weight <= self.carton.max_weight + EPS

    @property
    def has_coolant(self) -> bool:
        return self.coolant_weight > 0

    def as_dict(self) -> dict:
        return {
            "index": self.index,
            "carton": self.carton.as_dict(),
            "placements": [p.as_dict() for p in self.placements],
            "count": self.count,
            "goods_count": self.goods_count,
            "coolant_count": self.coolant_count,
            "weight": round(self.weight, 2),
            "goods_weight": round(self.goods_weight, 2),
            "coolant_weight": round(self.coolant_weight, 2),
            "total_weight": round(self.total_weight, 2),
            "weight_ok": self.weight_ok,
            "coolant_ok": self.coolant_ok,
            "coolant_items": self.coolant_items,
            "used_height": round(self.used_height, 2),
            "fill_rate": round(self.fill_rate, 4),
            "layers": [layer.as_dict() for layer in self.layers],
        }


@dataclass
class OrderPlan:
    """一个订单的完整预估方案。"""

    order_no: str
    items: list[Item]
    cartons: list[CartonPlan] = field(default_factory=list)
    unplaced: list[tuple[Item, int]] = field(default_factory=list)
    oversize: list[Item] = field(default_factory=list)
    strategy: str = ""
    #: 候选方案编号（1、2、3…）
    rank: int = 0
    #: 展示用名称，如「方案1」
    label: str = ""
    #: 订单类型（常温/冷藏/冷冻/深冷…）
    order_type: str = ""
    #: 温度区间 ℃
    temperature: tuple[float, float] = (15.0, 25.0)
    #: 冷媒合计用量 [{code, name, kind, qty, weight}]
    coolant_usage: list[dict] = field(default_factory=list)
    #: 排序说明（为什么推荐这一套）
    reason: str = ""

    @property
    def carton_count(self) -> int:
        return len(self.cartons)

    @property
    def total_item_count(self) -> int:
        """货品件数（不含冷媒）。"""
        return sum(plan.goods_count for plan in self.cartons)

    @property
    def total_coolant_count(self) -> int:
        return sum(plan.coolant_count for plan in self.cartons)

    @property
    def total_weight(self) -> float:
        return sum(plan.weight for plan in self.cartons)

    @property
    def total_used_volume(self) -> float:
        return sum(plan.used_volume for plan in self.cartons)

    @property
    def total_carton_volume(self) -> float:
        return sum(plan.carton.volume for plan in self.cartons)

    @property
    def fill_rate(self) -> float:
        if not self.total_carton_volume:
            return 0.0
        return self.total_used_volume / self.total_carton_volume

    @property
    def unplaced_count(self) -> int:
        return sum(qty for _, qty in self.unplaced)

    @property
    def weight_ok(self) -> bool:
        return all(plan.weight_ok for plan in self.cartons)

    @property
    def coolant_ok(self) -> bool:
        return all(plan.coolant_ok for plan in self.cartons)

    @property
    def coolant_weight(self) -> float:
        return sum(plan.coolant_weight for plan in self.cartons)

    @property
    def total_with_tare(self) -> float:
        return sum(plan.total_weight for plan in self.cartons)

    def as_dict(self, include_placements: bool = True) -> dict:
        cartons = []
        for plan in self.cartons:
            data = plan.as_dict()
            if not include_placements:
                data.pop("placements", None)
            cartons.append(data)
        return {
            "order_no": self.order_no,
            "rank": self.rank,
            "label": self.label or (f"方案{self.rank}" if self.rank else ""),
            "order_type": self.order_type,
            "temperature": {"min": self.temperature[0], "max": self.temperature[1]},
            "items": [item.as_dict() for item in self.items],
            "carton_count": self.carton_count,
            "item_count": self.total_item_count,
            "coolant_count": self.total_coolant_count,
            "total_weight": round(self.total_weight, 2),
            "total_with_tare": round(self.total_with_tare, 2),
            "coolant_weight": round(self.coolant_weight, 2),
            "coolant_usage": self.coolant_usage,
            "weight_ok": self.weight_ok,
            "coolant_ok": self.coolant_ok,
            "fill_rate": round(self.fill_rate, 4),
            "used_volume_l": mm3_to_liter(self.total_used_volume),
            "carton_volume_l": mm3_to_liter(self.total_carton_volume),
            "strategy": self.strategy,
            "reason": self.reason,
            "cartons": cartons,
            "unplaced": [
                {"sku": item.sku, "name": item.name, "qty": qty}
                for item, qty in self.unplaced
            ],
            "oversize": [item.as_dict() for item in self.oversize],
        }
