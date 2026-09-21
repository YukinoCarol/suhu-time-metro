# -*- coding: utf-8 -*-
"""用高德 Web 服务 API 重算耗时矩阵（需要用户申请的 AMAP_KEY）。

用法:
  set AMAP_KEY=你的key
  python scripts/build_matrix_amap.py

覆盖 data/matrix/{driving,driving_local,transit}.json 的 minutes，
source 标记为 amap（个别失败项回落估算值，source=amap+estimate）。
配额保护：同日缓存跳过、对称 OD 只算一次、失败重试、限速间隔（AMAP_INTERVAL，默认0.15s）。
断点续跑：进度每 50 对落盘到 .tmp/amap_ck_{mode}.json（gitignore），
中断后重跑自动跳过已完成对；连续 5 对失败自动中止（疑似配额耗尽/网络故障，
避免整片回落估算值），配额恢复后重跑即可续传。
Key 保密：AMAP_KEY 只从环境变量读取，任何入库文件不含 Key。
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
TMP = ROOT / ".tmp"
CITY_OF = {h[0]: h[2] for h in HUBS}
INTERVAL = float(os.environ.get("AMAP_INTERVAL", "0.15"))
FLUSH_EVERY = 50
ABORT_AFTER = 5


def amap_get(client, path, params):
    params = {**params, "key": KEY}
    d = None
    for i in range(3):
        d = client.get(f"{BASE}{path}", params=params, timeout=15).json()
        if d.get("status") == "1":
            return d
        time.sleep(0.5 * (i + 1))
    raise RuntimeError(
        f"amap fail {path} infocode={d.get('infocode')} info={d.get('info')}")


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
    TMP.mkdir(exist_ok=True)

    jobs = [
        ("driving", "/direction/driving", {"strategy": 0}, True),
        ("driving_local", "/direction/driving", {"strategy": 3}, True),
        ("transit", "/direction/transit/integrated", {"strategy": 0}, False),
    ]
    abort_all = False
    for mode, path, extra, symmetric in jobs:
        if abort_all:
            break
        cached = load_cached(mode)
        est_matrix = cached["matrix"] if cached else {}
        prev_failed = set()
        if cached:
            prev_failed = set(cached["meta"].get("fallback_pairs", []))

        # 当日断点：已完成对直接复用
        ck_path = TMP / f"amap_ck_{mode}.json"
        results = {}
        if ck_path.exists():
            saved = json.loads(ck_path.read_text(encoding="utf-8"))
            if saved.get("date") == today:
                results = saved.get("results", {})
        if results:
            print(f"{mode}: 断点续跑，已有 {len(results)} 对结果", flush=True)

        out = {a: {} for a in ids}
        api_calls = 0
        fails_in_a_row = 0
        failed = []
        for a in ids:
            if abort_all:
                break
            for b in ids:
                if a == b:
                    continue
                km = est_matrix.get(a, {}).get(b, {}).get("km")
                m = None
                if f"{a}|{b}" in results:
                    m = results[f"{a}|{b}"]
                elif symmetric and f"{b}|{a}" in results:
                    m = results[f"{b}|{a}"]
                elif (cached and cached["meta"].get("updated_at") == today
                        and f"{a}->{b}" not in prev_failed):
                    m = cached["matrix"].get(a, {}).get(b, {}).get("minutes")
                    if m is not None:
                        results[f"{a}|{b}"] = m
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
                        results[f"{a}|{b}"] = m
                        api_calls += 1
                        fails_in_a_row = 0
                        time.sleep(INTERVAL)
                        if api_calls % FLUSH_EVERY == 0:
                            ck_path.write_text(json.dumps(
                                {"date": today, "results": results},
                                ensure_ascii=False), encoding="utf-8")
                            print(f"  {mode}: 已完成 {len(results)} 对（断点已落盘）",
                                  flush=True)
                    except Exception as e:
                        print(f"  {mode} {a}->{b}: {e}，回落估算值", flush=True)
                        m = est_matrix.get(a, {}).get(b, {}).get("minutes")
                        failed.append(f"{a}->{b}")
                        fails_in_a_row += 1
                        if fails_in_a_row >= ABORT_AFTER:
                            print(f"  {mode}: 连续 {ABORT_AFTER} 对失败，疑似配额耗尽"
                                  "或网络中断，中止（断点已保存，重跑可续传）",
                                  flush=True)
                            ck_path.write_text(json.dumps(
                                {"date": today, "results": results},
                                ensure_ascii=False), encoding="utf-8")
                            abort_all = True
                            break
                out[a][b] = {"minutes": m, "km": km}

        if abort_all:
            print(f"{mode}: 未完成，保留原矩阵文件；断点在 {ck_path}", flush=True)
            continue

        ck_path.write_text(json.dumps({"date": today, "results": results},
                                      ensure_ascii=False), encoding="utf-8")
        no_missing = all(out[a][b]["minutes"] is not None
                         for a in ids for b in ids if a != b)
        if not failed and no_missing:
            source = "amap"
        elif no_missing:
            source = "amap+estimate"
        else:
            source = "estimate"
        meta = {
            "mode": mode,
            "source": source,
            "updated_at": today,
            "note": "驾车/公交分钟数=高德实际路径规划结果；km为直线距离。",
        }
        if failed:
            meta["fallback_pairs"] = failed
        data = {"meta": meta, "hubs": ids, "matrix": out}
        (DATA / f"{mode}.json").write_text(
            json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"{mode}: source={source} 已写入"
              f"（API调用 {api_calls} 次，失败回落 {len(failed)} 对）", flush=True)
        if failed:
            print(f"{mode}: 失败对清单: {', '.join(failed)}", flush=True)


if __name__ == "__main__":
    build()
