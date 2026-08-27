import { StateManager } from './state-manager';
import { SimEvent } from './wgs84-projection';
export { normalizeSimState } from './state-normalizer';
export interface RedisMessage {
    type: 'subscribe' | 'unsubscribe' | 'publish' | 'message' | 'error' | 'session';
    channel?: string;
    channels?: string[];
    message?: string;
    error?: string;
    status?: string;
    scenario?: string | null;
    sessionId?: string | null;
}
/** Spec 024: sim:progress 加载进度载荷。 */
export interface LoadProgress {
    phase: string;
    pct: number;
    detail?: string;
}
/** Spec 024: 会话状态推送载荷。 */
export interface SessionUpdate {
    status: string;
    scenario?: string | null;
    sessionId?: string | null;
    error?: string | null;
}
/** Spec 025: sim:score 实时评分载荷(由 Python CoopTrackingEvaluator.score() 发布)。
 *  字段集兼容三个评分示例的差异化维度(profile_adversarial_swarm_search
 *  含 alive_rate,profile_multi_uav_coop_decoy 含 comm_* 等),统一为可选字段。 */
export interface ScoreSnapshot {
    /** 帧类型: 每 tick 为 "score",仿真结束末帧为 "score_final"。 */
    type: 'score' | 'score_final';
    /** 评分 profile 名(例 "uav_search_track_car" / "multi_uav_coop_decoy"
     *  / "adversarial_swarm_search")。前端可用于面板配色或调试。 */
    profile?: string;
    /** 仿真时间 (s)。 */
    sim_time?: number;
    /** 第几个 tick。 */
    tick?: number;
    /** 累计总分 (0-100)。 */
    total_score?: number;
    /** 是否通过(profile 阈值)。 */
    passed?: boolean;
    /** 维度明细子字典(键随 profile 变化)。 */
    dimension_scores?: Record<string, number>;
    /** 真实目标数。 */
    n_targets?: number;
    /** 已完成目标数。 */
    n_completed?: number;
    /** 存活率(对抗例专用)。 */
    alive_rate?: number;
    /** 是否为最终帧。 */
    final?: boolean;
    /** wall-clock unix 时间戳(s,小数)。 */
    ts?: number;
    /** 持久化 evaluation.json 路径(仅 final 帧携带)。 */
    evaluation_path?: string;
}
export interface RedisClientConfig {
    host: string;
    port: number;
    stateChannel: string;
    commandsChannel: string;
    entityCommandsChannel: string;
    eventsChannel?: string;
    entityIds?: Record<string, string>;
}
export declare class RedisClient {
    private ws;
    private config;
    private stateManager;
    private reconnectAttempts;
    private maxReconnectAttempts;
    private reconnectDelay;
    private isConnected;
    private onConnectionChange?;
    private onSimEvent?;
    private onProgressCb?;
    private onSessionCb?;
    private onScoreCb?;
    constructor(config: RedisClientConfig, stateManager: StateManager);
    /** Register callback for sim:events messages. */
    onEvent(callback: (event: SimEvent) => void): void;
    /** Spec 024 (T018): Register callback for sim:progress load-progress messages. */
    onProgress(callback: (p: LoadProgress) => void): void;
    /** Spec 024 (T048): Register callback for session-status pushes (type:'session'). */
    onSession(callback: (s: SessionUpdate) => void): void;
    /** Spec 025: Register callback for sim:score live score snapshots. */
    onScore(callback: (snapshot: ScoreSnapshot) => void): void;
    connect(): Promise<void>;
    disconnect(): void;
    publish(channel: string, message: object): void;
    subscribe(channel: string): void;
    onConnectionStateChanged(callback: (connected: boolean) => void): void;
    private handleMessage;
    private handleChannelMessage;
    /** Parse a raw JSON object into a SimEvent. Returns null if invalid. */
    private parseSimEvent;
    private attemptReconnect;
}
//# sourceMappingURL=redis-client.d.ts.map