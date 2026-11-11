"""分层网格 3D 装箱算法。

为什么不用通用三维装箱库
------------------------
GitHub 上主流的通用 3D bin packing 库（如 ``enzoruiz/3dbinpacking``(py3dbp)、
``dwave-examples/3d-bin-packing``、``DeepPack3D``）都会把物品当作可任意旋转的长方体，
并追求「最小箱数/最大填充率」。它们与本项目的业务约束并不匹配：

* 瓶装是**直立圆柱**，不能旋转，必须轴线竖直；
* 同一排、相邻排要**紧密贴合**（圆柱之间可以交错嵌套，见六边形排布）；
* 必须**逐层堆叠**，保证人工/机械可以按层码放，而不是随机塞满。

因此本模块实现「同 SKU 成层 + 层内点阵 + 层间堆叠 + 缝隙回填」的分层装箱：

1. 每一层挑选一个「主 SKU」，按点阵铺满整层：
   * 瓶装：六边形交错（默认）或方形对齐，圆柱紧贴，行距为 ``d·√3/2``；
   * 盒装：方形点阵，允许水平 90° 旋转以取更优铺法；
2. 层内剩余零碎空间用「最大矩形（MaxRects）」回填更矮的其他货品，保证该层顶面尽量平整；
3. 一层码完后抬高 ``层高``，继续下一层，直到箱内高度或承重耗尽；
4. 当前箱装满后自动开新箱，最后在所有候选箱型中挑选「箱数最少、填充率最高」的方案。

算法是确定性的（无随机数），同一份输入永远得到同一份方案，方便人工复核。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Iterable, Sequence

if TYPE_CHECKING:  # pragma: no cover
    from .coldchain import ColdChainConfig

from .models import (
    EPS,
    Carton,
    CartonPlan,
    Item,
    LayerInfo,
    OrderPlan,
    PackConfig,
    Placement,
)


# ---------------------------------------------------------------------------
# 层内点阵
# ---------------------------------------------------------------------------


def _lattice(
    W: float,
    L: float,
    footprint: tuple[float, float],
    pattern: str,
    gap: float,
) -> list[tuple[float, float]]:
    """计算一层内物品外接矩形的左下角坐标（按行优先）。

    ``pattern="hex"`` 仅对圆瓶有意义：奇数排横向偏移半个间距，行距 ``d·√3/2``，
    使相邻排的圆柱刚好相切，这是圆瓶最紧密的平面排布。
    ``pattern="square"`` 为方形对齐，行距 = 列距 = 直径/边长 + 间隙。
    """
    w, l = footprint
    if w <= EPS or l <= EPS:
        return []

    if pattern == "hex":
        d = w
        pitch = d + gap
        row_pitch = pitch * math.sqrt(3.0) / 2.0
        if row_pitch <= EPS:
            return []
        max_row = int(math.floor((L - d) / row_pitch + 1e-9))
        if max_row < 0:
            return []
        spots: list[tuple[float, float]] = []
        for row in range(max_row + 1):
            offset = 0.0 if row % 2 == 0 else pitch / 2.0
            max_col = int(math.floor((W - d - offset) / pitch + 1e-9))
            if max_col < 0:
                continue
            base_y = row * row_pitch
            for col in range(max_col + 1):
                spots.append((offset + col * pitch, base_y))
        return spots

    pitch_x = w + gap
    pitch_y = l + gap
    cols = int(math.floor((W - w) / pitch_x + 1e-9)) + 1 if W + EPS >= w else 0
    rows = int(math.floor((L - l) / pitch_y + 1e-9)) + 1 if L + EPS >= l else 0
    if cols <= 0 or rows <= 0:
        return []
    return [(i * pitch_x, j * pitch_y) for j in range(rows) for i in range(cols)]


def _footprints(item: Item, cfg: PackConfig) -> list[tuple[str, tuple[float, float]]]:
    """返回该货品的候选底面（含排布方式）。瓶装只有一种朝向。"""
    if item.kind == "bottle":
        d = item.diameter
        if d <= EPS:
            return []
        return [(cfg.pattern, (d, d))]

    w, l = item.length, item.width
    if w <= EPS or l <= EPS:
        return []
    options = [("square", (w, l))]
    if cfg.rotate_box and abs(w - l) > EPS:
        options.append(("square", (l, w)))
    return options


# ---------------------------------------------------------------------------
# MaxRects：层内剩余空间回填
# ---------------------------------------------------------------------------


class _Floor:
    """一层底面的二维最大矩形空闲空间管理器。"""

    __slots__ = ("W", "L", "free")

    def __init__(self, W: float, L: float) -> None:
        self.W = float(W)
        self.L = float(L)
        # 每个元素 [x, y, w, l]
        self.free: list[list[float]] = [[0.0, 0.0, float(W), float(L)]]

    def find(self, w: float, l: float) -> tuple[float, float] | None:
        """Best-Short-Side-Fit：返回可放置的 (x, y)，找不到返回 None。"""
        best_key: tuple[float, float] | None = None
        best_xy: tuple[float, float] | None = None
        for fx, fy, fw, fl in self.free:
            if fw + EPS < w or fl + EPS < l:
                continue
            short = min(fw - w, fl - l)
            long_side = max(fw - w, fl - l)
            key = (short, long_side)
            if best_key is None or key < best_key:
                best_key = key
                best_xy = (fx, fy)
        return best_xy

    def place(self, x: float, y: float, w: float, l: float) -> None:
        new_free: list[list[float]] = []
        for fx, fy, fw, fl in self.free:
            # 不相交则原样保留
            if (
                x >= fx + fw - EPS
                or x + w <= fx + EPS
                or y >= fy + fl - EPS
                or y + l <= fy + EPS
            ):
                new_free.append([fx, fy, fw, fl])
                continue
            if x > fx + EPS:
                new_free.append([fx, fy, x - fx, fl])
            if x + w < fx + fw - EPS:
                new_free.append([x + w, fy, fx + fw - (x + w), fl])
            if y > fy + EPS:
                new_free.append([fx, fy, fw, y - fy])
            if y + l < fy + fl - EPS:
                new_free.append([fx, y + l, fw, fy + fl - (y + l)])
        self.free = self._prune(new_free)

    @staticmethod
    def _contains(a: Sequence[float], b: Sequence[float]) -> bool:
        return (
            a[0] <= b[0] + EPS
            and a[1] <= b[1] + EPS
            and a[0] + a[2] >= b[0] + b[2] - EPS
            and a[1] + a[3] >= b[1] + b[3] - EPS
        )

    def _prune(self, rects: list[list[float]]) -> list[list[float]]:
        rects = [r for r in rects if r[2] > EPS and r[3] > EPS]
        kept: list[list[float]] = []
        for i, r in enumerate(rects):
            duplicated = False
            for j, other in enumerate(rects):
                if i == j:
                    continue
                if self._contains(other, r):
                    # 严格更大则一定被包含；完全相等时保留索引较小的那个
                    if (
                        other[2] > r[2] + EPS
                        or other[3] > r[3] + EPS
                        or j < i
                    ):
                        duplicated = True
                        break
            if not duplicated:
                kept.append(r)
        return kept


# ---------------------------------------------------------------------------
# 规划一层
# ---------------------------------------------------------------------------


@dataclass
class _Layer:
    placements: list[Placement]
    height: float
    weight: float


def _plan_layer(
    carton: Carton,
    pool: list[list],
    z: float,
    cfg: PackConfig,
    weight_left: float,
    layer_no: int,
    seq_start: int,
) -> _Layer | None:
    """在高度 ``z`` 规划一层，并就地扣减 ``pool`` 中的数量。"""
    W, L = carton.length, carton.width
    max_h = carton.height - z
    if max_h <= EPS:
        return None

    limited_weight = cfg.respect_weight and math.isfinite(carton.max_weight)
    weight_cap = max(0.0, weight_left)

    # ---- 1. 挑选本层主 SKU：按「单层可放体积」最大 ----
    best = None  # dict
    for idx, (item, qty) in enumerate(pool):
        if qty <= 0:
            continue
        if item.height > max_h + EPS:
            continue
        if limited_weight and item.weight > 0 and weight_cap < item.weight - EPS:
            continue

        for pattern, fp in _footprints(item, cfg):
            if fp[0] > W + EPS or fp[1] > L + EPS:
                continue
            spots = _lattice(W, L, fp, pattern, cfg.gap)
            if not spots:
                continue
            n = min(len(spots), qty)
            if limited_weight and item.weight > 0:
                n = min(n, int(math.floor(weight_cap / item.weight + 1e-9)))
            if n <= 0:
                continue
            score = (n * fp[0] * fp[1] * item.height, n)
            if best is None or score > best["score"]:
                best = {
                    "score": score,
                    "idx": idx,
                    "pattern": pattern,
                    "fp": fp,
                    "spots": spots,
                    "n": n,
                }

    if best is None:
        return None

    primary: Item = pool[best["idx"]][0]
    layer_h = primary.height
    placements: list[Placement] = []
    seq = seq_start
    weight = 0.0

    def emit(item: Item, x: float, y: float, h: float, w: float, l: float) -> None:
        nonlocal seq, weight
        placements.append(
            Placement(
                sku=item.sku,
                name=item.name,
                kind=item.kind,
                x=x,
                y=y,
                z=z,
                w=w,
                l=l,
                h=h,
                d=item.diameter if item.kind == "bottle" else 0.0,
                layer=layer_no,
                seq=seq,
                weight=item.weight,
                is_coolant=item.is_coolant,
            )
        )
        seq += 1
        weight += item.weight

    # ---- 2. 主 SKU 按点阵铺满 ----
    count = best["n"]
    for i in range(count):
        x, y = best["spots"][i]
        emit(primary, x, y, primary.height, best["fp"][0], best["fp"][1])
    pool[best["idx"]][1] -= count
    weight_cap -= primary.weight * count

    # 六边形交错时外接矩形互相重叠，无法用矩形空闲空间模型描述，跳过回填
    if best["pattern"] == "hex":
        return _Layer(placements, layer_h, weight)

    # ---- 3. 用更矮的货品回填本层零碎空间（保证层顶面平整） ----
    floor = _Floor(W, L)
    for p in placements:
        floor.place(p.x, p.y, p.w, p.l)

    order = sorted(
        range(len(pool)),
        key=lambda i: -(
            pool[i][0].footprint[0] * pool[i][0].footprint[1] if pool[i][1] > 0 else 0.0
        ),
    )

    for idx in order:
        item, qty = pool[idx]
        if qty <= 0 or idx == best["idx"]:
            continue
        if item.height > layer_h + EPS:  # 不允许高过本层，否则层高会变化
            continue
        if limited_weight and item.weight > 0 and weight_cap < item.weight - EPS:
            break

        for _pattern, fp in _footprints(item, cfg):
            fw, fl = fp
            if fw > W + EPS or fl > L + EPS:
                continue
            while qty > 0:
                if limited_weight and item.weight > 0 and weight_cap < item.weight - EPS:
                    break
                spot = floor.find(fw, fl)
                if spot is None:
                    break
                x, y = spot
                floor.place(x, y, fw, fl)
                emit(item, x, y, item.height, fw, fl)
                pool[idx][1] -= 1
                qty -= 1
                weight_cap -= item.weight
            if qty <= 0:
                break

    if not placements:
        return None
    return _Layer(placements, layer_h, weight)


# ---------------------------------------------------------------------------
# 单个包装箱
# ---------------------------------------------------------------------------


def _placement_volume(p: Placement) -> float:
    """单件物品体积（瓶装按圆柱，盒装按长方体）。"""
    if p.kind == "bottle":
        return math.pi * (p.d / 2.0) ** 2 * p.h
    return p.w * p.l * p.h


def _place_layers(
    carton: Carton,
    pool: list[list],
    cfg: PackConfig,
    z: float,
    layer_no: int,
    seq: int,
    weight_left: float,
    placements: list[Placement],
    layer_infos: list[LayerInfo],
) -> tuple[float, int, int, float]:
    """从高度 ``z`` 开始逐层码放 ``pool``，就地扣减数量。"""
    guard = 0
    while z < carton.height - EPS:
        guard += 1
        if guard > 10000:
            break
        if not any(q > 0 for _, q in pool):
            break
        if cfg.respect_weight and weight_left <= EPS:
            break

        layer = _plan_layer(carton, pool, z, cfg, weight_left, layer_no, seq)
        if layer is None or not layer.placements:
            break

        placements.extend(layer.placements)
        seq += len(layer.placements)
        weight_left -= layer.weight

        skus: list[str] = []
        for p in layer.placements:
            if p.sku not in skus:
                skus.append(p.sku)
        layer_infos.append(
            LayerInfo(
                index=layer_no,
                z=z,
                height=layer.height,
                count=len(layer.placements),
                skus=skus,
            )
        )
        z += layer.height
        layer_no += 1

    return z, layer_no, seq, weight_left


def pack_carton(
    carton: Carton,
    pool: list[list],
    cfg: PackConfig,
    index: int = 0,
    cold: "ColdChainConfig | None" = None,
) -> CartonPlan:
    """把 ``pool``（货物，就地扣减）装进一个包装箱。

    分两段：
    1. **货物段**：从箱底开始逐层码放货物；
    2. **冷媒段**：按货物重量/体积与占比算出本箱冷媒数量，码放在货物**上方**。

    装箱时会给冷媒预留承重：货物的可用承重 = (箱限重 - 箱自重) / (1 + 冷媒/货物重量比)。
    """
    placements: list[Placement] = []
    layer_infos: list[LayerInfo] = []
    z = 0.0
    layer_no = 0
    seq = 0

    cold_enabled = cold is not None and cold.enabled

    # 冷媒会占重量：先估一个预留比例，避免货物把承重吃满
    reserve = 0.0
    if cold_enabled and cold is not None:
        pool_weight = sum(item.weight * qty for item, qty in pool)
        pool_volume = sum(item.volume * qty for item, qty in pool)
        reserve = cold.weight_reserve_ratio(pool_weight, pool_volume)

    max_weight = carton.max_weight if math.isfinite(carton.max_weight) else math.inf
    if cfg.respect_weight and math.isfinite(max_weight):
        payload = max(max_weight - carton.tare_weight, 0.0)
        goods_cap = payload / (1.0 + reserve) if reserve > 0 else payload
    else:
        goods_cap = math.inf

    # ---- 1. 货物（冷媒需要空间时，逐次压低货物可用高度） ----
    snapshot = [qty for _, qty in pool]
    floor_area = max(carton.length * carton.width, EPS)
    reserve_h = 0.0
    requirements: list[tuple[Any, int]] = []
    goods_weight = 0.0
    goods_volume = 0.0

    for attempt in range(4):
        # 还原货品数量，重新从箱底码放
        for entry, qty in zip(pool, snapshot):
            entry[1] = qty
        placements.clear()
        layer_infos.clear()
        z = 0.0
        layer_no = 0
        seq = 0

        if reserve_h > EPS:
            goods_carton = replace(carton, height=max(carton.height - reserve_h, 1.0))
        else:
            goods_carton = carton

        weight_left = goods_cap if cfg.respect_weight else math.inf
        z, layer_no, seq, _weight_left = _place_layers(
            goods_carton, pool, cfg, z, layer_no, seq, weight_left, placements, layer_infos
        )

        goods_weight = sum(p.weight for p in placements)
        goods_volume = sum(_placement_volume(p) for p in placements)
        requirements = (
            cold.coolant_requirements(goods_weight, goods_volume)
            if (cold_enabled and cold is not None and placements)
            else []
        )
        if not requirements:
            break

        coolant_volume = sum(spec.volume * qty for spec, qty in requirements)
        need_h = coolant_volume / floor_area
        # 冷媒是一层一层码的：高度至少按一层冷媒的高度向上取整
        min_cool_h = min((spec.height for spec, _ in requirements), default=0.0)
        if min_cool_h > EPS:
            need_h = math.ceil(need_h / min_cool_h) * min_cool_h
        if need_h <= EPS:
            break
        if z + need_h <= carton.height + 1.0:
            break
        # 冷媒放不下：给货物让出高度后重排
        reserve_h = min(carton.height * 0.7, need_h * 1.05 + 2.0)

    # ---- 2. 冷媒（码在货物上方） ----
    coolant_weight = 0.0
    coolant_items: list[dict] = []
    coolant_ok = True

    if cold_enabled and cold is not None and placements:
        if requirements:
            coolant_pool: list[list] = [[spec.to_item(qty), qty] for spec, qty in requirements]

            if cfg.respect_weight and math.isfinite(max_weight):
                coolant_budget = max(max_weight - carton.tare_weight - goods_weight, 0.0)
            else:
                coolant_budget = math.inf

            z, layer_no, seq, _ = _place_layers(
                carton,
                coolant_pool,
                cfg,
                z,
                layer_no,
                seq,
                coolant_budget,
                placements,
                layer_infos,
            )

            remaining = {item.sku: qty for item, qty in coolant_pool}
            for spec, required in requirements:
                placed = required - remaining.get(spec.code, 0)
                placed = max(0, min(placed, required))
                coolant_weight += placed * spec.weight
                coolant_items.append(
                    {
                        "code": spec.code,
                        "name": spec.display_name,
                        "kind": spec.kind,
                        "kind_label": spec.kind_label,
                        "required": required,
                        "placed": placed,
                        "unit_weight": round(spec.weight, 2),
                        "weight": round(placed * spec.weight, 2),
                    }
                )
                if placed < required:
                    coolant_ok = False

    weight = goods_weight + coolant_weight
    used_volume = sum(_placement_volume(p) for p in placements)
    used_height = max((p.top for p in placements), default=0.0)

    return CartonPlan(
        carton=carton,
        index=index,
        placements=placements,
        weight=weight,
        used_volume=used_volume,
        used_height=used_height,
        layers=layer_infos,
        goods_weight=goods_weight,
        coolant_weight=coolant_weight,
        coolant_items=coolant_items,
        coolant_ok=coolant_ok,
    )


def pack_into(
    carton: Carton,
    items: Iterable[Item],
    cfg: PackConfig,
    cold: "ColdChainConfig | None" = None,
) -> tuple[list[CartonPlan], list[tuple[Item, int]]]:
    """用同一种包装箱重复装箱，直到装不下或货品装完。"""
    pool: list[list] = [[item, item.qty] for item in items if item.qty > 0]
    plans: list[CartonPlan] = []
    guard = 0

    while any(q > 0 for _, q in pool):
        guard += 1
        if guard > 10000:
            break
        plan = pack_carton(carton, pool, cfg, index=len(plans), cold=cold)
        if not plan.placements:
            break
        plans.append(plan)

    leftover = [(item, qty) for item, qty in pool if qty > 0]
    return plans, leftover


# ---------------------------------------------------------------------------
# 订单级：选箱 + 多箱 + Top-K 候选
# ---------------------------------------------------------------------------


def item_fits_carton(item: Item, carton: Carton, cfg: PackConfig) -> bool:
    """单件货品能否放进该包装箱（不考虑其他货品）。"""
    if item.height > carton.height + EPS:
        return False
    if item.kind == "bottle":
        return item.diameter <= min(carton.length, carton.width) + EPS
    if cfg.rotate_box:
        a, b = sorted((carton.length, carton.width))
        w, l = sorted((item.length, item.width))
        return w <= a + EPS and l <= b + EPS
    return item.length <= carton.length + EPS and item.width <= carton.width + EPS


def _reindex(plans: list[CartonPlan]) -> None:
    for i, plan in enumerate(plans):
        plan.index = i


def _complete_mixed(
    cartons: Sequence[Carton],
    plans: list[CartonPlan],
    leftover: list[tuple[Item, int]],
    cfg: PackConfig,
    cold: "ColdChainConfig | None" = None,
) -> tuple[list[CartonPlan], list[tuple[Item, int]]]:
    """当单一箱型装不完时，用其他箱型补齐剩余货品。"""
    remaining = list(leftover)
    guard = 0

    while remaining and guard < 32:
        guard += 1
        progress = False
        for carton in sorted(cartons, key=lambda c: c.volume):
            if not remaining:
                break
            candidates = [
                item.clone(qty)
                for item, qty in remaining
                if qty > 0 and item_fits_carton(item, carton, cfg)
            ]
            if not candidates:
                continue
            new_plans, _left = pack_into(carton, candidates, cfg, cold=cold)
            if not new_plans:
                continue
            packed: dict[str, int] = {}
            for plan in new_plans:
                for p in plan.placements:
                    if p.sku:
                        packed[p.sku] = packed.get(p.sku, 0) + 1
            if not packed:
                continue
            progress = True
            plans.extend(new_plans)
            remaining = [
                (item, qty - packed.get(item.sku, 0))
                for item, qty in remaining
                if qty - packed.get(item.sku, 0) > 0
            ]
        if not progress:
            break

    return plans, remaining


@dataclass
class _Option:
    """一种候选装箱方案（对应一种选箱策略）。"""

    plans: list[CartonPlan]
    leftover: list[tuple[Item, int]]
    fill: float
    strategy: str
    carton_code: str
    carton_count: int
    total_volume: float
    total_cost: float
    cost_known: bool
    total_weight: float
    weight_ok: bool
    coolant_ok: bool
    leftover_units: int

    @property
    def signature(self) -> tuple:
        return (self.carton_count, tuple(sorted(p.carton.code for p in self.plans)))


def _make_option(
    plans: list[CartonPlan],
    leftover: list[tuple[Item, int]],
    strategy: str,
    carton_code: str = "",
) -> _Option:
    _reindex(plans)
    used = sum(p.used_volume for p in plans)
    capacity = sum(p.carton.volume for p in plans)
    prices = [p.carton.price for p in plans]
    cost_known = bool(prices) and all(price is not None for price in prices)
    cost = sum(price for price in prices if price is not None)
    codes = sorted({p.carton.code for p in plans})
    return _Option(
        plans=plans,
        leftover=list(leftover),
        fill=used / capacity if capacity else 0.0,
        strategy=strategy,
        carton_code=carton_code or (codes[0] if len(codes) == 1 else "+".join(codes)),
        carton_count=len(plans),
        total_volume=capacity,
        total_cost=cost,
        cost_known=cost_known,
        total_weight=sum(p.total_weight for p in plans),
        weight_ok=all(p.weight_ok for p in plans),
        coolant_ok=all(p.coolant_ok for p in plans),
        leftover_units=sum(q for _, q in leftover),
    )


def _rank(opt: _Option, cfg: PackConfig) -> tuple:
    """排序键（越小越优）：先保「装得下 + 冷媒放得下 + 承重达标」，再按目标。"""
    validity = 0
    if not opt.coolant_ok:
        validity += 2
    if cfg.weight_filter and not opt.weight_ok:
        validity += 4
    has_left = 0 if not opt.leftover else 1
    base = (validity, has_left, opt.leftover_units)

    if cfg.objective == "volume":
        return base + (opt.total_volume, opt.carton_count, -opt.fill)
    if cfg.objective == "cost" and opt.cost_known:
        return base + (opt.total_cost, opt.carton_count, -opt.fill)
    return base + (opt.carton_count, -opt.fill)


def _reason(opt: _Option, cfg: PackConfig) -> str:
    parts: list[str] = []
    label = {"cartons": "箱数最少", "volume": "总箱容积最小", "cost": "总成本最低"}.get(
        cfg.objective, "箱数最少"
    )
    parts.append(label)
    if not opt.leftover:
        parts.append("货物全部装箱")
    else:
        parts.append(f"剩余 {opt.leftover_units} 件未装")
    if opt.coolant_ok:
        parts.append("冷媒全部放下")
    else:
        parts.append("冷媒未全部放下")
    if opt.weight_ok:
        parts.append("承重达标")
    else:
        parts.append("承重超标")
    return "；".join(parts)


def _aggregate_coolants(plans: list[CartonPlan]) -> list[dict]:
    usage: dict[str, dict] = {}
    for plan in plans:
        for item in plan.coolant_items:
            entry = usage.setdefault(
                item["code"],
                {
                    "code": item["code"],
                    "name": item.get("name", ""),
                    "kind": item.get("kind", ""),
                    "kind_label": item.get("kind_label", ""),
                    "qty": 0,
                    "weight": 0.0,
                    "unit_weight": item.get("unit_weight", 0.0),
                },
            )
            entry["qty"] += int(item.get("placed", 0))
            entry["weight"] = round(entry["weight"] + float(item.get("weight", 0.0)), 2)
    return list(usage.values())


def _build_order_plan(
    order_no: str,
    items: list[Item],
    oversize: list[Item],
    opt: _Option,
    rank: int,
    cfg: PackConfig,
    cold: "ColdChainConfig | None",
) -> OrderPlan:
    _reindex(opt.plans)
    temperature = cold.temp_range if cold is not None else (15.0, 25.0)
    return OrderPlan(
        order_no=order_no,
        items=list(items),
        cartons=list(opt.plans),
        unplaced=list(opt.leftover),
        oversize=oversize,
        strategy=opt.strategy,
        rank=rank,
        label=f"方案{rank}",
        order_type=cold.order_type if cold is not None else "",
        temperature=temperature,
        coolant_usage=_aggregate_coolants(opt.plans),
        reason=_reason(opt, cfg),
    )


def optimize_order(
    order_no: str,
    items: Sequence[Item],
    cartons: Sequence[Carton],
    cfg: PackConfig,
    cold: "ColdChainConfig | None" = None,
) -> list[OrderPlan]:
    """为单个订单生成 Top-K 候选方案（默认 3 套：方案1/2/3）。

    返回按推荐度排序的 :class:`OrderPlan` 列表；列表为空表示没有任何箱型装得下。
    """
    items = [item for item in items if item.qty > 0 and not item.is_coolant]
    packable: list[Item] = []
    oversize: list[Item] = []
    for item in items:
        if any(item_fits_carton(item, c, cfg) for c in cartons):
            packable.append(item)
        else:
            oversize.append(item)

    if not cartons or not packable:
        if not items:
            return []
        # 没有任何箱型装得下：返回一个占位方案，保留超规/未装信息
        return [
            OrderPlan(
                order_no=order_no,
                items=list(items),
                cartons=[],
                unplaced=[(item, item.qty) for item in packable],
                oversize=oversize,
                strategy="无法装箱",
                rank=1,
                label="方案1",
                order_type=cold.order_type if cold is not None else "",
                temperature=cold.temp_range if cold is not None else (15.0, 25.0),
                reason="货品尺寸超过所有包装箱",
            )
        ]

    options: list[_Option] = []
    for carton in cartons:
        plans, leftover = pack_into(carton, packable, cfg, cold=cold)
        if not plans:
            continue
        options.append(
            _make_option(plans, leftover, f"单一箱型 {carton.display_name}", carton_code=carton.code)
        )

    # 「先装一箱 A，余量用 B 收尾」：最贴近现场操作，也能产出 1×XL + 1×L 这类实用组合
    for carton_a in cartons:
        for carton_b in cartons:
            if carton_a is carton_b:
                continue
            first_pool: list[list] = [[item, item.qty] for item in packable]
            first = pack_carton(carton_a, first_pool, cfg, index=0, cold=cold)
            if not first.placements:
                continue
            rest = [(item, qty) for item, qty in first_pool if qty > 0]
            if not rest:
                continue
            rest_items = [item.clone(qty) for item, qty in rest]
            plans_b, left_b = pack_into(carton_b, rest_items, cfg, cold=cold)
            if not plans_b:
                continue
            options.append(
                _make_option(
                    [first] + plans_b,
                    left_b,
                    f"1×{carton_a.code} + {carton_b.code} 收尾",
                )
            )

    # 两种箱型组合：A 装满后用 B 收尾（更贴近实际混装场景）
    pair_seen: set[tuple[str, str]] = set()
    for carton_a in cartons:
        for carton_b in cartons:
            if carton_a is carton_b:
                continue
            pair_key = (carton_a.code, carton_b.code)
            if pair_key in pair_seen:
                continue
            pair_seen.add(pair_key)
            plans_a, left_a = pack_into(carton_a, packable, cfg, cold=cold)
            if not plans_a or not left_a:
                continue
            rest = [item.clone(qty) for item, qty in left_a]
            plans_b, left_b = pack_into(carton_b, rest, cfg, cold=cold)
            if not plans_b:
                continue
            options.append(
                _make_option(
                    plans_a + plans_b,
                    left_b,
                    f"{carton_a.code} + {carton_b.code} 组合",
                )
            )

    # 单箱型装不全的，再尝试混合箱型补齐
    mixed: list[_Option] = []
    for opt in options:
        if not opt.leftover:
            continue
        plans, left = _complete_mixed(cartons, list(opt.plans), opt.leftover, cfg, cold=cold)
        if len(plans) > len(opt.plans):
            mixed.append(_make_option(plans, left, "混合箱型补齐"))
    options.extend(mixed)

    if not options:
        return []

    k = max(1, int(cfg.top_k or 3))

    # 先按目标排序，不够 K 个再用「填充率」「总重」补充，保证去重
    ordered: list[_Option] = []
    seen: set[tuple] = set()

    def push(candidates: Iterable[_Option]) -> None:
        for opt in candidates:
            if len(ordered) >= k:
                return
            if opt.signature in seen:
                continue
            seen.add(opt.signature)
            ordered.append(opt)

    # 备选方案要「像样」：箱数明显偏多的方案（> 最优箱数 + 2）排到最后
    best_count = min(o.carton_count for o in options)

    def sane(o: _Option) -> int:
        return 0 if o.carton_count <= best_count + 2 else 1

    push(sorted(options, key=lambda o: _rank(o, cfg)))
    if len(ordered) < k:
        push(sorted(options, key=lambda o: (sane(o), o.leftover_units, o.carton_count, o.total_volume)))
    if len(ordered) < k:
        push(sorted(options, key=lambda o: (sane(o), o.leftover_units, o.carton_count, -o.fill)))
    if len(ordered) < k:
        push(sorted(options, key=lambda o: (sane(o), o.leftover_units, o.total_weight, o.carton_count)))
    if len(ordered) < k:
        push(sorted(options, key=lambda o: (sane(o), o.carton_count)))

    return [
        _build_order_plan(order_no, items, oversize, opt, rank, cfg, cold)
        for rank, opt in enumerate(ordered[:k], start=1)
    ]
