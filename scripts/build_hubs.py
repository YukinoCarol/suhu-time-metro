# -*- coding: utf-8 -*-
"""生成 data/hubs.geojson 与 hubs.csv（供人工核对坐标）。"""
import csv
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from hubs import HUBS

ROOT = Path(__file__).resolve().parent.parent


def build():
    features = []
    for hid, name, city, kind, lng, lat, hsr in HUBS:
        features.append(
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [lng, lat]},
                "properties": {
                    "id": hid,
                    "name": name,
                    "city": city,
                    "kind": kind,
                    "hsr": hsr,
                },
            }
        )
    gj = {"type": "FeatureCollection", "features": features}
    out = ROOT / "data" / "hubs.geojson"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(gj, ensure_ascii=False, indent=1), encoding="utf-8")

    with (ROOT / "data" / "hubs.csv").open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["id", "name", "city", "kind", "lng", "lat", "hsr"])
        for row in HUBS:
            w.writerow(row[:-1])
    print(f"hubs: {len(features)} -> {out}")


if __name__ == "__main__":
    build()
