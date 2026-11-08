"""FastAPI 服务：上传 Excel → 装箱预估 → 返回 3D 方案数据。"""

from __future__ import annotations

import io
import json
from pathlib import Path

from urllib.parse import quote

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .excel_io import build_template
from .models import PackConfig
from .service import run_estimation

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
DATA_DIR = BASE_DIR.parent / "data"

app = FastAPI(
    title="包装预估 · 3D 装箱方案",
    description="瓶装/盒装混装、逐层堆叠的包装预估与 Three.js 三维预览",
    version="0.1.0",
)

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok"}


def _parse_config(raw: str | None) -> PackConfig:
    cfg = PackConfig()
    if not raw:
        return cfg
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail=f"config 不是合法 JSON：{exc}") from exc
    if not isinstance(data, dict):
        raise HTTPException(status_code=400, detail="config 必须是 JSON 对象")

    pattern = data.get("pattern")
    if pattern is not None:
        if pattern not in ("hex", "square"):
            raise HTTPException(status_code=400, detail="pattern 只能是 hex（六边形交错）或 square（方形对齐）")
        cfg.pattern = pattern

    if "gap" in data and data["gap"] is not None:
        try:
            gap = float(data["gap"])
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail="gap 必须是数字") from exc
        if gap < 0:
            raise HTTPException(status_code=400, detail="gap 不能为负数")
        cfg.gap = gap

    if "rotate_box" in data:
        cfg.rotate_box = bool(data["rotate_box"])
    if "respect_weight" in data:
        cfg.respect_weight = bool(data["respect_weight"])

    objective = data.get("objective")
    if objective is not None:
        if objective not in ("cartons", "volume", "cost"):
            raise HTTPException(status_code=400, detail="objective 只能是 cartons / volume / cost")
        cfg.objective = objective

    return cfg


@app.post("/api/estimate")
async def estimate(
    boxes: UploadFile = File(..., description="包装箱列表 xlsx"),
    orders: UploadFile = File(..., description="订单明细 xlsx"),
    config: str = Form("{}"),
    include_placements: bool = Form(True),
) -> dict:
    cfg = _parse_config(config)

    boxes_bytes = await boxes.read()
    orders_bytes = await orders.read()
    if not boxes_bytes or not orders_bytes:
        raise HTTPException(status_code=400, detail="上传的文件为空")

    try:
        return run_estimation(
            io.BytesIO(boxes_bytes),
            io.BytesIO(orders_bytes),
            cfg,
            include_placements=include_placements,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - 兜底，避免前端拿到 HTML 报错页
        raise HTTPException(status_code=500, detail=f"预估失败：{exc}") from exc


@app.get("/api/sample")
def sample() -> dict:
    """用内置示例数据跑一次预估（便于快速体验）。"""
    boxes_path = DATA_DIR / "boxes.xlsx"
    orders_path = DATA_DIR / "orders.xlsx"
    if not boxes_path.exists() or not orders_path.exists():
        raise HTTPException(
            status_code=404,
            detail="示例数据不存在，请先运行 python scripts/make_sample_data.py",
        )
    try:
        return run_estimation(boxes_path, orders_path, PackConfig())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/template/{kind}")
def template(kind: str) -> StreamingResponse:
    if kind not in ("cartons", "orders"):
        raise HTTPException(status_code=404, detail="模板类型只能是 cartons 或 orders")
    buffer = build_template(io.BytesIO(), kind)
    filename = "包装箱列表模板.xlsx" if kind == "cartons" else "订单明细模板.xlsx"
    headers = {"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"}
    return StreamingResponse(
        buffer,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers=headers,
    )
