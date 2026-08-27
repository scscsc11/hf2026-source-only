"use strict";
// 022 UAV 相机帧缓存层 — 游标追帧版。
//
// 背景:sync_camera:{uid}:frame:{n} hash 没有 latest 指针,消费端原本用
// KEYS + JS loop 找最大帧号。但 KEYS 在 Redis 单线程下是 O(N) 阻塞命令,
// 每帧 ~70 个 hash key 时单次 KEYS 实测 800-900ms,严重阻塞 sim 写状态。
//
// 修复:利用 UE 帧号单调递增的契约,为每个被观看的 uid 维护一个 cursor。
// 每次刷新优先用 hgetBuffer 直接读 sync_camera:{uid}:frame:{cursor+1};
// 命中即最新,未命中再回退到 readLatestFrame 重定位。这样把 KEYS 调用
// 从每轮必发降到仅启动/丢帧时偶发。
//
//  watched set 由 get(uid) 自动维护;长时间无新帧的 uid 会被清理,避免内存泄漏。
Object.defineProperty(exports, "__esModule", { value: true });
exports.CachedFrameStore = void 0;
exports.getCachedFrame = getCachedFrame;
const frame_reader_1 = require("./frame-reader");
/**
 * 后台轮询 sync_camera 帧,为每个被观看的 uid 缓存最新帧。
 *
 * 用法:
 *   const store = new CachedFrameStore(redis);
 *   await store.start();                  // 启动后台扫描
 *   const frame = store.get(uid);         // 同步返回缓存(无 Redis 调用)
 *   store.stop();                          // 停止扫描
 */
class CachedFrameStore {
    constructor(redis, opts = {}) {
        /** 诊断开关:控制 cursor HIT/MISS 高频日志;KEYS/tick 总耗时始终打。 */
        this.verbose = process.env.OPENSIM_FRAMECACHE_VERBOSE !== '0';
        this.cache = new Map();
        /** uid → 已缓存的最大 frame_no。 */
        this.cursor = new Map();
        /** 当前有前端在观看的 uid 集合。 */
        this.watched = new Set();
        /** uid → 连续 N+1 未命中次数。 */
        this.missCount = new Map();
        /** uid → 最后一次读到新帧的时间戳。 */
        this.lastSeenAt = new Map();
        this.timer = null;
        this.refreshing = false;
        /** 运行时挂载的帧更新钩子(hookFrameUpdate 注册)。 */
        this.hooks = [];
        this.redis = redis;
        this.refreshMs = opts.refreshMs ?? 100;
        this.missThreshold = opts.missThreshold ?? 3;
        this.staleTimeoutMs = opts.staleTimeoutMs ?? 5000;
        this._setTimeout = opts.setTimeout ?? setTimeout;
        this._clearTimeout = opts.clearTimeout ?? clearTimeout;
        // 诊断:默认走 console.log(诊断期);测试可注入空 log 静默。
        // verbose 控制 cursor HIT/MISS 高频行;KEYS 命中与 tick 总耗时始终打。
        this.log = opts.log ?? ((m) => console.log(m));
        this.onFrameUpdate = opts.onFrameUpdate;
    }
    /** 启动后台扫描。立即触发一次,之后按 refreshMs 周期触发。 */
    start() {
        if (this.timer)
            return;
        void this.refresh();
    }
    /** 停止后台扫描。 */
    stop() {
        if (this.timer) {
            this._clearTimeout(this.timer);
            this.timer = null;
        }
    }
    /**
     * 同步返回指定 uid 的最新帧缓存。
     * 若该 uid 首次被请求,会加入 watched 集合并触发后台刷新。
     * @returns 缓存帧;uid 不在缓存中时返回 undefined(调用方应回退到 readLatestFrame)。
     */
    get(uid) {
        if (!this.watched.has(uid)) {
            this.watched.add(uid);
            // 立即为该 uid 发起一次后台刷新,避免等待下一轮周期。
            void this.refreshUid(uid);
        }
        return this.cache.get(uid);
    }
    /**
     * 运行时挂一个帧更新钩子(供 CameraWsServer 推流用)。
     * 返回解绑函数。可挂多个;每帧更新时所有钩子按挂载顺序触发。
     * 与构造选项 onFrameUpdate 并存,后者优先触发。
     */
    hookFrameUpdate(cb) {
        this.hooks.push(cb);
        return () => {
            const i = this.hooks.indexOf(cb);
            if (i >= 0)
                this.hooks.splice(i, 1);
        };
    }
    /** 标记某 uid 已停流(让 HTTP 返回 no_stream 而不是 stale 缓存)。 */
    invalidate(uid) {
        this.cache.delete(uid);
        this.cursor.delete(uid);
        this.watched.delete(uid);
        this.missCount.delete(uid);
        this.lastSeenAt.delete(uid);
    }
    async refresh() {
        if (this.refreshing) {
            // 上一次还没完成(罕见,Redis 阻塞时可能发生);跳过本轮,等下一周期。
            this.log(`[FrameCache] tick skipped (refreshing lock held) watched=${this.watched.size}`);
            this.scheduleNext();
            return;
        }
        this.refreshing = true;
        const t0 = Date.now();
        try {
            this.log(`[FrameCache] tick start watched=${this.watched.size}`);
            this.evictStaleUids();
            for (const uid of this.watched) {
                await this.refreshUid(uid);
            }
        }
        catch (e) {
            this.log(`[FrameCache] refresh error: ${e.message}`);
        }
        finally {
            this.refreshing = false;
            this.log(`[FrameCache] tick done ms=${Date.now() - t0} watched=${this.watched.size}`);
            this.scheduleNext();
        }
    }
    /** 清理长时间无新帧的 uid,避免关闭窗口后仍空转。 */
    evictStaleUids() {
        const now = Date.now();
        const evicted = [];
        for (const uid of this.watched) {
            const lastSeen = this.lastSeenAt.get(uid);
            if (lastSeen !== undefined && now - lastSeen > this.staleTimeoutMs) {
                this.invalidate(uid);
                evicted.push(uid);
            }
        }
        if (evicted.length > 0) {
            this.log(`[FrameCache] evict stale uids=[${evicted.join(',')}]`);
        }
    }
    /** 刷新单个 uid:优先 N+1 探测,失败则回退 readLatestFrame。 */
    async refreshUid(uid) {
        const lastFrameNo = this.cursor.get(uid);
        if (lastFrameNo !== undefined) {
            const next = await (0, frame_reader_1.readNextFrame)(this.redis, uid, lastFrameNo);
            if (next) {
                this.updateCache(uid, next);
                this.missCount.set(uid, 0);
                if (this.verbose) {
                    this.log(`[FrameCache] uid=${uid} cursor=${lastFrameNo} next=${next.frameNo} HIT`);
                }
                return;
            }
            // N+1 未命中:累计 missCount,未达阈值时不发 KEYS。
            const misses = (this.missCount.get(uid) ?? 0) + 1;
            this.missCount.set(uid, misses);
            if (this.verbose) {
                this.log(`[FrameCache] uid=${uid} cursor=${lastFrameNo} next=${lastFrameNo + 1} MISS missCount=${misses}`);
            }
            if (misses < this.missThreshold) {
                return;
            }
        }
        // 无 cursor 或连续 miss 过多:回退到 KEYS 重定位最新帧。
        const tKeys = Date.now();
        this.log(`[FrameCache] uid=${uid} KEYS start (fallback) cursor=${lastFrameNo ?? 'none'}`);
        const latest = await (0, frame_reader_1.readLatestFrame)(this.redis, uid);
        this.log(`[FrameCache] uid=${uid} KEYS done ms=${Date.now() - tKeys} latest=${latest ? latest.frameNo : 'null'}`);
        if (latest) {
            this.updateCache(uid, latest);
            this.missCount.set(uid, 0);
        }
        else {
            // 该 uid 当前无流,缓存 null 让 HTTP 返回 no_stream。
            this.cache.set(uid, null);
            this.cursor.delete(uid);
        }
    }
    updateCache(uid, frame) {
        this.cache.set(uid, frame);
        this.cursor.set(uid, frame.frameNo);
        this.lastSeenAt.set(uid, Date.now());
        // WS 推送:新帧入缓存即通知 CameraWsServer 广播给订阅者。
        // 构造选项钩子 + 运行时 hooks 都触发。
        this.onFrameUpdate?.(uid, frame);
        for (const h of this.hooks)
            h(uid, frame);
    }
    scheduleNext() {
        this.timer = this._setTimeout(() => void this.refresh(), this.refreshMs);
    }
}
exports.CachedFrameStore = CachedFrameStore;
/**
 * 创建一个 camera HTTP handler,优先从 CachedFrameStore 同步返回缓存;
 * 缓存未命中(uid 没扫描到)时回退到 readLatestFrame(保证首帧延迟可接受)。
 */
function getCachedFrame(store, redis, uid) {
    const cached = store.get(uid);
    if (cached !== undefined) {
        return Promise.resolve(cached);
    }
    // uid 不在缓存中(可能是首帧或新开 PiP 窗口):同步阻塞读一次,
    // 后台 refresh 会接管后续。这是 KEYS 的兜底路径,频率应该很低。
    return (0, frame_reader_1.readLatestFrame)(redis, uid);
}
