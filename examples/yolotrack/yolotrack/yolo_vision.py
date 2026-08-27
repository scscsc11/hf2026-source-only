"""YOLO 视觉 worker：后台线程，订阅 spec/022 相机帧，推理，写入 thread-safe latest。

spec/022 协议（详见 /home/lpwang/ZCodeProject/opensim/specs/022-uav-camera-feed/）：
    KEY    sync_camera:{uav_id}:frame:{frame_no}   # type: hash
    FIELD  sim_time  = <double-as-string>
    FIELD  image     = <JPEG binary>

由于 Redis hash 没有"最新帧"指针，worker 用 SCAN 周期轮询，取最大 frame_no
（与上一帧相比更新才解码，节省 CPU）。

调试开关（环境变量）：
    YOLO_SAVE_MISS_DIR  设为目录路径后，每次 yolo 推理未检出都会把原始图
                        存到该目录（PNG），方便人工验证模型表现。
"""
from __future__ import annotations

import io
import logging
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import numpy as np

from .bbox_to_gimbal import bbox_to_pan_tilt_delta

logger = logging.getLogger(__name__)

# 调试：未识别帧保存目录（None 表示不保存）
_MISS_DIR = os.environ.get("YOLO_SAVE_MISS_DIR")


@dataclass
class YoloDetection:
    """YOLO 一次推理结果（线程间传递用）。"""
    bbox_xyxy: tuple[float, float, float, float]   # (x1, y1, x2, y2)
    confidence: float
    class_id: int
    class_name: str
    image_size: tuple[int, int]                   # (W, H)
    pan_delta: float                              # 度
    tilt_delta: float                             # 度
    sim_time: float                               # sim 侧时间戳（与 state 对齐）
    wall_time_ms: int                             # 写入 _latest 的本地时间


class YoloVisionWorker(threading.Thread):
    """订阅 spec/022 相机帧，YOLO 推理，输出 pan/tilt 增量。

    用法：
        worker = YoloVisionWorker(
            model_path="target_vehicle_yolov8s.pt",
            redis_host="127.0.0.1", redis_port=6379,
            uav_id="10002",
            imgsz=1024, conf=0.25,
            camera_hfov_deg=60.0, camera_vfov_deg=45.0,
        )
        worker.start()
        ...
        det = worker.get_latest(max_age_ms=200)
        worker.stop()
    """

    def __init__(
        self,
        model_path: str,
        redis_host: str = "127.0.0.1",
        redis_port: int = 6379,
        uav_id: str = "10002",
        imgsz: int = 1024,
        conf: float = 0.25,
        camera_hfov_deg: float = 60.0,
        camera_vfov_deg: float = 45.0,
        poll_interval_s: float = 0.05,           # 20Hz 轮询
        target_class: Optional[str] = None,      # 类别过滤；None = 取第一检测
    ):
        super().__init__(daemon=True, name="YoloVisionWorker")
        self.model_path = model_path
        self.redis_host = redis_host
        self.redis_port = redis_port
        self.uav_id = uav_id
        self.imgsz = imgsz
        self.conf = conf
        self.camera_hfov_deg = camera_hfov_deg
        self.camera_vfov_deg = camera_vfov_deg
        self.poll_interval_s = poll_interval_s
        self.target_class = target_class

        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self._latest: Optional[YoloDetection] = None

        # 延迟加载（线程启动后才 import ultralytics）
        self._model = None
        self._redis = None
        self._last_frame_no = -1
        self._target_class_id: Optional[int] = None

    def stop(self) -> None:
        self._stop_event.set()

    def get_latest(self, max_age_ms: int = 200) -> Optional[YoloDetection]:
        """取最近一次有效 YOLO 检测（按 wall_time 算 age）。"""
        with self._lock:
            det = self._latest
        if det is None:
            return None
        age_ms = int((time.time() * 1000) - det.wall_time_ms)
        if age_ms > max_age_ms:
            return None
        return det

    def run(self) -> None:
        try:
            self._setup()
        except Exception as e:
            logger.error("[YoloVisionWorker] 初始化失败: %s", e)
            return

        logger.info(
            "[YoloVisionWorker] 已启动: uav=%s, imgsz=%d, fov=%.1fx%.1f°",
            self.uav_id, self.imgsz,
            self.camera_hfov_deg, self.camera_vfov_deg,
        )

        while not self._stop_event.is_set():
            try:
                self._tick()
            except Exception as e:  # noqa: BLE001
                logger.warning("[YoloVisionWorker] tick 异常: %s", e)
            self._stop_event.wait(self.poll_interval_s)

        logger.info("[YoloVisionWorker] 已停止")

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _setup(self) -> None:
        # YOLO 模型
        from ultralytics import YOLO
        model_path = Path(self.model_path)
        if not model_path.exists():
            raise FileNotFoundError(
                f"YOLO 模型文件不存在: {model_path}（绝对路径：{model_path.absolute()}）"
            )
        self._model = YOLO(str(model_path))
        if self.target_class is not None:
            names = self._model.names
            for cid, cname in names.items():
                if cname == self.target_class:
                    self._target_class_id = cid
                    break
            if self._target_class_id is None:
                raise ValueError(
                    f"模型类别中找不到 {self.target_class!r}（{names}）"
                )

        # Redis
        import redis
        self._redis = redis.Redis(
            host=self.redis_host,
            port=self.redis_port,
            decode_responses=False,
        )
        # 触发一次连接（首次连不上不抛，轮询时再判）
        try:
            self._redis.ping()
        except Exception as e:
            logger.warning("[YoloVisionWorker] Redis 不可达: %s", e)

    def _tick(self) -> None:
        if self._redis is None:
            return

        # 找最新帧
        latest = self._find_latest_frame()
        if latest is None:
            return
        frame_no, key = latest
        if frame_no <= self._last_frame_no:
            return  # 还没新帧

        # 读取 + 解码
        data = self._redis.hgetall(key)
        if not data or b"image" not in data:
            # 没有 image 字段也要推进 frame_no，避免死循环
            self._last_frame_no = frame_no
            return
        image_bytes = data[b"image"]
        sim_time = float(data.get(b"sim_time", b"0.0") or 0.0)

        img = self._decode(image_bytes)
        if img is None:
            self._last_frame_no = frame_no
            return

        # YOLO 推理
        result = self._infer(img)
        # 不论是否检出，都要推进 frame_no（否则下一 tick 还在处理同一帧）
        self._last_frame_no = frame_no
        if result is None:
            # 推理无检出：log + 可选保存原图
            H, W = img.shape[:2]
            logger.info(
                "[YoloVisionWorker] MISS frame=%d sim_time=%.2f key=%s "
                "img=%dx%d conf_threshold=%.2f (no detection above threshold)",
                frame_no, sim_time, key.decode("utf-8", "replace"),
                W, H, self.conf,
            )
            if _MISS_DIR:
                self._save_miss_frame(frame_no, sim_time, img)
            return

        # 写入 latest
        with self._lock:
            self._latest = result
        logger.debug(
            "[YoloVisionWorker] HIT frame=%d conf=%.2f pan=%.1f tilt=%.1f",
            frame_no, result.confidence, result.pan_delta, result.tilt_delta,
        )

    def _save_miss_frame(self, frame_no: int, sim_time: float, img: np.ndarray) -> None:
        """把未识别的帧保存到磁盘（仅 YOLO_SAVE_MISS_DIR 设置时启用）。"""
        if not _MISS_DIR:
            return
        try:
            miss_dir = Path(_MISS_DIR)
            miss_dir.mkdir(parents=True, exist_ok=True)
            out = miss_dir / f"miss_frame{frame_no:06d}_sim{sim_time:.1f}.png"
            import cv2
            cv2.imwrite(str(out), img)
            logger.info("[YoloVisionWorker] saved miss frame: %s", out)
        except Exception as e:  # noqa: BLE001
            logger.warning("[YoloVisionWorker] save miss frame failed: %s", e)

    def _find_latest_frame(self) -> Optional[tuple[int, bytes]]:
        """SCAN 找最大 frame_no。返回 (frame_no, key)。"""
        pattern = f"sync_camera:{self.uav_id}:frame:*"
        max_no = -1
        max_key: Optional[bytes] = None
        for key in self._redis.scan_iter(match=pattern, count=100):
            # key 是 bytes 形式如 b'sync_camera:10002:frame:326'
            try:
                tail = key.rsplit(b":", 1)[-1]
                no = int(tail)
            except (ValueError, IndexError):
                continue
            if no > max_no:
                max_no = no
                max_key = key
        if max_key is None:
            return None
        return (max_no, max_key)

    def _decode(self, image_bytes: bytes) -> Optional[np.ndarray]:
        """JPEG/PNG bytes -> BGR numpy 数组。"""
        import cv2
        arr = np.frombuffer(image_bytes, dtype=np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        return img

    def _infer(self, img: np.ndarray) -> Optional[YoloDetection]:
        """YOLO 推理 + 选最优检测 + 算 pan/tilt delta。"""
        results = self._model(
            img, imgsz=self.imgsz, conf=self.conf, verbose=False,
        )
        if not results or results[0].boxes is None:
            return None
        boxes = results[0].boxes
        if len(boxes) == 0:
            return None

        # 选第一检测（或按类别过滤后取最高分）
        best_idx = 0
        best_conf = float(boxes.conf[0])
        for i in range(1, len(boxes)):
            cls_i = int(boxes.cls[i])
            if self._target_class_id is not None and cls_i != self._target_class_id:
                continue
            c = float(boxes.conf[i])
            if c > best_conf:
                best_conf = c
                best_idx = i

        box = boxes[best_idx]
        cls_id = int(box.cls[0])
        if (self._target_class_id is not None
                and cls_id != self._target_class_id):
            return None  # 过滤后无符合

        xyxy = box.xyxy[0].cpu().numpy().tolist()
        cx = (xyxy[0] + xyxy[2]) / 2.0
        cy = (xyxy[1] + xyxy[3]) / 2.0
        H, W = img.shape[:2]
        pan_delta, tilt_delta = bbox_to_pan_tilt_delta(
            (cx, cy), (W, H),
            self.camera_hfov_deg, self.camera_vfov_deg,
        )

        names = results[0].names
        return YoloDetection(
            bbox_xyxy=tuple(xyxy),
            confidence=best_conf,
            class_id=cls_id,
            class_name=names.get(cls_id, str(cls_id)),
            image_size=(W, H),
            pan_delta=pan_delta,
            tilt_delta=tilt_delta,
            sim_time=0.0,  # sim_time 不直接通过 worker 暴露（controller 自己从 state 取）
            wall_time_ms=int(time.time() * 1000),
        )
