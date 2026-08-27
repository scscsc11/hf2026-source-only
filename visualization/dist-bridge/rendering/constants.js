"use strict";
// SPDX-License-Identifier: MIT
//
// Spec 020 T7: single source of truth for rendering colours and Redis
// channel names. Before this file, hex colours were duplicated across
// vehicle-model / decoy-model / uav-model / trajectory-trail /
// *-zone-overlay (each defining its own 0xRRGGBB), and channel names
// were re-derived from env in app.ts AND defaulted again in
// redis-client.ts. Both are now imported from here.
//
// Colours are the visual identity of each entity kind; keeping them in
// one place means a palette tweak no longer chases the same literal
// through half a dozen files. The values below are the historical ones
// (red/green colour-blind safe) — T7 changes nothing visual.
Object.defineProperty(exports, "__esModule", { value: true });
exports.SCENARIO_KEY = exports.CHANNELS = exports.ZONE_ALPHA = exports.ZONE_COLORS = exports.TRAIL_COLORS = exports.ENTITY_LABEL_CSS = exports.ENTITY_EMISSIVE = exports.ENTITY_COLORS = void 0;
/** Hex colour for each rendered entity kind (body + label). */
exports.ENTITY_COLORS = {
    uav: 0x2196f3, // blue
    target: 0xf44336, // red
    decoy: 0xffc107, // amber/yellow
};
/** Emissive tint for each kind's body material (darker shade). */
exports.ENTITY_EMISSIVE = {
    uav: 0x0d47a1,
    target: 0xb71c1c,
    decoy: 0xff6f00,
};
/** CSS colour string for canvas-rendered label text. */
exports.ENTITY_LABEL_CSS = {
    uav: 'white',
    target: 'white',
    decoy: '#ffc107', // amber label text
};
/** Trajectory-trail colour per kind (mirrors trajectory-trail.ts). */
exports.TRAIL_COLORS = {
    uav: 0x2196f3, // blue
    target: 0xf44336, // red
    decoy: 0xff9800, // orange
};
/** Threat-zone overlay palette. */
exports.ZONE_COLORS = {
    killZone: 0xff3344, // air-defense (red)
    jamZone: 0xa050ff, // comm-jam (purple)
};
/** Default fill alpha per zone type. */
exports.ZONE_ALPHA = {
    killZone: 0.45,
    jamZone: 0.30,
};
/**
 * Redis/WebSocket channel names. `app.ts` reads env overrides; these are
 * the defaults when the env var is absent. redis-client.ts falls back to
 * these same defaults when config omits a channel.
 */
exports.CHANNELS = {
    state: 'sim:state',
    commands: 'sim:commands',
    entityCommands: 'entity:commands',
    events: 'sim:events',
    // Spec 024: 仿真器场景加载进度通道 (C++ 发布 → bridge 转发 → 前端进度条)。
    progress: 'sim:progress',
    // Spec 025: 实时评分通道 (Python 端 CoopTrackingEvaluator.score() 每 tick 发布)。
    score: 'sim:score',
    // Spec 028: UE 渲染控制通道。UE 发布 renderer_online/renderer_offline;
    // bridge 的 RenderScheduler 发布 assign/stop(按飞机粒度增量开关渲染)。
    // 数据流分离:UE 始终自取 sim:state(见 docs/ue-renderer-integration-requirements.md)。
    renderId: 'sim:render_id',
    // UE 渲染服务模式(render-service-mode):想定生命周期控制通道。
    // bridge 发布 load_scenario/end_scenario/shutdown(带 render_id 精确路由);
    // UE 发布 status 回报(idle/loading/rendering/teardown/shutting_down)。
    control: 'sim:control',
};
/** UE 渲染服务模式:想定 JSON 的 Redis key(全局共享,所有 UE 实例同一份)。 */
exports.SCENARIO_KEY = 'sim:scenario';
