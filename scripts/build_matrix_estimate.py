# -*- coding: utf-8 -*-
"""用估算模型生成三种耗时矩阵（驾车高速 / 驾车国道 / 公交）。

输出 data/matrix/{driving,driving_local,transit}.json，结构:
{
  "meta": {"mode": ..., "source": "estimate", "updated_at": ...,
           "note": "..."},
  "hubs": [hub_id, ...],
  "matrix": { "a": {"b": {"minutes": int, "km": float}, ...}, ... }
}
"""
import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from hubs import HUBS
from estimate_model import (
    drive_minutes,
    drive_local_minutes,
    transit_minutes,
    haversine_km,
)

ROOT = Path(__file__).resolve().parent.parent
MODES = {
    "driving": drive_minutes,
    "driving_local": drive_local_minutes,
    "transit": transit_minutes,
}

NOTES = {
    "driving": "驾车·高速优先。估算=直线×绕行系数/分段均速，待高德校准。",
    "driving_local": "驾车·国道(不走高速)。估算=高速估算×1.42+6min，待高德校准。",
    "transit": "公共交通(公交+地铁，跨市走轨交11号线链路，不含高铁)。待高德校准。",
}


def build():
    ids = [h[0] for h in HUBS]
    coords = {h[0]: (h[4], h[5]) for h in HUBS}
    for mode, fn in MODES.items():
        matrix = {}
        for a in ids:
            matrix[a] = {}
            for b in ids:
                if a == b:
                    continue
                km = round(
                    haversine_km(*coords[a], *coords[b]), 1
                )
                matrix[a][b] = {
                    "minutes": fn(km),
                    "km": km,
                }
        data = {
            "meta": {
                "mode": mode,
                "source": "estimate",
                "updated_at": date.today().isoformat(),
                "note": NOTES[mode],
            },
            "hubs": ids,
            "matrix": matrix,
        }
        out = ROOT / "data" / "matrix" / f"{mode}.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"{mode}: {len(ids)}x{len(ids)} -> {out}")


if __name__ == "__main__":
    build()
