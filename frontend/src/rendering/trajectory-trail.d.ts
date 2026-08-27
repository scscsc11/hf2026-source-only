import * as THREE from 'three';
import { WGS84Projection } from '../core/wgs84-projection';
/**
 * Trajectory trail renderer with a pre-allocated ring buffer.
 *
 * Each trail owns ONE THREE.BufferGeometry whose position attribute is a
 * fixed Float32Array. New points are appended and the draw range is
 * bumped; the buffer is reused for the trail's lifetime. This replaces
 * the previous per-update ``geometry.dispose() +
 * new BufferGeometry().setFromPoints()`` pattern which (across 10 UAV +
 * 30 vehicle trails) was a major frame-time contributor.
 *
 * One TrajectoryTrail = one coloured polyline for ONE entity. The kind
 * (uav / target / decoy) sets the default colour.
 */
export type TrailKind = 'uav' | 'target' | 'decoy';
export interface TrailConfig {
    maxPoints: number;
    color: number;
    lineWidth: number;
}
export declare class TrajectoryTrail {
    private line;
    private points;
    private projection;
    private config;
    private lastUpdate;
    private readonly kind;
    private positions;
    private positionAttr;
    constructor(projection: WGS84Projection, kind?: TrailKind, config?: Partial<TrailConfig>);
    /**
     * Append a (lat, lon, alt) sample to the trail. Throttled to one point
     * per ``updateIntervalMs``; near-duplicate points are dropped.
     */
    updatePosition(latitude: number, longitude: number, altitude: number, updateIntervalMs?: number): void;
    clear(): void;
    setVisible(visible: boolean): void;
    /** The single polyline object to add to the scene. */
    getLine(): THREE.Line;
    getKind(): TrailKind;
    getPointCount(): number;
}
//# sourceMappingURL=trajectory-trail.d.ts.map