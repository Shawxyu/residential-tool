# -*- coding: utf-8 -*-
"""
平面示意图 & 科研图输出
- 平面示意图：建筑黑色填充、用地红线、灰色道路（复刻 stage2_5.1，dpi 提升到 300）
- 科研图：PCA + 箱线图（复刻 stage4.1 风格，并修正二次标准化 bug）
"""
from __future__ import annotations

import os
import hashlib
from concurrent.futures import ThreadPoolExecutor, as_completed

import numpy as np
import pandas as pd
import geopandas as gpd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

from shapely.geometry import Polygon, box

import config as C
from config import CRS_WGS84, CRS_PROJECTED, ROAD_WIDTH_MAP, OUTPUT_DIR

# 论文级全局风格
plt.rcParams["font.family"] = "Times New Roman"
plt.rcParams["axes.unicode_minus"] = False
plt.rcParams["savefig.dpi"] = 300


# =========================================================
# 平面示意图
# =========================================================

# =========================================================
# 道路数据本地缓存（与建筑缓存同理，避免重复联网）
# =========================================================
ROADS_CACHE_DIR = os.path.join(C.CACHE_DIR, "roads")


def _roads_path(pid, geom) -> str:
    try:
        fp = hashlib.md5(geom.wkt.encode("utf-8")).hexdigest()[:12]
    except Exception:  # noqa: BLE001
        fp = "nogeom"
    return os.path.join(ROADS_CACHE_DIR, f"road_{pid}_{fp}.geojson")


def _roads_load(pid, geom):
    p = _roads_path(pid, geom)
    if not os.path.exists(p):
        return None
    try:
        g = gpd.read_file(p)
        if g.crs is None:
            g = g.set_crs(CRS_WGS84)
        return g.to_crs(CRS_WGS84)
    except Exception:  # noqa: BLE001
        try:
            os.remove(p)
        except Exception:  # noqa: BLE001
            pass
        return None


def _roads_save(pid, geom, gdf) -> None:
    if gdf is None or len(gdf) == 0 or geom is None:
        return
    try:
        os.makedirs(ROADS_CACHE_DIR, exist_ok=True)
        gdf.to_file(_roads_path(pid, geom), driver="GeoJSON")
    except Exception:  # noqa: BLE001
        pass


def _fetch_roads(geom) -> gpd.GeoDataFrame:
    """
    取地块周边的道路。

    取数顺序：本地全市路网索引 → Overpass（多镜像重试）→ osmnx 兜底。
    本地索引来自做离线底图时抓的全市路网，命中就不联网，
    所以平面图生成不再受 Overpass 限流影响。
    """
    # ⓿ 本地索引（毫秒级、不联网）
    try:
        import localidx as _lidx
        if _lidx.available("road"):
            buf = gpd.GeoDataFrame(
                {"Parcel_ID": [0]}, geometry=[geom.buffer(0.0003)],
                crs=CRS_WGS84)
            loc, cov = _lidx.query("road", buf)
            if 0 in cov:
                g = loc.get(0)
                if g is not None and not g.empty:
                    return g
                return gpd.GeoDataFrame(geometry=[], crs=CRS_WGS84)
    except Exception:  # noqa: BLE001
        pass

    import osm as _osm_mod
    try:
        minx, miny, maxx, maxy = geom.buffer(0.0005).bounds
        b = _osm_mod._bbox_str((minx, miny, maxx, maxy))
        hw = "|".join(ROAD_WIDTH_MAP.keys())
        q = (f'[out:json][timeout:90];'
             f'way["highway"~"^({hw})$"]({b});out geom;')
        d = _osm_mod._overpass(q, timeout=120, retries=3)
        r = _osm_mod._elements_to_gdf(d.get("elements", []))
        if r is not None and not r.empty:
            r = r[r.geometry.type.isin(["LineString", "MultiLineString"])].copy()
            if r.crs is None:
                r = r.set_crs(CRS_WGS84)
            return r.to_crs(CRS_WGS84)
    except Exception:  # noqa: BLE001
        pass

    try:
        import osmnx as ox
        r = ox.features_from_polygon(
            geom.buffer(0.0005),
            tags={"highway": list(ROAD_WIDTH_MAP.keys())},
        )
        if r is not None and not r.empty:
            r = r[r.geometry.type.isin(["LineString", "MultiLineString"])].copy()
            if r.crs is None:
                r = r.set_crs(CRS_WGS84)
            return r.to_crs(CRS_WGS84)
    except Exception:  # noqa: BLE001
        pass
    return gpd.GeoDataFrame(geometry=[], crs=CRS_WGS84)


def _roads_query(todo, chunk_size: int = 20):
    """把多个地块的 bbox 合并进一条 Overpass 查询，返回 (pid, 道路gdf) 字典"""
    import osm as _osm_mod

    hw = "|".join(ROAD_WIDTH_MAP.keys())
    out: dict = {}
    for i in range(0, len(todo), chunk_size):
        grp = todo[i:i + chunk_size]
        parts = []
        for pid, geom in grp:
            try:
                minx, miny, maxx, maxy = geom.buffer(0.0005).bounds
            except Exception:  # noqa: BLE001
                continue
            parts.append(
                f'way["highway"~"^({hw})$"]'
                f'({miny:.6f},{minx:.6f},{maxy:.6f},{maxx:.6f});'
            )
        if not parts:
            continue
        q = f'[out:json][timeout:120];({"".join(parts)});out geom;'
        try:
            d = _osm_mod._overpass(q, timeout=180, retries=3)
            r = _osm_mod._elements_to_gdf(d.get("elements", []))
        except Exception:  # noqa: BLE001
            continue
        if r is None or r.empty:
            continue
        r = r[r.geometry.type.isin(["LineString", "MultiLineString"])].copy()
        if r.crs is None:
            r = r.set_crs(CRS_WGS84)
        r = r.to_crs(CRS_WGS84)
        # 分回各自地块
        pg = gpd.GeoDataFrame(
            {"Parcel_ID": [pid for pid, _ in grp]},
            # 稍微外扩一点：把贴着地块边的城市道路也算进来，图上才看得到周边路网
            geometry=[geom.buffer(0.0003) for _, geom in grp], crs=CRS_WGS84,
        )
        try:
            hit = gpd.sjoin(r, pg, predicate="intersects", how="inner")
            for pid, gidx in hit.groupby("Parcel_ID"):
                out[pid] = r.loc[gidx.index.unique()]
        except Exception:  # noqa: BLE001
            continue
    return out


def _fetch_roads_batch(rows, workers: int = 6, progress=None,
                       chunk_size: int = 20) -> dict:
    """
    批量取道路：多个地块合并成一条 Overpass 查询 + 落盘缓存。

    以前是「画一张图发一次请求」，12 个地块光等网络就要 27 秒，
    而且并发太多还容易被 Overpass 限流。
    现在每 20 个地块合成 1 次请求，48 个地块只要 3 次请求；
    同一批地块第二次直接读本地缓存。
    """
    result: dict = {}
    todo = []

    # ⓿ 本地全市路网索引：命中的地块直接出结果，一个网络请求都不发
    try:
        import localidx as _lidx
        if _lidx.available("road") and rows:
            buf = gpd.GeoDataFrame(
                {"Parcel_ID": [pid for pid, _ in rows]},
                geometry=[g.buffer(0.0003) for _, g in rows], crs=CRS_WGS84)
            loc, cov = _lidx.query("road", buf)
            for pid in cov:
                g = loc.get(pid)
                result[pid] = g if g is not None and not g.empty else \
                    gpd.GeoDataFrame(geometry=[], crs=CRS_WGS84)
            if cov:
                if progress:
                    try:
                        progress(f"本地路网索引命中 {len(cov)}/{len(rows)} 个地块"
                                 f"（不联网）")
                    except TypeError:
                        pass
    except Exception:  # noqa: BLE001
        pass

    for pid, geom in rows:
        if pid in result:
            continue
        g = _roads_load(pid, geom)
        if g is not None:
            result[pid] = g
        else:
            todo.append((pid, geom))

    if todo:
        n_req = max(1, (len(todo) + chunk_size - 1) // chunk_size)
        if progress:
            try:
                progress(f"下载道路：{len(todo)} 个地块合并成 {n_req} 次请求…")
            except TypeError:
                pass

        # 按 chunk_size 切段，多线程并发跑这几段（段数本身很少，不会触发限流）
        segs = [todo[i:i + chunk_size] for i in range(0, len(todo), chunk_size)]
        with ThreadPoolExecutor(max_workers=max(1, min(workers, len(segs)))) as ex:
            futs = [ex.submit(_roads_query, seg, chunk_size) for seg in segs]
            for fut in as_completed(futs):
                try:
                    part = fut.result() or {}
                except Exception:  # noqa: BLE001
                    part = {}
                for pid, g in part.items():
                    result[pid] = g

        # 落盘（只存有内容的）
        geom_of = dict(rows)
        for pid, g in result.items():
            if pid not in geom_of:
                continue
            _roads_save(pid, geom_of[pid], g)

        if progress and result:
            try:
                progress(f"道路下载完成（{len(result)}/{len(rows)} 个地块）")
            except TypeError:
                pass

    return result


def draw_plan(pid, parcel_geom, buildings_wgs: gpd.GeoDataFrame,
              out_path: str, dpi: int = 300,
              roads_cache: dict | None = None,
              margin_ratio: float = 0.16):
    """绘制单个地块的平面示意图。

    v2：统一输出尺寸 —— 画幅固定为以地块为中心的正方形，
    范围 = 红线外包盒 + 四周 margin_ratio 的留白（裁剪原则：离红线留一定距离），
    不再用 bbox_inches="tight"，因此所有图尺寸完全一致（1200x1200 @300dpi）。
    道路只保留视野范围内的部分，远离住区的路段被裁掉。
    """
    parcel_proj = gpd.GeoSeries([parcel_geom], crs=CRS_WGS84).to_crs(CRS_PROJECTED)
    parcel = parcel_proj.iloc[0]

    # ---- 统一画幅：红线外包盒 + 留白，取正方形（短边补齐）----
    minx, miny, maxx, maxy = parcel.bounds
    w, h = maxx - minx, maxy - miny
    half = max(w, h) / 2 * (1 + 2 * margin_ratio)   # 含留白的半边长
    cx, cy = (minx + maxx) / 2, (miny + maxy) / 2
    view = box(cx - half, cy - half, cx + half, cy + half)

    # 建筑
    if buildings_wgs is not None and not buildings_wgs.empty:
        b = buildings_wgs.to_crs(CRS_PROJECTED)
        # 裁剪到地块，保持与指标一致
        b = b.copy()
        b["geometry"] = b.geometry.intersection(parcel)
        b = b[b.geometry.notna() & ~b.geometry.is_empty]
        b = b[b.geometry.area > 1.0]
    else:
        b = gpd.GeoDataFrame(geometry=[], crs=CRS_PROJECTED)

    # 道路
    if roads_cache is not None and pid in roads_cache:
        roads = roads_cache[pid]
    else:
        roads = _fetch_roads(parcel_geom)
        if roads_cache is not None:
            roads_cache[pid] = roads
    if not roads.empty:
        roads = roads.to_crs(CRS_PROJECTED)
        # 只保留视野内的道路：远离住区的部分直接截掉
        roads = roads.copy()
        roads["geometry"] = roads.geometry.intersection(view)
        roads = roads[roads.geometry.notna() & ~roads.geometry.is_empty]
        if "highway" not in roads.columns:
            roads["highway"] = "residential"
        roads["road_width"] = roads["highway"].map(ROAD_WIDTH_MAP).fillna(6)

    # 固定 4x4 英寸画布（1200x1200 @300dpi），不再 tight 裁切 → 尺寸统一
    fig, ax = plt.subplots(figsize=(4, 4))

    # 道路先画（灰色）
    if not roads.empty:
        for hw, width in ROAD_WIDTH_MAP.items():
            sub = roads[roads["highway"] == hw]
            if not sub.empty:
                sub.plot(ax=ax, linewidth=width / 8, color="lightgray")
        extra = roads[~roads["highway"].isin(ROAD_WIDTH_MAP.keys())]
        if not extra.empty:
            extra.plot(ax=ax, linewidth=0.8, color="lightgray")

    # 建筑（黑色填充）
    if not b.empty:
        b.plot(ax=ax, color="black", linewidth=0)

    # 用地红线
    parcel_proj.boundary.plot(ax=ax, color="red", linewidth=2)

    ax.set_xlim(cx - half, cx + half)
    ax.set_ylim(cy - half, cy + half)
    ax.set_aspect("equal")
    ax.axis("off")
    fig.subplots_adjust(left=0, right=1, top=1, bottom=0)
    fig.savefig(out_path, dpi=dpi)
    plt.close(fig)
    return out_path


def draw_plans_batch(parcels: gpd.GeoDataFrame, buildings_cache: dict,
                     out_dir: str, dpi: int = 300, progress=None) -> list[str]:
    """批量出平面图，返回 [(pid, path)]"""
    os.makedirs(out_dir, exist_ok=True)
    made = []
    total = len(parcels)

    # 先把所有地块的道路并发取回来（原来是一张图一次请求，慢就慢在这）
    rows = [(row.get("Parcel_ID"), row.geometry) for _, row in parcels.iterrows()]
    roads_cache: dict = {}
    try:
        roads_cache = _fetch_roads_batch(rows, workers=6, progress=progress)
    except Exception:  # noqa: BLE001
        roads_cache = {}
    # 关键：查不到道路的地块要显式塞一个空表。
    # 否则 draw_plan 会发现 pid 不在缓存里，又回头单独发一次网络请求，
    # 出图速度直接被打回原形。
    _empty_roads = gpd.GeoDataFrame(geometry=[], crs=CRS_WGS84)
    for pid, _g in rows:
        roads_cache.setdefault(pid, _empty_roads)

    for i, (_, row) in enumerate(parcels.iterrows(), 1):
        pid = row.get("Parcel_ID")
        if progress:
            try:
                progress(f"正在绘制第 {i}/{total} 张平面图（地块 {pid}）…",
                         int((i - 1) / max(total, 1) * 100))
            except TypeError:
                progress(f"正在绘制第 {i}/{total} 张平面图…")
        try:
            p = os.path.join(out_dir, f"parcel_{pid}.png")
            draw_plan(pid, row.geometry, buildings_cache.get(pid),
                      p, dpi=dpi, roads_cache=roads_cache)
            made.append((pid, p))
        except Exception:
            continue
    if progress:
        try:
            progress(f"平面图完成：{len(made)}/{total} 张", 100)
        except TypeError:
            pass
    return made


# =========================================================
# 科研图：PCA + 箱线图
# =========================================================

def _confidence_ellipse(x, y, ax, n_std=2.0, **kwargs):
    from matplotlib.patches import Ellipse
    if len(x) < 3:
        return
    cov = np.cov(x, y)
    mean_x, mean_y = np.mean(x), np.mean(y)
    evals, evecs = np.linalg.eigh(cov)
    order = evals.argsort()[::-1]
    evals, evecs = evals[order], evecs[:, order]
    angle = np.degrees(np.arctan2(evecs[1, 0], evecs[0, 0]))
    w, h = 2 * n_std * np.sqrt(evals)
    ax.add_patch(Ellipse((mean_x, mean_y), w, h, angle=angle, fill=False, **kwargs))


DEFAULT_FEATURES = [
    "FAR", "nearest_neighbor_mean", "orientation_std",
    "elongation_mean", "levels_std",
]


def draw_cluster_figure(df: pd.DataFrame, features: list[str], out_path: str,
                        dpi: int = 300) -> str:
    """
    PCA 散点图 + 各指标箱线图。
    修正点：聚类中心直接使用标准化空间 → PCA 投影，不再二次标准化。
    """
    import seaborn as sns
    from sklearn.preprocessing import StandardScaler
    from sklearn.decomposition import PCA

    features = [f for f in features if f in df.columns]
    if not features:
        raise ValueError("没有可用的绘图指标")

    X = df[features].apply(pd.to_numeric, errors="coerce")

    # levels_std 可信度控制（与原脚本一致）：层数被补全的样本其 levels_std 不可信
    if "levels_std" in X.columns and "levels_imputed" in df.columns:
        X.loc[df["levels_imputed"] == 1, "levels_std"] = np.nan

    # 中位数填补后再标准化（避免整行丢弃导致样本流失）
    med = X.median(numeric_only=True)
    X = X.fillna(med).fillna(0.0)
    df = df.loc[X.index].copy()

    scaler = StandardScaler()
    X_scaled = scaler.fit_transform(X)

    pca = PCA(n_components=2)
    X_pca = pca.fit_transform(X_scaled)
    df["PCA1"], df["PCA2"] = X_pca[:, 0], X_pca[:, 1]

    # 聚类中心：原始指标空间求均值 → 标准化一次 → PCA 投影
    centers = df.groupby("cluster")[features].mean()
    # 整簇都被补全时均值可能为 NaN，用全局中位数兜底
    centers = centers.fillna(med).fillna(0.0)
    centers_pca = pca.transform(scaler.transform(centers))

    sns.set_style("whitegrid")
    clusters = sorted(df["cluster"].unique())
    palette = sns.color_palette("Set2", len(clusters))
    n_box = len(features)
    ncols = 3
    nrows = 1 + int(np.ceil(n_box / ncols))

    fig = plt.figure(figsize=(14, 4 * nrows))

    # ---- (a) PCA ----
    ax1 = plt.subplot(nrows, ncols, 1)
    for i, c in enumerate(clusters):
        sub = df[df["cluster"] == c]
        ax1.scatter(sub["PCA1"], sub["PCA2"], s=35, alpha=0.75,
                    color=palette[i], label=f"Cluster {c}")
        _confidence_ellipse(sub["PCA1"], sub["PCA2"], ax1,
                            n_std=2, edgecolor=palette[i], linewidth=1.5)
    ax1.scatter(centers_pca[:, 0], centers_pca[:, 1], marker="X",
                s=165, c="black", linewidth=2, label="Centroids")
    ax1.set_title("(a) PCA Cluster Map")
    ax1.set_xlabel(f"PC1 ({pca.explained_variance_ratio_[0]*100:.1f}%)")
    ax1.set_ylabel(f"PC2 ({pca.explained_variance_ratio_[1]*100:.1f}%)")
    ax1.legend(fontsize=8)

    # ---- (b..) 箱线图 ----
    letters = "bcdefghijklmn"
    for idx, feat in enumerate(features):
        pos = 2 + idx
        if pos > nrows * ncols:
            break
        ax = plt.subplot(nrows, ncols, pos)
        sns.boxplot(data=df, x="cluster", y=feat, ax=ax,
                    width=0.6, color="#7fb3d5")
        ax.set_title(f"({letters[idx]}) {feat}")
        ax.set_xlabel("Cluster")
        ax.set_ylabel("")

    plt.tight_layout()
    fig.savefig(out_path, dpi=dpi, bbox_inches="tight")
    plt.close(fig)
    return out_path


def draw_elbow(sse_df: pd.DataFrame, out_path: str, dpi: int = 300) -> str:
    """肘部法折线图"""
    fig, ax = plt.subplots(figsize=(8, 6))
    ax.plot(sse_df["K"], sse_df["SSE"], marker="o", color="#3d8b7d")
    ax.set_xticks(list(sse_df["K"]))
    ax.set_xlabel("Number of Clusters (K)")
    ax.set_ylabel("SSE (Inertia)")
    ax.set_title("KMeans Elbow Method")
    ax.grid(True)
    plt.tight_layout()
    fig.savefig(out_path, dpi=dpi)
    plt.close(fig)
    return out_path
