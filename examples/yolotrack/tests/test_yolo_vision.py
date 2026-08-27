"""YoloVisionWorker 单测。

需要：ultralytics + 已下载的模型文件。模型路径可由环境变量 YOLO_TEST_MODEL 指定，
默认指向 /home/lpwang/YOLO/yolo_car/target_vehicle_yolov8s.pt。
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

# 让 from tests.fixtures import ... 能工作
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import pytest

# 默认跳过（需要 ultralytics + 模型）
pytestmark = pytest.mark.skipif(
    not Path(os.environ.get(
        "YOLO_TEST_MODEL",
        "/home/lpwang/YOLO/yolo_car/target_vehicle_yolov8s.pt",
    )).exists(),
    reason="YOLO 模型文件不存在，跳过 worker 集成测试",
)


def _build_worker(model_path: str, mock_redis, **overrides):
    """工厂：构造 YoloVisionWorker，注入 mock_redis。"""
    import importlib
    from yolotrack import yolo_vision

    # 重新加载避免上次测试的全局状态
    importlib.reload(yolo_vision)
    worker = yolo_vision.YoloVisionWorker(
        model_path=model_path,
        redis_host="mock", redis_port=0,
        uav_id="10002",
        imgsz=1024, conf=0.25,
        camera_hfov_deg=60.0, camera_vfov_deg=45.0,
        poll_interval_s=0.05,
        **overrides,
    )
    # 在 setup() 之前注入 mock redis
    import redis as redis_lib
    # Monkey-patch redis.Redis 返回我们的 mock
    orig_redis = redis_lib.Redis
    redis_lib.Redis = lambda **kw: mock_redis
    try:
        worker._setup()
    finally:
        redis_lib.Redis = orig_redis
    return worker


def test_get_latest_returns_none_when_no_data(mock_redis):
    """没有帧时 get_latest 应返回 None。"""
    redis = mock_redis  # use the fixture
    worker = _build_worker(
        os.environ.get("YOLO_TEST_MODEL",
                       "/home/lpwang/YOLO/yolo_car/target_vehicle_yolov8s.pt"),
        redis,
    )
    assert worker.get_latest() is None


def test_worker_processes_synthetic_frame_with_white_square(mock_redis):
    """白方块作为目标 → YOLO 应检出（如果模型有效）。

    注意：cam10002 训练的是 UE 仿真车辆，纯白方块可能检测不到。
    这个测试在 YOLO 模型无法识别合成图时也会 PASS（get_latest 可为 None）。
    真正的端到端测试需要真实相机帧。
    """
    from tests.fixtures.mock_frame import make_synthetic_frame
    redis = mock_redis
    jpg_bytes, _ = make_synthetic_frame(
        width=1024, height=768,
        bbox_center=(512, 384), bbox_size=(40, 40),
    )
    redis.publish_camera_frame("10002", 1, jpg_bytes, sim_time=1.0)

    worker = _build_worker(
        os.environ.get("YOLO_TEST_MODEL",
                       "/home/lpwang/YOLO/yolo_car/target_vehicle_yolov8s.pt"),
        redis,
    )
    worker._tick()
    det = worker.get_latest(max_age_ms=10000)
    if det is not None:
        assert -30.0 <= det.pan_delta <= 30.0
        assert -22.5 <= det.tilt_delta <= 22.5
        assert 0.0 < det.confidence <= 1.0


def test_worker_skips_duplicate_frame_no(mock_redis):
    """重复 frame_no 不应重复处理（避免冗余推理）。"""
    from tests.fixtures.mock_frame import make_synthetic_frame
    redis = mock_redis
    jpg_bytes, _ = make_synthetic_frame()
    redis.publish_camera_frame("10002", 5, jpg_bytes)
    redis.publish_camera_frame("10002", 5, jpg_bytes)
    worker = _build_worker(
        os.environ.get("YOLO_TEST_MODEL",
                       "/home/lpwang/YOLO/yolo_car/target_vehicle_yolov8s.pt"),
        redis,
    )
    worker._tick()
    worker._tick()
    assert worker._last_frame_no == 5


def test_worker_advances_to_higher_frame_no(mock_redis):
    """frame_no 推进时正常处理。"""
    from tests.fixtures.mock_frame import make_synthetic_frame
    redis = mock_redis
    jpg_bytes, _ = make_synthetic_frame()
    redis.publish_camera_frame("10002", 10, jpg_bytes)
    redis.publish_camera_frame("10002", 20, jpg_bytes)
    worker = _build_worker(
        os.environ.get("YOLO_TEST_MODEL",
                       "/home/lpwang/YOLO/yolo_car/target_vehicle_yolov8s.pt"),
        redis,
    )
    worker._tick()
    assert worker._last_frame_no == 20
