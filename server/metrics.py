# -*- coding: utf-8 -*-
"""
经济技术指标计算引擎
- 复刻并修正 原 stage2.3 的逻辑
- 新增：地块裁剪建筑（解决 BCR>1 的 OSM 边界问题）
- 新增：异常自动标记
"""
from __future__ import annotations

import math
import os
import time
import hashlib
from concurrent.futures import ThreadPoolExecutor, as_completed

import numpy as np
import pandas as pd
import geopandas as gpd
from shapely.geometry import Point

import config as C
from config import CRS_WGS84, CRS_PROJECTED
import osm as _OSM

# 建筑层数解析，沿用原脚本优先级
# 说明：osm_levels / osm_height 由 osm.py::_elements_to_gdf 从 OSM 原始标签里
# 提取（以前没提取，层数全是 NaN，容积率就只能靠经验值补全）。
# 后面几个 OSM 原始键名是兼容旧数据的兜底写法。
LEVEL_KEYS = ["osm_levels", "building:levels", "levels",
              "building:levels:aboveground"]
HEIGHT_KEYS = ["osm_height", "height", "building:height"]


def _to_pos_float(v) -> float:
    """把 '3' / '3.5' / '3;4' / '3,5' / '12 m' 之类解析成正数，失败返回 nan"""
    if v is None:
        return np.nan
    try:
        if pd.isna(v):
            return np.nan
    except (TypeError, ValueError):
        pass
    s = str(v).strip().lower().replace("m", "").replace(",", ".")
    s = s.split(";")[0].split("-")[0].strip()
    if not s:
        return np.nan
    try:
        f = float(s)
    except ValueError:
        return np.nan
    return f if f > 0 else np.nan


# 非住宅类建筑：商业裙楼 / 商场 / 厂房 / 停车楼等，通常层数很低
#: 明确的住宅类标签（用于判断 building 类型值在这一片是否可信）
RESIDENTIAL_TAGS = {
    "apartments", "apartment", "residential", "house", "detached",
    "semidetached_house", "terrace", "dormitory", "bungalow", "farm",
}

#: 明确的非住宅类标签
NON_RESIDENTIAL_TAGS = {
    "commercial", "retail", "office", "industrial", "warehouse",
    "supermarket", "kiosk", "service", "hotel", "hospital", "school",
    "kindergarten", "college", "university", "church", "temple",
    "train_station", "station", "parking", "shed", "garage", "garages",
    "roof", "hut", "cabin", "container", "ruins", "greenhouse",
    "sports_hall", "grandstand", "hangar", "bakehouse", "cowshed",
}


#: 经验层数曲线 —— 从本地 143 个「层数实测率 ≥85%、建筑数 ≥3」的地块上
#: 统计出来的（建筑密度 → 加权层数中位），不是拍脑袋定的：
#:
#:   建筑密度  0.13  0.16  0.20  0.24  0.28  0.32  0.39  0.47
#:   加权层数  32.4  30.0  24.4  19.2  14.6  15.0   9.6  10.1
#:   样本数     18    35    26    29    14    15     4     2
#:
#: 之前用的是「BCR≥0.3 给 7 层，否则 12 层」两档，在高层小区上会严重低估：
#: 信义·荔山公馆 建筑密度 0.172、9 栋高层塔楼，实测同密度住区中位是 28 层，
#: 旧规则只给 12 层，容积率算出来 1.75，而官方（占地 91,876.85㎡、
#: 建面 284,806㎡、12 栋高层）是 3.20 —— 差了近一倍。
_EMPIRICAL_BCR = (0.13, 0.16, 0.20, 0.24, 0.28, 0.32, 0.39, 0.47)
_EMPIRICAL_LV = (32.4, 30.0, 24.4, 19.2, 14.6, 15.0, 9.6, 10.1)

#: 同等建筑密度下，平均基底越小层数越低（城中村握手楼、老村 vs 塔楼）。
#: 实测：密度 0.18~0.26 区间，基底 300~600㎡ 的住区中位 18.9 层，
#: 600㎡ 以上 23.7 层 —— 约打 0.8 折。
_EMPIRICAL_MFP = (200.0, 300.0, 600.0, 1200.0)
_EMPIRICAL_MFP_FACTOR = (0.78, 0.80, 1.00, 1.02)


def _empirical_levels(bcr, mean_fp=None) -> float:
    """
    按本地实测样本统计的「建筑密度 → 典型层数」经验值，用于整栋无标签的地块。

    bcr      —— 该地块的建筑密度（footprint 合计 / 用地面积）
    mean_fp  —— 该地块建筑的平均基底面积（可选，用来区分握手楼和塔楼）
    """
    if bcr is None or not np.isfinite(bcr) or bcr <= 0:
        return 12.0
    lv = float(np.interp(bcr, _EMPIRICAL_BCR, _EMPIRICAL_LV))
    if mean_fp is not None and np.isfinite(mean_fp) and mean_fp > 0:
        lv *= float(np.interp(mean_fp, _EMPIRICAL_MFP, _EMPIRICAL_MFP_FACTOR))
    return float(np.clip(lv, 2.0, 45.0))


def _prior_levels(btype: str, footprint: float, bcr) -> float:
    """
    兜底经验层数：按「建筑类型 + 基底大小」给，不看旁边住宅塔楼。

    为什么必须分开：实测地块 2252 —— 14 栋住宅塔楼（786㎡/27层）+ 1 栋
    5349㎡ 的 commercial 没标层数。早期版本拿「同地块中位数 27 层」去补它，
    凭空多算 14 万㎡ 建筑面积，FAR 从 4.56 被抬到 6.78（虚高 49%）。
    商业裙楼实际只有 2~4 层。
    """
    t = str(btype or "").strip().lower()
    if t in NON_RESIDENTIAL_TAGS:
        return 3.0 if footprint >= 800 else 2.0
    if footprint >= 3000:          # 大基底的住宅也多半是低层板楼/配套
        return 6.0
    if pd.notna(bcr) and bcr >= 0.30:
        return 7.0
    return 12.0


def _impute_levels(b, lv, bcr_pre) -> pd.Series:
    """
    逐栋补全缺失层数。

    下面每条规则都对应一个真实踩过的坑，改之前请先读：

      · OSM 的 building 类型标签**经常标错**。科苑花园东区 10 栋住宅在
        OSM 里被整片标成 industrial、弘雅花园三期 17 栋全是 commercial。
        早期版本看到「非住宅」就给 2~3 层，FAR 从 3.30 直接崩到 0.59。
        → 只有当地块内确实存在住宅类建筑时，类型才可信。

      · 实测覆盖率太低时，少数观测值**不代表整体**。半岛城邦四期 51 栋里
        只有 4 栋标了 45 层，拿它去补其余 47 栋会把 FAR 抬高近 3 倍。
        → 覆盖率 < 30% 时，缺失栋一律走保守经验值，不用实测值外推。

      · 同一小区里**基底越大层数越低**（塔楼细高、裙楼矮胖）。半岛城邦一期
        那栋 1252㎡ 的低层配套（真实 2~3 层）曾被「基底最接近」补成 25 层，
        凭空多算 3.1 万㎡。
        → 基底超过观测中位基底 1.4 倍的，按 3 层算。
    """
    fill = pd.Series(np.nan, index=b.index, dtype="float64")
    n_all = len(b)
    if n_all == 0:
        return fill

    observed = lv.notna()
    n_obs = int(observed.sum())
    cov = n_obs / n_all
    miss = ~observed

    btype = (b["building"].astype(str).str.strip().str.lower()
             if "building" in b.columns
             else pd.Series("", index=b.index))

    # 类型标签是否可信：地块里至少有一栋明确的住宅类建筑
    has_res = bool(btype.isin(RESIDENTIAL_TAGS).any())

    obs_fp = b.loc[observed, "footprint"].to_numpy(dtype="float64")
    obs_lv = lv.loc[observed].to_numpy(dtype="float64")
    med_fp = float(np.median(obs_fp)) if n_obs else np.nan
    med_lv = float(np.median(obs_lv)) if n_obs else np.nan

    # 全体建筑的中位基底 —— 零标签地块（cov=0）判断「小品房」的唯一参照。
    # 不能用绝对阈值：深圳城中村的握手楼基底普遍只有 120~160㎡，
    # 一旦写死「<150㎡ 就是门卫房」，翻身小区 57 栋会整片被压成 2 层，
    # FAR 从 4.05 崩到 0.94。必须相对本地块自己的尺度来判。
    all_fp = b["footprint"].to_numpy(dtype="float64")
    med_all_fp = float(np.median(all_fp)) if n_all else np.nan
    mean_all_fp = float(np.mean(all_fp)) if n_all else np.nan

    # 覆盖率不足时的经验层数：走实测统计出来的曲线，不再用 7/12 两档
    emp = _empirical_levels(
        float(bcr_pre) if pd.notna(bcr_pre) else None, mean_all_fp
    )

    # 已实测的少数几栋是不是「高层塔楼」——用于识别裙楼/低层配套。
    # 半岛城邦四期 51 栋里只有 4 栋标了 33~50 层（塔楼，基底 766㎡），
    # 其余 47 栋基底仅 143㎡，是裙楼和低层配套。早期版本一律给经验值 7 层，
    # 多算约 4.7 万㎡，FAR 从 3.70 虚高到 4.80。
    tower_lv = float(np.median(obs_lv)) if (n_obs >= 2 and pd.notna(med_lv)) else np.nan
    tower_fp = float(np.median(obs_fp)) if n_obs >= 2 else np.nan
    is_tower_block = (
        n_obs >= 2
        and pd.notna(tower_lv) and tower_lv >= 15.0
        and pd.notna(tower_fp) and tower_fp > 0
    )

    for i in b.index[miss]:
        t = btype.at[i]
        fp = float(b.at[i, "footprint"])

        # ① 实测样本太少 → 不拿少数观测值推断整体，用保守经验值。
        #    只把「小品房」（门卫、垃圾房、配电房）单独降层，判定条件是
        #    「比本地块典型建筑小一个量级」而不是某个固定面积 ——
        #    城中村整片小基底不会被误伤（翻身小区、和丰小区、上三村）。
        if cov < 0.30:
            is_hut = (
                pd.notna(med_all_fp) and med_all_fp > 0
                and fp <= med_all_fp * 0.40
                and fp < 120.0
            )
            # 实测的是高层塔楼、而这栋的基底不到塔楼的一半 → 裙楼/低层配套
            is_podium = (
                is_tower_block
                and fp <= tower_fp * 0.50
            )
            if is_hut:
                fill.at[i] = 2.0
            elif is_podium:
                fill.at[i] = 3.0
            else:
                fill.at[i] = emp
            continue

        # ② 基底显著大于同地块典型塔楼 → 裙楼 / 配套，按低层算
        if pd.notna(med_fp) and med_fp > 0 and fp >= med_fp * 1.4:
            fill.at[i] = 3.0
            continue

        # ③ 基底显著偏小 → 配套小房
        if pd.notna(med_fp) and med_fp > 0 and fp <= med_fp * 0.35 and fp < 300:
            fill.at[i] = 2.0
            continue

        # ④ 类型可信且明确是非住宅 → 经验低层数
        if has_res and t in NON_RESIDENTIAL_TAGS:
            fill.at[i] = _prior_levels(t, fp, bcr_pre)
            continue

        # ⑤ 同类型实测中位数（至少 2 栋同类型，样本才有意义）
        same = observed & (btype == t)
        if int(same.sum()) >= 2:
            fill.at[i] = float(lv.loc[same].median())
            continue

        # ⑥ 退到全地块实测中位数
        if n_obs >= 2:
            fill.at[i] = med_lv
            continue

        # ⑦ 兜底
        fill.at[i] = emp

    return fill


def parse_levels(row) -> float:
    """提取建筑层数：优先 levels 标签，其次 height/3"""
    for k in LEVEL_KEYS:
        if k in row.index:
            v = _to_pos_float(row[k])
            if pd.notna(v):
                return v
    for k in HEIGHT_KEYS:
        if k in row.index:
            v = _to_pos_float(row[k])
            if pd.notna(v):
                lv = v / 3.0
                if lv > 0:
                    return lv
    return np.nan


def get_building_orientation(poly) -> float:
    """建筑主方向（0-90 度）"""
    try:
        rect = poly.minimum_rotated_rectangle
        coords = list(rect.exterior.coords)
        edges = []
        for i in range(4):
            p1, p2 = coords[i], coords[i + 1]
            dx, dy = p2[0] - p1[0], p2[1] - p1[1]
            edges.append((math.hypot(dx, dy), dx, dy))
        longest = max(edges, key=lambda x: x[0])
        angle = abs(math.degrees(math.atan2(longest[2], longest[1])))
        if angle > 180:
            angle -= 180
        if angle > 90:
            angle = 180 - angle
        return angle
    except Exception:
        return np.nan


def get_elongation(poly) -> float:
    """建筑长宽比（长边/短边）"""
    try:
        rect = poly.minimum_rotated_rectangle
        coords = list(rect.exterior.coords)
        lengths = sorted(
            Point(coords[i]).distance(Point(coords[i + 1])) for i in range(4)
        )
        short_edge, long_edge = lengths[0], lengths[-1]
        if short_edge <= 0:
            return np.nan
        return long_edge / short_edge
    except Exception:
        return np.nan


def nearest_neighbor_metrics(buildings: gpd.GeoDataFrame):
    """最近邻距离均值/标准差"""
    try:
        if len(buildings) < 2:
            return np.nan, np.nan
        centroids = np.array(
            [[g.centroid.x, g.centroid.y] for g in buildings.geometry]
        )
        from scipy.spatial import distance_matrix
        dm = distance_matrix(centroids, centroids)
        np.fill_diagonal(dm, np.inf)
        nearest = dm.min(axis=1)
        return float(nearest.mean()), float(nearest.std())
    except Exception:
        return np.nan, np.nan


def _emit(progress, msg: str, pct=None):
    """统一的进度回调，兼容 progress(msg) 与 progress(msg, pct)"""
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


def _parcel_bbox(geom, pad: float = 0.00015):
    """地块外扩一点点，避免边界上的建筑被漏掉"""
    minx, miny, maxx, maxy = geom.bounds
    return (minx - pad, miny - pad, maxx + pad, maxy + pad)


def _ring_filter(geom, tol: float = 0.00008, max_pts: int = 110):
    """
    把地块外环简化成 Overpass 的 poly:"lat lon lat lon ..." 字符串。
    简化是为了控制查询串长度（外环点太多会让请求被拒）。
    """
    try:
        gm = geom
        for _ in range(6):
            s = gm.simplify(tol, preserve_topology=True)
            if s.geom_type == "MultiPolygon":
                s = max(s.geoms, key=lambda p: p.area)
            if s.geom_type != "Polygon" or s.is_empty:
                return None
            pts = list(s.exterior.coords)
            if len(pts) <= max_pts:
                break
            tol *= 2.0
        if len(pts) < 4:
            return None
        return " ".join(f"{y:.5f} {x:.5f}" for x, y in pts)
    except Exception:  # noqa: BLE001
        return None


def _overpass_buildings(geom, progress=None) -> gpd.GeoDataFrame:
    """单个地块：直接用 Overpass 按 bbox 取建筑（多镜像轮询 + 退避重试）"""
    import osm as _osm_mod
    b = _osm_mod._bbox_str(_parcel_bbox(geom))
    q = (
        f'[out:json][timeout:120];'
        f'(way["building"]({b});relation["building"]({b}););'
        f'out geom;'
    )
    d = _osm_mod._overpass(q, timeout=180, retries=4, progress=progress)
    return _osm_mod._elements_to_gdf(d.get("elements", []))


# =========================================================
# 建筑数据本地缓存（同一批地块第二次算指标就不用再联网）
# =========================================================
BLD_CACHE_DIR = os.path.join(C.CACHE_DIR, "buildings")
try:
    os.makedirs(BLD_CACHE_DIR, exist_ok=True)
except Exception:  # noqa: BLE001
    pass


def _bld_ensure_dir():
    try:
        os.makedirs(BLD_CACHE_DIR, exist_ok=True)
    except Exception:  # noqa: BLE001
        pass


def _bld_path(pid, geom) -> str:
    """缓存文件名 = 地块 ID + 几何指纹（几何变了自动失效）"""
    try:
        fp = hashlib.md5(geom.wkt.encode("utf-8")).hexdigest()[:12]
    except Exception:  # noqa: BLE001
        fp = "nogeom"
    return os.path.join(BLD_CACHE_DIR, f"bld_{pid}_{fp}.geojson")


def _bld_load(pid, geom):
    """读缓存；没有或读失败返回 None。空结果缓存过期后再去问一次 OSM"""
    p = _bld_path(pid, geom)
    if not os.path.exists(p):
        return None
    try:
        g = gpd.read_file(p)
        if len(g) == 0 and _bld_is_expired(p):
            return None                     # 过期的「无建筑」结论，重新查
        if g.crs is None:
            g = g.set_crs(CRS_WGS84)
        return g.to_crs(CRS_WGS84)
    except Exception:  # noqa: BLE001
        try:
            os.remove(p)          # 坏文件直接清掉，别一直读到
        except Exception:  # noqa: BLE001
            pass
        return None


def _bld_save(pid, parcel_geom, gdf) -> None:
    """写缓存；只允许非空结果落盘（避免把一次网络抖动固化成「这里没建筑」）

    注意指纹必须用地块几何（parcel_geom），不是建筑数据的几何。
    """
    if gdf is None or len(gdf) == 0 or parcel_geom is None:
        return
    try:
        _bld_ensure_dir()
        gdf.to_file(_bld_path(pid, parcel_geom), driver="GeoJSON")
    except Exception:  # noqa: BLE001
        pass


# 「确认无建筑」的空结果也缓存，但带保质期——OSM 数据一直在补全，
# 太久以前的「这里没建筑」就不可信了
EMPTY_CACHE_TTL_DAYS = 7


def _bld_save_empty(pid, parcel_geom) -> None:
    if parcel_geom is None:
        return
    try:
        _bld_ensure_dir()
        gpd.GeoDataFrame(
            columns=["osm_id", "geometry"], geometry="geometry", crs=CRS_WGS84
        ).to_file(_bld_path(pid, parcel_geom), driver="GeoJSON")
    except Exception:  # noqa: BLE001
        pass


def _bld_is_expired(path: str) -> bool:
    try:
        age = time.time() - os.path.getmtime(path)
        return age > EMPTY_CACHE_TTL_DAYS * 86400
    except OSError:
        return False


def fetch_buildings_batch(parcels, progress=None,
                          per_request: int = 24, workers: int = 4,
                          use_disk_cache: bool = True):
    """
    批量取建筑：多个地块合并进一次 Overpass 请求，并多路并发。

    为什么这么做：
      逐个地块发请求时，选 20 个案例就是 20 次请求，Overpass 很快开始
      429 限流 / 504 超时，表现就是「一直转圈然后失败」。
      用 poly: 过滤把多个地块合成 1 次请求后请求数大幅下降，
      而且只返回地块内部的建筑，载荷更小、更快。

    三级提速（默认参数下 48 个地块约从 23s 降到 6~8s）：
      ① 磁盘缓存：同一批地块第二次算指标直接读本地，不再联网
      ② 并发请求：workers 路并行打 Overpass（瓶颈是等网络，不是 CPU）
      ③ 每批 24 个地块 + 失败自动拆小重试

    降级链：
      磁盘缓存 → 批量并发 → 失败项拆成 3 个/请求重试
      → 仍失败的交给调用方逐地块兜底（保证不丢案例）

    注意：per_request / workers 不是「一次只能处理多少地块」的上限，
    只是网络请求的分批与并发参数，地块数量本身没有限制。

    返回 (cache, covered)
      cache   : {Parcel_ID: 建筑 GeoDataFrame}
      covered : 已被权威查询过的 Parcel_ID 集合（即使结果是 0 栋建筑，
                也不需要再单独重试）
    """
    import osm as _osm_mod

    cache: dict = {}
    covered: set = set()
    rows = list(parcels.iterrows())
    empty = gpd.GeoDataFrame(geometry=[], crs=CRS_WGS84)

    # ---- ⓿ 本地全市建筑索引：命中就完全不联网 ----
    # 这份索引来自当初做离线底图时抓的全市建筑（data/basemap/raw），
    # 所以「查某地块里有哪些建筑」本来就不必问 Overpass：既快，又不会因为
    # 限流/请求被拆碎而漏数据（120 个地块实测：394s → 秒级）。
    try:
        import localidx as _lidx
        if _lidx.available("building"):
            loc, loc_cov = _lidx.query("building", parcels)
            if loc_cov:
                n_has = 0
                for pid in loc_cov:
                    g = loc.get(pid)
                    cache[pid] = g if g is not None and len(g) else empty
                    n_has += bool(len(cache[pid]))
                covered |= loc_cov
                _emit(progress,
                      f"本地建筑索引命中 {len(loc_cov)}/{len(rows)} 个地块"
                      f"（{n_has} 个有建筑，不联网）", 8)
    except Exception as exc:  # noqa: BLE001
        _emit(progress, f"本地索引不可用，改用联网取数（{type(exc).__name__}）")

    # ---- ❶ 磁盘缓存：索引没覆盖到的才查缓存 ----
    todo = rows
    if use_disk_cache:
        todo = []
        hit_n = 0
        for item in rows:
            pid = item[1].get("Parcel_ID")
            if pid in covered:
                continue
            g = _bld_load(pid, item[1].geometry)
            if g is not None:
                cache[pid] = g
                covered.add(pid)
                hit_n += 1
            else:
                todo.append(item)
        if hit_n:
            _emit(progress,
                  f"本地缓存命中 {hit_n}/{len(rows)} 个地块，跳过下载", 8)

    def _process(chunk, label):
        """处理一批地块；返回 (cache_part, covered_part, 失败的行)"""
        c_part: dict = {}
        cov_part: set = set()
        parts, want = [], []
        geom_of: dict = {}                     # pid -> 地块几何（落盘缓存要用）
        for _, r in chunk:
            pid = r.get("Parcel_ID")
            ring = _ring_filter(r.geometry)
            if ring is None:
                continue                       # 几何异常，留给逐地块兜底
            parts.append(f'way["building"](poly:"{ring}");')
            parts.append(f'relation["building"](poly:"{ring}");')
            want.append(pid)
            geom_of[pid] = r.geometry

        if not parts:
            return c_part, cov_part, list(chunk)

        q = f'[out:json][timeout:180];({"".join(parts)});out geom;'
        try:
            d = _osm_mod._overpass(q, timeout=240, retries=3, progress=progress)
            b = _osm_mod._elements_to_gdf(d.get("elements", []))
        except Exception as exc:  # noqa: BLE001
            _emit(progress, f"{label} 取数失败（{str(exc)[:45]}）")
            return c_part, cov_part, list(chunk)

        # 几何与地块做空间连接，落到各自的地块
        chunk_gdf = gpd.GeoDataFrame(
            {"Parcel_ID": [r.get("Parcel_ID") for _, r in chunk]},
            geometry=[r.geometry for _, r in chunk],
            crs=CRS_WGS84,
        )
        if b is not None and not b.empty:
            b = b[b.geometry.type.isin(["Polygon", "MultiPolygon"])].copy()
            if b.crs is None:
                b = b.set_crs(CRS_WGS84)
            b = b.to_crs(CRS_WGS84)
            try:
                hit = gpd.sjoin(b, chunk_gdf, predicate="intersects", how="inner")
                for pid, grp in hit.groupby("Parcel_ID"):
                    sub = b.loc[grp.index.unique()]
                    c_part[pid] = sub
                    # 只缓存「确实有建筑」的结果：把一次网络抖动造成的空结果
                    # 写进磁盘，会让这个地块以后永远取不到数
                    _bld_save(pid, geom_of.get(pid), sub)
            except Exception as exc:  # noqa: BLE001
                _emit(progress, f"{label} 空间匹配失败（{type(exc).__name__}）")
                return c_part, cov_part, list(chunk)

        geom_of_done = {k for k, v in c_part.items()
                        if v is not None and len(v) > 0}
        for pid in want:
            c_part.setdefault(pid, empty)
            cov_part.add(pid)
            if pid not in geom_of_done:
                # 查询本身成功、只是这个地块里没有建筑 → 记一个「确认无建筑」
                # 的缓存（带保质期），下次不用再问
                _bld_save_empty(pid, geom_of.get(pid))
        return c_part, cov_part, []

    def _run(chunks, base_pct, span, tag):
        """并发跑一批 chunk，返回仍未成功的行"""
        nonlocal covered                       # |= 是赋值，必须声明
        failed: list = []
        if not chunks:
            return failed
        with ThreadPoolExecutor(max_workers=max(1, min(workers, len(chunks)))) as ex:
            futs = {ex.submit(_process, ch, f"{tag}{ci}"): ci
                    for ci, ch in enumerate(chunks, 1)}
            done = 0
            for fut in as_completed(futs):
                ci = futs[fut]
                try:
                    c_part, cov_part, fail_part = fut.result()
                except Exception:  # noqa: BLE001
                    c_part, cov_part, fail_part = {}, set(), chunks[ci - 1]
                cache.update(c_part)
                covered |= cov_part
                failed += fail_part
                done += 1
                _emit(progress,
                      f"下载建筑 {done}/{len(chunks)} 批（{tag}）…",
                      base_pct + int(done / len(chunks) * span))
        return failed

    # ---- ❶ 第一轮：批量并发下载 ----
    chunks = [todo[i:i + per_request] for i in range(0, len(todo), per_request)]
    if chunks:
        _emit(progress,
              f"并发下载建筑：{len(chunks)} 批 × 每批最多 {per_request} 个"
              f"（{min(workers, len(chunks))} 路并行）…", 10)
    pending = _run(chunks, 10, 58, "第 ")

    # ---- ❷ 第二轮：失败的拆小重试（小请求更不容易被服务器拒绝）----
    if pending:
        small = 3
        chunks2 = [pending[i:i + small] for i in range(0, len(pending), small)]
        _emit(progress,
              f"{len(pending)} 个地块需要重试，拆成 {len(chunks2)} 个小请求…", 70)
        pending = _run(chunks2, 70, 10, "重试")

    if pending:
        # 判断「连不上网」还是「服务器繁忙」：前者不肯再逐地块重试，
        # 否则 16 个地块要白等二十多分钟，界面看起来就是「卡死」。
        ok, detail = _OSM.probe_network()
        if not ok:
            raise _OSM.NetworkUnavailable(
                f"无法连接 OpenStreetMap（4 个镜像均不可达，最后失败：{detail}）。"
                f"请检查本机网络；若使用代理软件，请在 data/proxy.txt 里写入代理地址"
                f"（例如 http://127.0.0.1:7897）后重启引擎。"
            )
        _emit(progress,
              f"仍有 {len(pending)} 个地块需要逐个下载（取数完成后统一计算指标）…", 80)

    return cache, covered


def _fetch_buildings(geom, progress=None):
    """
    下载地块内全部建筑，返回 (GeoDataFrame, err)。

    为什么不用 osmnx 的 features_from_polygon 做主路径：
    它把异常全部吞掉、还带自己的缓存库和超时设置，失败时完全看不出原因
    （旧版本就是这么变成「一直转圈然后说 OSM 数据不足」的）。
    现在主路径走自己的 Overpass 多镜像重试，失败原因能带回来；
    osmnx 只作最后兜底。
    """
    err = ""
    try:
        b = _overpass_buildings(geom, progress=progress)
        if b is not None and not b.empty:
            b = b[b.geometry.type.isin(["Polygon", "MultiPolygon"])].copy()
            if b.crs is None:
                b = b.set_crs(CRS_WGS84)
            return b.to_crs(CRS_WGS84), ""
        err = "OSM 中该地块范围内没有建筑面数据"
    except Exception as e:  # noqa: BLE001
        err = f"Overpass 取建筑失败：{type(e).__name__}: {str(e)[:70]}"

    # ---- 兜底：osmnx ----
    try:
        import osmnx as ox
        b = ox.features_from_polygon(geom, tags={"building": True})
        if b is not None and not b.empty:
            b = b[b.geometry.type.isin(["Polygon", "MultiPolygon"])].copy()
            if b.crs is None:
                b = b.set_crs(CRS_WGS84)
            return b.to_crs(CRS_WGS84), ""
    except Exception:  # noqa: BLE001
        pass
    return gpd.GeoDataFrame(geometry=[], crs=CRS_WGS84), err


def compute_metrics_for_parcel(pid, name, parcel_geom,
                               clip_buildings: bool = True,
                               fetch_cache: dict | None = None,
                               progress=None,
                               errors: list | None = None,
                               covered: set | None = None) -> dict | None:
    """
    计算单个地块的经济技术指标。
    clip_buildings=True 时，将建筑按地块边界裁剪，
    这是修复「建筑密度(BCR) > 1」异常的关键。

    fetch_cache : 建筑缓存（批量阶段已填好）
    covered     : 已被批量阶段权威查询过的 pid —— 即使结果是空，
                  也不再单独重试，避免大量无效请求
    errors      : 失败原因收集器，便于界面给出可读诊断
    """
    empty = gpd.GeoDataFrame(geometry=[], crs=CRS_WGS84)
    cached = fetch_cache.get(pid) if fetch_cache else None

    if cached is not None and not cached.empty:
        b_wgs = cached
    elif covered is not None and pid in covered:
        b_wgs = cached if cached is not None else empty
        if errors is not None:
            errors.append({
                "Parcel_ID": pid,
                "name": name or "",
                "reason": "该地块范围内 OSM 没有建筑面数据（可能是新填海区或未测绘区域）",
            })
    else:
        b_wgs, err = _fetch_buildings(parcel_geom, progress=progress)
        if fetch_cache is not None:
            fetch_cache[pid] = b_wgs
        if (b_wgs is None or b_wgs.empty) and errors is not None:
            errors.append({
                "Parcel_ID": pid,
                "name": name or "",
                "reason": err or "未取到建筑数据",
            })

    if b_wgs is None or b_wgs.empty:
        return None

    # --- 投影 ---
    parcel_proj = gpd.GeoSeries([parcel_geom], crs=CRS_WGS84).to_crs(CRS_PROJECTED)
    parcel_area = float(parcel_proj.area.iloc[0])
    parcel_perimeter = float(parcel_proj.length.iloc[0])
    if parcel_area <= 0:
        return None

    b = b_wgs.to_crs(CRS_PROJECTED)

    # --- 关键：裁剪到地块边界内 ---
    raw_count = len(b)
    clipped_flag = 0
    if clip_buildings:
        try:
            parcel_geom_proj = parcel_proj.iloc[0]
            b = b.copy()
            b["geometry"] = b.geometry.intersection(parcel_geom_proj)
            b = b[b.geometry.notna() & ~b.geometry.is_empty]
            b = b[b.geometry.area > 1.0]  # 丢掉裁剪后几乎消失的碎片
            clipped_flag = 1 if len(b) < raw_count else 0
        except Exception:
            pass

    if b.empty:
        return None

    b = gpd.GeoDataFrame(b, geometry="geometry", crs=CRS_PROJECTED)
    b["footprint"] = b.geometry.area
    footprint_sum = float(b["footprint"].sum())
    building_count = int(len(b))

    # --- 层数：实测优先，缺的才补（逐栋处理，不整块替换）---
    # 旧逻辑是「只要平均值算不出来，就把整块地全部换成经验值」，
    # 结果 90% 建筑明明标了层数也会被经验值覆盖，容积率严重失真。
    b["levels"] = b.apply(parse_levels, axis=1)
    lv = pd.to_numeric(b["levels"], errors="coerce")
    lv[lv <= 0] = np.nan

    n_all = int(len(b))
    n_valid = int(lv.notna().sum())
    levels_coverage = (n_valid / n_all) if n_all else 0.0

    levels_imputed = False
    far_imputed_share = 0.0
    if n_valid < n_all:
        levels_imputed = True
        bcr_pre = footprint_sum / parcel_area if parcel_area > 0 else np.nan
        fill_per = _impute_levels(b, lv, bcr_pre)
        b["levels"] = lv.fillna(fill_per)

        # 补全的这部分贡献了多少建筑面积 —— 比例越高，FAR 越不可信
        imp = lv.isna() & b["levels"].notna()
        imp_area = float((b.loc[imp, "footprint"] * b.loc[imp, "levels"]).sum())
        far_all = float((b["footprint"] * b["levels"]).sum())
        far_imputed_share = (imp_area / far_all) if far_all > 0 else 0.0

        n_same = int((fill_per == fill_per).sum())
        levels_source = (
            f"实测 {n_valid}/{n_all} 栋（{levels_coverage * 100:.0f}%），"
            f"其余 {n_all - n_valid} 栋按「同类型/相近基底」逐栋补齐"
            f"（补全部分占建筑面积 {far_imputed_share * 100:.0f}%）"
            if n_valid else
            f"该地块 OSM 无层数标签，按建筑类型与基底大小逐栋估算"
            f"（{n_same} 栋）"
        )
    else:
        b["levels"] = lv
        levels_source = f"全部实测（{n_all} 栋）"

    lv_final = pd.to_numeric(b["levels"], errors="coerce")
    avg_levels = float(lv_final.mean()) if len(lv_final) else np.nan
    max_levels = float(lv_final.max()) if len(lv_final) else np.nan
    levels_std = float(lv_final.std()) if len(lv_final) > 1 else np.nan

    # 基底面积加权平均层数。
    # 表里的 avg_levels 是「简单平均」（每栋一票，不看大小），
    # 用它乘 BCR 会对不上 FAR —— 这不是 bug，是两种平均口径不同。
    if footprint_sum > 0:
        avg_levels_w = float((b["footprint"] * lv_final).sum() / footprint_sum)
    else:
        avg_levels_w = np.nan

    # Σ(基底 × 层数) 再乘折减系数：OSM 轮廓是最大投影，含架空层/阳台/裙楼，
    # 直接求和会比计容建筑面积高约 20%（见 config.FLOOR_AREA_SHRINK 的标定说明）
    raw_floor_area = float((b["footprint"] * b["levels"]).sum())
    shrink = float(getattr(C, "FLOOR_AREA_SHRINK", 1.0) or 1.0)
    total_floor_area = raw_floor_area * shrink

    BCR = footprint_sum / parcel_area
    FAR = total_floor_area / parcel_area

    highrise_ratio = (
        float((b["levels"] >= 18).sum()) / building_count if building_count else 0.0
    )

    # --- 布局指标 ---
    nn_mean, nn_std = nearest_neighbor_metrics(b)
    orientations = b.geometry.apply(get_building_orientation)
    orientation_std = float(orientations.std()) if orientations.notna().sum() > 1 else np.nan
    elongations = b.geometry.apply(get_elongation)
    elongation_mean = float(elongations.mean()) if elongations.notna().sum() else np.nan
    elongation_std = float(elongations.std()) if elongations.notna().sum() > 1 else np.nan

    footprint_areas = b["footprint"].values
    footprint_std = float(np.std(footprint_areas))
    footprint_mean = float(np.mean(footprint_areas))
    footprint_cv = footprint_std / footprint_mean if footprint_mean > 0 else np.nan

    compactness = (
        4 * math.pi * parcel_area / (parcel_perimeter ** 2)
        if parcel_perimeter > 0 else np.nan
    )

    # --- 覆盖率（建筑占地 / 建筑轮廓外接）---
    return {
        "Parcel_ID": pid,
        "name": name or "未命名",
        # 地块
        "parcel_area_sqm": parcel_area,
        "parcel_area_log": math.log(parcel_area + 1),
        "compactness": compactness,
        # 建筑数量
        "building_count": building_count,
        "building_count_log": math.log(building_count + 1),
        # 开发强度
        "BCR": BCR,
        "FAR": FAR,
        "FAR_log": math.log(FAR + 1) if FAR > 0 else np.nan,
        # 几何 FAR：未折减口径（Σ基底×层数 / 用地）。
        # FAR 列是「近似计容」口径（×FLOOR_AREA_SHRINK）；
        # 前端可切换显示/聚类用哪个，两列都常驻，切换不需要重算。
        "far_geometric": raw_floor_area / parcel_area if parcel_area > 0 else np.nan,
        # 总建筑面积（㎡）：可直接和官方公布的建筑面积核对。
        # 已含折减系数（config.FLOOR_AREA_SHRINK），未折减的原始值见 raw_floor_area
        "building_area_sqm": total_floor_area,
        "floor_area_raw_sqm": raw_floor_area,
        # 高度
        "avg_levels": avg_levels,
        # 加权平均层数：FAR = BCR × avg_levels_weighted（核对用）
        "avg_levels_weighted": round(avg_levels_w, 4) if pd.notna(avg_levels_w) else np.nan,
        # 建筑面积里有多少比例来自「补出来的层数」—— 越高 FAR 越不可信
        "far_imputed_share": round(far_imputed_share, 4),
        "max_levels": max_levels,
        "levels_std": levels_std,
        "highrise_ratio": highrise_ratio,
        # 空间关系
        "nearest_neighbor_mean": nn_mean,
        "nearest_neighbor_std": nn_std,
        # 方向 / 形状
        "orientation_std": orientation_std,
        "elongation_mean": elongation_mean,
        "elongation_std": elongation_std,
        # 离散
        "footprint_std": footprint_std,
        "footprint_cv": footprint_cv,
        # 元信息
        "levels_imputed": int(levels_imputed),
        "levels_coverage": round(levels_coverage, 4),   # 有实测层数的建筑占比
        "levels_source": levels_source,                 # 一句话说明层数怎么来的
        "buildings_clipped": clipped_flag,
        "raw_building_count": raw_count,
    }


# =========================================================
# 异常标记
# =========================================================

def detect_anomalies(df: pd.DataFrame) -> pd.DataFrame:
    """
    为每行追加：
      anomaly_flags : 逗号分隔的问题标签
      anomaly_level : 0 正常 / 1 警告 / 2 严重
      anomaly_desc  : 人类可读说明
    """
    flags, levels, descs = [], [], []

    for _, r in df.iterrows():
        f, lv, d = [], 0, []

        bcr = r.get("BCR")
        far = r.get("FAR")
        bc = r.get("building_count", 0)
        area = r.get("parcel_area_sqm", 0)
        clipped = r.get("buildings_clipped", 0)

        # BCR > 1 —— 物理不可能，OSM 边界问题
        if pd.notna(bcr) and bcr > 1.0:
            f.append("BCR>1")
            lv = 2
            d.append(f"建筑密度 {bcr:.2f} 大于 1，建筑轮廓超出地块边界（OSM 边界识别问题）")
        elif pd.notna(bcr) and bcr > 0.7:
            f.append("BCR偏高")
            lv = max(lv, 1)
            d.append(f"建筑密度 {bcr:.2f} 偏高，请核对边界")

        # FAR 异常
        if pd.notna(far):
            if far > 12:
                f.append("FAR异常高")
                lv = 2
                d.append(f"容积率 {far:.2f} 异常偏高，通常为层数缺失被高估")
            elif far > 8:
                f.append("FAR偏高")
                lv = max(lv, 1)
                d.append(f"容积率 {far:.2f} 偏高，建议核对层数来源")

        # 层数被补全（按覆盖率分级，别把「补了 1 栋」和「整块都是估的」混为一谈）
        if r.get("levels_imputed", 0) == 1:
            cov = r.get("levels_coverage", 0) or 0
            src = r.get("levels_source", "") or ""
            if cov <= 0:
                # 为什么只是「警告」不是「严重」：实测深圳约 40% 的地块一块层数
                # 标签都没有，判成严重会被默认过滤直接抹掉四成样本 —— 那正是
                # 用户吐槽的「怎么筛都是 0」。所以让它保留，但把话说清楚。
                f.append("层数全为估算")
                lv = max(lv, 1)
                d.append(f"该地块 OSM 无层数标签（{src}），高度为经验值，"
                         f"容积率/建筑高度仅供参考")
            elif cov < 0.5:
                f.append("层数大量缺失")
                lv = max(lv, 1)
                d.append(f"仅 {cov * 100:.0f}% 的建筑有层数标签（{src}），高度指标可信度较低")
            elif cov < 0.85:
                f.append("部分层数缺失")
                lv = max(lv, 1)
                d.append(f"{cov * 100:.0f}% 的建筑有实测层数（{src}），"
                         f"其余按「同类型/相近基底」补齐")
            # cov >= 0.85 不标异常：缺失是极少数，对 FAR 影响可忽略，
            # 否则几乎每块地都会挂个「警告」，等于没有区分度

            # FAR 里有多少是「补出来的层数」撑起来的 —— 这才是 FAR 可不可信的关键。
            # 覆盖率看着高、但缺的偏偏是那几栋大基底建筑时，FAR 一样能偏很多。
            share = r.get("far_imputed_share", 0) or 0
            if share > 0.35:
                f.append("FAR大量靠估算")
                lv = max(lv, 1)
                d.append(f"建筑面积中 {share * 100:.0f}% 来自补全的层数，"
                         f"容积率可信度低，建议核对")

        # 建筑数过少
        if pd.notna(bc) and bc <= 2:
            f.append("建筑过少")
            lv = max(lv, 1)
            d.append(f"仅识别到 {int(bc)} 栋建筑，可能地块内部未完整映射")

        # 面积过小
        if pd.notna(area) and area < 12000:
            f.append("面积偏小")
            lv = max(lv, 1)
            d.append(f"地块面积仅 {area:,.0f} ㎡，样本代表性偏弱")

        # 建筑被裁剪
        if clipped == 1:
            f.append("已裁剪建筑")
            d.append("存在越界建筑，已按地块边界裁剪（修复 BCR 异常）")

        flags.append(",".join(f))
        levels.append(lv)
        descs.append("；".join(d) if d else "正常")

    out = df.copy()
    out["anomaly_flags"] = flags
    out["anomaly_level"] = levels
    out["anomaly_desc"] = descs
    return out


def compute_batch(parcels: gpd.GeoDataFrame, clip_buildings: bool = True,
                  progress=None) -> tuple[pd.DataFrame, dict, dict]:
    """
    批量计算指标。
    返回 (指标表, 建筑缓存, 诊断信息)
      - 建筑缓存用于后续出平面图，避免重复下载
      - 诊断信息 = {"total": n, "ok": k, "failed": [{Parcel_ID, name, reason}]}
    """
    results = []
    errors: list = []
    fetch_cache: dict = {}
    covered: set = set()
    total = max(len(parcels), 1)

    # ---- 阶段一：合并下载建筑（请求数降为 1/12，避免被 Overpass 限流）----
    _emit(progress, f"开始获取 {total} 个地块的建筑数据…", 2)
    try:
        fetch_cache, covered = fetch_buildings_batch(parcels, progress=progress)
    except _OSM.NetworkUnavailable:
        # 断网：直接冒泡，让接口快速失败并给出可读提示，
        # 不再退化成「逐地块下载」白等
        raise
    except Exception as e:  # noqa: BLE001
        _emit(progress, f"合并取数失败（{str(e)[:60]}），改为逐个地块下载…", 5)
        fetch_cache, covered = {}, set()

    # ---- 阶段二：并行算指标（此时已不联网；shapely 大量运算会释放 GIL）----
    def _one(item):
        i, (_, row) = item
        pid = row.get("Parcel_ID")
        try:
            rec = compute_metrics_for_parcel(
                pid, row.get("name", ""), row.geometry,
                clip_buildings=clip_buildings,
                fetch_cache=fetch_cache,
                errors=errors,
                covered=covered,
            )
            return rec, None
        except Exception as e:  # noqa: BLE001
            return None, {
                "Parcel_ID": pid,
                "name": row.get("name", "") or "",
                "reason": f"指标计算异常：{type(e).__name__}: {str(e)[:70]}",
            }

    items = list(enumerate(parcels.iterrows(), 1))
    n_workers = min(4, max(1, len(items)))
    if n_workers > 1:
        with ThreadPoolExecutor(max_workers=n_workers) as ex:
            for k, (rec, err) in enumerate(ex.map(_one, items), 1):
                if err:
                    errors.append(err)
                if rec:
                    results.append(rec)
                if k % 10 == 0 or k == total:
                    _emit(progress, f"[{k}/{total}] 计算经济技术指标…",
                          82 + int(k / total * 18))
    else:
        for k, item in enumerate(items, 1):
            rec, err = _one(item)
            if err:
                errors.append(err)
            if rec:
                results.append(rec)

    diag = {"total": total, "ok": len(results), "failed": errors}

    if not results:
        return pd.DataFrame(), fetch_cache, diag

    df = pd.DataFrame(results)
    df = detect_anomalies(df)
    _emit(progress, f"指标计算完成：{len(df)}/{total} 个地块成功", 100)
    return df, fetch_cache, diag
