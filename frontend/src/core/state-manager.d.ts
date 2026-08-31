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
     *  zones 留 undefined → ZoneController 收到 air_defense=[] → 经签名变更
     *  检测后触发 killZoneOverlay.update([]),清掉上一轮的红色杀伤区(依赖
     *  zone-controller 的签名比较逻辑,空列表也算签名变化)。 */
    reset(): void;
    subscribe(callback: StateSubscriber): () => void;
    private notifySubscribers;
}
//# sourceMappingURL=state-manager.d.ts.map