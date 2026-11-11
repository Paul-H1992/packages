"""冷链配置：温度范围、订单类型、蓝冰/干冰占比与用量计算。

占比语义
--------
* ``goods_ratio`` 是**货物**在「货物 + 冷媒」总量中的占比；
* 每个冷媒的 ``ratio`` 是它在总量中的占比；
* 三者之和应为 1（也允许 < 1，差额视为包装材料/空气）；
* 冷媒用量 = 货物量 × (冷媒占比 ÷ 货物占比) ÷ 单件冷媒量，**向上取整**。

举例：货物占比 70%、蓝冰 20%、干冰 10%，货物 10 kg，单块蓝冰 1 kg：

    蓝冰需求量 = 10 × (0.2 / 0.7) ≈ 2.86 kg → 向上取整 3 块
    干冰需求量 = 10 × (0.1 / 0.7) ≈ 1.43 kg → 向上取整 2 块

温度语义
--------
* 订单类型决定一个温度区间 ``temp_min ~ temp_max``（℃）；
* 每个冷媒有自己可维持的温度能力 ``temp_min ~ temp_max``；
* 蓝冰一般覆盖 0~10℃，干冰覆盖 -100~-20℃；
* 若冷媒的能力覆盖不了订单温度区间，会给出告警（不阻断计算）。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from typing import Any, Optional

from .models import Item

EPS = 1e-6

BLUE_ICE = "blue_ice"
DRY_ICE = "dry_ice"

COOLANT_LABELS = {BLUE_ICE: "蓝冰", DRY_ICE: "干冰"}
COOLANT_ALIASES = {
    BLUE_ICE: {"blue_ice", "blueice", "蓝冰", "冰袋", "蓄冷剂", "冰盒"},
    DRY_ICE: {"dry_ice", "dryice", "干冰", "固态二氧化碳"},
}


def parse_kind(value: Any) -> str:
    """把「蓝冰 / 干冰 / blue_ice / dry_ice」统一成内部枚举。"""
    text = str(value or "").strip().lower().replace(" ", "")
    for kind, aliases in COOLANT_ALIASES.items():
        if text in aliases:
            return kind
    if "干冰" in text or "dry" in text:
        return DRY_ICE
    if "蓝冰" in text or "冰" in text or "blue" in text:
        return BLUE_ICE
    return BLUE_ICE


def coolant_label(kind: str) -> str:
    return COOLANT_LABELS.get(kind, kind)


@dataclass
class CoolantSpec:
    """一种冷媒的规格（一块/一袋）。"""

    code: str
    name: str = ""
    kind: str = BLUE_ICE
    length: float = 0.0  # mm
    width: float = 0.0  # mm
    height: float = 0.0  # mm
    weight: float = 0.0  # g / 块
    temp_min: float = -100.0  # ℃
    temp_max: float = 100.0  # ℃
    ratio: float = 0.0  # 占总量的比例（0~1）
    enabled: bool = True

    @property
    def display_name(self) -> str:
        return self.name or f"{coolant_label(self.kind)} {self.code}"

    @property
    def volume(self) -> float:
        return self.length * self.width * self.height

    @property
    def kind_label(self) -> str:
        return coolant_label(self.kind)

    def validate(self) -> None:
        if not self.code:
            raise ValueError("冷媒编码不能为空")
        for field_name, label in (("length", "长"), ("width", "宽"), ("height", "高")):
            if getattr(self, field_name) <= 0:
                raise ValueError(f"冷媒 {self.code} 的{label}必须大于 0")
        if self.weight <= 0:
            raise ValueError(f"冷媒 {self.code} 的单块重量必须大于 0")
        if self.temp_min > self.temp_max:
            raise ValueError(f"冷媒 {self.code} 的温度下限不能高于上限")

    def can_hold(self, temp_min: float, temp_max: float) -> bool:
        """该冷媒能否覆盖目标温度区间。"""
        return self.temp_min <= temp_min + EPS and self.temp_max >= temp_max - EPS

    def to_item(self, qty: int = 1, order_no: str = "") -> Item:
        return Item(
            sku=self.code,
            name=self.display_name,
            kind="box",
            qty=qty,
            height=self.height,
            weight=self.weight,
            length=self.length,
            width=self.width,
            order_no=order_no,
            is_coolant=True,
        )

    def as_dict(self) -> dict:
        return {
            "code": self.code,
            "name": self.name,
            "kind": self.kind,
            "kind_label": self.kind_label,
            "length": round(self.length, 2),
            "width": round(self.width, 2),
            "height": round(self.height, 2),
            "weight": round(self.weight, 2),
            "volume_l": round(self.volume / 1_000_000.0, 4),
            "temp_min": self.temp_min,
            "temp_max": self.temp_max,
            "ratio": round(self.ratio, 4),
            "enabled": self.enabled,
        }


#: 内置冷媒默认规格（可在页面或接口里改）
DEFAULT_BLUE_ICE = CoolantSpec(
    code="BLUE-ICE",
    name="蓝冰 1kg",
    kind=BLUE_ICE,
    length=250,
    width=150,
    height=40,
    weight=1000,
    temp_min=0,
    temp_max=10,
)

DEFAULT_DRY_ICE = CoolantSpec(
    code="DRY-ICE",
    name="干冰 1kg",
    kind=DRY_ICE,
    length=200,
    width=120,
    height=50,
    weight=1000,
    temp_min=-100,
    temp_max=-20,
)


#: 订单类型预设：温度区间 + 冷媒占比
PRESETS: dict[str, dict[str, Any]] = {
    "常温": {
        "temp_min": 15.0,
        "temp_max": 25.0,
        "goods_ratio": 1.0,
        "blue_ice": 0.0,
        "dry_ice": 0.0,
        "enabled": False,
    },
    "冷藏": {
        "temp_min": 2.0,
        "temp_max": 8.0,
        "goods_ratio": 0.75,
        "blue_ice": 0.25,
        "dry_ice": 0.0,
        "enabled": True,
    },
    "冷冻": {
        "temp_min": -20.0,
        "temp_max": -10.0,
        "goods_ratio": 0.75,
        "blue_ice": 0.0,
        "dry_ice": 0.25,
        "enabled": True,
    },
    "深冷": {
        "temp_min": -80.0,
        "temp_max": -60.0,
        "goods_ratio": 0.70,
        "blue_ice": 0.0,
        "dry_ice": 0.30,
        "enabled": True,
    },
}


@dataclass
class ColdChainConfig:
    """一套冷链规则：温度区间 + 占比 + 冷媒规格。"""

    enabled: bool = False
    order_type: str = "常温"
    temp_min: float = 15.0
    temp_max: float = 25.0
    ratio_basis: str = "weight"  # weight=按重量 | volume=按体积
    goods_ratio: float = 1.0
    coolants: list[CoolantSpec] = field(default_factory=list)
    #: 订单类型 → 规则（温度/占比），可由页面或接口覆盖内置预设
    presets: dict[str, dict[str, float]] = field(default_factory=dict)

    # ---------------- 基础 ----------------

    @property
    def temp_range(self) -> tuple[float, float]:
        return (self.temp_min, self.temp_max)

    def active_coolants(self) -> list[CoolantSpec]:
        if not self.enabled:
            return []
        return [c for c in self.coolants if c.enabled and c.ratio > EPS]

    def find(self, kind: str) -> Optional[CoolantSpec]:
        for c in self.coolants:
            if c.kind == kind:
                return c
        return None

    # ---------------- 校验 ----------------

    def warnings(self) -> list[str]:
        out: list[str] = []
        if self.temp_min > self.temp_max:
            out.append(f"温度范围不合法：{self.temp_min}℃ > {self.temp_max}℃，已自动交换")
        if not self.enabled:
            return out
        if self.goods_ratio <= EPS:
            out.append("货物占比必须大于 0，否则无法计算冷媒用量")
        total = self.goods_ratio + sum(c.ratio for c in self.coolants if c.enabled)
        if total > 1 + 1e-6:
            out.append(f"货物 + 冷媒占比合计 {total * 100:.0f}% 超过 100%，请调整")
        for c in self.active_coolants():
            if not c.can_hold(self.temp_min, self.temp_max):
                out.append(
                    f"{c.display_name} 的适用温度 {c.temp_min}~{c.temp_max}℃ "
                    f"覆盖不了订单温度 {self.temp_min}~{self.temp_max}℃"
                )
        return out

    # ---------------- 用量 ----------------

    def coolant_requirements(
        self, goods_weight: float, goods_volume: float
    ) -> list[tuple[CoolantSpec, int]]:
        """按占比计算每个冷媒需要多少块。"""
        coolants = self.active_coolants()
        if not coolants or goods_weight <= 0 or goods_volume <= 0:
            return []
        if self.goods_ratio <= EPS:
            return []

        amount = goods_weight if self.ratio_basis == "weight" else goods_volume
        out: list[tuple[CoolantSpec, int]] = []
        for spec in coolants:
            need = amount * (spec.ratio / self.goods_ratio)
            unit = spec.weight if self.ratio_basis == "weight" else spec.volume
            if unit <= EPS:
                continue
            qty = int(math.ceil(need / unit - 1e-9))
            if qty > 0:
                out.append((spec, qty))
        return out

    def coolant_weight(self, goods_weight: float, goods_volume: float) -> float:
        """本单冷媒总重（g）。"""
        return sum(qty * spec.weight for spec, qty in self.coolant_requirements(goods_weight, goods_volume))

    def weight_reserve_ratio(self, goods_weight: float, goods_volume: float) -> float:
        """冷媒重量 ÷ 货物重量，用于装箱时预留承重。"""
        if goods_weight <= EPS:
            return 0.0
        return self.coolant_weight(goods_weight, goods_volume) / goods_weight

    def coolant_items(
        self, goods_weight: float, goods_volume: float, order_no: str = ""
    ) -> list[Item]:
        return [
            spec.to_item(qty, order_no=order_no)
            for spec, qty in self.coolant_requirements(goods_weight, goods_volume)
        ]

    # ---------------- 订单类型 ----------------

    def for_order_type(self, order_type: str | None) -> "ColdChainConfig":
        """按订单类型套用内置预设（温度 + 占比），冷媒规格沿用当前配置。"""
        name = (order_type or "").strip()
        if not name:
            return self

        preset = None
        if self.presets and name in self.presets:
            preset = self.presets[name]
        elif name in PRESETS:
            preset = PRESETS[name]
        if preset is None:
            return replace(self, order_type=name)

        def _pick(key: str, default: float) -> float:
            try:
                return float(preset.get(key, default))
            except (TypeError, ValueError):
                return default

        preset = {
            "temp_min": _pick("temp_min", self.temp_min),
            "temp_max": _pick("temp_max", self.temp_max),
            "goods_ratio": _pick("goods_ratio", self.goods_ratio),
            "blue_ice": _pick("blue_ice", 0.0),
            "dry_ice": _pick("dry_ice", 0.0),
            "enabled": bool(preset.get("enabled", self.enabled)),
        }
        coolants: list[CoolantSpec] = []
        for spec in self.coolants:
            ratio = preset["blue_ice"] if spec.kind == BLUE_ICE else preset["dry_ice"]
            coolants.append(replace(spec, ratio=ratio, enabled=ratio > 0 or spec.enabled))
        if not coolants:
            coolants = [
                replace(DEFAULT_BLUE_ICE, ratio=preset["blue_ice"]),
                replace(DEFAULT_DRY_ICE, ratio=preset["dry_ice"]),
            ]
        return replace(
            self,
            order_type=name,
            temp_min=preset["temp_min"],
            temp_max=preset["temp_max"],
            goods_ratio=preset["goods_ratio"],
            enabled=preset["enabled"],
            coolants=coolants,
        )

    def as_dict(self) -> dict:
        return {
            "enabled": self.enabled,
            "order_type": self.order_type,
            "temp_min": self.temp_min,
            "temp_max": self.temp_max,
            "ratio_basis": self.ratio_basis,
            "goods_ratio": round(self.goods_ratio, 4),
            "coolants": [c.as_dict() for c in self.coolants],
            "presets": self.presets,
        }


# ---------------------------------------------------------------------------
# 构造 / 解析
# ---------------------------------------------------------------------------


def default_cold_chain(enabled: bool = False, order_type: str = "常温") -> ColdChainConfig:
    """返回一套默认配置（蓝冰 + 干冰各一条规格）。"""
    return ColdChainConfig(
        enabled=enabled,
        order_type=order_type,
        temp_min=15.0,
        temp_max=25.0,
        ratio_basis="weight",
        goods_ratio=1.0,
        coolants=[replace(DEFAULT_BLUE_ICE), replace(DEFAULT_DRY_ICE)],
    )


def _num(value: Any, default: float) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def parse_coolant(data: dict[str, Any], fallback: CoolantSpec) -> CoolantSpec:
    kind = parse_kind(data.get("kind", fallback.kind))
    return CoolantSpec(
        code=str(data.get("code") or fallback.code),
        name=str(data.get("name") or fallback.name),
        kind=kind,
        length=_num(data.get("length"), fallback.length),
        width=_num(data.get("width"), fallback.width),
        height=_num(data.get("height"), fallback.height),
        weight=_num(data.get("weight"), fallback.weight),
        temp_min=_num(data.get("temp_min"), fallback.temp_min),
        temp_max=_num(data.get("temp_max"), fallback.temp_max),
        ratio=_num(data.get("ratio"), 0.0),
        enabled=bool(data.get("enabled", True)),
    )


def parse_cold_chain(data: dict[str, Any] | None) -> ColdChainConfig:
    """把接口/页面传来的 JSON 解析成 :class:`ColdChainConfig`。"""
    if not data:
        return default_cold_chain()

    raw_coolants = data.get("coolants")
    coolants: list[CoolantSpec] = []
    if isinstance(raw_coolants, list) and raw_coolants:
        for idx, item in enumerate(raw_coolants):
            if not isinstance(item, dict):
                continue
            kind = parse_kind(item.get("kind"))
            fallback = DEFAULT_BLUE_ICE if kind == BLUE_ICE else DEFAULT_DRY_ICE
            coolants.append(parse_coolant(item, fallback))
    if not coolants:
        coolants = [replace(DEFAULT_BLUE_ICE), replace(DEFAULT_DRY_ICE)]

    presets: dict[str, dict[str, float]] = {}
    raw_presets = data.get("presets")
    if isinstance(raw_presets, dict):
        for name, preset in raw_presets.items():
            if not isinstance(preset, dict):
                continue
            presets[str(name)] = {
                "temp_min": _num(preset.get("temp_min"), 15.0),
                "temp_max": _num(preset.get("temp_max"), 25.0),
                "goods_ratio": _num(preset.get("goods_ratio"), 1.0),
                "blue_ice": _num(preset.get("blue_ice"), 0.0),
                "dry_ice": _num(preset.get("dry_ice"), 0.0),
                "enabled": bool(preset.get("enabled", True)),
            }

    cold = ColdChainConfig(
        enabled=bool(data.get("enabled", False)),
        order_type=str(data.get("order_type") or "常温"),
        temp_min=_num(data.get("temp_min"), 15.0),
        temp_max=_num(data.get("temp_max"), 25.0),
        ratio_basis=str(data.get("ratio_basis") or "weight"),
        goods_ratio=_num(data.get("goods_ratio"), 1.0),
        coolants=coolants,
        presets=presets,
    )
    if cold.temp_min > cold.temp_max:
        cold.temp_min, cold.temp_max = cold.temp_max, cold.temp_min
    if cold.ratio_basis not in ("weight", "volume"):
        cold.ratio_basis = "weight"
    return cold
