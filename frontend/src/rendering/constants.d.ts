/** Hex colour for each rendered entity kind (body + label). */
export declare const ENTITY_COLORS: {
    readonly uav: 2201331;
    readonly target: 16007990;
    readonly decoy: 16761095;
};
/** Emissive tint for each kind's body material (darker shade). */
export declare const ENTITY_EMISSIVE: {
    readonly uav: 870305;
    readonly target: 12000284;
    readonly decoy: 16740096;
};
/** CSS colour string for canvas-rendered label text. */
export declare const ENTITY_LABEL_CSS: {
    readonly uav: "white";
    readonly target: "white";
    readonly decoy: "#ffc107";
};
/** Trajectory-trail colour per kind (mirrors trajectory-trail.ts). */
export declare const TRAIL_COLORS: {
    readonly uav: 2201331;
    readonly target: 16007990;
    readonly decoy: 16750592;
};
/** Threat-zone overlay palette. */
export declare const ZONE_COLORS: {
    readonly killZone: 16724804;
    readonly jamZone: 10506495;
};
/** Default fill alpha per zone type. */
export declare const ZONE_ALPHA: {
    readonly killZone: 0.45;
    readonly jamZone: 0.3;
};
/**
 * Redis/WebSocket channel names. `app.ts` reads env overrides; these are
 * the defaults when the env var is absent. redis-client.ts falls back to
 * these same defaults when config omits a channel.
 */
export declare const CHANNELS: {
    readonly state: "sim:state";
    readonly commands: "sim:commands";
    readonly entityCommands: "entity:commands";
    readonly events: "sim:events";
    readonly progress: "sim:progress";
    readonly score: "sim:score";
    readonly renderId: "sim:render_id";
    readonly control: "sim:control";
};
/** UE 渲染服务模式:想定 JSON 的 Redis key(全局共享,所有 UE 实例同一份)。 */
export declare const SCENARIO_KEY = "sim:scenario";
//# sourceMappingURL=constants.d.ts.map