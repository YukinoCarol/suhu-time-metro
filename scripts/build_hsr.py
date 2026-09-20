# -*- coding: utf-8 -*-
"""生成 data/hsr.json：高铁站 + 合成车次表（演示用）。

车次号与时刻为按公开时刻表规律合成的演示数据（source=synthetic），
非 12306 实时数据；接入后可整体替换。
"""
import json
import random
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from hubs import HUBS, HSR_LINES

ROOT = Path(__file__).resolve().parent.parent
random.seed(42)


def parse_hm(s):
    h, m = s.split(":")
    return int(h) * 60 + int(m)


def fmt_hm(minutes):
    minutes %= 24 * 60
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def gen_trains():
    trains = []
    for a, b, tmin, tmax, headway, first, last in HSR_LINES:
        t = parse_hm(first)
        end = parse_hm(last)
        while t <= end:
            dur = round((tmin + tmax) / 2 + random.uniform(-3, 3))
            dur = max(tmin, min(tmax, dur))
            no = f"G{7000 + random.randint(100, 899)}"
            trains.append(
                {"no": no, "from": a, "to": b, "dep": fmt_hm(t),
                 "arr": fmt_hm(t + dur), "minutes": dur}
            )
            # 反方向：到达后折返 8~14 分钟发车，保证 OD 双向可达
            dur_r = max(tmin, min(tmax, round((tmin + tmax) / 2 + random.uniform(-3, 3))))
            dep_r = t + dur + random.randint(8, 14)
            if dep_r < 24 * 60 - 1:
                no_r = f"G{7000 + random.randint(100, 899)}"
                trains.append(
                    {"no": no_r, "from": b, "to": a, "dep": fmt_hm(dep_r),
                     "arr": fmt_hm(dep_r + dur_r), "minutes": dur_r}
                )
            t += headway + random.randint(-3, 3)
    return trains


def main():
    coords = {h[0]: [h[4], h[5]] for h in HUBS}
    hsr_hubs = [h[0] for h in HUBS if h[6]]
    data = {
        "meta": {
            "source": "synthetic",
            "updated_at": date.today().isoformat(),
            "note": "车次为演示合成数据，非12306实时；门到门候车缓冲默认20分钟。",
        },
        "stations": hsr_hubs,
        "coords": coords,
        "trains": gen_trains(),
    }
    out = ROOT / "data" / "hsr.json"
    out.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"hsr: {len(data['trains'])} trains -> {out}")


if __name__ == "__main__":
    main()
