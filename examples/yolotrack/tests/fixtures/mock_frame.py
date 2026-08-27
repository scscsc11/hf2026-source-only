"""生成合成测试帧：黑底 + 白色矩形（模拟"目标车"）。"""
from __future__ import annotations

import numpy as np


def make_synthetic_frame(
    width: int = 1024,
    height: int = 768,
    bbox_center: tuple[int, int] | None = None,
    bbox_size: tuple[int, int] = (40, 40),
    bg_color: tuple[int, int, int] = (0, 0, 0),
    fg_color: tuple[int, int, int] = (255, 255, 255),
) -> tuple[bytes, np.ndarray]:
    """返回 (jpeg_bytes, bgr_array)。

    bbox_center：目标中心点像素坐标。默认图像中心。
    """
    img = np.full((height, width, 3), bg_color, dtype=np.uint8)
    if bbox_center is not None:
        cx, cy = bbox_center
        bw, bh = bbox_size
        x1 = max(0, cx - bw // 2)
        y1 = max(0, cy - bh // 2)
        x2 = min(width, cx + bw // 2)
        y2 = min(height, cy + bh // 2)
        img[y1:y2, x1:x2] = fg_color
    # JPEG 编码
    import cv2
    ok, buf = cv2.imencode(".jpg", img)
    if not ok:
        raise RuntimeError("cv2 JPEG encode failed")
    return bytes(buf), img
