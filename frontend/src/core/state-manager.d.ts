import { Waypoint, SimulationState } from './wgs84-projection';
export interface PathState {
    waypoints: Waypoint[];
    total_distance: number;
}
export type StateSubscriber = (state: SimulationState) => void;
export type { SimulationState };
export declare class StateManager {
    private state;
    private subscribers;
    constructor();
    private createDefaultState;
    updateState(message: Partial<SimulationState>): void;
    getState(): SimulationState;
    /** Spec 024: 重置为默认状态(关闭仿真时清空所有面板)。
     *  entities 显式设为 {} 以触发 onStateUpdate 的实体清空分支
     *  (entityManager/entityListPanel/commLinkRenderer/selection.reconcile/syncCameraPip);
     *  zones 留 undefined → ZoneController 收到空 bucket → 清威胁区。 */
    reset(): void;
    subscribe(callback: StateSubscriber): () => void;
    private notifySubscribers;
}
//# sourceMappingURL=state-manager.d.ts.map