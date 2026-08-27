"""bbox 中心 → 云台 pan/tilt 增量（纯函数）。

本模块是整个 yolotrack 的几何核心，无任何外部依赖，便于单测。
"""
from __future__ import annotations


def bbox_to_pan_tilt_delta(
    bbox_center: tuple[float, float],
    image_size: tuple[int, int],
    hfov_deg: float,
    vfov_deg: float,
) -> tuple[float, float]:
    """bbox 中心相对图像中心的 pan/tilt 增量（度）。

    Args:
        bbox_center: (cx, cy) bbox 中心点像素坐标。
        image_size: (W, H) 图像尺寸（像素）。
        hfov_deg: 相机水平视场角（度），即图像宽度方向覆盖的总角度。
        vfov_deg: 相机垂直视场角（度），即图像高度方向覆盖的总角度。

    Returns:
        (pan_delta, tilt_delta) 单位度。
        - pan_delta  > 0 表示目标在图像右边，需要云台向右转
        - tilt_delta > 0 表示目标在图像下边，需要云台向下转
    """
    cx, cy = bbox_center
    W, H = image_size
    if W <= 0 or H <= 0:
        raise ValueError(f"image_size 必须为正: got {image_size}")
    if hfov_deg < 0 or vfov_deg < 0:
        raise ValueError(
            f"视场角必须为非负: got hfov={hfov_deg}, vfov={vfov_deg}"
        )

    # 归一化到 [-1, 1]：图像中心 = 0
    dx_norm = (cx - W / 2.0) / (W / 2.0)
    dy_norm = (cy - H / 2.0) / (H / 2.0)

    # 偏移对应的角度（视场角是图像全宽/全高覆盖的总角度，半视场 = hfov/2）
    pan_delta = dx_norm * (hfov_deg / 2.0)
    tilt_delta = dy_norm * (vfov_deg / 2.0)
    return pan_delta, tilt_delta


def clip_pan_tilt(
    pan: float,
    tilt: float,
    pan_limit_deg: float = 180.0,
    tilt_limit_deg: float = 90.0,
) -> tuple[float, float]:
    """把绝对 pan/tilt 角度裁剪到云台机械限位内。"""
    pan = max(-pan_limit_deg, min(pan_limit_deg, pan))
    tilt = max(-tilt_limit_deg, min(tilt_limit_deg, tilt))
    return pan, tilt
