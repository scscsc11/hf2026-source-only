import { type ExecFileFn } from './render-ctl-client';
import { RenderScheduler } from './render-scheduler';
import { RenderServiceController } from './render-service';
export type SessionStatus = 'idle' | 'starting' | 'loading' | 'running' | 'paused' | 'stopping' | 'error';
export interface SessionState {
    status: SessionStatus;
    /** 当前运行的 scenario id(null = 无会话)。 */
    scenario: string | null;
    sessionId: string | null;
    error: string | null;
}
/** 注入的子进程句柄(屏蔽 ChildProcess 细节,测试可 mock)。 */
export interface ManagedProcess {
    readonly pid: number;
    /** 是否已退出(exit 事件触发后置 true)。 */
    readonly exited: boolean;
    /** 注册退出回调(仅触发一次)。 */
    onExit(cb: (code: number | null) => void): void;
    /** 发送信号。Windows 上 SIGTERM/SIGKILL 均强制终止。 */
    kill(signal: string): boolean;
    /**
     * 探测进程是否仍存活(kill(pid,0) 语义)。exit 事件可能因 detached+unref
     * 丢失,watchdog 用此方法兜底检测 competition 真正退出。
     * exited 已置 true 时返回 false;否则向 pid 发信号 0 探测。
     */
    isAlive(): boolean;
}
export interface SimManagerDeps {
    spawn: (cmd: string, args: string[], opts?: {
        cwd?: string;
        logFile?: string;
        env?: NodeJS.ProcessEnv;
        detached?: boolean;
    }) => ManagedProcess;
    redis: {
        publish(channel: string, message: string): Promise<void>;
        subscribe?(channel: string, onMessage: (msg: string) => void): Promise<() => void>;
        /** SET key(service 模式想定下发 sim:scenario)。 */
        set?(key: string, value: string): Promise<void>;
    };
    execFile?: ExecFileFn;
    /**
     * UE 渲染服务模式:bridge 生命周期的调度器 + 服务控制器(server.ts 装配注入,
     * attach/start 已完成)。缺省 → 渲染子系统休眠(仿真照跑)。
     */
    renderScheduler?: RenderScheduler;
    renderService?: RenderServiceController;
    redisHost?: string;
    redisPort?: number;
    stopGrace: number;
    commandChannel: string;
    stateChannel: string;
    onStateChange: (s: SessionState) => void;
    sleep: (ms: number) => Promise<void>;
    now: () => number;
    makeSessionId: () => string;
    warn?: (msg: string) => void;
}
/** discovery 注册表里的场景条目(传给 start 的第一个参数)。 */
export interface StartScenario {
    id: string;
    baselineAgent: string;
    defaultDuration: number;
    scenarioJson: string;
    /** 选手自定义算法（'module:Class'）；优先于 baselineAgent。undefined 则用 baseline。 */
    agent?: string;
    mode?: 'train' | 'eval';
    photoMode?: 'auto' | 'on' | 'off';
    yoloModel?: string;
    accuracy?: number;
    noiseSigma?: number;
    routeSeed?: number;
}
export interface StartOptions {
    pythonBin: string;
    scenariosDir: string;
    renderCtlBinary?: string;
    renderersDir?: string;
    /** scenario.json 绝对路径(render-ctl plan --config 需要)。由 endpoint 计算。 */
    scenarioJsonAbs: string;
    /**
     * 对外广播的 Redis 地址(写进 SET sim:scenario 的 simulation.redis_host)。
     * 远程 UE 拉到的想定自带此回连地址;缺省回退 deps.redisHost。
     */
    advertiseRedisHost?: string;
}
export declare class SimProcessManager {
    private state;
    private competitionProc;
    private stopRequested;
    private deps;
    /** sim:state 订阅取消函数(bridge 停止时调用)。 */
    private unsubscribeStateChannel;
    /** 是否已通过 bridge 启动仿真(区别于外部命令行启动)。 */
    private startedByBridge;
    /** 036: 正在等待仿真 ready 帧 / 首个 sim:state，期间保持 loading。 */
    private waitingForReady;
    /** 036: sim:progress ready 帧订阅的取消函数。 */
    private readyCleanup;
    /** 036: ready 帧超时定时器。 */
    private readyTimeout;
    /**
     * 进程存活 watchdog 定时器:每 2 秒探测 competition 进程是否仍存活。
     * 兜底 child exit 事件因 detached+unref 丢失的情况(实测 competition
     * 10 分钟自然结束后 exit 事件未触发,UE 成孤儿)。检测到进程死亡即触发清理。
     */
    private watchdogTimer;
    constructor(deps: SimManagerDeps);
    /**
     * 接通 scheduler ↔ service 双向钩子(装配一次,实例常驻):
     *   scheduler.online/offline → service 注册表(noteOnline/noteOffline)
     *   service 状态迁移          → scheduler ready 门控(rendering 才参与分配)
     */
    private wireRenderHooks;
    getState(): SessionState;
    /** 订阅 sim:state 频道,感知外部启动的仿真(命令行启动)。 */
    private subscribeToStateChannel;
    /** 处理 sim:state 消息,更新内部状态以反映外部启动的仿真。 */
    private handleStateChannelMessage;
    /** 所有状态变更的唯一出口:更新 + 广播 + 去重。 */
    private setState;
    /** 036: 订阅 sim:progress 的 ready/就绪 帧;收到即切 running。 */
    private subscribeToReadyProgress;
    /** 036: 取消 ready 订阅与超时,切到 running(幂等)。 */
    private markReady;
    /** 036: 清理 ready 相关订阅与超时(进程退出 / stop 时调用)。 */
    private cleanupReady;
    /**
     * 启动 competition 进程(单一子进程)。
     * competition 内部自己 spawn opensim-sim 并轮询就绪,bridge 不重复做。
     */
    start(sc: StartScenario, opts: StartOptions): Promise<SessionState>;
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
    private startRenderers;
    /** spawn 单个 UE 实例(service 模式;按 plan 的 argv/cwd/env)。失败返回 null(降级)。 */
    /**
     * 会话级渲染收尾(常驻轮换):end_scenario 所有 RENDERING UE 回 IDLE +
     * clearMission 清飞机池。不 kill UE —— 下个会话 load_scenario 直接复用。
     * best-effort:单个 UE 失败只 WARN;整体上限 10s(防卡死 UE 拖住 stop)。
     */
    private endRenderMission;
    /**
     * bridge 退出时的渲染清理(server.stop 调用):给所有在线 UE 发 shutdown,
     * UE 收到后优雅退出(~11s),终端窗口里的 UE 进程自动结束(不用手动关窗口)。
     * bridge 不再拥有 UE 进程(本地 UE 由 start 脚本开终端窗口跑,远程 UE 手动启动),
     * 故无 SIGKILL 兜底 —— shutdown 超时的 UE 会经 service.evict 移出注册表,
     * 其进程留在终端窗口里由操作员处理(罕见;正常 shutdown 11s 内退出)。
     */
    shutdownRenderers(): Promise<void>;
    private warn;
    /** 注册子进程退出监听:非 stop 上下文退出 → error。 */
    private watchExit;
    /**
     * competition 退出的统一清理路径(由 exit 事件或 watchdog 触发)。
     * code=null 表示 watchdog 探测到进程消失但未收到 exit 事件(按非 0 处理)。
     *
     * UE 常驻轮换:competition 没了只结束渲染 mission(end_scenario 回 IDLE),
     * 不 kill UE —— 下个会话直接 load_scenario 复用。UE 进程清理由 bridge
     * 退出时的 shutdownRenderers 负责。
     */
    private handleCompetitionExit;
    /**
     * 启动 competition 存活 watchdog(spawn 后调用)。
     * 兜底 child exit 事件因 detached+unref 丢失的情况:每 2 秒用
     * kill(pid,0) 探测 competition 进程,消失即触发清理。
     */
    private startWatchdog;
    private stopWatchdog;
    private watchdogTick;
    /** 暂停:发 pause 命令给引擎(competition 主循环空转检测自动跟随)。 */
    pause(): Promise<SessionState>;
    resume(): Promise<SessionState>;
    /** 关闭:发 end → end_scenario(UE 回 IDLE 常驻) → kill competition → 回 idle。幂等。 */
    stop(): Promise<SessionState>;
    /** 停止订阅 sim:state 频道(bridge 停止时调用)。 */
    unsubscribe(): Promise<void>;
    /** 两段式:SIGTERM → stopGrace → SIGKILL。 */
    private killProc;
}
/** 包装 child_process.spawn 为 ManagedProcess。 */
export declare function spawnChildProcess(cmd: string, args: string[], opts?: {
    cwd?: string;
    logFile?: string;
    env?: NodeJS.ProcessEnv;
    detached?: boolean;
}): ManagedProcess;
/** 生产环境依赖装配(ioredis publish/subscribe/set)。 */
export declare function createProductionDeps(params: {
    redisHost: string;
    redisPort: number;
    stopGrace: number;
    commandChannel: string;
    stateChannel: string;
    onStateChange: (s: SessionState) => void;
    /** UE 渲染服务模式:bridge 生命周期实例(server.ts 装配,attach/start 已完成)。 */
    renderScheduler?: RenderScheduler;
    renderService?: RenderServiceController;
}): SimManagerDeps;
//# sourceMappingURL=sim-process-manager.d.ts.map