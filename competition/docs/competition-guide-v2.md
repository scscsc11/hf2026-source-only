# 参赛说明（精简版）

本文面向参赛者，只保留完成比赛所需的核心规则、接口和运行方式。完整接口细节见 [`api-reference.md`](api-reference.md)，场景细则见 [`scenarios/`](scenarios/)。

## 1. 参赛任务

你需要提交一个 Python Agent 类，实现 `decide(obs, dt)`。平台约 10 Hz 调用一次，你的方法返回控制指令，runner 会把这些指令强制绑定到当前无人机。

三道赛题难度递进：

| 赛题 | 名称 | 规模 | 重点 |
|---|---|---:|---|
| `search_track` | 目标跟踪 | 1 架无人机、1 个目标 | 已知目标初始位置，持续跟踪目标 |
| `coop_decoy` | 多机协同搜索识别 | 3 架无人机、3 个目标、15 个诱饵 | 未知目标初始位置，协同搜索、识别诱饵、上报目标 |
| `adversarial_swarm` | 对抗集群搜索 | 10 架无人机、10 个目标、20 个诱饵、威胁区 | 未知目标初始位置，搜索打击、规避击毁区和通信干扰 |

核心限制：Agent 只能看到自己的观测、收到的队友消息和赛前简报。看不到目标真值、队友位置、精确威胁边界或动态干扰区位置。

## 2. Agent 接口

从对应赛题的基类继承，并实现 `decide()`：

```python
from competition.sdk.scenarios.search_track import SearchTrackAgent
from competition.sdk.core.commands import fly_to, point_gimbal


class MyAgent(SearchTrackAgent):
    def reset(self):
        self.acquired = False

    def decide(self, obs, dt):
        if obs.self.detection.detected:
            self.acquired = True
            return [
                point_gimbal(0.0, -45.0),
                fly_to(obs.self.detection.target_lat,
                       obs.self.detection.target_lon),
            ]

        tip = obs.briefing.target_initial_pos
        if tip is not None and not self.acquired:
            return [fly_to(tip[0], tip[1]), point_gimbal(0.0, -45.0)]

        return [point_gimbal(0.0, -45.0)]
```

赛题基类：

| 赛题 | 基类 |
|---|---|
| `search_track` | `competition.sdk.scenarios.search_track.SearchTrackAgent` |
| `coop_decoy` | `competition.sdk.scenarios.coop_decoy.CoopAgent` |
| `adversarial_swarm` | `competition.sdk.scenarios.adversarial_swarm.SwarmAgent` |

多机赛题中，runner 会为每架己方无人机各创建一个 Agent 实例。实例之间没有共享内存，协同只能通过 `broadcast()` / `send_to()` 和 `obs.comm_inbox` 完成。

`decide()` 内不要访问 Redis、网络或文件，也不要试图读取 `obs` 之外的真值信息。

## 3. 你能看到什么

每次 `decide(obs, dt)` 的 `obs` 固定包含三部分：

| 字段 | 内容 |
|---|---|
| `obs.self` | 当前无人机自己的位置、航向、速度、云台、相机检测、状态、干扰状态 |
| `obs.comm_inbox` | 队友发来的消息，只包含发送者 uid、payload 和接收时间 |
| `obs.briefing` | 赛前静态信息，如任务区、目标数量、近似威胁区、实时得分快照 |

相机检测在 `obs.self.detection`：

| 字段 | 含义 |
|---|---|
| `detected` | 本拍是否检测到物体 |
| `target_lat`, `target_lon` | 检测位置，不是真值 |
| `confidence` | 检测置信度 |
| `azimuth_error_deg` | 目标相对云台中心的方位偏差 |
| `target_type` | 通常为 `"ground_vehicle"` 或空字符串 |

注意：诱饵可能被伪装成 `"ground_vehicle"`。不要只靠 `target_type` 判断真假目标，应通过多帧位置变化判断：真实目标会移动，诱饵通常静止。

## 4. 可用指令

从 `competition.sdk.core.commands` 导入指令构造器：

| 指令 | 用途 |
|---|---|
| `fly_to(lat, lon, alt=None, speed=None)` | 飞到指定位置并盘旋 |
| `set_heading(heading_deg)` | 设置航向 |
| `set_speed(speed)` | 设置速度 |
| `point_gimbal(pan_deg, tilt_deg)` | 控制云台朝向，是主要感知手段 |
| `set_gimbal_fov(fov_deg)` | 设置相机视场角 |
| `broadcast(payload)` | 多机赛题广播消息，payload 不超过 50 字节 |
| `send_to(peer_uid, payload)` | 多机赛题点对点消息 |
| `report_target(lat, lon, target_id=None)` | 赛题二/三上报判定的真实目标位置 |

不存在 `attack`、`fire`、`launch` 这类开火指令。目标摧毁由裁判根据“一无人机持续跟踪目标超过20秒”自动判定，目标被摧毁后仍会继续移动，但继续跟踪该目标不记入得分。

## 5. 三道赛题要点

### `search_track`

- 默认 600 秒。
- 这是目标跟踪任务。
- 给出目标初始位置：`obs.briefing.target_initial_pos`。
- 目标是持续让目标处在相机视场内。
- 评分只看 `completion`：目标在视场内的累计时间比例。
- 通过线：`completion >= 0.8` 且总分 `>= 60`。

### `coop_decoy`

- 默认 600 秒。
- 这是目标搜索任务，不给目标初始位置。
- 3 架无人机、3 个真实目标、15 个诱饵。
- 只给 `target_count` 和协同阈值 `coop_k` 等静态参数。
- 同一真实目标被 2 架无人机同时盯防 20 秒后视为摧毁。
- 使用 `report_target()` 上报你判断的真实目标位置，精度影响得分。
- 盯防未识别诱饵会降低 `misid_penalty` 维度得分。
- 通过线：摧毁至少 2/3 真实目标且总分 `>= 70`。

### `adversarial_swarm`

- 默认 600 秒。
- 这是目标搜索任务，不给目标初始位置。
- 10 架无人机、10 个真实目标、20 个诱饵。
- 有击毁区、静态通信干扰区和动态通信干扰区。
- `obs.briefing.approximate_zones` 只给静态威胁区的近似 bbox、面积和高度带，不给精确边界。
- 动态干扰区位置不公开，只能通过 `obs.self.jammed` 和通信异常推断。
- 协同阈值为 1：1 架无人机持续盯防 20 秒即摧毁目标（单机即可，无需协同）。
- 通过线：摧毁率 `>= 70%`、总分 `>= 70`、存活率 `>= 50%`。

## 6. 运行方式

准备好 Agent 后运行：

```bash
python -m competition run \
    --scenario search_track \
    --agent my_agent:MyAgent \
    --duration 60
```

常用参数：

| 参数 | 说明 |
|---|---|
| `--scenario` | `search_track`、`coop_decoy` 或 `adversarial_swarm` |
| `--agent` | `模块路径:类名` |
| `--duration` | 仿真时长 |
| `--seed` | 随机化场景；同一 seed 可复现 |
| `--visualize` | 打开旁观可视化，不影响评分 |
| `--dry-run` | 不连接引擎和 Redis，仅做 SDK 调用冒烟测试 |
| `--no-start-sim` | 连接已启动的仿真器 |

运行结束后，评分结果写入 `output/*.evaluation.json`。

## 7. 建议开发流程

1. 从模板开始：`competition/templates/`。
2. 先跑 `search_track`，确认 Agent 能被加载并能返回合法指令。
3. 再实现搜索、跟踪、诱饵识别和通信协议。
4. 使用多个 `--seed` 测试策略稳定性。
5. 需要完整字段、参数范围和评分细节时，再查：
   - [`api-reference.md`](api-reference.md)
   - [`scenarios/search_track.md`](scenarios/search_track.md)
   - [`scenarios/coop_decoy.md`](scenarios/coop_decoy.md)
   - [`scenarios/adversarial_swarm.md`](scenarios/adversarial_swarm.md)
