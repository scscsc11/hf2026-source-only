# yolotrack — 赛题四 · YOLO 视觉跟踪

> **spec 029 说明**：本示例的 `YoloVisionWorker`（`yolotrack/yolo_vision.py`）已被 SDK 的 `YoloDetector`（验证态默认识别器）通过动态 import 复用。yolotrack 仍是 YOLO 检测算法的唯一真相源与独立运行入口；SDK 不复制其代码，仅通过 `sys.path + import` 在 `YoloDetector.start()` 时加载。`yolo_controller.py`（FSM 控制决策）不在复用范围内——那是选手 `decide()` 的职责。

基于 YOLOv8 的无人机云台跟踪 example，作为 OpenSim 赛题四提交。前端可自动发现和启动。

## 与 `uav_search_track_car` 的差异

| 项 | uav_search_track_car | yolotrack（本例） |
|----|---------------------|------------------|
| Detection provider | sim 端 `GimbalTrackingComponent` 几何 FOV | YOLOv8 视觉模型 |
| 控制器 | `FsmSearchTrackController` | `YoloSearchTrackController`（继承前者） |
| 相机帧 | 不用 | 订阅 spec/022 Redis hash |
| UAV 飞行控制 | 不变 | 不变（仍走 LoiterTracker 几何 LOS） |
| 云台指向 | 几何 LOS | bbox 中心 → 图像中心偏移 → pan/tilt delta |

**搜索模式完全沿用父类**；**跟踪模式优先用 YOLO 检测结果驱动云台**，yolo 检测缺失时自动 fallback 到几何 LOS。

## 快速开始

### CLI 模式

```bash
# 1. 装依赖
cd examples/yolotrack
pip install -r requirements.txt

# 2. dry-run（不连 redis，2 秒内退出）
cd ../../..     # 回到 opensim 根
python -m examples.yolotrack.run --dry-run --duration 0.3

# 3. 启动 redis + sim + 跑
redis-server &
python -m examples.yolotrack.run --start-sim --duration 60

# 4. 关闭 YOLO（等同 uav_search_track_car 行为）
python -m examples.yolotrack.run --no-yolo --start-sim --duration 30
```

### 前端模式

1. 启动 frontend（`start_all.sh` 或 `cd visualization && npm start`）
2. 浏览器打开面板，应该看到 "赛题四:YOLO 视觉跟踪" 选项
3. 点击启动 → 前端自动 spawn `opensim-sim` + 启动本 controller

## 数据流

```
UE 渲染端 (testwl) 
  ↓ publish to Redis hash (spec/022)
sync_camera:{uav_id}:frame:{frame_no}   (JPEG, sim_time, frame_no)
  ↓ subscribe (worker thread, in yolo_vision.py)
YoloVisionWorker
  - decode JPEG
  - YOLO model.track(imgsz=1024, conf=0.25)
  - bbox 中心 → pan/tilt delta
  - 写入 self._latest (thread-safe, max_age_ms)
  ↓ decide() 读取
YoloSearchTrackController.decide(state, dt)
  - SEARCH 模式：原 FSM（spiral + 云台扫掠）
  - TRACK 模式：
      * yolo fresh + 目标在视野内 → set_orientation(yolo_pan, yolo_tilt)
      * yolo 缺失/超时/出视野 → fallback 到父类几何 LOS
  ↓ publish
sim:commands → sim 端 gimbal 跟随
```

## 文件结构

```
examples/yolotrack/
├── __init__.py             # 空（让 python -m examples.yolotrack.run 可解析）
├── run.py                  # 入口，继承 _common/argparser + sim_runner
├── manifest.json           # 5 字段严格符合 spec/024
├── README.md
├── requirements.txt        # ultralytics, lap, opencv-python, redis
├── target_vehicle_yolov8s.pt  # 软链到 yolo_car 仓的 best.pt
├── yolotrack/              # 核心代码包
│   ├── __init__.py         # 只导出 bbox_to_gimbal（避免 import 链污染）
│   ├── bbox_to_gimbal.py   # 纯函数：bbox 中心 → pan/tilt delta
│   ├── yolo_vision.py      # 后台 worker：订阅 spec/022 + YOLO 推理
│   └── yolo_controller.py  # 继承 FsmSearchTrackController
├── config/
│   ├── algorithm.yaml      # FSM 参数 + yolo 块
│   └── scenario.json       # 1 FixedWingUAV (gimbal) + 1 TargetVehicle
└── tests/
    ├── conftest.py
    ├── pytest.ini          # pythonpath 含父级 uav_search_track_car
    ├── test_bbox_to_gimbal.py
    ├── test_yolo_controller.py
    ├── test_yolo_vision.py
    └── fixtures/
        ├── mock_redis.py
        └── mock_frame.py
```

## 关键配置（`config/algorithm.yaml`）

```yaml
controller: yolotrack.yolo_controller:YoloSearchTrackController

# 5 个原 FSM 参数（与 uav_search_track_car 一致）
mode: spiral
search_radius: 500.0
search_altitude_agl: 300.0
sweep_period: 4.0
loiter_radius: 200.0

# YOLO 子模块
yolo:
  enabled: true
  model_path: target_vehicle_yolov8s.pt  # 默认软链到 yolo_car 仓
  imgsz: 1024                            # 必须与训练尺寸一致
  conf: 0.25
  camera_hfov_deg: 60.0                  # 与 gimbal_tracking.fov 对齐
  camera_vfov_deg: 45.0
  redis_host: 127.0.0.1
  redis_port: 6379
  uav_id: "10002"
  frame_max_age_ms: 200                  # 超过此时间视为"无 yolo"，fallback
```

## 关键设计决定

### 1. 继承而非修改
`YoloSearchTrackController` 继承 `FsmSearchTrackController`，**不动父类**。SEARCH/状态机/滞回逻辑全部复用，只覆盖 `_track_commands` 加 yolo 决策。

### 2. 软链到 yolo_car 仓
`target_vehicle_yolov8s.pt` 是软链，指向 `/home/lpwang/YOLO/yolo_car/...`。重新训练后 `best.pt` 更新，软链自动生效。

### 3. 依赖独立
`requirements.txt` 独立，不污染 opensim 根。

### 4. 不污染 opensim 仓
- 没动 `examples/_common/*`
- 没动 `examples/uav_search_track_car/*`
- 没动 C++ 引擎或 UE 渲染器

### 5. Fallback 机制
YOLO 失效（无帧/超龄/目标出视野）→ fallback 到原几何 LOS。系统**不会因 yolo 故障而崩**。

## 测试

```bash
cd examples/yolotrack
python -m pytest tests/ -v
# 25 passed in 2.7s
```

覆盖：
- `bbox_to_gimbal.py`：11 个用例（中心、四角、边、fov、异常）
- `yolo_controller.py`：6 个用例（yolo 禁用/启用/fresh/stale/出视野/lifecycle）
- `yolo_vision.py`：4 个用例（无数据、合成帧、重复 frame、frame 推进）

## 故障排查

| 现象 | 可能原因 | 解决 |
|------|---------|------|
| `ModuleNotFoundError: yolotrack` | 当前目录不在 example 根 | `cd examples/yolotrack` |
| `ModuleNotFoundError: search_track` | 没把 uav_search_track_car 加 sys.path | run.py 已加，**确认在 opensim 根跑** `python -m examples.yolotrack.run` |
| `AttributeError: 'dict' object has no attribute 'controller'` | 用了 from_yaml（返回 AlgorithmConfig） | run.py 已改用 yaml.safe_load 返回 dict |
| YOLO 模型加载失败 | 软链断了 | `ls -la target_vehicle_yolov8s.pt`，检查指向 |
| YoloVisionWorker 启动后无输出 | redis 不可达 或 UE 端没推 spec/022 帧 | `redis-cli ping`；检查 UE renderer 端 ImageCapture 配置 |
| 跟踪模式云台"乱转" | camera_hfov_deg/vfov_deg 与 gimbal fov 不匹配 | 调整 `algorithm.yaml` 的 `yolo.camera_*_deg` |
| 训练数据换成新场景 | 模型类别/尺寸变了 | 重训 YOLO 模型 + 更新 `algorithm.yaml` 的 `imgsz/conf/classes` |

## 性能基线

- YoloVisionWorker 启动后约 1-2 秒（模型首次加载）
- 单帧推理：~30ms (RTX 4090, imgsz=1024)
- 轮询频率：20Hz（与 UE 端 30Hz 帧率匹配，且不浪费 CPU）
- 主控制环频率：10Hz（cfg.advanced.control_rate_hz）

## 依赖

```bash
# 最小依赖
pip install ultralytics>=8.4 lap>=0.5.12 opencv-python>=4.8 \
            redis>=5.0 numpy>=1.24
```

注：`ultralytics` 已含 `redis` 等部分依赖，但为清晰起见列出全部。
