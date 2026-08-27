# uav_search_track_car 评分标准 (Spec 025)

> ⚠️ **本文档为旧版示例评分说明,竞赛评分以 [`competition/docs/评分说明.md`](../../competition/docs/评分说明.md) 为权威。**
> (本单机示例参数本身仍可参考。)

## 示例目标
单架无人机快速搜索地面运动目标，并连续、稳定地把目标保持在相机视场中心跟踪。

## 评分参数

| 参数 | 值 | 说明 |
|---|---|---|
| K（协同阈值） | 1 | 单机示例，无需协同 |
| 连续达成基准 `dwell_target` | **300 s（5 分钟）** | 连续跟踪满 5 分钟 → completion 维度 100 分 |
| 中断容忍 `grace` | 2 s | 短暂丢失 ≤2 s 不清零、恢复时回补；>2 s 清零重来 |

## 评分维度与权重

总分 = Σ(权重 × 维度分)，各维度 0–100。

| 维度 | 权重 | 计算 |
|---|---|---|
| `search` 搜索耗时 | 0.20 | `100·max(0, 1 − search_time/duration)` —— 越快发现越高 |
| `completion` 连续达成 | 0.30 | `100·min(1, max_dwell_run/300)` —— 连续 5 分钟满分（线性） |
| `track_quality` 跟踪质量 | 0.25 | `100·`有效跟踪期间平均 `confidence`（目标居中 1.0、画面边缘 0） |
| `stability` 稳定性 | 0.15 | `0.5·100·(1 − min(1, resets/5)) + 0.5·100·track_in_view_fraction` |
| `time_to_all` 达成耗时 | 0.10 | 达成→`100·max(0, 1 − 耗时/duration)`；未达成→0 |

## 通过门槛
连续跟踪达成满分（`dwell ≥ 300 s`）**且**总分 ≥ 70。

## 跟踪质量维度（track_quality）
直接复用引擎 `gimbal_tracking.detection.confidence`：`confidence = 1 − 离轴角 / 半视场`。
- 目标在相机画面**正中心** → `confidence = 1.0`
- 偏离中心 → 逐渐降低
- 到画面边缘 → `0`

评分取有效跟踪期间的平均 confidence × 100，鼓励把目标稳定保持在画面中心。

## 连续跟踪状态机
- **有效跟踪** = `detected=True` 且锁定真目标（位置最近邻匹配到真目标，非诱饵）。
- 连续累计 dwell；**短暂中断（≤grace）不清零**，恢复时把中断时长回补进连续累计；中断 >grace 则清零、`resets++`。
- `dwell ≥ 300 s` → 该目标 `completed`（锁定，后续丢失也不回退）。

## 运行
```bash
# 默认 --duration 60 不足以达成 5 分钟连续跟踪；评估达成请用 ≥360 s
python -m examples.uav_search_track_car.run --start-sim --duration 360
```
输出 `output/<run_id>.evaluation.json`：`total_score` / `dimension_scores` / `per_target` /
`score_timeline`（每帧分数-时间曲线）。控制台 banner 同时打印 EVALUATION 块。

## 注意事项
- 引擎**不发布**云台锁定目标的 uid，"UAV 跟的是哪个目标"用 `detection.target_position` 的
  位置最近邻匹配判定（本示例只有 1 个真目标，匹配唯一）。
- 单机示例无诱饵，`misid` 恒为 0。
