# -*- coding: utf-8 -*-
"""
全局配置与路径管理
"""
import os

# ---------- 路径 ----------
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")
CACHE_DIR = os.path.join(DATA_DIR, "cache")
OUTPUT_DIR = os.path.join(DATA_DIR, "output")
WEB_DIR = os.path.join(BASE_DIR, "web")

for _d in (CACHE_DIR, OUTPUT_DIR):
    os.makedirs(_d, exist_ok=True)

# ---------- 坐标系 ----------
CRS_WGS84 = "EPSG:4326"       # 显示 / 存储
CRS_PROJECTED = "EPSG:4547"   # 深圳投影坐标，用于面积计算

# ---------- 默认面积阈值（㎡）----------
DEFAULT_MIN_AREA = 10_000     # 1 公顷
DEFAULT_MAX_AREA = 500_000    # 50 公顷

# ---------- 深圳整体范围（全域模式 / 掩膜取数范围）----------
# (minx, miny, maxx, maxy)，含深圳全部行政区及少量邻界
SHENZHEN_BBOX = (113.75, 22.40, 114.65, 22.90)

# ---------- 建筑面积折减系数 ----------
# OSM 的建筑轮廓是「建筑最大投影」，包含架空层、阳台、裙楼挑檐等；
# 而官方口径的建筑面积是计容面积，标准层通常小于底层轮廓。
# 直接用 Σ(基底 × 层数) 会系统性高估。
#
# 标定依据（半岛城邦一期，建筑识别完整的样本）：
#   OSM 算得  Σ(基底×层数) = 178,420 ㎡
#   官方公布  建筑面积      = 142,968 ㎡   → 比值 0.801
# 取 0.85 作为保守默认值（单点标定，避免过度拟合）。
# 想还原「纯几何口径」就把它设成 1.0。
FLOOR_AREA_SHRINK = 0.85

# ---------- 缓存版本号 ----------
# 取数逻辑一旦调整就递增，避免误用旧逻辑生成的缓存文件
# v4：_elements_to_gdf 开始保留 building:levels / height（修复层数全靠经验推断）
CACHE_VERSION = "v4"

# ---------- 深圳行政区（中文名 -> Overpass area 查询名）----------
SHENZHEN_DISTRICTS = {
    "福田区": "福田区",
    "罗湖区": "罗湖区",
    "南山区": "南山区",
    "宝安区": "宝安区",
    "龙岗区": "龙岗区",
    "龙华区": "龙华区",
    "盐田区": "盐田区",
    "光明区": "光明区",
    "坪山区": "坪山区",
    "大鹏新区": "大鹏新区",
}

# ---------- 道路等级 -> 典型宽度（米），沿用 stage2_5.1 ----------
ROAD_WIDTH_MAP = {
    "motorway": 40,
    "trunk": 36,
    "primary": 30,
    "secondary": 24,
    "tertiary": 18,
    "residential": 12,
    "service": 8,
}

# ---------- 默认高密度掩膜路径（可在网页覆盖）----------
# 优先用项目内副本（本地 / 云端部署都可用），不存在时回退到桌面原文件
_MASK_IN_PROJECT = os.path.join(DATA_DIR, "density_zone_mask.json")
DEFAULT_MASK_PATH = (_MASK_IN_PROJECT if os.path.exists(_MASK_IN_PROJECT)
                     else r"C:/Users/x/Desktop/Be_careful/density_zone_mask.json")

# ---------- 前端静态站点是否允许跨域访问本地引擎 ----------
CORS_ORIGINS = ["*"]


# ---------- 本机代理（可选）----------
# 什么时候需要：如果右上角「网络自检」显示 4 个 Overpass 镜像全部不可达，
# 而你本机有 Clash / V2Ray 之类代理，就在 data/proxy.txt 里写一行代理地址，
# 例如： http://127.0.0.1:7897
# 然后重启引擎（重新双击 start.bat）即可。
def _load_proxy() -> str:
    import os as _os
    for k in ("HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy"):
        v = _os.environ.get(k)
        if v:
            return v
    f = os.path.join(DATA_DIR, "proxy.txt")
    if os.path.exists(f):
        try:
            for line in open(f, encoding="utf-8"):
                line = line.strip()
                if line and not line.startswith("#"):
                    return line
        except Exception:  # noqa: BLE001
            pass
    return ""


PROXY = _load_proxy()
