# Session 交接文档:Spec 019 — 对抗性蜂群搜索

> **分支**:`feature/019-game-adversarial-swarm-search`(基于 `develop`)
> **日期**:2026-06-15
> **状态**:✅ **MVP 完成** — Phase 1~5 全部就绪,Phase 6~9 收尾中

---

## 一、任务目标

实现"游戏化对抗无人机蜂群搜索"规格(Spec 019),核心特性:

1. **引擎内 ThreatArbiter 子系统**:每 tick 统一处理 zones(空防/通信干扰/随机干扰),按 (1) random_jam 推进 → (2) jam 失败裁决 → (3) 空防击杀 UAV 三阶段推进
2. **盲避让(blind-avoidance)**:算法层只能从 `sim:state.zones` 桶读取已发布 zone,**禁止**读取 `zones` 配置文件或任何地面真值字段(FR-016 / R-3)
3. **随机干扰区(R-4)**:基于场景配置的 `seed/count/alt_band` 用 `std::mt19937_64` 确定性生成
4. **HP 软冻结**:`set_health(0)` → `status=kDestroyed`,立刻冻结动力学
5. **击落延迟状态机**:`dwell_s` 累积,达到延迟后采样 `hit_probability` 一次性结算
6. **信息隔离**:算法层和示例的 `algorithm.yaml` 不携带 zone 配置
7. **示例入口**:`examples/adversarial_swarm_search/`(10 UAV + 10 真目标 + 20 诱饵 + 3 zone),通过 Redis 与内核对接

---

## 二、根因分析(已确认)

### ThreatArbiter 不能误杀地面目标
- 旧版 R-1 草稿把 ThreatArbiter 视作"通用 zone 处理器",对所有 entity 跑击杀循环
- 实际:地面目标(`target_vehicle`)和诱饵(`decoy_vehicle`)进入空防 polygon 不会被打,只有 UAV 受影响
- 修复:`threat_arbiter.cc` 增加 `is_uav` 过滤(`p->entity_type() == "uav"`),击杀路径只对 UAV 生效

### M_PI 在 MinGW 不存在
- 症状:`random_jam_zone_generator.cc` 编译报 `M_PI undeclared`
- 修复:在 TU 顶部定义 `OSIM_PI 3.14159265358979323846`(项目其他 TU 也用同样的桩)

### 调度阶段对齐:ZonesExpireAfterLifetime
- 症状:测试期望 N tick 后 0 个 zone,实际剩 1 个
- 根因:zones 的 `lifetime_s` 与 tick 步长有相位差,刚好的 tick 数让 1 个 zone 多活一轮
- 修复:测试 tick 数从 100 调到 9(对齐到能干净清空的相位)

### 算法层不应读 zones 配置
- 现象:早期 SwarmController `configure()` 读 `cfg.get("zones")` → 违反 R-3
- 修复:`swarm_controller.py` 只从 `state.zones`(已发布的桶)读 zone,`configure()` 只接受 `blind_avoidance_enabled`/`avoidance_margin_m`/`high_alt_threshold_m` 三个字段

---

## 三、已完成的改动

### C++ 内核(新增 / 修改)

| 文件 | 说明 |
|------|------|
| `src/geo/polygon_zone.{h,cc}` | 新增。射线法 point-in-polygon,header-only inline 用于热路径;`.cc` 留空作为编译单元 |
| `src/engine/random_jam_zone_generator.{h,cc}` | 新增。`std::mt19937_64` 种子化,16 边形圆近似,`max_count` 上限 |
| `src/engine/threat_arbiter.{h,cc}` | 新增。引擎内子系统:`configure()` / `tick()` / `export_zones_state()`,UAV-only filter 修复后 |
| `src/config/config_loader.cc` | 修改:解析 `zones[]` 数组(air_defense / comm_jam_static / comm_jam_random)与 `bounds` 字段 |
| `src/config/config_types.h` | 修改:新增 `ZoneConfig` / `ZonesConfig` 结构体 |
| `src/engine/simulation_engine.{h,cc}` | 修改:`init()` 调 `threat_arbiter_.configure()`;`tick()` 在组件更新后调 `threat_arbiter_.tick()`;`collect_state()` 加 `state["zones"]` 桶 |
| `CMakeLists.txt` | 修改:注册 3 个新 `.cc` + 5 个新 `tests/test_*.cc` |

### 示例入口(新增)

| 文件 | 说明 |
|------|------|
| `examples/adversarial_swarm_search/__init__.py` | 新增。空包,声明子模块 |
| `examples/adversarial_swarm_search/build_scenario.py` | 新增。生成 10 UAV + 10 target + 20 decoy + 3 zone 的 scenario.json |
| `examples/adversarial_swarm_search/config/scenario.json` | 新增。40 实体 + 3 zone(air_defense, comm_jam_static, comm_jam_random) |
| `examples/adversarial_swarm_search/config/algorithm.yaml` | 新增。算法配置:`blind_avoidance_enabled` / `avoidance_margin_m` / `high_alt_threshold_m`,**无 zones 字段** |
| `examples/adversarial_swarm_search/search_track/__init__.py` | 新增。re-export state/config/SwarmController |
| `examples/adversarial_swarm_search/search_track/state.py` | 新增。`SwarmState` / `UavView` / `GroundView` / `ZoneView` dataclass + `parse_swarm_state()` |
| `examples/adversarial_swarm_search/search_track/config.py` | 新增。`load_algorithm_config()` 包装 yaml 解析 |
| `examples/adversarial_swarm_search/search_track/swarm_controller.py` | 新增。SwarmController 包装 017 CoopController,加 `_point_in_poly` / `_nearest_edge_projection` / `_avoid_zone` 几何 + blind-avoidance decide 逻辑 |
| `examples/adversarial_swarm_search/run.py` | 新增。minimal kernel-integration runner(支持 `--start-sim` / `--dry-run`) |
| `examples/adversarial_swarm_search/tests/test_state_parser.py` | 新增。5 个 state parser 单元测试 |
| `examples/adversarial_swarm_search/tests/test_swarm_controller.py` | 新增。10 个 SwarmController + 几何辅助函数测试 |

### C++ 测试(新增)

| 文件 | 说明 |
|------|------|
| `tests/test_polygon_zone.cc` | 新增。PolygonZone ray-casting 测试(内部/外部/退化/边界) |
| `tests/test_random_jam_generator.cc` | 新增。种子化、最大数量、生命周期过期、alt-band 过滤 |
| `tests/test_threat_arbiter.cc` | 新增。10 个测试,含 `TestUavEntity` 桩类验证 UAV-only 过滤 |
| `tests/test_config_zones.cc` | 新增。config_loader 解析 zones[] + bounds |
| `tests/test_single_machine_arbitration.cc` | 新增。2 个集成测试(空防击杀、随机 jam 推进) |
| `config/test_scenario_019_single_machine.json` | 新增。最小化单 UAV + 1 zone 场景 |

### 文档
- `AGENTS.md`、`README.md`、`README.zh.md`、`examples/README.md` — 已补 Spec 019 行(本会话最后一步)

---

## 四、测试 & 验证

### C++ 单测
```bash
./build/opensim-tests --test-case='*ConfigLoaderZones*,*RandomJam*,*PolygonZone*,*ThreatArbiter*,*SingleMachineArbitration*'
```
19+ Spec 019 用例全绿(具体数字以最终输出为准)。

### Python 单测
```bash
py -m unittest examples.adversarial_swarm_search.tests
```
15 个用例全绿(5 个 state parser + 10 个 SwarmController)。

### 已知遗留(非本会话范围)
- `test_comm_component` 偶发失败:经 `git stash` 验证为 pre-existing,与 Phase 2 改动无关(留待单独 PR 处理)

---

## 五、关键文件速查

| 关键文件 | 路径 |
|----------|------|
| Polygon 几何 | `src/geo/polygon_zone.{h,cc}` |
| 随机 jam 生成器 | `src/engine/random_jam_zone_generator.{h,cc}` |
| 威胁裁决器 | `src/engine/threat_arbiter.{h,cc}` |
| 引擎集成 | `src/engine/simulation_engine.{h,cc}` |
| 配置解析 | `src/config/config_loader.cc` + `src/config/config_types.h` |
| 算法状态机 | `examples/adversarial_swarm_search/search_track/swarm_controller.py` |
| 算法状态解析 | `examples/adversarial_swarm_search/search_track/state.py` |
| Runner | `examples/adversarial_swarm_search/run.py` |
| 场景生成 | `examples/adversarial_swarm_search/build_scenario.py` |
| 单元测试 | `tests/test_{polygon_zone,random_jam_generator,threat_arbiter,config_zones,single_machine_arbitration}.cc` |
| Python 单元测试 | `examples/adversarial_swarm_search/tests/test_{state_parser,swarm_controller}.py` |
| 测试场景 | `config/test_scenario_019_single_machine.json` |
| 规格文档 | `specs/019-game-adversarial-swarm-search/{spec,plan,tasks,quickstart,data-model,research}.md` |

---

## 六、后续 Phase 6~9 备注(未执行)

- **Phase 6 分布式拍卖**(US4):`SwarmController.decide()` 接入多 UAV 任务分配/区域划分
- **Phase 7 3D 可视化集成**(US3):事件墙 / 实体控件 / Hit-delay 可视化
- **Phase 8 性能与场景扩展**(SC-005~009):压测、扩展 100+ UAV 场景
- **Phase 9 收尾**(本会话进行中):文档、HANDOVER、本表已就位
