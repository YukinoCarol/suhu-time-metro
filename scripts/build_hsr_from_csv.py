# -*- coding: utf-8 -*-
"""把车次 CSV 转为 data/hsr.json（source=real）。

CSV 表头兼容以下列名（不区分先后）:
  车次 | 出发站/出发/始发/上车站 | 到达站/到达/终到/下车站 | 发时/出发时间/开车 | 到时/到达时间
站名白名单 = hubs.csv 中 hsr=1 的 10 站；兼容「苏州站/上海虹桥站」等带「站」字写法。
跨天班次（如 23:58 发 00:21 到）自动 +1440。仅收 G/D/C（高铁/动车/城际）车次。

用法:
  python scripts/build_hsr_from_csv.py [csv路径]
默认读取 data/hsr_timetable.csv（fetch_12306_timetable.py 的产物）。
"""
import csv
import json
import re
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

NAME_HUB = {
    "苏州": "stn", "苏州北": "szn", "苏州园区": "syq", "苏州新区": "sxq",
    "昆山南": "ksn", "苏州南": "szs", "太仓": "tcs", "常熟": "csr",
    "上海虹桥": "shq", "上海": "sh", "上海南": "shn",
}
TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
TRAIN_RE = re.compile(r"^[GDC]\d+[A-Z]?$")
HSR_IDS = sorted(set(NAME_HUB.values()))


def norm_station(s):
    s = (s or "").strip().replace(" ", "")
    if s.endswith("站"):
        s = s[:-1]
    return NAME_HUB.get(s, "")


def parse_hm(s):
    m = TIME_RE.match((s or "").strip())
    if not m:
        return None
    return int(m.group(1)) * 60 + int(m.group(2))


def main():
    csv_path = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "data" / "hsr_timetable.csv"
    if not csv_path.exists():
        print(f"错误: 找不到 {csv_path}；先运行 fetch_12306_timetable.py 或指定 CSV。")
        sys.exit(1)

    with csv_path.open(encoding="utf-8-sig", newline="") as f:
        rows = list(csv.reader(f))
    header = rows[0]
    idx = {}
    for want, keys in {
        "no": ["车次"],
        "fr": ["出发站", "出发", "始发", "上车站"],
        "to": ["到达站", "到达", "终到", "下车站"],
        "dep": ["发时", "出发时间", "开车", "发车"],
        "arr": ["到时", "到达时间"],
    }.items():
        for i, h in enumerate(header):
            if h.strip() in keys:
                idx[want] = i
                break
    if len(idx) < 5:
        print(f"错误: CSV 表头不完整，缺 {[k for k in ['no','fr','to','dep','arr'] if k not in idx]}，"
              f"实际表头: {header}")
        sys.exit(1)

    stations_seen = set()
    skipped_station, skipped_time, skipped_train = [], [], 0
    dedup = {}
    for row in rows[1:]:
        if len(row) <= max(idx.values()):
            continue
        no = row[idx["no"]].strip()
        fr, to = norm_station(row[idx["fr"]]), norm_station(row[idx["to"]])
        dep, arr = parse_hm(row[idx["dep"]]), parse_hm(row[idx["arr"]])
        if not fr or not to:
            skipped_station.append(f"{row[idx['fr']]}|{row[idx['to']]}")
            continue
        stations_seen |= {fr, to}
        if dep is None or arr is None:
            skipped_time.append(f"{no}: {row[idx['dep']]}-{row[idx['arr']]}")
            continue
        if not TRAIN_RE.match(no):
            skipped_train += 1
            continue
        minutes = arr - dep
        if minutes <= 0:            # 跨天到达
            minutes += 24 * 60
        key = (no, fr, to)
        if key not in dedup or minutes < dedup[key][1]:
            dedup[key] = ({"no": no, "from": fr, "to": to,
                           "dep": f"{dep // 60:02d}:{dep % 60:02d}",
                           "arr": f"{arr // 60:02d}:{arr % 60:02d}",
                           "minutes": minutes}, minutes)

    trains = [v[0] for v in dedup.values()]
    trains.sort(key=lambda t: (t["from"], t["dep"], t["to"]))

    coords = {}
    with (ROOT / "data" / "hubs.csv").open(encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            if r["id"] in HSR_IDS:
                coords[r["id"]] = [float(r["lng"]), float(r["lat"])]

    today = date.today().isoformat()
    skipped_uniq = sorted({s.split("|")[0] for s in skipped_station}
                          | {s.split("|")[1] for s in skipped_station})
    data = {
        "meta": {
            "mode": "hsr",
            "source": "real",
            "updated_at": today,
            "note": "车次时刻为用户提供/12306 实测时刻表（G/D/C，运行图以查询日为准）；"
                    "门到门候车缓冲默认20分钟。",
            "stations_skipped": skipped_uniq,
        },
        "stations": HSR_IDS,
        "coords": coords,
        "trains": trains,
    }
    out = ROOT / "data" / "hsr.json"
    out.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"hsr.json 已写入: {len(trains)} 条真实车次（source=real）")
    print(f"  覆盖站点: {sorted(stations_seen)}")
    if skipped_train:
        print(f"  跳过非G/D/C车次: {skipped_train} 条")
    if skipped_time:
        print(f"  跳过时间异常: {skipped_time[:5]}")
    if skipped_station:
        uniq = sorted(set(skipped_station))[:5]
        print(f"  跳过白名单外站名: {uniq}")
    sample = trains[:5]
    print("抽样前5条:", *[f"{t['no']} {t['from']}→{t['to']} "
                          f"{t['dep']}-{t['arr']} ({t['minutes']}′)" for t in sample], sep="\n  ")


if __name__ == "__main__":
    main()
