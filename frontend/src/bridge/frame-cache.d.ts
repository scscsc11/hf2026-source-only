import { type CameraFrameRedis, type LatestCameraFrame } from './frame-reader';
/** uid → 最新帧缓存(null 表示该 uid 暂无帧)。 */
export type FrameCacheMap = Map<string, LatestCameraFrame | null>;
export interface FrameCacheOptions {
    /** 后台扫描周期(毫秒),默认 100ms(10Hz)。 */
    refreshMs?: number;
    /** N+1 连续未命中多少次后回退 readLatestFrame,默认 3。 */
    missThreshold?: number;
    /** uid 多久无新帧后被移出 watched 集合(毫秒),默认 5000。 */
    staleTimeoutMs?: number;
    /** 注入的 setTimeout/NSDate(测试用)。 */
    setTimeout?: typeof setTimeout;
    /** 注入的 clearTimeout(测试用)。 */
    clearTimeout?: typeof clearTimeout;
    /** 日志(测试用)。 */
    log?: (msg: string) => void;
    /**
     * WS 推送钩子:每当某 uid 的缓存帧被更新(新帧入缓存)时触发。
     * CameraWsServer 据此把帧 broadcast 给订阅该 uid 的 WS 客户端。
     * 仅在 updateCache 被调用时触发(帧真正变化),不会重复推。
     */
    onFrameUpdate?: (uid: string, frame: LatestCameraFrame) => void;
}
/**
 * 后台轮询 sync_camera 帧,为每个被观看的 uid 缓存最新帧。
 *
 * 用法:
 *   const store = new CachedFrameStore(redis);
 *   await store.start();                  // 启动后台扫描
 *   const frame = store.get(uid);         // 同步返回缓存(无 Redis 调用)
 *   store.stop();                          // 停止扫描
 */
export declare class CachedFrameStore {
    private readonly redis;
    private readonly refreshMs;
    private readonly missThreshold;
    private readonly staleTimeoutMs;
    private readonly _setTimeout;
    private readonly _clearTimeout;
    private readonly log;
    /** WS 推送钩子(可选)。帧更新时触发,供 CameraWsServer 推流。 */
    private readonly onFrameUpdate?;
    /** 诊断开关:控制 cursor HIT/MISS 高频日志;KEYS/tick 总耗时始终打。 */
    private readonly verbose;
    private cache;
    /** uid → 已缓存的最大 frame_no。 */
    private cursor;
    /** 当前有前端在观看的 uid 集合。 */
    private watched;
    /** uid → 连续 N+1 未命中次数。 */
    private missCount;
    /** uid → 最后一次读到新帧的时间戳。 */
    private lastSeenAt;
    private timer;
    private refreshing;
    /** 运行时挂载的帧更新钩子(hookFrameUpdate 注册)。 */
    private hooks;
    constructor(redis: CameraFrameRedis, opts?: FrameCacheOptions);
    /** 启动后台扫描。立即触发一次,之后按 refreshMs 周期触发。 */
    start(): void;
    /** 停止后台扫描。 */
    stop(): void;
    /**
     * 同步返回指定 uid 的最新帧缓存。
     * 若该 uid 首次被请求,会加入 watched 集合并触发后台刷新。
     * @returns 缓存帧;uid 不在缓存中时返回 undefined(调用方应回退到 readLatestFrame)。
     */
    get(uid: string): LatestCameraFrame | null | undefined;
    /**
     * 运行时挂一个帧更新钩子(供 CameraWsServer 推流用)。
     * 返回解绑函数。可挂多个;每帧更新时所有钩子按挂载顺序触发。
     * 与构造选项 onFrameUpdate 并存,后者优先触发。
     */
    hookFrameUpdate(cb: (uid: string, frame: LatestCameraFrame) => void): () => void;
    /** 标记某 uid 已停流(让 HTTP 返回 no_stream 而不是 stale 缓存)。 */
    invalidate(uid: string): void;
    private refresh;
    /** 清理长时间无新帧的 uid,避免关闭窗口后仍空转。 */
    private evictStaleUids;
    /** 刷新单个 uid:优先 N+1 探测,失败则回退 readLatestFrame。 */
    private refreshUid;
    private updateCache;
    private scheduleNext;
}
/**
 * 创建一个 camera HTTP handler,优先从 CachedFrameStore 同步返回缓存;
 * 缓存未命中(uid 没扫描到)时回退到 readLatestFrame(保证首帧延迟可接受)。
 */
export declare function getCachedFrame(store: CachedFrameStore, redis: CameraFrameRedis, uid: string): Promise<LatestCameraFrame | null>;
//# sourceMappingURL=frame-cache.d.ts.map