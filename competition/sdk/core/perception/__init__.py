"""感知层 (spec 029): 识别器 + photo 缓存 + 三态编排。"""
from .base import BaseDetector
from .bbox_to_latlon import meters_to_deg, pan_tilt_to_latlon
from .default_detectors import AccuracySimulator, YoloDetector
from .photo_cache import PhotoCache
from .resolver import DetectionResolver

__all__ = [
    "BaseDetector", "AccuracySimulator", "YoloDetector",
    "PhotoCache", "DetectionResolver",
    "meters_to_deg", "pan_tilt_to_latlon",
]
