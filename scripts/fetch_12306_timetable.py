# -*- coding: utf-8 -*-
"""抓取 12306 公开余票查询接口的苏沪 OD 时刻（用户授权运行，Issue #2）。

只查 10 个高铁站之间的跨市 OD（42 组，双向），带限速/重试/当日断点缓存；
产出 data/hsr_timetable.csv（OD 级: 车次,出发站,到达站,发时,到时,历时），
再调用 scripts/build_hsr_from_csv.py 生成 data/hsr.json（source=real）。
最后用另一接口（车次经停查询）抽 5 条核对，输出 PASS/FAIL。

用法:
  python scripts/fetch_12306_timetable.py [--date 2026-09-24] [--interval 1.2] [--refresh]
说明: 仅查询公开余票接口，不登录、不购票；不涉及任何个人数据。
口径: 只收 G/D/C（高铁/动车/城际）车次。
"""
import argparse
import json
import re
import subprocess
import sys
import time
from datetime import date, timedelta
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
TMP = ROOT / ".tmp"
DATA = ROOT / "data"
OUT_CSV = DATA / "hsr_timetable.csv"

# 10 个高铁站的 12306 站名；电报码优先从 station_name.js 自动发现
STATION_HUB = {
    "苏州": "stn", "苏州北": "szn", "苏州园区": "syq", "苏州新区": "sxq",
    "昆山南": "ksn", "苏州南": "szs", "太仓": "tcs",
    "上海虹桥": "shq", "上海": "sh", "上海南": "shn",
}
SZ_SIDE = ["苏州", "苏州北", "苏州园区", "苏州新区", "昆山南", "苏州南", "太仓"]
SH_SIDE = ["上海虹桥", "上海", "上海南"]
FALLBACK_TELE = {
    "苏州": "SZH", "苏州北": "OHH", "昆山南": "KNH",
    "上海": "SHH", "上海虹桥": "AOH", "上海南": "SNH",
}

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://kyfw.12306.cn/otn/leftTicket/init",
}
QUERY_PATHS = [
    "/otn/leftTicket/queryZ", "/otn/leftTicket/query", "/otn/leftTicket/queryA",
    "/otn/leftTicket/queryG", "/otn/leftTicket/queryU", "/otn/leftTicket/queryE",
    "/otn/leftTicket/queryC", "/otn/leftTicket/queryT", "/otn/leftTicket/queryJ",
]
TIME_RE = re.compile(r"^\d{2}:\d{2}$")
TRAIN_RE = re.compile(r"^[GDC]\d+[A-Z]?$")


def discover_telecodes(client):
    """从官方 station_name.js 里找 10 站的电报码。"""
    r = client.get(
        "https://kyfw.12306.cn/otn/resources/js/framework/station_name.js",
        timeout=25)
    tele = {}
    for m in re.finditer(r"@[^@]+?\|([^|]+)\|([A-Z]{2,4})\|", r.text):
        name, tc = m.group(1), m.group(2)
        if name in STATION_HUB and name not in tele:
            tele[name] = tc
    return tele


def query_once(client, path, date_s, from_t, to_t):
    params = {
        "leftTicketDTO.train_date": date_s,
        "leftTicketDTO.from_station": from_t,
        "leftTicketDTO.to_station": to_t,
        "purpose_codes": "ADULT",
    }
    r = client.get(f"https://kyfw.12306.cn{path}", params=params, timeout=20)
    d = r.json()
    return d


def probe_query_path(client, date_s, from_t, to_t, hint=None):
    """找到当前可用的查询端点（12306 偶尔改路径）。"""
    order = ([f"/otn/leftTicket/{hint}"] if hint else []) + QUERY_PATHS
    for path in order:
        try:
            d = query_once(client, path, date_s, from_t, to_t)
            res = d.get("data", {}).get("result") if isinstance(d, dict) else None
            if isinstance(res, list):
                print(f"  使用查询端点 {path}（返回 {len(res)} 条车次）", flush=True)
                return path
            hint2 = d.get("c_url") if isinstance(d, dict) else None
            if hint2 and f"/otn/leftTicket/{hint2}" not in order:
                order.insert(0, f"/otn/leftTicket/{hint2}")
        except Exception as e:
            print(f"  端点 {path} 不可用: {type(e).__name__}", flush=True)
        time.sleep(0.8)
    return None


def parse_row(s):
    f = s.split("|")
    if len(f) < 15:
        return None
    train_no, no = f[2], f[3]
    from_t, to_t = f[6], f[7]
    dep, arr, lishi = f[8], f[9], f[10]
    if not (TRAIN_RE.match(no) and TIME_RE.match(dep) and TIME_RE.match(arr)):
        return None
    return {"train_no": train_no, "no": no, "from_t": from_t, "to_t": to_t,
            "dep": dep, "arr": arr, "lishi": lishi}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date",
                    default=(date.today() + timedelta(days=3)).isoformat(),
                    help="查询日期（余票查询口径=当日运行图，默认今天+3天）")
    ap.add_argument("--interval", type=float, default=1.2, help="请求间隔秒")
    ap.add_argument("--refresh", action="store_true", help="忽略当日断点缓存重抓")
    args = ap.parse_args()

    TMP.mkdir(exist_ok=True)
    date_s = args.date
    ck_path = TMP / "hsr12306_ck.json"
    ck = {}
    if ck_path.exists() and not args.refresh:
        saved = json.loads(ck_path.read_text(encoding="utf-8"))
        if saved.get("date") == date_s:
            ck = saved

    client = httpx.Client(headers=HEADERS, follow_redirects=True)

    tele = ck.get("tele")
    if not tele:
        client.get("https://kyfw.12306.cn/otn/leftTicket/init", timeout=20)
        tele = {**discover_telecodes(client)}
        missing = [n for n in STATION_HUB if n not in tele]
        if missing:
            print(f"提示: station_name.js 未含 {missing}，尝试内置电报码兜底", flush=True)
        tele = {**FALLBACK_TELE, **tele}

    pairs = [(a, b) for a in SZ_SIDE for b in SH_SIDE] + \
            [(b, a) for a in SZ_SIDE for b in SH_SIDE]
    pairs = [p for p in pairs if p[0] in tele and p[1] in tele]
    skip = len([(a, b) for a in SZ_SIDE for b in SH_SIDE] +
               [(b, a) for a in SZ_SIDE for b in SH_SIDE]) - len(pairs)
    if skip:
        print(f"提示: {skip} 个区间因缺电报码跳过", flush=True)

    path = ck.get("path") or probe_query_path(
        client, date_s, tele["苏州"], tele["上海虹桥"])
    if not path:
        print("错误: 所有 12306 查询端点都不可用（可能被风控/接口改版）。"
              "断点已保留，请稍后重试或改走手动 CSV 路径。", flush=True)
        sys.exit(1)

    rows = dict(ck.get("rows", {}))          # key: "A>B" -> [record...]
    for i, (a, b) in enumerate(pairs, 1):
        key = f"{a}>{b}"
        if key in rows:
            continue
        recs = []
        for attempt in range(3):
            try:
                d = query_once(client, path, date_s, tele[a], tele[b])
                tele_name = d.get("data", {}).get("map", {})
                for s in d["data"]["result"]:
                    rec = parse_row(s)
                    if rec:
                        rec["from"] = tele_name.get(rec["from_t"], a)
                        rec["to"] = tele_name.get(rec["to_t"], b)
                        recs.append(rec)
                break
            except Exception as e:
                print(f"  {key} 第{attempt+1}次失败: {type(e).__name__}", flush=True)
                time.sleep(1.5 * (attempt + 1))
        rows[key] = recs
        n = len(recs)
        print(f"  [{i}/{len(pairs)}] {key}: {n} 条 G/D/C 车次", flush=True)
        time.sleep(args.interval)
        if i % 5 == 0:
            ck_path.write_text(json.dumps(
                {"date": date_s, "tele": tele, "path": path.split("/")[-1],
                 "rows": rows}, ensure_ascii=False), encoding="utf-8")

    ck_path.write_text(json.dumps(
        {"date": date_s, "tele": tele, "path": path.split("/")[-1], "rows": rows},
        ensure_ascii=False), encoding="utf-8")

    # 去重: 同 (车次,出发,到达,发时) 只留一条
    seen = set()
    flat = []
    for key, recs in rows.items():
        for rec in recs:
            k = (rec["no"], rec["from"], rec["to"], rec["dep"])
            if k not in seen:
                seen.add(k)
                flat.append(rec)
    flat.sort(key=lambda r: (r["from"], r["dep"], r["to"]))

    with OUT_CSV.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv_writer(f)
        for rec in flat:
            w.writerow([rec["no"], rec["from"], rec["to"],
                        rec["dep"], rec["arr"], rec["lishi"]])
    print(f"CSV 已写入 {OUT_CSV}（{len(flat)} 条去重后车次）", flush=True)

    # 独立接口核对: 抽 5 条用车次经停查询（czxx/queryByTrainNo）回验
    # 只抽 10 站白名单内的车次（接口未按终点过滤，flat 里有其他区间）
    print("抽查核对（车次经停查询接口）:", flush=True)
    import random
    random.seed(7)
    samples = random.sample(flat, min(5, len(flat))) if False else []
    candidates = [r for r in flat if r["from"] in tele and r["to"] in tele]
    samples = random.sample(candidates, min(5, len(candidates)))
    ok = 0
    for rec in samples:
        try:
            r = client.get(
                "https://kyfw.12306.cn/otn/czxx/queryByTrainNo",
                params={"train_no": rec["train_no"],
                        "from_station_telecode": tele[rec["from"]],
                        "to_station_telecode": tele[rec["to"]],
                        "depart_date": date_s}, timeout=20)
            stops = r.json().get("data", {}).get("data", [])
            hit = [s for s in stops
                   if s.get("station_name") in (rec["from"], rec["to"])]
            dep_ok = any(s.get("station_name") == rec["from"]
                         and s.get("start_time") == rec["dep"] for s in hit)
            arr_ok = any(s.get("station_name") == rec["to"]
                         and s.get("arrive_time") == rec["arr"] for s in hit)
            if dep_ok and arr_ok:
                ok += 1
                print(f"  PASS {rec['no']} {rec['from']}→{rec['to']} "
                      f"{rec['dep']}-{rec['arr']}", flush=True)
            else:
                print(f"  FAIL {rec['no']} {rec['from']}→{rec['to']} "
                      f"{rec['dep']}-{rec['arr']} | 经停点: "
                      f"{[(s.get('station_name'), s.get('start_time'), s.get('arrive_time')) for s in hit]}", flush=True)
        except Exception as e:
            print(f"  SKIP {rec['no']}: {type(e).__name__}", flush=True)
    print(f"核对结果: {ok}/{len(samples)} PASS", flush=True)
    if samples and ok < len(samples):
        print("注意: 存在核对不一致项，请人工复核后再入库", flush=True)
        sys.exit(1)

    subprocess.run([sys.executable, str(ROOT / "scripts" / "build_hsr_from_csv.py"),
                    str(OUT_CSV)], check=True)


def csv_writer(fobj):
    import csv
    w = csv.writer(fobj)
    w.writerow(["车次", "出发站", "到达站", "发时", "到时", "历时"])
    return w


if __name__ == "__main__":
    main()
