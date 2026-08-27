"use strict";
// 仿真会话子进程编排器 —— 单进程 competition 模型 + UE 渲染旁路(UE 渲染服务模式)。
//
// 职责:
//   1. spawn `python -m competition run --scenario <id> --agent <baseline>
//      --start-sim` 单一进程(它内部自己 spawn opensim-sim 并轮询就绪);
//   2. UE 渲染旁路(service 模式):调 opensim-render-ctl plan → SET sim:scenario
//      (改写 redis_host/port 为 advertise 地址)→ setMission + beginMission →
//      spawn-if-needed 本地 UE(-rendermode=service)。UE 常驻轮换:会话 stop
//      只发 end_scenario 回 IDLE,不 kill;bridge 退出才 shutdown + SIGKILL 兜底。
//      任一步失败 → WARN 降级,仿真照跑。
//   3. pause/resume(向 sim:commands 发 {cmd:pause/resume});
//   4. stop(发 {cmd:end} + end_scenario + kill competition)。
//
// 渲染是 bridge 旁路:competition 不感知 UE;UE 崩溃不影响仿真。
// UE 注册表/订阅由 RenderScheduler + RenderServiceController 持有(bridge 生命周期,
// server.ts 装配注入) —— pub/sub 无重放,跨会话常驻才能保证切赛题时 UE 可见可复用。
// 可测性:spawn / execFile / redis / subscribe / 时间 / sleep 均经 SimManagerDeps 注入。
var __createBinding = (this && this.__createBinding) || (Object.create ? (function(o, m, k, k2) {
    if (k2 === undefined) k2 = k;
    var desc = Object.getOwnPropertyDescriptor(m, k);
    if (!desc || ("get" in desc ? !m.__esModule : desc.writable || desc.configurable)) {
      desc = { enumerable: true, get: function() { return m[k]; } };
    }
    Object.defineProperty(o, k2, desc);
}) : (function(o, m, k, k2) {
    if (k2 === undefined) k2 = k;
    o[k2] = m[k];
}));
var __setModuleDefault = (this && this.__setModuleDefault) || (Object.create ? (function(o, v) {
    Object.defineProperty(o, "default", { enumerable: true, value: v });
}) : function(o, v) {
    o["default"] = v;
});
var __importStar = (this && this.__importStar) || (function () {
    var ownKeys = function(o) {
        ownKeys = Object.getOwnPropertyNames || function (o) {
            var ar = [];
            for (var k in o) if (Object.prototype.hasOwnProperty.call(o, k)) ar[ar.length] = k;
            return ar;
        };
        return ownKeys(o);
    };
    return function (mod) {
        if (mod && mod.__esModule) return mod;
        var result = {};
        if (mod != null) for (var k = ownKeys(mod), i = 0; i < k.length; i++) if (k[i] !== "default") __createBinding(result, mod, k[i]);
        __setModuleDefault(result, mod);
        return result;
    };
})();
var __importDefault = (this && this.__importDefault) || function (mod) {
    return (mod && mod.__esModule) ? mod : { "default": mod };
};
Object.defineProperty(exports, "__esModule", { value: true });
exports.SimProcessManager = void 0;
exports.spawnChildProcess = spawnChildProcess;
exports.createProductionDeps = createProductionDeps;
const child_process_1 = require("child_process");
const fs = __importStar(require("fs"));
const path = __importStar(require("path"));
const ioredis_1 = __importDefault(require("ioredis"));
const render_ctl_client_1 = require("./render-ctl-client");
const constants_1 = require("../rendering/constants");
function scenarioOutputDir(scenariosDir, scenarioJson) {
    return path.join(scenariosDir, path.dirname(scenarioJson), 'output');
}
// ── SimProcessManager ─────────────────────────────────────────────────
class SimProcessManager {
    constructor(deps) {
        this.state = { status: 'idle', scenario: null, sessionId: null, error: null };
        this.competitionProc = null;
        this.stopRequested = false;
        /** sim:state 订阅取消函数(bridge 停止时调用)。 */
        this.unsubscribeStateChannel = null;
        /** 是否已通过 bridge 启动仿真(区别于外部命令行启动)。 */
        this.startedByBridge = false;
        /** 036: 正在等待仿真 ready 帧 / 首个 sim:state，期间保持 loading。 */
        this.waitingForReady = false;
        /** 036: sim:progress ready 帧订阅的取消函数。 */
        this.readyCleanup = null;
        /** 036: ready 帧超时定时器。 */
        this.readyTimeout = null;
        /**
         * 进程存活 watchdog 定时器:每 2 秒探测 competition 进程是否仍存活。
         * 兜底 child exit 事件因 detached+unref 丢失的情况(实测 competition
         * 10 分钟自然结束后 exit 事件未触发,UE 成孤儿)。检测到进程死亡即触发清理。
         */
        this.watchdogTimer = null;
        this.deps = deps;
        this.wireRenderHooks();
        this.subscribeToStateChannel();
    }
    /**
     * 接通 scheduler ↔ service 双向钩子(装配一次,实例常驻):
     *   scheduler.online/offline → service 注册表(noteOnline/noteOffline)
     *   service 状态迁移          → scheduler ready 门控(rendering 才参与分配)
     */
    wireRenderHooks() {
        const sched = this.deps.renderScheduler;
        const svc = this.deps.renderService;
        if (!sched || !svc)
            return;
        sched.onRendererOnline = (rid) => svc.noteOnline(rid);
        sched.onRendererOffline = (rid) => svc.noteOffline(rid);
        svc.onStateChange = (rid, st) => {
            if (st === 'rendering') {
                sched.setRendererReady(rid).catch((e) => this.warn(`setRendererReady ${rid}: ${e.message}`));
            }
            else {
                sched.setRendererNotReady(rid).catch((e) => this.warn(`setRendererNotReady ${rid}: ${e.message}`));
            }
        };
        // UE 不可达(指令超时 = Ctrl+C/崩溃)驱逐 → 回收飞机重分配,防死 UE 滞留偷飞机。
        svc.onRendererEvicted = (rid) => {
            this.warn(`[SimProcessManager] renderer evicted → reclaiming aircraft from ${rid}`);
            sched.evictRenderer(rid).catch((e) => this.warn(`evict ${rid}: ${e.message}`));
        };
    }
    getState() {
        return { ...this.state };
    }
    /** 订阅 sim:state 频道,感知外部启动的仿真(命令行启动)。 */
    async subscribeToStateChannel() {
        if (!this.deps.redis.subscribe)
            return;
        try {
            this.unsubscribeStateChannel = await this.deps.redis.subscribe(this.deps.stateChannel, (msg) => this.handleStateChannelMessage(msg));
        }
        catch (e) {
            this.warn(`failed to subscribe to ${this.deps.stateChannel}: ${e.message}`);
        }
    }
    /** 处理 sim:state 消息,更新内部状态以反映外部启动的仿真。 */
    handleStateChannelMessage(msg) {
        try {
            const parsed = JSON.parse(msg);
            const simStatus = parsed.status;
            if (!simStatus)
                return;
            if (this.startedByBridge) {
                // 036: bridge 启动的会话不依赖首个 sim:state 切 running——那发生在
                // controller 做路径规划之前,会过早收起进度条。真实就绪以 controller 的
                // '就绪' 进度帧为准(见 subscribeToReadyProgress); 进程崩溃/超时兜底。
                return;
            }
            if (this.state.status === 'idle') {
                if (simStatus === 'running') {
                    this.state = {
                        status: 'running',
                        scenario: null,
                        sessionId: `external_${Date.now().toString(36)}`,
                        error: null,
                    };
                    this.deps.onStateChange(this.getState());
                }
                else if (simStatus === 'paused') {
                    this.state = {
                        status: 'paused',
                        scenario: null,
                        sessionId: `external_${Date.now().toString(36)}`,
                        error: null,
                    };
                    this.deps.onStateChange(this.getState());
                }
            }
            else if (this.state.status === 'running' || this.state.status === 'paused') {
                if (simStatus === 'ended' || simStatus === 'idle') {
                    this.state = {
                        status: 'idle',
                        scenario: null,
                        sessionId: null,
                        error: null,
                    };
                    this.deps.onStateChange(this.getState());
                }
                else if (simStatus === 'running' || simStatus === 'paused') {
                    if (this.state.status !== simStatus) {
                        this.state = { ...this.state, status: simStatus };
                        this.deps.onStateChange(this.getState());
                    }
                }
            }
        }
        catch {
            // 忽略解析错误
        }
    }
    /** 所有状态变更的唯一出口:更新 + 广播 + 去重。 */
    setState(next, error = null) {
        if (this.state.status === next && this.state.error === error)
            return;
        this.state = { ...this.state, status: next, error };
        this.deps.onStateChange(this.getState());
    }
    /** 036: 订阅 sim:progress 的 ready/就绪 帧;收到即切 running。 */
    async subscribeToReadyProgress() {
        if (!this.deps.redis.subscribe)
            return;
        try {
            const unsub = await this.deps.redis.subscribe('sim:progress', (msg) => {
                try {
                    const parsed = JSON.parse(msg);
                    // 036: 只有 Python controller 的最终就绪帧(phase='就绪')才切 running。
                    // 引擎自身的 'ready' 只是进度节点,此时 controller 还在做路径规划,
                    // 不应过早把 session 置为 running(否则前端会提前收起进度条)。
                    if (parsed.type === 'load_progress' && parsed.phase === '就绪') {
                        this.warn('[SimProcessManager] controller ready frame received; transitioning to running');
                        this.markReady();
                    }
                }
                catch { /* ignore parse errors */ }
            });
            this.readyCleanup = () => {
                try {
                    unsub();
                }
                catch { /* ignore */ }
            };
        }
        catch (e) {
            this.warn(`failed to subscribe to sim:progress: ${e.message}`);
        }
    }
    /** 036: 取消 ready 订阅与超时,切到 running(幂等)。 */
    markReady() {
        if (!this.waitingForReady)
            return;
        this.waitingForReady = false;
        if (this.readyTimeout) {
            clearTimeout(this.readyTimeout);
            this.readyTimeout = null;
        }
        if (this.readyCleanup) {
            this.readyCleanup();
            this.readyCleanup = null;
        }
        if (this.state.status === 'loading' || this.state.status === 'starting') {
            this.setState('running');
        }
    }
    /** 036: 清理 ready 相关订阅与超时(进程退出 / stop 时调用)。 */
    cleanupReady() {
        this.waitingForReady = false;
        if (this.readyTimeout) {
            clearTimeout(this.readyTimeout);
            this.readyTimeout = null;
        }
        if (this.readyCleanup) {
            this.readyCleanup();
            this.readyCleanup = null;
        }
    }
    /**
     * 启动 competition 进程(单一子进程)。
     * competition 内部自己 spawn opensim-sim 并轮询就绪,bridge 不重复做。
     */
    async start(sc, opts) {
        // error 态允许重新开始:先结束残留渲染 mission(UE 回 IDLE,不 kill),再回 idle。
        if (this.state.status === 'error') {
            await this.endRenderMission();
            await this.killProc();
            this.competitionProc = null;
            this.stopWatchdog();
            this.state = { status: 'idle', scenario: null, sessionId: null, error: null };
            this.deps.onStateChange(this.getState());
        }
        if (this.state.status !== 'idle') {
            throw new Error('session_already_active');
        }
        this.stopRequested = false;
        this.startedByBridge = true;
        const sessionId = this.deps.makeSessionId();
        this.state = { status: 'starting', scenario: sc.id, sessionId, error: null };
        this.deps.onStateChange(this.getState());
        // competition 需从 repo 根运行(找 competition 包 + build/ 下的 sim)。
        // scenariosDir = <repo>/competition/scenarios,故 repo 根上溯两级。
        const repoRoot = path.dirname(path.dirname(opts.scenariosDir));
        const args = [
            '-m', 'competition', 'run',
            '--scenario', sc.id,
            '--agent', sc.agent || sc.baselineAgent,
            '--start-sim',
            '--duration', String(sc.defaultDuration),
            '--redis-host', this.deps.redisHost ?? '127.0.0.1',
            '--redis-port', String(this.deps.redisPort ?? 6379),
        ];
        // 感知参数条件追加：photoMode 三态（默认 auto → 非 dry_run 自动拉取 UE 相机 PNG 帧）。
        if (sc.mode)
            args.push('--mode', sc.mode);
        const photoMode = sc.photoMode ?? 'auto';
        args.push('--photo-mode', photoMode);
        if (sc.yoloModel)
            args.push('--yolo-model', sc.yoloModel);
        // 防泄漏钳制后的 accuracy/noise（已由 endpoint 限定 [0,0.9] / ≥30）。
        if (sc.accuracy !== undefined)
            args.push('--accuracy', String(sc.accuracy));
        if (sc.noiseSigma !== undefined)
            args.push('--noise-sigma', String(sc.noiseSigma));
        // 路线种子: 正整数才透传(0 = 不随机,后端默认行为)。
        if (sc.routeSeed && sc.routeSeed > 0)
            args.push('--seed', String(sc.routeSeed));
        const outDir = scenarioOutputDir(opts.scenariosDir, sc.scenarioJson);
        // 把 outDir 显式传给 controller: 让 prepared.json / controller.stderr.log /
        // sim.stderr.log 都写到同一目录(否则 controller 用默认 output='output' 写到
        // cwd=repoRoot 下的 output/,而 bridge 期望 competition/scenarios/.../output/)。
        args.push('--output', outDir);
        try {
            this.competitionProc = this.deps.spawn(opts.pythonBin, args, {
                cwd: repoRoot,
                logFile: path.join(outDir, 'controller.stderr.log'),
                env: {
                    ...process.env,
                    OPENSIM_SIM_STDERR: path.join(outDir, 'sim.stderr.log'),
                },
                detached: true,
            });
        }
        catch (e) {
            this.setState('error', 'competition_spawn_failed');
            throw e;
        }
        this.setState('loading');
        this.watchExit(this.competitionProc, 'competition_crashed');
        // 启动存活 watchdog:兜底 child exit 事件因 detached+unref 丢失的情况。
        this.startWatchdog();
        // 036: 在 spawn 后保持 loading;监听 sim:progress ready 帧或首个 sim:state
        // 才认为仿真真正运行。进程崩溃由 watchExit 兜底。
        this.waitingForReady = true;
        await this.subscribeToReadyProgress();
        this.readyTimeout = setTimeout(() => {
            if (this.waitingForReady) {
                this.warn('[SimProcessManager] ready timeout after 5 minutes; marking error');
                this.waitingForReady = false;
                this.readyCleanup?.();
                this.readyCleanup = null;
                this.setState('error', 'ready_timeout');
            }
        }, 5 * 60 * 1000);
        // 启动后短暂等待;进程立即退出 → crashed。
        await this.deps.sleep(50);
        if (this.competitionProc.exited) {
            this.setState('error', 'competition_crashed');
            throw new Error('competition_crashed');
        }
        // UE 渲染旁路(service 模式)。renderCtlBinary 缺省 → 跳过(仿真照跑)。
        // 任一步失败 → WARN 降级,不影响 competition 已起来的会话。
        // 036: 渲染启动完成后仍保持 loading,由 sim:progress ready / sim:state 触发 running。
        await this.startRenderers(opts).catch((e) => {
            this.warn(`renderer orchestration degraded: ${e.message}`);
        });
        return this.getState();
    }
    /**
     * UE 渲染旁路(service 模式):render-ctl plan → SET sim:scenario →
     * setMission + beginMission → spawn-if-needed 本地 UE。
     * 全程 best-effort:失败只 WARN,不抛错(仿真照跑)。
     *
     * 想定下发:service 模式 UE 不读本地 scenario.json,从 Redis key sim:scenario
     * 拉取;下发前把 simulation.redis_host/port 改写为 advertise 地址,远程 UE 拉到
     * 的想定自带正确回连地址。
     *
     * spawn-if-needed:UE 常驻轮换 —— 存活 UE 容量已够则不 spawn;容量不足才按
     * plan 补 spawn(首个会话 / UE 崩溃后)。本机无 GPU(plan.instances 空)时
     * 不 return —— 仍下发想定,远程 UE 可承接全部飞机。
     */
    async startRenderers(opts) {
        if (!opts.renderCtlBinary)
            return; // 渲染子系统休眠
        if (!this.deps.execFile) {
            this.warn('execFile not configured, skipping UE orchestration');
            return;
        }
        const scheduler = this.deps.renderScheduler;
        const service = this.deps.renderService;
        if (!scheduler || !service) {
            this.warn('render scheduler/service not injected, skipping UE orchestration');
            return;
        }
        if (!this.deps.redis.set) {
            this.warn('redis.set not available, skipping UE orchestration');
            return;
        }
        let plan;
        try {
            plan = await (0, render_ctl_client_1.planRenderers)({
                execFile: this.deps.execFile,
                renderCtlBinary: opts.renderCtlBinary,
                scenarioJsonAbs: opts.scenarioJsonAbs,
                renderersDir: opts.renderersDir ?? 'config/renderers',
            });
        }
        catch (e) {
            this.warn(`render-ctl plan failed: ${e.message}`);
            return;
        }
        if (plan.skipped.length > 0) {
            this.warn(`render-ctl skipped: ${plan.skipped.join('; ')}`);
        }
        if (plan.gimbalUavs.length === 0) {
            this.warn('no gimbal UAVs in scenario, render mission skipped');
            return;
        }
        // ── SET sim:scenario(改写 redis_host/port 为 advertise 地址)──────────
        try {
            const raw = fs.readFileSync(opts.scenarioJsonAbs, 'utf8');
            const scenarioJson = JSON.parse(raw);
            const sim = (scenarioJson.simulation ?? {});
            sim.redis_host = opts.advertiseRedisHost ?? this.deps.redisHost ?? '127.0.0.1';
            sim.redis_port = this.deps.redisPort ?? 6379;
            scenarioJson.simulation = sim;
            await this.deps.redis.set(constants_1.SCENARIO_KEY, JSON.stringify(scenarioJson));
            console.log(`[SimProcessManager] sim:scenario SET (redis_host=${sim.redis_host}:${sim.redis_port}, gimbal=${plan.gimbalUavs.length})`);
        }
        catch (e) {
            this.warn(`failed to SET sim:scenario: ${e.message}`);
            return;
        }
        // ── 装配会话任务:飞机池入调度器 + 武装自动 load ─────────────────────
        // beginMission 后,注册表中 online+idle 的 UE(本地终端窗口启动的、远程手动
        // 启动的)自动收到 load_scenario;后续新上线的 UE 同样自动触发。
        // 本地 UE 不再由 bridge spawn —— start 脚本开终端窗口前台跑 run.sh,
        // bridge 通过 Redis 发现(renderer_online),与远程 UE 模式完全统一。
        scheduler.setMission(plan.gimbalUavs, plan.excessUavs);
        service.beginMission();
        if (plan.instances.length === 0) {
            this.warn('no local GPU instances; relying on manually-started UEs');
        }
    }
    /** spawn 单个 UE 实例(service 模式;按 plan 的 argv/cwd/env)。失败返回 null(降级)。 */
    // 注:本地 UE 现由 start 脚本开终端窗口前台跑 run.sh,bridge 不 spawn。
    // 本方法已移除 —— UE 进程生命周期脱离 bridge,本地/远程统一为 Redis 发现模式。
    /**
     * 会话级渲染收尾(常驻轮换):end_scenario 所有 RENDERING UE 回 IDLE +
     * clearMission 清飞机池。不 kill UE —— 下个会话 load_scenario 直接复用。
     * best-effort:单个 UE 失败只 WARN;整体上限 10s(防卡死 UE 拖住 stop)。
     */
    async endRenderMission() {
        const service = this.deps.renderService;
        const scheduler = this.deps.renderScheduler;
        if (service) {
            service.endMission();
            const rendering = service.listRendering();
            if (rendering.length > 0) {
                this.warn(`[SimProcessManager] end_scenario → ${rendering.join(',')} (UE back to IDLE, kept alive)`);
                await Promise.race([
                    Promise.all(rendering.map((rid) => service.endScenario(rid).catch((e) => this.warn(`end_scenario ${rid}: ${e.message}`)))),
                    this.deps.sleep(10000),
                ]);
            }
        }
        scheduler?.clearMission();
    }
    /**
     * bridge 退出时的渲染清理(server.stop 调用):给所有在线 UE 发 shutdown,
     * UE 收到后优雅退出(~11s),终端窗口里的 UE 进程自动结束(不用手动关窗口)。
     * bridge 不再拥有 UE 进程(本地 UE 由 start 脚本开终端窗口跑,远程 UE 手动启动),
     * 故无 SIGKILL 兜底 —— shutdown 超时的 UE 会经 service.evict 移出注册表,
     * 其进程留在终端窗口里由操作员处理(罕见;正常 shutdown 11s 内退出)。
     */
    async shutdownRenderers() {
        const service = this.deps.renderService;
        if (service) {
            const online = service.listOnline();
            if (online.length > 0) {
                this.warn(`[SimProcessManager] shutdown → ${online.join(',')}`);
                await Promise.race([
                    Promise.all(online.map((rid) => service.shutdown(rid).catch((e) => this.warn(`shutdown ${rid}: ${e.message}`)))),
                    this.deps.sleep(15000),
                ]);
            }
        }
    }
    warn(msg) {
        (this.deps.warn ?? ((m) => console.warn(`[SimProcessManager] ${m}`)))(msg);
    }
    /** 注册子进程退出监听:非 stop 上下文退出 → error。 */
    watchExit(proc, errorCode) {
        proc.onExit((code) => {
            this.warn(`[SimProcessManager] watched proc exit: code=${code} status=${this.state.status} stopRequested=${this.stopRequested}`);
            this.handleCompetitionExit(code, errorCode);
        });
    }
    /**
     * competition 退出的统一清理路径(由 exit 事件或 watchdog 触发)。
     * code=null 表示 watchdog 探测到进程消失但未收到 exit 事件(按非 0 处理)。
     *
     * UE 常驻轮换:competition 没了只结束渲染 mission(end_scenario 回 IDLE),
     * 不 kill UE —— 下个会话直接 load_scenario 复用。UE 进程清理由 bridge
     * 退出时的 shutdownRenderers 负责。
     */
    handleCompetitionExit(code, errorCode) {
        if (this.stopRequested)
            return;
        const wasRunning = this.state.status !== 'idle' && this.state.status !== 'stopping';
        if (code === 0 || code === null) {
            // code===null: watchdog 探测到进程消失,按正常退出处理(仿真自然结束)。
            this.warn(`[SimProcessManager] competition ended (code=${code}), ending render mission (UE kept alive)`);
            this.endRenderMission().catch(() => { });
            this.competitionProc = null;
            this.cleanupReady(); // 036: 清理 ready 订阅
            this.stopWatchdog();
            if (wasRunning) {
                this.state = { status: 'idle', scenario: null, sessionId: null, error: null };
                this.deps.onStateChange(this.getState());
            }
            return;
        }
        // 非 0 退出:崩溃路径。
        if (wasRunning)
            this.setState('error', errorCode);
        this.cleanupReady(); // 036: 清理 ready 订阅
        this.stopWatchdog();
        // competition 崩溃:结束渲染 mission(UE 回 IDLE 常驻),kill competition 残余。
        this.warn(`[SimProcessManager] competition crashed code=${code}, ending render mission (UE kept alive)`);
        this.endRenderMission().catch(() => { });
        this.killProc().catch(() => { });
    }
    /**
     * 启动 competition 存活 watchdog(spawn 后调用)。
     * 兜底 child exit 事件因 detached+unref 丢失的情况:每 2 秒用
     * kill(pid,0) 探测 competition 进程,消失即触发清理。
     */
    startWatchdog() {
        this.stopWatchdog();
        this.watchdogTimer = setInterval(() => this.watchdogTick(), 2000);
        // unref:watchdog 不应阻止 bridge 退出(bridge 退出走 stop() 正常清理)。
        this.watchdogTimer.unref?.();
    }
    stopWatchdog() {
        if (this.watchdogTimer) {
            clearInterval(this.watchdogTimer);
            this.watchdogTimer = null;
        }
    }
    watchdogTick() {
        const proc = this.competitionProc;
        if (!proc)
            return; // 无会话,不探测
        if (this.state.status === 'idle' || this.state.status === 'stopping')
            return;
        // 进程已 exited(exit 事件触发过)或 isAlive 探测失败 → 触发清理。
        if (proc.exited)
            return; // exit 事件路径已处理
        if (!proc.isAlive()) {
            this.warn(`[SimProcessManager] watchdog: competition pid=${proc.pid} no longer alive (exit event missed?), cleaning up`);
            this.handleCompetitionExit(null, 'competition_crashed');
        }
    }
    /** 暂停:发 pause 命令给引擎(competition 主循环空转检测自动跟随)。 */
    async pause() {
        if (this.state.status !== 'running')
            throw new Error('not_running');
        await this.deps.redis.publish(this.deps.commandChannel, JSON.stringify({ cmd: 'pause' }));
        this.setState('paused');
        return this.getState();
    }
    async resume() {
        if (this.state.status !== 'paused')
            throw new Error('not_paused');
        await this.deps.redis.publish(this.deps.commandChannel, JSON.stringify({ cmd: 'resume' }));
        this.setState('running');
        return this.getState();
    }
    /** 关闭:发 end → end_scenario(UE 回 IDLE 常驻) → kill competition → 回 idle。幂等。 */
    async stop() {
        if (this.state.status === 'idle')
            return this.getState();
        this.stopRequested = true;
        this.setState('stopping');
        this.cleanupReady(); // 036: 停止后不再等待 ready
        this.stopWatchdog(); // 停 watchdog(主动 stop 走正常清理,不需兜底探测)
        try {
            await this.deps.redis.publish(this.deps.commandChannel, JSON.stringify({ cmd: 'end' }));
        }
        catch {
            // Redis 不可达也要尽力终止进程
        }
        // UE 常驻轮换:end_scenario 回 IDLE + clearMission,不 kill UE。
        await this.endRenderMission();
        await this.killProc();
        this.competitionProc = null;
        this.stopRequested = false;
        this.startedByBridge = false;
        this.state = { status: 'idle', scenario: null, sessionId: null, error: null };
        this.deps.onStateChange(this.getState());
        return this.getState();
    }
    /** 停止订阅 sim:state 频道(bridge 停止时调用)。 */
    async unsubscribe() {
        if (this.unsubscribeStateChannel) {
            this.unsubscribeStateChannel();
            this.unsubscribeStateChannel = null;
        }
    }
    /** 两段式:SIGTERM → stopGrace → SIGKILL。 */
    async killProc() {
        const proc = this.competitionProc;
        if (!proc || proc.exited)
            return;
        try {
            proc.kill('SIGTERM');
        }
        catch { /* ignore */ }
        await this.deps.sleep(this.deps.stopGrace * 1000);
        if (!proc.exited) {
            try {
                proc.kill('SIGKILL');
            }
            catch { /* ignore */ }
            await this.deps.sleep(200);
        }
    }
}
exports.SimProcessManager = SimProcessManager;
// ── 生产装配 ──────────────────────────────────────────────────────────
/** 包装 child_process.spawn 为 ManagedProcess。 */
function spawnChildProcess(cmd, args, opts) {
    let stdio;
    if (opts?.logFile) {
        fs.mkdirSync(path.dirname(opts.logFile), { recursive: true });
        const fd = fs.openSync(opts.logFile, 'a');
        stdio = ['ignore', fd, fd];
    }
    else {
        stdio = ['ignore', 'ignore', 'ignore'];
    }
    const child = (0, child_process_1.spawn)(cmd, args, {
        stdio,
        windowsHide: true,
        cwd: opts?.cwd,
        env: opts?.env,
        detached: opts?.detached,
    });
    if (opts?.detached) {
        try {
            child.unref();
        }
        catch { /* ignore */ }
    }
    let exited = false;
    let exitCode = null;
    const exitCbs = [];
    child.on('exit', (code) => {
        exited = true;
        exitCode = code;
        for (const cb of exitCbs)
            cb(code);
    });
    child.on('error', (err) => {
        console.warn(`[spawnChildProcess] spawn error for ${cmd}: ${err.message}`);
        exited = true;
        exitCode = -1;
        for (const cb of exitCbs)
            cb(exitCode);
    });
    return {
        get pid() { return child.pid ?? -1; },
        get exited() { return exited; },
        onExit(cb) {
            if (exited)
                cb(exitCode);
            else
                exitCbs.push(cb);
        },
        kill(signal) {
            if (!child.pid)
                return false;
            if (process.platform === 'win32') {
                try {
                    (0, child_process_1.spawn)('taskkill', ['/PID', String(child.pid), '/T', '/F'], { stdio: 'ignore' });
                    return true;
                }
                catch { /* fall through */ }
            }
            if (opts?.detached) {
                try {
                    process.kill(-child.pid, signal);
                    return true;
                }
                catch { /* fall through */ }
            }
            try {
                return child.kill(signal);
            }
            catch {
                return false;
            }
        },
        isAlive() {
            if (exited || !child.pid)
                return false;
            // kill(pid, 0) 不发信号,只探测进程是否存在。ESRCH → 已退出。
            try {
                process.kill(child.pid, 0);
                return true;
            }
            catch {
                return false;
            }
        },
    };
}
/** 生产环境依赖装配(ioredis publish/subscribe/set)。 */
function createProductionDeps(params) {
    const redis = new ioredis_1.default({ host: params.redisHost, port: params.redisPort, lazyConnect: false });
    return {
        spawn: spawnChildProcess,
        redis: {
            async publish(channel, message) { await redis.publish(channel, message); },
            // RenderScheduler 订阅 sim:render_id / RenderServiceController 订阅 sim:control 用。
            async subscribe(channel, onMessage) {
                const sub = new ioredis_1.default({ host: params.redisHost, port: params.redisPort, lazyConnect: false });
                await sub.subscribe(channel);
                sub.on('message', (_ch, msg) => onMessage(msg));
                return async () => {
                    try {
                        await sub.unsubscribe(channel);
                    }
                    catch { /* ignore */ }
                    try {
                        sub.disconnect();
                    }
                    catch { /* ignore */ }
                };
            },
            // service 模式想定下发:SET sim:scenario。
            async set(key, value) { await redis.set(key, value); },
        },
        // opensim-render-ctl plan 调用。
        execFile: (0, render_ctl_client_1.createNodeExecFile)(),
        renderScheduler: params.renderScheduler,
        renderService: params.renderService,
        redisHost: params.redisHost,
        redisPort: params.redisPort,
        stopGrace: params.stopGrace,
        commandChannel: params.commandChannel,
        stateChannel: params.stateChannel,
        onStateChange: params.onStateChange,
        sleep: (ms) => new Promise((r) => setTimeout(r, ms)),
        now: () => Date.now(),
        makeSessionId: () => `sess_${Date.now().toString(36)}_${Math.floor(Math.random() * 1e6).toString(36)}`,
    };
}
