# -*- coding: utf-8 -*-
"""
深圳住区形态分析工具 —— 本地计算引擎

启动：python -m uvicorn app:app --port 8765
（或双击 start.bat）
"""
from __future__ import annotations

from typing import Optional, List, Dict, Any

import io
import os
import re
import json
import time
import zipfile
import traceback
import uuid
import functools

import numpy as np
import pandas as pd
import geopandas as gpd
from shapely.geometry import shape
from shapely.ops import unary_union

from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import config as C
import osm as OSM
import metrics as M
import plots as P
import analysis as A

app = FastAPI(title="深圳住区形态分析工具", version="1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=C.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.exception_handler(OSM.NetworkUnavailable)
async def _network_unavailable_handler(request, exc):
    """断网时统一返回 503 + 可读提示，而不是 500 堆栈"""
    return JSONResponse(status_code=503, content={"detail": str(exc)})


# =========================================================
# 会话状态（单用户本地工具，内存驻留）
# =========================================================
STATE: dict = {
    "parcels": None,        # GeoDataFrame 候选地块（含 Parcel_ID / area_m2）
    "mask": None,           # 高密度掩膜
    "boundary": None,       # 当前使用的范围几何
    "buildings": {},        # pid -> 建筑 GeoDataFrame 缓存
    "metrics_full": None,   # 全量指标表
    "metrics_checked": None,  # 用户确认后的指标表
    "plans": {},            # pid -> png 路径
    "cluster_result": None,
    "session_dir": None,
    "log": [],
    "busy": False,
    "progress": {"msg": "", "pct": None, "ts": 0},
    # FAR 口径："shrunk"（近似计容，×FLOOR_AREA_SHRINK，默认）
    #           | "geometric"（几何口径，Σ基底×层数 / 用地，未折减）
    # 只影响「FAR」这一列的取值；两列原始值（FAR / far_geometric）都常驻指标表，
    # 切换不需要重算。
    "far_mode": "shrunk",
}


def _apply_far_mode(df: pd.DataFrame) -> pd.DataFrame:
    """
    按 STATE["far_mode"] 调整 FAR / FAR_log 列。
    geometric 模式下 FAR = far_geometric（未折减），其余模式保持原值。
    """
    if df is None or df.empty:
        return df
    if STATE.get("far_mode") != "geometric":
        return df
    if "far_geometric" not in df.columns:
        return df
    d = df.copy()
    far = pd.to_numeric(d["far_geometric"], errors="coerce")
    d["FAR"] = far
    d["FAR_log"] = np.where(far > 0, np.log(far + 1), np.nan)
    return d


def _log(msg: str, pct=None):
    """
    写日志 + 同步进度。
    pct 为 None 时前端显示「不确定进度」的流动条；
    为 0-100 时前端显示真实百分比进度条。
    """
    ts = time.strftime("%H:%M:%S")
    STATE["log"].append(f"[{ts}] {msg}")
    if len(STATE["log"]) > 300:
        STATE["log"] = STATE["log"][-300:]
    STATE["progress"] = {"msg": msg, "pct": pct, "ts": time.time()}
    print(f"[{ts}] {msg}", flush=True)


def _task_begin(title: str):
    """标记长任务开始（前端据此显示进度条）"""
    STATE["busy"] = True
    STATE["progress"] = {"msg": title, "pct": 0, "ts": time.time()}


def _task_end(msg: str = "完成"):
    STATE["busy"] = False
    STATE["progress"] = {"msg": msg, "pct": 100, "ts": time.time()}


def timed_task(title: str):
    """
    给长任务加进度包装：进入置 0%，正常/异常退出都置 100%。
    这样前端永远不会卡在「转圈但没有任何反馈」的状态。
    """
    def deco(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            _task_begin(title)
            try:
                return fn(*args, **kwargs)
            finally:
                _task_end()
        return wrapper
    return deco


def _session_dir() -> str:
    if not STATE["session_dir"]:
        sid = time.strftime("%Y%m%d_%H%M%S")
        d = os.path.join(C.OUTPUT_DIR, f"session_{sid}")
        os.makedirs(d, exist_ok=True)
        STATE["session_dir"] = d
    return STATE["session_dir"]


def _df_records(df: pd.DataFrame, limit: int | None = None, decimals: int = 4):
    """DataFrame -> JSON 安全的 records（NaN -> None，numpy 标量 -> python）"""
    if df is None or df.empty:
        return []
    d = df.head(limit) if limit else df
    out = []
    for rec in d.to_dict(orient="records"):
        clean = {}
        for k, v in rec.items():
            try:
                if v is None or (isinstance(v, float) and pd.isna(v)):
                    clean[k] = None
                elif isinstance(v, (np.integer,)):
                    clean[k] = int(v)
                elif isinstance(v, (np.floating,)):
                    clean[k] = None if pd.isna(v) else round(float(v), decimals)
                elif isinstance(v, (np.bool_,)):
                    clean[k] = bool(v)
                elif isinstance(v, float):
                    clean[k] = round(v, decimals)
                else:
                    clean[k] = v
            except Exception:
                clean[k] = str(v)
        out.append(clean)
    return out


def _get_final_metrics() -> pd.DataFrame:
    """
    取当前生效的指标表：优先用户确认后的，其次全量表。
    注意：不能用 `a or b` —— DataFrame 的布尔求值会报
    「truth value of a DataFrame is ambiguous」。
    返回前统一应用 FAR 口径（_apply_far_mode），保证导出/聚类一致。
    """
    for key in ("metrics_checked", "metrics_full"):
        d = STATE.get(key)
        if isinstance(d, pd.DataFrame) and not d.empty:
            return _apply_far_mode(d)
    return pd.DataFrame()


def _attachment_headers(filename: str, ascii_fallback: str = "export") -> dict:
    """
    构造 Content-Disposition。

    HTTP 头必须是 latin-1 可编码的，中文文件名会直接抛
    UnicodeEncodeError。所以这里走 RFC 5987：
      - filename=  只放 ASCII 兜底名（老浏览器用）
      - filename*= UTF-8''<百分号编码>（现代浏览器用，能正确显示中文）
    """
    from urllib.parse import quote

    quoted = quote(filename, safe="")
    ext = ""
    if "." in ascii_fallback:
        ext = "." + ascii_fallback.rsplit(".", 1)[1]
    fallback = ascii_fallback.replace('"', "")
    return {
        "Content-Disposition": (
            f'attachment; filename="{fallback}"; filename*=UTF-8\'\'{quoted}'
        )
    }


def _csv_response(df: pd.DataFrame, filename: str):
    buf = io.BytesIO()
    buf.write("\ufeff".encode("utf-8"))
    df.to_csv(buf, index=False, encoding="utf-8")
    buf.seek(0)
    return StreamingResponse(
        buf, media_type="text/csv",
        headers=_attachment_headers(filename, "residential_metrics.csv"),
    )


# =========================================================
# 0. 健康检查 / 环境信息
# =========================================================

@app.get("/api/health")
def health():
    return {
        "ok": True,
        "districts": list(C.SHENZHEN_DISTRICTS.keys()),
        "default_mask": C.DEFAULT_MASK_PATH,
        "mask_exists": os.path.exists(C.DEFAULT_MASK_PATH),
        "has_parcels": STATE["parcels"] is not None,
        "has_metrics": STATE["metrics_full"] is not None,
        "output_dir": C.OUTPUT_DIR,
    }


@app.get("/api/log")
def get_log():
    return {"log": STATE["log"][-60:]}


# =========================================================
# 0.5 FAR 口径设置（几何 / 近似计容）
# =========================================================

FAR_MODE_LABELS = {
    "shrunk": "近似计容 FAR（×折减系数，默认）",
    "geometric": "几何 FAR（Σ基底×层数 / 用地，未折减）",
}


@app.get("/api/settings/far-mode")
def get_far_mode():
    return {
        "ok": True,
        "mode": STATE.get("far_mode", "shrunk"),
        "labels": FAR_MODE_LABELS,
        "shrink": float(getattr(C, "FLOOR_AREA_SHRINK", 1.0) or 1.0),
    }


@app.post("/api/settings/far-mode")
def set_far_mode(payload: dict):
    mode = str(payload.get("mode", "")).strip()
    if mode not in FAR_MODE_LABELS:
        raise HTTPException(400, f"未知口径：{mode}（可选：{list(FAR_MODE_LABELS)}）")
    old = STATE.get("far_mode", "shrunk")
    STATE["far_mode"] = mode
    if mode != old:
        # 口径变了，旧的聚类结果（含 FAR 特征）不再可比，作废让前端重跑
        STATE["cluster_result"] = None
        _log(f"FAR 口径切换：{FAR_MODE_LABELS[old]} → {FAR_MODE_LABELS[mode]}"
             f"（已作废旧的聚类结果，请重新执行聚类）")
    return {"ok": True, "mode": mode, "label": FAR_MODE_LABELS[mode]}


@app.get("/api/progress")
def get_progress():
    """
    长任务实时进度。前端遮罩层轮询这个接口刷新进度条。
    pct 为 null 表示进度未知（前端显示流动条）。
    """
    p = dict(STATE.get("progress") or {})
    return {
        "msg": p.get("msg", ""),
        "pct": p.get("pct"),
        "busy": bool(STATE.get("busy")),
        "log": STATE["log"][-40:],
    }


@app.get("/api/net/test")
def net_test():
    """
    网络自检：并发测试 4 个 Overpass 镜像的连通性。

    为什么并发：串行时每个不通的镜像都要等满 30s 超时，4 个就是 2 分钟，
    而用户恰恰是在「卡住了」的时候点这个按钮 —— 让他再等两分钟很糟糕。
    并发后总耗时 ≈ 最慢的那个（约 30s）。
    """
    import urllib.request
    import urllib.parse
    import urllib.error
    import concurrent.futures

    q = "[out:json][timeout:25];way[building](22.53,114.04,22.55,114.07);out count;"

    def probe(ep):
        host = ep.split("/")[2]
        t0 = time.time()
        item = {"host": host, "url": ep}
        try:
            data = urllib.parse.urlencode({"data": q}).encode()
            req = urllib.request.Request(
                ep, data=data,
                headers={"User-Agent": OSM.UA},
            )
            opener = OSM._OPENER
            resp = opener.open(req, timeout=30) if opener is not None \
                else urllib.request.urlopen(req, timeout=30)
            with resp:
                j = json.loads(resp.read().decode("utf-8"))
                n = int(j["elements"][0]["tags"]["ways"])
                item.update(ok=True, ways=n, seconds=round(time.time() - t0, 1),
                            note="正常" if n > 0 else "可达但返回空（该镜像数据不全）")
        except urllib.error.HTTPError as e:
            tip = {429: "请求过频被限流", 504: "服务器过载", 502: "网关错误"}
            item.update(ok=False, seconds=round(time.time() - t0, 1),
                        note=f"HTTP {e.code} {tip.get(e.code, '')}".strip())
        except Exception as e:  # noqa: BLE001
            item.update(ok=False, seconds=round(time.time() - t0, 1),
                        note=f"{type(e).__name__}: {str(e)[:60]}")
        return item

    with concurrent.futures.ThreadPoolExecutor(
            max_workers=len(OSM.OVERPASS_ENDPOINTS)) as ex:
        results = list(ex.map(probe, OSM.OVERPASS_ENDPOINTS))

    ok_n = sum(1 for r in results if r.get("ok"))
    if ok_n:
        advice = f"{ok_n}/{len(results)} 个镜像可用，网络正常，可以取数据。"
        if C.PROXY:
            advice += f"（当前走了本机代理 {C.PROXY}）"
        fastest = min((r for r in results if r.get("ok")),
                      key=lambda r: r.get("seconds", 1e9))
        if fastest.get("seconds", 0) > 15:
            advice += (f"注意：可用镜像 {fastest['host']} 响应较慢"
                       f"（{fastest['seconds']}s），取数会明显偏慢，"
                       f"可稍后重试或配置代理。")
    else:
        advice = ("所有镜像都连不上。建议依次排查："
                  "① 稍等几分钟重试（Overpass 经常整体繁忙）；"
                  "② 若你本机有 Clash/V2Ray 等代理，把代理地址写进 "
                  "data\\proxy.txt（例如 http://127.0.0.1:7897）后重启引擎；"
                  "③ 用已下载的本地缓存先跑（缓存命中完全不需要联网）。")
    _log(f"网络自检：{ok_n}/{len(results)} 个 Overpass 镜像可用")
    return {"ok": ok_n > 0, "available": ok_n, "total": len(results),
            "results": results, "advice": advice, "proxy": C.PROXY or ""}


# =========================================================
# 1. 范围准备：全域 / 高密度掩膜 / 自定义
# =========================================================

class ScopeRequest(BaseModel):
    mode: str                 # "global" | "density" | "custom"
    districts: List[str] = []
    mask_path: Optional[str] = None
    min_area: float = C.DEFAULT_MIN_AREA
    max_area: float = C.DEFAULT_MAX_AREA
    use_cache: bool = True


@app.post("/api/scope/prepare")
@timed_task("正在准备候选地块")
def prepare_scope(req: ScopeRequest):
    """
    准备候选地块。三种模式：
      global  —— 深圳全域
      density —— 高密度掩膜范围
      custom  —— 自定义（上传的边界文件 或 勾选的行政区）
    """
    try:
        mask = None
        boundary = None
        label = ""

        # --- 高密度掩膜 ---
        if req.mode in ("density", "custom") or True:
            mp = req.mask_path or C.DEFAULT_MASK_PATH
            if os.path.exists(mp):
                mask = OSM.load_mask(mp)
                _log(f"加载高密度掩膜：{len(mask)} 个要素")

        if req.mode == "global":
            boundary = None
            label = "深圳全域"
            _log("范围模式：深圳全域")

        elif req.mode == "density":
            if mask is None or mask.empty:
                raise HTTPException(400, "未找到高密度掩膜文件，请检查路径")
            boundary = unary_union(mask.geometry.values)
            label = "高密度住区"
            _log("范围模式：高密度掩膜")

        elif req.mode == "custom":
            if req.districts:
                db = OSM.districts_boundary(req.districts, progress=_log)
                if db.empty:
                    raise HTTPException(400, "未能获取所选行政区边界，请检查网络")
                boundary = unary_union(db.geometry.values)
                label = "自选行政区：" + "、".join(req.districts)
                _log(f"范围模式：自选行政区 {req.districts}")
            elif STATE.get("boundary") is not None:
                boundary = STATE["boundary"]
                label = "自定义上传边界"
                _log("范围模式：沿用已上传边界")
            else:
                raise HTTPException(400, "请先上传边界文件或勾选行政区")
        else:
            raise HTTPException(400, f"未知模式：{req.mode}")

        STATE["mask"] = mask

        # --- 下载 / 读取地块 ---
        _log("开始获取居住地块数据…")
        if req.mode == "global":
            # 全域按深圳整体取（分块），不做掩膜过滤
            gdf = OSM.fetch_residential(None, use_cache=req.use_cache,
                                        progress=_log)
        else:
            gdf = OSM.fetch_residential(boundary, use_cache=req.use_cache,
                                        progress=_log)

        if gdf is None or gdf.empty:
            raise HTTPException(500, "未获取到任何居住地块，请稍后重试")

        # 面积重筛（用户可在界面调）
        gdf = OSM.assign_ids_and_area(gdf, req.min_area, req.max_area)
        if gdf.empty:
            raise HTTPException(500, f"面积筛选后无结果（{req.min_area:,.0f}–{req.max_area:,.0f} ㎡）")

        # 高密度模式下再叠一层掩膜相交（保险）
        if req.mode == "density" and mask is not None and not mask.empty:
            gdf = OSM.filter_by_mask(gdf, mask)
            gdf = OSM.assign_ids_and_area(gdf, req.min_area, req.max_area)

        STATE["parcels"] = gdf
        STATE["boundary"] = boundary
        STATE["buildings"] = {}
        STATE["metrics_full"] = None

        _log(f"候选地块就绪：{len(gdf)} 个")

        # 返回轻量数据（不含 wkt，减小体积）
        lightweight = gdf[[c for c in ["Parcel_ID", "name", "area_m2"]
                           if c in gdf.columns]].copy()
        geojson = json.loads(gdf[["Parcel_ID", "name", "area_m2", "geometry"]]
                             .to_json(ensure_ascii=False))

        return {
            "ok": True,
            "label": label,
            "count": len(gdf),
            "parcels": _df_records(lightweight),
            "geojson": geojson,
            "mask_exists": mask is not None and not mask.empty,
            "mask_area_km2": round(
                gpd.GeoSeries([boundary], crs=C.CRS_WGS84)
                .to_crs(C.CRS_PROJECTED).area.iloc[0] / 1e6, 2
            ) if boundary is not None else None,
        }

    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        _log("范围准备失败：" + str(e))
        traceback.print_exc()
        raise HTTPException(500, f"范围准备失败：{e}")


@app.post("/api/scope/upload-boundary")
async def upload_boundary(file: UploadFile = File(...)):
    """上传自定义边界（GeoJSON / json / zip(shp)）"""
    try:
        raw = await file.read()
        name = (file.filename or "").lower()

        tmpdir = os.path.join(C.CACHE_DIR, "uploads")
        os.makedirs(tmpdir, exist_ok=True)
        fpath = os.path.join(tmpdir, f"boundary_{uuid.uuid4().hex[:8]}_{file.filename}")

        if name.endswith(".zip"):
            with zipfile.ZipFile(io.BytesIO(raw)) as z:
                z.extractall(tmpdir)
                shps = [os.path.join(tmpdir, f) for f in os.listdir(tmpdir)
                        if f.lower().endswith(".shp")]
                if not shps:
                    raise HTTPException(400, "压缩包内未找到 .shp 文件")
                gdf = gpd.read_file(shps[0])
        else:
            with open(fpath, "wb") as f:
                f.write(raw)
            gdf = gpd.read_file(fpath)

        if gdf.crs is None:
            gdf = gdf.set_crs(C.CRS_WGS84)
        elif str(gdf.crs).upper() != C.CRS_WGS84:
            gdf = gdf.to_crs(C.CRS_WGS84)

        geom = unary_union(gdf.geometry.values)
        STATE["boundary"] = geom
        _log(f"已接收自定义边界，要素 {len(gdf)} 个")

        return {
            "ok": True,
            "count": len(gdf),
            "bounds": list(geom.bounds),
            "geojson": json.loads(gdf[["geometry"]].to_json(ensure_ascii=False)),
        }
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        raise HTTPException(500, f"边界文件解析失败：{e}")


@app.get("/api/scope/mask")
def get_mask():
    """返回掩膜几何，用于前端叠加显示"""
    mask = STATE.get("mask")
    if mask is None or mask.empty:
        if os.path.exists(C.DEFAULT_MASK_PATH):
            mask = OSM.load_mask(C.DEFAULT_MASK_PATH)
            STATE["mask"] = mask
        else:
            return {"ok": True, "geojson": None}
    return {
        "ok": True,
        "geojson": json.loads(mask[["geometry"]].to_json(ensure_ascii=False)),
    }


# =========================================================
# 2. 经济技术指标计算（不落盘，不展示中间表）
# =========================================================

class ComputeRequest(BaseModel):
    clip_buildings: bool = True
    parcel_ids: Optional[List[int]] = None


@app.post("/api/metrics/compute")
@timed_task("正在计算经济技术指标")
def compute_metrics(req: ComputeRequest):
    """批量计算经济技术指标 + 异常标记"""
    parcels = STATE.get("parcels")
    if parcels is None or parcels.empty:
        raise HTTPException(400, "请先准备候选地块")

    target = parcels
    if req.parcel_ids:
        target = parcels[parcels["Parcel_ID"].isin(req.parcel_ids)]
        if target.empty:
            raise HTTPException(400, "所选地块不在当前候选范围内，请重新加载地块后再试")

    _log(f"开始计算指标，共 {len(target)} 个地块")
    try:
        df, cache, diag = M.compute_batch(target, clip_buildings=req.clip_buildings,
                                          progress=_log)
    except OSM.NetworkUnavailable as e:
        _log(f"网络不可用：{e}")
        raise HTTPException(503, str(e))

    if df.empty:
        # 把失败原因整理成人话，而不是笼统的「OSM 数据不足」
        reasons: Dict[str, int] = {}
        for f in diag.get("failed", []):
            reasons[f["reason"]] = reasons.get(f["reason"], 0) + 1
        detail = "；".join(f"{r}（{c} 个地块）" for r, c in
                          sorted(reasons.items(), key=lambda x: -x[1])[:3])
        raise HTTPException(
            400,
            f"{len(target)} 个地块都没算出指标。原因：{detail or '未知'}。"
            f"若为网络问题，可点右上角「网络自检」确认 Overpass 是否可达，"
            f"或稍等几分钟重试。"
        )

    STATE["metrics_full"] = df
    # 人工修正的基准快照（原表永远留在这里，供「指标表（人工修正）」比对）
    STATE["metrics_baseline"] = df.copy()
    STATE["metrics_overrides"] = {}
    STATE["buildings"] = {**STATE.get("buildings", {}), **cache}

    n_bad = int((df["anomaly_level"] == 2).sum())
    n_warn = int((df["anomaly_level"] == 1).sum())
    _log(f"指标完成：{len(df)}/{diag['total']} 条，严重异常 {n_bad}，警告 {n_warn}")

    return {
        "ok": True,
        "count": len(df),
        "requested": diag["total"],
        "failed": diag.get("failed", []),
        "anomaly_severe": n_bad,
        "anomaly_warn": n_warn,
        "columns": list(df.columns),
    }


@app.get("/api/metrics/table")
def metrics_table(source: str = "current"):
    """获取全量指标表（供检查用，中间表只在此处按需拉取）

    source="current"  返回随人工修正实时更新的工作表（metrics_full）；
    source="baseline" 返回自动计算后的原始表（metrics_baseline），只读对照用。
    """
    key = "metrics_baseline" if source == "baseline" else "metrics_full"
    df = STATE.get(key)
    if df is None or df.empty:
        raise HTTPException(400, "尚无指标数据")
    df = _apply_far_mode(df)
    return {"ok": True, "rows": _df_records(df), "count": len(df),
            "far_mode": STATE.get("far_mode", "shrunk")}


# ---------- 人工修正（Step 3 手动改数） ----------
EDITABLE_FIELDS = {"avg_levels": "平均层数", "max_levels": "最高层数", "FAR": "容积率"}


def _recalc_derived(row: pd.Series, base_row: pd.Series, field: str, value: float) -> dict:
    """
    按「层数 ↔ 容积率」的物理关系重算派生指标。

    口径（与 metrics.py 完全一致）：
        BCR               = 基底面积 / 地块面积
        avg_levels_w      = Σ(基底×层数) / 基底面积      （基底加权平均层数）
        raw_floor_area    = Σ(基底×层数) = 基底面积 × avg_levels_w
        FAR(近似计容)     = raw_floor_area × shrink / 地块面积
        far_geometric     = raw_floor_area / 地块面积    （未折减）
        building_area_sqm = raw_floor_area × shrink

    表里显示的是「简单平均层数」avg_levels，它和 FAR 之间差一个加权系数
    k = avg_levels_w / avg_levels（由基准行算出，保证修正前后口径一致）。
    """
    shrink = float(getattr(C, "FLOOR_AREA_SHRINK", 1.0) or 1.0)
    area = float(row.get("parcel_area_sqm") or 0)
    bcr = float(row.get("BCR") or 0)
    foot = bcr * area                       # 基底面积合计
    mode = STATE.get("far_mode", "shrunk")

    # 加权系数 k：修正前后保持一致，避免「改了平均层数，FAR 跳变」
    try:
        k = float(base_row.get("avg_levels_weighted")) / float(base_row.get("avg_levels"))
    except Exception:  # noqa: BLE001
        k = 1.0
    if not np.isfinite(k) or k <= 0:
        k = 1.0

    out: Dict[str, float] = {}
    if foot <= 0 or area <= 0:
        # 缺基底面积时无法按物理关系换算，只改用户直接编辑的那一列
        out[field] = value
        return out

    if field == "avg_levels":
        new_avg = float(value)
        new_avg_w = new_avg * k
        raw = foot * new_avg_w
        out["avg_levels"] = round(new_avg, 3)
        out["avg_levels_weighted"] = round(new_avg_w, 4)
        out["FAR"] = round(raw * shrink / area, 4)
        out["far_geometric"] = round(raw / area, 4)
        out["building_area_sqm"] = round(raw * shrink, 1)
    elif field == "FAR":
        v = float(value)
        if mode == "geometric":
            # 几何口径下用户改的是未折减值
            raw = v * area
            out["far_geometric"] = round(v, 4)
            out["FAR"] = round(v * shrink, 4)
        else:
            raw = v * area / shrink
            out["FAR"] = round(v, 4)
            out["far_geometric"] = round(raw / area, 4)
        out["building_area_sqm"] = round(raw * shrink, 1)
        new_avg_w = raw / foot
        out["avg_levels_weighted"] = round(new_avg_w, 4)
        out["avg_levels"] = round(new_avg_w / k, 3)
    elif field == "max_levels":
        out["max_levels"] = round(float(value), 2)
    else:
        out[field] = value
    return out


class AdjustIn(BaseModel):
    pid: int
    field: str
    value: float


@app.post("/api/metrics/adjust")
def adjust_metric(inp: AdjustIn):
    """手动修正某个地块的一个指标，并重算派生指标（原值永久保留在 baseline）"""
    df = STATE.get("metrics_full")
    base = STATE.get("metrics_baseline")
    if df is None or df.empty or base is None:
        raise HTTPException(400, "尚无指标数据")
    if inp.field not in EDITABLE_FIELDS:
        raise HTTPException(400, f"该列不支持修改，可改：{'、'.join(EDITABLE_FIELDS.values())}")
    try:
        value = float(inp.value)
    except Exception:  # noqa: BLE001
        raise HTTPException(400, "请填入有效数值")
    if not np.isfinite(value) or value < 0:
        raise HTTPException(400, "数值无效（需为 ≥ 0 的数字）")

    idx = df.index[df["Parcel_ID"] == inp.pid]
    if len(idx) == 0:
        raise HTTPException(404, "找不到该地块")
    i = idx[0]
    bidx = base.index[base["Parcel_ID"] == inp.pid]
    b = bidx[0] if len(bidx) else i

    before = {k: df.at[i, k] for k in df.columns}
    derived = _recalc_derived(df.loc[i], base.loc[b], inp.field, value)
    for k, v in derived.items():
        if k in df.columns:
            df.at[i, k] = v

    # 层数类被人工确认后：清掉「补全」标记，实测率记 1，并写明来源
    if inp.field in ("avg_levels", "max_levels"):
        src = str(df.at[i, "levels_source"]) if "levels_source" in df.columns else ""
        if "levels_source" in df.columns:
            df.at[i, "levels_source"] = f"人工修正（原：{src or '—'}）"
        if "levels_imputed" in df.columns:
            df.at[i, "levels_imputed"] = 0
        if "levels_coverage" in df.columns:
            df.at[i, "levels_coverage"] = 1.0

    # 用同一套体检规则重新打标（FAR 修正后「FAR异常高」会自然消失）
    try:
        re = M.detect_anomalies(df.loc[[i]].copy())
        for col in ("anomaly_flags", "anomaly_level", "anomaly_desc"):
            if col in re.columns and col in df.columns:
                df.at[i, col] = re.iloc[0][col]
    except Exception as e:  # noqa: BLE001
        _log(f"人工修正后重新体检失败（不影响改数）：{e}")

    ov = STATE.setdefault("metrics_overrides", {})
    rec = ov.get(int(inp.pid), {"pid": int(inp.pid), "changes": {}})
    for k, v in derived.items():
        # 原值一律取「最初算出来的值」，多次修改时对照表才不会串味
        old = base.at[b, k] if k in base.columns else before.get(k)
        if isinstance(old, (np.integer, np.floating)):
            old = float(old)
        old = None if (isinstance(old, float) and not np.isfinite(old)) else old
        rec["changes"][k] = {"old": old, "new": v}
    rec["name"] = df.at[i, "name"] if "name" in df.columns else None
    ov[int(inp.pid)] = rec

    # 已确认表（Step4 之后的下游）同步修正，保证后续筛选/聚类/导出用的是修正值
    chk = STATE.get("metrics_checked")
    if isinstance(chk, pd.DataFrame) and not chk.empty and "Parcel_ID" in chk.columns:
        ci = chk.index[chk["Parcel_ID"] == inp.pid]
        if len(ci):
            for k, v in derived.items():
                if k in chk.columns:
                    chk.at[ci[0], k] = v

    _log(f"人工修正：地块 {inp.pid} 的 {EDITABLE_FIELDS[inp.field]} → {value}")
    return {"ok": True, "pid": int(inp.pid),
            "row": _df_records(_apply_far_mode(df.loc[[i]]))[0],
            "changes": rec["changes"], "adjusted_count": len(ov)}


@app.get("/api/metrics/adjusted")
def list_adjusted():
    """列出所有人工修正过的地块（原值 / 新值对照），供「指标表（人工修正）」使用"""
    ov = STATE.get("metrics_overrides") or {}
    items = []
    for pid, rec in ov.items():
        items.append({"Parcel_ID": pid, "name": rec.get("name"),
                      "changes": rec.get("changes", {})})
    return {"ok": True, "items": items, "count": len(items)}


@app.delete("/api/metrics/adjust/{pid}")
def revert_adjust(pid: int):
    """撤销某个地块的人工修正，恢复原始计算值"""
    df = STATE.get("metrics_full")
    base = STATE.get("metrics_baseline")
    if df is None or base is None:
        raise HTTPException(400, "尚无指标数据")
    idx = df.index[df["Parcel_ID"] == pid]
    bidx = base.index[base["Parcel_ID"] == pid]
    if len(idx) == 0 or len(bidx) == 0:
        raise HTTPException(404, "找不到该地块")
    i, b = idx[0], bidx[0]
    for col in df.columns:
        if col in base.columns:
            df.at[i, col] = base.at[b, col]
    try:
        re = M.detect_anomalies(df.loc[[i]].copy())
        for col in ("anomaly_flags", "anomaly_level", "anomaly_desc"):
            if col in re.columns and col in df.columns:
                df.at[i, col] = re.iloc[0][col]
    except Exception:  # noqa: BLE001
        pass
    chk = STATE.get("metrics_checked")
    if isinstance(chk, pd.DataFrame) and not chk.empty and "Parcel_ID" in chk.columns:
        ci = chk.index[chk["Parcel_ID"] == pid]
        if len(ci):
            for col in chk.columns:
                if col in base.columns:
                    chk.at[ci[0], col] = base.at[b, col]
    STATE.get("metrics_overrides", {}).pop(int(pid), None)
    _log(f"撤销人工修正：地块 {pid}")
    return {"ok": True, "pid": int(pid),
            "row": _df_records(_apply_far_mode(df.loc[[i]]))[0]}


class FilterCondition(BaseModel):
    field: str
    op: str          # ">" "<" ">=" "<=" "==" "between"
    value: Optional[float] = None
    value2: Optional[float] = None

class FilterRequest(BaseModel):
    conditions: List[FilterCondition] = []
    logic: str = "and"       # "and" | "or"
    exclude_anomalies: bool = True
    max_anomaly_level: int = 1
    exclude_ids: List[int] = []
    source: str = "current"  # "current"(人工修正表) | "baseline"(原表)


@app.post("/api/metrics/filter")
def filter_metrics(req: FilterRequest):
    """
    多条件筛选。支持：单条件、多条件 AND/OR、
    区间 between、以及异常等级过滤与手动剔除。
    """
    key = "metrics_baseline" if req.source == "baseline" else "metrics_full"
    df = STATE.get(key)
    if df is None or df.empty:
        raise HTTPException(400, "尚无指标数据")

    d = _apply_far_mode(df).copy()
    mask = pd.Series(True, index=d.index)

    # 逐条件记录「这个条件之后还剩多少」，方便定位是哪一步把样本清零的
    cond_stats: List[dict] = []
    for cond in req.conditions:
        if cond.field not in d.columns:
            cond_stats.append({"field": cond.field, "op": cond.op,
                               "value": cond.value, "kept": None,
                               "note": "该字段不存在，已忽略"})
            continue
        col = pd.to_numeric(d[cond.field], errors="coerce")
        v = cond.value
        if v is None:
            continue
        if cond.op == ">":
            m = col > v
        elif cond.op == ">=":
            m = col >= v
        elif cond.op == "<":
            m = col < v
        elif cond.op == "<=":
            m = col <= v
        elif cond.op == "==":
            m = col == v
        elif cond.op == "between":
            v2 = cond.value2 if cond.value2 is not None else v
            lo, hi = min(v, v2), max(v, v2)
            m = (col >= lo) & (col <= hi)
        else:
            continue
        m = m.fillna(False)
        mask = (mask & m) if req.logic == "and" else (mask | m)
        cond_stats.append({
            "field": cond.field, "op": cond.op, "value": v,
            "value2": cond.value2,
            "kept": int(mask.sum()),
            # 该指标在全部案例里都是空值 —— 这种条件永远不会命中，
            # 是最容易让人「怎么筛都是 0」的坑，必须显式指出来
            "all_missing": bool(col.notna().sum() == 0),
        })

    d = d[mask]
    after_conditions = len(d)

    # 异常过滤
    anomaly_excluded = 0
    excluded_flags: Dict[str, int] = {}
    if req.exclude_anomalies:
        keep = d["anomaly_level"] <= req.max_anomaly_level
        dropped = d[~keep]
        anomaly_excluded = int(len(dropped))
        if anomaly_excluded:
            # 「已裁剪建筑」只是说明性标记，不代表数据有问题，不计入排除原因
            informational = {"已裁剪建筑"}
            for flags in dropped.get("anomaly_flags",
                                     pd.Series(dtype=str)).fillna(""):
                for fl in [x for x in str(flags).split(",") if x]:
                    if fl in informational:
                        continue
                    excluded_flags[fl] = excluded_flags.get(fl, 0) + 1
        d = d[keep]

    # 手动剔除
    removed_by_hand = 0
    if req.exclude_ids:
        before = len(d)
        d = d[~d["Parcel_ID"].isin(req.exclude_ids)]
        removed_by_hand = before - len(d)

    total = len(df)
    kept = len(d)
    _log(f"筛选：{total} -> {kept} 个地块")

    # ---- 结果为 0 时，把「到底是谁干掉的全部样本」讲清楚 ----
    hint = ""
    if kept == 0:
        if after_conditions == 0:
            bad = next((c for c in cond_stats if c.get("all_missing")), None)
            if bad:
                hint = (f"筛选条件里「{bad['field']}」在本次全部案例中都是空值"
                        f"（该指标没算出来或数据缺失），任何数值比较都不会命中。"
                        f"请改用一个有数据的指标（如 FAR / BCR / 建筑数量）再试。")
            else:
                worst = [c for c in cond_stats if c.get("kept") is not None]
                worst.sort(key=lambda c: c["kept"])
                if worst:
                    hint = (f"条件筛选后一个都不剩。第一个卡住的条件是"
                            f"「{worst[0]['field']} {worst[0]['op']} "
                            f"{worst[0]['value']}」，执行后剩 "
                            f"{worst[0]['kept']} 个。请放宽数值范围，"
                            f"或把「条件关系」从 AND 改成 OR。")
                else:
                    hint = "你的筛选条件把所有案例都排除了，请放宽数值范围。"
        elif anomaly_excluded > 0:
            top = sorted(excluded_flags.items(), key=lambda x: -x[1])[:3]
            detail = "、".join(f"「{k}」{v} 个" for k, v in top)
            lvl_txt = {0: "仅保留完全正常", 1: "排除严重异常"}.get(
                req.max_anomaly_level, "全部保留")
            hint = (f"你的条件其实筛出了 {after_conditions} 个，但随后"
                    f"「自动排除异常数据（当前：{lvl_txt}）」把它们全部排除了。"
                    f"主要原因：{detail}。"
                    f"把该项改成「全部保留（不排除）」，或取消勾选，即可看到结果。")
        elif removed_by_hand:
            hint = (f"剩下 {removed_by_hand} 个案例都被你在 Step 3 手动剔除了。"
                    f"点 Step 3 的「恢复全部案例」再试。")

    # 记录筛后结果，供下一步出图
    STATE["metrics_checked"] = d.reset_index(drop=True)

    return {
        "ok": True,
        "total": total,
        "kept": kept,
        "rows": _df_records(d),
        "ids": d["Parcel_ID"].tolist() if "Parcel_ID" in d.columns else [],
        "anomaly_severe": int((d["anomaly_level"] == 2).sum()) if kept else 0,
        "anomaly_warn": int((d["anomaly_level"] == 1).sum()) if kept else 0,
        # ---- 诊断信息（让「为什么变成 0」一眼可见）----
        "after_conditions": after_conditions,
        "anomaly_excluded": anomaly_excluded,
        "removed_by_hand": removed_by_hand,
        "excluded_flags": excluded_flags,
        "condition_stats": cond_stats,
        "hint": hint,
    }


@app.post("/api/metrics/confirm")
def confirm_metrics(payload: dict):
    """用户确认最终案例集合"""
    ids = payload.get("ids", [])
    df = STATE.get("metrics_full")
    if df is None or df.empty:
        raise HTTPException(400, "尚无指标数据")
    if ids:
        d = df[df["Parcel_ID"].isin(ids)].reset_index(drop=True)
    else:
        d = df.reset_index(drop=True)
    STATE["metrics_checked"] = d
    _log(f"用户确认 {len(d)} 个案例")
    return {"ok": True, "count": len(d)}


@app.get("/api/metrics/export")
def export_metrics():
    """导出经济技术指标表 CSV"""
    df = _get_final_metrics()
    if df is None or df.empty:
        raise HTTPException(400, "尚无指标数据")
    return _csv_response(df, "经济技术指标表.csv")


# =========================================================
# 3. 平面示意图
# =========================================================

class PlanRequest(BaseModel):
    clip_buildings: bool = True
    dpi: int = 300


@app.post("/api/plans/generate")
@timed_task("正在生成平面示意图")
def generate_plans(req: PlanRequest):
    """为已确认的案例批量生成平面示意图"""
    checked = STATE.get("metrics_checked")
    parcels = STATE.get("parcels")
    if checked is None or checked.empty:
        raise HTTPException(400, "请先确认案例集合")
    if parcels is None:
        raise HTTPException(400, "地块数据已丢失，请重新准备范围")

    ids = set(checked["Parcel_ID"].tolist())
    target = parcels[parcels["Parcel_ID"].isin(ids)]

    out_dir = os.path.join(_session_dir(), "plans")
    _log(f"开始绘制 {len(target)} 张平面图")
    made = P.draw_plans_batch(target, STATE.get("buildings", {}),
                              out_dir, dpi=req.dpi, progress=_log)

    STATE["plans"] = {pid: path for pid, path in made}
    _log(f"平面图完成：{len(made)} 张")

    return {
        "ok": True,
        "count": len(made),
        "missing": [i for i in ids if i not in STATE["plans"]],
        "items": [
            {"Parcel_ID": pid, "url": f"/api/plans/image/{pid}"}
            for pid, _ in made
        ],
    }


@app.get("/api/plans/image/{pid}")
def plan_image(pid: int):
    path = STATE.get("plans", {}).get(pid)
    if not path or not os.path.exists(path):
        raise HTTPException(404, "图片不存在")
    return FileResponse(path, media_type="image/png")


@app.post("/api/plans/drop/{pid}")
def drop_plan(pid: int):
    """人工筛除：删除该平面图，并把该地块从确认案例中一并移除。

    轮廓不清晰的示意图说明该地块的 OSM 建筑数据不可信，
    指标同样不可信，所以必须连案例一起删，防止它混进聚类。
    """
    plans = STATE.get("plans") or {}
    if pid not in plans:
        raise HTTPException(404, "该地块没有平面图")

    plans.pop(pid, None)

    d = STATE.get("metrics_checked")
    removed = False
    if isinstance(d, pd.DataFrame) and not d.empty:
        d2 = d[d["Parcel_ID"] != pid].reset_index(drop=True)
        removed = len(d2) != len(d)
        STATE["metrics_checked"] = d2

    # 案例集合变了，旧聚类结果不再可比
    if STATE.get("cluster_result") is not None:
        STATE["cluster_result"] = None
        _log("案例集合已变动，旧聚类结果已作废")

    _d = STATE.get("metrics_checked")
    _n = len(_d) if isinstance(_d, pd.DataFrame) and not _d.empty else 0
    _log(f"人工剔除案例 {pid}，剩余 {_n} 个")
    return {
        "ok": True,
        "removed_case": removed,
        "plans_left": len(plans),
        "cases_left": 0 if STATE.get("metrics_checked") is None else len(STATE["metrics_checked"]),
    }


@app.get("/api/plans/download-zip")
def download_plans_zip():
    """打包下载所有平面图 + 指标表"""
    plans = STATE.get("plans") or {}
    if not plans:
        raise HTTPException(400, "尚无平面图")

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for pid, path in plans.items():
            if os.path.exists(path):
                z.write(path, f"平面图/parcel_{pid}.png")

        df = _get_final_metrics()
        if df is not None and not df.empty:
            csv_bytes = ("\ufeff" + df.to_csv(index=False)).encode("utf-8")
            z.writestr("经济技术指标表.csv", csv_bytes)

            # 附带典型案例
            for pid in list(plans.keys())[:0]:
                pass

    buf.seek(0)
    fname = f"住区案例成果_{time.strftime('%Y%m%d_%H%M')}.zip"
    return StreamingResponse(
        buf, media_type="application/zip",
        headers=_attachment_headers(fname, "residential_cases.zip"),
    )


# =========================================================
# 4. 聚类分析
# =========================================================

class FeatureRequest(BaseModel):
    features: List[str]

class ElbowRequest(BaseModel):
    features: List[str]
    k_min: int = 2
    k_max: int = 11

class KMeansRequest(BaseModel):
    features: List[str]
    k: int


def _cluster_source() -> pd.DataFrame:
    df = _get_final_metrics()
    if df.empty:
        raise HTTPException(400, "请先完成指标计算与筛选")
    return df


@app.post("/api/cluster/elbow")
@timed_task("正在做肘部分析")
def cluster_elbow(req: ElbowRequest):
    df = _cluster_source()
    if len(df) < 5:
        raise HTTPException(
            400,
            f"样本仅 {len(df)} 个，样本过少无法做可靠聚类。"
            f"建议放宽筛选条件，至少保留 5 个以上案例。"
        )
    try:
        sse, feats = A.elbow(df, req.features, req.k_min, req.k_max)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if sse.empty:
        raise HTTPException(400, "肘部分析未产生结果，请检查指标选择与 K 范围")
    dropped = [f for f in req.features if f not in feats]
    _log(f"肘部分析完成，K 范围 {int(sse['K'].min())}-{int(sse['K'].max())}"
         + (f"；已自动剔除全缺失指标：{'、'.join(dropped)}" if dropped else ""))
    return {"ok": True, "sse": _df_records(sse), "features": feats,
            "dropped": dropped}


@app.post("/api/cluster/elbow-figure")
def elbow_figure(req: ElbowRequest):
    """生成肘部图（论文用 300dpi PNG）"""
    df = _cluster_source()
    sse, _ = A.elbow(df, req.features, req.k_min, req.k_max)
    out = os.path.join(_session_dir(), "elbow_curve.png")
    P.draw_elbow(sse, out)
    _log("肘部图已生成")
    return FileResponse(out, media_type="image/png")


@app.post("/api/cluster/run")
@timed_task("正在执行 KMeans 聚类")
def cluster_run(req: KMeansRequest):
    df = _cluster_source()
    if len(df) < 5:
        raise HTTPException(
            400,
            f"样本仅 {len(df)} 个，样本过少无法做可靠聚类。"
            f"建议放宽筛选条件，至少保留 5 个以上案例。"
        )
    if req.k < 2 or req.k > max(2, len(df) - 1):
        raise HTTPException(400, f"K 取值不合法（{req.k}），样本数 {len(df)}")

    res, centers, feats = A.run_kmeans(df, req.features, req.k)
    dropped = [f for f in req.features if f not in feats]
    if dropped:
        _log(f"提示：{'、'.join(dropped)} 在所有案例中都是缺失值，已自动剔除，"
             f"实际参与聚类的指标：{'、'.join(feats)}")
    STATE["cluster_result"] = {"df": res, "centers": centers, "features": feats, "k": req.k}

    _log(f"KMeans 完成：K={req.k}，{len(res)} 个样本")

    # 每个簇导出
    exports = os.path.join(_session_dir(), "clusters")
    os.makedirs(exports, exist_ok=True)
    cluster_files = []
    for c in sorted(res["cluster"].unique()):
        sub = res[res["cluster"] == c]
        fp = os.path.join(exports, f"cluster_{c}.csv")
        sub.to_csv(fp, index=False, encoding="utf-8-sig")
        cluster_files.append({"cluster": int(c), "count": len(sub),
                              "file": f"cluster_{c}.csv"})

    return {
        "ok": True,
        "k": req.k,
        "rows": _df_records(res),
        "centers": _df_records(centers),
        "features": feats,
        "cluster_files": cluster_files,
    }


@app.get("/api/cluster/centers")
def cluster_centers():
    r = STATE.get("cluster_result")
    if not r:
        raise HTTPException(400, "尚未执行聚类")
    return {
        "ok": True,
        "centers": _df_records(r["centers"]),
        "k": r["k"],
        "features": r["features"],
    }


@app.get("/api/cluster/cases/{cluster_id}")
def cluster_cases(cluster_id: int, top_n: int = 10):
    """某簇的典型案例（离聚类中心最近）"""
    r = STATE.get("cluster_result")
    if not r:
        raise HTTPException(400, "尚未执行聚类")
    full, top = A.cluster_cases(r["df"], r["features"], cluster_id, top_n)
    if top.empty:
        raise HTTPException(404, f"第 {cluster_id} 类无样本")
    return {
        "ok": True,
        "cluster": cluster_id,
        "count": len(full),
        "top": _df_records(top),
        "all_ids": full["Parcel_ID"].tolist() if "Parcel_ID" in full.columns else [],
        "left": {k: v for k, v in (
            ("mean_far", float(full["FAR"].mean()) if "FAR" in full else None),
            ("mean_bcr", float(full["BCR"].mean()) if "BCR" in full else None),
        )},
    }


class FigureRequest(BaseModel):
    features: List[str]
    dpi: int = 300


@app.post("/api/cluster/figure")
@timed_task("正在生成科研图")
def cluster_figure(req: FigureRequest):
    """生成 PCA + 箱线图（论文用）"""
    r = STATE.get("cluster_result")
    if not r:
        raise HTTPException(400, "尚未执行聚类")
    out = os.path.join(_session_dir(), "Cluster_Analysis.png")
    # 用聚类时实际生效的指标，避免「用户勾了但在所有案例里都缺失」的指标再次引发维度问题
    feats = [f for f in req.features if f in (r.get("features") or [])] or r["features"]
    try:
        P.draw_cluster_figure(r["df"], feats, out, dpi=req.dpi)
    except Exception as e:  # noqa: BLE001
        _log(f"科研图生成失败：{e}")
        raise HTTPException(400, f"科研图生成失败：{e}")
    _log(f"PCA + 箱线图已生成（指标：{'、'.join(feats)}）")
    return FileResponse(out, media_type="image/png")


@app.get("/api/cluster/export/{kind}")
def cluster_export(kind: str):
    """导出聚类结果表 / 中心表"""
    r = STATE.get("cluster_result")
    if not r:
        raise HTTPException(400, "尚未执行聚类")
    if kind in ("result", "results"):
        return _csv_response(r["df"], "聚类结果表.csv")
    if kind == "centers":
        return _csv_response(r["centers"], "聚类中心表.csv")
    if kind == "cases":
        rows = []
        for c in sorted(r["df"]["cluster"].unique()):
            _, top = A.cluster_cases(r["df"], r["features"], int(c), 5)
            rows.append(top)
        merged = pd.concat(rows, ignore_index=True) if rows else pd.DataFrame()
        return _csv_response(merged, "典型案例表.csv")
    raise HTTPException(404, "未知导出类型")


# =========================================================
# 5. 成果打包
# =========================================================

@app.get("/api/download-all")
def download_all():
    """一键打包：指标表 + 平面图 + 聚类结果 + 科研图"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        df = _get_final_metrics()
        if df is not None and not df.empty:
            z.writestr("经济技术指标表.csv",
                       ("\ufeff" + df.to_csv(index=False)).encode("utf-8"))

        for pid, path in (STATE.get("plans") or {}).items():
            if os.path.exists(path):
                z.write(path, f"平面图/parcel_{pid}.png")

        r = STATE.get("cluster_result")
        if r:
            z.writestr("聚类/聚类结果表.csv",
                       ("\ufeff" + r["df"].to_csv(index=False)).encode("utf-8"))
            z.writestr("聚类/聚类中心表.csv",
                       ("\ufeff" + r["centers"].to_csv(index=False)).encode("utf-8"))
            for c in sorted(r["df"]["cluster"].unique()):
                sub = r["df"][r["df"]["cluster"] == c]
                z.writestr(f"聚类/cluster_{c}.csv",
                           ("\ufeff" + sub.to_csv(index=False)).encode("utf-8"))

        for f in ("Cluster_Analysis.png", "elbow_curve.png"):
            fp = os.path.join(_session_dir(), f)
            if os.path.exists(fp):
                z.write(fp, f"科研图/{f}")

    buf.seek(0)
    fname = f"住区形态分析成果_{time.strftime('%Y%m%d_%H%M')}.zip"
    return StreamingResponse(
        buf, media_type="application/zip",
        headers=_attachment_headers(fname, "residential_all.zip"),
    )


# =========================================================
# 5.5 本地离线底图（PMTiles，支持 HTTP Range 增量读取）
# =========================================================
BASEMAP_DIR = os.path.join(C.DATA_DIR, "basemap")
BASEMAP_FILE = "shenzhen.pmtiles"
_ALLOWED_BASEMAPS = {BASEMAP_FILE}


@app.get("/api/basemap/info")
def basemap_info():
    """前端据此决定「本地矢量底图」是否可选，以及最大数据缩放级别"""
    path = os.path.join(BASEMAP_DIR, BASEMAP_FILE)
    ok = os.path.exists(path)
    info = {
        "ok": ok,
        "file": BASEMAP_FILE,
        "url": f"/basemap/{BASEMAP_FILE}" if ok else None,
        "size_mb": round(os.path.getsize(path) / 1e6, 2) if ok else 0,
        "min_zoom": None,
        "max_zoom": None,
    }
    if ok:
        # 从 pmtiles 头部读出真实缩放范围，前端据此设置 maxDataZoom
        try:
            from pmtiles.reader import Reader
            with open(path, "rb") as fh:
                def _read(offset, length):
                    fh.seek(offset)
                    return fh.read(length)

                hdr = Reader(_read).header()
            info["min_zoom"] = hdr.get("min_zoom")
            info["max_zoom"] = hdr.get("max_zoom")
        except Exception as e:                       # noqa: BLE001
            info["header_error"] = f"{type(e).__name__}: {e}"
    else:
        info["hint"] = ("本地底图尚未生成。运行 tools/fetch_osm_basemap.py 下载数据，"
                        "再运行 tools/build_pmtiles.py 生成 shenzhen.pmtiles。")
    return info


@app.api_route("/basemap/{fname}", methods=["GET", "HEAD"])
def serve_basemap(fname: str, request: Request):
    """
    提供 .pmtiles 文件。protomaps-leaflet 会用 HTTP Range 按需取瓦片，
    所以必须支持 206 Partial Content，否则单文件几百 MB 没法增量读。
    """
    if fname not in _ALLOWED_BASEMAPS:                   # 白名单，防目录穿越
        raise HTTPException(404, "未知的底图文件")
    path = os.path.join(BASEMAP_DIR, fname)
    if not os.path.exists(path):
        raise HTTPException(
            404, "本地底图尚未生成，请先运行 tools/fetch_osm_basemap.py 与 tools/build_pmtiles.py")

    size = os.path.getsize(path)
    base_headers = {
        "Accept-Ranges": "bytes",
        "Cache-Control": "public, max-age=86400",
    }
    rng = request.headers.get("range")

    if not rng:
        if request.method == "HEAD":
            return JSONResponse({}, headers={**base_headers, "Content-Length": str(size)})
        return FileResponse(path, media_type="application/octet-stream",
                            headers=base_headers)

    m = re.match(r"bytes=(\d*)-(\d*)", rng.strip())
    if not m:
        raise HTTPException(416, "Range 格式不正确")
    g1, g2 = m.group(1), m.group(2)
    if g1 == "" and g2 == "":
        raise HTTPException(416, "Range 为空")
    if g1 == "":                                          # bytes=-N  末尾 N 字节
        length = min(int(g2), size)
        start = size - length
        end = size - 1
    else:
        start = int(g1)
        end = int(g2) if g2 else size - 1
        end = min(end, size - 1)
    if start > end or start >= size:
        return JSONResponse(
            {"detail": "Range 超出文件范围"},
            status_code=416,
            headers={**base_headers, "Content-Range": f"bytes */{size}"},
        )
    length = end - start + 1

    def _iter_range():
        with open(path, "rb") as fh:
            fh.seek(start)
            remain = length
            while remain > 0:
                chunk = fh.read(min(256 * 1024, remain))
                if not chunk:
                    break
                remain -= len(chunk)
                yield chunk

    headers = {
        **base_headers,
        "Content-Range": f"bytes {start}-{end}/{size}",
        "Content-Length": str(length),
    }
    if request.method == "HEAD":
        return JSONResponse({}, status_code=206, headers=headers)
    return StreamingResponse(_iter_range(), status_code=206,
                             media_type="application/octet-stream", headers=headers)


# =========================================================
# 6. 前端静态文件（本地模式下直接由引擎托管）
# =========================================================
if os.path.isdir(C.WEB_DIR):
    app.mount("/", StaticFiles(directory=C.WEB_DIR, html=True), name="web")
