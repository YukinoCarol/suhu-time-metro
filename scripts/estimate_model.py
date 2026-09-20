# -*- coding: utf-8 -*-
"""无 Key 时的耗时估算模型（开源打底，待高德校准）。

用「直线距离 × 绕行系数 → 分段平均车速」的方式推算门到门分钟数，
并用若干真实经验锚点做手工校准。产出数据 source="estimate"，
接入高德后由 build_matrix_amap.py 覆盖为 source="amap"。
"""
import math

# 直线距离 -> 实际道路里程 绕行系数
def road_km(d_straight: float) -> float:
    if d_straight <= 5:
        return d_straight * 1.30
    if d_straight <= 20:
        return d_straight * 1.25
    if d_straight <= 40:
        return d_straight * 1.20
    return d_straight * 1.15


def drive_minutes(d_straight: float) -> int:
    """驾车（高速优先），含起停/停车 4 分钟。"""
    d = road_km(d_straight)
    if d <= 8:
        v = 27
    elif d <= 20:
        v = 34
    elif d <= 40:
        v = 46
    elif d <= 70:
        v = 58
    elif d <= 110:
        v = 68
    else:
        v = 74
    return round(d / v * 60 + 4)


def drive_local_minutes(d_straight: float) -> int:
    """驾车（国道/不走高速）：更慢且波动大。"""
    return round(drive_minutes(d_straight) * 1.42 + 6)


def transit_minutes(d_straight: float) -> int:
    """公共交通（公交+地铁，跨市走苏州11号线—上海11号线链路），不含高铁。"""
    d = road_km(d_straight)
    if d <= 8:
        v, transfers = 22, 1
    elif d <= 20:
        v, transfers = 28, 2
    elif d <= 45:
        v, transfers = 34, 3
    else:
        v, transfers = 40, 4
    return round(d / v * 60 + 12 + transfers * 5)


def haversine_km(lng1, lat1, lng2, lat2) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))
