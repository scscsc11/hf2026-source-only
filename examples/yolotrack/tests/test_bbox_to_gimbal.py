"""bbox → pan/tilt 转换的纯函数测试。"""
from __future__ import annotations

import math

import pytest

from yolotrack.bbox_to_gimbal import bbox_to_pan_tilt_delta, clip_pan_tilt


class TestBboxToPanTiltDelta:
    """核心几何函数：bbox 中心 → 相对图像中心的 pan/tilt 增量。"""

    def test_image_center_yields_zero_delta(self):
        """图像中心 → pan=0, tilt=0（目标已居中）。"""
        pan, tilt = bbox_to_pan_tilt_delta(
            (512, 384), (1024, 768), 60.0, 45.0,
        )
        assert pan == pytest.approx(0.0, abs=1e-9)
        assert tilt == pytest.approx(0.0, abs=1e-9)

    def test_right_edge_yields_positive_pan(self):
        """目标在右边 → 正 pan delta（云台应向右转）。"""
        pan, tilt = bbox_to_pan_tilt_delta(
            (1024, 384), (1024, 768), 60.0, 45.0,
        )
        # 中心 = 512；右边 = 1024；归一化 = +1.0；delta = +30°
        assert pan == pytest.approx(30.0, abs=1e-9)
        assert tilt == pytest.approx(0.0, abs=1e-9)

    def test_left_edge_yields_negative_pan(self):
        pan, _ = bbox_to_pan_tilt_delta(
            (0, 384), (1024, 768), 60.0, 45.0,
        )
        assert pan == pytest.approx(-30.0, abs=1e-9)

    def test_bottom_edge_yields_positive_tilt(self):
        """目标在底部 → 正 tilt delta（云台应向下转）。"""
        _, tilt = bbox_to_pan_tilt_delta(
            (512, 768), (1024, 768), 60.0, 45.0,
        )
        # 中心=384, 底部=768, 归一化=+1, delta=+22.5°
        assert tilt == pytest.approx(22.5, abs=1e-9)

    def test_top_edge_yields_negative_tilt(self):
        _, tilt = bbox_to_pan_tilt_delta(
            (512, 0), (1024, 768), 60.0, 45.0,
        )
        assert tilt == pytest.approx(-22.5, abs=1e-9)

    def test_quarter_offsets(self):
        """中心 + 1/4 半径 → 1/2 视场角。"""
        pan, tilt = bbox_to_pan_tilt_delta(
            (768, 576), (1024, 768), 60.0, 45.0,
        )
        # dx_norm = (768-512)/512 = 0.5; pan = 0.5 * 30 = 15
        # dy_norm = (576-384)/384 = 0.5; tilt = 0.5 * 22.5 = 11.25
        assert pan == pytest.approx(15.0, abs=1e-9)
        assert tilt == pytest.approx(11.25, abs=1e-9)

    def test_works_with_arbitrary_image_size(self):
        """640x480 + 60x45 fov。中心 320, 240。"""
        pan, tilt = bbox_to_pan_tilt_delta(
            (480, 360), (640, 480), 60.0, 45.0,
        )
        # 中心 320,240; 偏移 160,120; 归一化 0.5, 0.5
        # pan = 0.5*30=15; tilt=0.5*22.5=11.25
        assert pan == pytest.approx(15.0, abs=1e-9)
        assert tilt == pytest.approx(11.25, abs=1e-9)

    def test_zero_fov_yields_zero_delta(self):
        """fov=0 时不报错，所有 delta 为 0。"""
        pan, tilt = bbox_to_pan_tilt_delta(
            (100, 100), (1024, 768), 0.0, 0.0,
        )
        assert pan == 0.0
        assert tilt == 0.0

    def test_negative_image_size_raises(self):
        with pytest.raises(ValueError, match="image_size"):
            bbox_to_pan_tilt_delta((0, 0), (0, 100), 60, 45)
        with pytest.raises(ValueError, match="image_size"):
            bbox_to_pan_tilt_delta((0, 0), (100, 0), 60, 45)

    def test_negative_fov_raises(self):
        with pytest.raises(ValueError, match="视场角"):
            bbox_to_pan_tilt_delta((0, 0), (1024, 768), -1.0, 45.0)
        with pytest.raises(ValueError, match="视场角"):
            bbox_to_pan_tilt_delta((0, 0), (1024, 768), 60.0, -1.0)

    def test_yolo_realistic_offsets(self):
        """YOLO 真实场景：cam10002 数据 bbox 中心偏移约 ±50px。"""
        # 假设 UAV 飞 100m 高，目标在中心偏右 50px, 偏下 20px
        pan, tilt = bbox_to_pan_tilt_delta(
            (562, 404), (1024, 768), 60.0, 45.0,
        )
        # dx_norm = 50/512 ≈ 0.0977; pan ≈ 2.93°
        # dy_norm = 20/384 ≈ 0.0521; tilt ≈ 1.17°
        assert pan == pytest.approx(2.93, abs=0.01)
        assert tilt == pytest.approx(1.17, abs=0.01)


class TestClipPanTilt:
    def test_within_limits_unchanged(self):
        assert clip_pan_tilt(10.0, 20.0, 180, 90) == (10.0, 20.0)

    def test_pan_clipped_to_limit(self):
        assert clip_pan_tilt(200.0, 0.0, 180, 90) == (180.0, 0.0)
        assert clip_pan_tilt(-200.0, 0.0, 180, 90) == (-180.0, 0.0)

    def test_tilt_clipped_to_limit(self):
        assert clip_pan_tilt(0.0, 100.0, 180, 90) == (0.0, 90.0)
        assert clip_pan_tilt(0.0, -100.0, 180, 90) == (0.0, -90.0)

    def test_default_limits(self):
        """默认 pan_limit=180, tilt_limit=90。"""
        assert clip_pan_tilt(50.0, 50.0) == (50.0, 50.0)
        assert clip_pan_tilt(181.0, 91.0) == (180.0, 90.0)
