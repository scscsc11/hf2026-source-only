"""识别器抽象基类 (spec 029).

所有识别器（AccuracySimulator / YoloDetector / 选手自研包装）实现同一接口，
使 DetectionResolver 能统一调度。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import List, Optional

from ..observation import Detection, Observation


class BaseDetector(ABC):
    """识别器接口：每帧从 obs 产出 List[Detection]。"""

    @abstractmethod
    def detect(self, obs: Observation, dt: float,
               truth_source: Optional[Detection] = None) -> List[Detection]:
        """产出本帧检测结果。空列表 = 本帧无检测。

        Args:
            obs: 选手可见的 Observation（其 ``self.detection`` 是空占位，
                不含引擎真值）。
            truth_source: 引擎几何真值源（runner 内部通道，选手不可见）。
                AccuracySimulator 据此按 accuracy 概率采样 + 噪声；
                YoloDetector / 选手自研识别器忽略它。
        """
        ...

    def start(self) -> None:
        """启动后台资源（如 YoloVisionWorker 线程）。默认无操作。"""
        return None

    def stop(self) -> None:
        """释放后台资源。默认无操作。"""
        return None
