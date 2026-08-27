"""DetectionResolver — 三态编排 + 渲染门控降级 (spec 029 + spec 032).

封装 runner 第 4 步：调 agent.sensor()，据返回值决定 detection 来源。
三态：
  List[Detection]  → 用选手结果（自研）
  SKIP_DETECTION   → 返回 None（端到端，跳过默认识别器）
  None / 未覆盖    → 调默认识别器
异常容错：sensor 抛异常 → 回退默认识别器（不崩帧）。

spec 032 渲染门控：当 sensor 返回 None（走默认识别器）时，据 obs.self.photo
是否为 None 决定用 primary（YoloDetector，需 photo）还是 fallback
（AccuracySimulator，无需 photo）。自研 sensor 不受门控影响。
"""
from __future__ import annotations

from typing import Callable, List, Optional, Set

from ..agent import Agent
from ..observation import Detection, Observation, SKIP_DETECTION
from .base import BaseDetector


class DetectionResolver:
    """每帧决策 detection 来源的三态编排器 + 渲染门控降级。

    Args:
        default_detector: 主默认识别器（向后兼容：赛题一 eval 模式 = YoloDetector；
            train 模式 = AccuracySimulator）。无 fallback 时行为与 spec 029 一致。
        fallback_detector: 降级识别器（spec 032：当 primary 是 YoloDetector 但
            该 UAV 无 photo 时自动降级到此）。通常 = AccuracySimulator。
        warn_fn: warning 输出函数（默认 print）。降级时 per-uid 调一次。
    """

    def __init__(self, default_detector: Optional[BaseDetector],
                 fallback_detector: Optional[BaseDetector] = None,
                 warn_fn: Callable[[str], None] = print) -> None:
        self._default = default_detector
        self._fallback = fallback_detector
        self._warn_fn = warn_fn if fallback_detector is not None else None
        self._warned_uids: Set[str] = set()

    def resolve(self, agent: Agent, obs: Observation,
                dt: float,
                truth_source: Optional[Detection] = None) -> Optional[List[Detection]]:
        """返回 List[Detection]（自研/默认）或 None（端到端跳过）。

        Args:
            truth_source: 引擎几何真值源（runner 内部通道）。仅默认识别器
                （AccuracySimulator）使用；选手 sensor / 端到端均不触碰。
                obs.self.detection 是空占位，**绝不**作为真值源。
        """
        try:
            result = agent.sensor(obs, dt)
        except Exception:
            # sensor 异常 → 回退默认识别器（容错，不崩帧）
            result = None
        if result is SKIP_DETECTION:
            return None
        if result is None:
            return self._resolve_default(obs, truth_source)
        # List[Detection] 或空列表 — 用选手结果
        return list(result)

    def _resolve_default(self, obs: Observation,
                         truth_source: Optional[Detection] = None) -> Optional[List[Detection]]:
        """走默认识别器，应用渲染门控降级 (spec 032)。"""
        if self._default is None:
            return None
        # spec 032 门控：有 fallback 且 obs 无 photo → 降级
        if self._fallback is not None and obs.self.photo is None:
            self._warn_once(obs.self.uid)
            return self._fallback.detect(obs, dt=0.0, truth_source=truth_source)
        return self._default.detect(obs, dt=0.0, truth_source=truth_source)

    def _warn_once(self, uid: str) -> None:
        """per-uid 一次性 warning：该 UAV 无 photo，降级到 AccuracySimulator。"""
        if self._warn_fn is None or uid in self._warned_uids:
            return
        self._warned_uids.add(uid)
        self._warn_fn(
            f"[perception] UAV {uid} has no photo frame, "
            f"falling back to AccuracySimulator (spec 032 render gate)")

    def stop(self) -> None:
        """释放默认识别器后台资源（spec 029）。

        AccuracySimulator 是无状态的 no-op；YoloDetector 持有后台 worker
        线程 + redis 连接，必须显式 stop 否则 run() 结束后泄漏。
        """
        if self._default is not None:
            self._default.stop()
        if self._fallback is not None:
            self._fallback.stop()
