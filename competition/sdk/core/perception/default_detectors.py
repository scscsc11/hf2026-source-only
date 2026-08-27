"""默认识别器 (spec 029).

AccuracySimulator: 训练态，按 accuracy 概率从引擎几何真值采样 detection。
YoloDetector: 验证态，动态 import 复用 examples/yolotrack 的 YoloVisionWorker。
"""
from __future__ import annotations

import random
from typing import List, Optional

from ..observation import Detection, Observation
from .base import BaseDetector
from .bbox_to_latlon import meters_to_deg, pan_tilt_to_latlon   # DRY: 共用几何工具

# ── 天气衰减系数表 ────────────────────────────────────────────────────
# 每项 = (accuracy_factor, noise_factor)，与渲染端 6 种天气枚举一一对应
# （见 AGENTS.md「场景天气配置」）。effective 值 = base × factor：
#   - accuracy_factor ≤ 1.0：恶劣天气降低检出概率
#   - noise_factor  ≥ 1.0：恶劣天气放大位置噪声
# 晴天 (1.0, 1.0) 为基准（不衰减）。未知 type 兜底为晴天。
WEATHER_FACTORS: dict = {
    "Clear_Skies":     (1.0,  1.0),
    "Partly_Cloudy":   (0.97, 1.1),
    "Rain":            (0.88, 1.4),
    "Foggy":           (0.80, 1.7),
    "Snow_Light":      (0.85, 1.5),
    "Sand_Dust_Calm":  (0.78, 1.8),
}
# 晴天基准（未知 weather 兜底）
_CLEAR_SKY = (1.0, 1.0)


class AccuracySimulator(BaseDetector):
    """训练态默认识别器：按 accuracy 从引擎几何真值概率采样。

    真值源通过 ``truth_source`` 参数（runner→resolver→detector 内部通道）
    传入，**绝不读 obs.self.detection**（该字段对选手是空占位，以防 S-2
    真值泄漏）。按 accuracy 概率伯努利检出；命中时位置加高斯噪声。

    ``weather`` 按天气类型对 base accuracy/noise 施加乘性衰减（见
    :data:`WEATHER_FACTORS`）：晴天不衰减，恶劣天气检出率下降、噪声增大。
    衰减在 detect() 运行时内部施加，不污染构造时传入的 base 值。
    """

    def __init__(self, accuracy: float, noise_sigma_m: float,
                 weather: str = "Clear_Skies",
                 rng_seed: Optional[int] = None) -> None:
        self.accuracy = float(accuracy)
        self.noise_sigma_m = float(noise_sigma_m)
        acc_f, noise_f = WEATHER_FACTORS.get(weather, _CLEAR_SKY)
        self.weather = weather
        self.effective_accuracy = round(self.accuracy * acc_f, 6)
        self.effective_noise_sigma_m = round(self.noise_sigma_m * noise_f, 6)
        self._rng = random.Random(rng_seed) if rng_seed is not None else random.Random()

    def detect(self, obs: Observation, dt: float,
               truth_source: Optional[Detection] = None) -> List[Detection]:
        truth = truth_source if truth_source is not None else Detection(detected=False, confidence=0.0)
        # 引擎无目标 → 不检出（无中生有）
        if not truth.detected:
            return [Detection(detected=False, confidence=0.0,
                              target_type=truth.target_type)]
        # 伯努利检出（用天气衰减后的 effective accuracy）
        if self._rng.random() > self.effective_accuracy:
            return [Detection(detected=False, confidence=0.0,
                              target_type=truth.target_type)]
        # 命中：位置加高斯噪声（用天气衰减后的 effective noise）
        lat = truth.target_lat
        lon = truth.target_lon
        if self.effective_noise_sigma_m > 0 and lat is not None and lon is not None:
            d_lat = self._rng.gauss(0, meters_to_deg(self.effective_noise_sigma_m, lat, False))
            d_lon = self._rng.gauss(0, meters_to_deg(self.effective_noise_sigma_m, lat, True))
            lat += d_lat
            lon += d_lon
        # confidence ∈ [eff_accuracy*0.8, eff_accuracy]
        lo, hi = self.effective_accuracy * 0.8, self.effective_accuracy
        conf = self._rng.uniform(lo, hi) if hi > lo else hi
        return [Detection(detected=True, confidence=round(conf, 4),
                          target_lat=lat, target_lon=lon,
                          azimuth_error_deg=truth.azimuth_error_deg,
                          target_type=truth.target_type)]


class YoloDetector(BaseDetector):
    """验证态默认识别器：动态 import 复用 examples/yolotrack 的 YoloVisionWorker。

    不复制 yolotrack 代码。通过 sys.path + import 复用已跑通的 YOLOv8 推理。
    SDK 侧仅新增 bbox→lat/lon 反算（pan_tilt_to_latlon）。
    ultralytics 延迟 import 到方法内，SDK 核心不耦合。
    """

    def __init__(self, model_path: str, uav_id: str, imgsz: int = 1024,
                 conf: float = 0.25, camera_hfov_deg: float = 60.0,
                 camera_vfov_deg: float = 45.0,
                 yolotrack_path: str = "examples/yolotrack",
                 redis_host: str = "127.0.0.1", redis_port: int = 6379) -> None:
        self.model_path = model_path
        self.uav_id = uav_id
        self.imgsz = imgsz
        self.conf = conf
        self.camera_hfov_deg = camera_hfov_deg
        self.camera_vfov_deg = camera_vfov_deg
        self.yolotrack_path = yolotrack_path
        self.redis_host = redis_host
        self.redis_port = redis_port
        self._worker = None   # 延迟创建（start 时）

    def start(self) -> None:
        """启动后台 YoloVisionWorker 线程。延迟 import ultralytics。"""
        if self._worker is not None:
            return   # 已在运行，幂等（防重复 start 泄漏 worker）
        import sys
        from pathlib import Path
        repo_root = Path(__file__).resolve().parents[4]
        yt = str(Path(self.yolotrack_path)
                 if Path(self.yolotrack_path).is_absolute()
                 else repo_root / self.yolotrack_path)
        if yt not in sys.path:
            sys.path.insert(0, yt)
        from yolotrack.yolo_vision import YoloVisionWorker   # 延迟 import
        self._worker = YoloVisionWorker(
            model_path=self.model_path, uav_id=self.uav_id,
            imgsz=self.imgsz, conf=self.conf,
            camera_hfov_deg=self.camera_hfov_deg,
            camera_vfov_deg=self.camera_vfov_deg,
            redis_host=self.redis_host, redis_port=self.redis_port)
        self._worker.start()

    def stop(self) -> None:
        if self._worker is not None:
            self._worker.stop()
            self._worker = None

    def detect(self, obs: Observation, dt: float,
               truth_source: Optional[Detection] = None) -> List[Detection]:
        # truth_source 由 AccuracySimulator 使用；YOLO 走真实推理，忽略它。
        if self._worker is None:
            return []
        yd = self._worker.get_latest(max_age_ms=200)
        if yd is None:
            return []
        tlat, tlon = pan_tilt_to_latlon(
            uav_lat=obs.self.lat, uav_lon=obs.self.lon, uav_alt=obs.self.alt,
            gimbal_pan=obs.self.gimbal_pan, gimbal_tilt=obs.self.gimbal_tilt,
            pan_delta=yd.pan_delta, tilt_delta=yd.tilt_delta)
        return [Detection(
            detected=True, confidence=float(yd.confidence),
            target_lat=tlat, target_lon=tlon,
            target_type=str(getattr(yd, "class_name", "ground_vehicle")))]
