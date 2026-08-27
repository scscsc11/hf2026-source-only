# 参赛者赛题说明

> 本文档面向**参赛人员**，说明三道赛题的设置、目的、评分准则、可获信息、可用 SDK 与输出要求。
> 权威接口定义见 `competition/docs/api-reference.md` 与 `specs/026-competition-agent-sdk/contracts/`。

---

## 0. 总览：你在做什么

你编写一个**自主智能体（Agent）**，控制一架空投到陌生任务区的无人机（或多架无人机编队），在**不掌握全局真值**的前提下，通过机载相机感知、编队通信协同，完成"搜索—识别—打击（目指）"的 ISR 任务。平台每 0.1 秒（10 Hz）调用一次你的 `decide(obs, dt)`，你返回一组指令，引擎执行。

三道赛题难度递进：

| 赛题 | 场景 | 你的角色 | 无人机 | 真实目标 | 诱饵 | 威胁区 | 时长 |
|---|---|---|---|---|---|---|---|
| 一 | 单机搜索跟踪 | 调度者（持续盯防） | 1 | 1 | 0 | 无 | 600s |
| 二 | 多机协同识别打击 | 识别者（目指+协同打击） | 3 | 3 | 15 | 无 | 600s |
| 三 | 对抗集群搜索 | 识别者+生存者（目指+打击+规避） | 10 | 10 | 20 | 击毁区+干扰区 | 600s |

**核心约束（贯穿三赛题）**：你只能看到自己无人机的传感器数据 + 队友发来的消息 + 赛前简报。你看不到其他无人机的位置、目标的真实位置、诱饵的真实类型、区域的精确边界。一切"全局信息"都必须靠感知和协同推断。这是数据层强制——禁止的信息在你的 `obs` 里**物理上不存在**，不是"有但不让你看"。

---

## 1. 你的唯一职责：实现 `decide(obs, dt)`

你继承赛题对应的 Agent 基类，实现**一个方法**：

```python
from competition.sdk.scenarios.search_track import SearchTrackAgent

class MyAgent(SearchTrackAgent):
    def decide(self, obs, dt):
        # 读 obs，返回指令列表
        return [fly_to(...), point_gimbal(...)]
```

**生命周期**（runner 驱动，你不用管）：
1. `MyAgent(my_uid)` — 每架受控无人机构造一个实例。
2. `configure(config)` — 注入静态参数（可选重写）。
3. `reset()` — 每局首次 `decide` 前调用一次（可选重写，初始化状态）。
4. `decide(obs, dt)` — 每个决策周期（~10 Hz）调用，返回指令列表。

**五条不变量**（违反即破坏隔离契约，会被判违规）：
- I-1：`decide()` 内不得访问 Redis/文件/网络；除自身内部状态外是纯函数。
- I-2：`reset()` 在每局首次 `decide` 前调用。
- I-3：返回的指令列表**按顺序**发布。
- I-4：不得试图获取 `obs` 以外的全局信息（直连总线读真值属违规）。
- I-5：指令只作用于自己的 `my_uid`——runner 强制注入，你无法操控别人的无人机。

---

## 2. 你能看到什么：`obs` 三件套

每个 `decide(obs, dt)` 收到的 `obs` 只有三个顶层字段（形状永不改变）：

### 2.1 `obs.self` — 自己无人机的可感状态（SelfView）

| 字段 | 含义 |
|---|---|
| `lat`, `lon`, `alt` | 自身位置 |
| `heading_deg`, `speed` | 航向、速度 |
| `gimbal_pan`, `gimbal_tilt`, `gimbal_fov_deg` | 云台方位/俯仰/视场角 |
| `detection` | **相机检测结果**（见下） |
| `status` | `"active"` 或 `"destroyed"` |
| `jammed` | 当前是否被通信干扰 |
| `comm_stats` | 通信统计（发送/送达/被干扰拒绝字节数等） |

**`detection`（关键）**——这是你感知目标的**唯一**途径：

| 字段 | 含义 |
|---|---|
| `detected` | 相机视场内是否检测到物体 |
| `target_lat`, `target_lon` | 检测到的物体位置（**相机测量值，非真值**） |
| `confidence` | 置信度 [0,1] |
| `azimuth_error_deg` | 方位误差 |
| `target_type` | `"ground_vehicle"`（真实目标**或被误识别的诱饵**）或 `""` |

⚠️ **诱饵伪装**：当相机被诱饵欺骗时，引擎如实上报"误识别"，但你的 `obs` 里 `target_type` 会被**伪装成 `"ground_vehicle"`**。你**无法**靠 `target_type` 字段区分真实目标与诱饵——必须靠多帧运动一致性判断（诱饵静止，真实目标移动）。这是赛题二/三的核心难点。

### 2.2 `obs.comm_inbox` — 队友发来的消息

每条 `Message` 只有 `sender_uid`、`payload`（≤50 字节字符串）、`recv_time`。**队友的位置绝不包含在内**——要知道队友在哪，只能靠他主动在 payload 里告诉你（你们自行约定格式，如 `"R:27.0,125.0"`）。

### 2.3 `obs.briefing` — 赛前静态简报（MissionBriefing）

整局不变（除 `score_view` 每拍更新）。各赛题给的字段不同：

| 字段 | 赛题一 | 赛题二 | 赛题三 |
|---|---|---|---|
| `mission_area` | 任务区边界 | 任务区边界 | 任务区边界 |
| `target_initial_pos` | ✅ 目标初始 (lat,lon) | ❌ None | ❌ None |
| `target_count` | ❌ None（=1） | ✅ 目标数 | ✅ 目标数 |
| `approximate_zones` | ❌ () | ❌ () | ✅ 击毁区/静态干扰区的**近似**信息 |
| `score_view` | ✅ 每拍实时得分 | ✅ 每拍实时得分 | ✅ 每拍实时得分 |
| `params` | `target_speed` | `coop_k`, `sector_center` | `fleet_size`, `mission_area`, 动态干扰区**统计参数** |
| `known_threats` | （已废弃，恒为空） | （已废弃，恒为空） | （已废弃，恒为空，改用 `approximate_zones`） |

**`approximate_zones`（仅赛题三）**：每个 `ApproxZoneSpec` 给你：
- `kind`：`"air_defense"`（击毁区/SAM）或 `"comm_jam_static"`（静态干扰区）
- `bbox`：区域的大致方框 `((lat_min,lon_min),(lat_max,lon_max))`，**已外扩约 20%**——比真实区域大一圈，你只知道"在哪一片"，不知道精确边界
- `area_m2`：区域面积（让你知道威胁规模）
- `alt_min`/`alt_max`：高度带——**飞过 `alt_max` 即可避开**击毁区

**动态干扰区（`comm_jam_random`，仅赛题三）**：位置**完全不告知**，运行时随机生成、存活一段时间后消失。你只能靠 `obs.self.jammed` 感知自己是否被干扰，靠失联队友推测其大致位置。`params` 里会给统计参数（数量、半径、寿命、生成间隔），让你知道"会有几个多大的干扰区"，但不知道在哪。

**`score_view`（三赛题都有）**：每拍更新的实时得分快照，含 `total_score`、`dimension_scores`、`passed`、`n_destroyed`、`n_targets`、`sim_time`。首拍为 `None`（还没分数）。可用于自适应策略。

---

## 3. 你能用哪些 SDK：指令构造器

你**不直接构造指令字典**，而是调用 `competition.sdk.core.commands` 里的强类型函数。runner 发布时会强制 `unique_id = 你的 my_uid`。

### 导航指令
| 函数 | 作用 |
|---|---|
| `fly_to(lat, lon, alt=None, speed=None, loiter_radius=200, turn_direction="right")` | 飞到指定点并盘旋（主移动指令） |
| `set_heading(heading_deg)` | 设航向 |
| `set_speed(speed)` | 设速度 |

### 云台/相机指令（感知接口）
| 函数 | 作用 |
|---|---|
| `point_gimbal(pan_deg, tilt_deg)` | **主感知接口**——瞄准云台。相机只检测视场锥内的目标，必须主动瞄准搜索/跟踪 |
| `set_gimbal_fov(fov_deg)` | 设视场角 [5,120]°。大视场搜索广但置信度低，小视场精度高但范围窄 |

### 通信指令（赛题二/三）
| 函数 | 作用 |
|---|---|
| `broadcast(payload)` | 广播给所有队友（≤50 字节，受速率/距离/干扰限制） |
| `send_to(peer_uid, payload)` | 点对点发送 |

### 目指指令（赛题二/三）
| 函数 | 作用 |
|---|---|
| `report_target(lat, lon, target_id=None)` | 上报你判定的真实目标位置（目指信息）。裁判据此评分目指精度。每目标每秒限 1 次，已摧毁目标的上报被忽略 |

⚠️ **不存在的指令**：`attack`/`fire`/`launch`（无"开火"概念——打击靠协同盯防达成）、`deploy_decoy`（诱饵是静态场景实体）。打击效果通过"协同持续盯防真实目标"自动达成，无需显式攻击指令。

---

## 4. 三道赛题详解

### 赛题一：单机搜索跟踪（`search_track`）

**场景**：1 架无人机 + 1 辆真实目标车（无诱饵、无威胁区）。目标车会机动。

**你的目标**：用相机**持续盯住**目标车，让它在视场内的累计时间尽量长。

**你获知的信息**：
- `briefing.target_initial_pos`：目标初始位置（开局直飞过去即可，降低搜索成本）
- `briefing.params["target_speed"]`：目标速度
- 之后靠 `obs.self.detection` 持续跟踪

**评分（单一维度）**：
- `completion`（权重 1.0）：累计"目标在视场内"的时间 / 总时长。满分 = 全程盯住。
- 通过线：`completion ≥ 0.8` 且总分 ≥ 60。

**关键参数**：K=1（单机即满足"协同"），`grace_s=2.0`（≤2 秒的短暂丢失不重置、不扣分）。

**赛题一不使用 `report_target`**——它是纯跟踪任务，引擎直接用你的检测结果评分。

---

### 赛题二：多机协同识别打击（`coop_decoy`）

**场景**：3 架无人机 + 3 个真实目标 + **15 个诱饵**（无威胁区）。目标会机动，诱饵静止。

**你的目标**：
1. 区分真实目标与诱饵（靠运动一致性——诱饵不动）。
2. 协同盯防真实目标：**2 架无人机同时盯防同一真实目标 20 秒** → 该目标被"摧毁"。
3. 上报目指信息：对每个真实目标调用 `report_target(lat, lon)`，报告越准分越高。
4. 别在诱饵上浪费时间：盯防诱饵会扣分（但诱饵被盯满 20 秒后"识别"了，不再扣分）。

**你获知的信息**：
- `briefing.target_count = 3`（目标数量，**不给位置**）
- `briefing.params`：`coop_k=2`（协同阈值，2 架无人机同时盯防才满足）、`sector_center`（任务区中心）
- 靠 3 架无人机的相机 + 通信协同搜索、识别、共享目标位置

**评分（三维度加权）**：
| 维度 | 权重 | 含义 | 满分条件 |
|---|---|---|---|
| `kill` | 0.50 | 摧毁的真实目标比例 | 全部 3 个目标被盯防摧毁 |
| `accuracy` | 0.30 | 目指 RMSE 精度 | 所有 `report_target` 与真值偏差为 0 |
| `misid_penalty` | 0.20 | 误识别惩罚的**反向得分** | 没在未摧毁诱饵上浪费盯防时间 |

- `accuracy`：`100 × max(0, 1 - RMSE/D_max)`，`D_max=120m`（RMSE 达 120m 得 0 分）。
- `misid_penalty`：`100 × max(0, 1 - 误盯诱饵秒数/30s)`（误盯诱饵累计 30s 得 0 分）。
- 通过线：摧毁 ≥ 2/3 目标 且总分 ≥ 70。

**关键机制**：
- "摧毁"= 2 架无人机同时持续有效跟踪同一真实目标达 20 秒（检测结果匹配到同一真目标）。
- `grace_s=2.0`：≤2 秒的跟踪中断不重置（back-fill），>2 秒重置该目标的盯防累计。
- 摧毁是永久的（locked），已摧毁目标不再接受 `report_target`。

---

### 赛题三：对抗集群搜索（`adversarial_swarm`）

**场景**：10 架无人机 + 10 个真实目标 + 20 个诱饵 + **威胁区**（1 个击毁区 + 1 个静态干扰区 + 动态随机干扰区）。目标会机动，诱饵静止。**无人机会被击毁**。

**你的目标**（综合任务）：
1. 搜索、识别、摧毁全部 10 个真实目标（1 架无人机持续盯防 20 秒即摧毁）。
2. 上报准确的目指信息。
3. **快速完成**——越快摧毁全部目标，`mission_time` 分越高。
4. **保生存**——少被击毁区打掉。
5. 别在诱饵上浪费时间。

**你获知的信息**：
- `briefing.target_count = 10`（不给位置）
- `briefing.approximate_zones`：击毁区/静态干扰区的**近似方框**（外扩 20%，含面积、类型、高度带）——知道"哪片有威胁、多大、飞多高能避开"，但不知精确边界
- `briefing.params["comm_jam_random"]`：动态干扰区统计参数（数量、半径、寿命、间隔），**不含位置**
- 靠 10 架无人机协同：搜索分工、目标共享、失联队友推测干扰区

**威胁区机制**：
- **击毁区（`air_defense`/SAM）**：无人机在其高度带内停留 ≥ `hit_delay_s`（默认 2s）即被摧毁。**规避方法**：爬升超过其 `alt_max`（`approximate_zones` 里给了），或绕开其近似方框。
- **静态干扰区（`comm_jam_static`）**：在其范围内通信失效（消息被丢弃，`comm_stats.rejected_jam` 上升）。位置近似已知（`approximate_zones`）。
- **动态干扰区（`comm_jam_random`）**：运行时随机生成、存活一段后消失。位置**完全隐藏**，只能靠 `obs.self.jammed` 感知。靠失联队友的最后心跳位置 + 安全半径推测。

**评分（五维度加权）**：
| 维度 | 权重 | 含义 | 满分条件 |
|---|---|---|---|
| `kill` | 0.35 | 摧毁的真实目标比例 | 全部 10 个摧毁 |
| `accuracy` | 0.25 | 目指 RMSE 精度 | `report_target` 偏差为 0，`D_max=150m` |
| `mission_time` | 0.25 | 完成全部击杀的时间 | `T_done ≤ T0=120s` 满分，之后线性衰减至 `T0+T_flex=240s` 归 0 |
| `alive` | 0.10 | 存活无人机比例 | 全部存活 |
| `misid_penalty` | 0.05 | 误识别惩罚反向得分 | 不在未摧毁诱饵上浪费时间（`misid_cap=60s`） |

- 通过线：摧毁 ≥ 70% 目标 且总分 ≥ 70 且存活率 ≥ 50%。
- K=1：1 架无人机持续盯防 20 秒即摧毁（单机即满足，无需协同）。

---

## 5. 你需要输出什么

### 5.1 提交物：一个 Agent 模块

你提交**一个 Python 模块**，里面定义你的 Agent 类。通过 CLI 指定：

```bash
python -m competition run \
    --scenario search_track \
    --agent my_team.agent:MyAgent
```

`my_team/agent.py` 里：

```python
from competition.sdk.scenarios.search_track import SearchTrackAgent
from competition.sdk.core.commands import fly_to, point_gimbal

class MyAgent(SearchTrackAgent):
    def reset(self):
        self._went_to_initial = False

    def decide(self, obs, dt):
        tip = obs.briefing.target_initial_pos
        if tip is not None and not self._went_to_initial:
            self._went_to_initial = True
            return [fly_to(tip[0], tip[1])]
        # ... 搜索/跟踪逻辑
        return [point_gimbal(0.0, -45.0)]
```

各赛题基类：
- 赛题一：`competition.sdk.scenarios.search_track.SearchTrackAgent`
- 赛题二：`competition.sdk.scenarios.coop_decoy.CoopAgent`
- 赛题三：`competition.sdk.scenarios.adversarial_swarm.SwarmAgent`

### 5.2 `decide()` 返回值

返回一个 `list[Command]`（按顺序发布）。返回空列表 `[]` 表示本拍不发指令。每个 Command 由 §3 的构造器函数生成。

### 5.3 多机控制

赛题二/三你控制多架无人机。runner 为**每架**受控无人机各构造一个你的 Agent 实例（同一类），分别调用各自的 `decide()`。各实例只看到自己的 `obs.self`——要协同，靠 `broadcast`/`send_to` 通信。你的 Agent 类必须支持多实例独立运行（状态存在各实例的 `self` 上）。

---

## 6. 评分总分与通过判定

总分 = `Σ 维度权重 × 维度得分`（各维度 0-100），总分钳制在 [0,100]。**总分非单调**——它会随误识别增加、无人机被击毁、协同重置而下降；只有 `completion_rate`/`kill_rate` 是单调递增的进度指标。

通过判定（各赛题不同）：
- 赛题一：`completion ≥ 0.8` 且总分 ≥ 60。
- 赛题二：摧毁 ≥ 2/3 目标 且总分 ≥ 70。
- 赛题三：摧毁 ≥ 70% 目标 且总分 ≥ 70 且存活率 ≥ 50%。

实时得分经 `obs.briefing.score_view` 每拍可见，可用于自适应策略（如发现 `accuracy` 低则多发 `report_target`）。

---

## 7. 通信约束（赛题二/三）

| 约束 | 值 |
|---|---|
| 单条 payload 上限 | 50 字节（UTF-8） |
| 速率限制 | 4 Hz 滑动窗口 |
| 通信距离 | ~1000m（超出丢弃） |
| 干扰影响 | 在干扰区内消息被丢弃，`comm_stats.rejected_jam` 上升 |

你与队友自行约定 payload 格式。建议约定简洁高效（如 `"T:lat,lon"` 报目标、`"J:lat,lon"` 报干扰区、`"S:status"` 报状态）。

---

## 8. 开发与调试

### 本地运行
```bash
python -m competition run --scenario <name> --agent <module:Class> --duration <s>
```

### 随机化场景（训练多样性）
加 `--seed N`：随 seed 平移全场实体位置、重排目标航迹、重设动态干扰区种子。同一 seed 可复现。

### 可视化
平台提供 3D 可视化（上帝视角，仅用于旁观调试，**参赛者 Agent 看不到这个视角**）。

### 参考：baseline 算法
`competition/baselines/` 提供三个参考实现（纯 SDK、严格隔离）：
- `search_track_fsm.py`：阿基米德螺旋搜索 + FSM 跟踪。
- `coop_distributed.py`：相位偏置螺旋分工 + 运动判诱饵 + 目标共享 + `report_target`。
- `swarm_distributed.py`：10 机分布式 + 静态威胁区 bbox 规避 + 失联推测干扰区。

建议先读懂 baseline，再在其基础上改进或重写。

---

## 9. 常见误区与提示

1. **"我看不到目标在哪不公平"**——这是设计如此。战时对敌方位置只有传感器感知，没有上帝视角。赛题一给了初始位置作简化；赛题二/三靠搜索。
2. **"诱饵怎么识别"**——靠**多帧运动一致性**。连续盯一个物体 20+ 拍，若位置几乎不变→大概率诱饵；若移动→真实目标。`target_type` 字段不可信（诱饵被伪装成 `ground_vehicle`）。
3. **"打击怎么发起"**——无需开火。1 架无人机持续盯防同一真实目标 20 秒，目标自动被判定摧毁。
4. **"击毁区精确边界在哪"**——不给精确边界。`approximate_zones` 给外扩 20% 的方框，保守起见进入方框就爬升到 `alt_max` 之上。
5. **"动态干扰区在哪"**——完全不告知。靠 `obs.self.jammed`（自己被干扰）+ 失联队友的最后心跳推测。
6. **"总分为什么下降了"**——正常。误识别增加、无人机被击毁、协同中断都会扣分。关注 `kill_rate`（单调进度）而非瞬时总分。
7. **"能直连 Redis 读真值吗"**——**违规**。I-1/I-4 不变量禁止，且违背比赛精神。隔离是数据层强制，合规 Agent 物理上拿不到禁止信息。

---

## 10. 信息暴露规则速查表

| 信息 | 赛题一 | 赛题二 | 赛题三 |
|---|---|---|---|
| 目标初始位置 | ✅ `briefing.target_initial_pos` | ❌ | ❌ |
| 目标过程位置 | ❌（靠相机） | ❌（靠相机） | ❌（靠相机） |
| 目标数量 | 隐含=1 | ✅ `briefing.target_count` | ✅ `briefing.target_count` |
| 目标航迹/航点 | ❌ | ❌ | ❌ |
| 击毁区精确多边形 | 无区域 | 无区域 | ❌（只给近似 bbox） |
| 击毁区近似方框+面积 | 无区域 | 无区域 | ✅ `briefing.approximate_zones` |
| 静态干扰区近似方框 | 无区域 | 无区域 | ✅ `briefing.approximate_zones` |
| 动态干扰区位置 | 无 | 无 | ❌（靠 `obs.self.jammed`） |
| 动态干扰区统计参数 | 无 | 无 | ✅ `briefing.params` |
| 队友位置 | ❌（靠通信） | ❌（靠通信） | ❌（靠通信） |
| 诱饵类型 | ❌（伪装） | ❌（伪装） | ❌（伪装） |
| 实时得分 | ✅ `briefing.score_view` | ✅ | ✅ |

---

## 附录：各赛题评分参数表

| 参数 | 赛题一 | 赛题二 | 赛题三 |
|---|---|---|---|
| K（协同阈值） | 1 | 2 | 1 |
| `dwell_target_s`（摧毁/识别阈值） | — | 20s | 20s |
| `grace_s`（中断容忍） | 2s | 2s | 2s |
| `D_max_m`（目指 RMSE 零分点） | — | 120m | 150m |
| `misid_cap_s`（误盯诱饵零分点） | — | 30s | 60s |
| `T0_s` / `T_flex_s`（任务时间） | — | — | 120s / 120s |
| 通过线（摧毁率） | `completion≥0.8` | ≥2/3 | ≥70% |
| 通过线（总分） | ≥60 | ≥70 | ≥70 |
| 通过线（存活率） | — | — | ≥50% |
| 维度权重 | `completion:1.0` | `kill:0.5 / accuracy:0.3 / misid:0.2` | `kill:0.35 / accuracy:0.25 / mission_time:0.25 / alive:0.10 / misid:0.05` |
