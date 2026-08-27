"""A* navigation startup injector for competition scenarios.

使用引擎端的 C++ PathPlanner::findPath 规划路线。流程（批量版）：
1. Python 发 ``astar_plan_batch`` 命令（一次性提交一辆车的所有段）
2. 引擎在一个 tick / idle-body 内循环执行每段 A*，结果数组写入
   ``entity_state["astar_plan_results"]``（复数，每段一个元素）
3. Python 读取结果数组，拼接成整条路线，用 ``set_trajectory`` 一次性发给车辆

历史对比：逐段版（astar_plan）每段一次命令 + 一次 sim:state 轮询，
adversarial_swarm ~260 段 → 启动 30~40s；批量版 260 段 → 30 次实体级 RPC。

提供两个注入函数：
  * inject_astar_target: 注入真目标（TargetVehicle），从 points.json 随机选路
  * inject_astar_decoy:   注入诱饵（DecoyVehicle），从 random_routes_20.json 随机选路
"""
from __future__ import annotations

import json
import random
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

CMD_CHANNEL = "sim:commands"
STATE_CHANNEL = "sim:state"


def _load_routes(path: Path) -> List[dict]:
    try:
        # utf-8-sig 容忍 BOM: routes json 可能被 PS/编辑器写入 BOM(见 CLAUDE.md 跨平台坑清单#1)
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        return data.get("Paths", [])
    except Exception:
        return []


def _build_waypoints(route: dict) -> List[dict]:
    wps: List[dict] = []
    start = route.get("Start", {})
    if start:
        wps.append({
            "lat": float(start["Latitude"]),
            "lon": float(start["Longitude"]),
            "wait": float(start.get("WaitTime", 0.0)),
            "label": "Start",
        })
    for i, wp in enumerate(route.get("Waypoints", [])):
        wps.append({
            "lat": float(wp["Latitude"]),
            "lon": float(wp["Longitude"]),
            "wait": float(wp.get("WaitTime", 0.0)),
            "label": f"WPT[{i}]",
        })
    end = route.get("End", {})
    if end:
        wps.append({
            "lat": float(end["Latitude"]),
            "lon": float(end["Longitude"]),
            "wait": float(end.get("WaitTime", 0.0)),
            "label": "End",
        })
    return wps


def _publish_cmd(client, unique_id: str, cmd: str, params: dict) -> int:
    msg = {
        "unique_id": unique_id,
        "cmd": cmd,
        "params": params,
    }
    if hasattr(client, "publish_raw"):
        return client.publish_raw(msg)
    if hasattr(client, "publish"):
        from .commands import Command
        c = Command(verb=cmd, params=params)
        return client.publish(unique_id, c)
    if hasattr(client, "_redis") and client._redis is not None:
        return client._redis.publish(CMD_CHANNEL, json.dumps(msg))
    raise RuntimeError("Client does not support command publishing")


def publish_regenerate_zones(client) -> int:
    """通知引擎:所有靶标车路线已注入完毕,请基于真实路线重新生成静态 zone。

    背景:引擎 init() 阶段生成 zone 时,prepare_scenario 已清空靶标车的预设
    waypoints,真实 A* 路线要等到 inject_startup 才通过 set_trajectory 注入。
    所以初次生成的 zone 拿到的是空 routes,退化为 random 分支,没压在真实路径
    上。本命令让引擎在 sim 线程(deferred,与 tick 串行)基于注入后的组件状态
    重配 zone,使击毁区/静态干扰区真正落在目标必经路线上。

    unique_id 留空:command_router 只凭顶层 cmd 字段判定引擎命令,不看 unique_id。
    无 generate 块的场景(如无 zone 配置)重配是 no-op,调用无害。
    """
    return _publish_cmd(client, unique_id="", cmd="regenerate_zones", params={})


# 段级进度回调签名: (本实体已规划段数, 本实体总段数, uid)。
# 用于把 inject_startup 串行规划路线的进度上报到前端进度条。
ProgressCb = Optional[Callable[[int, int, str], None]]

# 路线规划进度映射到进度条 85%-98% (引擎加载占 0%-85%, 前端缩放; 98%-100%
# 由 RunnerBase 主循环就绪帧占用)。
PROGRESS_ROUTE_START = 0.85
PROGRESS_ROUTE_END = 0.98
PROGRESS_CHANNEL = "sim:progress"


def make_route_progress_cb(client, total_units: int, log=print):
    """构造一个车辆级进度回调, 把"规划车辆路线"进度发到 sim:progress 频道。

    三个赛题 runner 的 inject_startup 调用本函数, 传入 *所有实体合计* 的
    规划单元数 total_units (批量版 = 车辆数, 由 count_injectable_vehicles 统计);
    返回的闭包由 inject_astar_target/decoy 在每辆车规划完后调用
    ``cb(1, 1, uid)`` (批量版一次规划整条路线, 每车上报一次)。

    线程安全的 per-uid 绝对计数: 闭包维护 ``done_by_uid: {uid: int}``,
    每次调用直接把 ``done_in_entity`` 赋值给本 uid 的键 (绝对值, 非自增)。
    全局已完成数 = ``sum(done_by_uid.values())``, 映射到 0.85-0.98 的 pct。
    由于每个 uid 只由规划该实体的线程写入 (CPython dict 键赋值是原子的),
    即便闭包被多个实体/线程共享 (如 inject_startup_concurrent) 也不会丢更新。

    total_units <= 0 时返回 no-op (无可规划单元, 不发进度)。
    """
    if total_units <= 0:
        return lambda done, total, uid: None
    done_by_uid: Dict[str, int] = {}

    def cb(done_in_entity: int, total_in_entity: int, uid: str) -> None:
        # 绝对计数: 直接记录本 uid 当前的累计完成单元数 (而非 +1 自增),
        # 避免多线程共享闭包时的丢更新竞态。批量版每车上报 done=1。
        done_by_uid[uid] = done_in_entity
        # 快照 values 再求和: 避免其它 worker 新增 uid 键时 CPython 抛
        # "dictionary changed size during iteration" RuntimeError。
        global_done = sum(list(done_by_uid.values()))
        pct = PROGRESS_ROUTE_START + (PROGRESS_ROUTE_END - PROGRESS_ROUTE_START) * (
            global_done / total_units)
        pct = max(PROGRESS_ROUTE_START, min(PROGRESS_ROUTE_END, pct))
        payload = {
            "type": "load_progress",
            "phase": "规划车辆路线",
            "pct": round(pct, 4),
            "detail": f"{global_done}/{total_units} 辆",
        }
        redis = getattr(client, "_redis", None)
        if redis is None:
            return
        try:
            redis.publish(PROGRESS_CHANNEL, json.dumps(payload))
        except Exception as e:
            log(f"[NAV] 进度上报失败: {e}")

    return cb


def count_route_segments(entities: List[dict],
                         routes_by_type: Dict[str, str],
                         route_assignment: Dict[str, str]) -> int:
    """Sum of A* segments (``len(waypoints) - 1``) across all injectable vehicles.

    在 runner 的 inject_startup 里一次性算出 *所有实体合计* 的路线段总数,
    传给 :func:`make_route_progress_cb` 作为 total_segs, 以便规划期间把
    "规划车辆路线" 进度上报到前端进度条。

    参数:
      * ``entities`` — scenario.json 的 entities 列表 (与 inject_startup_concurrent
        接收的同一个列表)。
      * ``routes_by_type`` — {etype: routes_path} 映射, 用于按实体类型选路文件。
        如 ``{"TargetVehicle": points_path, "ground_vehicle": points_path,
        "DecoyVehicle": decoys_path}``。未命中 etype 时按 ``_INJECTABLE_TYPES``
        顺序取第一个已映射的 path 作 fallback; 仍无则跳过。
      * ``route_assignment`` — {uid: route_name}, 由 prepare_scenario 填充。

    返回合计段数。单个实体的路线名查不到 / 路线池为空 / 无航点 → 贡献 0
    (其注入会 no-op, 不影响进度分母)。``_load_routes`` 的结果按 path 缓存,
    避免对同一文件重复解析。
    """
    # 按 path 缓存解析结果: 同一 routes_path 可能被多个 etype 引用。
    routes_cache: Dict[str, List[dict]] = {}

    def _routes_for(path: str) -> List[dict]:
        if path not in routes_cache:
            routes_cache[path] = _load_routes(Path(path))
        return routes_cache[path]

    # fallback path: _INJECTABLE_TYPES 中第一个在 routes_by_type 出现的。
    fallback_path: Optional[str] = None
    for t in _INJECTABLE_TYPES:
        if t in routes_by_type:
            fallback_path = routes_by_type[t]
            break

    total = 0
    for ent in entities:
        etype = ent.get("type")
        if etype not in _INJECTABLE_TYPES:
            continue
        path = routes_by_type.get(etype) or fallback_path
        if not path:
            continue
        uid = str(ent.get("id") or ent.get("name") or "")
        route_name = route_assignment.get(uid)
        if not route_name:
            # 该实体查不到分配路线 → 这里贡献 0 段。但 inject_astar_*/pick_route
            # 会 fallback 到 rng.choice(routes) 仍注入一条, 故实际段数可能超过
            # 本计数 (global_done > total_segs)。生产不变量: route_assignment 由
            # prepare_scenario 完整填充 (所有实体都已分配), 此分支不会命中;
            # make_route_progress_cb 里 pct 的 clamp (max/min 钳到 0.51-1.0) 是
            # 最后一道安全网, 防止 detail 文本出现 "7/5 段" 这类误导显示。
            continue
        route = None
        for r in _routes_for(path):
            if r.get("Name") == route_name:
                route = r
                break
        if route is None:
            continue
        n_wps = len(_build_waypoints(route))
        total += max(0, n_wps - 1)
    return total


def count_injectable_vehicles(entities: List[dict],
                              routes_by_type: Dict[str, str],
                              route_assignment: Optional[Dict[str, str]] = None,
                              ) -> int:
    """统计将要注入 A* 路线的车辆数 (批量版进度回调的分母)。

    与 :func:`count_route_segments` 同样的实体/路线匹配逻辑, 但只数车数
    (每辆可注入车贡献 1), 不累加段数。批量版 ``astar_plan_batch`` 一次规划
    整条路线, 进度按"每完成一辆车 +1"上报, 所以分母用车辆数而非段数。

    逐段版 (count_route_segments) 保留不删: 它仍是未来若需段级进度时的
    参考实现, 且单元测试可能引用。
    """
    route_assignment = route_assignment or {}
    routes_cache: Dict[str, List[dict]] = {}

    def _routes_for(path: str) -> List[dict]:
        if path not in routes_cache:
            routes_cache[path] = _load_routes(Path(path))
        return routes_cache[path]

    fallback_path: Optional[str] = None
    for t in _INJECTABLE_TYPES:
        if t in routes_by_type:
            fallback_path = routes_by_type[t]
            break

    total = 0
    for ent in entities:
        etype = ent.get("type")
        if etype not in _INJECTABLE_TYPES:
            continue
        path = routes_by_type.get(etype) or fallback_path
        if not path:
            continue
        uid = str(ent.get("id") or ent.get("name") or "")
        route_name = route_assignment.get(uid)
        if not route_name:
            continue
        route = None
        for r in _routes_for(path):
            if r.get("Name") == route_name:
                route = r
                break
        if route is None:
            continue
        if len(_build_waypoints(route)) >= 2:
            total += 1
    return total


def _extract_astar_result(ent: dict) -> Optional[dict]:
    if not isinstance(ent, dict):
        return None
    if "astar_plan_result" in ent and isinstance(ent["astar_plan_result"], dict):
        return ent["astar_plan_result"]
    ap = ent.get("astar_planner")
    if isinstance(ap, dict) and "astar_plan_result" in ap:
        if isinstance(ap["astar_plan_result"], dict):
            return ap["astar_plan_result"]
    return None


def _plan_segment(client, uid: str, start_lat: float, start_lon: float,
                  end_lat: float, end_lon: float, timeout: float = 2.0,
                  log=print) -> Optional[List[dict]]:
    """通过引擎的 astar_plan 命令规划一段路径。

    返回路径点列表 [{"lat", "lon"}, ...]，失败返回 None。
    """
    redis = getattr(client, "_redis", None)
    if redis is None:
        log(f"[DEBUG] _plan_segment {uid}: no redis client")
        return None

    _publish_cmd(client, uid, "astar_plan", {
        "start_lat": start_lat,
        "start_lon": start_lon,
        "end_lat": end_lat,
        "end_lon": end_lon,
    })
    log(f"[DEBUG] _plan_segment {uid}: astar_plan command sent")

    deadline = time.time() + timeout
    pubsub = redis.pubsub(ignore_subscribe_messages=True)
    pubsub.subscribe(STATE_CHANNEL)
    try:
        msg_count = 0
        while time.time() < deadline:
            msg = pubsub.get_message(timeout=0.1)
            if not (msg and msg.get("type") == "message"):
                continue
            msg_count += 1
            try:
                state = json.loads(msg["data"])
            except (json.JSONDecodeError, TypeError):
                continue
            found = False
            for key in (uid, str(uid)):
                if key in state:
                    ent = state[key]
                    if isinstance(ent, dict):
                        result = _extract_astar_result(ent)
                        if result and isinstance(result, dict):
                            log(f"[DEBUG] _plan_segment {uid}: found astar_plan_result: success={result.get('success')}, count={result.get('count', 'N/A')}")
                            if result.get("success"):
                                wps = result.get("waypoints", [])
                                return [{"lat": float(p["lat"]), "lon": float(p["lon"])}
                                        for p in wps]
                            return None
                        found = True
                        break
                    found = True
                    break
                ents = state.get("entities", {}) or {}
                if key in ents:
                    ent = ents[key]
                    if isinstance(ent, dict):
                        result = _extract_astar_result(ent)
                        if result and isinstance(result, dict):
                            log(f"[DEBUG] _plan_segment {uid}: found astar_plan_result in entities: success={result.get('success')}")
                            if result.get("success"):
                                wps = result.get("waypoints", [])
                                return [{"lat": float(p["lat"]), "lon": float(p["lon"])}
                                        for p in wps]
                            return None
            if msg_count % 10 == 0:
                log(f"[DEBUG] _plan_segment {uid}: received {msg_count} messages, no result yet")
        log(f"[DEBUG] _plan_segment {uid}: timeout after {timeout}s, received {msg_count} messages")
        return None
    finally:
        try:
            pubsub.close()
        except Exception:
            pass


def _plan_full_route(client, uid: str, waypoints: List[dict], log=print,
                     progress_cb: ProgressCb = None) -> Tuple[List[dict], bool]:
    """用引擎 A* 逐段规划完整路线。

    返回 ``(pts, ok)``: ``ok=True`` 表示所有段都 A* 成功 (全程沿路网);
    任一段失败则 ``ok=False`` 且 ``pts`` 为已规划前缀。比赛要求车必须沿路网,
    失败时禁止直线 fallback, 调用方应直接停车 (set_speed 0)。

    若提供 ``progress_cb``, 则在每段规划成功后调用
    ``progress_cb(i, n_segs, uid)`` (i 为本段 1-based 序号,
    n_segs = len(waypoints) - 1)。回调失败不影响规划 (try/except 保护)。
    """
    if len(waypoints) < 2:
        return [], False

    n_segs = len(waypoints) - 1
    all_pts: List[dict] = []
    # 从 waypoints[1] 开始（跳过起点）
    for i in range(1, len(waypoints)):
        prev = waypoints[i - 1]
        cur = waypoints[i]
        path = _plan_segment(client, uid, prev["lat"], prev["lon"], cur["lat"], cur["lon"], log=log)
        if not path:
            # 比赛要求车必须沿路网: A* 失败禁止直线 fallback, 直接停车。
            log(f"[NAV] {uid} 段 {i} 无路网路径, 停车 (禁止 off-road)")
            return all_pts, False
        for j, p in enumerate(path):
            if j == 0 and all_pts:
                continue
            all_pts.append(p)

        # 本段规划成功 → 上报进度 (i 为本段 1-based 序号)。
        if progress_cb is not None:
            try:
                progress_cb(i, n_segs, uid)
            except Exception as e:
                log(f"[NAV] {uid} 段 {i} 进度回调失败: {e}")

    return all_pts, True


def _extract_batch_result(ent: dict) -> Optional[List[dict]]:
    """从 entity state 里提取 ``astar_plan_results`` (复数) 数组。

    兼容两种嵌套: 顶层 ``astar_plan_results`` 或 ``astar_planner.astar_plan_results``。
    返回段结果列表 ``[{success, waypoints, count, ...}, ...]``; 不存在返回 None。
    """
    if not isinstance(ent, dict):
        return None
    if "astar_plan_results" in ent and isinstance(ent["astar_plan_results"], list):
        return ent["astar_plan_results"]
    ap = ent.get("astar_planner")
    if isinstance(ap, dict) and isinstance(ap.get("astar_plan_results"), list):
        return ap["astar_plan_results"]
    return None


def _plan_route_batch(client, uid: str, waypoints: List[dict],
                      timeout: float = 10.0,
                      log=print) -> Tuple[List[dict], bool]:
    """用引擎的 ``astar_plan_batch`` 命令一次性规划一辆车的完整路线。

    与 :func:`_plan_full_route` (逐段串行 RPC) 的对比:
      * 逐段版: N 段 = N 次 ``astar_plan`` 命令 + N 次 sim:state 轮询 (每段最少
        一个 tick + 100ms 轮询粒度)。adversarial_swarm ~260 段 → 30~40s。
      * 批量版: 1 次 ``astar_plan_batch`` 命令 + 1 次 sim:state 轮询。引擎在
        一个 idle-body/tick 内循环算完所有段, 一次性返回 ``astar_plan_results``
        数组。260 段 → 30 次实体级 RPC (每车一次)。

    返回 ``(pts, ok)``: ``ok=True`` 表示所有段都 A* 成功 (全程沿路网);
    任一段失败则 ``ok=False`` 且 ``pts`` 为已规划前缀。比赛要求车必须沿路网,
    失败时禁止直线 fallback, 调用方应直接停车 (set_speed 0)。

    注意: 单个实体内部段是串行依赖 (段 i 终点 = 段 i+1 起点), 但这些起终点
    在路线池里就是预定义的, 没有真正的拓扑依赖 —— 引擎只是按顺序对每对
    (start, end) 独立跑 A*。所以一次批量提交等价于多次单段提交, 只是省掉了
    N-1 次 Redis 往返和 N-1 个 tick 等待。
    """
    if len(waypoints) < 2:
        return [], False

    redis = getattr(client, "_redis", None)
    if redis is None:
        log(f"[DEBUG] _plan_route_batch {uid}: no redis client")
        return [], False

    # 把相邻 waypoint 对打包成 segments, 字段名与单段 astar_plan 一致。
    segments = []
    for i in range(1, len(waypoints)):
        prev = waypoints[i - 1]
        cur = waypoints[i]
        segments.append({
            "start_lat": float(prev["lat"]),
            "start_lon": float(prev["lon"]),
            "end_lat": float(cur["lat"]),
            "end_lon": float(cur["lon"]),
        })

    _publish_cmd(client, uid, "astar_plan_batch", {"segments": segments})
    log(f"[DEBUG] _plan_route_batch {uid}: astar_plan_batch sent ({len(segments)} segments)")

    # 阻塞轮询 sim:state 取 astar_plan_results (复数) 数组。批量命令一次返回
    # 所有段结果, 所以只等一个结果帧即可。
    deadline = time.time() + timeout
    pubsub = redis.pubsub(ignore_subscribe_messages=True)
    pubsub.subscribe(STATE_CHANNEL)
    try:
        msg_count = 0
        while time.time() < deadline:
            msg = pubsub.get_message(timeout=0.1)
            if not (msg and msg.get("type") == "message"):
                continue
            msg_count += 1
            try:
                state = json.loads(msg["data"])
            except (json.JSONDecodeError, TypeError):
                continue
            results = None
            for key in (uid, str(uid)):
                if key in state and isinstance(state[key], dict):
                    results = _extract_batch_result(state[key])
                    if results is not None:
                        break
                ents = state.get("entities", {}) or {}
                if key in ents and isinstance(ents[key], dict):
                    results = _extract_batch_result(ents[key])
                    if results is not None:
                        break
            if results is None:
                continue
            # 收到完整段结果数组 → 拼接为整条路线。
            log(f"[DEBUG] _plan_route_batch {uid}: got {len(results)} segment results after {msg_count} msgs")
            all_pts: List[dict] = []
            for seg_result in results:
                if not (isinstance(seg_result, dict) and seg_result.get("success")):
                    log(f"[NAV] {uid} 某段无路网路径, 停车 (禁止 off-road)")
                    return all_pts, False
                wps = seg_result.get("waypoints", []) or []
                for j, p in enumerate(wps):
                    if j == 0 and all_pts:
                        continue  # 跳过段间重复的衔接点
                    all_pts.append({"lat": float(p["lat"]), "lon": float(p["lon"])})
            return all_pts, True
        log(f"[DEBUG] _plan_route_batch {uid}: timeout after {timeout}s, {msg_count} msgs")
        return [], False
    finally:
        try:
            pubsub.close()
        except Exception:
            pass


def pick_route(routes_path: str, rng: Optional[random.Random] = None,
               route_name: Optional[str] = None) -> Optional[dict]:
    """单条随机选路（无种子，仅用于无种子的单实体场景）。

    本函数不保证多实体间的路线互不相同 —— 多实体场景请用
    :func:`assign_routes`，它会一次性为所有实体分配互不相同的路线。
    """
    if rng is None:
        rng = random
    routes = _load_routes(Path(routes_path))
    if not routes:
        return None
    if route_name:
        for r in routes:
            if r.get("Name") == route_name:
                return r
    return rng.choice(routes)


def assign_routes(routes_path: str, count: int, seed: int = 0,
                  rng: Optional[random.Random] = None) -> List[dict]:
    """为 ``count`` 个实体一次性分配路线，同次仿真内互不相同。

    返回长度为 ``count`` 的路线列表（实体数 > 路线数时取模回绕，必然
    有重复，但覆盖整池）。

    两种模式：
      * ``seed > 0`` —— **确定**：``routes[(seed + i) % N]``（i=0..count-1）。
        同种子 → 同路线集合，可复现；首实体 offset=0 正好对齐前端
        ``(seed % N) + 1`` 号路线。用于真小车（前端填了蓝方行为随机种子）。
      * ``seed == 0`` —— **随机**：``rng.shuffle(routes)`` 后按序取。
        同次仿真内不同实体互不相同，但每次仿真不同（不可复现）。
        ``rng`` 必须由调用方传入；真小车用种子化 RNG（seed>0 时）或
        未种子化 RNG（seed==0 时），诱饵**永远**用独立未种子化 RNG
        （与种子无关，每次仿真随机）。
    """
    routes = _load_routes(Path(routes_path))
    if not routes or count <= 0:
        return []
    n = len(routes)
    if seed and seed > 0:
        return [routes[(seed + i) % n] for i in range(count)]
    if rng is None:
        rng = random
    shuffled = routes[:]
    rng.shuffle(shuffled)
    return [shuffled[i % n] for i in range(count)]


def inject_astar_target(client, entity: dict, routes_path: str,
                        target_speed: Optional[float] = None,
                        arrive_threshold: float = 15.0,
                        rng: Optional[random.Random] = None,
                        route_name: Optional[str] = None,
                        log=print,
                        progress_cb: ProgressCb = None) -> None:
    if rng is None:
        rng = random

    routes = _load_routes(Path(routes_path))
    if not routes:
        log(f"[NAV] 路线池为空 {routes_path}")
        return

    traj = (entity.get("components", {}) or {}).get("trajectory", {}) or {}
    tp = traj.get("params", {}) or {}
    if target_speed is None:
        target_speed = float(tp.get("speed", 20.0))

    route = None
    if route_name:
        for r in routes:
            if r.get("Name") == route_name:
                route = r
                break
        if route is None:
            log(f"[NAV] 未找到路线 '{route_name}'")
    if route is None:
        route = rng.choice(routes)

    waypoints = _build_waypoints(route)
    if len(waypoints) < 2:
        return

    uid = str(entity.get("id") or entity.get("name") or "10001")
    log(f"[NAV] 实体 {uid} 使用路线 '{route.get('Name', 'unnamed')}'")

    # 用引擎 A* 批量规划整条路线 (一次命令算完所有段; 任一段失败即停车, 禁止 off-road)
    astar_pts, ok = _plan_route_batch(client, uid, waypoints, log=log)
    if not ok or not astar_pts:
        # A* 有失败段 → 直接停车 (比赛要求车必须沿路网, 不走直线 fallback)
        try:
            _publish_cmd(client, uid, "set_speed", {"speed": 0.0})
            log(f"[NAV] {uid} 路线含 A* 失败段, 停车 (禁止 off-road)")
        except Exception as e:
            log(f"[NAV] {uid} 停车命令发送失败: {e}")
        return

    log(f"[NAV] {uid} A* 规划出 {len(astar_pts)} 个路径点 (全程沿路网)")
    try:
        _publish_cmd(client, uid, "set_speed", {"speed": target_speed})
        _publish_cmd(client, uid, "set_trajectory", {
            "waypoints": astar_pts
        })
        log(f"[NAV] {uid} set_trajectory 已发送 ({len(astar_pts)} pts)")
    except Exception as e:
        log(f"[NAV] {uid} 命令发送失败: {e}")

    # 车辆级进度: 批量版一次规划整条路线, 所以这辆车完成时上报一次
    # (done=1, total=1)。make_route_progress_cb 的分母由调用方设为车辆数,
    # 每车 +1 即可推进进度条, 替代原来逐段上报的细粒度。
    if progress_cb is not None:
        try:
            progress_cb(1, 1, uid)
        except Exception as e:
            log(f"[NAV] {uid} 进度回调失败: {e}")


def inject_astar_decoy(client, entity: dict, routes_path: str,
                       decoy_speed: float = 5.0,
                       arrive_threshold: float = 15.0,
                       rng: Optional[random.Random] = None,
                       route_name: Optional[str] = None,
                       log=print,
                       progress_cb: ProgressCb = None) -> None:
    if decoy_speed <= 0:
        return

    if rng is None:
        rng = random

    route = pick_route(routes_path, rng=rng, route_name=route_name)
    if not route:
        return
    waypoints = _build_waypoints(route)
    if len(waypoints) < 2:
        return

    uid = str(entity.get("id") or entity.get("name") or "30001")

    astar_pts, ok = _plan_route_batch(client, uid, waypoints, log=log)
    if not ok or not astar_pts:
        # A* 有失败段 → 直接停车 (比赛要求车必须沿路网, 不走直线 fallback)
        try:
            _publish_cmd(client, uid, "set_speed", {"speed": 0.0})
            log(f"[NAV] 诱饵 {uid} 路线含 A* 失败段, 停车 (禁止 off-road)")
        except Exception as e:
            log(f"[NAV] 诱饵 {uid} 停车命令发送失败: {e}")
        return

    log(f"[NAV] 诱饵 {uid} A* 规划出 {len(astar_pts)} 个路径点 (全程沿路网)")
    try:
        _publish_cmd(client, uid, "set_speed", {"speed": decoy_speed})
        _publish_cmd(client, uid, "set_trajectory", {"waypoints": astar_pts})
        log(f"[NAV] 诱饵 {uid} set_trajectory 已发送")
    except Exception as e:
        log(f"[NAV] 诱饵 {uid} 命令发送失败: {e}")

    if progress_cb is not None:
        try:
            progress_cb(1, 1, uid)
        except Exception as e:
            log(f"[NAV] 诱饵 {uid} 进度回调失败: {e}")


# ── 实体级并发注入入口 ────────────────────────────────────────────────
# 性能背景: inject_startup 原为 `for ent: inject_astar_*()` 串行, 每辆车逐段
# 发 astar_plan 并阻塞轮询 Redis (get_message 100ms 粒度)。adversarial_swarm
# 约 260 段全串行 → 启动耗时 30~40s。现已切换到批量版 (_plan_route_batch →
# astar_plan_batch): 每辆车一次命令算完整条路线。本入口仍把"每辆车调一次
# inject_astar_*"并发化 (不同实体相互独立), 但单车耗时已大幅下降, 并发收益
# 变小 —— 保留是因为多车并发仍能压一点 wall-clock, 且零成本。
_INJECTABLE_TYPES = ("TargetVehicle", "DecoyVehicle", "ground_vehicle")


def inject_startup_concurrent(
    client,
    entities: List[dict],
    *,
    inject_fn,
    max_workers: int = 8,
    log=print,
) -> Tuple[int, int]:
    """并发注入多实体的 A* 路线。

    把 N 辆车的注入从串行循环改为线程池并发。每个 worker 内部
    (inject_astar_target/decoy → _plan_full_route → _plan_segment) 仍按段串行,
    仅不同实体相互并发 —— 由此规避 ``plan_result_`` 按 entity 存储、同实体
    并发下发多段会互相覆盖的竞态。

    ``inject_fn(ent) -> None`` 由调用方提供, 封装"按 etype 分派到
    inject_astar_target / inject_astar_decoy"的逻辑, 使本函数与具体 scenario
    解耦 (调用方负责构造每实体独立 RNG, 因 random.Random 非线程安全)。

    返回 ``(ok_count, fail_count)``。单个实体异常只 log, 不中断整体
    (与原串行循环的容错语义一致: inject_astar_* 内部已 try/except 停车命令)。

    并发安全前提 (已在 spec 035 plan.md 验证):
      * redis-py ``Redis`` 线程安全 (内置连接池), publish 可并发;
      * ``_plan_segment`` 每次新建独立 pubsub, 不共享, 并发读状态安全。
    """
    vehicles = [e for e in entities if e.get("type") in _INJECTABLE_TYPES]
    n = len(vehicles)
    if n == 0:
        log("[INJECT] 无可注入车辆")
        return 0, 0

    workers = max(1, min(max_workers, n))
    log(f"[INJECT] 并发注入 {n} 辆车 (workers={workers})")
    t0 = time.time()
    ok = 0
    fail = 0
    with ThreadPoolExecutor(max_workers=workers,
                            thread_name_prefix="astar-inject") as ex:
        futs = {ex.submit(inject_fn, e): e for e in vehicles}
        for fut in as_completed(futs):
            ent = futs[fut]
            uid = str(ent.get("id") or ent.get("name") or "?")
            try:
                fut.result()
                ok += 1
            except Exception as e:  # noqa: BLE001 — 容错: 单实体失败不中断
                fail += 1
                log(f"[INJECT] 实体 {uid} 注入异常: {e}")
    log(f"[INJECT] 完成: ok={ok} fail={fail} 耗时={time.time()-t0:.2f}s")
    return ok, fail
