/** 一条发往 UE 的控制指令(assign/stop)。 */
export interface RenderControlCommand {
    event: 'assign' | 'stop';
    render_id: string;
    aircraft: string[];
}
/** RenderScheduler 依赖的最小 Redis 能力(注入,便于单测 mock)。 */
export interface RenderSchedulerDeps {
    /** publish 到指定频道(channel 参数避免硬编码,便于测试)。 */
    publish(channel: string, message: string): Promise<void>;
    /**
     * 订阅 sim:render_id,收到消息时回调。
     * 返回 unsubscribe 函数(detach 时调)。
     */
    subscribe(channel: string, onMessage: (msg: string) => void): Promise<() => void>;
    /** 可选日志。 */
    warn?(msg: string): void;
    /** 可选普通日志。 */
    info?(msg: string): void;
    /** 可选:render-ctl plan 提供的容量(renderId → maxAircraft)。覆盖 UE 上报值。 */
    capacityOverride?: Record<string, number>;
}
/** 在线 UE 渲染端的信息。 */
interface RendererInfo {
    renderId: string;
    maxAircraft: number;
}
/**
 * 把 aircraft 按 renderers 容量贪心分配(填满第一个再溢出到下一个)。
 * 不变性:每架飞机至多出现在一个 UE 的分配里。
 */
export declare function planAssignment(aircraft: string[], renderers: RendererInfo[]): Map<string, string[]>;
/**
 * 有状态分配器:跟踪在线 UE 注册表(常驻) + 每会话飞机池 + ready 门控。
 *
 * 事件流:
 *   attach()                 → 订阅 sim:render_id
 *   UE online                → 注册容量(不分配 —— 等 ready)+ onRendererOnline 钩子
 *   setRendererReady(rid)    → UE 进 RENDERING → drainPending → assign+stop
 *   setRendererNotReady(rid) → UE 离开 RENDERING → 不再参与分配(保留注册)
 *   UE offline / crash       → 收回飞机入 pending → 重分配 + onRendererOffline 钩子
 *   detach()                 → unsubscribe + 清空全部状态
 */
export declare class RenderScheduler {
    private deps;
    /** renderId → 容量信息(在线 UE;常驻,跨会话保留)。 */
    private renderers;
    /** pid → renderId(UE 进程崩溃时反查)。 */
    private pidToRenderId;
    /** 待分配飞机(无 ready UE 承接或被收回)。每会话 setMission 填充。 */
    private pending;
    /** 已分配:renderId → aircraft[](stop/重分配时增量操作)。 */
    private assigned;
    /** 本会话全部 gimbal 飞机(= aircraft ∪ excess),per-UE stop 补集的全集。 */
    private missionAll;
    /** ready UE(收到 status:rendering;只有它们参与分配)。 */
    private ready;
    /** subscribe 返回的 unsubscribe 句柄。 */
    private unsub;
    private attached;
    /** UE renderer_online 钩子(驱动 RenderServiceController.noteOnline)。 */
    onRendererOnline?: (renderId: string, maxAircraft: number) => void;
    /** UE renderer_offline/崩溃钩子(驱动 RenderServiceController.noteOffline)。 */
    onRendererOffline?: (renderId: string) => void;
    constructor(deps: RenderSchedulerDeps);
    /** attach:订阅 sim:render_id。bridge 启动时调用一次,常驻。幂等。 */
    attach(): Promise<void>;
    /** detach:退订 + 清空全部状态(含 UE 注册表)。bridge 退出时调用。幂等。 */
    detach(): Promise<void>;
    /** 会话开始:飞机入 pending 池,记录 missionAll 全集。重复调用前先 clearMission。 */
    setMission(aircraft: string[], excessUavs?: string[]): void;
    /**
     * 会话结束:清飞机池与分配记录;UE 注册表/ready 集合保留(常驻轮换)。
     * 调用方(Manager)在此之前已发 end_scenario,UE 回 IDLE 后经
     * setRendererNotReady 退出 ready 集合。
     */
    clearMission(): void;
    /** 注册一个 UE 进程(spawn 时调用,建立 pid↔renderId 映射)。 */
    registerUeProcess(pid: number, renderId: string): void;
    /** UE 进程崩溃(由 watchExit 触发)。按 pid 反查 renderId 并收回飞机重分配。 */
    onUeCrash(pid: number): Promise<void>;
    /** 处理 sim:render_id 频道的消息(renderer_online / renderer_offline)。 */
    onMessage(raw: string): Promise<void>;
    /** UE 进 RENDERING(由 RenderServiceController 状态钩子驱动):参与分配。 */
    setRendererReady(renderId: string): Promise<void>;
    /**
     * 驱逐不可达 UE(指令超时 = Ctrl+C/崩溃;由 RenderServiceController.onRendererEvicted 驱动)。
     * 委托 removeRenderer:删 renderers/assigned/ready、best-effort publish stop、
     * 飞机回 pending、onRendererOffline 钩子(service.noteOffline 幂等)、drainPending
     * 重分配给其他 ready UE(若无则飞机留 pending,等新 UE 上线接手)。
     */
    evictRenderer(renderId: string): Promise<void>;
    /** UE 离开 RENDERING(end_scenario/teardown):退出分配(注册保留,飞机收回 pending)。 */
    setRendererNotReady(renderId: string): Promise<void>;
    /** 当前在线 UE 数(含未 ready;Manager 的 spawn-if-needed 用)。 */
    onlineCount(): number;
    /** 当前在线 UE 的总容量(含未 ready;Manager 的 spawn-if-needed 用)。 */
    totalOnlineCapacity(): number;
    /** UE 上线:记录容量(优先 capacityOverride)。不分配 —— 等 ready 门控。 */
    private addRenderer;
    /** UE 下线/崩溃:注销,收回其飞机入 pending,重分配给其他 ready UE,publish stop。 */
    private removeRenderer;
    /**
     * 把 pending 飞机按各 ready UE 容量贪心分配;
     * 对每个 ready UE publish assign(全量集) + stop(missionAll - 全量集) 精确收口。
     */
    private drainPending;
    private publish;
    private warn;
    private info;
}
export {};
//# sourceMappingURL=render-scheduler.d.ts.map