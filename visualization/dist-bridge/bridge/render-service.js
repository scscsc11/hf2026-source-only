"use strict";
// UE 渲染服务模式 —— bridge 侧的想定生命周期控制器。
//
// 背景:UE 新增 `-rendermode=service`(见 ueclaude-client docs/render-service-bridge-handoff.md)。
// service 模式 UE 不读本地 scenario.json,想定从 Redis key `sim:scenario` 拉取;
// 启动后进 IDLE,等 bridge 在 sim:control 频道发指令:
//   load_scenario → GET sim:scenario → spawn 实体 → RENDERING
//   end_scenario  → 销毁实体 → 回 IDLE(可接下一个想定,进程不退出)
//   shutdown      → 优雅退出(~11s)
// UE 每次状态迁移发布 status 到 sim:control:
//   {event:"status", render_id, state, ack, ok, detail, timestamp}
//
// 本模块职责:
//   1. bridge 生命周期常驻订阅 sim:control,维护 UE 注册表(renderId → 状态)。
//      —— pub/sub 无重放,订阅必须早于任何 UE 上线,且跨会话常驻,否则丢跟踪。
//   2. beginMission 后对「online 且 idle」的 UE 自动发 load_scenario(每 UE 每会话一次)。
//   3. load/end/shutdown 指令的 ack 等待(ok=false → reject 带 detail;超时 → reject)。
//   4. 状态迁移经 onStateChange 钩子外发(SimProcessManager 据此驱动 scheduler 门控)。
//
// 渲染开关(assign/stop)不在本模块 —— 那是 RenderScheduler 在 sim:render_id 上的职责。
Object.defineProperty(exports, "__esModule", { value: true });
exports.RenderServiceController = void 0;
const constants_1 = require("../rendering/constants");
/** action → 期望的终态(ok=true 且 state 到达终态才算 ack 完成)。 */
const TERMINAL_STATE = {
    load_scenario: 'rendering',
    end_scenario: 'idle',
    shutdown: 'shutting_down',
};
const VALID_STATES = new Set(['idle', 'loading', 'rendering', 'teardown', 'shutting_down']);
// ── RenderServiceController ───────────────────────────────────────────
/**
 * bridge 生命周期常驻。注册表只增不减(UE offline/shutdown 才删),
 * 跨会话保留 render_id —— 页面切赛题不退出 bridge,UE 全程可复用。
 */
class RenderServiceController {
    constructor(deps) {
        this.ues = new Map();
        this.unsub = null;
        this.started = false;
        /** beginMission 后置 true:自动给 online+idle 的 UE 发 load_scenario。 */
        this.missionArmed = false;
        this.deps = deps;
    }
    /** 启动:订阅 sim:control。必须在任何 UE 上线前调用(pub/sub 无重放)。 */
    async start() {
        if (this.started)
            return;
        this.started = true;
        this.unsub = await this.deps.subscribe(constants_1.CHANNELS.control, (msg) => {
            try {
                this.onMessage(msg);
            }
            catch (e) {
                this.warn(`onMessage error: ${e.message}`);
            }
        });
    }
    /** 停止:退订 + 拒绝全部在途指令 + 清空注册表(bridge 退出时调用)。 */
    async stop() {
        if (!this.started)
            return;
        this.started = false;
        this.missionArmed = false;
        if (this.unsub) {
            try {
                await this.unsub();
            }
            catch { /* best-effort */ }
            this.unsub = null;
        }
        for (const entry of this.ues.values())
            this.teardownEntry(entry);
        this.ues.clear();
    }
    /** UE renderer_online(由 RenderScheduler 钩子转发)。幂等。 */
    noteOnline(renderId) {
        if (this.ues.has(renderId))
            return;
        const entry = {
            renderId,
            state: 'unknown',
            loadState: 'none',
            inFlight: null,
            statusWarnTimer: null,
        };
        this.ues.set(renderId, entry);
        // online 后迟迟无 status:可能是老 file 模式 UE(硬切 service 后不兼容),告警提示。
        const delay = this.deps.statusWarnDelayMs ?? 90000;
        entry.statusWarnTimer = setTimeout(() => {
            entry.statusWarnTimer = null;
            if (entry.state === 'unknown') {
                this.warn(`renderer ${renderId} online but no status on sim:control after ${delay}ms (legacy file-mode UE? service mode required)`);
            }
        }, delay);
        this.info(`renderer noted online: ${renderId}`);
        // status:idle 可能先于 renderer_online 到达(已隐式注册),这里补一次自动 load 检查。
        this.maybeAutoLoad(entry);
    }
    /** UE renderer_offline / 崩溃(由 RenderScheduler 钩子转发)。 */
    noteOffline(renderId) {
        const entry = this.ues.get(renderId);
        if (!entry)
            return;
        this.teardownEntry(entry);
        this.ues.delete(renderId);
        this.info(`renderer noted offline: ${renderId}`);
    }
    /** 会话开始:武装自动 load;所有已注册 UE 的 load 进度重置,idle 的立即触发。 */
    beginMission() {
        this.missionArmed = true;
        for (const entry of this.ues.values()) {
            entry.loadState = 'none';
            this.maybeAutoLoad(entry);
        }
    }
    /** 会话结束:解除自动 load(end_scenario 由调用方按需发,本方法不主动发)。 */
    endMission() {
        this.missionArmed = false;
    }
    /** 查询 UE 当前状态;undefined = 未注册(不在线)。 */
    getState(renderId) {
        return this.ues.get(renderId)?.state;
    }
    listOnline() {
        return [...this.ues.keys()];
    }
    listRendering() {
        return [...this.ues.values()].filter((e) => e.state === 'rendering').map((e) => e.renderId);
    }
    /** 发 load_scenario 并等 ack:ok=true 且 state=rendering → resolve。 */
    loadScenario(renderId) {
        return this.command(renderId, 'load_scenario', this.deps.loadTimeoutMs ?? 60000);
    }
    /** 发 end_scenario 并等 ack:ok=true 且 state=idle → resolve。 */
    endScenario(renderId) {
        return this.command(renderId, 'end_scenario', this.deps.endTimeoutMs ?? 30000);
    }
    /** 发 shutdown 并等 ack:ok=true 且 state=shutting_down → resolve(进程退出不等)。 */
    shutdown(renderId) {
        return this.command(renderId, 'shutdown', this.deps.shutdownTimeoutMs ?? 15000);
    }
    // ── 内部 ──────────────────────────────────────────────────────────
    /** 发指令 + 登记在途 ack 等待。每 UE 同时只允许一个在途指令。 */
    async command(renderId, action, timeoutMs) {
        const entry = this.ues.get(renderId);
        if (!entry)
            throw new Error(`unknown renderer ${renderId} (not online)`);
        if (entry.inFlight)
            throw new Error(`command in flight for ${renderId}: ${entry.inFlight.action}`);
        const ackPromise = new Promise((resolve, reject) => {
            entry.inFlight = {
                action,
                resolve,
                reject,
                timer: setTimeout(() => {
                    entry.inFlight = null;
                    // 超时 = UE 不可达(Ctrl+C/崩溃/网络断,协议无心跳,指令超时是唯一判据)。
                    // reject 前 evict:从注册表移除 + 通知 scheduler 回收飞机。否则死 UE
                    // 滞留偷飞机,且下个会话还会 auto-load 它 → 再次超时(日志里 c9a 10 次重试)。
                    this.evict(renderId, `${action} timeout`);
                    reject(new Error(`${action} timeout after ${timeoutMs}ms (render_id=${renderId})`));
                }, timeoutMs),
            };
        });
        try {
            await this.deps.publish(constants_1.CHANNELS.control, JSON.stringify({ action, render_id: renderId }));
        }
        catch (e) {
            clearTimeout(entry.inFlight.timer);
            entry.inFlight = null;
            throw e;
        }
        return ackPromise;
    }
    /** 处理 sim:control 频道的 status 回报。 */
    onMessage(raw) {
        let msg;
        try {
            msg = JSON.parse(raw);
        }
        catch {
            this.warn('ignoring non-JSON control message');
            return;
        }
        if (msg.event !== 'status' || !msg.render_id || !msg.state)
            return;
        if (!VALID_STATES.has(msg.state)) {
            this.warn(`ignoring status with unknown state: ${msg.state}`);
            return;
        }
        const renderId = msg.render_id;
        const state = msg.state;
        // status 可能先于 renderer_online 到达(UE 启动时两者近乎同时)——隐式注册。
        let entry = this.ues.get(renderId);
        if (!entry) {
            entry = { renderId, state: 'unknown', loadState: 'none', inFlight: null, statusWarnTimer: null };
            this.ues.set(renderId, entry);
            this.info(`renderer registered via status: ${renderId}`);
        }
        if (entry.statusWarnTimer) {
            clearTimeout(entry.statusWarnTimer);
            entry.statusWarnTimer = null;
        }
        entry.state = state;
        // ack 结算:ack 字段匹配在途指令;ok=false → reject(detail 带原因);
        // ok=true 且到达终态 → resolve(中间态如 loading 继续等)。
        if (entry.inFlight && msg.ack === entry.inFlight.action) {
            const flight = entry.inFlight;
            if (msg.ok === false) {
                clearTimeout(flight.timer);
                entry.inFlight = null;
                flight.reject(new Error(`${flight.action} rejected by ${renderId}: ${msg.detail ?? 'no detail'}`));
            }
            else if (state === TERMINAL_STATE[flight.action]) {
                clearTimeout(flight.timer);
                entry.inFlight = null;
                flight.resolve();
            }
        }
        this.onStateChange?.(renderId, state);
        // shutting_down = 进程将退出(~11s),从注册表移除(下次上线是新 render_id)。
        if (state === 'shutting_down') {
            this.teardownEntry(entry);
            this.ues.delete(renderId);
            return;
        }
        if (state === 'idle')
            this.maybeAutoLoad(entry);
    }
    /** 自动 load 条件:mission 武装 + idle + 本会话未尝试过 + 无在途指令。 */
    maybeAutoLoad(entry) {
        if (!this.missionArmed)
            return;
        if (entry.state !== 'idle')
            return;
        if (entry.loadState !== 'none')
            return;
        if (entry.inFlight)
            return;
        entry.loadState = 'loading';
        this.info(`auto load_scenario: ${entry.renderId}`);
        const p = this.loadScenario(entry.renderId);
        p.then(() => { entry.loadState = 'loaded'; })
            .catch((e) => {
            entry.loadState = 'failed';
            this.warn(`auto load_scenario failed (${entry.renderId}): ${e.message}`);
        });
        // 兜底:超时 reject 与 .catch 链的微任务时序竞争可能导致 Node 误报
        // unhandledRejection;额外挂一个空 catch 吸收(fire-and-forget 不应抛未捕获)。
        p.catch(() => { });
    }
    /**
     * 驱逐不可达 UE(指令超时):拒绝在途指令 + 清告警定时器 + 移出注册表 +
     * 通知 scheduler 回收飞机重分配。幂等(已不在注册表则 no-op)。
     */
    evict(renderId, reason) {
        const entry = this.ues.get(renderId);
        if (!entry)
            return;
        this.teardownEntry(entry);
        this.ues.delete(renderId);
        this.warn(`renderer evicted: ${renderId} (${reason})`);
        this.onRendererEvicted?.(renderId);
    }
    /** 清理条目:拒绝在途指令 + 清告警定时器。 */
    teardownEntry(entry) {
        if (entry.inFlight) {
            clearTimeout(entry.inFlight.timer);
            const flight = entry.inFlight;
            entry.inFlight = null;
            flight.reject(new Error(`renderer ${entry.renderId} gone during ${flight.action}`));
        }
        if (entry.statusWarnTimer) {
            clearTimeout(entry.statusWarnTimer);
            entry.statusWarnTimer = null;
        }
    }
    warn(msg) {
        (this.deps.warn ?? ((m) => console.warn(`[RenderService] ${m}`)))(msg);
    }
    info(msg) {
        (this.deps.info ?? ((m) => console.log(`[RenderService] ${m}`)))(msg);
    }
}
exports.RenderServiceController = RenderServiceController;
