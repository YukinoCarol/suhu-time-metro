// 主入口：左 MapLibre 热力地图 + 右 D3 时间地铁
import {
  state, HUBS, HUB_COORD, HUB_BY_ID, initHubs, specOf, nameOf,
  getTimes, getIso, getRoute, MODES, MODE_NAME, MODE_HEX,
  BAND_COLORS, statusInfo, smoothLine,
} from "./core.js";
import { renderMetro } from "./metro.js";

const $ = (id) => document.getElementById(id);
let map;
let timesData = null;
let routeData = null;
let hubMarkers = [];
let originMarker = null;
let destMarker = null;
let refreshSeq = 0;

const enrichedHubs = () => HUBS.map(p => ({
  ...p, lng: HUB_COORD[p.id][0], lat: HUB_COORD[p.id][1],
}));
const coordOf = (x) => (x.type === "hub" ? HUB_COORD[x.id] : x.coord);

/* ---------- 选择器 ---------- */

function fillSelect(sel) {
  const el = $(sel);
  const cur = sel === "selOrigin" ? state.origin : state.dest;
  el.innerHTML = "";
  if (cur.type === "point") {
    const o = document.createElement("option");
    o.value = "__point";
    o.textContent = `◎ ${nameOf(cur)}`;
    el.appendChild(o);
  }
  for (const city of ["苏州", "上海"]) {
    const og = document.createElement("optgroup");
    og.label = city;
    for (const h of HUBS.filter(h => h.city === city)) {
      const o = document.createElement("option");
      o.value = h.id;
      o.textContent = h.name;
      og.appendChild(o);
    }
    el.appendChild(og);
  }
  el.value = cur.type === "hub" ? cur.id : "__point";
}

function syncPickUI() {
  $("pickOrigin").classList.toggle("active", state.picking === "origin");
  $("pickDest").classList.toggle("active", state.picking === "dest");
}

function setOrigin(x) {
  state.origin = x;
  fillSelect("selOrigin");
  refresh();
}

function setDest(x) {
  state.dest = x;
  fillSelect("selDest");
  refresh();
}

/* ---------- 地图 ---------- */

const emptyFC = () => ({ type: "FeatureCollection", features: [] });

function hubGeoFC() {
  return {
    type: "FeatureCollection",
    features: enrichedHubs().map(h => ({
      type: "Feature",
      geometry: { type: "Point", coordinates: [h.lng, h.lat] },
      properties: { id: h.id, name: h.name, city: h.city, kind: h.kind, hsr: h.hsr },
    })),
  };
}

function initMap() {
  map = new maplibregl.Map({
    container: "map",
    style: {
      version: 8,
      sources: {
        "esri-base": {
          type: "raster",
          tiles: ["https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Light_Gray_Base/MapServer/tile/{z}/{y}/{x}"],
          tileSize: 256,
          maxzoom: 16,
          attribution: "© Esri · © OpenStreetMap contributors",
        },
        "esri-ref": {
          type: "raster",
          tiles: ["https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Light_Gray_Reference/MapServer/tile/{z}/{y}/{x}"],
          tileSize: 256,
          maxzoom: 16,
        },
      },
      layers: [
        { id: "base", type: "raster", source: "esri-base" },
        { id: "base-ref", type: "raster", source: "esri-ref" },
      ],
    },
    center: [121.15, 31.2],
    zoom: 8,
    preserveDrawingBuffer: true,
  });
  map.on("load", onMapLoad);
  map.on("click", onMapClick);
  window.__map = map;
}

function onMapLoad() {
  try {
    map.fitBounds([[120.35, 30.85], [121.95, 31.56]], { padding: 40, duration: 0 });

  map.addSource("hubs", { type: "geojson", data: hubGeoFC() });
  map.addLayer({
    id: "hub-circle", type: "circle", source: "hubs",
    paint: {
      "circle-radius": 6.5,
      "circle-color": "#ffffff",
      "circle-stroke-color": "#141b22",
      "circle-stroke-width": 2.5,
    },
  });
  map.addLayer({
    id: "hub-outer", type: "circle", source: "hubs",
    filter: ["==", ["get", "hsr"], true],
    paint: {
      "circle-radius": 10,
      "circle-color": "rgba(0,0,0,0)",
      "circle-stroke-color": MODE_HEX[state.mode],
      "circle-stroke-width": 2,
    },
  });

  map.addSource("iso", { type: "geojson", data: emptyFC() });
  map.addLayer({
    id: "iso-fill", type: "fill", source: "iso", beforeId: "hub-circle",
    paint: {
      "fill-color": [
        "match", ["get", "band"],
        0, BAND_COLORS[0], 1, BAND_COLORS[1], 2, BAND_COLORS[2],
        3, BAND_COLORS[3], 4, BAND_COLORS[4], 5, BAND_COLORS[5],
        "#e0e0e0",
      ],
      "fill-opacity": [
        "match", ["get", "band"],
        5, 0.0,  // 90′ 以上不填充，避免整幅矩形色块
        0.32,
      ],
      "fill-antialias": false,
    },
  });

  map.addSource("route", { type: "geojson", data: emptyFC() });
  map.addLayer({
    id: "route-line", type: "line", source: "route",
    paint: { "line-color": MODE_HEX[state.mode], "line-width": 6, "line-opacity": 0.9 },
  });
  map.addLayer({
    id: "route-dash", type: "line", source: "route",
    paint: { "line-color": "#ffffff", "line-width": 2, "line-opacity": 0.9 },
  });
  map.setPaintProperty("route-dash", "line-dasharray", [0.1, 2]);

  addHubLabels();
  map.on("zoomend", syncHubLabelVis);
  map.on("mouseenter", "hub-circle", () => { map.getCanvas().style.cursor = "pointer"; });
  map.on("mouseleave", "hub-circle", () => { map.getCanvas().style.cursor = "crosshair"; });

  refresh();
  } catch (err) {
    window.__onLoadErr = String((err && err.stack) || err);
    throw err;
  }
}

function onMapClick(e) {
  const feats = map.queryRenderedFeatures(e.point, { layers: ["hub-circle"] });
  if (feats.length) {
    const id = feats[0].properties.id;
    if (state.picking === "origin") setOrigin({ type: "hub", id });
    else setDest({ type: "hub", id });
  } else {
    const coord = [+e.lngLat.lng.toFixed(5), +e.lngLat.lat.toFixed(5)];
    if (state.picking === "origin") setOrigin({ type: "point", coord, name: "地图选点·起点" });
    else setDest({ type: "point", coord, name: "地图选点·终点" });
    state.picking = "dest";
    syncPickUI();
  }
}

/* ---------- 线路与标注 ---------- */

function updateRouteLine() {
  const src = map.getSource("route");
  if (!src) return;
  const legs = routeData?.modes?.[state.mode]?.legs || [];
  const pts = smoothLine(legs.flat());
  src.setData(pts.length >= 2
    ? { type: "Feature", geometry: { type: "LineString", coordinates: pts } }
    : emptyFC());
  map.setPaintProperty("route-line", "line-color", MODE_HEX[state.mode]);
  map.setPaintProperty("hub-outer", "circle-stroke-color", MODE_HEX[state.mode]);
}

function ensureMarkers() {
  if (originMarker) return;
  const mk = (text, bg) => {
    const d = document.createElement("div");
    d.className = "odChip";
    d.style.background = bg;
    d.textContent = text;
    return d;
  };
  originMarker = new maplibregl.Marker({ element: mk("起", "#141b22") });
  destMarker = new maplibregl.Marker({ element: mk("终", MODE_HEX[state.mode]) });
}

function updateMarkers() {
  ensureMarkers();
  originMarker.setLngLat(coordOf(state.origin)).addTo(map);
  destMarker.setLngLat(coordOf(state.dest)).addTo(map);
  destMarker.getElement().style.background = MODE_HEX[state.mode];
  syncHubLabelVis();
}

function addHubLabels() {
  for (const m of hubMarkers) m.remove();
  hubMarkers = enrichedHubs().map(h => {
    const d = document.createElement("div");
    d.className = "hubLabel";
    d.textContent = h.name;
    d.title = `${h.name}（${h.city}）`;
    const mk = new maplibregl.Marker({ element: d, anchor: "top", offset: [0, 11] })
      .setLngLat([h.lng, h.lat])
      .addTo(map);
    mk._hubId = h.id;
    return mk;
  });
  syncHubLabelVis();
}

// 起点/终点枢纽由「起/终」chip 标识，普通标签隐藏避免重叠；
// 低缩放时城区标签过密，只在 zoom≥10.5 显示
function syncHubLabelVis() {
  const hide = new Set([
    state.origin.type === "hub" ? state.origin.id : "",
    state.dest.type === "hub" ? state.dest.id : "",
  ].filter(Boolean));
  const z = map ? map.getZoom() : 0;
  for (const m of hubMarkers) {
    m.getElement().style.display = (hide.has(m._hubId) || z < 10.5) ? "none" : "";
  }
}

/* ---------- 右栏 ---------- */

function renderCompare() {
  const box = $("compare");
  const modes = routeData?.modes || {};
  const rows = MODES
    .map(m => ({ m, v: modes[m]?.minutes, d: modes[m]?.detail }))
    .filter(r => r.v != null)
    .sort((a, b) => a.v - b.v);
  const best = rows[0]?.m;
  let html = `<h3>${nameOf(state.origin)} → ${nameOf(state.dest)}</h3>`;
  for (const r of rows) {
    html += `<div class="cmpRow ${r.m === best ? "best" : ""}">
      <span class="dot" style="background:${MODE_HEX[r.m]}"></span>
      <span class="name">${MODE_NAME[r.m]}</span>
      <span class="min" style="color:${MODE_HEX[r.m]}">${r.v}′</span>
    </div>`;
  }
  const hsr = modes.hsr;
  if (state.dest !== state.origin && hsr?.detail) {
    const d = hsr.detail;
    if (d.intra_city) {
      html += `<div class="brk"><div><span class="k">同城直达</span><span class="v">不绕高铁站</span></div></div>`;
    } else if (d.train) {
      const nm = (id) => (HUB_BY_ID[id] || {}).name || id;
      html += `<div class="brk">
        <div><span class="k">${nameOf(state.origin)} → ${nm(d.station_from)}</span><span class="v">${d.access_from}′</span></div>
        <div><span class="k">候车缓冲</span><span class="v">${d.buffer}′</span></div>
        <div><span class="k">${d.train.no} ${d.train.dep}→${d.train.arr}${d.next_day ? "（次日）" : ""}</span><span class="v">${d.ride}′</span></div>
        <div><span class="k">${nm(d.station_to)} → ${nameOf(state.dest)}</span><span class="v">${d.access_to}′</span></div>
      </div>`;
    }
  }
  box.innerHTML = html;
}

function renderRank() {
  const box = $("rank");
  const t = (timesData?.modes || {})[state.mode] || {};
  const originId = state.origin.type === "hub" ? state.origin.id : null;
  const destId = state.dest.type === "hub" ? state.dest.id : null;
  const rows = HUBS
    .filter(h => h.id !== originId)
    .map(h => ({ ...h, v: t[h.id] }))
    .sort((a, b) => (a.v ?? 1e9) - (b.v ?? 1e9));
  let html = `<h3>从「${nameOf(state.origin)}」出发 · ${MODE_NAME[state.mode]}排行（前 12）</h3>`;
  html += "<table>";
  rows.slice(0, 12).forEach(r => {
    const cls = r.id === destId ? ' class="sel"' : "";
    html += `<tr data-hub="${r.id}"${cls}>
      <td>${r.name}</td>
      <td class="mins">${r.v == null ? "—" : r.v + "′"}</td>
    </tr>`;
  });
  html += "</table>";
  box.innerHTML = html;
  box.querySelectorAll("tr[data-hub]").forEach(tr => {
    tr.onclick = () => setDest({ type: "hub", id: tr.dataset.hub });
  });
}

function updateTitles() {
  $("metroTitle").textContent = `时间地铁 · ${MODE_NAME[state.mode]}`;
  $("metroSub").textContent =
    `从「${nameOf(state.origin)}」出发 · 站距 = 门到门分钟（90′外按95′示意）`;
}

/* ---------- 刷新 ---------- */

async function refresh() {
  const seq = ++refreshSeq;
  const fromSpec = specOf(state.origin);
  const toSpec = specOf(state.dest);
  try {
    timesData = await getTimes(fromSpec);
    if (seq !== refreshSeq) return;
    renderMetro(d3.select("#metro"), {
      hubs: enrichedHubs(),
      times: timesData.modes,
      mode: state.mode,
      destId: state.dest.type === "hub" ? state.dest.id : null,
      originId: state.origin.type === "hub" ? state.origin.id : null,
      originCoord: coordOf(state.origin),
      originName: nameOf(state.origin),
      onSelect: (id) => setDest({ type: "hub", id }),
    });
    const iso = await getIso(fromSpec, state.mode, state.buffer);
    if (seq !== refreshSeq) return;
    map.getSource("iso")?.setData(iso);
    routeData = await getRoute(fromSpec, toSpec, state.buffer);
    if (seq !== refreshSeq) return;
    updateRouteLine();
    updateMarkers();
    renderCompare();
    renderRank();
    updateTitles();
  } catch (err) {
    console.error(err);
    window.__appErr = String((err && err.stack) || err);
    $("srcBadge").textContent = "数据加载失败，请查看控制台";
    $("srcBadge").className = "est";
  }
}

function renderChips() {
  const box = $("modeChips");
  box.innerHTML = "";
  for (const m of MODES) {
    const b = document.createElement("button");
    b.className = "chip" + (m === state.mode ? " active" : "");
    b.style.setProperty("--c", MODE_HEX[m]);
    b.textContent = MODE_NAME[m];
    b.onclick = () => {
      state.mode = m;
      renderChips();
      refresh();
    };
    box.appendChild(b);
  }
}

/* ---------- 启动 ---------- */

async function init() {
  await initHubs();
  fillSelect("selOrigin");
  fillSelect("selDest");
  renderChips();
  initMap();

  $("selOrigin").onchange = (e) => setOrigin({ type: "hub", id: e.target.value });
  $("selDest").onchange = (e) => setDest({ type: "hub", id: e.target.value });
  $("btnSwap").onclick = () => {
    [state.origin, state.dest] = [state.dest, state.origin];
    fillSelect("selOrigin");
    fillSelect("selDest");
    refresh();
  };
  $("selBuffer").onchange = (e) => {
    state.buffer = +e.target.value;
    refresh();
  };
  $("pickOrigin").onclick = () => { state.picking = "origin"; syncPickUI(); };
  $("pickDest").onclick = () => { state.picking = "dest"; syncPickUI(); };

  const s = await statusInfo();
  const est = Object.values(s.sources).every(v =>
    v === "estimate" || v === "synthetic");
  const badge = $("srcBadge");
  if (!window.__appErr) {
    badge.textContent = est
      ? "演示估算数据 · 接入高德Key后自动校准"
      : "已接入高德实测数据";
    badge.className = est ? "est" : "amap";
  }
}

syncPickUI();
init();
