# 参赛 SDK 接口参考

本文件是参赛 SDK 的**唯一权威参考**，涵盖 Agent 基类、感知-决策双层架构、数据结构、控制命令与全部 CLI 参数。

> 本文档末尾 [§6 架构概览与快速开始](#6-架构概览与快速开始) 给出全景数据流图与最小可运行骨架，建议在通读各节后回看该节建立整体认识。

---

## 1. Agent 基类与生命周期

`competition.sdk.core.agent.Agent`。参赛者继承此类并实现下列方法，runner 负责按生命周期调用。

```python
from competition.sdk.core.agent import Agent
from competition.sdk.core.observation import Observation, Detection, SKIP_DETECTION
from competition.sdk.core.commands import Command, fly_to, point_gimbal
```

### 1.1 方法表

| 方法 | 是否必须 | 调用时机 | 作用 |
|---|---|---|---|
| `__init__(self, my_uid: str)` | 必须 | 启动一次，每个受控实体各一次 | 保存 `self.my_uid`（受控实体 unique_id） |
| `configure(self, config)` | 可选 | 运行前一次 | 读取注入的静态任务/算法参数，整局不变 |
| `reset(self)` | 可选 | 每局首个 `decide()` 前 | 清空内部状态 |
| `sensor(self, obs, dt) -> Optional[list[Detection]]` | 可选 | 每个决策周期，在 `decide()` 前 | 自研感知；不覆盖则走默认识别器（详见 §2） |
| `decide(self, obs, dt: float) -> list[Command]` | 必须 | 每个决策周期，约 10 Hz | 解析 `obs`，返回命令列表，顺序敏感 |

### 1.2 不变量

1. `decide()` 除 `self` 内部状态外为纯函数，不直接读写 Redis、文件或网络。
2. 返回的命令列表按序发布，顺序敏感。
3. 命令只作用于 `self.my_uid`；runner 发布时强制注入 `unique_id=my_uid`，无法越权控制其他实体。
4. 返回空列表 `[]` 表示本周期不发命令。
5. 赛题三中本机被击毁（`obs.self.status == "destroyed"`）后 runner 自动停止调用 `decide()`。

### 1.3 `obs` 的数据来源与隔离（重要）

每周期 runner 调 `build_obs()` 将引擎全真值状态投影为**只含本机信息**的 `Observation`：

```python
obs.self        # SelfView          本机可物理感知的自身状态
obs.comm_inbox  # tuple[Message,...] 本周期收到的队友消息（仅 sender uid + payload）
obs.briefing    # MissionBriefing   赛前静态简报，整局不变（score_view 每拍更新除外）
```

**隔离承诺**：obs 只读「本机自身」的实体数据，**绝不包含**其他实体的姿态/真值/检测。队友位置仅能经队友主动发消息（`comm_inbox`）告知；目标位置仅能经相机检测（`obs.self.detection`，且不保证真值）获得。此为数据层的物理隔离——禁用字段在 obs 中不存在，而非访问控制。

若干易被误判为「上帝信息」的字段，澄清如下：

| 字段 | 是否上帝信息 | 说明 |
|---|---|---|
| `obs.self.status` | **否** | 本机**自身**的生存状态（`"active"` / `"destroyed"`），即本机是否存活，属合法自身感知（与 `jammed`、`comm_stats` 同类），**不含任何他人信息**。被击毁后 runner 停调 `decide()` |
| `obs.self.jammed` | 否 | 本机自身是否正被通信干扰（赛题三），感知动态威胁的唯一合法入口 |
| `obs.self.comm_stats` | 否 | 本机自身通信收发统计，间接感知干扰 |
| `obs.self.detection` | 否 | 本机相机的检测结果，但**位置含噪声、会被诱饵骗**，不是真值（见下） |

### 1.4 `obs.self.detection` 的产生链路（重要）

此为理解整个感知-决策架构的关键。`detection` **并非引擎自动下发的真值**，而是经以下链路产生：

```
1. 引擎全真值状态（含目标精确坐标）—— 选手不可见
2. build_obs() 投影时，detection 槽位先填空占位 Detection(detected=False)
   （引擎几何真值绝不直接进 obs，防止选手在识别前即取得真值坐标）
3. runner 调 sensor()（可选实现），按其返回值决定 detection 来源：
   ├─ 不实现/返回 None → runner 自动跑「默认识别器」产出 detection
   ├─ 返回 list[Detection] → 采用自研结果
   └─ 返回 SKIP_DETECTION → 跳过识别（端到端，detection 保持空）
4. runner 将识别层产出填入 obs.self.detection → 传给 decide()
```

**结论**：`decide()` 所见 `obs.self.detection` 为**识别层产出**（默认识别器 / 自研 / 跳过），**不保证真值**，可能含噪声、漏检或被诱饵骗。引擎不绕过感知层直接向 decide 下发真值。sensor 的实现方式（§2）决定 detection 的最终内容。

---

## 2. sensor 层（感知）

sensor 层在每周期 `decide()` 之前运行，负责产出 `obs.self.detection`。**该层为可选**——不实现时由平台默认识别器代为检测。

### 2.1 三种选择总览

sensor 层的核心决策为以下三选其一（通过 `sensor()` 的实现方式选择）：

| 选择 | 选择方式 | 需实现内容 | 效果 | `obs.self.detection` 填什么 | 默认识别器是否运行 |
|---|---|---|---|---|---|
| **① 用默认识别器** | 不实现 `sensor`，或返回 `None` | **无需实现任何感知代码**（最简单） | 平台按 `accuracy`/`noise` 模拟有误差的相机 | 默认识别器产出 | 是 |
| **② 自研识别** | 覆盖 `sensor()`，返回非空 `list[Detection]` | 实现识别算法（读 `obs.self.photo` → 输出检测结果） | 采用自研识别模型，精度自控 | 自研结果（`list[0]` 为主检测） | 否 |
| **③ 端到端** | 覆盖 `sensor()`，返回 `SKIP_DETECTION` | 实现端到端策略（图片 → 控制命令，不经 Detection） | 完全跳过识别层，`decide` 直接处理图像 | `Detection(detected=False)`（占位） | 否 |

> **选型指引**：
> - **初始开发或仅关注决策算法** → 选 ①，无需实现 `sensor()`，仅实现 `decide()` 即可。
> - **具备自研视觉模型、需更高识别精度** → 选 ②，在 `sensor()` 中运行该模型。
> - **采用端到端强化学习/一体策略、无需中间 Detection 表示** → 选 ③。

无论选择何种，`decide()` 拿到的 `obs.self.detection` 均已由 runner 填好（选 ③ 时为空占位，改为读 `obs.self.photo`）。三种来源对 decide 透明。

### 2.2 运行模式：train / eval

模式由 CLI `--mode` 控制，仅影响「默认识别器」的实现（选 ②③ 自研时无差别）：

| 模式 | 默认识别器 | 适用 |
|---|---|---|
| `train`（默认，**选手开发用**） | `AccuracySimulator`（纯 Python，按 `accuracy` 概率采样 + 高斯噪声） | 本地开发、调试、CI，无需 GPU/无 YOLO 环境 |
| `eval`（**官方评测采用**） | `YoloDetector`（YOLOv8 识别 UE 渲染图） | 官方评测；选手本地开发用 train 即可，无需手动切换 |

> **选手本地开发使用 train 模式即可**。`eval` 为官方评测采用的模式，选手在本地开发调试时保持默认 `train` 即可完成全部算法实现；若需在本地验证自研识别（选 ②）在真实 YOLOv8 通路下的效果，可显式 `--mode eval`（需自备 YOLO 权重与 UE 渲染端），但此非参赛必需步骤。

### 2.3 选择 ①：默认识别器（调 `accuracy` 和 `noise_sigma_m`）

train 模式下最常用，**无需任何代码**。经 CLI 设定两个参数，AccuracySimulator 据此模拟「有误差的相机」：

| 参数 | CLI | 默认 | 含义 |
|---|---|---|---|
| `accuracy` | `--accuracy <p>` | 0.85 | 检出概率 ∈ [0,1]。每帧每个真目标独立伯努利；未命中 → `Detection(detected=False)`。**上界钳 0.9** |
| `noise_sigma_m` | `--noise-sigma <m>` | 50.0 | 命中时位置高斯噪声标准差（米），换算成经纬度偏移。**下界钳 30 m** |

AccuracySimulator 命中时：`confidence ∈ [eff_acc*0.8, eff_acc]` 随机；未命中或无真目标 → `Detection(detected=False)`。

**天气衰减**（AccuracySimulator 按场景 `weather.type` 施加乘性衰减，effective = base × factor）：

| 天气 `type` | accuracy_factor | noise_factor |
|---|---|---|
| `Clear_Skies` | 1.0 | 1.0 |
| `Partly_Cloudy` | 0.97 | 1.1 |
| `Rain` | 0.88 | 1.4 |
| `Foggy` | 0.80 | 1.7 |
| `Snow_Light` | 0.85 | 1.5 |
| `Sand_Dust_Calm` | 0.78 | 1.8 |

> **退化等价**：`--accuracy 1.0 --noise-sigma 0` → 输出与引擎几何真值逐字段相等（如需等价真值基线可用此配置）。默认值有意设为 `0.85 / 50`（spec 032），以提供真实识别误差激励感知层算法迭代。

### 距离门限（2026-08-26）

`AccuracySimulator` 支持距离相关检出建模（train 主路径 + eval 无 photo
降级 fallback 均生效；YOLO 主路径不受影响）：

- `max_detection_range_m`（默认 0 = 禁用）：本机与目标水平距离达到/超过此值必检不出
- `full_accuracy_range_m`（默认 0）：以内不做距离衰减；此后 accuracy 线性降至
  `max_detection_range_m` 处 0

配置入口（优先级从高到低）：

1. CLI：`--max-detection-range` / `--full-accuracy-range`（米）
2. scenario.json 顶层 `perception` 块（与 `weather` 同级）：

   ```json
   "perception": {
     "max_detection_range_m": 3000,
     "full_accuracy_range_m": 1000
   }
   ```

3. 缺省 0（禁用，与历史行为一致）

引擎侧另有独立的几何硬门限 `gimbal_tracking.max_detection_range`
（`config/defaults.json`，发行默认 3000；实体级 `gimbal_tracking.params`
可 override），作用于几何真值层：斜距超出（`>` 判定，边界值含在内可检出）
一律 `detected=false`，**含诱饵误检（misid）掷骰——超距连误检都不发生**。
两层边界约定略有差异（C++ 用斜距且边界可检出；Python 用水平距离且边界
必检不出），属设计内的近似（UAV 高度 ≤500m 时两者差 <0.2%，当前三赛题高度即此档）。两层建议
配同值。诱饵误检率 `misid_prob`（默认 0.5）同样在引擎侧这两个入口配置。
选手侧（agent API）不可设置以上任何参数。

对既有赛题的量化影响（启用 3000/1000 后）：三个赛题初始 UAV-目标距离
均 ≤3.7km（满精度区内），门限主要影响远距搜索阶段的检出统计；天气衰减
照旧叠加（如 Rain：accuracy ×0.88、noise ×1.4）。

### 2.4 选择 ②：自研识别算法

覆盖 `sensor()`，读取 `obs.self.photo` 运行自研识别模型，返回非空 `list[Detection]`。**任何模式下默认识别器均不启动**——平台采用自研结果。

骨架伪代码（占位逻辑：将图像中心当作目标方位反算 lat/lon；**实际应替换为自研识别算法**）：

```python
import numpy as np, cv2
from typing import List, Optional
from competition.sdk.core.agent import Agent
from competition.sdk.core.observation import Detection, Observation
from competition.sdk.core.perception.bbox_to_latlon import pan_tilt_to_latlon
from competition.sdk.core.commands import Command, fly_to, point_gimbal


class MyPerceptionAgent(Agent):
    def configure(self, config):
        # 加载自研识别模型（YOLO/CNN/...）
        # self._model = load_my_model(config["model_path"])
        ...

    def sensor(self, obs: Observation, dt: float) -> Optional[List[Detection]]:
        photo = obs.self.photo
        if photo is None:
            return None                       # 无画面 → 回退默认识别器
        img = cv2.imdecode(np.frombuffer(photo, np.uint8), cv2.IMREAD_COLOR)
        # ↓↓↓ 占位：替换为自研识别算法，产出 pan_delta/tilt_delta/conf ↓↓↓
        pan_delta, tilt_delta, conf = 0.0, 0.0, 0.9   # 图像中心 = 光轴方向 = delta 0
        # 反算目标经纬度（pan_tilt_to_latlon: 本机姿 + 光轴偏移 → 地面经纬度）
        tlat, tlon = pan_tilt_to_latlon(
            uav_lat=obs.self.lat, uav_lon=obs.self.lon, uav_alt=obs.self.alt,
            gimbal_pan=obs.self.gimbal_pan, gimbal_tilt=obs.self.gimbal_tilt,
            pan_delta=pan_delta, tilt_delta=tilt_delta)
        return [Detection(detected=True, confidence=conf,
                          target_lat=tlat, target_lon=tlon)]

    def decide(self, obs: Observation, dt: float) -> List[Command]:
        det = obs.self.detection              # ← sensor 的产出
        if det.detected and det.target_lat is not None:
            return [fly_to(det.target_lat, det.target_lon),
                    point_gimbal(0.0, -45.0)]
        return [point_gimbal(0.0, -45.0)]
```

要点：
- 接口契约：返回 `list[Detection]`；`detections[0]` 作为 `obs.self.detection` 主检测传入 `decide`。
- **容错**：`sensor()` 抛异常 → 自动回退默认识别器（不崩帧、不中断比赛）。
- 若需实现完整 detector 而非回调：可参考 `competition.sdk.core.perception.base.BaseDetector`（抽象方法 `detect(obs, dt, truth_source=None)` + 可选 `start()/stop()`），但通常覆盖 `sensor()` 回调即可。

### 2.5 选择 ③：端到端决策（返回 `SKIP_DETECTION`）

`sensor()` 返回 `SKIP_DETECTION` 哨兵，平台**跳过识别层**；`decide()` 直接读 `obs.self.photo`，端到端推理出控制命令（不经过 `Detection` 中间表示）。

```python
from competition.sdk.core.observation import SKIP_DETECTION


class MyEndToEndAgent(Agent):
    def configure(self, config):
        # self._policy = load_end_to_end_policy(...)
        ...

    def sensor(self, obs, dt):
        return SKIP_DETECTION                 # 端到端：不需要 detection，跳过识别层

    def decide(self, obs, dt):
        photo = obs.self.photo                # PNG bytes（photo_mode=auto 默认注入）
        if photo is None:
            return []                         # 无画面 → 本帧不动
        action = self._policy(photo)          # 端到端策略：图片 → 控制指令
        return [action.to_command()]
```

### 2.6 `sensor()` 返回值四态语义

| 返回值 | 含义 | `obs.self.detection` 填什么 | 默认识别器是否运行 |
|---|---|---|---|
| `None` 或未覆盖 | 用默认识别器 | 默认识别器产出 | **是** |
| 非空 `list[Detection]` | 自研识别结果 | 自研结果的第一个 detection | 否 |
| 空列表 `[]`（特例） | 明确本帧无检测 | `Detection(detected=False)` | 否 |
| `SKIP_DETECTION` | 端到端，跳过识别层 | `Detection(detected=False)` | 否（避免白跑） |

`SKIP_DETECTION` 是 `competition.sdk.core.observation` 中的模块级哨兵单例，用于显式区分「端到端跳过识别」与「用默认识别」。

### 2.7 渲染门控降级（官方评测多机场景）

spec 032 渲染门控：官方评测（eval）时，当走默认识别器（sensor 返回 `None`）且该 UAV 无 photo，`YoloDetector` 无法工作，自动降级到 `AccuracySimulator`（不崩帧），并打 per-uid 一次性 warning：

```
[perception] UAV <uid> has no photo frame, falling back to AccuracySimulator (spec 032 render gate)
```

仅影响「走默认识别器」的情形；自研 `sensor()` 不受门控影响。多机渲染池（全部 UAV 都有 photo）待实现（见 `specs/032-perception-unify-task23/contracts/render-pool.md`）。

### 2.8 `photo` 字段

```python
obs.self.photo: Optional[bytes]   # PNG bytes | None
```

- 内容：UE 渲染端最新一帧相机画面，**PNG 编码**的 bytes（magic bytes `89 50 4E 47`）。须按 PNG 解码，勿按 JPEG 解析。
- 来源：runner 后台线程轮询拉取、缓存最新帧，主循环按 `uid` 取——**不阻塞决策节拍**。
- 默认 `photo_mode=auto`：带 UE 的标准环境启动后自动注入，无需额外开关。
- 为 `None` 的情形：`dry_run`、`photo_mode=off`、UE 未给本机 assign 渲染、Redis 暂无该机帧。
- **`photo` 与 `decide()` 纯度**：`decide()` 内禁止访问 Redis/网络/文件。`photo` 是 runner **预先注入**至 `obs.self.photo` 的合法数据，读取它不违反隔离契约。

解码示例：
```python
import numpy as np, cv2
img = cv2.imdecode(np.frombuffer(obs.self.photo, np.uint8), cv2.IMREAD_COLOR)
# 或 PIL：
from PIL import Image; import io
img = Image.open(io.BytesIO(obs.self.photo))
```
使用前须判空。`SelfView` 另预留 `detections: Tuple[Detection, ...] = ()`（复数）字段供将来多 bbox 使用，赛题一单目标仅用 `detection`（单数）。

---

## 3. decide 层（决策）

### 3.1 接口签名

```python
def decide(self, obs: Observation, dt: float) -> List[Command]
```

- `obs`：本周期观测（三顶层字段固定，见 3.2）。`obs.self.detection` 已由 sensor 层填好。
- `dt`：距上次 `decide()` 的秒数（控制周期 `1/control_rate_hz`，默认 0.1 s）。
- 返回：命令列表，顺序敏感、按序发布；空列表 = 本周期不发命令。

### 3.2 输入 `obs` 字段

#### `SelfView`

本机可物理感知的自身状态（仅读「本机自身」，不含他人信息）。

| 字段 | 类型 | 含义 / 取值 |
|---|---|---|
| `uid` | str | 本机 unique_id，等于 `agent.my_uid` |
| `lat` / `lon` | float | WGS84 经纬度 |
| `alt` | float | 高度（m），全程恒为 500，不可改 |
| `heading_deg` | float | 航向角（度） |
| `speed` | float | 速度（m/s），合法区间 15–40 |
| `gimbal_pan` | float | 云台方位角（度） |
| `gimbal_tilt` | float | 云台俯仰角（度） |
| `gimbal_fov_deg` | float | 相机视场角（度），区间 5–50 |
| `detection` | `Detection` | 本机相机检测结果（识别层产出，不保证真值，见 §1.4 / §2） |
| `detections` | `tuple[Detection,...]` | 多目标检测列表，预留字段，默认空 |
| `photo` | `Optional[bytes]` | UE 渲染最新相机帧 PNG bytes；无帧为 `None`（详见 §2.8） |
| `status` | str | 本机**自身**生存状态 `"active"` / `"destroyed"`，非他人信息；被击毁后 runner 停调 decide |
| `jammed` | bool | 本机当前是否被通信干扰，仅赛题三；感知动态威胁的唯一合法入口 |
| `comm_stats` | `CommStats` | 本机通信统计，见下 |

#### `Detection`

识别层产出（默认识别器 / 自研 / 端到端跳过）。位置可能为诱饵误识别或含噪声，**不是真值**。

| 字段 | 类型 | 含义 |
|---|---|---|
| `detected` | bool | 是否有目标进入相机视场 |
| `confidence` | float | 置信度 [0,1]；默认识别器给偏移置信度，自研/YOLO 来源为模型置信度 |
| `target_lat` / `target_lon` | `Optional[float]` | 检测到的位置，可能含噪声或误识；不可当作目标精确坐标 |
| `azimuth_error_deg` | `Optional[float]` | 相对光轴的方位偏移（度） |
| `target_type` | str | `"ground_vehicle"` / `"decoy_vehicle"` / `""`；诱饵会被伪装成 `ground_vehicle`，无法靠此字段识破 |

> **诱饵判别**：诱饵与真目标均沿路网机动、速度相近——多帧位置变化（位移）已无法区分两者，需采用航线/机动模式一致性等更高阶判别。渲染画面的车型/颜色差异在代码层不可读。

#### `Message`

`obs.comm_inbox` 的元素。

| 字段 | 类型 | 含义 |
|---|---|---|
| `sender_uid` | str | 发送者 uid，不暴露发送方位姿 |
| `payload` | str | 不超过 50 字节，格式由参赛者自定义，如 `"T:lat,lon"` 表目标 |
| `recv_time` | float | 接收时刻（sim_time） |

#### `CommStats`

间接感知通信干扰（被丢弃的消息仅能由此间接感知）。

| 字段 | 类型 | 含义 |
|---|---|---|
| `sent` | int | 本周期发送数 |
| `delivered` | int | 成功投递数 |
| `received` | int | 收到数 |
| `rejected_bytes` | int | 因超长被拒数 |
| `rejected_rate` | int | 因超速率（4 Hz）被拒数 |
| `rejected_jam` | int | 因被干扰被拒数 |

#### `MissionBriefing`

赛前静态简报，字段只增不删，均有默认值。

| 字段 | 类型 | 含义 | 赛题一 | 赛题二 | 赛题三 |
|---|---|---|---|---|---|
| `self_uid` | str | 本机 uid | ✓ | ✓ | ✓ |
| `fleet_size` | int | 编队受控实体总数 | 1 | 3 | 10 |
| `mission_area` | `Optional[AreaSpec]` | 任务区域矩形边界 | ✓ | ✓ | ✓ |
| `known_threats` | `tuple[ZoneSpec,...]` | 已废弃，恒为 `()` | — | — | — |
| `target_initial_pos` | `Optional[(lat,lon)]` | 目标初始位置 | 给 | — | — |
| `target_count` | `Optional[int]` | 目标数量 | 隐含 1 | 给（3） | 给（10） |
| `approximate_zones` | `tuple[ApproxZoneSpec,...]` | 近似威胁区，bbox+面积+类型+高度带，外扩约 20%，无精确多边形 | — | — | 给 |
| `params` | dict | 白名单参数，如 `coop_k`、动态干扰区统计 max_count/radius/lifetime，不含位置 | ✓ | ✓ | ✓ |
| `score_view` | `Optional[ScoreView]` | 每拍实时得分快照，首拍为 `None` | ✓ | ✓ | ✓ |

附属结构：
- `AreaSpec`：`lat_min` / `lat_max` / `lon_min` / `lon_max`（度，矩形边界）。
- `ApproxZoneSpec`：`kind`（威胁类型）/ `bbox`（外扩矩形）/ `area_m2`（真实面积）/ `alt_min` / `alt_max`（高度带）。
- `ScoreView`（只读）：`total_score` / `dimension_scores` / `passed` / `n_destroyed` / `n_targets` / `sim_time`。

### 3.3 输出 `Command`

模块 `competition.sdk.core.commands`。参赛者调用下列构造器函数取得 `Command` 对象，由 runner 发布并强制绑定到 `self.my_uid`；不得直接构造命令 dict。

#### 导航

| 构造器 | 引擎 verb | 参数 | 说明 |
|---|---|---|---|
| `fly_to(lat, lon, alt=None, speed=None, loiter_radius=200.0, turn_direction="right")` | `set_destination` | `lat, lon` 目标经纬度，必填；`speed` 15–40 m/s，超范围静默钳位；`loiter_radius` 大于 0；`turn_direction` 取 `"right"` 或 `"left"` | 导航至目标点并盘旋。`alt` 当前不生效，高度锁 500 m，传任何值被忽略 |
| `set_heading(heading_deg)` | `set_heading` | `heading_deg` 航向角（度） | 设置航向 |
| `set_speed(speed)` | `set_speed` | `speed` 15–40 m/s，超范围静默钳位 | 设置速度 |

#### 云台 / 相机

相机只在视场内检测目标，须先用 `point_gimbal` 瞄准才能搜索或跟踪。

| 构造器 | 引擎 verb | 参数 | 说明 |
|---|---|---|---|
| `point_gimbal(pan_deg, tilt_deg)` | `component.gimbal_tracking.set_orientation` | `pan` −180~180（钳位）；`tilt` −90~90（钳位，下为负） | 瞄准云台。`pan=0, tilt=-45` 为地面搜索默认 |
| `set_gimbal_fov(fov_deg)` | `set_fov` | `fov_deg` 5–50°，赛规钳位，超 50° 静默下钳 | 宽 FOV 覆盖大但 confidence 低，窄 FOV 反之 |

#### 通信

仅赛题二、三可用；赛题一无队友。受引擎四级限制：字节不超过 50、速率 4 Hz 、干扰区。

| 构造器 | 引擎 verb | 参数 | 说明 |
|---|---|---|---|
| `broadcast(payload: str)` | `comm.broadcast` | `payload` 不超过 50 字节 UTF-8 字符串 | 广播给所有队友 |
| `send_to(peer_uid: str, payload: str)` | `comm.send` | `peer_uid` 目标队友 uid；`payload` 不超过 50 字节 | 点对点发送 |

`payload` 超 50 字节时构造阶段即抛 `PayloadTooLarge`，不进入引擎。

#### 目标上报（专用接口，不能用通信消息替代）

| 构造器 | 引擎 verb | 参数 | 说明 |
|---|---|---|---|
| `report_target(lat, lon, target_id=None)` | `agent.report` | `lat, lon` 判定的真目标位置；`target_id` 可选标签，裁判按解析的目标计，与标签无关 | 判定真目标位置的意图信号。引擎不处理，runner 路由给评分器算精度。限速 1 报告/目标/秒；对已摧毁目标的报告、报"尸体"均整条丢弃 |

通信消息（`broadcast`/`send_to`）评分器不收，目指上报只能用此接口。赛题一靠它每秒上报驱动持续目指精度；赛题二、三靠它按每目标累计 RMSE 评分。

#### 不存在的命令

- 无 `attack` / `fire` / `launch`：杀伤靠引擎区域裁决（SAM）或盯防达成（赛题二需 2 架同时、赛题三需 3 架同时盯防真目标 20 秒）。
- 无 `deploy_decoy`：诱饵是场景静态实体，不可投放。
- 无改高度命令：高度全程锁 500 m。
- 赛题一无通信命令，但含 `report_target`。

---

## 4. 平台物理参数

平台给定，只读；运行中部分参数可由控制命令间接调整，调整区间由平台限定。

| 参数 | 取值 | 可改性 | 说明 |
|---|---|---|---|
| 飞行高度 | 500 m | 不可改，全程锁死 | `fly_to` 的 `alt` 参数被忽略；`obs.self.alt` 恒为 500 |
| 合法速度区间 | 15–40 m/s | 区间不可改 | 超范围被静默钳位 |
| 起始速度 | 20 m/s | 运行中可由 `set_speed` 改 | 初始值不可改 |
| 最大转向率 | 平台限定 | 不可改 | 航向指令受此限制 |
| 默认盘旋半径 | 200 m | `fly_to` 中可指定 | 必须大于 0 |
| 云台 FOV | 初始 30° | 由 `set_gimbal_fov` 改，区间 5–50° | 上限 50° 为赛规 |
| 云台方位转速 | 60°/s | 不可改 | 瞄准响应快慢 |
| 云台俯仰转速 | 30°/s | 不可改 | 瞄准响应快慢 |
| 通信距离上限 | 无限制 | 不可改 | 全场任意两机可达 |
| 通信字节上限 | 50 字节 | 不可改 | 超长抛 `PayloadTooLarge` |
| 通信速率上限 | 4 Hz | 不可改 | 滑窗限速 |
| 机型 | 固定翼 UAV | 不可改 | 三题统一 |

---

## 5. 运行模式与 CLI 参数

启动：
```bash
python -m competition run --scenario <题> --agent <模块:类> [选项]
```

| 参数 | 取值 / 默认 | 说明 |
|---|---|---|
| `--scenario` | `search_track` / `coop_decoy` / `adversarial_swarm` | 赛题，必填 |
| `--agent` | `module.path:ClassName` | Agent 类，必填。可写 `baselines.xxx:Cls` 简写 |
| `--duration` | float，默认按场景 | 仿真时长（秒） |
| `--mode` | `train`（默认）/ `eval` | `train`=选手开发用（AccuracySimulator）；`eval`=官方评测用（YoloDetector）。选手本地保持 `train` 即可 |
| `--photo-mode` | `auto`（默认）/ `on` / `off` | 相机帧拉取开关（见下表） |
| `--accuracy` | float，默认 0.85，上限钳 0.9 | train 模式 AccuracySimulator 检出概率 |
| `--noise-sigma` | float，默认 50，下限钳 30，单位米 | train 模式位置噪声标准差 |
| `--yolo-model` | 路径，默认空 | eval 模式的 YOLOv8 权重；选手本地开发一般用不到 |
| `--seed` | int，默认 0 | 场景随机种子，可复现 |
| `--scenario-json` | 路径 | 自定义场景 JSON |
| `--start-sim` / `--no-start-sim` | 默认开 | 是否自动 spawn 仿真引擎 |
| `--dry-run` | flag | 无渲染、无引擎，仅本地推演，`photo` 为 `None` |
| `--visualize` | flag | 生成 2D 可视化 |
| `--viz-dir` / `--output` | 路径 | 可视化/输出目录 |
| `--redis-host` | 默认 `127.0.0.1` | Redis 主机 |

**`--photo-mode` 三态语义**：

| 模式 | dry_run | 行为 |
|---|---|---|
| `auto`（默认） | True | 不启动 PhotoCache（无 UE） |
| `auto`（默认） | False | 启动 PhotoCache；Redis 有帧→注入 `obs.self.photo`，无帧→None（安全降级，不报错） |
| `on` | True | 不启动 |
| `on` | False | 启动（同 auto 非 dry_run） |
| `off` | 任意 | 不启动，`obs.self.photo` 恒 None |

三个赛题共用同一套感知-决策架构（spec 029 + spec 032），CLI 参数和 `algorithm.yaml` 配置完全一致，均支持三种感知选择（默认识别器 / 自研 / 端到端）。

常用命令示例：
```bash
# 选手本地开发（默认 train）：AccuracySimulator 模拟检出概率 0.85、噪声 50m
python -m competition run --scenario search_track --agent my_pkg:MyAgent --duration 600

# 纯控制等价真值基线（accuracy=1.0 无误差）
python -m competition run --scenario search_track \
    --agent baselines.search_track_fsm:FsmAgent --accuracy 1.0 --noise-sigma 0 --duration 600
```

---

## 6. 架构概览与快速开始

通读前 5 节后，本节给出全景数据流与最小骨架，以建立整体认识。

### 6.1 数据流：sensor → decide 两层回调

每架己方无人机在主循环中按固定节拍（约 10 Hz）跑一遍完整流程。runner 在中间插入**两个回调调用点**，夹着一个「识别层」：

```
引擎真值/photo ──► obs(self 状态 + photo，detection 槽位先填空占位)
                        │
                        ▼
                 ┌─ sensor() ──► 产出 Detection（可选）
                 │     │   不实现/返回 None → 走默认识别器
                 │     ▼
                 │  obs.self.detection（识别层产出，不保证真值）
                 ▼
              decide() ──► list[Command]（导航/云台/通信/上报）
                        │
                        ▼
                     引擎执行
```

**两层定位**：
- **sensor**：回答「看到了什么」——产出 `Detection`，**不确定、可能含噪声、会被诱饵骗**。可选实现，不实现则走默认识别器。
- **decide**：回答「做什么」——读 `obs.self.detection`（sensor 已填好），返回确定的控制命令。**必须实现**。

### 6.2 最小可运行骨架

继承 `Agent`、仅实现 `decide`。此时 sensor 未覆盖，默认走 `AccuracySimulator`（train 模式）：

```python
from competition.sdk.core.agent import Agent
from competition.sdk.core.commands import fly_to, point_gimbal

class MyAgent(Agent):
    def decide(self, obs, dt):
        det = obs.self.detection          # ← 默认识别器产出
        if det.detected and det.target_lat is not None:
            return [fly_to(det.target_lat, det.target_lon),
                    point_gimbal(0.0, -45.0)]
        return []                          # 空列表 = 本周期不发命令
```

运行：
```bash
python -m competition run --scenario search_track --agent my_pkg:MyAgent --duration 600
```

---

## 附录：import 速查

```python
# 基类与观测
from competition.sdk.core.agent import Agent
from competition.sdk.core.observation import (
    Observation, Detection, Message, MissionBriefing,
    SelfView, CommStats, AreaSpec, ZoneSpec, ApproxZoneSpec, ScoreView,
    SKIP_DETECTION,
)
# 命令构造器
from competition.sdk.core.commands import (
    Command, fly_to, set_heading, set_speed,
    point_gimbal, set_gimbal_fov,
    broadcast, send_to, report_target,
    PayloadTooLarge,
)
# 自研识别反算工具（可选）
from competition.sdk.core.perception.bbox_to_latlon import pan_tilt_to_latlon, meters_to_deg
```
