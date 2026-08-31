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
 * FOV cone: a standard right cone along the boresight, extended to the same
 * length as the direction line so its central axis lands on the terrain at
 * the aim point:
 *
 *     L_cone = L_ray = agl / sin(|tilt|)
 *
 * The cone's radius is tan(fov/2) * L_cone, so its true 3D half-angle stays
 * exactly fov/2 — it remains an honest FOV indicator at every tilt.
 *
 * Why size to the aim point (centre axis) and not the near or far rim: a
 * tilted cone's circular base cannot have both its near edge (toward nadir)
 * and far edge (toward horizon) on flat ground at once. Sizing for the NEAR
 * rim (L = agl*cos(fov/2)/sin(|tilt|+fov/2)) stops the cone well short of
 * the aim point — at shallow tilts it was only a fraction of the direction
 * line, so the cone looked like it was floating in the air, never reaching
 * the ground the gimbal is aimed at. Sizing for the FAR rim
 * (L = agl*cos(fov/2)/sin(|tilt|-fov/2)) is undefined when |tilt| < fov/2
 * (the common shallow-gimbal case) and otherwise overshoots. Extending to
 * the aim point instead puts the cone's centre on the ground; the near
 * (lower) rim dips below the surface and is hidden by the opaque terrain
 * mesh, leaving a clear elliptical footprint where the cone intersects the
 * ground — i.e. the cone visibly meets the terrain.
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