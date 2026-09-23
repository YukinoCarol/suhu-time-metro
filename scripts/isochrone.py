# -*- coding: utf-8 -*-
"""等时圈 v2：细格网 IDW 场 + Marching Squares 矢量化（无第三方绘图依赖，仅 numpy）。

背景（2026-09-23 方法学升级）：
- v1 直接输出逐格 Polygon（16965 个、每格约 1.9km），前端呈现阶梯色块，
  等时线是"距离圆"观感，且 90′ 以上不填充导致覆盖缺失。
- v2 把同一套枢纽时间场先算到细格网（默认 0.005°，约 500m），
  再用 Marching Squares 提取等值线、串成闭合环，输出**矢量多边形带**：
  阶梯消失、边界连续；新增 120/150/180 三档，180′ 以上仍以最浅色覆盖。

诚实声明：本方法仍是"枢纽时间 + 距离插值"的场，不是沿路网的最短路面；
边界之所以不再是圆，是因为 32 个枢纽的时间确实不同（IDW 场本身非径向对称）。
真正的路网等时圈需要逐点路径规划采样，不在本模块范围内。
"""

from __future__ import annotations

import math
import statistics

import numpy as np

from estimate_model import haversine_km

# 8 档时间阈值（分钟）+ 1 档溢出（180 以上仍覆盖，用最浅色）
THRESHOLDS = (15, 30, 45, 60, 90, 120, 150, 180)
EARTH_R = 6371.0088


def haversine_grid(lon0: float, lat0: float, lons, lats) -> np.ndarray:
    """枢纽 (lon0,lat0) 到整幅格网的距离（km），返回 shape=(len(lats), len(lons))。"""
    lo = np.radians(lons)[None, :]
    la = np.radians(lats)[:, None]
    dlon = lo - math.radians(lon0)
    dlat = la - math.radians(lat0)
    a = (np.sin(dlat / 2) ** 2
         + np.cos(la) * math.cos(math.radians(lat0)) * np.sin(dlon / 2) ** 2)
    return 2 * EARTH_R * np.arcsin(np.sqrt(np.clip(a, 0, 1)))


def idw_field(hub_minutes: dict, hub_coord: dict, lons, lats,
              power: float = 2.0, eps: float = 0.5) -> np.ndarray:
    """反距离加权：把枢纽分钟数插成连续场（分钟）。

    ⚠️ 局限：IDW 结果被限制在枢纽时间值域内（苏沪驾车最大约 115′），
    因此永远无法表达 120′ 以上，也无界外增长——高档位等时圈需用 additive_field。
    """
    num = np.zeros((len(lats), len(lons)), dtype=np.float64)
    den = np.zeros_like(num)
    for hid, minutes in hub_minutes.items():
        if minutes is None:
            continue
        c = hub_coord[hid]
        d = haversine_grid(c[0], c[1], lons, lats) + eps
        w = 1.0 / np.power(d, power)
        num += w * float(minutes)
        den += w
    return num / np.maximum(den, 1e-12)


def calibration_factor(matrix: dict, est_fn, hub_ids, hub_coord,
                       default: float = 1.0) -> float:
    """用实测矩阵反推估算模型的整体比例系数（中位数），用于野外段外推校准。

    实测值 / 估算值 的中位数：>1 表示估算偏快（需放大），<1 表示偏慢。
    """
    ratios = []
    for a in hub_ids:
        for b in hub_ids:
            if a == b:
                continue
            ent = matrix.get(a, {}).get(b)
            if not ent or ent.get("minutes") in (None, 0):
                continue
            ca, cb = hub_coord[a], hub_coord[b]
            e = est_fn(haversine_km(ca[0], ca[1], cb[0], cb[1]))
            if e > 0:
                ratios.append(ent["minutes"] / e)
    if len(ratios) < 10:
        return default
    return float(statistics.median(ratios))


def est_lookup(est_fn, dmax: float = 320.0, step: float = 5.0):
    """把标量估算函数打成查表（直线距离 km → 分钟），避免逐点 Python 调用。

    步长取 5km：估算函数本身是取整的分段常数，过密的表会让场出现平台，
    在多个锚点取 min 的接缝处产生碎小等值环；粗表使其近似分段线性、更平滑。
    """
    xs = np.arange(0.0, dmax + step, step)
    ys = np.array([est_fn(float(x)) for x in xs], dtype=np.float64)
    return xs, ys


def additive_field(hub_minutes: dict, hub_coord: dict, lons, lats, est_fn,
                   calib: float = 1.0, lut=None) -> np.ndarray:
    """加性锚点模型：t(x) = min_h [ t_h + calib × (est(d) - est(0)) ]。

    相比 IDW 的优点：
    - 枢纽处严格等于实测/计算值（est(0) 被扣除）；
    - 随距离无界增长 → 120/150/180 档可正常成圈；
    - 等时线在"最近（时间上）枢纽"切换处自然弯折，比纯几何插值更贴近路网结构。
    仍是估计场：野外段的位移用分段车速估算并按实测比例校准，非逐点路径规划。
    """
    xs, ys = lut if lut is not None else est_lookup(est_fn)
    base = ys[0]
    best = None
    for hid, minutes in hub_minutes.items():
        if minutes is None:
            continue
        c = hub_coord[hid]
        d = haversine_grid(c[0], c[1], lons, lats)
        t = float(minutes) + calib * (np.interp(d, xs, ys) - base)
        best = t if best is None else np.minimum(best, t)
    if best is None:
        return np.zeros((len(lats), len(lons)), dtype=np.float64)
    return best


# ---------- Marching Squares（向量化） ----------
# 顶点编号：0=左上 1=右上 2=右下 3=左下；边编号：0=上 1=右 2=下 3=左
_SEG_TABLE = {
    0: [], 15: [],
    1: [(3, 0)], 2: [(0, 1)], 3: [(3, 1)], 4: [(1, 2)],
    6: [(0, 2)], 7: [(3, 2)], 8: [(2, 3)], 9: [(2, 0)],
    11: [(2, 1)], 12: [(1, 3)], 13: [(1, 0)], 14: [(0, 3)],
}
# 鞍点（5/10）按中心值二选一
_SEG_AMBIG = {
    5: ([(3, 2), (0, 1)], [(3, 0), (1, 2)]),
    10: ([(3, 0), (1, 2)], [(3, 2), (0, 1)]),
}


def _edge_pts(A, B, C, D, YS, XS, level, idx):
    """在**已筛选出的格子子集**上求各边插值点（1D 数组）。

    A/B/C/D 为子集四角值，YS/XS 为子集行列索引（均为 1D）。
    顶点编号：0=左上 1=右上 2=右下 3=左下；边编号：0=上 1=右 2=下 3=左。
    """
    def lerp(p, q):
        return (level - p) / np.where(np.abs(q - p) < 1e-12, 1e-12, q - p)

    out = {}
    for e in idx:
        if e == 0:      # 上边 a—b，沿 x
            out[e] = (XS + lerp(A, B), YS + 0.0)
        elif e == 1:    # 右边 b—c，沿 y
            out[e] = (XS + 1.0, YS + lerp(B, C))
        elif e == 2:    # 下边 d—c，沿 x
            out[e] = (XS + lerp(D, C), YS + 1.0)
        else:           # 左边 a—d，沿 y
            out[e] = (XS + 0.0, YS + lerp(A, D))
    return out


def contour_segments(z: np.ndarray, level: float):
    """提取 z == level 的等值线段，返回 [(x1, y1, x2, y2), ...]（索引坐标）。

    仅处理四角跨越该阈值的格子（预筛），大格网下比全幅扫描快一个量级。
    """
    a, b, c, d = z[:-1, :-1], z[:-1, 1:], z[1:, 1:], z[1:, :-1]
    cmin = np.minimum(np.minimum(a, b), np.minimum(c, d))
    cmax = np.maximum(np.maximum(a, b), np.maximum(c, d))
    ys, xs = np.where((cmin < level) & (cmax >= level))
    if len(ys) == 0:
        return np.empty((0, 4))
    A, B, C, D = a[ys, xs], b[ys, xs], c[ys, xs], d[ys, xs]
    inside = ((A >= level).astype(np.uint8)
              + (B >= level).astype(np.uint8) * 2
              + (C >= level).astype(np.uint8) * 4
              + (D >= level).astype(np.uint8) * 8)
    segs = []

    def emit(edges, mask):
        if not mask.any():
            return
        pts = _edge_pts(A[mask], B[mask], C[mask], D[mask],
                        ys[mask].astype(float), xs[mask].astype(float),
                        level, edges)
        for e1, e2 in pairs:
            segs.append(np.column_stack([pts[e1][0], pts[e1][1],
                                         pts[e2][0], pts[e2][1]]))

    for case, pairs in _SEG_TABLE.items():
        if not pairs:
            continue
        mask = inside == case
        if not mask.any():
            continue
        emit(sorted({e for p in pairs for e in p}), mask)
    # 鞍点 5/10：用四角均值近似中心值判定
    center = (A + B + C + D) / 4.0
    for case, (hi, lo) in _SEG_AMBIG.items():
        mask = inside == case
        if not mask.any():
            continue
        for pairs, sel in ((hi, mask & (center >= level)),
                           (lo, mask & (center < level))):
            if sel.any():
                emit([0, 1, 2, 3], sel)
    return np.vstack(segs) if segs else np.empty((0, 4))


def chain_rings(segs: np.ndarray, tol: float = 1e-9):
    """把散乱线段串成闭合环（按端点邻接贪心行走），返回 [[(x,y), ...], ...]。

    相邻格子在共享边上的插值公式相同，端点逐位相等，故容差可取得极小。
    """
    if len(segs) == 0:
        return []
    # 端点量化成 key，建立邻接
    adj: dict = {}
    used = np.zeros(len(segs), dtype=bool)

    def key(x, y):
        return (round(float(x) / tol), round(float(y) / tol))

    ends = []
    for i, (x1, y1, x2, y2) in enumerate(segs):
        k1, k2 = key(x1, y1), key(x2, y2)
        ends.append((k1, k2))
        adj.setdefault(k1, []).append(i)
        adj.setdefault(k2, []).append(i)
    rings = []
    for start in range(len(segs)):
        if used[start]:
            continue
        x1, y1, x2, y2 = segs[start]
        used[start] = True
        ring = [(float(x1), float(y1)), (float(x2), float(y2))]
        cur = key(x2, y2)
        start_key = key(x1, y1)
        closed = False
        while True:
            nxt = None
            for cand in adj.get(cur, ()):
                if not used[cand]:
                    nxt = cand
                    break
            if nxt is None:
                break
            x1, y1, x2, y2 = segs[nxt]
            used[nxt] = True
            k1, k2 = ends[nxt]
            if k1 == cur:
                ring.append((float(x2), float(y2)))
                cur = k2
            else:
                ring.append((float(x1), float(y1)))
                cur = k1
            if cur == start_key:
                closed = True
                break
        # 只保留闭合环；被图幅截断的开链丢弃（该区域由兜底底色覆盖）
        if closed and len(ring) >= 4:
            ring.append(ring[0])
            rings.append(ring)
    return rings


def _ring_area(ring):
    """鞋带公式算环的面积（度² 量级，仅用于过滤噪声小环）。"""
    s = 0.0
    for (x1, y1), (x2, y2) in zip(ring, ring[1:]):
        s += x1 * y2 - x2 * y1
    return abs(s) / 2.0


def _rdp(chain, tol: float):
    """Douglas–Peucker 简化（迭代实现），用于开放折线。"""
    if len(chain) < 3 or tol <= 0:
        return list(chain)
    keep = [False] * len(chain)
    keep[0] = keep[-1] = True
    stack = [(0, len(chain) - 1)]
    while stack:
        i, j = stack.pop()
        if j <= i + 1:
            continue
        x1, y1 = chain[i]
        x2, y2 = chain[j]
        dx, dy = x2 - x1, y2 - y1
        norm = math.hypot(dx, dy) or 1e-12
        dmax, kmax = 0.0, -1
        for k in range(i + 1, j):
            x, y = chain[k]
            dist = abs(dy * (x - x1) - dx * (y - y1)) / norm
            if dist > dmax:
                dmax, kmax = dist, k
        if dmax > tol:
            keep[kmax] = True
            stack.append((i, kmax))
            stack.append((kmax, j))
    return [p for p, k in zip(chain, keep) if k]


def simplify(ring, tol: float):
    """闭合环简化：闭合环首尾同点，直接 RDP 会退化成两点，
    故先按"距起点最远的顶点"切成两段分别简化再拼接。"""
    if len(ring) < 5 or tol <= 0:
        return list(ring)
    x0, y0 = ring[0]
    idx = max(range(1, len(ring) - 1),
              key=lambda k: (ring[k][0] - x0) ** 2 + (ring[k][1] - y0) ** 2)
    head = _rdp(ring[:idx + 1], tol)
    tail = _rdp(ring[idx:], tol)
    return head[:-1] + tail


def band_features(field: np.ndarray, lons, lats, thresholds=THRESHOLDS,
                  min_cells: float = 6.0, simp_tol: float = 0.35):
    """场 → 各档等值线闭合环（经纬度），返回 {band_index: [ring, ...]}。

    band i = {t <= thresholds[i]}。min_cells 为环面积下限（格数），
    用于剔除 min() 锥面接缝处的碎小噪声环；simp_tol 为简化容差（格）。
    """
    lo0 = float(lons[0])
    dlo = float(lons[1] - lons[0])
    la0 = float(lats[0])
    dla = float(lats[1] - lats[0])
    cell_area = abs(dlo * dla)
    out = {}
    for i, th in enumerate(thresholds):
        segs = contour_segments(field, float(th))
        rings = chain_rings(segs)
        keep_rings = []
        for r in rings:
            if len(r) < 4 or _ring_area_xy(r) < min_cells:
                continue
            keep_rings.append(simplify(r, simp_tol))
        # 索引坐标 → 经纬度；行索引 0 对应最小纬度，故 y 直接正向映射
        geo = [[[lo0 + x * dlo, la0 + y * dla] for (x, y) in r] for r in keep_rings]
        geo = [r for r in geo if len(r) >= 4
               and _ring_area(r) >= min_cells * cell_area]
        out[i] = geo
    return out


def _ring_area_xy(ring):
    """索引坐标下按格数计的环面积（过滤用）。"""
    s = 0.0
    for (x1, y1), (x2, y2) in zip(ring, ring[1:]):
        s += x1 * y2 - x2 * y1
    return abs(s) / 2.0


def build_isochrone(hub_minutes: dict, hub_coord: dict, *, step: float = 0.005,
                    bbox=(118.55, 29.45, 123.65, 32.95),
                    thresholds=THRESHOLDS, est_fn=None, calib: float = 1.0,
                    method: str = "additive"):
    """完整流程：枢纽时间 → 细格网场 → 矢量多边形带。

    est_fn 提供时用加性锚点模型（可表达 >115′ 的高档位）；
    未提供时回退 IDW（值域受限）。
    返回 (FeatureCollection, meta)。features 按 band 由大到小（外→内）排列。
    """
    lons = np.arange(bbox[0], bbox[2] + step / 2, step)
    lats = np.arange(bbox[1], bbox[3] + step / 2, step * 0.85)
    if est_fn is not None and method == "additive":
        field = additive_field(hub_minutes, hub_coord, lons, lats, est_fn, calib)
        used = "additive"
    else:
        field = idw_field(hub_minutes, hub_coord, lons, lats)
        used = "idw"
    bands = band_features(field, lons, lats, thresholds)
    feats = []
    # 兜底层（band = len(thresholds)）：180′ 以上仍覆盖，最浅色铺满图幅
    feats.append({
        "type": "Feature",
        "geometry": {"type": "Polygon", "coordinates": [[
            [bbox[0], bbox[1]], [bbox[2], bbox[1]],
            [bbox[2], bbox[3]], [bbox[0], bbox[3]], [bbox[0], bbox[1]],
        ]]},
        "properties": {"band": len(thresholds), "up_to": None,
                       "level_hi": thresholds[-1]},
    })
    for i in sorted(bands, reverse=True):     # 外圈先画，内圈覆盖其上
        for ring in bands[i]:
            feats.append({
                "type": "Feature",
                "geometry": {"type": "Polygon", "coordinates": [ring]},
                "properties": {"band": i,
                               "up_to": thresholds[i] if i < len(thresholds) else None,
                               "level_hi": thresholds[i - 1] if i else 0},
            })
    meta = {"grid": [len(lons), len(lats)], "step_deg": step,
            "bbox": list(bbox), "thresholds": list(thresholds),
            "rings": len(feats), "method": used, "calib": round(float(calib), 3),
            "field_min": round(float(field.min()), 1),
            "field_max": round(float(field.max()), 1)}
    return {"type": "FeatureCollection", "features": feats}, meta
