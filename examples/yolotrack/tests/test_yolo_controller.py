"""YoloSearchTrackController 单测。

测试覆盖：
  - yolo.enabled=false 时行为等同于父类
  - yolo fresh + 有 target_position → yolo 路径
  - yolo stale / 无 → fallback 路径
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

import pytest

# 需要搜索父类 search_track
import sys
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))                    # example/
sys.path.insert(0, str(HERE.parent.parent / "uav_search_track_car"))

from search_track.state import (
    Attitude, Detection, GeoPosition, GimbalState, SimState,
    TargetState, UavState,
)


# ------------------------------------------------------------------
# 工具
# ------------------------------------------------------------------

def make_state(
    *,
    detected: bool = True,
    target_lat: float = 27.002,
    target_lon: float = 125.002,
    uav_lat: float = 27.0,
    uav_lon: float = 125.0,
    uav_yaw: float = 0.0,
    gimbal_pan: float = 0.0,
    gimbal_tilt: float = -30.0,
    sim_time: float = 1.0,
) -> SimState:
    return SimState(
        sim_time=sim_time, timestamp=sim_time, status="running",
        uav=UavState(
            position=GeoPosition(uav_lat, uav_lon, 300.0),
            attitude=Attitude(yaw=uav_yaw, pitch=0.0, roll=0.0),
            velocity=20.0, heading=uav_yaw,
        ),
        gimbal=GimbalState(gimbal_pan, gimbal_tilt, False, 60.0),
        detection=Detection(
            detected=detected, confidence=0.9 if detected else 0.0,
            target_position=GeoPosition(target_lat, target_lon, 0.0) if detected else None,
            azimuth_error_deg=None,
        ),
        target_truth=TargetState(GeoPosition(target_lat, target_lon, 0.0), 8.0, 0.0),
    )


def force_track_mode(controller, target_position: Optional[GeoPosition] = None):
    """强制 controller 进入 TRACK 模式，绕过 SEARCH 状态机。

    不调 configure（避免覆盖 _yolo_enabled），而是直接设 _configured=True
    + 必要参数 + 显式建 tracker。
    """
    # 父类 FSM 需要的参数
    controller._control_rate_hz = 10
    controller._search_radius = 500.0
    controller._search_altitude_agl = 300.0
    controller._sweep_period = 4.0
    controller._loiter_radius = 200.0
    controller._configured = True

    controller._mode = "TRACK"
    # 显式建 tracker（父类只在 SEARCH→TRACK 切换时建，测试里直接强制 mode）
    from search_track.tracking_strategy import LoiterTracker, TrackerParams
    controller._tracker = LoiterTracker(
        params=TrackerParams(loiter_radius=200.0),
    )
    controller._tracker.reset(27.0, 125.0)


# ------------------------------------------------------------------
# 测试
# ------------------------------------------------------------------

class TestNoYoloMode:
    """yolo.enabled=false → 完全等价于原 FsmSearchTrackController。"""

    def test_controller_runs_with_yolo_disabled(self):
        from yolotrack.yolo_controller import YoloSearchTrackController
        c = YoloSearchTrackController()
        c.configure({
            "control_rate_hz": 10,
            "search_radius": 500,
            "search_altitude_agl": 300,
            "sweep_period": 4,
            "loiter_radius": 200,
            "yolo": {"enabled": False},
        })
        c.reset()
        # SEARCH 模式应该正常工作
        state = make_state(detected=False, sim_time=0.5)
        cmds = c.decide(state.without_truth(), 0.1)
        assert len(cmds) > 0
        # yolo 计数器为 0
        assert c.yolo_hits == 0
        assert c.yolo_fallbacks == 0
        # 不会启动 worker
        assert c._vision is None


class TestYoloTrackPath:
    """yolo fresh + 目标在视野内 → yolo 路径。"""

    def test_yolo_fresh_drives_gimbal(self):
        from yolotrack.yolo_controller import YoloSearchTrackController
        from yolotrack.yolo_vision import YoloDetection

        c = YoloSearchTrackController()
        c._yolo_enabled = True
        # 注入 fake vision
        det = YoloDetection(
            bbox_xyxy=(482.0, 364.0, 542.0, 404.0),  # 中心 (512, 384)
            confidence=0.92,
            class_id=0, class_name="TargetVehicle",
            image_size=(1024, 768),
            pan_delta=0.0, tilt_delta=0.0,
            sim_time=1.0, wall_time_ms=int(__import__("time").time() * 1000),
        )
        # 用 mock get_latest
        class FakeVision:
            def get_latest(self, max_age_ms=200):
                return det
        c._vision = FakeVision()
        c._frame_max_age_ms = 200
        # 强制 TRACK
        force_track_mode(c)

        state = make_state(detected=True, sim_time=1.0,
                          uav_yaw=0.0, gimbal_pan=5.0, gimbal_tilt=-30.0)
        cmds = c.decide(state.without_truth(), 0.1)

        # 找到云台 set_orientation 命令
        set_orient = [cmd for cmd in cmds
                      if cmd.cmd == "component.gimbal_tracking.set_orientation"]
        assert len(set_orient) == 1
        # yolo 计数 +1
        assert c.yolo_hits == 1
        assert c.yolo_fallbacks == 0

    def test_yolo_stale_falls_back(self):
        """yolo 超过 max_age → fallback 到 LoiterTracker。"""
        from yolotrack.yolo_controller import YoloSearchTrackController
        from yolotrack.yolo_vision import YoloDetection
        import time as _time

        c = YoloSearchTrackController()
        c._yolo_enabled = True

        # 注入一个 wall_time 极旧的 det
        stale_det = YoloDetection(
            bbox_xyxy=(482, 364, 542, 404), confidence=0.9,
            class_id=0, class_name="TargetVehicle",
            image_size=(1024, 768),
            pan_delta=0.0, tilt_delta=0.0,
            sim_time=1.0, wall_time_ms=0,  # epoch → 必然超 max_age
        )

        class FakeVision:
            def get_latest(self, max_age_ms=200):
                # 模拟 YoloVisionWorker 的 age 检查
                age_ms = int((_time.time() * 1000) - stale_det.wall_time_ms)
                if age_ms > max_age_ms:
                    return None
                return stale_det

        c._vision = FakeVision()
        c._frame_max_age_ms = 200
        force_track_mode(c)

        state = make_state(detected=True, sim_time=1.0,
                          uav_yaw=0.0, gimbal_pan=5.0, gimbal_tilt=-30.0)
        cmds = c.decide(state.without_truth(), 0.1)
        # get_latest 返回 None → 走 fallback
        assert c.yolo_fallbacks == 1
        assert c.yolo_hits == 0

    def test_yolo_target_out_of_view_falls_back(self):
        """yolo 目标在图像边缘外（>1.5x 半宽）→ fallback。"""
        from yolotrack.yolo_controller import YoloSearchTrackController
        from yolotrack.yolo_vision import YoloDetection

        c = YoloSearchTrackController()
        c._yolo_enabled = True

        # bbox 中心在 x=2000，远超出图像 1024 边界（dx_norm ~ 2.9）
        out_of_view = YoloDetection(
            bbox_xyxy=(1980, 364, 2020, 404), confidence=0.9,
            class_id=0, class_name="TargetVehicle",
            image_size=(1024, 768),
            pan_delta=180.0, tilt_delta=0.0,
            sim_time=1.0, wall_time_ms=int(__import__("time").time() * 1000),
        )
        class FakeVision:
            def get_latest(self, max_age_ms=200):
                return out_of_view
        c._vision = FakeVision()
        c._frame_max_age_ms = 200
        force_track_mode(c)

        state = make_state(detected=True, sim_time=1.0)
        c.decide(state.without_truth(), 0.1)
        # dx_norm 远大于 1.5 → 走 fallback
        assert c.yolo_fallbacks == 1
        assert c.yolo_hits == 0


class TestYoloGimbalMath:
    """测试 yolo 路径下云台角度计算的正确性。

    关键不变量：连续帧累积调用 _yolo_track_commands() 时，pan/tilt
    不应"漂移"（即累加 yolo delta 导致失控）。每帧的 target_pan 应
    在父类算的 LOS pan 附近 + yolo delta 修正，而不是无限累加。
    """

    def _make_controller(self):
        from yolotrack.yolo_controller import YoloSearchTrackController
        c = YoloSearchTrackController()
        c._yolo_enabled = True
        force_track_mode(c)
        # 显式给一个固定 LOS 起点（不让父类 LoiterTracker 重算）
        c._last_pan = 0.0
        c._last_tilt = -30.0
        return c

    def test_yolo_offset_corrects_toward_image_center(self):
        """目标在图像右侧 → 云台应往右转（target_pan > LOS_pan）。"""
        from yolotrack.yolo_vision import YoloDetection
        import time
        c = self._make_controller()

        # yolo 报告：目标在图像中心右侧 5°
        # (bbox 中心在 W+W/2/2 = W*0.75，即 dx_norm=0.5)
        # pan_delta = 0.5 * hfov/2 = 0.5 * 30 = 15°
        det = YoloDetection(
            bbox_xyxy=(748, 364, 788, 404),  # 中心 (768, 384)
            confidence=0.9, class_id=0, class_name="TargetVehicle",
            image_size=(1024, 768),
            pan_delta=15.0, tilt_delta=0.0,
            sim_time=1.0, wall_time_ms=int(time.time() * 1000),
        )
        class FV:
            def get_latest(self, max_age_ms=200): return det
        c._vision = FV()
        c._frame_max_age_ms = 200

        # 造一个 state 让父类 LoiterTracker 算出 los_pan（可能是某个值）
        state = make_state(detected=True, sim_time=1.0,
                          uav_yaw=0.0, gimbal_pan=10.0, gimbal_tilt=-30.0)
        cmds = c._track_commands(state.without_truth(), 0.1)
        set_orient = [cmd for cmd in cmds
                      if cmd.cmd == "component.gimbal_tracking.set_orientation"]
        assert len(set_orient) == 1
        target_pan = set_orient[0].params["pan"]
        # 修复后：target_pan = los_pan - yolo.pan_delta
        # 关键不变量：连续多次调用 target_pan 应一致（或逼近一致），不应漂移
        first = target_pan
        for _ in range(5):
            cmds = c._track_commands(state.without_truth(), 0.1)
            set_orient = [cmd for cmd in cmds
                          if cmd.cmd == "component.gimbal_tracking.set_orientation"]
            target_pan = set_orient[0].params["pan"]
        # 5 次后不应变化过大（旧的 bug 会让 target_pan 累加偏移）
        assert abs(target_pan - first) < 5.0, \
            f"target_pan 漂移过大: first={first}, after_5={target_pan}"

    def test_yolo_offset_zero_keeps_target_stable(self):
        """bbox 在图像中心 → target_pan ≈ LOS_pan（不累加）。"""
        from yolotrack.yolo_vision import YoloDetection
        import time
        c = self._make_controller()

        det = YoloDetection(
            bbox_xyxy=(482, 364, 542, 404),  # 中心 (512, 384) = 图像中心
            confidence=0.9, class_id=0, class_name="TargetVehicle",
            image_size=(1024, 768),
            pan_delta=0.0, tilt_delta=0.0,
            sim_time=1.0, wall_time_ms=int(time.time() * 1000),
        )
        class FV:
            def get_latest(self, max_age_ms=200): return det
        c._vision = FV()
        c._frame_max_age_ms = 200

        state = make_state(detected=True, sim_time=1.0,
                          uav_yaw=0.0, gimbal_pan=0.0, gimbal_tilt=-30.0)
        # 连续 10 帧
        pans = []
        for _ in range(10):
            cmds = c._track_commands(state.without_truth(), 0.1)
            set_orient = [cmd for cmd in cmds
                          if cmd.cmd == "component.gimbal_tracking.set_orientation"]
            pans.append(set_orient[0].params["pan"])
        # 10 帧后 pan 变化应很小（LOS 计算稳定，yolo delta=0）
        assert max(pans) - min(pans) < 1.0, \
            f"pan 抖动过大: {pans}"


class TestControllerLifecycle:
    """controller 生命周期相关测试。"""

    def test_shutdown_stops_vision(self):
        from yolotrack.yolo_controller import YoloSearchTrackController
        c = YoloSearchTrackController()
        # fake vision（不需要起线程）
        class FakeVision:
            stopped = False
            def stop(self): self.stopped = True
            def join(self, timeout=None): pass
        fake = FakeVision()
        c._vision = fake
        c.shutdown()
        assert fake.stopped
        assert c._vision is None

    def test_shutdown_safe_when_no_vision(self):
        from yolotrack.yolo_controller import YoloSearchTrackController
        c = YoloSearchTrackController()
        c.shutdown()  # 不应崩
        assert c._vision is None
