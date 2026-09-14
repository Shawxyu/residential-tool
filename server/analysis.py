# -*- coding: utf-8 -*-
"""
聚类分析引擎
- 肘部法（SSE）
- KMeans 聚类 + 各簇导出
- 聚类中心 + 典型案例（离中心最近）
- PCA / 箱线图数据
"""
from __future__ import annotations

from typing import List, Optional, Dict, Any, Tuple

import numpy as np
import pandas as pd
from sklearn.cluster import KMeans
from sklearn.preprocessing import StandardScaler
from sklearn.impute import SimpleImputer
from sklearn.decomposition import PCA

RANDOM_STATE = 42


def _prepare(df: pd.DataFrame, features: List[str]):
    """
    可信度控制 + 缺失填补 + 标准化。

    坑（已修）：sklearn ≥ 1.2 的 SimpleImputer 在 fit 时会**直接丢弃整列全缺失
    的特征**。原脚本没同步收缩 features，于是还原聚类中心时维度对不上，报
    "Shape of passed values is (k, n-1), indices imply (k, n)" 而 500。
    这里按 imputer 的实际支持列收缩 features，保证全程维度一致。
    """
    df = df.copy()

    # levels_std 可信度控制：层数被补全的样本，其 levels_std 不可信
    if "levels_std" in df.columns and "levels_imputed" in df.columns:
        mask = pd.to_numeric(df["levels_imputed"], errors="coerce") == 1
        df.loc[mask, "levels_std"] = np.nan

    features = [f for f in features if f in df.columns]
    if not features:
        raise ValueError("所选聚类指标在结果表中都不存在，请重新勾选。")

    X = df[features].apply(pd.to_numeric, errors="coerce")

    imputer = SimpleImputer(strategy="median")
    X_imp = imputer.fit_transform(X)

    dropped: List[str] = []
    if hasattr(imputer, "get_support"):
        try:
            support = list(imputer.get_support())
            if len(support) == len(features):
                dropped = [f for f, keep in zip(features, support) if not keep]
                features = [f for f, keep in zip(features, support) if keep]
        except Exception:  # noqa: BLE001
            pass

    if not features:
        raise ValueError(
            "没有任何可用聚类指标：所选指标在所有案例中都是缺失值。"
            "建议改用 FAR、BCR、建筑数量、最近邻距离等在表格里有值的指标。"
        )

    scaler = StandardScaler()
    X_scaled = scaler.fit_transform(X_imp)

    # 最后一道保险：维度必须完全对齐
    if X_scaled.shape[1] != len(features):
        features = features[:X_scaled.shape[1]]

    df.attrs["dropped_features"] = dropped
    return df, features, X_scaled, scaler, imputer


def elbow(df: pd.DataFrame, features: List[str], k_min: int = 2, k_max: int = 11):
    """肘部法：返回每个 K 的 SSE"""
    n = len(df)
    if n < 3:
        raise ValueError(f"样本数过少（{n} 个），至少需要 3 个案例才能做肘部分析")
    # KMeans 要求 k <= n_samples，且实际有意义的最大 K 为 n-1
    k_max = min(k_max, n - 1)
    k_min = max(2, min(k_min, k_max))
    _, features, X_scaled, _, _ = _prepare(df, features)

    rows = []
    for k in range(k_min, k_max + 1):
        if k >= n:
            break
        km = KMeans(n_clusters=k, random_state=RANDOM_STATE, n_init=20)
        km.fit(X_scaled)
        rows.append({"K": k, "SSE": float(km.inertia_)})
    return pd.DataFrame(rows), features


def run_kmeans(df: pd.DataFrame, features: List[str], k: int):
    """KMeans 聚类，返回带 cluster 列的表 + 中心表"""
    df2, features, X_scaled, scaler, imputer = _prepare(df, features)

    km = KMeans(n_clusters=k, random_state=RANDOM_STATE, n_init=20)
    labels = km.fit_predict(X_scaled)
    # 类别编号从 1 开始（0 对人不直观）；整体 +1，中心行号、cases 查询、
    # 导出 CSV、PCA 图全部保持同一套编号，不存在显示与数据错位。
    labels = labels + 1
    df2["cluster"] = labels

    # 聚类中心：还原到原始指标尺度，便于阅读
    centers_raw = pd.DataFrame(
        scaler.inverse_transform(km.cluster_centers_),
        columns=features,
    )
    centers_raw["cluster"] = range(1, k + 1)
    centers_raw["count"] = [int((labels == i).sum()) for i in range(1, k + 1)]

    return df2, centers_raw, features


def cluster_cases(df_with_cluster: pd.DataFrame, features: List[str],
                   cluster_id: int, top_n: int = 10):
    """
    找到离聚类中心最近的典型案例（在标准化空间算欧氏距离，与 KMeans 一致）
    """
    features = [f for f in features if f in df_with_cluster.columns]
    sub = df_with_cluster[df_with_cluster["cluster"] == cluster_id].copy()
    if sub.empty:
        return pd.DataFrame(), pd.DataFrame()

    all_df, features, X_scaled, scaler, _ = _prepare(
        df_with_cluster[features].assign(
            levels_imputed=df_with_cluster.get("levels_imputed", 0)
        ), features
    )
    scaled_all = pd.DataFrame(X_scaled, columns=features, index=df_with_cluster.index)
    scaled_sub = scaled_all.loc[sub.index]

    centroid = scaled_sub.mean()
    dist = np.sqrt(((scaled_sub - centroid) ** 2).sum(axis=1))
    sub["distance_to_center"] = dist.values
    sub = sub.sort_values("distance_to_center")

    keep = [c for c in ["Parcel_ID", "name", "cluster", "distance_to_center",
                        "FAR", "BCR", "avg_levels", "building_count"]
            if c in sub.columns]
    return sub.sort_values("distance_to_center"), sub[keep].head(top_n)


def pca_2d(df: pd.DataFrame, features: List[str]):
    """PCA 二维投影数据"""
    features = [f for f in features if f in df.columns]
    X = df[features].apply(pd.to_numeric, errors="coerce")
    X = X.fillna(X.median(numeric_only=True))
    X_scaled = StandardScaler().fit_transform(X)
    pca = PCA(n_components=2)
    coords = pca.fit_transform(X_scaled)
    return {
        "pc1": coords[:, 0].tolist(),
        "pc2": coords[:, 1].tolist(),
        "explained": [float(v) for v in pca.explained_variance_ratio_],
        "features": features,
    }
