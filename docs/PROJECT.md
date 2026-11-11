# 包装预估项目文档

> 一句话：**把「包装箱列表 + 订单明细」两张 Excel，算成「每个订单用哪几种箱、每件货品放在第几层的哪个坐标」，并直接在网页里用 3D 看方案。**
>
> 本文说明：业务模型有哪些、算法怎么算、每段实现落在哪个文件哪一行、参考了哪些开源项目。

---

## 目录

1. [一分钟看懂](#1-一分钟看懂)
2. [业务流程与数据流](#2-业务流程与数据流)
3. [业务模型（数据模型）](#3-业务模型数据模型)
4. [核心算法](#4-核心算法)
5. [实现地图：每个功能在哪一行](#5-实现地图每个功能在哪一行)
6. [参考与使用的开源项目](#6-参考与使用的开源项目)
7. [接口与三维预览](#7-接口与三维预览)
8. [运行、测试与扩展](#8-运行测试与扩展)

---

## 1. 一分钟看懂

| 项目 | 内容 |
| --- | --- |
| 输入 | ① 包装箱列表 Excel（内长/内宽/内高/限重）<br>② 订单明细 Excel（订单号 + 货品编码 + 数量）<br>③ 货品档案（类型、直径或长宽高、单重） |
| 中间处理 | 订单按 SKU 合并 → 分层网格装箱 → 多箱型择优 |
| 输出 | 每个订单：用几个箱、每箱几层、每件货品的 `(x, y, z)` 坐标、填充率、总重 |
| 展示 | 网页里的 Three.js 三维视图，可旋转、缩放、逐层查看 |
| 技术栈 | Python 3.11 + FastAPI + openpyxl（后端）；Three.js（前端） |

**核心约束**（决定算法怎么设计）：

- 瓶装是**直立圆柱**，**永不旋转**；
- 层内一排紧贴一排（圆瓶用六边形交错最密）；
- **一层一层向上堆叠**，层高一致、层顶面平整；
- 盒装是长方体，允许水平转 90°（可关）。

---

## 2. 业务流程与数据流

```
 包装箱列表.xlsx ──┐
                   │  read_cartons()          ┌──────────────┐
                   ├─────────────────────────►│  List[Carton]│──┐
 订单明细.xlsx   ──┤                          └──────────────┘  │
 货品档案(sheet) ──┘  read_orders()           ┌──────────────┐  │
                          │  按订单号+SKU合并 │  List[Item]  │──┤
                          └──────────────────►└──────────────┘  │
                                                                ▼
                                                    optimize_order()
                                                    （逐箱、逐层、选箱）
                                                                │
                                                                ▼
                                                    OrderPlan → JSON
                                                                │
                                                                ▼
                                             浏览器 Three.js 三维渲染
```

对应代码：

- 读表 + 合并：`app/excel_io.py`
- 编排：`app/service.py` → `run_estimation()`
- 算法：`app/packer.py` → `optimize_order()`
- 接口：`app/main.py` → `POST /api/estimate`
- 渲染：`app/static/app.js` → `buildScene()`

---

## 3. 业务模型（数据模型）

模型全部是 `app/models.py` 里的 Python `dataclass`，字段即业务含义，序列化后就是前端 JSON。

### 3.1 `Carton`（包装箱）— `models.py:40`

| 字段 | 含义 | 单位 |
| --- | --- | --- |
| `code` / `name` | 箱型编码 / 名称 | — |
| `length` / `width` / `height` | **内**长、内宽、内高（可用空间） | mm |
| `max_weight` | 最大承重，不填为无限 | g |
| `price` | 单价（用于「成本最低」策略） | 元 |

关键方法：`volume`（体积）、`floor_area`（底面积）、`validate()`。

### 3.2 `Item`（货品）— `models.py:89`

| 字段 | 含义 | 说明 |
| --- | --- | --- |
| `kind` | `"bottle"` 瓶装 / `"box"` 盒装 | 决定排布方式 |
| `diameter` | 瓶装直径 | 瓶装必填 |
| `length` / `width` / `height` | 盒装长宽高；`height` 两种货品都必填 | mm |
| `qty` | 数量 | 已按订单合并 |
| `weight` | 单件重量 | g |

关键属性：

- `footprint` → 底面外接矩形。瓶装是 `(d, d)`，盒装是 `(长, 宽)`；
- `volume` → 真实体积。瓶装按圆柱 `π(d/2)²h`，盒装按长方体；**填充率用它算，不虚高**。

### 3.3 `PackConfig`（算法参数）— `models.py:168`

| 参数 | 默认 | 作用 |
| --- | --- | --- |
| `pattern` | `hex` | 瓶装层内排布：`hex` 六边形交错 / `square` 方形对齐 |
| `gap` | `0` | 物品安全间隙 mm，0 = 紧贴 |
| `rotate_box` | `True` | 盒装是否允许水平转 90°（瓶装永远不许转） |
| `respect_weight` | `True` | 是否遵守箱型限重 |
| `objective` | `cartons` | 选箱目标：箱数最少 / 容积最小 / 成本最低 |

### 3.4 `Placement`（一个已放物品）— `models.py:196`

坐标约定（右手系，箱内腔）：

```
z ↑ 高
  │
  │        ┌────────┐
  │       /        /│
  │      ┌────────┐ │
  │      │        │ │
  └──────┴────────┴─┴──► x 长
        y（宽，指向屏幕里）
```

- 所有物品记录**外接长方体左下后角** `(x, y, z)` 与 `(w, l, h)`；
- 瓶装额外记 `d`（直径），前端据此画圆柱，圆心 = `(x + d/2, y + d/2)`；
- `layer` 记录层号，`seq` 记录放置顺序。

### 3.5 `LayerInfo` / `CartonPlan` / `OrderPlan`

| 模型 | 位置 | 内容 |
| --- | --- | --- |
| `LayerInfo` | `models.py:248` | 第几层、起始高度、层高、件数、包含哪些 SKU |
| `CartonPlan` | `models.py:268` | 一个箱子的所有放置 + 层信息 + 重量 + 填充率 |
| `OrderPlan` | `models.py:301` | 一个订单的所有箱子 + 未装箱/超规货品 + 汇总指标 |

---

## 4. 核心算法

### 4.1 总体：分层网格装箱（Layered Grid Packing）

一句话：**每一层选一个主 SKU 铺满整层，剩下的缝隙塞更矮的货品，码完一层往上抬，直到装满一箱。**

```
optimize_order(items, cartons, cfg)
├── 1. 拆出「单件都放不下」的超规货品，单独报告
├── 2. 对每种箱型试装：pack_into(carton, items)
│       └── 反复 pack_carton()，装满一箱开下一箱
├── 3. 按目标打分，选最优箱型
└── 4. 若仍有剩余，用其他箱型补齐（混合箱型）

pack_carton(carton, pool)
├── z = 0
└── while 剩余高度够放至少一件:
        layer = plan_layer(carton, pool, z)
        z += layer.height
```

分段实现：

| 步骤 | 函数 | 位置 |
| --- | --- | --- |
| 单件能否放进箱 | `item_fits_carton` | `packer.py:440` |
| 装一个箱子 | `pack_carton` | `packer.py:350` |
| 同箱型重复装 | `pack_into` | `packer.py:412` |
| 规划一层 | `_plan_layer` | `packer.py:207` |
| 选箱 + 多箱 | `optimize_order` | `packer.py:504` |

### 4.2 层内排布：点阵（`_lattice`，`packer.py:48`）

#### ① 六边形交错（瓶装默认，最密）

设瓶径 `d`、间隙 `g`，节距 `p = d + g`：

- 行距：`p_y = p · √3 / 2 ≈ 0.866 p`
- 偶数排：圆心横坐标 `= d/2 + i·p`
- 奇数排：整体再右移 `p/2`

相邻两排圆心距：

```
√( (p/2)² + (p·√3/2)² ) = p = d + g
```

刚好相切，不重叠也不留空。相比方形直排，理论容量多 `2/√3 − 1 ≈ 15.5%`。

```
偶排  ○ ○ ○ ○ ○ ○
奇排    ○ ○ ○ ○ ○      ← 错开半个直径，嵌进凹槽
偶排  ○ ○ ○ ○ ○ ○
```

> 注意：六边形排布时**外接正方形会互相重叠**，但**实体圆柱不重叠**。
> 这也是为什么测试里要用「圆心距」判断，而不是简单比较外接矩形。

#### ② 方形对齐（可选）

行列都按 `边长 + g` 等距排列，严格「一排对一排」，便于码垛和贴标。

### 4.3 层内缝隙回填：MaxRects（`_Floor`，`packer.py:116`）

主 SKU 铺不满整层时（比如剩余高度只够铺一层小件），用 **MaxRects 最大矩形**算法把空隙塞满：

1. 维护一组**互不重叠的空闲矩形**；
2. 每放一件，用它的矩形去切分所有相交的空闲矩形（左/右/下/上四块）；
3. 删掉被其他空闲矩形完全包含的碎片；
4. 选位用 **Best-Short-Side-Fit**：在所有放得下的空闲矩形里，选「短边剩余最小」的，减少碎片。

```
放置前             放置后
┌──────────┐      ┌────┬─────┐
│          │  →   │新件│     │
│          │      ├────┴─────┤
└──────────┘      └──────────┘
```

**重要限制**：回填只允许「高度 ≤ 本层层高」的货品，这样层高不变、层顶面平整，
不会出现「这层一半高一半矮」导致上层悬空。

### 4.4 选箱策略（`optimize_order` 的 `rank`，`packer.py:558`）

对每种箱型都完整装一遍，拿到：用了几箱、有没有剩余、总填充率，然后排序：

| `objective` | 排序键（越小越优） |
| --- | --- |
| `cartons`（默认） | 有剩余？ → 剩余件数 → **箱数** → 填充率（取负） |
| `volume` | 有剩余？ → 剩余件数 → **总箱容积** → 箱数 |
| `cost` | 有剩余？ → 剩余件数 → **总成本** → 箱数 |

「剩余件数」永远排第一：先保证**装得下**，再谈省箱、省容积、省钱。

举个例子（示例数据 SO20241107001：36 个圆瓶 + 10 个方盒）：

- 较大箱 `CTN-XL` 一箱装完 → 1 箱、填充 34.6%；
- 中箱 `CTN-M` 要 4 箱 → 箱数多。

按「箱数最少」就选 `CTN-XL`；把目标改成「总容积最小」，程序会自动选 4 个中箱（总容积更小）。
**两种目标都实现了，可在页面上切换。**

### 4.5 混合箱型补齐（`_complete_mixed`，`packer.py:458`）

有些订单用单一箱型装不全（比如一件很高、一件很宽，没有哪种箱子同时满足），
这时把剩余货品拿去试其他箱型，能装多少装多少，循环到装完或再也装不下为止。

### 4.6 复杂度

| 层级 | 复杂度 | 说明 |
| --- | --- | --- |
| 单层 | `O(k·P + n²)` | `k`=候选 SKU 数，`P`=点阵点数，`n`=层内件数（MaxRects 切分） |
| 单箱 | `O((H / h_min) × 单层)` | 最多层数 × 每层开销 |
| 订单 | `O(箱型数 × 单箱)` | 对每种箱型各装一遍 |

示例数据（4 个订单、184 件、4 种箱型）实测约 **5 ms**（含 Excel 解析，10 次平均 4.8 ms）；算法**无随机数**，同一输入永远同一结果，方便人工复核和现场复现。

### 4.7 正确性怎么保证

`tests/test_packer.py` 里写了一套**独立于算法**的几何校验器，逐对检查：

| 校验 | 方法 |
| --- | --- |
| 是否出界 | 每件 `x+w ≤ 内长`、`y+l ≤ 内宽`、`z+h ≤ 内高` |
| 瓶装是否直立 | 外接矩形必须是正方形（`w = l = d`），高度等于瓶高 |
| 瓶—瓶 | 圆心距 ≥ `(d₁+d₂)/2` |
| 瓶—盒 | 圆心到矩形最近点的距离 ≥ `d/2` |
| 盒—盒 | 三轴投影不同时重叠 |

另有测试覆盖：六边形容量、层数递增、限重、超规货品、混合箱型、订单合并、单位解析。
当前 **13 个测试全部通过**。

---

## 5. 实现地图：每个功能在哪一行

### 5.1 后端

| 功能 | 文件 | 关键函数 / 类 | 行号 |
| --- | --- | --- | --- |
| 包装箱模型 | `app/models.py` | `Carton` | 40 |
| 货品模型 | `app/models.py` | `Item` | 89 |
| 算法参数 | `app/models.py` | `PackConfig` | 168 |
| 单件放置 | `app/models.py` | `Placement` | 196 |
| 单箱结果 | `app/models.py` | `CartonPlan` | 268 |
| 订单结果 | `app/models.py` | `OrderPlan` | 301 |
| 层内点阵（六边形/方形） | `app/packer.py` | `_lattice` | 48 |
| 候选底面（含盒装旋转） | `app/packer.py` | `_footprints` | 94 |
| MaxRects 空闲矩形管理 | `app/packer.py` | `_Floor` | 116 |
| 规划一层（主 SKU + 回填） | `app/packer.py` | `_plan_layer` | 207 |
| 装一个箱 | `app/packer.py` | `pack_carton` | 350 |
| 同箱型重复装 | `app/packer.py` | `pack_into` | 412 |
| 单件适配判断 | `app/packer.py` | `item_fits_carton` | 440 |
| 混合箱型补齐 | `app/packer.py` | `_complete_mixed` | 458 |
| 选箱 + 订单级编排 | `app/packer.py` | `optimize_order` | 504 |
| Excel 列名识别 | `app/excel_io.py` | `_map_columns` | 123 |
| 单位解析（mm/cm/kg…） | `app/excel_io.py` | `parse_number` | 76 |
| 读包装箱 | `app/excel_io.py` | `read_cartons` | 196 |
| 读订单 + **合并同 SKU** | `app/excel_io.py` | `read_orders` | 322 |
| 生成模板 | `app/excel_io.py` | `build_template` | 471 |
| 业务编排 | `app/service.py` | `run_estimation` | 14 |
| HTTP 接口 | `app/main.py` | `estimate` / `sample` / `template` | 82 / 109 / 125 |
| 命令行 | `app/cli.py` | `main` | — |

### 5.2 前端（Three.js）

| 功能 | 文件 | 函数 | 行号 |
| --- | --- | --- | --- |
| 场景/相机/灯光初始化 | `app/static/app.js` | — | 33~76 |
| 构建 3D 场景（箱体 + 圆柱 + 方块） | `app/static/app.js` | `buildScene` | 127 |
| 层过滤 + 层间展开 | `app/static/app.js` | `applyView` | 228 |
| 自动取景（把方案完整框进画面） | `app/static/app.js` | `fitCamera` | 245 |
| 悬浮提示 | `app/static/app.js` | pointermove 回调 | 302~325 |
| 订单列表 | `app/static/app.js` | `renderOrderList` | 341 |
| 统计面板 | `app/static/app.js` | `renderStats` | 399 |
| 图例 | `app/static/app.js` | `renderLegend` | 426 |
| 上传 + 预估请求 | `app/static/app.js` | `runEstimate` | 495 |

---

## 6. 参考与使用的开源项目

### 6.1 调研过、但**没有直接采用**的装箱算法

| 项目 | 它做什么 | 为什么本场景不用 |
| --- | --- | --- |
| [enzoruiz/3dbinpacking（py3dbp）](https://github.com/enzoruiz/3dbinpacking) · [PyPI](https://pypi.org/project/py3dbp/) | Python 三维装箱：长方体、6 向旋转、单箱限重、输出装下/装不下 | ① 只支持长方体，圆瓶只能按外接方柱近似，浪费 10%~15% 平面空间；② 默认会旋转物品，与「瓶装不旋转」冲突；③ 结果是「塞满」，不产生可复现的**分层**方案 |
| [dwave-examples/3d-bin-packing](https://github.com/dwave-examples/3d-bin-packing) | 用约束二次模型（CQM）求最少箱数 | 需要 D-Wave Leap 云账号和配额；不支持圆柱；无分层概念 |
| [DeepPack3D](https://www.sciencedirect.com/science/article/pii/S2665963824001209)（论文）| 深度强化学习 + 构造式启发式，在线装箱基准 | 研究向，需训练与调参；物品仍是可旋转长方体；无界面 |
| [jerry800416/3D-bin-packing](https://github.com/jerry800416/3D-bin-packing) | 基于 py3dbp 的改进版（重量分布等） | 同样是长方体 + 追求最小箱数，不满足直立/分层约束 |

**结论**：本场景的约束（圆柱直立 + 不旋转 + 成排 + 逐层）正好是通用装箱器的弱项。
通用算法追求「最大填充率」，堆出来的是人工无法复现的乱序结构；
仓库/打包真正需要的是**能按层执行**的方案。所以核心算法自研，
但保留了「纯盒装、允许旋转」时接入 py3dbp 做对照的空间。

### 6.2 实际使用的开源库

| 库 | 用途 | 链接 |
| --- | --- | --- |
| **Three.js** | 浏览器 3D 渲染（圆柱、长方体、线框、轨道控制器） | https://github.com/mrdoob/three.js |
| **FastAPI** | HTTP 接口、文件上传 | https://github.com/fastapi/fastapi |
| **openpyxl** | 读写 Excel（.xlsx） | https://github.com/tonybaloney/openpyxl |
| **Uvicorn** | ASGI 服务器 | https://github.com/encode/uvicorn |
| **pytest** | 单元测试 | https://github.com/pytest-dev/pytest |

> 说明：MaxRects 是经典的二维矩形装箱启发式算法（Jukka Jylänki 的 survey 有系统整理），
> 本项目在 `_Floor` 里实现了它的「空闲矩形切分 + Best-Short-Side-Fit 选位」核心部分。
> 参考阅读：<https://github.com/juj/RectangleBinPack>

---

## 7. 接口与三维预览

### 7.1 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/` | 3D 预览页 |
| POST | `/api/estimate` | 上传两个 Excel（字段名 `boxes`、`orders`；可选 `config` JSON）→ 返回全部订单方案 |
| GET | `/api/sample` | 用 `data/` 下的示例数据跑一次 |
| GET | `/api/template/cartons` | 下载包装箱模板 |
| GET | `/api/template/orders` | 下载订单模板 |
| GET | `/api/health` | 健康检查 |

`config` 示例：

```json
{ "pattern": "hex", "gap": 0, "rotate_box": true, "respect_weight": true, "objective": "cartons" }
```

### 7.2 返回结构（节选）

```jsonc
{
  "summary": { "order_count": 4, "carton_count": 5, "fill_rate": 0.3905 },
  "orders": [{
    "order_no": "SO20241107001",
    "carton_count": 1,
    "cartons": [{
      "carton": { "code": "CTN-XL", "length": 600, "width": 500, "height": 450 },
      "layers": [{ "index": 0, "z": 0, "height": 220, "count": 36, "skus": ["BTL-500"] }],
      "placements": [{
        "sku": "BTL-500", "kind": "bottle",
        "x": 0, "y": 0, "z": 0, "w": 65, "l": 65, "h": 220, "d": 65,
        "cx": 32.5, "cy": 32.5, "layer": 0
      }]
    }]
  }]
}
```

### 7.3 三维视图怎么做的

1. **坐标映射**：装箱坐标 `(x, y, z)` → Three.js `(x, z, y)`（Three.js 的 Y 轴朝上）；
2. **瓶装**用 `CylinderGeometry(d/2, d/2, h)`，圆柱默认沿 Y 轴，天然就是直立；
3. **盒装**用 `BoxGeometry(w, h, l)`；
4. 每个箱子一个 `Group`，沿 X 轴排开，箱体用线框 + 低透明度面表示；
5. **层滑块**：隐藏 `layer > N` 的网格；**层间展开**：把每件物品沿 Y 轴抬高 `layer × 展开值`；
6. **自动取景**：把包围盒 8 个角点投影到相机坐标系，分别按水平/垂直视场角算所需距离，取大者。

---

## 8. 运行、测试与扩展

### 8.1 运行

```bash
cd /Users/paul/PycharmProjects/packages
pip install -r requirements.txt

# 生成示例 Excel（data/boxes.xlsx、data/orders.xlsx）
python scripts/make_sample_data.py

# 启动
python -m uvicorn app.main:app --reload --port 8010
# 打开 http://127.0.0.1:8010 ，点「加载示例数据」

# 命令行批处理
python -m app.cli --boxes data/boxes.xlsx --orders data/orders.xlsx --out plan.json

# 测试
python -m pytest tests/ -q
```

### 8.2 Excel 列名对照

| 逻辑字段 | 支持的中文/英文列名 |
| --- | --- |
| 箱型编码 | 箱型编码、包装箱编码、箱号、箱型、型号、code |
| 内长/内宽/内高 | 内长、长度、长、length；内宽、宽、width；内高、高、height |
| 最大承重 | 最大承重、承重、限重、max_weight |
| 订单号 | 订单号、订单编号、单号、order_no |
| 货品编码 | 货品编码、商品编码、货号、sku |
| 类型 | 类型、品类、包装类型（瓶装/盒装，也支持 bottle/box） |
| 直径 | 直径、瓶径、外径、diameter |
| 数量 / 单重 | 数量、件数、qty；单重、重量、weight |

### 8.3 已知限制

| 限制 | 影响 | 扩展方向 |
| --- | --- | --- |
| 未建模承压/易碎 | 上层可以压任意货品 | `Item` 加 `fragile`、`max_stack`，在 `_plan_layer` 限制上层 SKU |
| 未建模重心 | 可能一头重 | 按层统计重量重心，超出安全区时换主 SKU |
| 层内混装有限 | 只在「更矮」时回填 | 换 3D MaxRects/Guillotine 或 OR-Tools CP-SAT |
| 全量返回坐标 | 订单很多时 JSON 较大 | 现已支持 `include_placements=false`（只出汇总）；后续可按订单懒加载 `/api/estimate/{order_no}` |
| 只支持矩形箱 | 异形箱/托盘不行 | 把箱内腔抽象成 3D 占据栅格 |

---

## 附：一句话记住算法

> **逐层选主货、点阵铺满、MaxRects 塞缝、码完抬升、装不下换箱、多箱型比箱数。**

---

## 9. v0.2 扩展：冷链（蓝冰 / 干冰）、承重过滤、Top3、Excel 导出

### 9.1 新增模块

| 模块 | 文件 | 作用 |
| --- | --- | --- |
| 冷媒规格 `CoolantSpec` | `app/coldchain.py` | 蓝冰 / 干冰的单块尺寸、重量、可维持温度、占比 |
| 冷链配置 `ColdChainConfig` | `app/coldchain.py` | 温度区间、货物/冷媒占比、占比基准、订单类型预设 |
| 用量计算 `coolant_requirements()` | `app/coldchain.py` | 按占比算每种冷媒需要几块（向上取整） |
| 订单类型预设 `PRESETS` | `app/coldchain.py` | 常温 / 冷藏 / 冷冻 / 深冷的内置温度与占比，可被前端覆盖 |
| 冷媒段装箱 | `app/packer.py: pack_carton()` | 货物先码、冷媒后码在顶部；空间不够时压低货物高度重排 |
| 承重预留与过滤 | `app/packer.py` | 货物可用承重 = (限重−自重)/(1+冷媒比)；超重方案降级 |
| Top-K 候选 | `app/packer.py: optimize_order()` | 单箱型、A+B 收尾、混合补齐，去重后取前 K 套 |
| Excel 导出 | `app/excel_io.py: export_with_recommendations()` | 在原订单表右侧追加「推荐装箱方案1/2/3」并附明细 sheet |

### 9.2 占比公式

```
冷媒需求量 = 货物量 × (冷媒占比 ÷ 货物占比)
冷媒块数   = ⌈需求量 ÷ 单块重量(或体积)⌉
```

### 9.3 新增/变更的接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/estimate` | 支持 `config.cold_chain`（温度、占比、冷媒规格、订单类型预设）与 `top_k` / `weight_filter` |
| POST | `/api/export` | 返回追加推荐列的 Excel |
| GET | `/api/sample/export` | 用示例数据导出推荐 Excel |

### 9.4 返回结构变化

- 每个订单新增 `candidates` 数组（方案1/2/3），顶层字段仍等于方案1，兼容原前端；
- 每套方案新增：`order_type`、`temperature`、`coolant_usage`、`coolant_weight`、`weight_ok`、`coolant_ok`、`reason`；
- 每个箱子新增：`goods_weight`、`coolant_weight`、`coolant_items`、`total_weight`、`weight_ok`、`coolant_ok`、`goods_count`、`coolant_count`。

### 9.5 测试

新增 10 个测试：占比用量、按体积基准、冷媒在货物上方、冷链关闭、Top-K 数量、承重过滤、Excel 导出列、订单类型读取与预设。当前 **23 个测试全部通过**。
