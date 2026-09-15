# 深圳住区形态分析工具

> 面向建筑 / 城乡规划专业的住区形态指标计算与筛查工具。把原本要在 ArcGIS 里手动完成的「取图 → 看属性 → 筛选 → 算指标」压缩成一条在浏览器里连续跑完的工作流，并附带人工复查、聚类分析与科研图输出。

[English](README_EN.md)

## 为什么做这个工具

传统做法一般用 ArcGIS 做住区形态指标的计算，但有两个反复出现的痛点：

1. **地图数据获取范围受限。** 从 OpenStreetMap 取数据时，每次导出往往只能覆盖很小一块范围，想做全市尺度的样本就得反复切分、拼接。
2. **筛选要靠在 GIS 里先处理。** 如果只关心「居住地块」，必须先把数据导进 ArcGIS、靠字段属性才能筛出来，还要再做一轮图层处理。

这个工具把「地图处理」和「数据初筛」的时间省掉了：直接在浏览器里框选范围、点选地块，指标就自动算出来；并且把**数据处理、人工复查、聚类分析、科研图纸输出**一并包了进来，整体能把前期准备时间压下来。

## 部署说明

- **放到 GitHub（代码仓库）**：本仓库就是完整源码，任何人 `git clone` 后按「快速开始」即可本地运行。
- **一个「打开就能用」的公网链接**：GitHub 本身只能托管代码，不能运行 Python 后端；GitHub Pages 也只能托管静态前端，而本工具的计算依赖后端服务，因此纯 Pages 部署会出现「连不上引擎」。要让他人无需本地配置就能打开，需要能运行 Python 的托管服务。代码照常放在 GitHub，托管服务拉取仓库启动即可。
- **在线演示（临时托管）**：https://c4665e1afb7b4180ada3932500d31ac3.app.workbuddy.host —— 由 WorkBuddy 临时托管，打开即用、无需本地配置。

## 功能特性

- **交互式选地**：地图框选 / 点选收集住区案例，支持全域、高密度分区、自定义边界三种范围模式。
- **自动指标计算**：基于 OSM 建筑与路网，计算容积率（FAR）、建筑密度（BCR）、平均 / 最高层数等经济技术指标。
- **数据体检**：自动标记异常（BCR>1、FAR 异常、层数覆盖率过低等），可逐条查看、勾选剔除。
- **人工修正**：原表只读对照，修正表可双击编辑层数 / 容积率，FAR 自动反算。
- **条件筛选**：单条件 / 多条件组合 / 区间，并可选择用原表还是修正表作为筛选数据源。
- **聚类分析**：自选指标 → 肘部法选 K → KMeans 聚类 → 输出聚类中心。
- **科研图输出**：PCA 主成分图、箱线图等可直接用于论文。
- **离线矢量底图**：用 OSM 原始数据自制的 `shenzhen.pmtiles`，缩放清晰、断网可用，不依赖任何在线瓦片服务。

## 工作流

界面左侧 7 步，逐级解锁：

| 步骤 | 内容 |
|---|---|
| 1 选择范围 | 全域 / 高密度 / 自定义 |
| 2 点选地块 | 地图交互收集案例 |
| 3 指标与异常 | 自动计算指标 + 数据体检 |
| 4 条件筛选 | 单 / 多条件、区间 |
| 5 成果输出 | 指标表 + 平面示意图 |
| 6 聚类分析 | 肘部法 → KMeans → 中心 |
| 7 科研图输出 | PCA + 箱线图 |

<img width="1273" height="701" alt="1 选择范围" src="https://github.com/user-attachments/assets/4c8b7e77-1ba9-434f-a019-7918133a2219" />
<img width="1271" height="701" alt="2 选择地块" src="https://github.com/user-attachments/assets/cb3f1a78-a66e-436f-ab55-9db267519ae5" />
<img width="1271" height="697" alt="3 指标检查" src="https://github.com/user-attachments/assets/c4252599-f754-4dfb-a629-6f132f1a8ed9" />
<img width="1273" height="700" alt="4 案例筛选" src="https://github.com/user-attachments/assets/cde8513b-df32-40f2-bf83-3e81ec394133" />
<img width="1269" height="699" alt="5 成果核验" src="https://github.com/user-attachments/assets/3f02bd18-25fe-44b8-a596-4b2ddbe8194d" />
<img width="1270" height="700" alt="6 聚类分析" src="https://github.com/user-attachments/assets/167f923d-f87c-4213-9112-383c108842cc" />
<img width="1270" height="696" alt="7 科研图输出" src="https://github.com/user-attachments/assets/97243811-66e6-4776-a397-2561fc893dae" />

## 技术栈

- 后端：Python + FastAPI（指标计算、筛选、聚类、出图，封装为 HTTP 接口）
- 前端：原生 HTML / CSS / JavaScript + Leaflet（地图与矢量底图，全部本地化，不依赖 CDN）
- 数据处理：geopandas / shapely / osmnx / pandas / scikit-learn / matplotlib

## 快速开始（本地运行）

环境要求：Python 3.9+，首次取数需联网（之后走本地缓存）。

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 启动计算引擎（默认 8765 端口）
cd server
python -m uvicorn app:app --host 0.0.0.0 --port 8765
```

浏览器打开 `http://127.0.0.1:8765` 即可使用。
（Windows 用户若本地有 `start.bat`，双击亦可。）

## 目录结构

```
residential-tool/
├── README.md                本文档（中文）
├── README_EN.md             英文说明
├── requirements.txt         依赖清单
├── LICENSE                  MIT 许可证
├── .gitignore
├── server/                  本地计算引擎（FastAPI）
│   ├── app.py              路由 + 进度上报 + 指标/筛选/聚类接口
│   ├── config.py           配置与路径
│   ├── osm.py              Overpass 取数 + 缓存 + 代理
│   ├── metrics.py          经济技术指标 + 异常检测
│   ├── plots.py            平面图 + 科研图
│   ├── analysis.py         肘部法 / KMeans / PCA
│   └── localidx.py         本地建筑 / 路网索引
├── web/                     前端（纯静态，可单独托管）
│   ├── index.html
│   ├── app.js              入口
│   ├── css/style.css
│   ├── js/app.js           主逻辑
│   └── vendor/             Leaflet + protomaps（本地化，不依赖 CDN）
└── data/
    ├── basemap/
    │   └── shenzhen.pmtiles   离线矢量底图（单文件）
    └── density_zone_mask.json 高密度分区掩膜
```

> `data/cache/`、`data/output/`、`data/basemap/raw/` 为运行时生成的大文件，已在 `.gitignore` 中排除，不会进入仓库。


## 数据来源与合规

地图与建筑 / 路网数据来自 OpenStreetMap（ODbL 协议）。离线底图是用 OSM 原始矢量数据自行切片生成（`shenzhen.pmtiles`），符合 OSM 的 Tile Usage Policy，未使用受限的在线瓦片服务。

## 许可证

[MIT](LICENSE)
