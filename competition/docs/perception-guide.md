# 感知层使用指南（spec 029）

本文面向参赛选手，讲解 OpenSim 竞赛 SDK 的**感知-决策分离架构**：你可以在传统的"纯控制决策"之外，选择"自研识别 + 决策"或"端到端一体算法"两种学术范式参赛。

阅读前置：建议先看 [参赛手册](参赛手册.md) 与 [API 参考](api-reference.md)。本文专注于 `sensor()` / `decide()` 双回调、`obs.self.detection` 的来源、以及训练/验证两种运行模式的差异。

---

## 1. 概述：sensor / decide 双回调

每架己方无人机在主循环里按固定节拍（约 10 Hz）跑一遍完整流程。从"拉状态"到"发命令"，runner 在中间插入了**两个回调调用点**，夹着一个"识别层"：

```
┌─ runner 每帧主循环（per entity）──────────────────────────────────┐
│  1. poll sim:state        → WorldState（全真值，选手不可见）        │
│  2. photo_cache.get(uid)  → 最新 PNG bytes（后台线程拉取，auto 模式默认开启） │
│  3. build_obs(ws, uid)    → obs_base                                │
│     └─ obs_base.self.detection 此刻是引擎几何值（内部真值源）        │
│  4. 注入 photo → obs_with_photo                                     │
│  5. ★ 识别层（由 sensor 返回值决定来源，见 §3）：                    │
│     ├─ 选手 sensor 返回 List[Detection] → 用选手结果                 │
│     ├─ 选手 sensor 返回 SKIP_DETECTION → 跳过识别（端到端）          │
│     └─ 未覆盖 / 返回 None            → 进默认识别器                  │
│           run_mode=train → AccuracySimulator（按 accuracy 概率采样） │
│           run_mode=eval  → YoloDetector（真实 YOLOv8）              │
│  6. 用最终结果覆盖 obs.self.detection → obs'                        │
│  7. ★ agent.decide(obs', dt) → List[Command]                       │
│  8. publish commands                                                │
└────────────────────────────────────────────────────────────────────┘
```

**关键性质**：
- `obs.self.detection` 对 `decide()` 而言统一是"识别层产出，**不保证真值**"。引擎几何检测不再直接给选手——它只在 SDK 内部作为默认识别器的真值源（训练态）或被绕过（验证态/自研）。
- 三种来源对 `decide` **完全透明**：你拿到的 `obs'` 里 `detection` 已填好，无需关心它来自哪里。
- 端到端选手（返回 `SKIP_DETECTION`）在任何模式下都**不跑默认识别器**，`obs.self.photo` 直接进 `decide` 供你用。

### 正交的两个维度

对选手而言只有一种选择：**是否提供自研 `sensor`**。`run_mode` 是平台方的内部行为，选手不感知。

| 维度 | 取值 | 谁决定 | 含义 |
|---|---|---|---|
| `detection_source` | `user` / `default` / `end_to_end` | 自动（由 `sensor()` 返回值推断） | 选手是否自研识别 |
| `run_mode` | `train` / `eval` | 平台方 CLI `--mode` | 默认识别器内部实现（模拟 / YOLO） |

---

## 2. 三种选手范式

完整可运行示例见 [`competition/templates/search_track_perception_template.py`](../templates/search_track_perception_template.py)。下面分三种范式展开。

### 范式 A — 自研识别 + 决策（分层）

你实现 `sensor()` 用 `obs.self.photo` 跑自己的识别模型，返回 `List[Detection]`；`decide()` 读 `obs.self.detection`（即你自己的产出）做控制。**任何 `run_mode` 下默认识别器都不会启动**——平台尊重你的自研结果。

```python
from typing import List
from competition.sdk.core.agent import Agent
from competition.sdk.core.commands import Command, fly_to, point_gimbal
from competition.sdk.core.observation import Detection, Observation


class MyPerceptionAgent(Agent):
    def configure(self, config):
        # self._model = load_my_yolo_or_cnn(config["model_path"])
        ...

    def sensor(self, obs: Observation, dt: float):
        # 用本帧相机画面做识别（photo 需 --photo 启用 PhotoCache）
        photo = obs.self.photo
        if photo is None:
            return None              # 无画面 → 回退默认识别器
        boxes = self._model.detect(photo)   # 你的识别算法
        return [Detection(detected=True,
                          confidence=b.conf,
                          target_lat=b.lat,
                          target_lon=b.lon) for b in boxes]

    def decide(self, obs: Observation, dt: float) -> List[Command]:
        det = obs.self.detection           # ← 你 sensor 的产出
        if det.detected and det.target_lat is not None:
            return [fly_to(det.target_lat, det.target_lon),
                    point_gimbal(0.0, -45.0)]
        return [point_gimbal(0.0, -45.0)]
```

### 范式 B — 端到端（一体算法）

你让 `sensor()` 返回 `SKIP_DETECTION` 哨兵，平台**跳过识别层**；`decide()` 直接读 `obs.self.photo`，自己端到端推理出控制命令（不经过 `Detection` 中间表示）。

```python
from competition.sdk.core.observation import SKIP_DETECTION


class MyEndToEndAgent(Agent):
    def configure(self, config):
        # self._policy = load_end_to_end_policy(...)
        ...

    def sensor(self, obs: Observation, dt: float):
        return SKIP_DETECTION          # 端到端：不需要 detection

    def decide(self, obs: Observation, dt: float) -> List[Command]:
        photo = obs.self.photo         # PNG bytes（默认 photo_mode=auto 自动注入）
        if photo is None:
            return []                  # 无画面 → 本帧不动
        action = self._policy(photo)   # 端到端：图片 → 控制指令
        return [action.to_command()]
```

### 范式 C — 纯控制（用默认识别）

你**不实现 `sensor`**（或让它返回 `None`），`decide()` 读 `obs.self.detection`（默认识别器产出）。这与改造前的赛题一行为一致，三个 baseline 都属于这一类。

```python
class MyControlAgent(Agent):
    # 不写 sensor() → 默认识别器接管
    def decide(self, obs: Observation, dt: float) -> List[Command]:
        det = obs.self.detection       # ← 默认识别器产出
        if det.detected:
            return [fly_to(det.target_lat, det.target_lon)]
        return []
```

可参考 [`competition/baselines/search_track_fsm.py`](../baselines/search_track_fsm.py)（SEARCH↔TRACK 状态机，纯控制范式）。

---

## 3. `sensor()` 返回值说明

`sensor(obs, dt)` 是**可选覆盖**的方法。未覆盖时基类返回 `None`。返回值有三种主态（加一个空列表特例）：

| 返回值 | 含义 | `obs.self.detection` 填什么 | 默认识别器是否运行 |
|---|---|---|---|
| `List[Detection]`（非空） | 自研识别结果 | 你的第一个 detection | **否** |
| `[]`（空列表，特例） | 选手明确"本帧无检测" | `Detection(detected=False)` | **否**（`detection_source=user`） |
| `SKIP_DETECTION` | 端到端，不要 detection | `Detection(detected=False)` | **否**（避免白跑） |
| `None` / 未覆盖 | 用默认识别器 | 默认识别器产出 | **是** |

`SKIP_DETECTION` 是 `competition.sdk.core.observation` 里的模块级哨兵单例，用于显式区分"端到端跳过识别"与"用默认识别"。

> **容错**：若 `sensor()` 抛异常，runner 自动回退到默认识别器（不崩帧、不中断比赛）。

---

## 4. `detection` 的语义

```python
@dataclass(frozen=True)
class Detection:
    detected: bool
    confidence: float                  # [0, 1]
    target_lat: Optional[float] = None
    target_lon: Optional[float] = None
    azimuth_error_deg: Optional[float] = None
    target_type: str = ""              # "ground_vehicle" | "decoy_vehicle" | ""
```

**关键语义**：
- `target_lat/target_lon` 是**识别出的位置，不保证真值**。可能含噪声（训练态 `AccuracySimulator`）或来自真实 YOLO（验证态）。不要把它当成目标精确坐标。
- `confidence` 含义随来源略有不同：默认识别器给出 [0,1] 偏移置信度；自研/YOLO 来源是模型置信度。
- **诱饵伪装**：相机可能把诱饵识别成 `ground_vehicle`（你无法仅凭 `target_type` 区分）。诱饵是静止的、真目标会动——建议用**多帧位置变化**做运动学判别。
- 诱饵误识别概率由引擎层处理，识别器不再额外注入。

---

## 5. `accuracy` 参数与训练态 `AccuracySimulator`

训练态（`--mode train`）的默认识别器是 `AccuracySimulator`：它读引擎几何真值，按 `accuracy` 概率**伯努利检出**，命中时位置加高斯噪声。用于在**没有真实 YOLO 模型**时模拟一个"有误差的相机"，方便你开发与调试决策算法。

| 参数 | 含义 |
|---|---|
| `--accuracy <p>` | 检出概率，∈ [0, 1]。每帧每个真目标独立掷骰；未命中 → `Detection(detected=False)` |
| `--noise-sigma <m>` | 命中时 `target_lat/lon` 的高斯噪声标准差（米），换算成经纬度偏移 |

命中时 `confidence ∈ [accuracy*0.8, accuracy]` 随机。

**退化等价**：`--accuracy 1.0 --noise-sigma 0` → 输出与引擎几何真值逐字段相等（如需等价真值基线可用此配置）。

**默认值**（spec 032）：`accuracy=0.85, noise_sigma_m=50.0`，提供真实识别误差激励感知层算法迭代。

---

## 6. 训练 / 验证模式

由 CLI `--mode` 控制，只影响**默认识别器**（你自研 `sensor` 时无差别）：

| `--mode` | 默认识别器 | 何时用 |
|---|---|---|
| `train`（默认） | `AccuracySimulator`（纯 Python，概率采样） | 开发调试、CI、无 GPU/无 YOLO 环境 |
| `eval` | `YoloDetector`（真实 YOLOv8，动态 import 复用 `examples/yolotrack`） | 正式评测、最终验证 |

### `YoloDetector` 与 yolotrack 的关系（验证态）

`YoloDetector` 不复制 yolotrack 代码，而是在 `start()` 时通过 `sys.path + import` 加载 `examples/yolotrack/yolotrack/yolo_vision.py` 的 `YoloVisionWorker`（已跑通的 YOLOv8 推理通路）。SDK 侧仅新增 bbox→lat/lon 反算（`pan_tilt_to_latlon`），把视觉检测结果填进 `Detection`。

- **`ultralytics` / `opencv` 是 yolotrack 的独立依赖**（见 `examples/yolotrack/requirements.txt`），不进 opensim 根。SDK 核心**不耦合**这些库——`YoloDetector` 的 import 延迟到方法内，未装依赖时仅 `YoloDetector` 不可用，其余功能正常。
- **不复用** `yolo_controller.py`（FSM 控制决策）——那是选手 `decide()` 的职责。详见 [yolotrack README](../../examples/yolotrack/README.md) 顶部的 spec 029 说明。

---

## 7. CLI 用法

三个赛题共用同一套感知层参数（spec 032）。默认 `accuracy=0.85, noise_sigma_m=50.0, photo_mode=auto`。

```bash
# 任意赛题 · 训练态（默认）：AccuracySimulator 模拟检出概率 0.85、噪声 50m
# photo_mode=auto 默认开启：带 UE 的环境自动把相机 PNG 帧注入 obs.self.photo
python -m competition run --scenario search_track \
    --agent my_pkg:MyAgent --mode train --duration 600

# 任意赛题 · 验证态：真实 YOLOv8（需先 pip install -r examples/yolotrack/requirements.txt）
python -m competition run --scenario search_track \
    --agent my_pkg:MyAgent --mode eval \
    --photo-mode on --yolo-model target_vehicle_yolov8s.pt --duration 600

# 任意赛题 · 纯控制（等价真值基线，accuracy=1.0 无误差）
python -m competition run --scenario search_track \
    --agent baselines.search_track_fsm:FsmAgent \
    --accuracy 1.0 --noise-sigma 0 --duration 600

# 赛题二/三（默认：accuracy=0.85, noise=50，与赛题一同一套参数）
python -m competition run --scenario coop_decoy \
    --agent baselines.coop_distributed:CoopDistributedAgent --duration 600
python -m competition run --scenario adversarial_swarm \
    --agent baselines.swarm_distributed:SwarmDistributedAgent --duration 600

# 赛题二/三 · 训练态（AccuracySimulator 概率检出）
python -m competition run --scenario coop_decoy \
    --agent baselines.coop_distributed:CoopDistributedAgent \
    --mode train --accuracy 0.85 --noise-sigma 50 --duration 600

# 赛题二/三 · 验证态（YoloDetector，需 photo_mode=on + UE 渲染器）
python -m competition run --scenario coop_decoy \
    --agent baselines.coop_distributed:CoopDistributedAgent \
    --mode eval --photo-mode on --yolo-model models/target_vehicle_yolov8s.pt --duration 600
```

### 感知相关参数（三个赛题通用，spec 032）

| 参数 | 默认 | 说明 |
|---|---|---|
| `--mode {train,eval}` | `train` | 默认识别器实现 |
| `--photo-mode {auto,on,off}` | `auto` | 相机帧拉取模式（见下表）；`auto`=非 dry_run 自动拉取 UE 渲染的 PNG 相机帧注入 `obs.self.photo`，无帧则 None |
| `--photo` | — | （废弃别名）等价于 `--photo-mode on`，保留向后兼容 |
| `--no-photo` | — | 等价于 `--photo-mode off`，显式关闭相机帧拉取 |
| `--accuracy <p>` | `0.85` | `AccuracySimulator` 检出概率（train 模式） |
| `--noise-sigma <m>` | `50.0` | `AccuracySimulator` 位置噪声标准差，米（train 模式） |
| `--yolo-model <path>` | `""` | `YoloDetector` 模型路径（eval 模式） |

**`--photo-mode` 三态语义**：

| 模式 | dry_run | 行为 |
|---|---|---|
| `auto`（默认） | True | 不启动 PhotoCache（无 UE） |
| `auto`（默认） | False | 启动 PhotoCache；Redis 有帧→注入 `obs.self.photo`，无帧→None（安全降级，不报错） |
| `on` | True | 不启动 |
| `on` | False | 启动（同 auto 非 dry_run） |
| `off` | 任意 | 不启动，`obs.self.photo` 恒 None |

> **`photo_mode` 与 `decide()` 纯度**：`decide()` 内禁止访问 Redis/网络/文件。`photo` 是 runner **预先注入**到 `obs.self.photo` 的合法数据，读取它不违反隔离契约。

---

## 8. `photo` 字段

```python
obs.self.photo: Optional[bytes]   # PNG bytes | None
```

- 内容：UE 渲染端最新一帧相机画面，**PNG 编码**的 bytes（spec 022 `sync_camera:{uid}:frame:{n}` Redis hash，magic bytes `89 50 4E 47`）。请按 PNG 解码，不要按 JPEG 解析。
- 来源：runner 后台线程 ~30 Hz 轮询拉取、缓存最新帧，主循环按 `uid` 取——**不阻塞决策节拍**。
- 默认 `photo_mode=auto`：带 UE 的标准环境启动后，UE 一旦向 Redis 写入该机帧即自动注入；**无需任何额外开关或参数**。
- 为 `None` 的情形：`dry_run` 模式、`photo_mode=off`、UE 未给本机 assign 渲染、Redis 暂无该机帧。
- **解码示例**（PNG）：
  ```python
  import numpy as np, cv2
  img = cv2.imdecode(np.frombuffer(obs.self.photo, np.uint8), cv2.IMREAD_COLOR)
  # 或 PIL：
  from PIL import Image; import io
  img = Image.open(io.BytesIO(obs.self.photo))
  ```

`SelfView` 还预留了 `detections: Tuple[Detection, ...] = ()`（复数）字段，供将来视觉通路多 bbox 使用。赛题一单目标只用 `detection`（单数），`detections` 保持空 tuple。

---

## 9. 赛题落地范围

| 赛题 | photo | 默认 detection 来源 | run_mode | 说明 |
|---|---|---|---|---|
| `search_track`（赛题一） | ✅ PNG 入 `obs.self.photo`（默认 auto 开启） | `default` | `train` / `eval` | 完整落地新通路 |
| `coop_decoy`（赛题二） | ✅ PNG 入 `obs.self.photo`（默认 auto 开启，需多 GPU 渲染池） | `default`（`accuracy=0.85`） | `train` / `eval` | spec 032：与赛题一完全对齐 |
| `adversarial_swarm`（赛题三） | ✅ PNG 入 `obs.self.photo`（默认 auto 开启，需多 GPU 渲染池） | `default`（`accuracy=0.85`） | `train` / `eval` | 同上 |

**三个赛题共用同一套感知-决策架构**（spec 029 + spec 032）。均支持三种范式（自研 / 端到端 / 纯控制）× 两种模式（train / eval），CLI 参数和 `algorithm.yaml` 配置完全一致。

**赛题二/三渲染门控**：UE 渲染器一次只 assign 一架飞机出 photo。多机场景（赛题二 3 机 / 赛题三 10 机）中，无 photo 的 UAV 自动降级到 `AccuracySimulator`（不崩帧），并打一次性 warning。选手自研 `sensor()` 不受门控影响。多机渲染池（全部 UAV 都有 photo）待实现，见 `specs/032-perception-unify-task23/contracts/render-pool.md`。

---

## 参考

- [参赛手册](参赛手册.md)
- [API 参考](api-reference.md)
- [感知-决策模板](../templates/search_track_perception_template.py)（三种范式可运行示例）
- [赛题一 baseline](../baselines/search_track_fsm.py)（纯控制范式参考）
- [yolotrack 示例](../../examples/yolotrack/)（验证态 YOLO 检测的唯一真相源）
- [spec 029](../../specs/029-perception-decision-sdk/spec.md)（完整设计规格）
