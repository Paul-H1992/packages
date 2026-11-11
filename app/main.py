"""FastAPI 服务：上传 Excel → 装箱预估 → 返回 3D 方案数据 / 导出推荐 Excel。"""

from __future__ import annotations

import io
import json
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .coldchain import ColdChainConfig, default_cold_chain, parse_cold_chain
from .excel_io import build_template, export_with_recommendations
from .models import PackConfig
from .service import run_estimation

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
DATA_DIR = BASE_DIR.parent / "data"

app = FastAPI(
    title="包装预估 · 冷链 3D 装箱方案",
    description="瓶装/盒装混装、温度范围与蓝冰/干冰占比、逐层堆叠的包装预估与 Three.js 三维预览",
    version="0.2.0",
)

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok"}


def _load_payload(raw: str | None) -> dict:
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail=f"config 不是合法 JSON：{exc}") from exc
    if not isinstance(data, dict):
        raise HTTPException(status_code=400, detail="config 必须是 JSON 对象")
    return data


def _parse_config(raw: str | None) -> PackConfig:
    cfg = PackConfig()
    data = _load_payload(raw)
    if not data:
        return cfg

    pattern = data.get("pattern")
    if pattern is not None:
        if pattern not in ("hex", "square"):
            raise HTTPException(
                status_code=400,
                detail="pattern 只能是 hex（六边形交错）或 square（方形对齐）",
            )
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
    if "weight_filter" in data:
        cfg.weight_filter = bool(data["weight_filter"])

    objective = data.get("objective")
    if objective is not None:
        if objective not in ("cartons", "volume", "cost"):
            raise HTTPException(status_code=400, detail="objective 只能是 cartons / volume / cost")
        cfg.objective = objective

    top_k = data.get("top_k")
    if top_k is not None:
        try:
            value = int(top_k)
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail="top_k 必须是整数") from exc
        if value < 1:
            raise HTTPException(status_code=400, detail="top_k 必须大于 0")
        cfg.top_k = min(value, 10)

    return cfg


def _parse_cold(raw: str | None) -> ColdChainConfig:
    data = _load_payload(raw)
    cold_data = data.get("cold_chain")
    if not isinstance(cold_data, dict):
        return default_cold_chain()
    try:
        return parse_cold_chain(cold_data)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"冷链配置有误：{exc}") from exc


@app.post("/api/estimate")
async def estimate(
    boxes: UploadFile = File(..., description="包装箱列表 xlsx"),
    orders: UploadFile = File(..., description="订单明细 xlsx"),
    config: str = Form("{}"),
    include_placements: bool = Form(True),
) -> dict:
    cfg = _parse_config(config)
    cold = _parse_cold(config)

    boxes_bytes = await boxes.read()
    orders_bytes = await orders.read()
    if not boxes_bytes or not orders_bytes:
        raise HTTPException(status_code=400, detail="上传的文件为空")

    try:
        return run_estimation(
            io.BytesIO(boxes_bytes),
            io.BytesIO(orders_bytes),
            cfg,
            cold,
            include_placements=include_placements,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - 兜底，避免前端拿到 HTML 报错页
        raise HTTPException(status_code=500, detail=f"预估失败：{exc}") from exc


@app.post("/api/export")
async def export(
    boxes: UploadFile = File(..., description="包装箱列表 xlsx"),
    orders: UploadFile = File(..., description="订单明细 xlsx"),
    config: str = Form("{}"),
) -> StreamingResponse:
    """导出：在原订单表右侧追加「推荐装箱方案1/2/3」，并附方案明细表。"""
    cfg = _parse_config(config)
    cold = _parse_cold(config)

    boxes_bytes = await boxes.read()
    orders_bytes = await orders.read()
    if not boxes_bytes or not orders_bytes:
        raise HTTPException(status_code=400, detail="上传的文件为空")

    try:
        result = run_estimation(
            io.BytesIO(boxes_bytes),
            io.BytesIO(orders_bytes),
            cfg,
            cold,
            include_placements=False,
        )
        buffer = export_with_recommendations(io.BytesIO(orders_bytes), result)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"导出失败：{exc}") from exc

    filename = "订单装箱推荐方案.xlsx"
    headers = {"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"}
    return StreamingResponse(
        buffer,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers=headers,
    )


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
        return run_estimation(boxes_path, orders_path, PackConfig(), default_cold_chain(enabled=True))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/sample/export")
def sample_export() -> StreamingResponse:
    """用示例数据导出一份带推荐列的 Excel（页面「导出 Excel」在未上传文件时使用）。"""
    boxes_path = DATA_DIR / "boxes.xlsx"
    orders_path = DATA_DIR / "orders.xlsx"
    if not boxes_path.exists() or not orders_path.exists():
        raise HTTPException(
            status_code=404,
            detail="示例数据不存在，请先运行 python scripts/make_sample_data.py",
        )
    try:
        result = run_estimation(
            boxes_path,
            orders_path,
            PackConfig(),
            default_cold_chain(enabled=True),
            include_placements=False,
        )
        buffer = export_with_recommendations(orders_path, result)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    filename = "示例订单装箱推荐方案.xlsx"
    headers = {"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"}
    return StreamingResponse(
        buffer,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers=headers,
    )


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
