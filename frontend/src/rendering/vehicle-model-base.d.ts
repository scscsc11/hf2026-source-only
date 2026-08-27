import * as THREE from 'three';
import { WGS84Projection, TargetState } from '../core/wgs84-projection';
/** Palette + label for one vehicle variant. */
export interface VehicleModelConfig {
    /** Body colour (hex). */
    bodyColor: number;
    /** Body emissive tint (hex). */
    bodyEmissive: number;
    /** Roof colour (hex). */
    roofColor: number;
    /** Roof emissive tint (hex). */
    roofEmissive: number;
    /** Sprite label text + its canvas fill colour. */
    label: {
        text: string;
        fillStyle: string;
    };
    /** When true, add the diagonal black roof stripe (decoy marker). */
    decoyStripe?: boolean;
}
export declare const TARGET_VEHICLE_CONFIG: VehicleModelConfig;
export declare const DECOY_VEHICLE_CONFIG: VehicleModelConfig;
/**
 * Four-wheeled box-vehicle renderer. Body + roof + wheels + label are
 * shared; the `decoyStripe` flag adds the decoy-only diagonal marker.
 */
export declare class VehicleModelBase {
    protected model: THREE.Group;
    protected projection: WGS84Projection;
    constructor(projection: WGS84Projection, config: VehicleModelConfig);
    private createModel;
    update(state: TargetState): void;
    getModel(): THREE.Group;
    dispose(): void;
}
//# sourceMappingURL=vehicle-model-base.d.ts.map