// 数据层：状态 + 后端请求缓存
export const state = {
  mode: "driving",
  origin: { type: "hub", id: "sip" },   // {type:'hub'|'point', id?|coord?, name?}
  dest: { type: "hub", id: "huangpu" },
  buffer: 20,
  picking: "dest",                       // 地图点选写入 起点/终点
};

export const hubsGJ = { type: "FeatureCollection", features: [] };
export const HUBS = [];        // properties 列表
export const HUB_COORD = {};   // id -> [lng, lat]
export const HUB_BY_ID = {};

export async function initHubs() {
  const gj = await (await fetch("/api/hubs")).json();
  hubsGJ.features = gj.features;
  for (const ft of gj.features) {
    const p = ft.properties;
    HUBS.push(p);
    HUB_COORD[p.id] = ft.geometry.coordinates;
    HUB_BY_ID[p.id] = p;
  }
}

export function specOf(x) {
  return x.type === "hub" ? `hub:${x.id}` : `${x.coord[0].toFixed(5)},${x.coord[1].toFixed(5)}`;
}

export function nameOf(x) {
  if (x.type === "hub") return (HUB_BY_ID[x.id] || {}).name || x.id;
  return x.name || "自选点";
}

// 常用点（自由点预设）：坐标取自高德地理编码/POI（2026-09-21）
export const FAVORITES = [
  { key: "home",      name: "我家·观澜逸品花苑南区", coord: [120.70008, 31.23369] },
  { key: "zhongshan", name: "中山公园",             coord: [121.42743, 31.21679] },
  { key: "mao",       name: "MAO Livehouse",        coord: [121.47208, 31.20994] },
  { key: "hengtai",   name: "常熟·衡泰花园",        coord: [120.76362, 31.67440] },
  { key: "huayuan",   name: "常熟·花园新区五区",    coord: [120.82863, 31.57084] },
];
export const favValue = (f) => `fav:${f.key}`;
export const favOfValue = (v) =>
  v && v.startsWith("fav:") ? FAVORITES.find(f => favValue(f) === v) : null;
export const isFavoriteCoord = (coord) =>
  FAVORITES.some(f => Math.abs(f.coord[0] - coord[0]) < 1e-4
                  && Math.abs(f.coord[1] - coord[1]) < 1e-4);

const cache = new Map();
async function cached(key, url) {
  if (!cache.has(key)) {
    const p = fetch(url).then(r => {
      if (!r.ok) throw new Error(`${url}: HTTP ${r.status}`);
      return r.json();
    });
    cache.set(key, p);
    // 失败的请求从缓存移除，下次重试（否则一次失败会永久毒化该 key）
    p.catch(() => { if (cache.get(key) === p) cache.delete(key); });
  }
  return cache.get(key);
}

export const getTimes = (fromSpec, buffer) =>
  cached(`T|${fromSpec}|${buffer}`,
    `/api/times?from=${encodeURIComponent(fromSpec)}&buffer=${buffer}`);
export const getIso = (fromSpec, mode, buffer) =>
  cached(`I|${fromSpec}|${mode}|${buffer}`,
    `/api/isochrone?from=${encodeURIComponent(fromSpec)}&mode=${mode}&buffer=${buffer}`);
export const getRoute = (fromSpec, toSpec, buffer) =>
  cached(`R|${fromSpec}|${toSpec}|${buffer}`,
    `/api/route?from=${encodeURIComponent(fromSpec)}&to=${encodeURIComponent(toSpec)}&buffer=${buffer}`);
// 真实路线几何 + 经过路段/换乘明细（后端按需调高德并缓存，无Key回落直线）
export const getRouteGeo = (fromSpec, toSpec, mode, buffer) =>
  cached(`G|${fromSpec}|${toSpec}|${mode}|${buffer}`,
    `/api/route_geo?from=${encodeURIComponent(fromSpec)}&to=${encodeURIComponent(toSpec)}&mode=${mode}&buffer=${buffer}`);

export function invalidateFrom(fromSpec) {
  for (const k of [...cache.keys()]) if (k.split("|")[1] === fromSpec) cache.delete(k);
}

export const MODES = ["driving", "driving_local", "transit", "hsr"];
export const MODE_NAME = {
  driving: "开车·高速",
  driving_local: "开车·国道",
  transit: "公共交通",
  hsr: "高铁门到门",
};
export const MODE_COLOR = {
  driving: "var(--driving)",
  driving_local: "var(--driving_local)",
  transit: "var(--transit)",
  hsr: "var(--hsr)",
};
export const MODE_HEX = {
  driving: "#d7263d",
  driving_local: "#c77b0a",
  transit: "#1a9641",
  hsr: "#2b6cb8",
};

// 等时面色带（与等时圈 v2 的 9 档阈值为一一对应；末档为 180′ 以上兜底色）
export const BAND_THRESHOLDS = [15, 30, 45, 60, 90, 120, 150, 180];
export const BAND_COLORS = [
  "#1a9850", "#91cf60", "#d9ef8b", "#fee08b",
  "#fc8d59", "#e2724a", "#d73027", "#a50026",
  "#eceff2",   // 180′ 以上：极浅灰，仍覆盖图幅
];
export const BAND_LABELS = [...BAND_THRESHOLDS.map(t => `${t}′`), "180′+"];
// 描边色：同色系压暗，用于让等时线边界清晰
export const BAND_STROKE = [
  "#0f6b38", "#5fa03c", "#a8bb52", "#c9b45c",
  "#c96a3a", "#b8552f", "#a02020", "#6d0018",
  "#d5d9de",
];
export const bandFillOpacity = (band) => (band === BAND_COLORS.length - 1 ? 0.22 : 0.42);

export function statusInfo() {
  return fetch("/api/status").then(r => r.json());
}

// 两点方位角（0=北，顺时针），用于地铁图布局
export function bearingDeg(a, b) {
  const p1 = a[1] * Math.PI / 180, p2 = b[1] * Math.PI / 180;
  const dl = (b[0] - a[0]) * Math.PI / 180;
  const y = Math.sin(dl) * Math.cos(p2);
  const x = Math.cos(p1) * Math.sin(p2) - Math.sin(p1) * Math.cos(p2) * Math.cos(dl);
  return (Math.atan2(y, x) * 180 / Math.PI + 360) % 360;
}

// 点列 → 平滑曲线采样（Catmull-Rom），供地图画线路
export function smoothLine(points, seg = 24) {
  const P = points.filter((p, i) => i === 0 || p[0] !== points[i - 1][0] || p[1] !== points[i - 1][1]);
  if (P.length < 2) return P;
  const out = [];
  for (let i = 0; i < P.length - 1; i++) {
    const p0 = P[Math.max(i - 1, 0)], p1 = P[i], p2 = P[i + 1], p3 = P[Math.min(i + 2, P.length - 1)];
    for (let j = 0; j < seg; j++) {
      const t = j / seg, t2 = t * t, t3 = t2 * t;
      const x = 0.5 * ((2 * p1[0]) + (-p0[0] + p2[0]) * t + (2 * p0[0] - 5 * p1[0] + 4 * p2[0] - p3[0]) * t2 + (-p0[0] + 3 * p1[0] - 3 * p2[0] + p3[0]) * t3);
      const y = 0.5 * ((2 * p1[1]) + (-p0[1] + p2[1]) * t + (2 * p0[1] - 5 * p1[1] + 4 * p2[1] - p3[1]) * t2 + (-p0[1] + 3 * p1[1] - 3 * p2[1] + p3[1]) * t3);
      out.push([x, y]);
    }
  }
  out.push(P[P.length - 1]);
  return out;
}
