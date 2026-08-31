"""几何换算工具 (spec 029 D3)。

meters_to_deg: 米 → 经纬度偏移（AccuracySimulator 噪声用）。
pan_tilt_to_latlon: pan/tilt delta → 目标 lat/lon（YoloDetector 用，Task 4 追加）。
latlon_distance_m: 两经纬度点水平距离（AccuracySimulator 距离衰减用）。
"""
from __future__ import annotations

import math

# 纬度 1 度 ≈ 111320 米；经度 1 度 ≈ 111320 * cos(lat) 米
_M_PER_DEG_LAT = 111320.0


def meters_to_deg(meters: float, lat: float, is_lon: bool) -> float:
    """米换算成经纬度偏移量。"""
    if is_lon:
        cos_lat = max(math.cos(math.radians(lat)), 1e-6)
        return meters / (_M_PER_DEG_LAT * cos_lat)
    return meters / _M_PER_DEG_LAT


def latlon_distance_m(lat1: float, lon1: float,
                      lat2: float, lon2: float) -> float:
    """两经纬度点的水平距离（米）。

    equirectangular 近似，与 meters_to_deg 同口径（纬度 1 度 ≈ 111320 米，
    经度按平均纬度的 cos 缩放）。AccuracySimulator 距离衰减用。
    假设两点位于同一局部区域（不跨日期变更线/极点），全球尺度请勿使用。
    """
    mean_lat = math.radians((lat1 + lat2) / 2.0)
    d_lat_m = (lat2 - lat1) * _M_PER_DEG_LAT
    d_lon_m = (lon2 - lon1) * _M_PER_DEG_LAT * math.cos(mean_lat)
    return math.hypot(d_lat_m, d_lon_m)


def pan_tilt_to_latlon(uav_lat: float, uav_lon: float, uav_alt: float,
                       gimbal_pan: float, gimbal_tilt: float,
                       pan_delta: float, tilt_delta: float
                       ) -> tuple[float, float]:
    """从本机位姿 + 云台角 + pan/tilt delta 反算目标地面 lat/lon。

    简化模型：假设目标在地面（alt=0），用云台总指向角（gimbal + delta）
    估算地面投影点的水平距离，再换算成经纬度偏移。

    Args:
        uav_lat/lon/alt: 本机位置。
        gimbal_pan/tilt: 当前云台角（度）。
        pan_delta/tilt_delta: 识别层算出的修正量（度）。

    Returns:
        (target_lat, target_lon) 目标地面坐标。
    """
    total_tilt = gimbal_tilt + tilt_delta   # 总俯仰角
    total_pan = gimbal_pan + pan_delta      # 总方位角
    # 正向模型：tilt = -90（正下方）→ 水平距离 = 0；tilt = -45 → 距离 = alt；
    # tilt → 0（水平看）→ 距离 → ∞。故 horiz = alt / tan(|tilt|)。
    # clamp abs_tilt 到 [1e-3, 90]：上限走 nadir 短路，下限防 tan(0)=0 除零。
    abs_tilt = max(min(abs(total_tilt), 90.0), 1e-3)
    if 90.0 - abs_tilt <= 0:
        # 正下方（tilt = ±90）
        return uav_lat, uav_lon
    # 地面水平距离（米）：horiz = alt / tan(|tilt|)
    ground_dist = uav_alt / math.tan(math.radians(abs_tilt))
    # 按 pan 方向分解到 lat/lon
    # pan=0 向北（+lat），pan=90 向东（+lon）——与 bbox_to_gimbal 约定对齐
    pan_rad = math.radians(total_pan)
    d_north = ground_dist * math.cos(pan_rad)
    d_east = ground_dist * math.sin(pan_rad)
    d_lat = meters_to_deg(d_north, uav_lat, is_lon=False)
    d_lon = meters_to_deg(d_east, uav_lat, is_lon=True)
    return uav_lat + d_lat, uav_lon + d_lon
