import * as THREE from 'three';
import { WGS84Projection, UAVState } from '../core/wgs84-projection';
export declare class UAVModelRenderer {
    private model;
    private projection;
    constructor(projection: WGS84Projection);
    private createModel;
    update(state: UAVState): void;
    getModel(): THREE.Group;
}
//# sourceMappingURL=uav-model.d.ts.map