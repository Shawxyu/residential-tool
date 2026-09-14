# -*- coding: utf-8 -*-
"""
本地 OSM 索引查询：把「查建筑 / 查道路」从联网请求变成本地数组运算。

数据来源：data/cache/local_{building,road}.parquet
（由 tools/build_local_index.py 从离线底图原始数据生成，含全市建筑与路网）

查询流程
--------
  1. 用所有地块的总包围盒，先在 float32 包围盒数组上做一次粗筛；
  2. 只把粗筛命中的 WKB 解码成几何（通常只有几百到几千条，不是 15 万条）；
  3. 与地块做空间连接，按 Parcel_ID 分组成 GeoDataFrame。

所以 120 个地块的建筑查询是毫秒级，而且完全不联网 —— 既不受 Overpass
限流影响，也不会因为请求被拆碎而「查不到建筑」。
"""

import os
import json
import threading

import numpy as np
import pandas as pd
import geopandas as gpd

BASE = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(os.path.dirname(BASE), "data", "cache")

# 索引名 -> 文件名
_NAMES = ("building", "road", "rail")

_LOCK = threading.Lock()
_MEMO: dict = {}          # name -> {"df": ..., "bbox": (w,s,e,n), "meta": {...}}
_MISS: set = set()        # 确认不存在的索引，别反复去磁盘找


def _path(name: str) -> str:
    return os.path.join(CACHE_DIR, f"local_{name}.parquet")


def _meta_path(name: str) -> str:
    return os.path.join(CACHE_DIR, f"local_{name}.json")


def available(name: str) -> bool:
    """索引文件是否就绪"""
    if name in _MEMO:
        return True
    if name in _MISS:
        return False
    ok = os.path.exists(_path(name))
    if not ok:
        _MISS.add(name)
    return ok


def meta(name: str):
    p = _meta_path(name)
    if not os.path.exists(p):
        return None
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return None


def _load(name: str):
    """首次查询时才把索引读进内存，之后常驻（21MB，读一次约 1 秒）"""
    if name in _MEMO:
        return _MEMO[name]
    with _LOCK:
        if name in _MEMO:
            return _MEMO[name]
        df = pd.read_parquet(_path(name))
        m = meta(name) or {}
        bbox = tuple(m.get("bbox") or df[["minx", "miny", "maxx", "maxy"]]
                     .agg(["min", "min", "max", "max"]).to_numpy().ravel()[[0, 1, 2, 3]])
        # bbox 顺序：total_bounds 给的是 (minx,miny,maxx,maxy)
        bbox = (float(df["minx"].min()), float(df["miny"].min()),
                float(df["maxx"].max()), float(df["maxy"].max()))
        _MEMO[name] = {"df": df, "bbox": bbox, "meta": m}
        return _MEMO[name]


def bbox(name: str):
    """索引覆盖范围 (w,s,e,n)；没有索引返回 None"""
    if not available(name):
        return None
    try:
        return _load(name)["bbox"]
    except Exception:  # noqa: BLE001
        return None


def covers(name: str, geom) -> bool:
    """geom 是否完整落在索引范围内（在范围内才可放心用本地数据）"""
    bb = bbox(name)
    if bb is None or geom is None or geom.is_empty:
        return False
    w, s, e, n = bb
    gx0, gy0, gx1, gy1 = geom.bounds
    return gx0 >= w and gy0 >= s and gx1 <= e and gy1 <= n


def query_bbox(name: str, bbox, landuse: str = ""):
    """
    按包围盒取整块要素（给「准备候选地块」用）。

    bbox    : (minx, miny, maxx, maxy)
    landuse : 传值就只保留该用地类型（例如 residential）
    返回 GeoDataFrame；索引不可用返回 None。
    """
    if not available(name):
        return None
    try:
        idx = _load(name)
    except Exception:  # noqa: BLE001
        return None

    df = idx["df"]
    w, s, e, n = bbox
    m = ((df["maxx"].to_numpy() >= w) & (df["minx"].to_numpy() <= e) &
         (df["maxy"].to_numpy() >= s) & (df["miny"].to_numpy() <= n))
    cand = df[m]
    if cand.empty:
        return gpd.GeoDataFrame(
            columns=["osm_id", "osm_type", "name", "landuse", "building",
                     "highway", "osm_levels", "osm_height", "geometry"],
            crs=4326)

    geoms = gpd.GeoSeries.from_wkb(pd.Series(cand["wkb"].to_numpy()), crs=4326)
    gdf = gpd.GeoDataFrame(
        {
            "osm_id": cand["osm_id"].to_numpy(),
            "osm_type": cand["osm_type"].to_numpy(),
            "name": cand["name"].to_numpy(),
            "landuse": cand["landuse"].to_numpy() if "landuse" in cand else "",
            "building": cand["building"].to_numpy() if "building" in cand else "",
            "highway": cand["highway"].to_numpy() if "highway" in cand else "",
            "osm_levels": cand["osm_levels"].to_numpy() if "osm_levels" in cand else "",
            "osm_height": cand["osm_height"].to_numpy() if "osm_height" in cand else "",
        },
        geometry=geoms.values, crs=4326,
    )
    if landuse:
        gdf = gdf[gdf["landuse"] == landuse].reset_index(drop=True)
    return gdf


def query(name: str, parcels: gpd.GeoDataFrame,
          id_col: str = "Parcel_ID", predicate: str = "intersects"):
    """
    在本地索引里查与各地块相交的要素。

    返回 (result, covered)
      result  : {Parcel_ID -> GeoDataFrame}（列与 OSM 线上取数完全一致）
      covered : 索引覆盖到的 Parcel_ID 集合；没覆盖的交给调用方走 Overpass
    """
    result, covered = {}, set()
    if not available(name) or parcels is None or parcels.empty:
        return result, covered

    try:
        idx = _load(name)
    except Exception:  # noqa: BLE001
        return result, covered

    df = idx["df"]
    w, s, e, n = idx["bbox"]

    # 只处理落在索引范围内的地块
    sub = parcels.copy()
    if sub.crs is not None and sub.crs.to_epsg() != 4326:
        sub = sub.to_crs(4326)
    bx = sub.bounds
    inside = ((bx["minx"] >= w) & (bx["miny"] >= s) &
              (bx["maxx"] <= e) & (bx["maxy"] <= n))
    sub = sub[inside]
    for pid in sub[id_col]:
        covered.add(pid)
    if sub.empty:
        return result, covered

    # ① 粗筛：先用总包围盒砍一刀
    m = ((df["maxx"].to_numpy() >= bx["minx"].min()) &
         (df["minx"].to_numpy() <= bx["maxx"].max()) &
         (df["maxy"].to_numpy() >= bx["miny"].min()) &
         (df["miny"].to_numpy() <= bx["maxy"].max()))
    cand = df[m]
    if cand.empty:
        return result, covered

    # ② 只对候选解码几何
    geoms = gpd.GeoSeries.from_wkb(pd.Series(cand["wkb"].to_numpy()),
                                   crs=4326)
    gdf = gpd.GeoDataFrame(
        {
            "osm_id": cand["osm_id"].to_numpy(),
            "osm_type": cand["osm_type"].to_numpy(),
            "name": cand["name"].to_numpy(),
            "landuse": cand["landuse"].to_numpy() if "landuse" in cand else "",
            "building": cand["building"].to_numpy() if "building" in cand else "",
            "highway": cand["highway"].to_numpy() if "highway" in cand else "",
            "osm_levels": cand["osm_levels"].to_numpy() if "osm_levels" in cand else "",
            "osm_height": cand["osm_height"].to_numpy() if "osm_height" in cand else "",
        },
        geometry=geoms.values, crs=4326,
    )

    # ③ 与地块做空间连接
    try:
        hit = gpd.sjoin(gdf, sub[[id_col, "geometry"]],
                        predicate=predicate, how="inner")
    except Exception:  # noqa: BLE001
        return result, covered

    for pid, grp in hit.groupby(id_col):
        result[pid] = gdf.loc[grp.index.unique()]
    return result, covered


def count(name: str) -> int:
    m = meta(name)
    return int(m.get("count", 0)) if m else 0


def summary() -> str:
    parts = []
    for nm in _NAMES:
        if available(nm):
            m = meta(nm) or {}
            parts.append(f"{nm} {m.get('count', '?')} 条")
        else:
            parts.append(f"{nm} 未建")
    return "；".join(parts)
