# multi_uav_coop_decoy 评分标准 (Spec 025)

## 示例目标
3 架无人机协同搜索并跟踪 3 个运动真目标（另有 15 个静止诱饵）。**每个真目标被 ≥1 架
UAV 连续跟踪 20 s 以上**算完成，编队再转向下一个目标，直至所有目标完成。

## 评分参数

| 参数 | 值 | 说明 |
|---|---|---|
| K（协同阈值） | **1（默认，优化策略：扇区分配+诱饵分类器，单机完成每目标）** | 一个目标须被 ≥1 架 UAV 持续跟踪才计完成（K 已固定为 1，单机即满足） |
| 连续达成基准 `dwell_target` | 20 s | 每目标连续协同跟踪 20 s → 该目标 completed |
| 中断容忍 `grace` | 2 s | 短暂中断 ≤2 s 回补；>2 s 清零重来 |
| 满协同阈值 `full_coop_K` | 3 | 3 架同跟同一目标的 tick 计入 `full_coop` 加分维度 |

## 评分维度与权重

总分 = Σ(权重 × 维度分)，各维度 0–100。

| 维度 | 权重 | 计算 |
|---|---|---|
| `completion` 目标完成率 | 0.30 | `100·completion_rate` —— 已达成(≥1机同跟20s)的目标占比 |
| `track_quality` 跟踪质量 | 0.20 | `100·`有效跟踪期间平均 `confidence`（居中高、偏离低） |
| `time_to_all` 全目标达成耗时 | 0.20 | 全完成→`100·max(0,1−耗时/duration)`；否则 0 |
| `misid` 误识惩罚 | 0.10 | `100·max(0, 1 − misid_rate/0.3)` —— 误跟诱饵越少越高 |
| `comm` 协同通信效率 | 0.10 | `100·comm_delivered/comm_sent` |
| `full_coop` 满协同加分 | 0.10 | `100·(≥3机同跟同一目标的 tick 占比)` |

## 通过门槛
`completion_rate == 1.0`（全部 3 个目标达成）**且**总分 ≥ 75。

## 跟踪质量维度（track_quality）
复用引擎 `detection.confidence = 1 − 离轴角/半视场`：目标在画面正中心 → 1.0，偏离 → 降低，
边缘 → 0。评分取所有有效跟踪（真目标、非诱饵）期间的平均 confidence × 100。

## 连续跟踪状态机（每个真目标独立）
- **有效跟踪者** = `detected=True`、锁定真目标（位置最近邻匹配，非诱饵）、未 destroyed 的 UAV。
- **连续累计中** = 该目标当前有效跟踪者数 ≥ 1（K=1，单机即可）。
- 连续累计 dwell；短暂中断(≤grace)不清零、回补；>grace 清零、`resets++`。
- `dwell ≥ 20 s` → 该目标 `completed`（锁定）。目标轮转天然支持：达成后不再判定，编队聚焦未完成目标。

## 运行
```bash
# K=2（赛题二评分规则：2 架 UAV 同时持续跟踪 20s 才摧毁）
python -m examples.multi_uav_coop_decoy.run --start-sim --duration 120
```
输出 `output/run_<ts>.evaluation.json`：`total_score` / `dimension_scores` / `per_target`（每目标
completed/max_dwell_run/resets）/ `score_timeline`。

## 注意事项
- **K 已固定为 1**：单架 UAV 持续跟踪同一真目标 20s 即算完成，无需多机协同同跟。
  `coop_controller` 的 yield 逻辑（一架锁定后其他让出）不影响 K=1 下的完成度。
- 引擎不发布目标 uid，"哪架 UAV 跟哪个目标"用 `detection.target_position` 位置最近邻匹配；
  诱饵被误识时 target_position 指向诱饵 → 自动判为非有效跟踪并计入 `misid`。
