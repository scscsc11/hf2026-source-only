import { SimulationState } from './state-manager';
import * as THREE from 'three';
export declare class ZoneController {
    private killZoneOverlay;
    private jamZoneOverlay;
    private killEventFX;
    private lastEntityStatus;
    private referenceLat;
    private referenceLon;
    private static readonly JAM_THROTTLE_MS;
    private lastAirDefenseSig;
    private lastJamUpdate;
    constructor(scene: THREE.Scene, referenceLat: number, referenceLon: number);
    /** Refresh overlays + trigger kill FX for active→destroyed transitions.
     *  `now` is exposed as an optional second argument so tests can drive a
     *  deterministic clock; in production it defaults to performance.now(). */
    update(state: SimulationState, now?: number): void;
    private findAnyUav;
    /** First-UAV position for camera auto-centering (reused by App). */
    findAnyUavPosition(state: SimulationState): {
        latitude: number;
        longitude: number;
        altitude: number;
    } | null;
}
//# sourceMappingURL=zone-controller.d.ts.map