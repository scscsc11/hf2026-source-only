"use strict";
// RenderScheduler —— bridge 侧的 UE 渲染调度层(UE 渲染服务模式适配版)。
//
// 职责(契约见 docs/ue-renderer-integration-requirements.md §3 + render-service 交接文档):
//   1. bridge 生命周期常驻订阅 sim:render_id,接收 UE 的 renderer_online/renderer_offline。
//      —— pub/sub 无重放,订阅必须跨会话常驻,否则切赛题后常驻 UE 对新会话不可见。
//   2. 每会话 setMission(飞机池 + 超出本地容量的 excess),ready UE 之间贪心分配。
//   3. readiness 门控:只有 RenderServiceController 确认 status:rendering 的 UE
//      才参与分配(service 模式 UE 在非 RENDERING 态丢弃 assign/stop)。
//   4. 每个 ready UE 精确收口:assign(分配集) + stop(missionAll - 分配集)。
//      service 模式 UE load 后 spawn 全部实体,per-UE stop 补集防止多 UE 各自越界
//      渲染(stop 幂等,对"默认 OFF"的 UE 无害)。
//   5. UE 崩溃(进程退出,由 watchExit 触发)→ 收回其飞机,重分配给其他 ready UE。
//
// 数据流分离:UE 始终自取 sim:state 拿全量飞机状态,本模块只管"开关哪架"。
//
// 容量来源:优先用 render-ctl plan 提供的 max_aircraft(capacityOverride),
// 回退到 UE 上报的 max_aircraft。UE 端 max_aircraft 常硬编码(实测上报 2),
// 与 registry 配置不一致会导致第 N 架飞机留 pending 池无人渲染,故以 plan 值为准。
//
// 生命周期:
//   attach()            → 订阅 sim:render_id(bridge 启动时,常驻)
//   setMission(ac,exc)  → 会话开始:飞机入 pending 池(可重复调用,先 clearMission)
//   clearMission()      → 会话结束:清飞机池(UE 注册表保留 —— 常驻轮换)
//   detach()            → 退订 + 全清(bridge 退出时)
Object.defineProperty(exports, "__esModule", { value: true });
exports.RenderScheduler = void 0;
exports.planAssignment = planAssignment;
const constants_1 = require("../rendering/constants");
// ── 纯逻辑:贪心分配(便于单测) ──────────────────────────────────────────
/**
 * 把 aircraft 按 renderers 容量贪心分配(填满第一个再溢出到下一个)。
 * 不变性:每架飞机至多出现在一个 UE 的分配里。
 */
function planAssignment(aircraft, renderers) {
    const result = new Map();
    let idx = 0;
    for (const r of renderers) {
        const bucket = [];
        while (idx < aircraft.length && bucket.length < r.maxAircraft) {
            bucket.push(aircraft[idx]); // idx < length 已由 while 条件保证
            idx++;
        }
        if (bucket.length > 0)
            result.set(r.renderId, bucket);
    }
    return result;
}
// ── RenderScheduler 类 ────────────────────────────────────────────────
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
class RenderScheduler {
    constructor(deps) {
        /** renderId → 容量信息(在线 UE;常驻,跨会话保留)。 */
        this.renderers = new Map();
        /** pid → renderId(UE 进程崩溃时反查)。 */
        this.pidToRenderId = new Map();
        /** 待分配飞机(无 ready UE 承接或被收回)。每会话 setMission 填充。 */
        this.pending = [];
        /** 已分配:renderId → aircraft[](stop/重分配时增量操作)。 */
        this.assigned = new Map();
        /** 本会话全部 gimbal 飞机(= aircraft ∪ excess),per-UE stop 补集的全集。 */
        this.missionAll = new Set();
        /** ready UE(收到 status:rendering;只有它们参与分配)。 */
        this.ready = new Set();
        /** subscribe 返回的 unsubscribe 句柄。 */
        this.unsub = null;
        this.attached = false;
        this.deps = deps;
    }
    /** attach:订阅 sim:render_id。bridge 启动时调用一次,常驻。幂等。 */
    async attach() {
        if (this.attached)
            return;
        this.attached = true;
        this.unsub = await this.deps.subscribe(constants_1.CHANNELS.renderId, (msg) => this.onMessage(msg).catch((e) => this.warn(`onMessage error: ${e.message}`)));
    }
    /** detach:退订 + 清空全部状态(含 UE 注册表)。bridge 退出时调用。幂等。 */
    async detach() {
        if (!this.attached)
            return;
        this.attached = false;
        if (this.unsub) {
            try {
                await this.unsub();
            }
            catch { /* best-effort */ }
            this.unsub = null;
        }
        this.renderers.clear();
        this.pidToRenderId.clear();
        this.assigned.clear();
        this.pending = [];
        this.missionAll.clear();
        this.ready.clear();
    }
    /** 会话开始:飞机入 pending 池,记录 missionAll 全集。重复调用前先 clearMission。 */
    setMission(aircraft, excessUavs) {
        this.pending = [...aircraft];
        this.missionAll = new Set([...aircraft, ...(excessUavs ?? [])]);
    }
    /**
     * 会话结束:清飞机池与分配记录;UE 注册表/ready 集合保留(常驻轮换)。
     * 调用方(Manager)在此之前已发 end_scenario,UE 回 IDLE 后经
     * setRendererNotReady 退出 ready 集合。
     */
    clearMission() {
        this.pending = [];
        this.missionAll.clear();
        // 不 assigned.clear():注册表保留的 UE 下次 ready 时 drainPending 依赖
        // assigned 条目存在。改为把每个在线 UE 的分配集重置为空。
        for (const rid of this.renderers.keys())
            this.assigned.set(rid, new Set());
    }
    /** 注册一个 UE 进程(spawn 时调用,建立 pid↔renderId 映射)。 */
    registerUeProcess(pid, renderId) {
        this.pidToRenderId.set(pid, renderId);
    }
    /** UE 进程崩溃(由 watchExit 触发)。按 pid 反查 renderId 并收回飞机重分配。 */
    async onUeCrash(pid) {
        const renderId = this.pidToRenderId.get(pid);
        if (renderId === undefined)
            return;
        this.pidToRenderId.delete(pid);
        await this.removeRenderer(renderId);
    }
    /** 处理 sim:render_id 频道的消息(renderer_online / renderer_offline)。 */
    async onMessage(raw) {
        let msg;
        try {
            msg = JSON.parse(raw);
        }
        catch {
            this.warn(`ignoring non-JSON render_id message`);
            return;
        }
        if (msg.event === 'renderer_online' && msg.render_id) {
            await this.addRenderer(msg.render_id, typeof msg.max_aircraft === 'number' ? msg.max_aircraft : 0);
        }
        else if (msg.event === 'renderer_offline' && msg.render_id) {
            await this.removeRenderer(msg.render_id);
        }
    }
    /** UE 进 RENDERING(由 RenderServiceController 状态钩子驱动):参与分配。 */
    async setRendererReady(renderId) {
        if (!this.renderers.has(renderId))
            return; // 未 online,忽略
        if (this.ready.has(renderId))
            return; // 幂等
        this.ready.add(renderId);
        this.info(`renderer ready: ${renderId}`);
        await this.drainPending();
    }
    /**
     * 驱逐不可达 UE(指令超时 = Ctrl+C/崩溃;由 RenderServiceController.onRendererEvicted 驱动)。
     * 委托 removeRenderer:删 renderers/assigned/ready、best-effort publish stop、
     * 飞机回 pending、onRendererOffline 钩子(service.noteOffline 幂等)、drainPending
     * 重分配给其他 ready UE(若无则飞机留 pending,等新 UE 上线接手)。
     */
    async evictRenderer(renderId) {
        await this.removeRenderer(renderId);
    }
    /** UE 离开 RENDERING(end_scenario/teardown):退出分配(注册保留,飞机收回 pending)。 */
    async setRendererNotReady(renderId) {
        if (!this.ready.has(renderId))
            return;
        this.ready.delete(renderId);
        this.info(`renderer not ready: ${renderId}`);
        // UE 销毁实体后不再渲染,其飞机收回 pending(等本 UE 重新 ready 或其他 UE 承接)。
        const taken = this.assigned.get(renderId);
        if (taken && taken.size > 0) {
            this.pending.push(...taken);
            this.assigned.set(renderId, new Set());
        }
    }
    /** 当前在线 UE 数(含未 ready;Manager 的 spawn-if-needed 用)。 */
    onlineCount() {
        return this.renderers.size;
    }
    /** 当前在线 UE 的总容量(含未 ready;Manager 的 spawn-if-needed 用)。 */
    totalOnlineCapacity() {
        let sum = 0;
        for (const r of this.renderers.values())
            sum += r.maxAircraft;
        return sum;
    }
    // ── 内部 ──────────────────────────────────────────────────────────
    /** UE 上线:记录容量(优先 capacityOverride)。不分配 —— 等 ready 门控。 */
    async addRenderer(renderId, reported) {
        if (this.renderers.has(renderId))
            return; // 幂等:重复 online 忽略
        const cap = this.deps.capacityOverride?.[renderId] ?? reported;
        const maxAircraft = cap > 0 ? cap : reported;
        if (maxAircraft <= 0) {
            this.warn(`renderer ${renderId} online with maxAircraft<=0 (reported=${reported}, override=${this.deps.capacityOverride?.[renderId]}); skipping`);
            return;
        }
        this.renderers.set(renderId, { renderId, maxAircraft });
        this.assigned.set(renderId, new Set());
        this.info(`renderer online: ${renderId} maxAircraft=${maxAircraft}`);
        this.onRendererOnline?.(renderId, maxAircraft);
    }
    /** UE 下线/崩溃:注销,收回其飞机入 pending,重分配给其他 ready UE,publish stop。 */
    async removeRenderer(renderId) {
        const info = this.renderers.get(renderId);
        if (!info)
            return;
        const taken = this.assigned.get(renderId);
        this.renderers.delete(renderId);
        this.assigned.delete(renderId);
        this.ready.delete(renderId);
        if (taken && taken.size > 0) {
            // 通知该 UE 停止这些飞机(尽力;UE 可能已死,redis publish 即返回)。
            const ac = [...taken];
            await this.publish({ event: 'stop', render_id: renderId, aircraft: ac });
            this.pending.push(...ac);
        }
        this.onRendererOffline?.(renderId);
        await this.drainPending();
    }
    /**
     * 把 pending 飞机按各 ready UE 容量贪心分配;
     * 对每个 ready UE publish assign(全量集) + stop(missionAll - 全量集) 精确收口。
     */
    async drainPending() {
        if (this.ready.size === 0)
            return;
        if (this.missionAll.size === 0)
            return; // 无活动 mission(clearMission 后),不发任何指令
        const renderers = [...this.renderers.values()]
            .filter((r) => this.ready.has(r.renderId))
            .sort((a, b) => a.renderId.localeCompare(b.renderId)); // 确定性顺序
        if (renderers.length === 0)
            return;
        // 各 UE 剩余容量 = maxAircraft - 已分配数。
        const remaining = new Map();
        for (const r of renderers) {
            remaining.set(r.renderId, r.maxAircraft - (this.assigned.get(r.renderId)?.size ?? 0));
        }
        const stillPending = [];
        for (const ac of this.pending) {
            // 找第一个有剩余容量的 ready UE(确定性,按 renderId 序)。
            const target = renderers.find((r) => (remaining.get(r.renderId) ?? 0) > 0);
            if (!target) {
                stillPending.push(ac);
                continue;
            }
            // 防御:assigned 条目应与 renderers 同步(addRenderer 建立),不存在则补。
            if (!this.assigned.has(target.renderId))
                this.assigned.set(target.renderId, new Set());
            this.assigned.get(target.renderId).add(ac);
            remaining.set(target.renderId, remaining.get(target.renderId) - 1);
        }
        this.pending = stillPending;
        // 对每个 ready UE 发全量 assign + stop 补集。UE 端 assign/stop 幂等
        // (契约 §7:重复 assign 同一架飞机 = 幂等无操作),重发全量安全。
        for (const r of renderers) {
            const all = this.assigned.get(r.renderId);
            if (!all)
                continue;
            const allocated = [...all];
            if (allocated.length > 0) { // 空 assign 无信息量,跳过(stop 补集照发)
                this.info(`assign renderer=${r.renderId} aircraft=${allocated.join(',')}`);
                await this.publish({ event: 'assign', render_id: r.renderId, aircraft: allocated });
            }
            const complement = [...this.missionAll].filter((ac) => !all.has(ac));
            if (complement.length > 0) {
                await this.publish({ event: 'stop', render_id: r.renderId, aircraft: complement });
            }
        }
    }
    publish(cmd) {
        return this.deps.publish(constants_1.CHANNELS.renderId, JSON.stringify(cmd));
    }
    warn(msg) {
        (this.deps.warn ?? ((m) => console.warn(`[RenderScheduler] ${m}`)))(msg);
    }
    info(msg) {
        (this.deps.info ?? ((m) => console.log(`[RenderScheduler] ${m}`)))(msg);
    }
}
exports.RenderScheduler = RenderScheduler;
