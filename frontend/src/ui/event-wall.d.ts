import { EventManager } from '../core/event-manager';
import { SimEvent } from '../core/wgs84-projection';
export interface EventTypeStyleEntry {
    label: string;
    colorClass: string;
}
export declare const EventTypeStyle: Record<string, EventTypeStyleEntry>;
/** Get a human-readable label for an event_type. */
export declare function eventLabel(eventType: string): string;
/** Get the CSS color class for an event_type. */
export declare function eventColorClass(eventType: string): string;
export type TeamFilterValue = 'white' | 'red' | 'blue' | 'all';
export declare class TeamFilter {
    private current;
    static getDefaultFilter(): TeamFilterValue;
    setFilter(value: TeamFilterValue): void;
    getFilter(): TeamFilterValue;
    /** Check if an event passes the current team filter. */
    passes(event: SimEvent): boolean;
}
export declare class EventWall {
    private listEl;
    private emptyEl;
    private filter;
    private unsubscribe?;
    constructor(eventManager: EventManager);
    private setupFilterButtons;
    private render;
    private renderFiltered;
    private renderEventItem;
    private payloadSummary;
    /** Dispose event subscriptions. */
    dispose(): void;
}
//# sourceMappingURL=event-wall.d.ts.map