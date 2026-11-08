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
from dataclasses import dataclass
from typing import Iterable, Sequence

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


def pack_carton(
    carton: Carton,
    pool: list[list],
    cfg: PackConfig,
    index: int = 0,
) -> CartonPlan:
    """把 ``pool``（就地扣减）装进一个包装箱。"""
    placements: list[Placement] = []
    layer_infos: list[LayerInfo] = []
    z = 0.0
    layer_no = 0
    seq = 0
    weight_left = carton.max_weight if math.isfinite(carton.max_weight) else math.inf
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

    weight = sum(p.weight for p in placements)
    used_volume = sum(p.w * p.l * p.h if p.kind == "box" else math.pi * (p.d / 2) ** 2 * p.h for p in placements)
    used_height = max((p.top for p in placements), default=0.0)

    return CartonPlan(
        carton=carton,
        index=index,
        placements=placements,
        weight=weight,
        used_volume=used_volume,
        used_height=used_height,
        layers=layer_infos,
    )


def pack_into(
    carton: Carton,
    items: Iterable[Item],
    cfg: PackConfig,
) -> tuple[list[CartonPlan], list[tuple[Item, int]]]:
    """用同一种包装箱重复装箱，直到装不下或货品装完。"""
    pool: list[list] = [[item, item.qty] for item in items if item.qty > 0]
    plans: list[CartonPlan] = []
    guard = 0

    while any(q > 0 for _, q in pool):
        guard += 1
        if guard > 10000:
            break
        plan = pack_carton(carton, pool, cfg, index=len(plans))
        if not plan.placements:
            break
        plans.append(plan)

    leftover = [(item, qty) for item, qty in pool if qty > 0]
    return plans, leftover


# ---------------------------------------------------------------------------
# 订单级：选箱 + 多箱
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
            new_plans, _left = pack_into(carton, candidates, cfg)
            if not new_plans:
                continue
            packed: dict[str, int] = {}
            for plan in new_plans:
                for p in plan.placements:
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

    _reindex(plans)
    return plans, remaining


def optimize_order(
    order_no: str,
    items: Sequence[Item],
    cartons: Sequence[Carton],
    cfg: PackConfig,
) -> OrderPlan:
    """为单个订单挑选最优箱型并完成装箱。"""
    items = [item for item in items if item.qty > 0]
    packable: list[Item] = []
    oversize: list[Item] = []
    for item in items:
        if any(item_fits_carton(item, c, cfg) for c in cartons):
            packable.append(item)
        else:
            oversize.append(item)

    if not cartons or not packable:
        return OrderPlan(
            order_no=order_no,
            items=list(items),
            cartons=[],
            unplaced=[(item, item.qty) for item in packable],
            oversize=oversize,
            strategy="无可用箱型",
        )

    options = []
    for carton in cartons:
        plans, leftover = pack_into(carton, packable, cfg)
        if not plans:
            continue
        used = sum(p.used_volume for p in plans)
        capacity = sum(p.carton.volume for p in plans)
        options.append(
            {
                "carton": carton,
                "plans": plans,
                "leftover": leftover,
                "leftover_units": sum(q for _, q in leftover),
                "fill": used / capacity if capacity else 0.0,
            }
        )

    if not options:
        return OrderPlan(
            order_no=order_no,
            items=list(items),
            cartons=[],
            unplaced=[(item, item.qty) for item in packable],
            oversize=oversize,
            strategy="无法装箱",
        )

    # 排序键：先看是否有剩余，再按选箱目标排序
    def rank(opt: dict) -> tuple:
        has_left = 0 if not opt["leftover"] else 1
        count = len(opt["plans"])
        total_volume = sum(p.carton.volume for p in opt["plans"])
        prices = [p.carton.price for p in opt["plans"]]
        total_cost = sum(price for price in prices if price is not None)

        if cfg.objective == "volume":
            return (has_left, opt["leftover_units"], total_volume, count)
        if cfg.objective == "cost" and all(price is not None for price in prices):
            return (has_left, opt["leftover_units"], total_cost, count)
        # 默认：箱数最少，其次填充率最高
        return (has_left, opt["leftover_units"], count, -opt["fill"])

    best = min(options, key=rank)
    plans: list[CartonPlan] = best["plans"]
    leftover: list[tuple[Item, int]] = best["leftover"]
    strategy = f"单一箱型 {best['carton'].display_name}"

    if leftover:
        plans, leftover = _complete_mixed(cartons, plans, leftover, cfg)
        strategy = f"主箱型 {best['carton'].display_name} + 其他箱型补齐"

    _reindex(plans)
    return OrderPlan(
        order_no=order_no,
        items=list(items),
        cartons=plans,
        unplaced=leftover,
        oversize=oversize,
        strategy=strategy,
    )
