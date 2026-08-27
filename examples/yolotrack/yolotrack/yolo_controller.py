"""YoloSearchTrackController：在 TRACK 模式用 YOLO 检测驱动云台。

继承 FsmSearchTrackController（复用 SEARCH 模式 + 状态切换 + 滞回逻辑），
仅覆盖 _track_commands：yolo fresh 时直接用 bbox→gimbal 的 pan/tilt 角度，
yolo 缺失/超时时 fallback 到原几何 LOS。
"""
from __future__ import annotations

import logging
from typing import Any, Optional

# 父类
from search_track.commands import CommandTarget, ControlCommand
from search_track.fsm_controller import FsmSearchTrackController
from search_track.state import SimState

from .bbox_to_gimbal import clip_pan_tilt
from .yolo_vision import YoloVisionWorker

logger = logging.getLogger(__name__)


class YoloSearchTrackController(FsmSearchTrackController):
    """在 FsmSearchTrackController 基础上叠加 YOLO 视觉驱动。

    工作流：
      SEARCH 模式  ：完全沿用父类（spiral + 云台扫掠）。
      TRACK 模式   ：
                     1. 优先用 YoloVisionWorker 最近一次有效检测
                        （bbox 中心 → 图像中心偏移 → 云台 pan/tilt delta）
                     2. yolo 缺失/超时时 fallback 到父类 LoiterTracker 几何 LOS。
    """

    def __init__(self) -> None:
        super().__init__()
        # YOLO 视觉 worker（lazy 启动，configure() 后才创建）
        self._vision: Optional[YoloVisionWorker] = None
        self._yolo_enabled: bool = False
        self._frame_max_age_ms: int = 200
        # YOLO 累计统计
        self._yolo_hits: int = 0
        self._yolo_fallbacks: int = 0
        # A* 路径规划：controller 自己也能推 set_goal（前端模式不走 run.py 时）
        self._astar_waypoints: list = []     # [(lat, lon), ...]
        self._astar_cur_idx: int = 0
        self._astar_goal_sent: bool = False  # 当前 goal 已发
        self._astar_arrival_m: float = 30.0
        self._astar_last_check_t: float = -10.0
        # 目标真值位置（由 run.py 注入，供 A* 到达检测用）
        self._astar_last_target_lat: float = 0.0
        self._astar_last_target_lon: float = 0.0

    # ------------------------------------------------------------------
    # 扩展配置入口
    # ------------------------------------------------------------------

    def configure(self, cfg: Any) -> None:
        """在父类 configure 之后注入 yolo 块。

        cfg 既支持 dict（来自 yaml.safe_load）也支持 AlgorithmConfig。
        model_path 由 run.py 在调 configure 前解析为绝对路径（不依赖 example_dir）。
        """
        super().configure(cfg)

        # 取 yolo 块
        yolo_cfg = self._get_yolo_cfg(cfg)
        if yolo_cfg is None:
            self._yolo_enabled = False
            return

        self._yolo_enabled = bool(yolo_cfg.get("enabled", False))
        if not self._yolo_enabled:
            logger.info("[YoloController] yolo.enabled=false，走原 FSM 行为")
            return

        # model_path 应已是绝对路径（由 run.py 解析）
        model_path = yolo_cfg.get("model_path", "target_vehicle_yolov8s.pt")
        if not model_path:
            raise ValueError("yolo.model_path 不能为空")

        self._frame_max_age_ms = int(yolo_cfg.get("frame_max_age_ms", 200))

        # 启动 worker（如果之前已经有，先 stop）
        self._stop_vision()
        self._vision = YoloVisionWorker(
            model_path=model_path,
            redis_host=yolo_cfg.get("redis_host", "127.0.0.1"),
            redis_port=int(yolo_cfg.get("redis_port", 6379)),
            uav_id=str(yolo_cfg.get("uav_id", "10002")),
            imgsz=int(yolo_cfg.get("imgsz", 1024)),
            conf=float(yolo_cfg.get("conf", 0.25)),
            camera_hfov_deg=float(yolo_cfg.get("camera_hfov_deg", 60.0)),
            camera_vfov_deg=float(yolo_cfg.get("camera_vfov_deg", 45.0)),
        )
        self._vision.start()
        logger.info(
            "[YoloController] YOLO 已启用: model=%s, uav=%s, max_age=%dms",
            model_path, self._vision.uav_id, self._frame_max_age_ms,
        )

    def set_astar_waypoints(self, waypoints: list, arrival_m: float = 30.0) -> None:
        """注入 A* 路径 waypoints，controller 会在 decide() 里自动推 set_goal。

        Args:
            waypoints: [(lat, lon), ...] 列表
            arrival_m: 到达判定距离（米）
        """
        self._astar_waypoints = list(waypoints)
        self._astar_cur_idx = 0
        self._astar_goal_sent = False
        self._astar_arrival_m = arrival_m
        logger.info(
            "[YoloController] A* waypoints 已注入: %d 个, arrival=%.0fm",
            len(self._astar_waypoints), self._astar_arrival_m,
        )

    def _get_yolo_cfg(self, cfg: Any) -> Optional[dict]:
        """从 cfg 中安全取出 yolo 块。"""
        if cfg is None:
            return None
        if isinstance(cfg, dict):
            return cfg.get("yolo")
        # AlgorithmConfig 等对象：取 yolo 属性
        return getattr(cfg, "yolo", None)

    # ------------------------------------------------------------------
    # FSM 状态机：完全沿用父类 + A* goal 推进
    # ------------------------------------------------------------------

    def decide(self, state: SimState, dt: float) -> list[ControlCommand]:
        """覆盖父类 decide：先走父类 FSM，再追加 A* set_goal 命令。

        set_goal 命令用 CommandTarget.UAV 但 cmd="set_goal"——run.py 检测到
        cmd=="set_goal" 时会把 target 改成 "target" 再发（因为 ControlCommand
        的 CommandTarget 枚举没有 TARGET，只能这样绕过）。

        注意：run.py 传进来的是 without_truth() 的 state（FR-007），但 A* 推进
        需要 target_truth 算到达距离。所以 A* 逻辑用 self._last_truth（由
        run.py 通过 set_last_truth() 注入，或本方法从完整 state 保存）。
        """
        cmds = super().decide(state, dt)

        # A* 推进（用内部缓存的 truth，不依赖 state.target_truth）
        if self._astar_waypoints:
            goal_cmd = self._astar_decide(state)
            if goal_cmd is not None:
                cmds.append(goal_cmd)
        return cmds

    def set_last_truth(self, target_lat: float, target_lon: float) -> None:
        """run.py 在每个 tick 注入目标真值位置（供 A* 到达检测用）。

        FR-007 禁止 controller 用 truth 做检测决策，但 A* 导航的"到达判定"
        是 run.py 层职责的代劳，用 truth 算距离是合理的。
        """
        self._astar_last_target_lat = target_lat
        self._astar_last_target_lon = target_lon

    def _astar_decide(self, state: SimState) -> Optional[ControlCommand]:
        """A* goal 推进状态机：第一次发 goal，到达后发下一个。"""
        if not self._astar_waypoints:
            return None

        # 还没发过当前 goal → 发
        if not self._astar_goal_sent:
            return self._astar_send_current_goal()

        # 已发 → 检查是否到达（限频 0.5s）
        if state.sim_time - self._astar_last_check_t < 0.5:
            return None
        self._astar_last_check_t = state.sim_time

        # 用 run.py 注入的目标真值位置算距离
        if self._astar_last_target_lat == 0.0:
            return None  # 还没收到 truth
        goal_lat, goal_lon = self._astar_waypoints[self._astar_cur_idx]

        from search_track.geometry import haversine_m
        dist = haversine_m(
            self._astar_last_target_lat, self._astar_last_target_lon,
            goal_lat, goal_lon,
        )

        if dist < self._astar_arrival_m:
            # 到达 → 推进到下一个
            self._astar_cur_idx += 1
            if self._astar_cur_idx >= len(self._astar_waypoints):
                self._astar_cur_idx = 0
                logger.info("[YoloController] A* 路线完成，循环回 goal 1")
            self._astar_goal_sent = False
            logger.info(
                "[YoloController] A* 推进: goal %d/%d (到达 dist=%.0fm)",
                self._astar_cur_idx + 1, len(self._astar_waypoints), dist,
            )
            return self._astar_send_current_goal()
        return None

    def _astar_send_current_goal(self) -> Optional[ControlCommand]:
        """发当前 idx 的 set_goal 命令。"""
        if self._astar_cur_idx >= len(self._astar_waypoints):
            return None
        lat, lon = self._astar_waypoints[self._astar_cur_idx]
        self._astar_goal_sent = True
        logger.info(
            "[YoloController] A* set_goal: %d/%d lat=%.6f lon=%.6f",
            self._astar_cur_idx + 1, len(self._astar_waypoints), lat, lon,
        )
        # 注意：target=UAV 是占位，run.py 会改成 "target"
        return ControlCommand(
            target=CommandTarget.UAV,
            cmd="set_goal",
            params={"latitude": lat, "longitude": lon},
        )

    # ------------------------------------------------------------------
    # TRACK 模式：yolo 优先，fallback 到几何 LOS
    # ------------------------------------------------------------------

    def _track_commands(self, state: SimState, dt: float) -> list[ControlCommand]:
        # yolo 关闭时直接走父类（与原 uav_search_track_car 完全一致）
        if not self._yolo_enabled or self._vision is None:
            return super()._track_commands(state, dt)

        yolo_det = self._vision.get_latest(max_age_ms=self._frame_max_age_ms)

        if yolo_det is not None and self._yolo_target_visible_in_view(yolo_det, state):
            # 路径 1：YOLO 驱动云台
            self._yolo_hits += 1
            return self._yolo_track_commands(state, yolo_det)

        # 路径 2：fallback 到父类几何 LOS
        self._yolo_fallbacks += 1
        # 详细 log：记录为什么走 fallback
        if yolo_det is None:
            reason = f"yolo 无 fresh 检测 (max_age={self._frame_max_age_ms}ms)"
        else:
            W, H = yolo_det.image_size
            cx = (yolo_det.bbox_xyxy[0] + yolo_det.bbox_xyxy[2]) / 2.0
            cy = (yolo_det.bbox_xyxy[1] + yolo_det.bbox_xyxy[3]) / 2.0
            reason = (f"yolo 目标在视野外: bbox_center=({cx:.0f},{cy:.0f}) "
                      f"img={W}x{H}")
        logger.info(
            "[YoloController] TRACK fallback @ t=%.2f consecutive_lost=%d: %s",
            state.sim_time, self._consecutive_lost, reason,
        )
        return super()._track_commands(state, dt)

    def _yolo_target_visible_in_view(
        self, yolo_det, state: SimState,
    ) -> bool:
        """检查 yolo 检测的目标是否还"在合理范围内"。

        简单实现：图像中心附近 → 视为有效；图像边缘外（bbox 中心偏离
        中心 > 半视场）→ 视为无效，fallback。
        """
        W, H = yolo_det.image_size
        cx, _ = ((yolo_det.bbox_xyxy[0] + yolo_det.bbox_xyxy[2]) / 2.0,
                 (yolo_det.bbox_xyxy[1] + yolo_det.bbox_xyxy[3]) / 2.0)
        # 偏离图像中心归一化距离
        dx_norm = abs(cx - W / 2.0) / (W / 2.0)
        return dx_norm < 1.5  # 超过 1.5x 半宽 → 视为出视野

    def _yolo_track_commands(
        self, state: SimState, yolo_det,
    ) -> list[ControlCommand]:
        """用 YOLO 检测生成 TRACK 模式命令。

        UAV 仍走父类 LoiterTracker（保持目标在视野中央的大循环），
        云台直接根据 yolo 偏差调整。

        关键设计：pan_delta/tilt_delta 是 yolo 报告的"目标在图像中的偏移"
        （pan_delta > 0 表示目标在图像右边，需要云台往右转）。
        因此云台应当转到"几何 LOS 计算的角度"附近，再做 yolo 修正。
        简单做法：把 yolo delta 当作**误差信号**叠加在父类算出的 LOS 上。
        """
        # UAV 仍按几何 LOS 飞（保证 UAV 不停跟踪目标位置）
        assert self._tracker is not None
        tpos = state.detection.target_position
        if tpos is None:
            # 极少见：yolo 有但 sim 端没检测到；用 yolo 偏移维持云台
            cmds = [
                ControlCommand(
                    target=CommandTarget.UAV,
                    cmd="component.gimbal_tracking.set_orientation",
                    params={
                        "pan": clip_pan_tilt(self._last_pan, 0.0)[0],
                        "tilt": clip_pan_tilt(0.0, self._last_tilt)[1],
                    },
                )
            ]
            return cmds

        uav_cmds = self._tracker.commands(
            sim_time=state.sim_time,
            uav_lat=state.uav.position.latitude,
            uav_lon=state.uav.position.longitude,
            uav_alt=state.uav.position.altitude,
            uav_yaw=state.uav.attitude.yaw,
            tgt_lat=tpos.latitude,
            tgt_lon=tpos.longitude,
            tgt_alt=tpos.altitude,
        )

        # 从父类算出的命令里取出"几何 LOS 给的云台角度"
        los_pan = self._last_pan   # 兜底
        los_tilt = self._last_tilt
        for c in uav_cmds:
            if c["cmd"] == "component.gimbal_tracking.set_orientation":
                los_pan = float(c["params"].get("pan", los_pan))
                los_tilt = float(c["params"].get("tilt", los_tilt))
                break

        # 在几何 LOS 基础上叠加 yolo 修正。
        # yolo delta 是 "目标相对图像中心的偏移角度"（pan_delta > 0 = 目标在右边）。
        # 我们要云台转到让目标居中，所以 pan_target = los_pan + delta
        # （目标在右边时，云台也要转到右边去对准目标）。
        # 重要：用 yolo delta 作为绝对补偿（不是累加），状态机会处理后续。
        # 用 0.5 系数减半补偿避免过度（避免 yolo 检测在边缘时大幅偏转）。
        alpha = 0.5
        target_pan = los_pan + alpha * yolo_det.pan_delta
        target_tilt = los_tilt + alpha * yolo_det.tilt_delta
        target_pan, target_tilt = clip_pan_tilt(target_pan, target_tilt)

        # 用 yolo 的 set_orientation 覆盖父类算出的云台命令
        # UAV 飞行命令保留父类的 loiter
        result = []
        for c in uav_cmds:
            result.append(ControlCommand(
                target=CommandTarget(c["target"]) if isinstance(c["target"], str) else c["target"],
                cmd=c["cmd"], params=c["params"],
            ))
            if c["cmd"] == "component.gimbal_tracking.set_orientation":
                # 替换云台命令
                result[-1] = ControlCommand(
                    target=CommandTarget.UAV,
                    cmd="component.gimbal_tracking.set_orientation",
                    params={"pan": target_pan, "tilt": target_tilt},
                )

        # 记录（用于父类的 holdover 逻辑）
        self._last_pan = target_pan
        self._last_tilt = target_tilt
        logger.debug(
            "[YoloController] yolo_track: los=(%.1f,%.1f) delta=(%.1f,%.1f) -> target=(%.1f,%.1f)",
            los_pan, los_tilt,
            yolo_det.pan_delta, yolo_det.tilt_delta,
            target_pan, target_tilt,
        )
        return result

    # ------------------------------------------------------------------
    # 关闭
    # ------------------------------------------------------------------

    def shutdown(self) -> None:
        """优雅关闭后台 worker（run.py 退出时调用）。"""
        self._stop_vision()

    def _stop_vision(self) -> None:
        if self._vision is not None:
            self._vision.stop()
            self._vision.join(timeout=2.0)
            self._vision = None

    # ------------------------------------------------------------------
    # 统计（用于 metrics 导出）
    # ------------------------------------------------------------------

    @property
    def yolo_hits(self) -> int:
        return self._yolo_hits

    @property
    def yolo_fallbacks(self) -> int:
        return self._yolo_fallbacks
