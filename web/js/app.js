/* ============================================================
   深圳住区形态分析工具 —— 前端控制台
   ============================================================ */
"use strict";

/* ---------------- 全局状态 ---------------- */
// 引擎地址解析：
//  · 部署版（https://xxx.app.workbuddy.host 等任何 http/https 非本机域名）→ 同源，开箱即用
//  · 本地打开（localhost / 127.0.0.1 / file://）→ 默认本机引擎 127.0.0.1:8765
//  · 用户在「引擎设置」里手动填过 → 尊重手动配置（localStorage）
function defaultApiBase() {
  const saved = localStorage.getItem("engineUrl");
  const h = location.hostname;
  const isLocal = location.protocol === "file:" ||
                  h === "localhost" || h === "127.0.0.1" || h === "";
  if (saved) {
    // 自愈：部署版（非本机域名）下，如果 localStorage 里存的是旧版默认的
    // 127.0.0.1:8765，会导致永远连不上（指向打开网页的设备自己），直接忽略它
    const savedIsLocalAddr = /^https?:\/\/(127\.0\.0\.1|localhost)(:\d+)?\/?$/.test(saved);
    if (!isLocal && savedIsLocalAddr) {
      localStorage.removeItem("engineUrl");
    } else {
      return saved;
    }
  }
  return isLocal ? "http://127.0.0.1:8765" : location.origin;
}
const S = {
  api: defaultApiBase(),
  online: false,
  step: 1,
  maxStep: 1,

  mode: "density",
  districts: [],
  boundaryUploaded: false,

  parcels: [],            // 轻量列表
  geojson: null,          // 地块几何
  maskGeojson: null,
  map: null,
  parcelLayer: null,
  maskLayer: null,
  picked: new Set(),      // 已选 Parcel_ID
  layerByPid: {},

  metrics: [],            // 全量指标
  removed: new Set(),     // 手动剔除的 Parcel_ID
  filtered: [],           // 筛选结果
  farMode: "shrunk",      // FAR 口径：shrunk 近似计容 / geometric 几何

  features: [],           // 聚类指标
  clusterResult: null,
  confirmed: [],          // 第4步确认的 Parcel_ID
  planItems: [],
};

const $  = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));
const fmt = (n, d = 0) =>
  n === null || n === undefined || isNaN(n) ? "—"
  : Number(n).toLocaleString("zh-CN", { minimumFractionDigits: d, maximumFractionDigits: d });

/* ---------------- 指标元数据 ---------------- */
const FIELD_META = [
  ["FAR",                    "容积率 FAR",        "总建筑面积 / 地块面积，衡量开发强度（口径随「容积率口径」设置）"],
  ["far_geometric",          "容积率（几何）",     "未折减口径：Σ基底×层数 / 用地，恒为几何值，不随口径设置变化"],
  ["BCR",                    "建筑密度 BCR",      "建筑基底面积 / 地块面积"],
  ["avg_levels",             "平均层数",          "所有建筑层数的平均值"],
  ["max_levels",             "最大层数",          "地块内最高建筑的层数"],
  ["levels_std",             "层数标准差",        "层数离散程度，反映高度混合度"],
  ["highrise_ratio",         "高层占比",          "≥18 层建筑所占比例"],
  ["building_count",         "建筑数量",          "地块内建筑栋数"],
  ["parcel_area_sqm",        "地块面积",          "地块用地面积（㎡）"],
  ["compactness",            "地块紧凑度",        "4πA/P²，越接近 1 越规整"],
  ["nearest_neighbor_mean",  "平均最近邻距离",    "建筑间平均间距，反映疏密"],
  ["nearest_neighbor_std",   "最近邻距离标准差",  "间距的均匀程度"],
  ["orientation_std",        "朝向标准差",        "建筑朝向一致性"],
  ["elongation_mean",        "平均长宽比",        "建筑平面狭长程度"],
  ["elongation_std",         "长宽比标准差",      "建筑形体差异度"],
  ["footprint_std",          "基底面积标准差",    "建筑规模差异"],
  ["footprint_cv",           "基底面积变异系数",  "建筑规模相对离散度"],
];
const RECOMMEND_FEATURES = ["FAR", "nearest_neighbor_mean", "orientation_std",
                            "elongation_mean", "levels_std"];

const OPERATORS = [
  [">", "大于 >"], [">=", "大于等于 ≥"], ["<", "小于 <"],
  ["<=", "小于等于 ≤"], ["between", "区间 between"], ["==", "等于 ="],
];

/* ============================================================
   API
   ============================================================ */
async function api(path, opts = {}) {
  const url = S.api.replace(/\/$/, "") + path;
  const res = await fetch(url, {
    headers: opts.body instanceof FormData ? {} : { "Content-Type": "application/json" },
    ...opts,
  });
  if (!res.ok) {
    let msg = res.statusText;
    try { const j = await res.json(); msg = j.detail || msg; } catch (e) {}
    throw new Error(msg);
  }
  const ct = res.headers.get("content-type") || "";
  if (ct.includes("application/json")) return res.json();
  return res;
}

const apiUrl = p => S.api.replace(/\/$/, "") + p;

/* ============================================================
   UI 工具
   ============================================================ */
function overlay(on, title, sub) {
  const el = $("#overlay");
  el.classList.toggle("on", on);
  if (title) $("#ovTitle").textContent = title;
  if (sub !== undefined) $("#ovSub").textContent = sub || "";
  if (!on) { stopProgress(); hideBar(); }
}
const setSub = t => { $("#ovSub").textContent = t; };

/* ---------------- 真实进度条 ---------------- */
function showBar() {
  $("#ovBarWrap").style.display = "";
  $("#ovPct").style.display = "";
}
function hideBar() {
  $("#ovBarWrap").style.display = "none";
  $("#ovPct").style.display = "none";
  $("#ovBarWrap").classList.remove("indet");
  $("#ovBar").style.width = "0%";
}
function setBar(pct) {
  const wrap = $("#ovBarWrap"), bar = $("#ovBar");
  if (pct === null || pct === undefined || isNaN(pct)) {
    wrap.classList.add("indet");
    $("#ovPct").textContent = "处理中…";
  } else {
    wrap.classList.remove("indet");
    bar.style.width = Math.max(0, Math.min(100, pct)) + "%";
    $("#ovPct").textContent = Math.round(pct) + " %";
  }
}

let _progTimer = null;
function stopProgress() {
  if (_progTimer) { clearInterval(_progTimer); _progTimer = null; }
}
/* 从引擎拉取真实进度（百分比 + 当前步骤 + 日志） */
async function pollProgress() {
  if (!S.online) return;
  try {
    const p = await api("/api/progress");
    if (p.log && p.log.length) pushLog(p.log);
    if (p.msg) $("#ovSub").textContent = p.msg;
    setBar(typeof p.pct === "number" ? p.pct : null);
  } catch (e) {}
}
/* 启动带进度条的长任务遮罩 */
function startProgress(title, sub) {
  overlay(true, title, sub || "");
  showBar();
  setBar(null);
  stopProgress();
  _progTimer = setInterval(pollProgress, 700);
  pollProgress();
}

function toast(msg, kind) {
  const d = document.createElement("div");
  d.textContent = msg;
  const ok = kind !== "err";
  d.style.cssText = `position:fixed;bottom:24px;left:50%;transform:translateX(-50%);
    z-index:9999;padding:10px 20px;font-size:12.5px;font-weight:600;border-radius:999px;
    background:${ok ? "rgba(255,255,255,.16)" : "rgba(255,59,48,.30)"};
    color:${ok ? "#f5f5f7" : "#ff9d95"};
    border:1px solid ${ok ? "rgba(255,255,255,.24)" : "rgba(255,157,149,.45)"};
    -webkit-backdrop-filter:blur(18px) saturate(160%);backdrop-filter:blur(18px) saturate(160%);
    box-shadow:0 8px 24px rgba(0,0,0,.45), inset 0 1px 0 rgba(255,255,255,.16);`;
  document.body.appendChild(d);
  setTimeout(() => d.remove(), 2600);
}

function pushLog(lines) {
  const box = $("#logBox");
  box.textContent = lines.join("\n");
  box.scrollTop = box.scrollHeight;
}

/* ---------------- 步骤导航 ---------------- */
function goStep(n) {
  if (n > S.maxStep) return;
  S.step = n;
  $$(".panel").forEach(p => p.classList.toggle("active", +p.dataset.panel === n));
  $$(".step").forEach(s => {
    const i = +s.dataset.step;
    s.classList.toggle("active", i === n);
    s.classList.toggle("done", i < n || (i === n && i < S.step));
    s.classList.toggle("locked", i > S.maxStep);
  });
  $(".main").scrollTop = 0;
  if (n === 2 && S.map) setTimeout(() => S.map.invalidateSize(), 60);
  // 从左侧栏直接进入 Step 4 时也要保证至少有一条条件行，
  // 否则点「执行筛选」会因为没有条件而看起来像卡住
  if (n === 4) { try { renderConditions(); } catch (e) {} }
}
const unlock = n => { S.maxStep = Math.max(S.maxStep, n); goStep(n); };

$$(".step").forEach(s => s.onclick = () => {
  const i = +s.dataset.step;
  if (i <= S.maxStep) goStep(i);
});

/* ============================================================
   引擎连接
   ============================================================ */
async function checkEngine() {
  try {
    const h = await api("/api/health");
    S.online = true;
    $("#connDot").className = "dot on";
    $("#connText").textContent = "本地引擎已连接";
    renderDistricts(h.districts || []);
    if (h.default_mask) $("#maskPath").value = h.default_mask;
    return true;
  } catch (e) {
    S.online = false;
    $("#connDot").className = "dot off";
    $("#connText").textContent = "未连接本地引擎";
    return false;
  }
}

async function pollLog() {
  if (!S.online) return;
  try {
    const r = await api("/api/log");
    if (r.log && r.log.length) pushLog(r.log);
  } catch (e) {}
}

$("#btnEngineCfg").onclick = () => {
  $("#engineUrl").value = S.api;
  $("#cfgOverlay").classList.add("on");
};
$("#btnCfgClose").onclick = () => $("#cfgOverlay").classList.remove("on");

/* 网络自检：逐个测 Overpass 镜像，判断是网络问题还是服务器繁忙 */
$("#btnNetTest").onclick = async () => {
  toast("正在测试 Overpass 连通性…");
  try {
    const r = await api("/api/net/test");
    console.group("Overpass 网络自检");
    r.results.forEach(x => console.log(
      `${x.ok ? "✅" : "❌"} ${x.host}  ${x.seconds}s  ${x.note}` +
      (x.ways != null ? `  (${x.ways} 条建筑)` : "")));
    console.log("结论:", r.advice);
    console.groupEnd();
    const lines = r.results.map(x =>
      `${x.ok ? "✅" : "❌"} ${x.host}  ${x.seconds}s  ${x.note}`).join("\n");
    alert(`Overpass 网络自检  (${r.available}/${r.total} 可用)\n\n` +
          lines + `\n\n${r.advice}`);
    toast(r.ok ? `网络正常（${r.available}/${r.total} 可用）` : "所有镜像不可达，请稍后重试",
          r.ok ? "ok" : "err");
  } catch (e) {
    toast("自检失败：引擎未连接", "err");
  }
};

$("#btnCfgSave").onclick = async () => {
  S.api = $("#engineUrl").value.trim().replace(/\/$/, "");
  localStorage.setItem("engineUrl", S.api);
  const ok = await checkEngine();
  $("#cfgOverlay").classList.remove("on");
  toast(ok ? "引擎连接成功" : "连接失败，请确认已运行 start.bat", ok ? "ok" : "err");
};

/* ============================================================
   STEP 1 · 范围
   ============================================================ */
$$(".mode").forEach(m => m.onclick = () => {
  $$(".mode").forEach(x => x.classList.remove("sel"));
  m.classList.add("sel");
  S.mode = m.dataset.mode;
  $("#blkDensity").style.display = S.mode === "density" ? "" : "none";
  $("#blkCustom").style.display  = S.mode === "custom"  ? "" : "none";
});

function renderDistricts(list) {
  const g = $("#districtGrid");
  g.innerHTML = "";
  list.forEach(name => {
    const lab = document.createElement("label");
    lab.className = "feat";
    lab.innerHTML = `<input type="checkbox" value="${name}">
      <div><div class="fn">${name}</div></div>`;
    lab.querySelector("input").onchange = e => {
      if (e.target.checked) S.districts.push(name);
      else S.districts = S.districts.filter(d => d !== name);
      lab.classList.toggle("on", e.target.checked);
    };
    g.appendChild(lab);
  });
}

$("#boundaryFile").onchange = async e => {
  const f = e.target.files[0];
  if (!f) return;
  const fd = new FormData();
  fd.append("file", f);
  overlay(true, "解析边界文件", f.name);
  try {
    const r = await api("/api/scope/upload-boundary", { method: "POST", body: fd });
    S.boundaryUploaded = true;
    $("#boundaryInfo").innerHTML =
      `<span class="badge ok">已接收</span> 共 ${r.count} 个要素，边界范围
       [${r.bounds.map(v => v.toFixed(3)).join(", ")}]`;
    toast("边界文件解析成功");
  } catch (err) {
    $("#boundaryInfo").innerHTML = `<span class="badge danger">失败</span> ${err.message}`;
  } finally { overlay(false); }
};

$("#btnLoadParcels").onclick = async () => {
  if (!S.online && !(await checkEngine())) {
    toast("请先启动本地引擎（双击 start.bat）", "err");
    return;
  }
  if (S.mode === "custom" && !S.boundaryUploaded && S.districts.length === 0) {
    toast("自定义模式需上传边界文件或勾选行政区", "err");
    return;
  }

  startProgress("正在加载候选地块",
    S.mode === "global"
      ? "深圳全域：首次下载约 30 秒，之后走本地缓存秒开…"
      : "正在下载 OSM 数据…");

  try {
    const body = {
      mode: S.mode,
      districts: S.districts,
      mask_path: $("#maskPath").value.trim() || null,
      min_area: +$("#minArea").value,
      max_area: +$("#maxArea").value,
      use_cache: $("#useCache").value === "true",
    };
    const r = await api("/api/scope/prepare", { method: "POST", body: JSON.stringify(body) });

    S.parcels = r.parcels;
    S.geojson = r.geojson;
    S.picked = new Set();
    S.metrics = [];
    S.filtered = [];
    S.removed = new Set();

    $("#s2Total").textContent = fmt(r.count);
    $("#s2Scope").textContent = r.label;
    $("#lgMask").style.display = r.mask_exists ? "" : "none";

    renderMap();
    updatePickStats();
    unlock(2);
    toast(`已加载 ${r.count} 个候选地块`);
  } catch (e) {
    toast("加载失败：" + e.message, "err");
  } finally {
    overlay(false);
  }
};

/* ============================================================
   STEP 2 · 地图点选
   ============================================================ */
/* ---------- 底图方案 ----------
   ① 本地矢量底图（默认，离线可用）
      读 data/basemap/shenzhen.pmtiles —— 由 OSM 原始矢量数据自己渲染而成，
      符合 OSM Tile Usage Policy（不下瓦片、下数据自建地图服务）。
   ② 在线栅格兜底：CARTO 浅色 / CARTO 彩色 / OSM 街道
   ③ 无底图（纯矢量）

   刻意不用高德 / 腾讯底图（GCJ-02 偏移会让地块整体错位几百米），
   也不用 Esri（国内建筑、路网细节很差）。 */
/* 活动范围 ≈ 离线底图的数据范围（113.75–114.65°E / 22.40–22.90°N）外留一点余量，
   这样拖动不会跑到「没有数据」的空白区 */
const SHENZHEN_BOUNDS = L.latLngBounds([22.38, 113.72], [22.92, 114.68]);
const MAP_MIN_ZOOM = 9;
const MAP_MAX_ZOOM = 19;

const LOCAL_PMTILES_URL = "/basemap/shenzhen.pmtiles";
let _localMaxDataZoom = 13;            // 由 /api/basemap/info 按实际文件覆盖

/* 本地矢量底图配色：基于 Protomaps LIGHT，调成「淡蓝绿小清新」 */
const LOCAL_FLAVOR = {
  background: "#f2efe9", earth: "#f2efe9",
  park_a: "#cfddd5", park_b: "#b9dcc8",
  hospital: "#e4dad9", industrial: "#dbe6ea", school: "#e4ded7",
  wood_a: "#d0ded0", wood_b: "#c2dcc2",
  pedestrian: "#e6e3d8",
  scrub_a: "#d3e0d8", scrub_b: "#bfd9cd",
  sand: "#e2e0d7", beach: "#e8e4d0",
  aerodrome: "#dadbdf", runway: "#e9e9ed",
  water: "#a8dbe8", zoo: "#c6dcdc", military: "#e6e6e6",
  tunnel_other_casing: "#dfe4e4", tunnel_minor_casing: "#dfe4e4",
  tunnel_link_casing: "#dfe4e4", tunnel_major_casing: "#dfe4e4",
  tunnel_highway_casing: "#dfe4e4",
  tunnel_other: "#d5d9d9", tunnel_minor: "#d5d9d9", tunnel_link: "#d5d9d9",
  tunnel_major: "#d5d9d9", tunnel_highway: "#d5d9d9",
  pier: "#e0e4e4", buildings: "#d8d4cc",
  minor_service_casing: "#e4eaea", minor_casing: "#e4eaea", link_casing: "#e4eaea",
  major_casing_late: "#e4eaea", highway_casing_late: "#e4eaea",
  other: "#ededed", minor_service: "#ededed",
  minor_a: "#f7f7f7", minor_b: "#ffffff", link: "#ffffff",
  major_casing_early: "#e4eaea", major: "#ffffff",
  highway_casing_early: "#e4eaea", highway: "#ffffff",
  railway: "#a7b1b3", boundaries: "#b0b0b0",
  bridges_other_casing: "#dfe4e4", bridges_minor_casing: "#dfe4e4",
  bridges_link_casing: "#dfe4e4", bridges_major_casing: "#dfe4e4",
  bridges_highway_casing: "#dfe4e4",
  bridges_other: "#ededed", bridges_minor: "#ffffff", bridges_link: "#ffffff",
  bridges_major: "#f7f7f7", bridges_highway: "#ffffff",
  roads_label_minor: "#91888b", roads_label_minor_halo: "#ffffff",
  roads_label_major: "#938a8d", roads_label_major_halo: "#ffffff",
  ocean_label: "#5a9bb0",
  subplace_label: "#8f8f8f", subplace_label_halo: "#e0e0e0",
  city_label: "#5c5c5c", city_label_halo: "#e0e0e0",
  state_label: "#b3b3b3", state_label_halo: "#e0e0e0",
  country_label: "#a3a3a3",
  address_label: "#91888b", address_label_halo: "#ffffff",
  blue: "#1A8CBD", green: "#20834D", lapis: "#315BCF", pink: "#EF56BA",
  red: "#F2567A", slategray: "#6A5B8F", tangerine: "#CB6704", turquoise: "#00C3D4",
  grassland: "rgba(210,239,207,1)", barren: "rgba(255,243,215,1)",
  urban_area: "rgba(230,230,230,1)", farmland: "rgba(216,239,210,1)",
  glacier: "rgba(255,255,255,1)", scrub: "rgba(234,239,210,1)",
  forest: "rgba(196,231,210,1)",
};

/* 在线栅格兜底源（均为 WGS84 / Web Mercator，与地块坐标系一致）
   注意：CARTO 已要求 API key，返回 HTTP 200 的「API KEY REQUIRED」错误图，
   不触发 tileerror 也没法检测 —— 已从回退链中移除，只剩 OSM 可靠。 */
const TILE_PROVIDERS = {
  "osm": { url: "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
    opts: { maxZoom: MAP_MAX_ZOOM, attribution: "© OpenStreetMap contributors" } },
  "carto-light": { url: "https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}.png",
    opts: { maxZoom: MAP_MAX_ZOOM, subdomains: "abcd",
            attribution: "© OpenStreetMap © CARTO" } },
};

/* 降级顺序：本地 → OSM → 纯矢量 */
const BASEMAP_ORDER = ["local", "osm", "none"];
const BASEMAP_LABEL = { local: "本地矢量（离线）", osm: "OSM 街道（在线）",
  "carto-light": "CARTO（需 key，已弃用）", none: "无底图" };

let _tileKey = localStorage.getItem("basemap") || "local";
let _tileLayer = null;
let _localOk = null;          // 本地 pmtiles 是否可用（缓存）
let _localHint = "";

async function checkLocalBasemap() {
  if (_localOk !== null) return _localOk;
  try {
    const j = await (await fetch("/api/basemap/info")).json();
    _localOk = !!j.ok;
    _localHint = j.hint || "";
    if (j.max_zoom) _localMaxDataZoom = j.max_zoom;   // 与文件实际范围保持一致
  } catch (e) {
    _localOk = false;
  }
  return _localOk;
}

function makeLocalVectorLayer() {
  if (!window.protomapsL) throw new Error("矢量底图组件未加载");
  /* 注意：当前 vendored 库的 leafletLayer 构造函数里，flavor 参数只认
     字符串（light/dark/white/grayscale/black），传自定义对象会抛
     "Flavor not found"。自定义配色必须先转成 paintRules/labelRules。 */
  const custom = {
    paintRules: protomapsL.paintRules(LOCAL_FLAVOR),
    labelRules: protomapsL.labelRules(LOCAL_FLAVOR, "zh"),
    backgroundColor: LOCAL_FLAVOR.background,
  };
  return protomapsL.leafletLayer({
    url: LOCAL_PMTILES_URL,
    ...custom,
    maxDataZoom: _localMaxDataZoom,
    maxZoom: MAP_MAX_ZOOM,
    bounds: SHENZHEN_BOUNDS,
    noWrap: true,
    attribution: '© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> 贡献者',
  });
}

function applyBasemap(key, map) {
  const m = map || S.map;
  if (!m) return;
  _tileKey = key;
  localStorage.setItem("basemap", key);
  if (_tileLayer) { try { m.removeLayer(_tileLayer); } catch (e) {} _tileLayer = null; }

  let idx = BASEMAP_ORDER.indexOf(key);
  if (idx < 0) idx = 0;

  /* 逐个源尝试；只有「连续失败且成功时不复位」才会掉到 none ——
     关键修复：成功的瓦片会重置计数，避免平移几次就误判成断网、最终掉光 */
  const tryAt = async (i) => {
    if (i >= BASEMAP_ORDER.length) return;
    const k = BASEMAP_ORDER[i];
    if (k === "none") { _tileLayer = null; return; }

    if (k === "local") {
      if (!(await checkLocalBasemap())) {
        if (i === idx) toast("本地底图尚未生成，已改用在线底图");
        return tryAt(i + 1);
      }
      let layer;
      try { layer = makeLocalVectorLayer(); }
      catch (e) { return tryAt(i + 1); }
      /* 本地底图不轻易放弃：初次 fitBounds 的大幅平移会取消一批进行中的
         矢量瓦片，产生成串 tileerror，曾被误判为「本地坏了」而切到在线源。
         现在：只要成功渲染过任意一块瓦片就坚持用本地；从未成功且错误
         堆积到 12 次才降级。在线源（CARTO）已开始要求 API key，回退
         拿到的常是错误瓦片图（HTTP 200 不触发 tileerror），反而更糟。 */
      let fails = 0, oks = 0, _downgraded = false;
      layer.on("tileerror", () => {
        if (++fails >= 12 && oks === 0 && !_downgraded) {
          _downgraded = true;
          try { m.removeLayer(layer); } catch (e) {}
          if (_tileLayer === layer) _tileLayer = null;
          if (i === idx) toast("本地底图读取异常，已改用在线底图");
          tryAt(i + 1);
        }
      });
      layer.on("tileload", () => { oks++; fails = 0; });
      layer.addTo(m);
      layer.bringToBack();
      _tileLayer = layer;
      return;
    }

    const p = TILE_PROVIDERS[k];
    if (!p) return tryAt(i + 1);
    const layer = L.tileLayer(p.url, { bounds: SHENZHEN_BOUNDS, ...p.opts });
    let fails = 0;
    layer.on("tileerror", () => {
      if (++fails >= 6 && i < BASEMAP_ORDER.length - 1) {
        try { m.removeLayer(layer); } catch (e) {}
        if (_tileLayer === layer) _tileLayer = null;
        if (i === idx) toast(`「${BASEMAP_LABEL[k] || k}」加载不畅，自动换源…`);
        tryAt(i + 1);
      }
    });
    layer.on("tileload", () => { fails = 0; });
    layer.addTo(m);
    layer.bringToBack();
    _tileLayer = layer;
  };

  tryAt(idx);
}

/* 底图切换器 + 重新加载按钮 */
(function initBasemapSel() {
  const sel = $("#tileSel");
  const wrap = $(".map-basemap");
  if (wrap && window.L) L.DomEvent.disableClickPropagation(wrap);
  if (sel) {
    sel.value = _tileKey;
    sel.onchange = () => {
      applyBasemap(sel.value);
      toast("底图已切换：" + (BASEMAP_LABEL[sel.value] || sel.value));
    };
  }
  const retry = $("#btnBasemapRetry");
  if (retry) {
    L.DomEvent.disableClickPropagation(retry);
    retry.onclick = () => {
      _localOk = null;                     // 重新探测本地底图
      applyBasemap(_tileKey);
      toast("正在重新加载底图…");
    };
  }
})();

function renderMap() {
  if (!S.map) {
    S.map = L.map("map", {
      zoomControl: true,
      // 只允许在深圳及周边活动，避免满世界拉瓦片
      maxBounds: SHENZHEN_BOUNDS,
      maxBoundsViscosity: 1.0,
      minZoom: MAP_MIN_ZOOM,
      maxZoom: MAP_MAX_ZOOM,
    }).setView([22.60, 114.05], 11);
    applyBasemap(_tileKey, S.map);
  }
  [S.parcelLayer, S.maskLayer].forEach(l => l && S.map.removeLayer(l));
  S.layerByPid = {};

  if (S.maskGeojson) {
    S.maskLayer = L.geoJSON(S.maskGeojson, {
      style: { color: "#d94f3d", weight: 1.6, fillColor: "#d94f3d",
               fillOpacity: 0.12, dashArray: "4 3" },
      interactive: false,
    }).addTo(S.map);
  }

  if (!S.geojson) return;

  // 同一时刻只保留一个悬停信息窗；5 秒后自动关闭（点击弹窗同理）
  let hoverTipLayer = null, hoverTipTimer = null;
  function showHoverTip(layer, html) {
    if (hoverTipLayer && hoverTipLayer !== layer) hoverTipLayer.closeTooltip();
    clearTimeout(hoverTipTimer);
    hoverTipLayer = layer;
    layer.bindTooltip(html, {
      direction: "top", offset: [0, -6], opacity: 0.97, className: "parcel-tip",
    }).openTooltip();
    hoverTipTimer = setTimeout(() => layer.closeTooltip(), 5000);
  }

  S.parcelLayer = L.geoJSON(S.geojson, {
    style: baseStyle,
    onEachFeature: (feat, layer) => {
      const p = feat.properties;
      S.layerByPid[p.Parcel_ID] = layer;

      const infoHtml = (picked) =>
        `<div style="font-size:12px;line-height:1.7;font-family:inherit">
           <b style="color:#2a7568">地块 ${p.Parcel_ID}</b><br>
           ${p.name || "未命名"}<br>
           面积 ${fmt(p.area_m2)} ㎡<br>
           ${picked
             ? '<span style="color:#2f7d5f;font-weight:600">✓ 已加入案例</span>'
             : '<span style="color:#9db3ad">点击选中 / 再点取消</span>'}
         </div>`;

      layer.on("mouseover", () => {
        if (!S.picked.has(p.Parcel_ID)) layer.setStyle({ weight: 1.6, fillOpacity: 0.30 });
        showHoverTip(layer, infoHtml(S.picked.has(p.Parcel_ID)));
      });
      layer.on("mouseout", () => layer.setStyle(baseStyle(feat)));

      layer.on("click", () => {
        const id = p.Parcel_ID;
        if (S.picked.has(id)) S.picked.delete(id);
        else S.picked.add(id);
        layer.setStyle(baseStyle(feat));
        // 点击后悬停窗就没用了，换成正式弹窗（同样 5 秒自关）
        clearTimeout(hoverTipTimer);
        layer.closeTooltip();
        layer.unbindPopup();
        layer.bindPopup(infoHtml(S.picked.has(id))).openPopup();
        clearTimeout(layer._popTimer);
        layer._popTimer = setTimeout(() => layer.closePopup(), 5000);
        updatePickStats();
      });
    },
  }).addTo(S.map);

  S.map.fitBounds(S.parcelLayer.getBounds().pad(0.05));
}

function baseStyle(feat) {
  const id = feat.properties.Parcel_ID;
  const on = S.picked.has(id);
  return {
    color: on ? "#d94f3d" : "#7a8f88",
    weight: 1,
    fillColor: on ? "#d94f3d" : "#FFC46B",
    fillOpacity: on ? 0.30 : 0.22,
  };
}

function repaint() {
  Object.entries(S.layerByPid).forEach(([pid, layer]) => {
    const feat = layer.feature;
    layer.setStyle(baseStyle(feat));
  });
}

function updatePickStats() {
  let area = 0;
  S.parcels.forEach(p => { if (S.picked.has(p.Parcel_ID)) area += p.area_m2 || 0; });
  $("#s2Picked").textContent = fmt(S.picked.size);
  $("#s2Area").innerHTML = fmt(area) + "<small>㎡</small>";
}

$("#btnPickAll").onclick = () => {
  if (!S.map || !S.parcelLayer) return;
  const b = S.map.getBounds();
  S.parcelLayer.eachLayer(l => {
    const id = l.feature.properties.Parcel_ID;
    if (b.contains(l.getBounds().getCenter())) S.picked.add(id);
  });
  repaint(); updatePickStats();
  toast(`视野内已全选，共 ${S.picked.size} 个`);
};

$("#btnPickNone").onclick = () => {
  S.picked.clear(); repaint(); updatePickStats();
};

$("#btnToStep3").onclick = async () => {
  if (S.picked.size === 0) { toast("请先在地图上点选至少一个地块", "err"); return; }
  startProgress("正在计算经济技术指标", `共 ${S.picked.size} 个案例，优先用本地建筑索引（不联网）…`);
  try {
    const ids = Array.from(S.picked);
    const r = await api("/api/metrics/compute", {
      method: "POST",
      body: JSON.stringify({ clip_buildings: true, parcel_ids: ids }),
    });
    const t = await api("/api/metrics/table");
    S.metrics = t.rows;
    S.farMode = t.far_mode;
    const b = await api("/api/metrics/table?source=baseline");
    S.baseline = b.rows;
    S.removed = new Set();
    await loadAdjusted();     // 重建原表 + 人工修正表（loadAdjusted 内部会 renderMetrics）
    unlock(3);

    const failed = r.failed || [];
    if (failed.length) {
      // 部分成功也要说清楚是哪几个、为什么
      const lines = failed.slice(0, 6)
        .map(f => `地块 ${f.Parcel_ID}（${f.name || "未命名"}）：${f.reason}`).join("\n");
      const more = failed.length > 6 ? `\n…另有 ${failed.length - 6} 个` : "";
      alert(`指标计算完成 ${r.count}/${r.requested} 个。\n` +
            `以下 ${failed.length} 个地块未能算出指标：\n\n${lines}${more}`);
      toast(`完成 ${r.count}/${r.requested}，${failed.length} 个无数据`, "err");
    } else {
      toast(`指标计算完成，共 ${t.count} 条`);
    }
  } catch (e) {
    toast("计算失败：" + e.message, "err");
  } finally {
    overlay(false);
  }
};

/* ============================================================
   STEP 3 · 指标与异常
   ============================================================ */
function activeMetrics() {
  return S.metrics.filter(r => !S.removed.has(r.Parcel_ID));
}

function renderMetrics() {
  // 指标表 = 自动计算原表（只读），与下方「指标表（人工修正）」对照
  const rows = (S.baseline || []).filter(r => !S.removed.has(r.Parcel_ID));
  const sev = rows.filter(r => r.anomaly_level === 2).length;
  const warn = rows.filter(r => r.anomaly_level === 1).length;
  const ok = rows.length - sev - warn;

  $("#s3Stats").innerHTML = `
    <div class="stat"><div class="k">案例总数</div><div class="v">${fmt(rows.length)}</div></div>
    <div class="stat"><div class="k">完全正常</div><div class="v">${fmt(ok)}</div></div>
    <div class="stat warn"><div class="k">需留意</div><div class="v">${fmt(warn)}</div></div>
    <div class="stat danger"><div class="k">严重异常</div><div class="v">${fmt(sev)}</div></div>
    <div class="stat"><div class="k">已剔除</div><div class="v">${fmt(S.removed.size)}</div></div>`;

  const note = $("#anomalyNote");
  if (sev > 0) {
    note.innerHTML = `<div class="note danger">
      <b>检测到 ${sev} 条严重异常。</b>
      最常见的是<b>建筑密度 BCR &gt; 1</b>——建筑轮廓超出地块边界，源于 OSM 边界识别误差。
      本工具已按地块边界对建筑做裁剪修正；若数值仍异常，建议直接剔除。
      其他异常包括层数缺失被补全、FAR 异常偏高、建筑数过少等。请逐条核对后决定去留。</div>`;
  } else if (warn > 0) {
    note.innerHTML = `<div class="note warn">
      有 ${warn} 条案例存在需留意的数据特征（黄色行）。可展开查看具体说明，确认无误后继续。</div>`;
  } else {
    note.innerHTML = `<div class="note">数据体检通过，未发现异常。</div>`;
  }

  const onlyAnom = $("#onlyAnomaly").checked;
  const show = onlyAnom ? rows.filter(r => r.anomaly_level > 0) : rows;

  const cols = ["parcel_area_sqm", "building_count", "BCR", "FAR", "far_geometric",
                "building_area_sqm", "avg_levels", "max_levels",
                "levels_coverage", "highrise_ratio"];
  const labels = {
    parcel_area_sqm: "地块面积㎡", building_count: "建筑数", BCR: "建筑密度",
    FAR: "容积率", far_geometric: "容积率(几何)",
    building_area_sqm: "总建筑面积㎡", avg_levels: "平均层数",
    max_levels: "最高层数", levels_coverage: "层数实测率", highrise_ratio: "高层占比",
  };
  const pct3 = new Set(["BCR", "FAR", "far_geometric", "highrise_ratio"]);

  let html = `<thead><tr><th style="width:30px"></th><th style="min-width:170px">案例</th>`;
  cols.forEach(c => html += `<th class="num">${labels[c]}</th>`);
  html += "</tr></thead><tbody>";

  show.forEach(r => {
    const cls = r.anomaly_level === 2 ? "row-danger" : r.anomaly_level === 1 ? "row-warn" : "";
    const badge = r.anomaly_level === 2
      ? '<span class="badge danger">严重</span>'
      : r.anomaly_level === 1
        ? '<span class="badge warn">留意</span>'
        : '<span class="badge ok">正常</span>';
    const impBadge = r.levels_imputed === 1
      ? '<span class="badge warn">层数补全</span>' : "";
    const covPct = r.levels_coverage != null ? Math.round(r.levels_coverage * 100) : null;

    html += `<tr class="${cls} pickable" data-id="${r.Parcel_ID}" title="点击展开说明">
      <td><input type="checkbox" class="rw"></td>
      <td>
        <div style="font-weight:600">${r.name || "未命名"}</div>
        <div style="margin-top:3px;display:flex;gap:5px;align-items:center;flex-wrap:wrap">
          <span style="font-size:11px;color:var(--ink-3)">#${r.Parcel_ID}</span>${badge}${impBadge}
        </div>
      </td>`;
    cols.forEach(c => {
      let v = r[c];
      if (c === "levels_coverage") v = covPct == null ? "—" : covPct + "%";
      else v = fmt(v, pct3.has(c) ? 3 : c === "avg_levels" ? 1 : 0);
      html += `<td class="num">${v}</td>`;
    });
    html += "</tr>";

    // 详情行：默认收起，点主行展开
    const detailBits = [
      ["最近邻距", fmt(r.nearest_neighbor_mean, 1) + " m"],
      ["朝向标准差", fmt(r.orientation_std, 1)],
      ["长宽比", fmt(r.elongation_mean, 2)],
      ["层数来源", r.levels_source || "—"],
    ].map(([k, v]) => `<span style="margin-right:16px"><b>${k}</b> ${v}</span>`).join("");
    html += `<tr class="detail-row" data-detail="${r.Parcel_ID}" style="display:none">
      <td></td><td colspan="${cols.length + 1}">
        <div style="font-size:12px;line-height:1.8;color:var(--ink-2);padding:4px 0">
          ${r.anomaly_desc
            ? `<div style="margin-bottom:6px"><b>体检说明：</b>${r.anomaly_desc}</div>`
            : `<div style="margin-bottom:6px;color:var(--ink-3)">无异常说明。</div>`}
          <div style="color:var(--ink-3)">${detailBits}</div>
        </div>
      </td></tr>`;
  });
  html += "</tbody>";
  $("#tblMetrics").innerHTML = html;
  $("#s3PickHint").textContent = `共 ${show.length} 行 · 点击行可展开说明`;

  // 行点击 = 展开/收起详情；复选框不触发
  $$("#tblMetrics tbody tr.pickable").forEach(tr => {
    tr.style.cursor = "pointer";
    tr.onclick = (e) => {
      if (e.target.closest("input.rw")) return;
      const d = $(`#tblMetrics tr.detail-row[data-detail="${tr.dataset.id}"]`);
      if (d) d.style.display = d.style.display === "none" ? "" : "none";
    };
  });

  // 下方独立渲染「指标表（人工修正）」（可编辑整表）
  renderAdjustedTable();
}

/* ---------------- Step3 人工修正 ---------------- */
const FIELD_LABEL = { avg_levels: "平均层数", max_levels: "最高层数", FAR: "容积率",
  far_geometric: "容积率(几何)", building_area_sqm: "总建筑面积㎡",
  avg_levels_weighted: "加权平均层数" };

function startEditCell(td) {
  const pid = +td.dataset.id, f = td.dataset.f;
  const old = td.textContent.replace("✎", "").trim();
  td.innerHTML = `<input type="number" step="0.1" value="${old.replace(/,/g, "")}"
    style="width:74px;text-align:right;padding:2px 4px;font-size:12px">`;
  const inp = td.querySelector("input");
  inp.focus(); inp.select();
  const done = async (save) => {
    const v = parseFloat(inp.value);
    td.innerHTML = old + '<span class="pen">✎</span>';
    if (!save || isNaN(v) || v === parseFloat(old)) return;
    try {
      await api("/api/metrics/adjust", { method: "POST",
        body: JSON.stringify({ pid, field: f, value: v }) });
      await loadAdjusted();
      refreshMetricsFromServer();
      toast(`已修正 #${pid} 的${FIELD_LABEL[f] || f}：${old} → ${v}`);
    } catch (e) {
      toast("修改失败：" + e.message, "err");
    }
  };
  inp.onblur = () => done(true);
  inp.onkeydown = e => {
    if (e.key === "Enter") { e.preventDefault(); inp.blur(); }
    if (e.key === "Escape") { inp.value = old; inp.blur(); }
  };
  td.onclick = null;   // 编辑中不再触发
}

async function loadAdjusted() {
  try {
    const r = await api("/api/metrics/adjusted");
    S.adjusted = r.items || [];
    S.adjustedMap = Object.fromEntries(S.adjusted.map(x => [x.Parcel_ID, x]));
  } catch (e) { S.adjusted = []; S.adjustedMap = {}; }
  renderMetrics();   // 重渲染原表 + 人工修正表
}

async function refreshMetricsFromServer() {
  try {
    const r = await api("/api/metrics/table");
    S.metrics = r.rows;
    renderMetrics();
  } catch (e) { /* 保持原样 */ }
}

function renderAdjustedTable() {
  // 指标表（人工修正）= 当前工作表（随人工修正实时更新），可整表编辑
  const wrap = $("#tblAdjusted");
  if (!wrap) return;
  const rows = activeMetrics();
  const onlyAnom = $("#onlyAnomaly").checked;
  const show = onlyAnom ? rows.filter(r => r.anomaly_level > 0) : rows;

  const cols = ["parcel_area_sqm", "building_count", "BCR", "FAR", "far_geometric",
                "building_area_sqm", "avg_levels", "max_levels",
                "levels_coverage", "highrise_ratio"];
  const labels = {
    parcel_area_sqm: "地块面积㎡", building_count: "建筑数", BCR: "建筑密度",
    FAR: "容积率", far_geometric: "容积率(几何)",
    building_area_sqm: "总建筑面积㎡", avg_levels: "平均层数",
    max_levels: "最高层数", levels_coverage: "层数实测率", highrise_ratio: "高层占比",
  };
  const pct3 = new Set(["BCR", "FAR", "far_geometric", "highrise_ratio"]);
  const EDITABLE_COLS = new Set(["avg_levels", "max_levels", "FAR"]);

  let html = `<thead><tr><th style="min-width:170px">案例</th>`;
  cols.forEach(c => html += `<th class="num">${labels[c]}</th>`);
  html += "</tr></thead><tbody>";

  if (!show.length) {
    html += `<tr><td colspan="${cols.length + 1}" style="text-align:center;color:var(--ink-3);padding:18px">尚无指标数据</td></tr>`;
  }
  show.forEach(r => {
    const cls = r.anomaly_level === 2 ? "row-danger" : r.anomaly_level === 1 ? "row-warn" : "";
    const isEd = String(r.Parcel_ID) in (S.adjustedMap || {});
    const edTag = isEd ? '<span class="badge neutral" style="margin-left:4px">已修正</span>' : "";
    const covPct = r.levels_coverage != null ? Math.round(r.levels_coverage * 100) : null;
    const revBtn = isEd
      ? `<button class="btn mini ghost btn-rev" data-pid="${r.Parcel_ID}" style="margin-top:5px">撤销</button>` : "";

    html += `<tr class="${cls}" data-id="${r.Parcel_ID}">
      <td>
        <div style="font-weight:600">${r.name || "未命名"}</div>
        <div style="margin-top:3px;display:flex;gap:5px;align-items:center;flex-wrap:wrap">
          <span style="font-size:11px;color:var(--ink-3)">#${r.Parcel_ID}</span>${edTag}
        </div>${revBtn ? `<div style="margin-top:4px">${revBtn}</div>` : ""}
      </td>`;
    cols.forEach(c => {
      let v = r[c];
      if (c === "levels_coverage") v = covPct == null ? "—" : covPct + "%";
      else v = fmt(v, pct3.has(c) ? 3 : c === "avg_levels" ? 1 : 0);
      if (EDITABLE_COLS.has(c)) {
        html += `<td class="num editable" data-f="${c}" data-id="${r.Parcel_ID}"
          title="双击修改">${v}<span class="pen">✎</span></td>`;
      } else {
        html += `<td class="num">${v}</td>`;
      }
    });
    html += "</tr>";
  });
  html += "</tbody>";
  wrap.innerHTML = html;

  // 可编辑单元格：双击/单击铅笔 → 输入框 → 回车或失焦保存
  $$("#tblAdjusted td.editable").forEach(td => {
    td.onclick = e => { if (!td.querySelector("input")) { e.stopPropagation(); startEditCell(td); } };
    td.ondblclick = e => { e.stopPropagation(); startEditCell(td); };
  });
  // 撤销人工修正
  $$("#tblAdjusted .btn-rev").forEach(b => {
    b.onclick = async () => {
      const pid = +b.dataset.pid;
      if (!confirm(`撤销地块 ${pid} 的全部人工修正，恢复原始计算值？`)) return;
      try {
        await api(`/api/metrics/adjust/${pid}`, { method: "DELETE" });
        await loadAdjusted();
        await refreshMetricsFromServer();
        toast(`已撤销 #${pid} 的人工修正`);
      } catch (e) { toast("撤销失败：" + e.message, "err"); }
    };
  });
}

$("#onlyAnomaly").onchange = renderMetrics;

/* ---------------- FAR 口径切换 ---------------- */
function renderFarModeHint() {
  const el = $("#farModeHint");
  if (!el) return;
  el.textContent = S.farMode === "geometric"
    ? "当前「容积率」列 = 几何口径（未折减）；切换立即生效，筛选与聚类同步使用，无需重算指标。"
    : "当前「容积率」列 = 近似计容口径（×折减系数）；「容积率(几何)」列恒为未折减值，切换无需重算。";
}

$("#farMode").onchange = async () => {
  const mode = $("#farMode").value;
  try {
    const r = await api("/api/settings/far-mode", {
      method: "POST", body: JSON.stringify({ mode }),
    });
    S.farMode = r.mode;
    renderFarModeHint();
    // 指标表不需要重算：重新拉一次即可（服务端按新口径返回 FAR 列）
    if (S.metrics && S.metrics.length) {
      const t = await api("/api/metrics/table");
      S.metrics = t.rows;
      const b = await api("/api/metrics/table?source=baseline");
      S.baseline = b.rows;
      renderMetrics();
    }
    toast(`容积率口径已切换：${r.label}。若已做过聚类，请重新执行聚类。`);
  } catch (e) {
    toast("口径切换失败：" + e.message, "err");
    $("#farMode").value = S.farMode;
  }
};

$("#btnRemoveSel").onclick = () => {
  const ids = $$("#tblMetrics tbody tr.pickable").filter(tr => {
    const cb = tr.querySelector(".rw");
    return cb && cb.checked;
  }).map(tr => +tr.dataset.id);
  if (!ids.length) { toast("请先勾选要剔除的行", "err"); return; }
  ids.forEach(id => S.removed.add(id));
  renderMetrics();
  toast(`已剔除 ${ids.length} 条`);
};

$("#btnRestore").onclick = () => {
  S.removed.clear(); renderMetrics(); toast("已恢复全部案例");
};

$("#btnToStep4").onclick = () => { unlock(4); renderConditions(); };

/* ============================================================
   STEP 4 · 条件筛选
   ============================================================ */
function fieldOptions(selected) {
  return FIELD_META.map(([k, label]) =>
    `<option value="${k}" ${k === selected ? "selected" : ""}>${label}</option>`).join("");
}
function opOptions(selected) {
  return OPERATORS.map(([k, label]) =>
    `<option value="${k}" ${k === selected ? "selected" : ""}>${label}</option>`).join("");
}

function addCondition(field = "FAR", op = ">", v = 3.2, v2 = "") {
  const d = document.createElement("div");
  d.className = "cond";
  d.innerHTML = `
    <select class="cfield">${fieldOptions(field)}</select>
    <select class="cop">${opOptions(op)}</select>
    <input type="number" class="cval" step="0.01" value="${v}" placeholder="数值">
    <input type="number" class="cval2" step="0.01" value="${v2}" placeholder="上限" style="display:none">
    <button class="del" title="删除">×</button>`;
  const cop = d.querySelector(".cop"), cv2 = d.querySelector(".cval2");
  const sync = () => { cv2.style.display = cop.value === "between" ? "" : "none"; saveConditions(); };
  d.querySelector(".cval").oninput = saveConditions;
  d.querySelector(".cval2").oninput = saveConditions;
  d.querySelector(".cfield").onchange = saveConditions;
  cop.onchange = sync; sync();
  d.querySelector(".del").onclick = () => { d.remove(); saveConditions(); };
  $("#condList").appendChild(d);
  saveConditions();
  return d;
}

function renderConditions() {
  // 优先恢复上次的条件（引擎被重启/刷新页面后不至于「条件凭空消失」）
  const saved = localStorage.getItem("filterConds");
  if (saved && $("#condList").children.length === 0) {
    try {
      const arr = JSON.parse(saved);
      if (Array.isArray(arr) && arr.length) {
        $("#condList").innerHTML = "";
        arr.forEach(c => addCondition(c.field || "FAR", c.op || ">", c.value, c.value2 || ""));
        return;
      }
    } catch (e) { /* 存档坏了就退回默认条件 */ }
  }
  if ($("#condList").children.length === 0) addCondition();
}

function saveConditions() {
  const conds = $$("#condList .cond").map(d => ({
    field: d.querySelector(".cfield").value,
    op: d.querySelector(".cop").value,
    value: d.querySelector(".cval").value,
    value2: d.querySelector(".cval2").value,
  }));
  try { localStorage.setItem("filterConds", JSON.stringify(conds)); } catch (e) {}
}

$("#btnAddCond").onclick = () => addCondition();

$("#preset").onchange = e => {
  const v = e.target.value;
  if (!v) return;
  $("#condList").innerHTML = "";
  if (v === "far32") addCondition("FAR", ">", 3.2);
  if (v === "bcr30") addCondition("BCR", ">", 0.30);
  if (v === "far32_bcr35") { addCondition("FAR", ">", 3.2); addCondition("BCR", "<", 0.35); }
  if (v === "far25_bcr30") { addCondition("FAR", "between", 2.5, 5); addCondition("BCR", ">", 0.25); }
  if (v === "high18") addCondition("highrise_ratio", ">", 0.5);
  saveConditions();
  e.target.value = "";
};

$$(".logicbar .pill").forEach(p => p.onclick = () => {
  $$(".logicbar .pill").forEach(x => x.classList.remove("on"));
  p.classList.add("on");
});

$("#btnFilterReset").onclick = () => {
  $("#condList").innerHTML = ""; addCondition(); $("#filterResult").innerHTML = "";
  try { localStorage.removeItem("filterConds"); } catch (e) {}
};

$("#btnRunFilter").onclick = async () => {
  const conds = $$("#condList .cond").map(d => {
    const op = d.querySelector(".cop").value;
    return {
      field: d.querySelector(".cfield").value,
      op,
      value: parseFloat(d.querySelector(".cval").value),
      value2: op === "between" ? parseFloat(d.querySelector(".cval2").value) : null,
    };
  }).filter(c => !isNaN(c.value));
  saveConditions();

  if (conds.length === 0) {
    toast("请至少填一条条件的数值", "err");
    return;
  }
  // 条件为空（或被清空）时仍然能跑，但要明确告诉用户「没设条件 = 全保留」
  const logic = $(".logicbar .pill.on").dataset.logic;
  overlay(true, "正在筛选", "");
  try {
    const r = await api("/api/metrics/filter", {
      method: "POST",
      body: JSON.stringify({
        conditions: conds,
        logic,
        exclude_anomalies: $("#excludeAnomaly").checked,
        max_anomaly_level: +$("#maxAnomalyLevel").value,
        exclude_ids: Array.from(S.removed),
        source: $("#filterSource") ? $("#filterSource").value : "current",
      }),
    });
    S.filtered = r.rows;
    renderFilterResult(r);
    toast(`筛选完成：${r.total} → ${r.kept}`);
  } catch (e) {
    const msg = String(e.message || e);
    // 引擎重启/断连后 STATE 会清空，这时「看起来像没条件」，其实是数据没了
    if (msg.includes("尚无指标数据") || msg.includes("400")) {
      $("#filterResult").innerHTML = `<div class="note danger" style="line-height:1.9">
        <b>指标数据已失效。</b>常见原因是本地引擎被重启或与引擎断开了连接，
        服务端内存里的指标表随之清空。<br>
        请回到 <b>Step 3</b> 重新点一次「计算指标」（若 Step 2 的地块也没了，
        需从 Step 2 「加载候选地块」开始）。你的筛选条件已自动保存，回来直接点执行即可。
      </div>`;
      toast("指标数据已失效，请回到 Step 3 重新计算", "err");
    } else {
      toast("筛选失败：" + msg, "err");
    }
  } finally { overlay(false); }
};

function renderFilterResult(r) {
  const cols = ["Parcel_ID", "name", "parcel_area_sqm", "building_count",
                "BCR", "FAR", "far_geometric", "building_area_sqm", "avg_levels", "max_levels",
                "levels_coverage", "highrise_ratio"];
  const labels = { Parcel_ID: "ID", name: "名称", parcel_area_sqm: "地块面积㎡",
    building_count: "建筑数", BCR: "建筑密度", FAR: "容积率",
    far_geometric: "容积率(几何)",
    building_area_sqm: "总建筑面积㎡", levels_coverage: "层数实测率",
    avg_levels: "平均层数", max_levels: "最高层数", highrise_ratio: "高层占比" };
  const numCols = new Set(cols.slice(2));

  let html = `<div class="stats" style="margin-bottom:14px">
      <div class="stat"><div class="k">筛选前</div><div class="v">${fmt(r.total)}</div></div>
      <div class="stat"><div class="k">筛选后</div><div class="v">${fmt(r.kept)}</div></div>
      <div class="stat warn"><div class="k">其中需留意</div><div class="v">${fmt(r.anomaly_warn)}</div></div>
      <div class="stat danger"><div class="k">其中严重异常</div><div class="v">${fmt(r.anomaly_severe)}</div></div>
    </div>`;

  // 让「异常过滤」这一步不再隐形：到底排除掉几个、都是什么原因
  if (r.anomaly_excluded > 0) {
    const fl = Object.entries(r.excluded_flags || {})
      .sort((a, b) => b[1] - a[1])
      .map(([k, v]) => `${k} ${v} 个`).join("、");
    html += `<div class="note" style="font-size:12.5px;margin-bottom:12px">
      已按设置排除 <b>${fmt(r.anomaly_excluded)}</b> 个异常案例${fl ? `（${fl}）` : ""}。
      想保留它们，把下面的「自动排除异常数据」改成「全部保留」。</div>`;
  }

  if (r.kept === 0) {
    let extra = "";
    if (r.hint) {
      extra += `<div class="note" style="border-left:3px solid #c0392b;margin-bottom:10px">
        <b>为什么是 0：</b>${r.hint}</div>`;
    }
    if (r.condition_stats && r.condition_stats.length) {
      extra += `<div style="font-size:12px;color:var(--ink-3,#7a8a8a);margin-bottom:12px">
        各条件执行后剩余：` + r.condition_stats.map(c => c.kept === null
        ? `${c.field}（${c.note || "已忽略"}）`
        : `${c.field} ${c.op} ${c.value} → ${c.kept} 个`).join("；") + `</div>`;
    }
    html += `<div class="empty">筛选后为 0，请看下面的原因定位。</div>${extra}`;

    // 如果是异常过滤清零的，给一键放宽按钮
    if ((r.anomaly_excluded || 0) > 0 && (r.after_conditions || 0) > 0) {
      html += `<div class="btnrow"><button class="btn" id="btnFixAnomaly">
        改为「全部保留（不排除）」并重新筛选</button></div>`;
    }
    $("#filterResult").innerHTML = html;
    const fix = $("#btnFixAnomaly");
    if (fix) {
      fix.onclick = () => {
        $("#maxAnomalyLevel").value = "2";
        $("#excludeAnomaly").checked = false;
        $("#btnRunFilter").click();
      };
    }
    return;
  }

  html += `<div class="tablewrap"><table><thead><tr>`;
  cols.forEach(c => html += `<th class="${numCols.has(c) ? "num" : ""}">${labels[c]}</th>`);
  html += `</tr></thead><tbody>`;
  r.rows.forEach(row => {
    html += `<tr>`;
    cols.forEach(c => {
      let v = row[c];
      if (numCols.has(c)) v = fmt(v, c === "BCR" || c === "FAR" || c === "highrise_ratio" ? 3 : 1);
      html += `<td class="${numCols.has(c) ? "num" : ""}">${v ?? "—"}</td>`;
    });
    html += `</tr>`;
  });
  html += `</tbody></table></div>
    <div class="btnrow">
      <button class="btn" id="btnConfirmCases">确认这 ${r.kept} 个案例</button>
    </div>`;

  $("#filterResult").innerHTML = html;

  $("#btnConfirmCases").onclick = async () => {
    overlay(true, "正在确认案例", "");
    try {
      await api("/api/metrics/confirm", {
        method: "POST",
        body: JSON.stringify({ ids: r.ids }),
      });
      const cases = S.metrics.filter(x => r.ids.includes(x.Parcel_ID));
      const farAvg = cases.reduce((a, b) => a + (b.FAR || 0), 0) / (cases.length || 1);
      const bcrAvg = cases.reduce((a, b) => a + (b.BCR || 0), 0) / (cases.length || 1);
      $("#s5Stats").innerHTML = `
        <div class="stat"><div class="k">案例数</div><div class="v">${fmt(r.kept)}</div></div>
        <div class="stat"><div class="k">平均容积率</div><div class="v">${farAvg.toFixed(2)}</div></div>
        <div class="stat"><div class="k">平均建筑密度</div><div class="v">${bcrAvg.toFixed(3)}</div></div>`;
      S.confirmed = r.ids.slice();
      $("#btnExportMetrics").disabled = false;
      unlock(5);
      toast("案例已确认");
    } catch (e) {
      toast("确认失败：" + e.message, "err");
    } finally { overlay(false); }
  };
};

/* ============================================================
   STEP 5 · 成果输出
   ============================================================ */
$("#btnGenPlans").onclick = async () => {
  startProgress("正在生成平面图", "每个地块需下载建筑与道路数据…");
  try {
    const r = await api("/api/plans/generate", {
      method: "POST",
      body: JSON.stringify({ clip_buildings: true, dpi: 300 }),
    });
    S.planItems = r.items;
    S.planMissing = (r.missing || []).length;
    renderGallery(r);
    $("#btnDownloadZip").disabled = false;
    $("#s5Next").style.display = "";
    toast(`已生成 ${r.count} 张平面图`);
  } catch (e) {
    toast("生成失败：" + e.message, "err");
  } finally {
    overlay(false);
  }
};

function renderGallery(r) {
  if (!r.items.length) {
    $("#planGallery").innerHTML = `<div class="empty">未生成任何平面图。</div>`;
    return;
  }
  S.planItems = r.items;
  renderPlanGalleryInner();
}

function renderPlanGalleryInner() {
  const items = S.planItems || [];
  const missCount = S.planMissing || 0;
  if (!items.length) {
    $("#planGallery").innerHTML = `<div class="empty">所有示意图已剔除。</div>`;
    return;
  }
  $("#planGallery").innerHTML = `
    <div class="h" style="margin-top:20px">
      <h2 style="font-size:14px">平面示意图</h2>
      <span class="sub">共 ${items.length} 张${
        missCount ? `，缺失 ${missCount} 个（OSM 无建筑数据）` : ""} ·
        轮廓不清晰的点「剔除」，会连带删除该案例</span>
    </div>
    <div class="gallery">
      ${items.map(it => `
        <div class="gitem" data-pid="${it.Parcel_ID}">
          <img src="${apiUrl(it.url)}" loading="lazy" alt="parcel ${it.Parcel_ID}">
          <div class="cap" style="display:flex;align-items:center;justify-content:space-between;gap:6px">
            <b>地块 ${it.Parcel_ID}</b>
            <button class="btn mini danger btn-drop" data-pid="${it.Parcel_ID}"
              title="删除此图，并从案例集合中移除该地块">✕ 剔除</button>
          </div>
        </div>`).join("")}
    </div>`;

  $$("#planGallery .btn-drop").forEach(btn => {
    btn.onclick = async ev => {
      ev.stopPropagation();
      const pid = +btn.dataset.pid;
      if (!confirm(`剔除地块 ${pid}？\n\n该平面图会被删除，\n同时这个住区案例会从指标表、聚类和导出中一并移除。`)) return;
      btn.disabled = true;
      try {
        const r = await api(`/api/plans/drop/${pid}`, { method: "POST" });
        S.planItems = (S.planItems || []).filter(x => +x.Parcel_ID !== pid);
        if (Array.isArray(S.confirmed)) S.confirmed = S.confirmed.filter(x => +x !== pid);
        // 同步 Step5 顶部统计
        if (S.metrics) {
          const cases = S.metrics.filter(x => (S.confirmed || []).includes(x.Parcel_ID));
          const farAvg = cases.reduce((a, b) => a + (b.FAR || 0), 0) / (cases.length || 1);
          const bcrAvg = cases.reduce((a, b) => a + (b.BCR || 0), 0) / (cases.length || 1);
          const stats = $("#s5Stats");
          if (stats) stats.innerHTML = `
            <div class="stat"><div class="k">案例数</div><div class="v">${fmt(cases.length)}</div></div>
            <div class="stat"><div class="k">平均容积率</div><div class="v">${farAvg.toFixed(2)}</div></div>
            <div class="stat"><div class="k">平均建筑密度</div><div class="v">${bcrAvg.toFixed(3)}</div></div>`;
        }
        renderPlanGalleryInner();
        toast(`已剔除地块 ${pid}，剩余案例 ${r.cases_left} 个`);
      } catch (e) {
        toast("剔除失败：" + e.message, "err");
        btn.disabled = false;
      }
    };
  });
}

$("#btnExportMetrics").onclick = () => window.open(apiUrl("/api/metrics/export"), "_blank");
$("#btnDownloadZip").onclick   = () => window.open(apiUrl("/api/plans/download-zip"), "_blank");
$("#btnToStep6").onclick = () => { unlock(6); renderFeatures(); };

/* ============================================================
   STEP 6 · 聚类
   ============================================================ */
function renderFeatures() {
  const build = (container, selected) => {
    container.innerHTML = "";
    FIELD_META.forEach(([k, label, desc]) => {
      const on = selected.includes(k);
      const lab = document.createElement("label");
      lab.className = "feat" + (on ? " on" : "");
      lab.innerHTML = `<input type="checkbox" value="${k}" ${on ? "checked" : ""}>
        <div><div class="fn">${label}</div><div class="fd">${desc}</div></div>`;
      lab.querySelector("input").onchange = e => {
        lab.classList.toggle("on", e.target.checked);
        syncFeatures();
      };
      container.appendChild(lab);
    });
  };
  build($("#featGrid"), S.features.length ? S.features : RECOMMEND_FEATURES);
  build($("#featGrid2"), S.features.length ? S.features : RECOMMEND_FEATURES);
  syncFeatures();
}

function syncFeatures() {
  const a = $$("#featGrid input:checked").map(i => i.value);
  // 两个面板保持同步
  $$("#featGrid2 input").forEach(i => {
    i.checked = a.includes(i.value);
    i.parentElement.classList.toggle("on", i.checked);
  });
  S.features = a;
}

$("#featGrid2").addEventListener("change", e => {
  if (e.target.tagName !== "INPUT") return;
  const a = $$("#featGrid2 input:checked").map(i => i.value);
  $$("#featGrid input").forEach(i => {
    i.checked = a.includes(i.value);
    i.parentElement.classList.toggle("on", i.checked);
  });
  S.features = a;
});

$("#btnFeatDefault").onclick = () => {
  S.features = [...RECOMMEND_FEATURES];
  renderFeatures();
};

/* ---------------- 肘部分析 ---------------- */
let elbowData = null;

/* 聚类样本数 = 第 4 步确认的案例数（引擎侧也会做同样校验） */
function clusterSampleCount() {
  return (S.confirmed || []).length;
}
function guardSample() {
  const n = clusterSampleCount();
  if (n < 5) {
    toast(`样本仅 ${n} 个，无法做可靠聚类`, "err");
    alert(`当前只有 ${n} 个案例，样本过少无法做可靠聚类。\n\n` +
          `请回到第 4 步放宽筛选条件（例如降低容积率下限、不排除某类异常），` +
          `至少保留 5 个以上案例后再做聚类分析。`);
    return false;
  }
  return true;
}

$("#btnElbow").onclick = async () => {
  if (!S.features.length) { toast("请至少勾选一个聚类指标", "err"); return; }
  if (!guardSample()) return;
  startProgress("正在运行肘部分析", "逐个 K 值计算 SSE…");
  try {
    const r = await api("/api/cluster/elbow", {
      method: "POST",
      body: JSON.stringify({
        features: S.features,
        k_min: +$("#kMin").value,
        k_max: +$("#kMax").value,
      }),
    });
    elbowData = r.sse;
    drawElbow(r.sse);
    $("#btnElbowFig").disabled = false;
    // 引擎可能剔除了「所有案例里都缺失」的指标，同步生效指标，避免后续维度不一致
    if (r.features && r.features.length && r.features.length !== S.features.length) {
      const dropped = (r.dropped && r.dropped.length)
        ? r.dropped : S.features.filter(f => !r.features.includes(f));
      S.features = r.features;
      renderFeatures();
      alert(`以下指标在所有案例中都是缺失值，已自动剔除，不参与聚类：\n\n` +
            dropped.map(f => `· ${f}`).join("\n") +
            `\n\n实际参与聚类的指标：${r.features.join("、")}`);
    }
    // 自动推荐 K（最大曲率）
    const rec = recommendK(r.sse);
    $("#kVal").value = rec;
    toast(`肘部分析完成，建议 K = ${rec}`);
  } catch (e) {
    toast("分析失败：" + e.message, "err");
  } finally { overlay(false); }
};

function recommendK(sse) {
  if (sse.length < 3) return sse[0]?.K || 5;
  let best = sse[0].K, bestScore = -Infinity;
  for (let i = 1; i < sse.length - 1; i++) {
    const prev = sse[i - 1].SSE, cur = sse[i].SSE, next = sse[i + 1].SSE;
    const drop1 = prev - cur, drop2 = cur - next;
    const score = drop1 - drop2;
    if (score > bestScore) { bestScore = score; best = sse[i].K; }
  }
  return best;
}

function drawElbow(sse) {
  const W = 760, H = 300, P = { l: 62, r: 24, t: 22, b: 42 };
  const iw = W - P.l - P.r, ih = H - P.t - P.b;
  const xs = sse.map(d => d.K), ys = sse.map(d => d.SSE);
  const xmin = Math.min(...xs), xmax = Math.max(...xs);
  const ymin = 0, ymax = Math.max(...ys) * 1.06;

  const X = v => P.l + (v - xmin) / (xmax - xmin || 1) * iw;
  const Y = v => P.t + ih - (v - ymin) / (ymax - ymin || 1) * ih;

  const rec = recommendK(sse);
  let path = sse.map((d, i) => `${i ? "L" : "M"}${X(d.K).toFixed(1)},${Y(d.SSE).toFixed(1)}`).join(" ");

  let grid = "";
  for (let i = 0; i <= 5; i++) {
    const y = P.t + ih * i / 5, val = ymax * (1 - i / 5);
    grid += `<line x1="${P.l}" y1="${y}" x2="${W - P.r}" y2="${y}" stroke="#e2eeea"/>
             <text x="${P.l - 8}" y="${y + 4}" text-anchor="end" font-size="10" fill="#6b8880">${fmt(val)}</text>`;
  }
  let xt = "";
  xs.forEach(k => {
    xt += `<text x="${X(k)}" y="${H - P.b + 17}" text-anchor="middle" font-size="10.5" fill="#3c5a54">${k}</text>`;
  });
  const dots = sse.map(d =>
    `<circle cx="${X(d.K)}" cy="${Y(d.SSE)}" r="${d.K === rec ? 5.5 : 3.4}"
      fill="${d.K === rec ? "#a8412f" : "#358b7b"}"
      stroke="#fff" stroke-width="1.4"/>
     ${d.K === rec ? `<line x1="${X(d.K)}" y1="${P.t + ih}" x2="${X(d.K)}" y2="${Y(d.SSE)}"
        stroke="#a8412f" stroke-width="1" stroke-dasharray="3 3"/>` : ""}`).join("");

  $("#elbowBox").innerHTML = `
    <div class="canvasbox">
      <svg viewBox="0 0 ${W} ${H}" style="width:100%;display:block">
        ${grid}
        <line x1="${P.l}" y1="${P.t + ih}" x2="${W - P.r}" y2="${P.t + ih}" stroke="#cfe0da"/>
        <line x1="${P.l}" y1="${P.t}" x2="${P.l}" y2="${P.t + ih}" stroke="#cfe0da"/>
        <path d="${path}" fill="none" stroke="#358b7b" stroke-width="2.2"/>
        ${dots}
        <text x="${P.l + iw / 2}" y="${H - 8}" text-anchor="middle" font-size="11" fill="#16403a">聚类数量 K</text>
        <text x="16" y="${P.t + ih / 2}" text-anchor="middle" font-size="11" fill="#16403a"
              transform="rotate(-90 16 ${P.t + ih / 2})">SSE (Inertia)</text>
      </svg>
    </div>
    <div class="note" style="margin-top:12px">
      曲线拐点通常在 <b>K = ${rec}</b> 附近，已自动填入下方聚类参数。若学术判断不同，可直接修改。
    </div>`;
}

$("#btnElbowFig").onclick = () => {
  // 用后端出 300dpi 论文图
  fetch(apiUrl("/api/cluster/elbow-figure"), {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ features: S.features, k_min: +$("#kMin").value, k_max: +$("#kMax").value }),
  }).then(r => r.blob()).then(b => {
    const a = document.createElement("a");
    a.href = URL.createObjectURL(b); a.download = "elbow_curve.png"; a.click();
  }).catch(e => toast("下载失败：" + e.message, "err"));
};

/* ---------------- KMeans ---------------- */
$("#btnCluster").onclick = async () => {
  if (!S.features.length) { toast("请至少勾选一个聚类指标", "err"); return; }
  if (!guardSample()) return;
  startProgress("正在执行聚类", "KMeans 迭代计算中…");
  try {
    const r = await api("/api/cluster/run", {
      method: "POST",
      body: JSON.stringify({ features: S.features, k: +$("#kVal").value }),
    });
    S.clusterResult = r;
    renderCluster(r);
    renderCenters(r);
    renderCaseOptions(r);
    $("#btnExportCluster").disabled = false;
    toast(`聚类完成，K = ${r.k}`);
  } catch (e) {
    toast("聚类失败：" + e.message, "err");
  } finally { overlay(false); }
};

function renderCluster(r) {
  const counts = {};
  r.rows.forEach(x => counts[x.cluster] = (counts[x.cluster] || 0) + 1);
  const keys = Object.keys(counts).sort((a, b) => a - b);

  $("#clusterBox").innerHTML = `
    <div class="stats" style="margin-bottom:14px">
      ${keys.map(k => `<div class="stat"><div class="k">类别 ${k}</div>
        <div class="v">${counts[k]}<small>个</small></div></div>`).join("")}
    </div>
    <div class="tablewrap" style="max-height:300px">
      <table>
        <thead><tr><th>ID</th><th>名称</th><th class="num">类别</th>
          <th class="num">容积率</th><th class="num">建筑密度</th>
          <th class="num">平均层数</th><th class="num">建筑数</th></tr></thead>
        <tbody>
          ${r.rows.slice(0, 300).map(x => `<tr>
            <td>${x.Parcel_ID}</td><td>${x.name || "—"}</td>
            <td class="num"><span class="badge neutral">${x.cluster}</span></td>
            <td class="num">${fmt(x.FAR, 2)}</td>
            <td class="num">${fmt(x.BCR, 3)}</td>
            <td class="num">${fmt(x.avg_levels, 1)}</td>
            <td class="num">${fmt(x.building_count)}</td>
          </tr>`).join("")}
        </tbody>
      </table>
    </div>
    <div style="font-size:11.5px;color:var(--ink-3);margin-top:8px">
      已按类别分别导出 CSV：${r.cluster_files.map(f => `cluster_${f.cluster}.csv（${f.count} 个）`).join("、")}
    </div>`;
}

function renderCenters(r) {
  const cols = ["cluster", "count", ...r.features];
  const labels = Object.fromEntries(FIELD_META.map(([k, l]) => [k, l]));
  labels.cluster = "类别"; labels.count = "样本数";
  $("#clusterBox").innerHTML += `
    <h3 style="font-size:13px;font-weight:700;color:var(--teal-800);margin:18px 0 8px">聚类中心（原始指标尺度）</h3>
    <div class="tablewrap" style="max-height:none">
      <table><thead><tr>${cols.map(c =>
        `<th class="${c === "cluster" ? "" : "num"}">${labels[c] || c}</th>`).join("")}</tr></thead>
        <tbody>${r.centers.map(c => `<tr>
          <td><b style="color:var(--teal-700)">${c.cluster}</b></td>
          ${cols.slice(1).map(k => `<td class="num">${k === "count" ? c[k] : fmt(c[k], 3)}</td>`).join("")}
        </tr>`).join("")}</tbody>
      </table>
    </div>`;
}

function renderCaseOptions(r) {
  const keys = [...new Set(r.rows.map(x => x.cluster))].sort((a, b) => a - b);
  $("#caseCluster").innerHTML = keys.map(k =>
    `<option value="${k}">类别 ${k}</option>`).join("");
}

$("#btnCases").onclick = async () => {
  const c = $("#caseCluster").value;
  if (c === "") { toast("请先执行聚类", "err"); return; }
  try {
    const r = await api(`/api/cluster/cases/${c}?top_n=10`);
    $("#casesBox").innerHTML = `
      <div class="note">类别 ${c} 共 ${r.count} 个样本，下列 ${r.top.length} 个为离聚类中心最近的典型案例。
        建议优先用这些案例制作论文插图。</div>
      <div class="tablewrap">
        <table><thead><tr>
          <th class="num">排名</th><th>ID</th><th>名称</th>
          <th class="num">到中心距离</th><th class="num">容积率</th>
          <th class="num">建筑密度</th><th class="num">平均层数</th>
        </tr></thead><tbody>
          ${r.top.map((x, i) => `<tr>
            <td class="num">${i + 1}</td><td>${x.Parcel_ID}</td><td>${x.name || "—"}</td>
            <td class="num mono">${(x.distance_to_center || 0).toFixed(4)}</td>
            <td class="num">${fmt(x.FAR, 2)}</td>
            <td class="num">${fmt(x.BCR, 3)}</td>
            <td class="num">${fmt(x.avg_levels, 1)}</td>
          </tr>`).join("")}
        </tbody></table>
      </div>`;
  } catch (e) { toast("获取失败：" + e.message, "err"); }
};

$("#btnExportCluster").onclick = () => {
  window.open(apiUrl("/api/cluster/export/result"), "_blank");
  setTimeout(() => window.open(apiUrl("/api/cluster/export/centers"), "_blank"), 400);
  setTimeout(() => window.open(apiUrl("/api/cluster/export/cases"), "_blank"), 800);
};

$("#btnToStep7").onclick = () => { unlock(7); renderFeatures(); };

/* ============================================================
   STEP 7 · 科研图
   ============================================================ */
$("#btnFigure").onclick = async () => {
  if (!S.features.length) { toast("请至少勾选一个绘图指标", "err"); return; }
  startProgress("正在生成科研图", "PCA 降维 + 箱线图绘制…");
  try {
    const res = await fetch(apiUrl("/api/cluster/figure"), {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ features: S.features, dpi: 300 }),
    });
    if (!res.ok) {
      let m = "生成失败";
      try { m = (await res.json()).detail || m; } catch (e) {}
      throw new Error(m);
    }
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url; a.download = "Cluster_Analysis.png"; a.click();
    $("#figBox").innerHTML = `
      <div class="h" style="margin-top:18px"><h2 style="font-size:14px">PCA 与箱线图</h2>
        <span class="sub">300dpi · 可直接用于论文</span></div>
      <div class="figbox"><img src="${url}" alt="Cluster Analysis"></div>`;
    $("#btnFigureDl").disabled = false;
    toast("科研图已生成");
  } catch (e) {
    toast("生成失败：" + e.message, "err");
  } finally { overlay(false); }
};

$("#btnFigureDl").onclick = () => {
  const img = $("#figBox img");
  if (!img) return;
  const a = document.createElement("a");
  a.href = img.src; a.download = "Cluster_Analysis.png"; a.click();
};

$("#btnDownloadAll").onclick = () => window.open(apiUrl("/api/download-all"), "_blank");

/* ============================================================
   启动
   ============================================================ */
(async function init() {
  $("#minArea").value = 10000;
  $("#maxArea").value = 500000;
  renderFeatures();
  renderConditions();

  const ok = await checkEngine();
  if (ok) {
    try {
      const m = await api("/api/scope/mask");
      if (m.geojson) S.maskGeojson = m.geojson;
    } catch (e) {}
    try {
      const fm = await api("/api/settings/far-mode");
      S.farMode = fm.mode || "shrunk";
      const sel = $("#farMode");
      if (sel) sel.value = S.farMode;
      renderFarModeHint();
    } catch (e) {}
    pushLog(["引擎已连接，可以开始。"]);
  } else {
    pushLog([
      "未检测到本地计算引擎。",
      "",
      "请双击项目根目录的 start.bat 启动引擎，",
      "然后点击右上角「引擎设置」重新连接。",
    ]);
  }
})();
