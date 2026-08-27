import * as THREE from 'three';
import { GimbalState } from '../core/wgs84-projection';
/**
 * Gimbal direction ray + FOV cone.
 *
 * Direction ray: a thin line from the UAV along the gimbal boresight,
 * sized so its tip lands on the terrain:
 *
 *     L_ray = altitudeAboveGround / sin(|tilt|)
 *
 * (tilt is the depression angle from horizontal; at -90 deg the ray points
 * straight down and L_ray == altitudeAboveGround; at shallow tilts it grows
 * but is capped so it doesn't shoot past the horizon). This fixes the
 * original bug where the ray had a fixed 330-unit length and stopped in
 * mid-air when a UAV flew high.
 *
 * FOV cone: a standard right cone along the boresight, sized so its NEAR
 * rim — the base edge pointing most steeply at the ground — sits on the
 * terrain:
 *
 *     L_cone = agl * cos(fov/2) / sin(|tilt| + fov/2)
 *
 * The cone's radius is tan(fov/2) * L_cone, so its true 3D half-angle
 * stays exactly fov/2 — it remains an honest FOV indicator at every tilt.
 *
 * Why the near rim and not the far rim: a tilted cone's circular base
 * cannot have both its near edge (toward nadir) and far edge (toward
 * horizon) on a flat ground at once (only at |tilt| = 90 deg do they
 * coincide). Sizing for the FAR rim — the previous behaviour,
 * L_old = agl*cos(fov/2)/sin(|tilt|-fov/2) — drove the cone far past the
 * aim point and pushed the near rim deep underground at shallow gimbal
 * angles, so the cone appeared to shoot through the terrain instead of
 * lying on it. Sizing for the near rim puts the cone's footprint on the
 * ground (near rim at the terrain, far rim trailing slightly above it),
 * which reads as a cone lying on the surface — no ground penetration.
 * The terrain mesh being opaque further hides any incidental overlap.
 *
 * Geometry note: the indicator is attached as a child of the UAV model and
 * lives in the UAV body frame where local -Z is "forward" (nose). The model
 * group is rotated by pan (Y) then tilt (X) so local -Z points along the
 * gimbal boresight. WGS84Projection maps 1 metre of altitude to 1 Three.js
 * unit (Y), so the altitude delta in metres is directly the ray length in
 * world units when looking straight down.
 */
export declare class GimbalIndicator {
    private model;
    private directionLine;
    private fovCone;
    private static readonly kMinTiltDeg;
    private static readonly kMaxRayM;
    private static readonly kMinRayM;
    constructor();
    /**
     * @param gimbalState  pan/tilt/fov from the kernel.
     * @param uavAltitudeM the UAV's current altitude (metres).
     * @param groundAltitudeM  terrain elevation under the UAV (metres). 0
     *        when no heightmap is loaded — in that case the ray falls back
     *        to a length proportional to the UAV altitude.
     */
    update(gimbalState: GimbalState, uavAltitudeM?: number, groundAltitudeM?: number): void;
    setFovVisible(visible: boolean): void;
    getModel(): THREE.Group;
}
//# sourceMappingURL=gimbal-indicator.d.ts.map