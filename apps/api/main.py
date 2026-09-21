# -*- coding: utf-8 -*-
"""苏沪耗时地铁 · FastAPI 后端（耗时计算的唯一来源）。

端点:
- GET /api/status                  数据来源状态
- GET /api/hubs                    枢纽 GeoJSON
- GET /api/times?from=&buffer=     起点→全部枢纽 四方式分钟数
- GET /api/route?from=&to=&buffer= 起终点四方式分钟数 + 画线 legs
- GET /api/isochrone?from=&mode=   等时面 GeoJSON（IDW 插值格网）
- GET /api/data/...                数据原文件；/ 挂载前端静态页

origin/destination 规格: "hub:<id>" 或 "<lng>,<lat>"。
query_now: 出发时刻（当日分钟数），用于高铁门到门选最近一班车。
"""

import json
import os
import sys
from datetime import datetime
from pathlib import Path

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


def access_minutes(hub, station):
    """枢纽↔高铁站 接驳分钟：取驾车/公交中较优。"""
    if hub == station:
        return 0
    a, b = HUB_COORD[hub], HUB_COORD[station]
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
        km = haversine_km(ca[0], ca[1], cb[0], cb[1])
        m = min(drive_minutes(km), transit_minutes(km) + 2)
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
        acc_o = 0 if ha == so else drive_minutes(
            haversine_km(ca[0], ca[1], *HUB_COORD[so]))
        for sd in cand_stations(hb, cb):
            trains = TRAINS_BY_PAIR.get((so, sd), [])
            if not trains:
                continue
            acc_d = 0 if hb == sd else drive_minutes(
                haversine_km(*HUB_COORD[sd], cb[0], cb[1]))
            ride = min(t["minutes"] for t in trains)
            total = acc_o + buffer + ride + acc_d
            cand = {"total": total, "station_from": so, "station_to": sd,
                    "ride": ride, "access_from": acc_o, "access_to": acc_d,
                    "buffer": buffer}
            if query_now is not None:
                tr, nd = _pick_train(trains, query_now + acc_o + buffer)
                cand["train"] = tr
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


@app.get("/api/isochrone")
def api_isochrone(from_: str = Query(..., alias="from"),
                  mode: str = "driving", buffer: int = 20):
    if mode not in MODES:
        raise HTTPException(400, f"bad mode: {mode}")
    key = (mode, from_, buffer)
    cached = _ISO_CACHE.get(key)
    if cached is not None:
        return cached
    hub_min = {h["id"]: time_entry(mode, from_, f"hub:{h['id']}", buffer)["minutes"]
               for h in HUBS}
    valid = [(k, v) for k, v in hub_min.items() if v is not None]
    # 范围足够大，让 90′ 等时线在图幅内自然闭合（90′ 驾车半径约 100km）
    bbox = (119.35, 30.25, 122.25, 32.25)
    step_lon, step_lat = 0.02, 0.017
    n_lon = int((bbox[2] - bbox[0]) / step_lon)
    n_lat = int((bbox[3] - bbox[1]) / step_lat)
    feats = []
    for i in range(n_lon):
        for j in range(n_lat):
            lng = bbox[0] + (i + 0.5) * step_lon
            lat = bbox[1] + (j + 0.5) * step_lat
            num = den = 0.0
            for hid, mv in valid:
                c = HUB_COORD[hid]
                d = haversine_km(lng, lat, c[0], c[1]) + 0.5
                w = 1.0 / (d * d)
                num += w * mv
                den += w
            t = num / den
            band = 5
            for k, th in enumerate((15, 30, 45, 60, 90)):
                if t <= th:
                    band = k
                    break
            half_lon, half_lat = step_lon / 2, step_lat / 2
            feats.append({
                "type": "Feature",
                "geometry": {"type": "Polygon", "coordinates": [[
                    [lng - half_lon, lat - half_lat],
                    [lng + half_lon, lat - half_lat],
                    [lng + half_lon, lat + half_lat],
                    [lng - half_lon, lat + half_lat],
                    [lng - half_lon, lat - half_lat],
                ]]},
                "properties": {"band": band, "minutes": round(t)},
            })
    result = {"type": "FeatureCollection", "features": feats,
              "meta": {"mode": mode, "from": from_,
                       "source": "estimate-interpolated",
                       "hub_minutes": hub_min}}
    _ISO_CACHE[key] = result
    return result


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
