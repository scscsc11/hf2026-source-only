import * as THREE from 'three';
import { WGS84Projection, NavigationPathState } from '../core/wgs84-projection';
export interface PathConfig {
    color: number;
    lineWidth: number;
    waypointSize: number;
}
export declare class NavigationPath {
    private pathLine;
    private waypointMarkers;
    private projection;
    private config;
    constructor(projection: WGS84Projection, config?: Partial<PathConfig>);
    update(state: NavigationPathState): void;
    private clearWaypoints;
    setVisible(visible: boolean): void;
    getPathLine(): THREE.Line;
    getWaypointMarkers(): THREE.Mesh[];
    getLine(): THREE.Group;
}
//# sourceMappingURL=navigation-path.d.ts.map