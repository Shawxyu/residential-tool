# -*- coding: utf-8 -*-
"""
OSM 数据获取层
- 通过 Overpass API 分块下载，避免 504
- 下载结果落地为本地缓存（GeoJSON），下一次秒开
- 支持三种范围模式：全域 / 高密度掩膜 / 自定义（上传文件 或 勾选行政区）
"""
from __future__ import annotations

import os
import json
import time
import hashlib
import urllib.parse
import urllib.request
import urllib.error

import geopandas as gpd
import pandas as pd
from shapely.geometry import shape, Polygon, MultiPolygon, LineString
from shapely.ops import unary_union

from config import (
    CRS_WGS84, CRS_PROJECTED, CACHE_DIR,
    DEFAULT_MIN_AREA, DEFAULT_MAX_AREA,
    SHENZHEN_BBOX, CACHE_VERSION, PROXY,
)

OVERPASS_ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",          # 主力，国内可直连
    "https://overpass.kumi.systems/api/interpreter",    # 备用
    "https://overpass.private.coffee/api/interpreter",  # 备用
    "https://overpass.osm.ch/api/interpreter",          # 备用
]

UA = "ShenzhenResidentialTool/1.0 (academic research)"

# 若配置了本机代理（环境变量 或 data/proxy.txt），所有请求都走它
_OPENER = (urllib.request.build_opener(
    urllib.request.ProxyHandler({"http": PROXY, "https": PROXY}))
    if PROXY else None)


class NetworkUnavailable(RuntimeError):
    """Overpass 全部镜像都连不上（断网 / DNS 失败 / 代理失效）。

    和「Overpass 服务器繁忙」是两回事：
      - 繁忙：镜像能连通，只是 429/504 —— 换镜像重试有意义；
      - 不可达：连 TCP 都建立不了 —— 重试和退化成逐地块下载都是白等，
        必须立刻停下并提示用户去检查网络 / 配置代理。
    """


# 探活结果短时缓存，避免一个任务里反复探测
_PROBE_CACHE: dict = {"ts": 0.0, "ok": False, "detail": ""}


def _urlopen(req, timeout: int):
    """统一出口：有代理走代理，没有就直连"""
    if _OPENER is not None:
        return _OPENER.open(req, timeout=timeout)
    return urllib.request.urlopen(req, timeout=timeout)


def _emit(progress, msg: str, pct=None):
    """
    统一的进度回调。回调既可以是 progress(msg)，也可以是 progress(msg, pct)。
    """
    if not progress:
        return
    try:
        progress(msg, pct)
    except TypeError:
        try:
            progress(msg)
        except Exception:  # noqa: BLE001
            pass
    except Exception:  # noqa: BLE001
        pass


# =========================================================
# Overpass 基础请求
# =========================================================

def _overpass(query: str, timeout: int = 180, retries: int = 5,
              progress=None) -> dict:
    """
    向 Overpass 发请求，多镜像轮询 + 退避重试。

    实测结论（深圳家宽直连，无需代理）：
      - overpass-api.de 本身可直连，通常 2-3 秒返回
      - 失败绝大多数是 429（请求过频被限流）或 504（服务器过载），
        属于服务端繁忙，不是网络不通 —— 等几秒换个镜像重试即可
    所以这里对 429/504 用更长的退避，并轮换镜像。
    """
    last_err = None
    conn_fail = 0          # 连续「连接层失败」计数（区别于服务器返回错误码）
    for attempt in range(retries):
        ep = OVERPASS_ENDPOINTS[attempt % len(OVERPASS_ENDPOINTS)]
        name = ep.split("/")[2]
        try:
            data = urllib.parse.urlencode({"data": query}).encode()
            req = urllib.request.Request(ep, data=data, headers={"User-Agent": UA})
            with _urlopen(req, timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            last_err = e
            conn_fail = 0
            # 429 限流 / 504 过载：退避更久
            wait = 8 + attempt * 6 if e.code in (429, 504) else 3 + attempt * 3
            tip = {429: "请求过频被限流", 504: "服务器过载"}.get(e.code, f"HTTP {e.code}")
            _emit(progress, f"{name} {tip}，{wait}s 后换镜像重试（{attempt + 1}/{retries}）…")
            time.sleep(wait)
        except Exception as e:  # noqa: BLE001
            last_err = e
            conn_fail += 1
            # 已经逐个镜像都连不上 —— 这是断网，不是服务器繁忙，
            # 再退避重试只是让用户干等，直接停。
            if conn_fail >= len(OVERPASS_ENDPOINTS):
                break
            # 连接层异常（DNS 失败 / 连接被拒 / 超时）多半不是「重试就能好」，
            # 退避从 3/6/9 收到 1/2/3，避免断网时白白空转半分钟。
            wait = 1 + attempt
            _emit(progress, f"{name} 请求异常（{type(e).__name__}），{wait}s 后换镜像重试…")
            time.sleep(wait)
    raise RuntimeError(
        f"Overpass 请求失败（已重试 {retries} 次，换过多个镜像）。"
        f"通常是 Overpass 服务器繁忙，稍等几分钟再点一次即可。"
        f"最后错误：{last_err}"
    )


def probe_network(timeout: int = 8, max_age: float = 60.0) -> tuple:
    """
    快速探活：用一个极小查询依次试每个镜像，任一成功即认为网络可用。
    返回 (ok, detail)。detail 里带最后一个失败镜像和错误类型，便于提示。
    结果缓存 max_age 秒，避免一次任务内重复探测。

    用途：当「批量合并取数」全军覆没时，判断到底是 Overpass 繁忙，
    还是本机根本连不上 —— 后者不应再退化成逐地块重试。
    """
    now = time.time()
    if now - _PROBE_CACHE["ts"] < max_age:
        return _PROBE_CACHE["ok"], _PROBE_CACHE["detail"]

    q = '[out:json][timeout:10];node(22.54,114.05,22.541,114.051);out count;'
    detail = ""
    ok = False
    for ep in OVERPASS_ENDPOINTS:
        name = ep.split("/")[2]
        try:
            data = urllib.parse.urlencode({"data": q}).encode()
            req = urllib.request.Request(ep, data=data, headers={"User-Agent": UA})
            with _urlopen(req, timeout) as resp:
                resp.read()
            ok, detail = True, name
            break
        except Exception as e:  # noqa: BLE001
            detail = f"{name}（{type(e).__name__}）"

    _PROBE_CACHE.update({"ts": now, "ok": ok, "detail": detail})
    return ok, detail


def _save_cache(gdf: gpd.GeoDataFrame, cache_file: str, progress=None) -> bool:
    """
    写缓存。GeoJSON 不支持超过 2GB / 复杂嵌套类型，
    这里统一把非基础类型转成字符串，并逐级降级。
    """
    tmp = cache_file + ".tmp"
    try:
        out = gdf.copy()
        for col in out.columns:
            if col == out.geometry.name:
                continue
            if out[col].dtype == object:
                out[col] = out[col].astype(str)
        # 先写临时文件再改名，避免写一半的坏文件
        if os.path.exists(tmp):
            os.remove(tmp)
        out.to_file(tmp, driver="GeoJSON")
        if os.path.exists(cache_file):
            os.remove(cache_file)
        os.replace(tmp, cache_file)
        return True
    except Exception as e:  # noqa: BLE001
        _emit(progress, f"缓存写入失败（不影响本次使用）：{type(e).__name__}: {e}")
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass
        return False


# =========================================================
# Overpass 元素 -> GeoDataFrame
# =========================================================

# 建筑高度相关标签（按优先级取第一个非空值）
# 注意：列名不能用冒号，否则写 GeoJSON 缓存时部分驱动会报错，
# 所以统一转成 osm_levels / osm_height 这两个安全列名。
LEVEL_TAG_KEYS = ("building:levels", "building:levels:aboveground", "levels")
HEIGHT_TAG_KEYS = ("height", "building:height", "building:height:roof")


def _first_tag(tags: dict, keys) -> str:
    """按优先级取标签值，返回字符串；取不到返回空串（GeoJSON 友好）"""
    for k in keys:
        v = tags.get(k)
        if v is None:
            continue
        s = str(v).strip()
        if s:
            return s
    return ""


def _elements_to_gdf(elements) -> gpd.GeoDataFrame:
    """把 Overpass 的 way/relation geometry 转成 GeoDataFrame"""
    recs = []
    for el in elements:
        tags = el.get("tags", {}) or {}
        geom = None
        etype = el.get("type")

        if etype == "way" and el.get("geometry"):
            pts = [(p["lon"], p["lat"]) for p in el["geometry"] if p]
            # 道路类要素是线，其余按面处理
            if tags.get("highway") and len(pts) >= 2:
                try:
                    geom = LineString(pts)
                except Exception:
                    geom = None
            elif len(pts) >= 4:
                try:
                    geom = Polygon(pts)
                    if not geom.is_valid:
                        geom = geom.buffer(0)
                except Exception:
                    geom = None

        elif etype == "relation" and el.get("members"):
            # 尽量用 outer ring 拼多边形；拼不出来则跳过
            rings = []
            for m in el["members"]:
                if m.get("role") in ("outer", "") and m.get("geometry"):
                    pts = [(p["lon"], p["lat"]) for p in m["geometry"] if p]
                    if len(pts) >= 4:
                        rings.append(pts)
            if rings:
                try:
                    polys = [Polygon(r) for r in rings if len(r) >= 4]
                    polys = [p.buffer(0) if not p.is_valid else p for p in polys]
                    geom = unary_union(polys)
                except Exception:
                    geom = None

        if geom is None or geom.is_empty:
            continue

        recs.append({
            "osm_id": el.get("id"),
            "osm_type": etype,
            "name": tags.get("name", "") or tags.get("name:zh", "") or "",
            "landuse": tags.get("landuse", ""),
            "building": tags.get("building", ""),
            "highway": tags.get("highway", ""),
            # 高度信息：以前这里直接丢掉，导致层数 100% 走经验推断、容积率失真
            "osm_levels": _first_tag(tags, LEVEL_TAG_KEYS),
            "osm_height": _first_tag(tags, HEIGHT_TAG_KEYS),
            "geometry": geom,
        })

    if not recs:
        return gpd.GeoDataFrame(
            columns=["osm_id", "osm_type", "name", "landuse", "building",
                     "highway", "osm_levels", "osm_height", "geometry"],
            crs=CRS_WGS84,
        )
    return gpd.GeoDataFrame(recs, crs=CRS_WGS84)


# =========================================================
# 范围解析：三种模式
# =========================================================

def districts_boundary(district_names: list[str],
                      progress=None) -> gpd.GeoDataFrame:
    """按行政区名取边界（用于自由选区 + 地图聚焦）"""
    if not district_names:
        return gpd.GeoDataFrame(geometry=[], crs=CRS_WGS84)

    parts = "".join(
        f'rel["name"~"^{n}$"]["admin_level"~"6|7|8"](area.a);'
        for n in district_names
    )
    q = (
        '[out:json][timeout:120];'
        'area["name:en"="Shenzhen"]->.a;'
        f'({parts});out geom;'
    )
    d = _overpass(q, progress=progress)
    gdf = _elements_to_gdf(d.get("elements", []))
    if gdf.empty:
        # 退一步：不限 admin_level
        parts = "".join(f'rel["name"~"^{n}$"](area.a);' for n in district_names)
        q = ('[out:json][timeout:120];'
             'area["name:en"="Shenzhen"]->.a;'
             f'({parts});out geom;')
        d = _overpass(q, progress=progress)
        gdf = _elements_to_gdf(d.get("elements", []))
    if not gdf.empty:
        gdf = gdf.dissolve().reset_index(drop=True)[["geometry"]]
        gdf = gpd.GeoDataFrame(gdf, crs=CRS_WGS84)
    return gdf


def load_mask(path: str) -> gpd.GeoDataFrame:
    """读取高密度掩膜 GeoJSON"""
    gdf = gpd.read_file(path)
    if gdf.crs is None:
        gdf = gdf.set_crs(CRS_WGS84)
    elif str(gdf.crs).upper() != CRS_WGS84:
        gdf = gdf.to_crs(CRS_WGS84)
    return gdf


def _boundary_token(geom) -> str:
    """给几何范围生成稳定 hash，用于缓存命名"""
    b = []
    if geom is None or geom.is_empty:
        return "global"
    minx, miny, maxx, maxy = geom.bounds
    b = [round(minx, 6), round(miny, 6), round(maxx, 6), round(maxy, 6)]
    raw = json.dumps(b, sort_keys=True)
    return hashlib.md5(raw.encode()).hexdigest()[:16]


def _tile_bounds(geom, ncols: int = 4, nrows: int = 5):
    """把范围切成 ncols x nrows 的瓦片，降低单次 Overpass 压力"""
    minx, miny, maxx, maxy = geom.bounds
    dx = (maxx - minx) / ncols
    dy = (maxy - miny) / nrows
    tiles = []
    for i in range(ncols):
        for j in range(nrows):
            box = Polygon([
                (minx + i * dx, miny + j * dy),
                (minx + (i + 1) * dx, miny + j * dy),
                (minx + (i + 1) * dx, miny + (j + 1) * dy),
                (minx + i * dx, miny + (j + 1) * dy),
            ])
            if box.intersects(geom):
                tiles.append(box)
    return tiles


def _bbox_str(bbox) -> str:
    """shapely bounds (minx,miny,maxx,maxy) -> Overpass 'south,west,north,east'"""
    minx, miny, maxx, maxy = bbox
    return f"{miny},{minx},{maxy},{maxx}"


def _parcel_query(bbox, timeout: int = 300) -> str:
    """
    取范围内的居住地块（landuse=residential 面）。

    历史说明：早期版本还并取了「住宅类建筑」当作候选地块，但那个查询
    （building~apartments|residential|house|dormitory 全域）在深圳会直接
    504 / 长时间挂起 —— 这正是「全域住区案例一直加载不出来」的根因。
    建筑数据在指标计算阶段本来就会按地块重新下载，这里没必要取，
    去掉后取数时间从「几十分钟或失败」降到约 25 秒。
    """
    b = _bbox_str(bbox)
    return (
        f'[out:json][timeout:{timeout}];'
        f'(way["landuse"="residential"]({b});'
        f'relation["landuse"="residential"]({b}););'
        f'out geom;'
    )


def _tile_bounds_bbox(bbox, ncols: int = 5, nrows: int = 4):
    """把 bbox 切成网格，用于单次查询失败时的分块降级"""
    minx, miny, maxx, maxy = bbox
    dx = (maxx - minx) / ncols
    dy = (maxy - miny) / nrows
    out = []
    for i in range(ncols):
        for j in range(nrows):
            out.append((minx + i * dx, miny + j * dy,
                        minx + (i + 1) * dx, miny + (j + 1) * dy))
    return out


def _bbox_token(bbox) -> str:
    raw = json.dumps([round(float(v), 6) for v in bbox])
    return hashlib.md5(raw.encode()).hexdigest()[:16]


def fetch_residential(boundary_geom, use_cache: bool = True,
                      progress=None) -> gpd.GeoDataFrame:
    """
    下载 / 读取居住地块。

    策略（按实测优化）：
      1) 先命中本地缓存 —— 命中即秒开，完全不需要联网；
      2) 否则对范围 bbox 发一次 landuse=residential 查询
         （深圳全域实测 9669 个要素 / 25 秒 / 11.4 MB）；
      3) 单次失败才退化为 5x4 分块下载，逐块报告进度。

    注意：这里只计算面积、不做面积筛选（阈值交给界面调），
    所以用户改面积阈值时不需要重新下载。
    """
    if boundary_geom is not None and not getattr(boundary_geom, "is_empty", True):
        bb = tuple(boundary_geom.bounds)
        # 只要范围落在深圳框内，就统一用深圳框取数 ——
        # 这样全域 / 高密度 / 自选行政区共用同一份缓存，只需要下载一次
        zx0, zy0, zx1, zy1 = SHENZHEN_BBOX
        if bb[0] >= zx0 - 0.02 and bb[1] >= zy0 - 0.02 \
                and bb[2] <= zx1 + 0.02 and bb[3] <= zy1 + 0.02:
            bbox = tuple(SHENZHEN_BBOX)
        else:
            bbox = bb
    else:
        bbox = tuple(SHENZHEN_BBOX)

    token = _bbox_token(bbox)
    cache_file = os.path.join(CACHE_DIR,
                              f"parcels_{CACHE_VERSION}_{token}.geojson")

    # ⓿ 本地 landuse 索引优先：秒开、不联网，而且不会被限流截成残缺结果
    # （联网那次实测只取到 2891 个，本地快照里其实有 9739 个 residential）
    try:
        import localidx as _lidx
        if _lidx.available("landuse"):
            ibb = _lidx.bbox("landuse")
            if ibb and (bbox[0] >= ibb[0] and bbox[1] >= ibb[1] and
                        bbox[2] <= ibb[2] and bbox[3] <= ibb[3]):
                _emit(progress, "使用本地全市用地索引（不联网）…", 30)
                part = _lidx.query_bbox("landuse", bbox, landuse="residential")
                if part is not None and not part.empty:
                    _emit(progress, f"本地索引返回 {len(part)} 个居住地块", 80)
                    gdf = part.drop_duplicates(
                        subset=["osm_id", "osm_type"]).reset_index(drop=True)
                    if boundary_geom is not None and not getattr(
                            boundary_geom, "is_empty", True):
                        gdf = gdf[gdf.geometry.intersects(boundary_geom)]
                        gdf = gdf.reset_index(drop=True)
                    gdf = assign_ids_and_area(
                        gdf, min_area=1000.0, max_area=float("inf"),
                        sort=True, drop_wkt=True)
                    if use_cache and not gdf.empty:
                        _save_cache(gdf, cache_file, progress)
                    _emit(progress, f"取数完成：{len(gdf)} 个居住地块", 100)
                    return gdf
    except Exception:  # noqa: BLE001
        pass          # 索引不可用就照旧走联网

    if use_cache and os.path.exists(cache_file):
        _emit(progress, "命中本地缓存，直接加载（无需联网）", 60)
        try:
            gdf = gpd.read_file(cache_file)
            if gdf.crs is None:
                gdf = gdf.set_crs(CRS_WGS84)
            if not gdf.empty:
                _emit(progress, f"缓存载入完成：{len(gdf)} 个居住地块", 100)
                return gdf
        except Exception as e:  # noqa: BLE001
            _emit(progress, f"缓存读取失败（将重新下载）：{type(e).__name__}")

    _emit(progress, "正在向 OpenStreetMap 请求居住地块（单次查询）…", 8)
    part = None
    try:
        d = _overpass(_parcel_query(bbox), timeout=420, retries=4,
                      progress=progress)
        part = _elements_to_gdf(d.get("elements", []))
        _emit(progress, f"单次查询返回 {len(part)} 个要素", 55)
    except Exception as exc:  # noqa: BLE001
        _emit(progress, f"单次查询失败（{str(exc)[:60]}），改为分块下载…", 12)
        part = None

    if part is None or part.empty:
        tiles = _tile_bounds_bbox(bbox)
        parts = []
        total = len(tiles)
        for idx, tb in enumerate(tiles, 1):
            _emit(progress, f"分块下载 {idx}/{total}…", int(idx / total * 70))
            try:
                d = _overpass(_parcel_query(tb, timeout=180),
                              timeout=240, retries=3, progress=progress)
                p = _elements_to_gdf(d.get("elements", []))
                if not p.empty:
                    parts.append(p)
            except Exception as exc:  # noqa: BLE001
                _emit(progress, f"区块 {idx}/{total} 失败，已跳过：{str(exc)[:70]}")
            time.sleep(0.8)
        if not parts:
            return gpd.GeoDataFrame(
                columns=["osm_id", "osm_type", "name", "landuse", "building",
                         "geometry", "Parcel_ID", "area_m2"],
                crs=CRS_WGS84,
            )
        part = gpd.GeoDataFrame(pd.concat(parts, ignore_index=True), crs=CRS_WGS84)

    _emit(progress, "整理数据（去重 / 裁剪 / 计算面积）…", 80)
    gdf = part.drop_duplicates(subset=["osm_id", "osm_type"]).reset_index(drop=True)

    if boundary_geom is not None and not getattr(boundary_geom, "is_empty", True):
        gdf = gdf[gdf.geometry.intersects(boundary_geom)].reset_index(drop=True)

    # 缓存只设一个很宽松的下限（1000㎡），把碎小地块滤掉、控制缓存体积；
    # 真正的面积阈值由界面决定，因此用户改阈值不需要重新下载。
    gdf = assign_ids_and_area(gdf, min_area=1000.0, max_area=float("inf"),
                              sort=True, drop_wkt=True)

    if use_cache and not gdf.empty:
        _save_cache(gdf, cache_file, progress)

    _emit(progress, f"取数完成：{len(gdf)} 个居住地块", 100)
    return gdf


# =========================================================
# 编号 + 面积筛选
# =========================================================

def assign_ids_and_area(gdf: gpd.GeoDataFrame,
                        min_area: float = DEFAULT_MIN_AREA,
                        max_area: float = DEFAULT_MAX_AREA,
                        sort: bool = True,
                        drop_wkt: bool = False) -> gpd.GeoDataFrame:
    """
    面积计算、面积筛选、分配 Parcel_ID。

    sort      —— 按 osm_id 稳定排序后再编号，保证「同一次筛选」
                 无论先后顺序如何，ID 都对应同一个地块。
    drop_wkt  —— 写缓存时丢掉 geometry_wkt，能省掉近一半文件体积。
    """
    if gdf.empty:
        return gdf

    gdf = gdf[gdf.geometry.notna() & ~gdf.geometry.is_empty].copy()
    proj = gdf.to_crs(CRS_PROJECTED)
    gdf["area_m2"] = proj.geometry.area

    gdf = gdf[(gdf["area_m2"] >= min_area) & (gdf["area_m2"] <= max_area)].copy()

    if sort and "osm_id" in gdf.columns:
        cols = [c for c in ("osm_type", "osm_id") if c in gdf.columns]
        gdf = gdf.sort_values(cols, kind="stable")

    gdf = gdf.reset_index(drop=True)

    if not gdf.empty:
        gdf["Parcel_ID"] = range(1, len(gdf) + 1)
        if drop_wkt:
            gdf = gdf.drop(columns=[c for c in ["geometry_wkt"] if c in gdf.columns])
        else:
            gdf["geometry_wkt"] = gdf.geometry.to_wkt()
    return gdf


def filter_by_mask(gdf: gpd.GeoDataFrame, mask: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """筛选与高密度掩膜相交的地块"""
    if gdf.empty or mask is None or mask.empty:
        return gdf
    if mask.crs != gdf.crs:
        mask = mask.to_crs(gdf.crs)
    mask_union = unary_union(mask.geometry.values)
    out = gdf[gdf.geometry.intersects(mask_union)].copy()
    return out.reset_index(drop=True)
