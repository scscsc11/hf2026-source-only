# Session 交接文档：优化 multi_uav_coop_decoy 控制算法

> **分支**：`optimize/multi-uav-coop-decoy-ctrl`（基于 `develop`）
> **日期**：2026-06-15
> **状态**：✅ **已完成** — 35/35 测试通过，端到端运行 3/3 目标命中，comm sent/delivered 177/326

---

## 一、任务目标

1. **修复 UAV 原地盘旋**：3 架 UAV 应分散到地图不同区域协同搜索，而不是叠在一起绕同一螺旋盘旋
2. **让地面目标机动**：3 个 TargetVehicle 应沿预设路径移动（不同速度、不同轨迹），以检验搜索/跟踪算法应对运动目标的有效性

---

## 二、根因分析（已确认）

### UAV 原地盘旋
- `search_strategies.py:21` 的 `spiral_next_waypoint`：`spiral_growth_rate=30 m/圈`（每 12 秒一圈），120 秒只能爬到 ~300m 半径，远小于 `search_radius=800`
- 三架 UAV 各以起飞点为 base 做螺旋（`fsm_controller.py:80`），起飞点几乎重合（差 0.5km），角速度相同 → 三机叠在一条线上
- 没有任何区域分工机制

### 目标不机动
- `TargetTrajectoryComponent::update()`（C++ `target_trajectory_component.cc:82`）受 `is_navigating` 门控（Feature 007 auto-static）
- `TargetVehicle::init()`（C++ `target_vehicle.cc:26`）强制 `is_navigating_=false`
- 只有运行时下发 `set_trajectory` 命令才会把 `is_navigating_` 置 true（`target_vehicle.cc:114`）
- `run.py` 从未下发任何目标轨迹命令

---

## 三、已完成的改动

### 新文件（未 git add）
| 文件 | 说明 |
|------|------|
| `examples/multi_uav_coop_decoy/search_track/sector_search.py` | 扇区搜索几何：`sector_waypoint(t, params, uav_index, n_uavs)` + `destination_point()` helper + `sector_bearing/radius` 辅助函数。3 架 UAV 把 360° 均分为 3 个扇区，各自在扇区内做扩张扫描（triangle wave + 线性半径增长） |
| `examples/multi_uav_coop_decoy/tests/test_sector_search.py` | 11 个测试：destination_point 往返一致、半径线性增长+钳位、扇区 bearing 不越界、不同 UAV 不同 bearing、waypoint 扇区内+半径正确、随时间扩张 |
| `examples/multi_uav_coop_decoy/tests/test_run_inject.py` | 7 个测试：scenario 读取轨迹、inject 下发 set_speed+set_trajectory（顺序正确）、dry-run 不下发、skip 不存在的 target、fleet index 分配 |

### 已修改文件
| 文件 | 改动摘要 |
|------|----------|
| `search_track/coop_controller.py` | 新增 `set_fleet_index()`、`set_sector_center()`；`configure()` 读取 sector 字段；`decide()` SEARCH 分支：调 FSM 驱动状态机计数但用 `_search_commands_sector()` 替代螺旋命令；TRACK 不变 |
| `search_track/config_reuse.py` | `load_algorithm_config()` 把 6 个 017 新字段（`use_sector_search` 等）从 yaml 顶层 merge 进 `AlgorithmConfig.advanced` |
| `config/algorithm.yaml` | 新增扇区搜索字段（`use_sector_search: true`、`search_sweep_time: 90`、`sector_angular_speed_dps: 25`、`sector_center_*: null`）；`spiral_growth_rate` 从 30 调到 120（回退安全网） |
| `config/scenario.json` | 3 个 target 各加了 4 点 waypoints（不同速度/轨迹：target_1 低速往返 5m/s、target_2 中速折线 9m/s、target_3 中速三角 12m/s） |
| `run.py` | `_build_controllers()` 新增 fleet index 注入 + UAV 质心自动填充 sector center；新增 `_load_target_trajectories()` 和 `_inject_target_trajectories()` 在主循环前激活目标轨迹 |
| `tests/test_coop_controller.py` | 新增 3 个测试：`test_sector_search_fans_uavs_apart`、`test_sector_search_can_be_disabled_falls_back_to_spiral`、`_configured_controller_with()` helper |

### 未改动
- **C++ sim 核心**（`target_vehicle.cc`、`target_trajectory_component.cc`、`command_router.cc`）
- **016 基线**（`examples/uav_search_track_car/`）
- **`FsmSearchTrackController` / `search_strategies.py`**

---

## 四、测试 & 端到端验证

### 单元测试
```
35 passed
```

### 端到端运行（live sim + Redis + 3D 可视化）
```
sim duration       : 60.0 s
wall duration      : 111.1 s
UAVs               : 3
true targets found : 3/3
tracking ticks     : 10690  (其中 4040 misid)
comm sent/delivered: 177/326
```

---

## 五、本会话补充修复（接续上一 session 之后）

上一 session 收尾时 4 个测试失败，状态里标注"4 failed, 26 passed"。本会话（接续）补做：

### 1. `coop_controller.py` 注入 `unique_id`（修 016 路径留下的命名差异）
- 根因：016 单 UAV 示例里实体命名为 `"uav"`，017 三机命名为 `uav_alpha/bravo/charlie`，`to_publish()` 用 `target="uav"` 在 017 里 NAME 回退全失败 → UAV 收不到 `set_destination`，进入 TRACK 后仍走扇区/螺旋航迹。
- 修复：`decide()` 发布前给每个 `ControlCommand` 注入 `unique_id=self.my_uid`，C++ 路由按 uid 优先命中。

### 2. 目标位置锚点检测（修 TRACK 阶段跟踪丢失不触发）
- 根因：`auto_track: true` 让 C++ 云台每 tick 自动锁最近目标，可能从真目标切到诱饵；算法层只看到 `detected` 布尔，没有"目标身份切换"信号，`_consecutive_lost` 永远清零，TRACK→SEARCH 转换永不触发。
- 修复：`CoopController` 增加 `self._track_anchor_lat/lon` + `track_jump_threshold_m=80`；TRACK 阶段用 `haversine_m` 比对"锚点 vs 当前 detection 位置"，跳变 >80m 就构造 `detected=False` 影子 detection 喂给 FSM，自然走 `_consecutive_lost=k_lost=30` → mode 退回 SEARCH。
- 锚点设置/清理：SEARCH→TRACK 边沿设锚；TRACK→SEARCH 边沿清锚；位置在阈值内则平滑滚动以容忍真目标自身运动。

### 3. 通信 0/0 路由修复（plan 遗漏 + 三处 C++ bug）
- 现象：端到端跑出来 `comm sent/delivered: 0/0`，所有 UAV 静默。
- 根因 4 个互相叠加：
  1. `main.cc` 缺 `set_entity_handler_by_unique_id` 桥接（plan 漏列）→ UAV 命令 NAME 回退也命中不了 uid 路由
  2. `main.cc` 缺 `set_comm_handler` 桥接 + `wire_comm_peer_resolver()` → 路由器 `kComm` 分支派发后没有 CommComponent 能接
  3. `CommComponent::state()` 返回 `{ "comm": {...} }` 双重嵌套 → `sim:state` 出现 `entity.comm.comm.{stats}` → Python 端 `entity.comm.stats` 拿不到数据
  4. `broadcast()` 内 `peer_resolver_(entry["name"])` 把 name 当 uid 喂，命中失败；类型字符串 `"fixed_wing_uav" != "uav"`（factory 实际 emit `"uav"`）→ 过滤掉了所有 peer
- 修复：
  - `src/main.cc` 注册 `set_entity_handler_by_unique_id` / `set_comm_handler`，调用 `engine.wire_comm_peer_resolver()`
  - `src/engine/simulation_engine.{h,cc}` 新增 `handle_comm_command()` + `wire_comm_peer_resolver()`
  - `src/components/comm_component.cc`：`state()` 返回扁平 dict；broadcast 用 `entry["unique_id"]` 喂 resolver；类型过滤改 `"uav"`

### 4. 4 个测试失败收尾
- `_search_commands_sector` 返回类型从 `list[dict]` 改为 `list[ControlCommand]`，去掉内部 `.to_publish()` 调用
- `test_run_inject.py` 补齐 `Attitude(yaw,pitch,roll)` / `velocity` / `heading` 必填字段
- 全量 35/35 通过

---

## 六、关键文件速查

| 关键文件 | 路径 |
|----------|------|
| 扇区搜索几何（新） | `examples/multi_uav_coop_decoy/search_track/sector_search.py` |
| 协同控制器（改） | `examples/multi_uav_coop_decoy/search_track/coop_controller.py` |
| Runner（改） | `examples/multi_uav_coop_decoy/run.py` |
| 算法配置（改） | `examples/multi_uav_coop_decoy/config/algorithm.yaml` |
| 场景配置（改） | `examples/multi_uav_coop_decoy/config/scenario.json` |
| FSM 控制器（016，未改） | `examples/uav_search_track_car/search_track/fsm_controller.py` |
| 搜索策略（016，未改） | `examples/uav_search_track_car/search_track/search_strategies.py` |
| C++ 轨迹组件（未改） | `src/components/target_trajectory_component.cc` |
| C++ TargetVehicle（未改） | `src/entities/target_vehicle.cc` |
| C++ 命令路由（未改） | `src/interface/command_router.cc` |
| config 加载器（016） | `examples/uav_search_track_car/search_track/config.py` |
| 命令格式定义（016） | `examples/uav_search_track_car/search_track/commands.py` |
| Python 环境 | `C:\Python314\python.exe`（3.14.3，pytest 9.0.2） |
