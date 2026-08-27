# adversarial_swarm_search 评分标准 (Spec 025)

## 示例目标
10 架无人机在对抗环境（空防区 + 通信干扰）下协同搜索并跟踪 10 个真目标（另有 20 个诱饵），
**每个真目标被 ≥1 架 UAV 连续跟踪 20 s** 算完成，直至全部完成；**同时尽量保全 UAV**
（留存越多越好）。

## 评分参数

| 参数 | 值 | 说明 |
|---|---|---|
| K（协同阈值） | **1** | K 已固定为 1，单机持续跟踪 20s 即完成（无需协同同跟） |
| 连续达成基准 `dwell_target` | 20 s | 每目标连续协同跟踪 20 s → completed |
| 中断容忍 `grace` | 2 s | 短暂中断 ≤2 s 回补；>2 s 清零重来 |

## 总分公式（跟踪为主 + 跟踪质量 + 存活加成）

```
total_score = 100 · ( 0.5·completion_rate + 0.2·track_quality + 0.3·alive_rate )
```

| 分量 | 权重 | 含义 |
|---|---|---|
| `completion` 完成度 | 0.5 | `completion_rate` —— 已达成(≥1机同跟20s)目标占比 |
| `track_quality` 跟踪质量 | 0.2 | 有效跟踪期间平均 `confidence`（居中高、偏离低） |
| `alive` 存活率 | 0.3 | `alive_rate` —— 期末存活 UAV / 初始 UAV |

> 诊断项（不计入总分，见 evaluation.json）：误识率、避让有效性、每目标详情。

## 通过门槛
`completion_rate == 1.0` **且** `alive_rate ≥ 0.5` **且**总分 ≥ 70。

## 跟踪质量维度（track_quality）
复用引擎 `detection.confidence = 1 − 离轴角/半视场`：目标在画面正中心 → 1.0，偏离 → 降低，
边缘 → 0。评分取所有有效跟踪（真目标、非诱饵、未 destroyed）期间的平均 confidence × 100。

## 连续跟踪状态机（每个真目标独立）
- **有效跟踪者** = `detected=True`、锁定真目标（位置最近邻，非诱饵）、未 destroyed 的 UAV。
- **连续累计中** = 该目标当前有效跟踪者数 ≥ 1（K=1，单机即可）。
- 连续累计 dwell；短暂中断(≤grace)不清零、回补；>grace 清零、`resets++`。
- `dwell ≥ 20 s` → 该目标 `completed`（锁定）。

## 总分非单调说明
`completion_rate` 单调不减（已达成目标锁定），但 `alive_rate` 单调不增（UAV 只被击落不复活），
两者方向相反 → 总分会上下波动（完成度涨时升、被击落时降）。这反映"截至当前的累计质量"，
是设计意图。`score_timeline` 可观察这条曲线。

## 运行
```bash
python -m examples.adversarial_swarm_search.run --start-sim --duration 60
```
输出 `output/run_<ts>.evaluation.json`：`total_score` / `dimension_scores` / `per_target` /
`alive_rate` / `score_timeline`。

## 注意事项
- K 已固定为 1：初始 10 架也是 K=1，单机持续跟踪 20s 即完成（不再自适应）。
- 对抗 runner 有 self-termination（自身被毁即退出本进程）；评分基于已收集的 tick 序列，
  进程提前终止时对已 observe 的数据出分。
- 空防区进区滞留 2 s 必击落（`hit_probability=1.0`），避让决定存活率。
