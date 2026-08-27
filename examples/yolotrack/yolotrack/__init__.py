"""yolotrack: YOLOv8-driven gimbal tracker for OpenSim 赛题四.

子模块：
  - bbox_to_gimbal   纯函数，无外部依赖
  - yolo_vision      后台 worker（依赖 ultralytics, redis, opencv）
  - yolo_controller  继承 FsmSearchTrackController（依赖 search_track）

注意：yolo_controller 不在这里 import，以避免在测试 bbox_to_gimbal 时
强依赖 search_track / ultralytics。controller 由 run.py 显式导入。
"""
from .bbox_to_gimbal import bbox_to_pan_tilt_delta, clip_pan_tilt

__all__ = [
    "bbox_to_pan_tilt_delta",
    "clip_pan_tilt",
]
