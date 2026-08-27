# UAV 搜索与跟踪小车示例

控制一架 `FixedWingUAV`（带云台相机）自动搜索并跟踪一辆 `TargetVehicle`。

## 5 分钟上手

```bash
# 终端 1：启动 Redis
redis-server

# 终端 2：构建并启动仿真器（如果尚未构建）
cmake -B build && cmake --build build
./build/opensim-sim --config examples/uav_search_track_car/config/scenario.json

# 终端 3：运行示例
pip install redis pyyaml
python -m examples.uav_search_track_car.run --duration 60
```

期望输出：
```text
[run] first state @ sim_time=0.05
[run] control loop @ 10.0 Hz for 60.0 sim-seconds
  t=  0.0  mode=SEARCH  uav=(27.00000, 125.00000, 300m)  detected=False
  ...
  t= 23.5  mode=TRACK   uav=(...)  detected=True
SCENARIO COMPLETE
  search time    : 23.5 s
  track total    : 36.5 s
  track in view  : 91.2 %
  mode switches  : 1
  metrics json   : .../output/run_20260613_153012.json
  trace csv      : .../output/run_20260613_153012.csv
```

## 一键启动（自动启动仿真器）

```bash
python -m examples.uav_search_track_car.run --start-sim --duration 60
```

## 调参

修改 `config/algorithm.yaml`，重新运行即生效。5 个核心参数：

| 参数 | 范围 | 含义 |
|---|---|---|
| `mode` | `spiral` / `grid` | 搜索模式 |
| `search_radius` | 50–5000 m | 搜索半径 |
| `search_altitude_agl` | 50–1500 m | 搜索高度 |
| `sweep_period` | 0.5–30 s | 云台扫掠周期 |
| `loiter_radius` | 50–2000 m | 跟踪 loiter 半径 |

## 替换算法

实现 `Controller` 子类，30 行内：

```python
from search_track.controller import Controller
from search_track.commands import CommandTarget, ControlCommand

class MyController(Controller):
    def decide(self, state, dt):
        if state.detection.detected:
            tgt = state.detection.target_position
            # 算 LOS、算 loiter、下发命令
            ...
        return []
```

然后：
```bash
python -m examples.uav_search_track_car.run --controller my_pkg.my_module:MyController
```

## 算法结构

```
search_track/
├── state.py              # SimState 数据类
├── commands.py           # ControlCommand 数据类
├── controller.py         # Controller 抽象基类
├── fsm_controller.py     # 默认 FSM 控制器（搜索 ↔ 跟踪）
├── search_strategies.py  # 螺旋搜索 + 云台扫掠
├── tracking_strategy.py  # 跟踪 loiter + LOS 云台
├── geometry.py           # haversine / bearing / LOS
├── metrics.py            # 性能指标记录
├── config.py             # YAML 配置加载
└── client.py             # Redis 客户端封装
```

## 输出

每次运行在 `output/` 下生成：
- `run_<ts>.json` — 汇总指标（搜索时间、跟踪占比、模式切换次数）
- `run_<ts>.csv`  — 每 tick 状态轨迹

## 测试

```bash
cd examples/uav_search_track_car
pytest tests/ -v
```

不需要 Redis 或运行中的仿真器（用 `MockSimClient` 注入伪状态）。
