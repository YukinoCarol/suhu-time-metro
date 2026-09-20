# -*- coding: utf-8 -*-
"""用高德 Web 服务 API 重算耗时矩阵（需要用户申请的 AMAP_KEY）。

用法:
  set AMAP_KEY=你的key
  python scripts/build_matrix_amap.py

覆盖 data/matrix/{driving,driving_local,transit}.json 的 minutes，
source 标记为 amap（个别失败项回落估算值，source=amap+estimate）。
配额保护：同日缓存跳过、对称 OD 只算一次、失败重试、0.15s 间隔。
"""
import json
import os
import sys
import time
from datetime import date
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).parent))
from hubs import HUBS

KEY = os.environ.get("AMAP_KEY", "")
BASE = "https://restapi.amap.com/v3"
DATA = ROOT / "data" / "matrix"
CITY_OF = {h[0]: h[2] for h in HUBS}


def amap_get(client, path, params):
    params = {**params, "key": KEY}
    d = None
    for i in range(3):
        d = client.get(f"{BASE}{path}", params=params, timeout=15).json()
        if d.get("status") == "1":
            return d
        time.sleep(0.5 * (i + 1))
    raise RuntimeError(f"amap fail {path}: {d}")


def load_cached(mode):
    p = DATA / f"{mode}.json"
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return None


def build():
    if not KEY:
        print("请先 set AMAP_KEY=xxx（Web服务Key，需开通驾车/公交路径规划）")
        sys.exit(1)
    ids = [h[0] for h in HUBS]
    coords = {h[0]: (h[4], h[5]) for h in HUBS}
    today = date.today().isoformat()
    client = httpx.Client()

    jobs = [
        ("driving", "/direction/driving", {"strategy": 0}, True),
        ("driving_local", "/direction/driving", {"strategy": 3}, True),
        ("transit", "/direction/transit/integrated", {"strategy": 0}, False),
    ]
    for mode, path, extra, symmetric in jobs:
        cached = load_cached(mode)
        est_matrix = cached["matrix"] if cached else {}
        out = {}
        for a in ids:
            out[a] = {}
            for b in ids:
                if a == b:
                    continue
                km = est_matrix.get(a, {}).get(b, {}).get("km")
                m = None
                if cached and cached["meta"].get("updated_at") == today:
                    m = cached["matrix"].get(a, {}).get(b, {}).get("minutes")
                if m is None and symmetric and b in out and a in out[b]:
                    m = out[b][a]["minutes"]
                if m is None:
                    try:
                        params = dict(extra)
                        params["origin"] = f"{coords[a][0]:.6f},{coords[a][1]:.6f}"
                        params["destination"] = f"{coords[b][0]:.6f},{coords[b][1]:.6f}"
                        if mode == "transit":
                            params["city"] = CITY_OF[a]
                            params["cityd"] = CITY_OF[b]
                        d = amap_get(client, path, params)
                        if mode == "transit":
                            dur = int(d["route"]["transits"][0]["duration"])
                        else:
                            dur = int(d["route"]["paths"][0]["duration"])
                        m = round(dur / 60)
                        time.sleep(0.15)
                    except Exception as e:
                        print(f"  {mode} {a}->{b}: {e}，回落估算值")
                        m = est_matrix.get(a, {}).get(b, {}).get("minutes")
                out[a][b] = {"minutes": m, "km": km}

        have_amap = all(out[a][b]["minutes"] is not None
                        for a in ids for b in ids if a != b)
        data = {
            "meta": {
                "mode": mode,
                "source": "amap" if have_amap else "amap+estimate",
                "updated_at": today,
                "note": "驾车/公交分钟数=高德实际路径规划结果；km为直线距离。",
            },
            "hubs": ids,
            "matrix": out,
        }
        (DATA / f"{mode}.json").write_text(
            json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"{mode}: source={data['meta']['source']} 已写入")


if __name__ == "__main__":
    build()
