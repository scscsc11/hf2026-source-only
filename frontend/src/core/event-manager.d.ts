import { SimEvent, Team } from './wgs84-projection';
export type EventSubscriber = (events: SimEvent[]) => void;
/** Normalize a team value per the contract rules:
 *  - undefined/null → 'white'
 *  - unknown string  → 'white' (with console.warn)
 */
export declare function normalizeTeam(raw?: Team | null): Team;
/**
 * EventManager — maintains a ring buffer of SimEvent messages, deduplicates
 * rapid identical events, and distributes them to subscribers.
 *
 * Mirrors the observer pattern used by StateManager.
 */
export declare class EventManager {
    private buffer;
    private capacity;
    private dedupInterval;
    private dedupKeys;
    private subscribers;
    constructor(options?: {
        capacity?: number;
        dedupIntervalMs?: number;
    });
    /** Push a new event. Duplicate events within the dedup window are silently
     *  dropped. Unknown event_type values are accepted (forward-compatible). */
    push(event: SimEvent): void;
    /** Current event buffer (newest last). */
    getAll(): SimEvent[];
    /** Spec 024: 清空事件缓冲(关闭仿真/新会话时调用,重置事件墙)。
     *  通知订阅者 → EventWall 重新渲染为空。 */
    clear(): void;
    /** Subscribe to event updates. Returns unsubscribe function. */
    subscribe(callback: EventSubscriber): () => void;
    private notifySubscribers;
}
//# sourceMappingURL=event-manager.d.ts.map