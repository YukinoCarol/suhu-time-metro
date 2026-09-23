# -*- coding: utf-8 -*-
"""苏沪耗时地铁 · FastAPI 后端（耗时计算的唯一来源）。

端点:
- GET /api/status                  数据来源状态
- GET /api/hubs                    枢纽 GeoJSON
- GET /api/times?from=&buffer=     起点→全部枢纽 四方式分钟数
- GET /api/route?from=&to=&buffer= 起终点四方式分钟数 + 画线 legs
- GET /api/route_geo?from=&to=&mode= 真实路线几何+经过路段/换乘明细（按需调高德，磁盘缓存）
- GET /api/isochrone?from=&mode=   等时面 GeoJSON（IDW 插值格网）
- GET /api/data/...                数据原文件；/ 挂载前端静态页

origin/destination 规格: "hub:<id>" 或 "<lng>,<lat>"。
query_now: 出发时刻（当日分钟数），用于高铁门到门选最近一班车。
"""

import hashlib
import json
import os
import sys
from datetime import datetime
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from estimate_model import (  # noqa: E402
    drive_local_minutes,
    drive_minutes,
    haversine_km,
    transit_minutes,
)
from isochrone import (  # noqa: E402
    THRESHOLDS,
    build_isochrone,
    calibration_factor,
    est_lookup,
)

DATA = ROOT / "data"
WEB = ROOT / "apps" / "web"
AMAP_KEY = os.environ.get("AMAP_KEY", "")
MODES = ["driving", "driving_local", "transit", "hsr"]
EST_FN = {
    "driving": drive_minutes,
    "driving_local": drive_local_minutes,
    "transit": transit_minutes,
}


def _load(path):
    with path.open(encoding="utf-8") as f:
        return json.load(f)


_hubs_gj = _load(DATA / "hubs.geojson")
HUBS = [ft["properties"] for ft in _hubs_gj["features"]]
HUB_BY_ID = {h["id"]: h for h in HUBS}
HUB_COORD = {ft["properties"]["id"]: ft["geometry"]["coordinates"]
             for ft in _hubs_gj["features"]}

MATRICES = {
    m: _load(DATA / "matrix" / f"{m}.json")
    for m in ("driving", "driving_local", "transit")
}
HSR = _load(DATA / "hsr.json")
HSR_STATIONS = sorted(set(HSR["stations"]))
TRAINS_BY_PAIR = {}
for t in HSR["trains"]:
    TRAINS_BY_PAIR.setdefault((t["from"], t["to"]), []).append(t)
HSR_STATIONS_BY_CITY = {}
for s in HSR_STATIONS:
    HSR_STATIONS_BY_CITY.setdefault(HUB_BY_ID[s]["city"], []).append(s)


def _hm(s):
    h, m = s.split(":")
    return int(h) * 60 + int(m)


def parse_point(spec):
    """→ (kind, hub_id|None, [lng, lat])"""
    if spec.startswith("hub:"):
        hid = spec[4:]
        if hid not in HUB_BY_ID:
            raise HTTPException(404, f"unknown hub {hid}")
        return "hub", hid, HUB_COORD[hid]
    try:
        lng, lat = (float(v) for v in spec.split(","))
    except ValueError:
        raise HTTPException(400, f"bad point: {spec!r}")
    return "point", None, [lng, lat]


def nearest_hub(lng, lat):
    best, bd = None, 1e18
    for h in HUBS:
        c = HUB_COORD[h["id"]]
        d = haversine_km(lng, lat, c[0], c[1])
        if d < bd:
            best, bd = h, d
    return best


def access_minutes(p_from, p_to, coord_from=None, coord_to=None):
    """接驳分钟（起点侧/终点侧通用）：
    - 两端都是枢纽：直接查高德实测矩阵（公交取驾车与公交较优，与矩阵口径一致）；
    - 含自由点：沿用估算模型兜底（不产生 API 调用）。
    coord_* 为自由点坐标；枢纽坐标自动查表。
    """
    a = coord_from if coord_from is not None else HUB_COORD[p_from]
    b = coord_to if coord_to is not None else HUB_COORD[p_to]
    if p_from and p_to:
        m = MATRICES["driving"]["matrix"][p_from][p_to]["minutes"]
        t = MATRICES["transit"]["matrix"][p_from][p_to]["minutes"]
        return min(m, t + 2)
    km = haversine_km(a[0], a[1], b[0], b[1])
    return min(drive_minutes(km), transit_minutes(km) + 2)


def rail_options(hub_from, hub_to):
    """枚举（出发站, 到达站, 车次列表）组合，两端均限同城站。"""
    out = []
    for so in HSR_STATIONS_BY_CITY.get(HUB_BY_ID[hub_from]["city"], []):
        for sd in HSR_STATIONS_BY_CITY.get(HUB_BY_ID[hub_to]["city"], []):
            trains = TRAINS_BY_PAIR.get((so, sd), [])
            if trains:
                out.append((so, sd, trains))
    return out


def _pick_train(trains, depart_at):
    ts = sorted(trains, key=lambda x: x["dep"])
    tr = next((x for x in ts if _hm(x["dep"]) >= depart_at), None)
    if tr is None:
        return ts[0], True  # 次日首班
    return tr, False


def hsr_entry(p_from, p_to, buffer=20, query_now=None):
    """高铁门到门：接驳 + 候车 + 车程 + 出站接驳，枚举站对取最优。"""
    ka, ha, ca = parse_point(p_from)
    kb, hb, cb = parse_point(p_to)
    if ha and hb and ha == hb:
        return {"minutes": 0, "legs": [], "detail": None}
    # 两枢纽同城：不绕高铁站，直达地面交通取较优
    if (ha and hb and HUB_BY_ID[ha]["city"] == HUB_BY_ID[hb]["city"]):
        if ha == hb:
            return {"minutes": 0, "legs": [], "detail": None}
        # 双枢纽同城走实测矩阵；含自由点走估算兜底
        m = access_minutes(ha, hb, ca, cb)
        return {"minutes": m, "legs": [[ca, cb]],
                "detail": {"intra_city": True, "total": m,
                           "note": "同城直达（开车/公交取较优，不绕高铁）"}}

    def cand_stations(side_hub, side_coord):
        if side_hub is not None:
            return [s for s in HSR_STATIONS_BY_CITY.get(
                HUB_BY_ID[side_hub]["city"], [])]
        out = []
        for s in HSR_STATIONS:
            c = HUB_COORD[s]
            if drive_minutes(haversine_km(side_coord[0], side_coord[1],
                                          c[0], c[1])) <= 60:
                out.append(s)
        return out

    best = None
    for so in cand_stations(ha, ca):
        acc_o = 0 if ha == so else access_minutes(ha, so, ca, None)
        for sd in cand_stations(hb, cb):
            trains = TRAINS_BY_PAIR.get((so, sd), [])
            if not trains:
                continue
            acc_d = 0 if hb == sd else access_minutes(hb, sd, cb, None)
            ride = min(t["minutes"] for t in trains)
            total = acc_o + buffer + ride + acc_d
            cand = {"total": total, "station_from": so, "station_to": sd,
                    "ride": ride, "access_from": acc_o, "access_to": acc_d,
                    "buffer": buffer}
            if query_now is not None:
                # 有出发时刻：按所乘班次的实际候车与车程重算，
                # 保证 total 与展示车次自洽（原实现 total 用最快班次、
                # 展示下一班，二者不一致）
                tr, nd = _pick_train(trains, query_now + acc_o + buffer)
                wait = (_hm(tr["dep"]) + (1440 if nd else 0)
                        - query_now - acc_o)
                total = acc_o + wait + tr["minutes"] + acc_d
                cand.update({"total": total, "ride": tr["minutes"],
                             "wait": wait, "train": tr})
                if nd:
                    cand["next_day"] = True
            if best is None or total < best["total"]:
                best = cand
    if best is None:
        return {"minutes": None, "legs": [], "detail": None}
    so, sd = best["station_from"], best["station_to"]
    legs = []
    if ha != so:
        legs.append([ca, HUB_COORD[so]])
    legs.append([HUB_COORD[so], HUB_COORD[sd]])
    if hb != sd:
        legs.append([HUB_COORD[sd], cb])
    return {"minutes": best["total"], "legs": legs, "detail": best}


def matrix_entry(p_from, p_to, mode):
    ka, ha, ca = parse_point(p_from)
    kb, hb, cb = parse_point(p_to)
    if ha and hb and ha == hb:
        return {"minutes": 0, "legs": [], "detail": None}
    if ha and hb:
        m = MATRICES[mode]["matrix"][ha][hb]["minutes"]
        return {"minutes": m, "legs": [[ca, cb]], "detail": None}
    hub_a = ha or nearest_hub(*ca)["id"]
    hub_b = hb or nearest_hub(*cb)["id"]
    fn = EST_FN[mode]
    total = 0
    legs = []
    if ha is None:
        c2 = HUB_COORD[hub_a]
        total += fn(haversine_km(ca[0], ca[1], c2[0], c2[1]))
        legs.append([ca, c2])
    c2, c3 = HUB_COORD[hub_a], HUB_COORD[hub_b]
    if hub_a != hub_b:  # 自由点就近枢纽与目标枢纽相同时，矩阵无对角线，只算两端接驳
        total += MATRICES[mode]["matrix"][hub_a][hub_b]["minutes"]
        legs.append([c2, c3])
    if hb is None:
        c4 = HUB_COORD[hub_b]
        total += fn(haversine_km(c4[0], c4[1], cb[0], cb[1]))
        legs.append([c4, cb])
    return {"minutes": total, "legs": legs, "detail": None}


def time_entry(mode, p_from, p_to, buffer=20, query_now=None):
    if mode == "hsr":
        return hsr_entry(p_from, p_to, buffer, query_now)
    return matrix_entry(p_from, p_to, mode)


app = FastAPI(title="苏沪耗时地铁", version="0.1.0")


@app.get("/api/status")
def api_status():
    return {
        "amap_configured": bool(AMAP_KEY),
        "sources": {**{m: MATRICES[m]["meta"]["source"]
                       for m in ("driving", "driving_local", "transit")},
                    "hsr": HSR["meta"]["source"]},
        "hubs": len(HUBS),
    }


@app.get("/api/hubs")
def api_hubs():
    return _hubs_gj


@app.get("/api/times")
def api_times(from_: str = Query(..., alias="from"), buffer: int = 20):
    ka, ha, ca = parse_point(from_)
    out = {m: {} for m in MODES}
    for h in HUBS:
        for m in MODES:
            out[m][h["id"]] = time_entry(m, from_, f"hub:{h['id']}",
                                         buffer)["minutes"]
    return {"from": from_, "modes": out}


@app.get("/api/route")
def api_route(from_: str = Query(..., alias="from"), to: str = Query(...),
              buffer: int = 20):
    ka, ha, ca = parse_point(from_)
    kb, hb, cb = parse_point(to)
    now = datetime.now()
    query_now = now.hour * 60 + now.minute
    modes = {m: time_entry(m, from_, to, buffer, query_now) for m in MODES}
    return {"from": from_, "to": to, "modes": modes}


_ISO_CACHE: dict = {}
ISO_CACHE_DIR = DATA / "iso_cache"

# 等时圈野外段校准系数：用实测矩阵反推估算模型的系统偏差（模块加载时算一次）
ISO_LUT = {  # 各模式的外推用估算函数 + 查表
    "driving": est_lookup(drive_minutes),
    "driving_local": est_lookup(drive_local_minutes),
    "transit": est_lookup(transit_minutes),
    "hsr": est_lookup(drive_minutes),   # 高铁野外段按驾车位移估计
}
ISO_CALIB = {
    m: calibration_factor(MATRICES[m]["matrix"], EST_FN[m], [h["id"] for h in HUBS],
                          HUB_COORD)
    for m in ("driving", "driving_local", "transit")
}
ISO_CALIB["hsr"] = ISO_CALIB["driving"]


def _iso_cache_path(mode, from_spec, buffer):
    h = hashlib.md5(f"{mode}|{from_spec}|{buffer}".encode()).hexdigest()[:16]
    return ISO_CACHE_DIR / f"{mode}_{h}.json"


@app.get("/api/isochrone")
def api_isochrone(from_: str = Query(..., alias="from"),
                  mode: str = "driving", buffer: int = 20):
    """等时圈 v2：细格网加性场 + Marching Squares 矢量化，8 档至 180′。"""
    if mode not in MODES:
        raise HTTPException(400, f"bad mode: {mode}")
    key = (mode, from_, buffer)
    if key in _ISO_CACHE:
        return _ISO_CACHE[key]
    cp = _iso_cache_path(mode, from_, buffer)
    if cp.exists():
        _ISO_CACHE[key] = _load(cp)
        return _ISO_CACHE[key]
    hub_min = {}
    for h in HUBS:
        ent = time_entry(mode, from_, f"hub:{h['id']}", buffer)
        hub_min[h["id"]] = ent["minutes"]
    # 等时圈按直线位移外推（非逐点路径规划），故用几何直线距离而非链路耗时
    est_fn = EST_FN.get(mode) or drive_minutes
    fc, meta = build_isochrone(hub_min, HUB_COORD, est_fn=est_fn,
                               calib=ISO_CALIB.get(mode, 1.0))
    meta.update({"mode": mode, "from": from_, "buffer": buffer,
                 "source": "interpolated-contour",
                 "hub_minutes": hub_min})
    fc["meta"] = meta
    ISO_CACHE_DIR.mkdir(exist_ok=True)
    cp.write_text(json.dumps(fc, ensure_ascii=False), encoding="utf-8")
    _ISO_CACHE[key] = fc
    return fc


# ---------- 真实路线几何（方案A：按需实调高德 + 磁盘缓存，无Key/失败回落直线） ----------

GEO_CACHE = DATA / "route_geo"
GEO_CACHE.mkdir(exist_ok=True)
AMAP_BASE = "https://restapi.amap.com/v3"


def _geo_cache_path(mode, from_spec, to_spec):
    h = hashlib.md5(f"{mode}|{from_spec}|{to_spec}".encode()).hexdigest()[:16]
    return GEO_CACHE / f"{mode}_{h}.json"


def _coord_spec(spec):
    """hub:id → "lng,lat"（高德接口只认坐标）。"""
    k, h, c = parse_point(spec)
    return f"{c[0]:.6f},{c[1]:.6f}"


def _amap_get(path, params):
    """带重试的高德 GET；Key 未配置或调用失败返回 None。"""
    if not AMAP_KEY:
        return None
    try:
        d = httpx.get(f"{AMAP_BASE}{path}",
                      params={**params, "key": AMAP_KEY}, timeout=15).json()
        if d.get("status") == "1":
            return d
    except Exception:
        pass
    return None


def _straight_geo(from_spec, to_spec, mode):
    """回落：直线示意（与旧行为一致）。"""
    _, ha, ca = parse_point(from_spec)
    _, hb, cb = parse_point(to_spec)
    return {"type": "Feature", "geometry": {"type": "LineString",
                                            "coordinates": [ca, cb]},
            "properties": {"mode": mode, "kind": "straight"}}


def _drive_geo(from_spec, to_spec, mode):
    """驾车（strategy 0=高速 / 3=不走高速）：真实道路折线 + 主要经过道路。"""
    d = _amap_get("/direction/driving", {
        "origin": _coord_spec(from_spec), "destination": _coord_spec(to_spec),
        "strategy": 0 if mode == "driving" else 3,
        "extensions": "base",
    })
    if d is None:
        return None
    p = d["route"]["paths"][0]
    pts = []
    for st in p["steps"]:
        pts += [[float(v) for v in xy.split(",")]
                for xy in st["polyline"].split(";")]
    roads, seen = [], set()
    for st in p["steps"]:
        name = st.get("road") or st.get("name") or ""
        if name and name not in seen:
            seen.add(name)
            roads.append(name)
    return {"type": "Feature", "geometry": {"type": "LineString",
                                            "coordinates": pts},
            "properties": {"mode": mode, "kind": "amap",
                           "km": round(int(p["distance"]) / 1000, 1),
                           "roads": roads[:12]}}


def _transit_geo(from_spec, to_spec):
    """公交：真实换乘链（线路号/步行段）+ 各段折线。城市取起终点所在城市
    （枢纽查表，自由点取最近枢纽所在城市）。"""
    _, ha, ca = parse_point(from_spec)
    _, hb, cb = parse_point(to_spec)
    city_o = HUB_BY_ID[ha]["city"] if ha else nearest_hub(*ca)["city"]
    city_d = HUB_BY_ID[hb]["city"] if hb else nearest_hub(*cb)["city"]
    d = _amap_get("/direction/transit/integrated", {
        "origin": _coord_spec(from_spec), "destination": _coord_spec(to_spec),
        "city": city_o, "cityd": city_d,
        "strategy": 0,
    })
    if d is None:
        return None
    ts = d["route"]["transits"]
    if not ts:
        return None
    t = ts[0]
    segs, lines_all, walk_m = [], [], 0
    for seg in t.get("segments", []):
        if seg.get("walking") and seg["walking"].get("distance"):
            walk_m += int(seg["walking"]["distance"])
            wr = []
            for st in seg["walking"].get("steps", []):
                wr += [[float(v) for v in xy.split(",")]
                       for xy in st["polyline"].split(";")]
            if wr:
                segs.append({"kind": "walk", "m": int(seg["walking"]["distance"]),
                             "coords": wr})
        for bus in seg.get("bus", {}).get("buslines", []):
            name = bus.get("name", "")
            lines_all.append(name)
            br = [[float(v) for v in xy.split(",")]
                  for xy in bus.get("polyline", "").split(";") if xy]
            segs.append({"kind": "bus", "name": name,
                         "m": int(bus.get("distance", 0)), "coords": br})
    coords = [c for s in segs for c in s["coords"]]
    if len(coords) < 2:
        return None
    return {"type": "Feature", "geometry": {"type": "LineString",
                                            "coordinates": coords},
            "properties": {"mode": "transit", "kind": "amap",
                           "km": round(int(d["route"]["distance"]) / 1000, 1),
                           "walk_m": walk_m, "lines": lines_all, "segs": segs}}


def _hsr_geo(from_spec, to_spec, buffer):
    """高铁：站到站示意线 + 接驳直线；明细复用 hsr_entry（含真实车次）。"""
    now = datetime.now()
    r = hsr_entry(from_spec, to_spec, buffer, now.hour * 60 + now.minute)
    _, ha, ca = parse_point(from_spec)
    _, hb, cb = parse_point(to_spec)
    segs = []
    for a, b in r["legs"]:
        segs.append({"kind": "hsr" if (a in [HUB_COORD[s] for s in HSR_STATIONS]
                                       and b in [HUB_COORD[s] for s in HSR_STATIONS])
                     else "access", "coords": [a, b]})
    props = {"mode": "hsr", "kind": "schematic"}
    d = r.get("detail")
    if d and not d.get("intra_city") and d.get("train"):
        nm = lambda i: (HUB_BY_ID[i] or {}).get("name", i)
        tr = d["train"]
        props.update({"train_no": tr["no"], "train_dep": tr["dep"],
                      "train_arr": tr["arr"],
                      "station_from": nm(d["station_from"]),
                      "station_to": nm(d["station_to"])})
    return {"type": "Feature", "geometry": {"type": "LineString",
                                            "coordinates": [c for s in segs for c in s["coords"]]},
            "properties": {**props, "segs": segs}}


@app.get("/api/route_geo")
def api_route_geo(from_: str = Query(..., alias="from"),
                  to: str = Query(...), mode: str = "driving",
                  buffer: int = 20):
    if mode not in MODES:
        raise HTTPException(400, f"bad mode: {mode}")
    ka, ha, ca = parse_point(from_)
    kb, hb, cb = parse_point(to)
    if ha and hb and ha == hb:
        return {"route": None, "note": "same_point"}
    cp = _geo_cache_path(mode, from_, to)
    if cp.exists():  # 磁盘缓存：同 OD 同模式不重复调 API
        return _load(cp)
    geo = None
    if mode in ("driving", "driving_local"):
        geo = _drive_geo(_coord_spec(from_), _coord_spec(to), mode)
    elif mode == "transit":
        geo = _transit_geo(_coord_spec(from_), _coord_spec(to))
    else:
        geo = _hsr_geo(from_, to, buffer)
    if geo is None:  # 无 Key / 调用失败 / 无方案 → 直线回落
        geo = _straight_geo(from_, to, mode)
    out = {"route": geo,
           "generated_at": datetime.now().isoformat(timespec="seconds")}
    if geo["properties"].get("kind") == "amap":
        cp.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    return out


@app.get("/api/data/hubs.geojson")
def api_data_hubs():
    return FileResponse(DATA / "hubs.geojson",
                        media_type="application/geo+json")


@app.get("/api/data/matrix/{mode}")
def api_data_matrix(mode: str):
    if mode not in MATRICES:
        raise HTTPException(404)
    return FileResponse(DATA / "matrix" / f"{mode}.json",
                        media_type="application/json")


@app.get("/api/data/hsr.json")
def api_data_hsr():
    return FileResponse(DATA / "hsr.json", media_type="application/json")


@app.middleware("http")
async def static_revalidate(request, call_next):
    # 静态资源要求协商缓存（ETag 重验证），避免前端更新后浏览器仍用启发式缓存的旧模块
    resp = await call_next(request)
    if request.url.path == "/" or request.url.path.startswith("/js/"):
        resp.headers["Cache-Control"] = "no-cache"
    return resp


app.mount("/", StaticFiles(directory=WEB, html=True), name="web")
