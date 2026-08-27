import * as THREE from 'three';
import { WGS84Projection, EntityState } from '../core/wgs84-projection';
export declare class CommLinkRenderer {
    private group;
    private projection;
    private active;
    private material;
    constructor(projection: WGS84Projection);
    getModel(): THREE.Group;
    setVisible(visible: boolean): void;
    /**
     * Drive the link visualization from the multi-entity state.
     * For each UAV with comm.inbox entries, spawn (or refresh) a link
     * from each distinct sender.
     */
    update(entities: Record<string, EntityState>): void;
    private spawnOrUpdate;
    private createLine;
    private updateLineGeometry;
    private tickFade;
    /** Spec 024: 立即清空所有通信线(关闭仿真重置)。
     *  update({}) 仅触发 fade(30 帧),且 reset 后不再有 sim:state 驱动 tickFade,
     *  残留线不会自然消失 —— 故需显式立即清空。 */
    clear(): void;
    dispose(): void;
}
//# sourceMappingURL=comm-link.d.ts.map