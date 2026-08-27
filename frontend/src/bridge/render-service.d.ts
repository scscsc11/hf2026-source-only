/** UE service 模式 FSM 状态(与 UE 端一致)。 */
export type UeServiceState = 'idle' | 'loading' | 'rendering' | 'teardown' | 'shutting_down';
/** bridge → UE 的三种想定生命周期指令。 */
export type RenderServiceAction = 'load_scenario' | 'end_scenario' | 'shutdown';
export interface RenderServiceDeps {
    publish(channel: string, message: string): Promise<void>;
    /** 订阅 sim:control,返回 unsubscribe。 */
    subscribe(channel: string, onMessage: (msg: string) => void): Promise<() => void>;
    warn?(msg: string): void;
    info?(msg: string): void;
    /** load_scenario ack 超时(默认 60s —— spawn 200 实体约 1-2s,留足余量)。 */
    loadTimeoutMs?: number;
    /** end_scenario ack 超时(默认 30s)。 */
    endTimeoutMs?: number;
    /** shutdown ack 超时(默认 15s —— UE 正常 ~11s 退出)。 */
    shutdownTimeoutMs?: number;
    /** renderer_online 后无 status 的告警延迟(默认 90s;可能是老 file 模式 UE)。 */
    statusWarnDelayMs?: number;
}
/**
 * bridge 生命周期常驻。注册表只增不减(UE offline/shutdown 才删),
 * 跨会话保留 render_id —— 页面切赛题不退出 bridge,UE 全程可复用。
 */
export declare class RenderServiceController {
    private deps;
    private ues;
    private unsub;
    private started;
    /** beginMission 后置 true:自动给 online+idle 的 UE 发 load_scenario。 */
    private missionArmed;
    /** 状态迁移钩子(SimProcessManager 接 scheduler.setRendererReady/NotReady)。 */
    onStateChange?: (renderId: string, state: UeServiceState) => void;
    /**
     * UE 被驱逐钩子(指令超时 = UE 不可达 = Ctrl+C/崩溃/网络断)。
     * SimProcessManager 接此钩子驱动 scheduler.evictRenderer 回收飞机重分配。
     * 注意:noteOffline(renderer_offline 驱动)不触发本钩子 —— scheduler 已自清理,
     * 顺带调 noteOffline 幂等;只有超时驱逐(无 offline 信号)需要本钩子通知 scheduler。
     */
    onRendererEvicted?: (renderId: string) => void;
    constructor(deps: RenderServiceDeps);
    /** 启动:订阅 sim:control。必须在任何 UE 上线前调用(pub/sub 无重放)。 */
    start(): Promise<void>;
    /** 停止:退订 + 拒绝全部在途指令 + 清空注册表(bridge 退出时调用)。 */
    stop(): Promise<void>;
    /** UE renderer_online(由 RenderScheduler 钩子转发)。幂等。 */
    noteOnline(renderId: string): void;
    /** UE renderer_offline / 崩溃(由 RenderScheduler 钩子转发)。 */
    noteOffline(renderId: string): void;
    /** 会话开始:武装自动 load;所有已注册 UE 的 load 进度重置,idle 的立即触发。 */
    beginMission(): void;
    /** 会话结束:解除自动 load(end_scenario 由调用方按需发,本方法不主动发)。 */
    endMission(): void;
    /** 查询 UE 当前状态;undefined = 未注册(不在线)。 */
    getState(renderId: string): UeServiceState | 'unknown' | undefined;
    listOnline(): string[];
    listRendering(): string[];
    /** 发 load_scenario 并等 ack:ok=true 且 state=rendering → resolve。 */
    loadScenario(renderId: string): Promise<void>;
    /** 发 end_scenario 并等 ack:ok=true 且 state=idle → resolve。 */
    endScenario(renderId: string): Promise<void>;
    /** 发 shutdown 并等 ack:ok=true 且 state=shutting_down → resolve(进程退出不等)。 */
    shutdown(renderId: string): Promise<void>;
    /** 发指令 + 登记在途 ack 等待。每 UE 同时只允许一个在途指令。 */
    private command;
    /** 处理 sim:control 频道的 status 回报。 */
    private onMessage;
    /** 自动 load 条件:mission 武装 + idle + 本会话未尝试过 + 无在途指令。 */
    private maybeAutoLoad;
    /**
     * 驱逐不可达 UE(指令超时):拒绝在途指令 + 清告警定时器 + 移出注册表 +
     * 通知 scheduler 回收飞机重分配。幂等(已不在注册表则 no-op)。
     */
    private evict;
    /** 清理条目:拒绝在途指令 + 清告警定时器。 */
    private teardownEntry;
    private warn;
    private info;
}
//# sourceMappingURL=render-service.d.ts.map